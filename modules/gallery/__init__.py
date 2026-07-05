"""Gallery module — the main image browser with SQLite indexing, metadata,
favorites, collections, search and a metadata side-panel.

This module is the CyberHub flagship: it owns the 3-column gallery
layout and most of the user-visible chrome (sidebar + grid + meta panel +
statusbar). Other modules (Viewer, Tools, Settings) are simpler pages
served inside the same topbar shell.

Settings (configured from the Settings page):
    folder              str       Image folder root (replaces gallery.py path arg)
    per_page            int       Images per page (default 200)
    background_reindex  bool      Periodically rescan the folder (default false)
    reindex_interval    int       Seconds between scans (default 30)
    thumb_workers       int|null  Thumbnail worker count (null = auto)
    verbose             bool      Debug logging from metadata reader

All settings are also accepted via gallery.py-style CLI args on hub.py
for the current session (--port, --listen, --share-network, --models,
--verbose); the values in settings.json are used when the args are not
given.
"""

import concurrent.futures
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path

from core import Module
from core.metadata import get_image_metadata, parse_sd_parameters
from core import metadata as _metadata_mod
from core.server import build_topbar, build_module_menu, theme_body_class, _BASE_CSS, _BASE_JS, HELP_OVERLAY_HTML

# ─── Constants ────────────────────────────────────────────────────────────────

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}
THUMB_SIZE = (300, 300)
# Note: the actual thumbnail directory name (".thumbs") and DB name ("cyberdelia.db")
# are set in on_startup() — kept hardcoded there because they need access to hub.resources_dir.
DEFAULT_PER_PAGE = 200       # overwritten from settings on startup
DEFAULT_REINDEX_INTERVAL = 30

try:
    from PIL import Image
    HAS_PIL = True
    LANCZOS = getattr(Image, "Resampling", Image).LANCZOS
except ImportError:
    HAS_PIL = False
    Image = None
    LANCZOS = None
    print("[WARN] Pillow not installed — pip install Pillow")

try:
    from send2trash import send2trash
    HAS_TRASH = True
except ImportError:
    HAS_TRASH = False
    print("[WARN] send2trash not installed — delete will permanently remove files. "
          "pip install send2trash")


def log_debug(msg):
    if getattr(_metadata_mod, "VERBOSE", False):
        print(f"[DEBUG] {msg}")


def safe_resolve(root, rel_path):
    """Resolve `rel_path` against `root`, returning None on traversal attempts."""
    try:
        root_abs = os.path.realpath(root)
        full = os.path.realpath(os.path.join(root, rel_path))
        if not full.startswith(root_abs + os.sep) and full != root_abs:
            log_debug(f"Path traversal blocked: {rel_path}")
            return None
        return full
    except (ValueError, OSError):
        return None


# ─── Parallel file reader ──────────────────────────────────────────────────────

def _read_file_payload(item):
    """Read metadata + image dimensions for one file. Pure / thread-safe — no DB,
    no shared mutable state — so it can run in a worker thread. `item` carries the
    bookkeeping fields the DB-write step needs; we just append (meta, w, h)."""
    full_path, full_dir, fname, fpath, fav, size, mtime, had_row = item
    meta = get_image_metadata(fpath)
    w = h = 0
    if HAS_PIL:
        try:
            with Image.open(fpath) as img:
                w, h = img.size
        except Exception:
            pass
    return (full_path, full_dir, fname, fav, size, mtime, had_row, meta, w, h)


# ─── Database ─────────────────────────────────────────────────────────────────

class GalleryDB:
    def __init__(self, db_path, thumb_dir, roots):
        """
        db_path:   absolute path to the SQLite database file
        thumb_dir: absolute path to the central thumbnail cache directory
        roots:     dict of {display_name: absolute_path} for image folders
        """
        self.db_path = db_path
        self.thumb_dir = thumb_dir
        self.roots = roots  # {"SD Output": "D:\\SD Images\\output", ...}
        self.lock = threading.Lock()
        self._local = threading.local()
        self.has_fts = False
        self.search_index_ready = False
        self._search_index_notice_shown = False
        self._create_tables()
        self._ensure_search_index()

    def _get_conn(self):
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.execute("PRAGMA busy_timeout=30000")
        return self._local.conn

    def optimize(self):
        """Compact + refresh the database. Returns a dict describing what ran.

        VACUUM rewrites the entire file, so on a large library it can take tens of
        seconds while reclaiming little — it's only worth it when meaningful free
        space exists. So we VACUUM only when >10% of pages are free AND at least
        ~5 MB would be reclaimed; otherwise we just refresh stats and compact the
        FTS search index (which keeps search fast). This makes the common case fast.
        """
        try:
            before = os.path.getsize(self.db_path)
        except OSError:
            before = 0
        conn = self._get_conn()
        did_vacuum = False
        did_fts = False
        with self.lock:
            conn.commit()  # close any implicit transaction
            try:
                page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                free = conn.execute("PRAGMA freelist_count").fetchone()[0]
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            except sqlite3.OperationalError:
                page_count = free = page_size = 0
            reclaimable = free * page_size
            want_vacuum = bool(page_count) and (free / page_count) > 0.10 and reclaimable > 5 * 1024 * 1024
            # Compact the FTS index (merges b-tree segments → faster search).
            if self.has_fts:
                try:
                    conn.execute("INSERT INTO gallery_search(gallery_search) VALUES('optimize')")
                    conn.commit()
                    did_fts = True
                except sqlite3.OperationalError:
                    pass
            old_iso = conn.isolation_level
            conn.isolation_level = None  # autocommit — required for VACUUM
            try:
                if want_vacuum:
                    conn.execute("VACUUM")
                    did_vacuum = True
                conn.execute("ANALYZE")
            finally:
                conn.isolation_level = old_iso
        try:
            after = os.path.getsize(self.db_path)
        except OSError:
            after = before
        return {"before": before, "after": after, "vacuumed": did_vacuum, "fts": did_fts}

    def _create_tables(self):
        conn = self._get_conn()
        with self.lock:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS folders (
                    path TEXT PRIMARY KEY, name TEXT, parent TEXT,
                    file_count INTEGER DEFAULT 0, has_subfolders INTEGER DEFAULT 0,
                    mtime REAL, cover_image TEXT
                );
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY, folder TEXT, name TEXT, ext TEXT,
                    size INTEGER, mtime REAL, width INTEGER, height INTEGER,
                    has_metadata INTEGER DEFAULT 0, metadata_json TEXT,
                    favorite INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    count INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS file_tags (
                    file_path TEXT,
                    tag_id INTEGER,
                    tag_type TEXT DEFAULT 'prompt',
                    PRIMARY KEY (file_path, tag_id)
                );
                CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder);
                CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent);
                CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
                CREATE INDEX IF NOT EXISTS idx_file_tags_tag ON file_tags(tag_id);
                CREATE INDEX IF NOT EXISTS idx_file_tags_file ON file_tags(file_path);
                CREATE TABLE IF NOT EXISTS collections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    color TEXT DEFAULT '#4a9eff',
                    created REAL
                );
                CREATE TABLE IF NOT EXISTS file_collections (
                    file_path TEXT,
                    collection_id INTEGER,
                    added REAL,
                    PRIMARY KEY (file_path, collection_id)
                );
                CREATE INDEX IF NOT EXISTS idx_fc_collection ON file_collections(collection_id);
                CREATE INDEX IF NOT EXISTS idx_fc_file ON file_collections(file_path);
            """)
            # Migration: add favorite column to existing DBs (must run before index creation)
            try:
                conn.execute("ALTER TABLE files ADD COLUMN favorite INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_favorite ON files(favorite)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime)")
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS gallery_search USING fts5(
                        path UNINDEXED,
                        folder UNINDEXED,
                        name,
                        tags,
                        metadata,
                        tokenize = 'unicode61 tokenchars ''_:-.'''
                    )
                """)
                self.has_fts = True
            except sqlite3.OperationalError as e:
                self.has_fts = False
                print(f"[SEARCH] FTS5 unavailable; using legacy LIKE search ({e})")
            conn.commit()

    def index_tree(self, force=False, prune_deleted=None, workers=None):
        conn = self._get_conn()
        t0 = time.time(); new_files = 0; scanned_files = 0; batch_changes = 0; skipped_dirs = 0
        pruned_files = 0; pruned_folders = 0
        read_time = db_time = 0.0
        last_progress = t0
        # Resolve worker count for the parallel metadata/dimension read step.
        # 0/None = auto. Reads are I/O-bound (file headers + PIL open), so a few
        # more threads than cores helps; capped to keep network drives sane.
        if not workers:
            workers = min(8, (os.cpu_count() or 4) + 2)
        workers = max(1, int(workers))
        FLUSH = 500          # files buffered before a parallel read + DB write
        worklist = []
        if prune_deleted is None:
            prune_deleted = force
        all_folders = set() if prune_deleted else None
        all_files = set() if prune_deleted else None
        scanned_roots = set()
        batch_size = 250

        def flush_worklist():
            """Read the buffered files' metadata/dimensions in parallel, then write
            the results to SQLite serially (one writer — SQLite has a single-writer
            model). Returns nothing; mutates the enclosing counters."""
            nonlocal new_files, batch_changes, last_progress, read_time, db_time
            if not worklist:
                return
            items = worklist[:]
            worklist.clear()
            tr = time.time()
            if workers > 1 and len(items) > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                    results = list(ex.map(_read_file_payload, items))
            else:
                results = [_read_file_payload(it) for it in items]
            read_time += time.time() - tr
            td = time.time()
            # Write in small sub-batches, releasing self.lock between each so live
            # Gallery read requests (get_subfolders/get_files use the same lock) can
            # interleave. Holding the lock across the whole 500-file flush made the
            # gallery appear to "not load" during a heavy reindex.
            LOCK_CHUNK = 25
            for start in range(0, len(results), LOCK_CHUNK):
                sub = results[start:start + LOCK_CHUNK]
                with self.lock:
                    for (full_path, full_dir, fname, fav, size, mtime, had_row, meta, w, h) in sub:
                        conn.execute("""INSERT OR REPLACE INTO files
                            (path, folder, name, ext, size, mtime, width, height, has_metadata, metadata_json, favorite)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (full_path, full_dir, fname, Path(fname).suffix.lower(), size,
                             mtime, w, h, 1 if meta else 0, json.dumps(meta) if meta else None, fav))
                        self._index_tags(conn, full_path, meta)
                        self._index_search(conn, full_path, full_dir, fname, meta,
                                           replace=had_row and not force)
                        batch_changes += 1
                        new_files += 1
                        if batch_changes >= batch_size:
                            conn.commit(); batch_changes = 0
            db_time += time.time() - td
            now = time.time()
            if now - last_progress >= 5:
                print(f"[INDEX] {new_files} new/updated files indexed so far "
                      f"({scanned_files} scanned, {workers} read workers)...")
                last_progress = now

        # A force reindex re-adds every file, so clear the FTS index once up front
        # and let _index_search insert without the per-row DELETE (avoids O(n^2)).
        if force and self.has_fts:
            self.search_index_ready = False
            with self.lock:
                try: conn.execute("DELETE FROM gallery_search")
                except sqlite3.OperationalError: pass
        for root_name, root_abs in self.roots.items():
            if not os.path.isdir(root_abs):
                print(f"[INDEX] Root unavailable, skipping: {root_name} -> {root_abs}")
                continue
            print(f"[INDEX] Scanning root: {root_name}")
            scanned_roots.add(root_name)
            for dirpath, dirnames, filenames in os.walk(root_abs):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                rel_dir = os.path.relpath(dirpath, root_abs).replace("\\", "/")
                if rel_dir == ".":
                    full_dir = root_name
                else:
                    full_dir = root_name + "/" + rel_dir
                parent = os.path.dirname(full_dir).replace("\\", "/") if "/" in full_dir else ""
                if prune_deleted:
                    all_folders.add(full_dir)
                images = [f for f in filenames if Path(f).suffix.lower() in IMAGE_EXTENSIONS]
                has_subs = 1 if dirnames else 0
                cover = (full_dir + "/" + images[0]) if images else None
                try:
                    dir_mtime = os.path.getmtime(dirpath)
                except OSError:
                    dir_mtime = 0
                if not force and not prune_deleted:
                    with self.lock:
                        folder_row = conn.execute("SELECT file_count, mtime FROM folders WHERE path=?", (full_dir,)).fetchone()
                    if folder_row and folder_row[0] == len(images) and abs((folder_row[1] or 0) - dir_mtime) < 0.01:
                        skipped_dirs += 1
                        scanned_files += len(images)
                        now = time.time()
                        if (scanned_files and scanned_files % 2500 == 0) or now - last_progress >= 30:
                            print(
                                f"[INDEX] Scanned {scanned_files} files "
                                f"({new_files} new/updated so far, {skipped_dirs} unchanged folders skipped)..."
                            )
                            last_progress = now
                        continue
                with self.lock:
                    conn.execute("INSERT OR REPLACE INTO folders VALUES (?,?,?,?,?,?,?)",
                        (full_dir, os.path.basename(dirpath) if rel_dir != "." else root_name,
                         parent, len(images), has_subs, dir_mtime, cover))
                    if not force and not prune_deleted:
                        current_paths = {
                            root_name + "/" + os.path.relpath(os.path.join(dirpath, image_name), root_abs).replace("\\", "/")
                            for image_name in images
                        }
                        stale_paths = [
                            r[0] for r in conn.execute("SELECT path FROM files WHERE folder=?", (full_dir,)).fetchall()
                            if r[0] not in current_paths
                        ]
                        if stale_paths:
                            pruned_files += self._prune_file_records(conn, stale_paths)
                for fname in images:
                    fpath = os.path.join(dirpath, fname)
                    rel_from_root = os.path.relpath(fpath, root_abs).replace("\\", "/")
                    full_path = root_name + "/" + rel_from_root
                    scanned_files += 1
                    if prune_deleted:
                        all_files.add(full_path)
                    try: stat = os.stat(fpath)
                    except OSError: continue
                    with self.lock:
                        row = conn.execute("SELECT mtime, favorite FROM files WHERE path=?", (full_path,)).fetchone()
                    if not force:
                        if row and abs(row[0] - stat.st_mtime) < 0.01:
                            now = time.time()
                            if scanned_files % 2500 == 0 or now - last_progress >= 30:
                                print(
                                    f"[INDEX] Scanned {scanned_files} files "
                                    f"({new_files} new/updated so far)..."
                                )
                                last_progress = now
                            continue
                    # Defer the expensive metadata/dimension read to a parallel flush.
                    fav = row[1] if row else 0
                    worklist.append((full_path, full_dir, fname, fpath, fav,
                                     stat.st_size, stat.st_mtime, bool(row)))
                    if len(worklist) >= FLUSH:
                        flush_worklist()
        flush_worklist()  # drain any remaining buffered files
        print(
            f"[INDEX] Scan complete: {scanned_files} image files seen, "
            f"{new_files} new/updated, {skipped_dirs} unchanged folders skipped. "
            "Finalizing database..."
        )
        with self.lock:
            if batch_changes:
                conn.commit()
            stale_roots = self._prune_unconfigured_roots(conn)
            pruned_files += stale_roots[0]
            pruned_folders += stale_roots[1]
            if prune_deleted:
                print("[INDEX] Checking for deleted files...")
                # Only prune files/folders belonging to roots that were actually scanned
                ef = {r[0] for r in conn.execute("SELECT path FROM files").fetchall()}
                td = {p for p in (ef - all_files) if p.split("/", 1)[0] in scanned_roots}
                if td:
                    td_list = list(td)
                    for i in range(0, len(td_list), 1000):
                        pruned_files += self._prune_file_records(conn, td_list[i:i + 1000])
                        print(f"[INDEX] Pruned {min(i + 1000, len(td_list))}/{len(td_list)} deleted files...")
                ef2 = {r[0] for r in conn.execute("SELECT path FROM folders").fetchall()}
                td2 = {p for p in (ef2 - all_folders) if p.split("/", 1)[0] in scanned_roots}
                if td2:
                    conn.executemany("DELETE FROM folders WHERE path=?", [(p,) for p in td2])
                    pruned_folders += len(td2)
                    print(f"[INDEX] Pruned {len(td2)} deleted folders")
            else:
                missing = self._prune_missing_folders(conn, scanned_roots)
                pruned_files += missing[0]
                pruned_folders += missing[1]
                print("[INDEX] Full deleted-file prune skipped for quick startup scan")
            conn.commit()
        elapsed = time.time() - t0
        print(f"[INDEX] Indexed {new_files} new/updated files in {elapsed:.2f}s")
        if new_files:
            print(
                f"[INDEX] Timing: parallel read {read_time:.2f}s ({workers} workers), "
                f"database/search {db_time:.2f}s, other {max(0.0, elapsed-read_time-db_time):.2f}s"
            )
        if pruned_files or pruned_folders:
            print(f"[INDEX] Cleanup: {pruned_folders} folders, {pruned_files} files removed from DB")
        if new_files or prune_deleted or pruned_files:
            print("[INDEX] Updating tag counts...")
            with self.lock:
                conn.execute("CREATE TEMP TABLE IF NOT EXISTS tag_counts (tag_id INTEGER PRIMARY KEY, count INTEGER)")
                conn.execute("DELETE FROM tag_counts")
                conn.execute("INSERT INTO tag_counts SELECT tag_id, COUNT(*) FROM file_tags GROUP BY tag_id")
                conn.execute("UPDATE tags SET count = COALESCE((SELECT count FROM tag_counts WHERE tag_counts.tag_id = tags.id), 0)")
                conn.execute("DELETE FROM tags WHERE count = 0")
                conn.execute("DROP TABLE IF EXISTS tag_counts")
                conn.commit()
        else:
            print("[INDEX] Tag counts unchanged")
        self._search_cache = {}
        self._search_cache_key = None
        print("[INDEX] Checking search index...")
        self._ensure_search_index()

    def _ensure_search_index(self):
        if not self.has_fts:
            return
        conn = self._get_conn()
        with self.lock:
            try:
                file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                search_count = conn.execute("SELECT COUNT(*) FROM gallery_search").fetchone()[0]
            except sqlite3.OperationalError:
                return
        self.search_index_ready = (search_count == file_count)
        if file_count and not self.search_index_ready and not self._search_index_notice_shown:
            print(
                f"[SEARCH] Full-text search index is incomplete "
                f"({search_count}/{file_count}). Using legacy search until Settings > Maintenance > Rebuild search is run."
            )
            self._search_index_notice_shown = True

    def rebuild_search_index(self):
        """Rebuild the FTS5 full-text search index from scratch.

        Optimised for large libraries:
        - Processes rows in chunks of 2 000 to avoid loading the entire
          metadata_json column into RAM at once.
        - Computes the heavy regex / JSON work *outside* the lock so other
          gallery requests can proceed between chunks.
        - Uses executemany() for batch INSERTs instead of one INSERT per row.

        Expected throughput: ~10 000–40 000 rows/s on a typical machine,
        so 37 000 files should finish in well under 10 seconds.
        """
        if not self.has_fts:
            return {"ok": False, "error": "FTS5 search is not available"}
        conn = self._get_conn()
        t0 = time.time()

        # Clear the table once, then rebuild in chunks.
        with self.lock:
            total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            conn.execute("DELETE FROM gallery_search")
            conn.commit()

        CHUNK = 2000
        offset = 0
        processed = 0

        while True:
            # Fetch one chunk — release lock between chunks so gallery stays responsive.
            with self.lock:
                chunk_rows = conn.execute(
                    "SELECT path, folder, name, metadata_json FROM files LIMIT ? OFFSET ?",
                    (CHUNK, offset),
                ).fetchall()
            if not chunk_rows:
                break

            # Compute FTS text outside the lock (regex + JSON parsing is expensive).
            batch = []
            for path, folder, name, metadata_json in chunk_rows:
                meta = {}
                if metadata_json:
                    try:
                        meta = json.loads(metadata_json)
                    except (TypeError, json.JSONDecodeError):
                        meta = {}
                tags = " ".join(tag for tag, _ in self._extract_tags(meta))
                metadata_text = self._search_text(name, meta)
                batch.append((path, folder, name or "", tags, metadata_text))

            # Batch-insert this chunk.
            with self.lock:
                conn.executemany(
                    "INSERT INTO gallery_search(path, folder, name, tags, metadata) VALUES (?,?,?,?,?)",
                    batch,
                )
                conn.commit()

            processed += len(chunk_rows)
            offset += CHUNK
            elapsed = time.time() - t0
            print(f"[SEARCH] Rebuilt {processed}/{total} files ({elapsed:.1f}s elapsed)...")

        self._search_cache = {}
        self._search_cache_key = None
        self.search_index_ready = True
        self._search_index_notice_shown = False
        elapsed = time.time() - t0
        print(f"[SEARCH] Full-text index rebuilt: {processed} files in {elapsed:.2f}s")
        return {"ok": True, "files": processed, "seconds": elapsed}

    def resolve_path(self, db_path):
        """Resolve a DB path (root_name/rel/path) to an absolute filesystem path."""
        if not db_path:
            return None
        parts = db_path.split("/", 1)
        root_name = parts[0]
        remainder = parts[1] if len(parts) > 1 else ""
        root_abs = self.roots.get(root_name)
        if not root_abs:
            return None
        if not remainder:
            return root_abs
        return safe_resolve(root_abs, remainder)

    @staticmethod
    def _extract_tags(meta):
        """Extract searchable tags from metadata. Returns list of (name, type) tuples."""
        tags = []
        raw = meta.get("parameters", "")
        if not raw:
            raw = meta.get("prompt", "")
        if not raw:
            return tags
        parsed = parse_sd_parameters(raw)
        # Prompt tags (split on comma)
        prompt = parsed.get("prompt", "")
        if prompt:
            for tag in re.split(r",\s*", prompt):
                tag = re.sub(r"[\\/()\[\]{}]+", "", tag).strip().lower()
                tag = re.sub(r"\s+", " ", tag)
                if len(tag) >= 2 and len(tag) <= 80 and not tag.startswith("<"):
                    tags.append((tag, "prompt"))
        # Settings as tags (model, sampler, etc.)
        settings = parsed.get("settings", {})
        for key in ("Model", "Sampler", "Source"):
            val = settings.get(key, "").strip()
            if val and len(val) >= 2:
                tags.append((f"{key.lower()}:{val.lower()}", "setting"))
        # LoRA extraction from prompt
        for match in re.finditer(r"<lora:([\w\s._-]+?)(?::[\d.]+)?>", prompt, re.IGNORECASE):
            tags.append((f"lora:{match.group(1).strip().lower()}", "lora"))
        return tags

    def _index_tags(self, conn, file_path, meta):
        """Extract tags from metadata and store in tags/file_tags tables."""
        # Remove old tags for this file
        conn.execute("DELETE FROM file_tags WHERE file_path=?", (file_path,))
        if not meta:
            return
        tags = self._extract_tags(meta)
        for tag_name, tag_type in tags:
            # Insert or get tag
            conn.execute("INSERT OR IGNORE INTO tags (name, count) VALUES (?, 0)", (tag_name,))
            row = conn.execute("SELECT id FROM tags WHERE name=?", (tag_name,)).fetchone()
            if row:
                conn.execute("INSERT OR IGNORE INTO file_tags (file_path, tag_id, tag_type) VALUES (?,?,?)",
                             (file_path, row[0], tag_type))

    @staticmethod
    def _flatten_metadata_values(value, out, limit=1200):
        if len(out) >= limit:
            return
        if isinstance(value, dict):
            for k, v in value.items():
                if len(out) >= limit:
                    break
                out.append(str(k))
                GalleryDB._flatten_metadata_values(v, out, limit)
        elif isinstance(value, (list, tuple)):
            for item in value:
                if len(out) >= limit:
                    break
                GalleryDB._flatten_metadata_values(item, out, limit)
        elif value is not None:
            text = str(value).strip()
            if text:
                out.append(text[:4000])

    @staticmethod
    def _fts_query(query):
        raw_terms = re.findall(r"[A-Za-z0-9_.-]+", (query or "").lower())
        terms = []
        for term in raw_terms:
            term = term.strip("_:.-")
            if term and re.search(r"[a-z0-9]", term):
                terms.append(term)
        if not terms:
            return ""
        # Prefix terms make partial searches like "illustr" match "Illustrious".
        return " ".join(f"{term}*" for term in terms[:12])

    def _search_text(self, name, meta):
        parts = [name or ""]
        tag_names = [tag for tag, _tag_type in self._extract_tags(meta or {})]
        parts.extend(tag_names)
        flat = []
        self._flatten_metadata_values(meta or {}, flat)
        parts.extend(flat)
        text = " ".join(p for p in parts if p)
        # Also index a split variant so sub-words inside names joined by _ . : - /
        # (e.g. "CyberRealistic_zit_v5.0") become individually searchable — this
        # lets you find a checkpoint by a fragment like "zit". The original joined
        # tokens stay indexed too, so exact tag search (model:..., lora:...,
        # score_7) keeps working.
        split = re.sub(r"[._:/\\-]+", " ", text)
        if split != text:
            text = text + " " + split
        return text

    def _index_search(self, conn, file_path, folder, name, meta, replace=True):
        if not self.has_fts:
            return
        tags = " ".join(tag for tag, _tag_type in self._extract_tags(meta or {}))
        metadata = self._search_text(name, meta or {})
        try:
            # `path` is UNINDEXED in FTS5, so DELETE WHERE path=? is a full-table
            # scan — O(n) per call, O(n^2) over a full rebuild. Callers that just
            # cleared the table (rebuild) pass replace=False to skip it.
            if replace:
                conn.execute("DELETE FROM gallery_search WHERE path=?", (file_path,))
            conn.execute(
                "INSERT INTO gallery_search(path, folder, name, tags, metadata) VALUES (?,?,?,?,?)",
                (file_path, folder, name or "", tags, metadata)
            )
        except sqlite3.OperationalError as e:
            self.has_fts = False
            print(f"[SEARCH] FTS index disabled after error: {e}")

    def _delete_search(self, conn, file_path):
        if self.has_fts:
            try:
                conn.execute("DELETE FROM gallery_search WHERE path=?", (file_path,))
            except sqlite3.OperationalError:
                pass

    def _delete_search_many(self, conn, paths):
        """Delete many FTS rows with one scan per chunk.

        `path` is UNINDEXED in the FTS5 table, so `DELETE ... WHERE path=?`
        scans the whole FTS table. Doing that once per pruned file made the
        final deleted-file pass crawl on large libraries. A single `IN (...)`
        statement still scans, but only once for hundreds of paths.
        """
        if not self.has_fts:
            return
        paths = list(paths)
        CHUNK = 500  # stay comfortably below SQLite's common 999 variable limit
        try:
            for i in range(0, len(paths), CHUNK):
                sub = paths[i:i + CHUNK]
                if not sub:
                    continue
                placeholders = ",".join("?" for _ in sub)
                conn.execute(f"DELETE FROM gallery_search WHERE path IN ({placeholders})", sub)
        except sqlite3.OperationalError:
            pass

    def _prune_file_records(self, conn, paths):
        """Remove DB/search/thumb records for files that no longer exist on disk."""
        paths = list(paths)
        if not paths:
            return 0
        conn.executemany("DELETE FROM file_tags WHERE file_path=?", [(p,) for p in paths])
        conn.executemany("DELETE FROM file_collections WHERE file_path=?", [(p,) for p in paths])
        self._delete_search_many(conn, paths)
        conn.executemany("DELETE FROM files WHERE path=?", [(p,) for p in paths])
        pruned = 0
        for p in paths:
            thumb = get_thumb_path(self.thumb_dir, p)
            if os.path.exists(thumb):
                try:
                    os.remove(thumb)
                except Exception:
                    pass
            pruned += 1
        return pruned

    def _prune_missing_folders(self, conn, scanned_roots):
        """Remove folders that are in SQLite but no longer exist on disk.

        Quick startup scans intentionally avoid comparing every file in the DB.
        Folder existence checks are much cheaper, and they keep the sidebar from
        showing deleted directories with broken thumbnails.
        """
        if not scanned_roots:
            return 0, 0
        rows = []
        for root in scanned_roots:
            rows.extend(conn.execute(
                "SELECT path FROM folders WHERE path=? OR path LIKE ? ORDER BY LENGTH(path)",
                (root, root + "/%")
            ).fetchall())

        missing_roots = []
        pruned_files = 0
        pruned_folders = 0
        for (folder_path,) in rows:
            if any(folder_path == m or folder_path.startswith(m + "/") for m in missing_roots):
                continue
            abs_path = self.resolve_path(folder_path)
            if abs_path and os.path.isdir(abs_path):
                continue
            missing_roots.append(folder_path)

            file_rows = conn.execute(
                "SELECT path FROM files WHERE folder=? OR folder LIKE ?",
                (folder_path, folder_path + "/%")
            ).fetchall()
            paths = [r[0] for r in file_rows]
            if paths:
                pruned_files += self._prune_file_records(conn, paths)
            cur = conn.execute(
                "DELETE FROM folders WHERE path=? OR path LIKE ?",
                (folder_path, folder_path + "/%")
            )
            pruned_folders += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        if pruned_files or pruned_folders:
            print(f"[INDEX] Pruned {pruned_folders} missing folders and {pruned_files} files")
        return pruned_files, pruned_folders

    def _prune_unconfigured_roots(self, conn):
        """Remove DB rows for gallery roots that are no longer configured.

        Settings stores the selected root folders, while SQLite stores the indexed
        tree. If a root is removed from Settings, scans no longer visit it, so its
        old rows need an explicit cleanup pass.
        """
        configured = set(self.roots.keys())
        if not configured:
            return 0, 0
        roots = {
            (r[0] or "").split("/", 1)[0]
            for r in conn.execute("SELECT path FROM folders UNION SELECT path FROM files").fetchall()
            if r[0]
        }
        stale_roots = sorted(r for r in roots if r and r not in configured)
        if not stale_roots:
            return 0, 0
        pruned_files = 0
        pruned_folders = 0
        for root in stale_roots:
            paths = [
                r[0] for r in conn.execute(
                    "SELECT path FROM files WHERE path=? OR path LIKE ?",
                    (root, root + "/%")
                ).fetchall()
            ]
            if paths:
                pruned_files += self._prune_file_records(conn, paths)
            cur = conn.execute(
                "DELETE FROM folders WHERE path=? OR path LIKE ?",
                (root, root + "/%")
            )
            pruned_folders += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        if pruned_files or pruned_folders:
            print(f"[INDEX] Pruned {pruned_folders} unconfigured-root folders and {pruned_files} files")
        return pruned_files, pruned_folders

    def _prune_empty_folder_records(self, conn, candidate_folders):
        """Remove indexed folder branches that no longer contain indexed images."""
        candidates = set()
        for folder in candidate_folders or []:
            folder = (folder or "").strip("/")
            while folder:
                candidates.add(folder)
                if "/" not in folder:
                    break
                folder = folder.rsplit("/", 1)[0]
        pruned = 0
        for folder in sorted(candidates, key=len, reverse=True):
            has_files = conn.execute(
                "SELECT 1 FROM files WHERE folder=? OR folder LIKE ? LIMIT 1",
                (folder, folder + "/%")
            ).fetchone()
            if has_files:
                continue
            cur = conn.execute(
                "DELETE FROM folders WHERE path=? OR path LIKE ?",
                (folder, folder + "/%")
            )
            pruned += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return pruned

    def get_subfolders(self, parent=""):
        conn = self._get_conn()
        with self.lock:
            rows = conn.execute("""
                SELECT
                    f.path,
                    f.name,
                    f.file_count,
                    EXISTS (
                        SELECT 1
                        FROM folders c
                        WHERE c.parent = f.path
                          AND c.path != c.parent
                          AND EXISTS (
                              SELECT 1
                              FROM folders d
                              WHERE (d.path = c.path OR d.path LIKE c.path || '/%')
                                AND d.file_count > 0
                              LIMIT 1
                          )
                    ) AS has_visible_children,
                    f.cover_image
                FROM folders f
                WHERE f.parent = ?
                  AND f.path != ?
                  AND EXISTS (
                      SELECT 1
                      FROM folders d
                      WHERE (d.path = f.path OR d.path LIKE f.path || '/%')
                        AND d.file_count > 0
                      LIMIT 1
                  )
                ORDER BY f.name COLLATE NOCASE
            """, (parent, parent)).fetchall()
        return [{"path": r[0], "name": r[1], "count": r[2], "has_children": bool(r[3]), "cover": r[4]} for r in rows]

    def get_files(self, folder="", sort="name", order="asc", page=1, per_page=DEFAULT_PER_PAGE, favorite_only=False, time_filter=None):
        conn = self._get_conn()
        sort_col = {"name": "name", "date": "mtime", "size": "size", "favorite": "favorite"}.get(sort, "name")
        # Favorite sort: favorites first, then by date
        if sort == "favorite":
            order_clause = "favorite DESC, mtime DESC"
        else:
            order_dir = "DESC" if order == "desc" else "ASC"
            order_clause = f"{sort_col} {order_dir}"
        offset = (page - 1) * per_page
        where = ["folder=?"]
        params = [folder]
        if favorite_only:
            where.append("favorite=1")
        if time_filter:
            now = time.time()
            if time_filter == "today":
                cutoff = now - 86400
            elif time_filter == "7days":
                cutoff = now - 7 * 86400
            elif time_filter == "30days":
                cutoff = now - 30 * 86400
            else:
                cutoff = 0
            if cutoff:
                where.append("mtime>=?")
                params.append(cutoff)
        where_sql = " AND ".join(where)
        with self.lock:
            total = conn.execute(f"SELECT COUNT(*) FROM files WHERE {where_sql}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT path, name, ext, size, mtime, width, height, has_metadata, favorite FROM files WHERE {where_sql} ORDER BY {order_clause} LIMIT ? OFFSET ?",
                params + [per_page, offset]).fetchall()
        pages = max(1, (total + per_page - 1) // per_page)
        return {
            "files": [{"path": r[0], "name": r[1], "ext": r[2], "size": r[3], "mtime": r[4],
                        "width": r[5], "height": r[6], "has_metadata": bool(r[7]), "favorite": bool(r[8])} for r in rows],
            "total": total, "page": page, "pages": pages, "per_page": per_page
        }

    def get_timeline_files(self, time_filter="today", sort="date", order="desc", page=1, per_page=DEFAULT_PER_PAGE):
        """Get files across all folders filtered by time."""
        conn = self._get_conn()
        now = time.time()
        cutoffs = {"today": 86400, "7days": 7*86400, "30days": 30*86400}
        cutoff = now - cutoffs.get(time_filter, 86400)
        sort_col = {"name": "name", "date": "mtime", "size": "size"}.get(sort, "mtime")
        order_dir = "DESC" if order == "desc" else "ASC"
        offset = (page - 1) * per_page
        with self.lock:
            total = conn.execute("SELECT COUNT(*) FROM files WHERE mtime>=?", (cutoff,)).fetchone()[0]
            rows = conn.execute(
                f"SELECT path, name, ext, size, mtime, width, height, has_metadata, favorite FROM files WHERE mtime>=? ORDER BY {sort_col} {order_dir} LIMIT ? OFFSET ?",
                (cutoff, per_page, offset)).fetchall()
        pages = max(1, (total + per_page - 1) // per_page)
        return {
            "files": [{"path": r[0], "name": r[1], "ext": r[2], "size": r[3], "mtime": r[4],
                        "width": r[5], "height": r[6], "has_metadata": bool(r[7]), "favorite": bool(r[8])} for r in rows],
            "total": total, "page": page, "pages": pages, "per_page": per_page
        }

    def get_all_favorites(self, sort="date", order="desc", page=1, per_page=DEFAULT_PER_PAGE):
        """Get all favorites across all folders."""
        conn = self._get_conn()
        sort_col = {"name": "name", "date": "mtime", "size": "size"}.get(sort, "mtime")
        order_dir = "DESC" if order == "desc" else "ASC"
        offset = (page - 1) * per_page
        with self.lock:
            total = conn.execute("SELECT COUNT(*) FROM files WHERE favorite=1").fetchone()[0]
            rows = conn.execute(
                f"SELECT path, name, ext, size, mtime, width, height, has_metadata, favorite FROM files WHERE favorite=1 ORDER BY {sort_col} {order_dir} LIMIT ? OFFSET ?",
                (per_page, offset)).fetchall()
        pages = max(1, (total + per_page - 1) // per_page)
        return {
            "files": [{"path": r[0], "name": r[1], "ext": r[2], "size": r[3], "mtime": r[4],
                        "width": r[5], "height": r[6], "has_metadata": bool(r[7]), "favorite": bool(r[8])} for r in rows],
            "total": total, "page": page, "pages": pages, "per_page": per_page
        }

    def toggle_favorite(self, rel_path):
        """Toggle favorite status. Returns new state."""
        conn = self._get_conn()
        with self.lock:
            row = conn.execute("SELECT favorite FROM files WHERE path=?", (rel_path,)).fetchone()
            if not row: return None
            new_val = 0 if row[0] else 1
            conn.execute("UPDATE files SET favorite=? WHERE path=?", (new_val, rel_path))
            conn.commit()
        return {"path": rel_path, "favorite": bool(new_val)}

    def get_file_metadata(self, rel_path):
        conn = self._get_conn()
        with self.lock:
            row = conn.execute("SELECT metadata_json, width, height, size, name, ext FROM files WHERE path=?", (rel_path,)).fetchone()
        if not row: return None
        meta_raw = json.loads(row[0]) if row[0] else {}
        info = {"width": row[1], "height": row[2], "size": row[3], "name": row[4], "ext": row[5]}
        parsed = {}
        if "parameters" in meta_raw:
            parsed = parse_sd_parameters(meta_raw["parameters"])
        elif "prompt" in meta_raw:
            prompt = meta_raw.get("prompt", "")
            if isinstance(prompt, str) and prompt.lstrip().startswith(("{", "[")):
                parsed["workflow"] = prompt[:2000] + "..." if len(prompt) > 2000 else prompt
            else:
                parsed["prompt"] = prompt
            if "Negative prompt" in meta_raw: parsed["negative_prompt"] = meta_raw["Negative prompt"]
            if "workflow" in meta_raw:
                wf = meta_raw["workflow"]
                parsed["workflow"] = wf[:2000] + "..." if len(wf) > 2000 else wf
        # civitai enrichment is performed by the module handler (uses hub.civitai)
        return {"info": info, "raw_meta": meta_raw, "parsed": parsed}

    def get_stats(self):
        conn = self._get_conn()
        with self.lock:
            f = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            d = conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
            m = conn.execute("SELECT COUNT(*) FROM files WHERE has_metadata=1").fetchone()[0]
            fav = conn.execute("SELECT COUNT(*) FROM files WHERE favorite=1").fetchone()[0]
        return {"files": f, "folders": d, "with_metadata": m, "favorites": fav}

    # Search result cache: query_key -> list of file paths
    _search_cache = {}
    _search_cache_key = None

    def search(self, query, page=1, per_page=DEFAULT_PER_PAGE, sort="date", order="desc"):
        """Global search using the full-text index, with legacy LIKE fallback."""
        conn = self._get_conn()
        sort_col = {"name": "name", "date": "mtime", "size": "size"}.get(sort, "mtime")
        order_dir = "DESC" if order == "desc" else "ASC"
        fts_query = self._fts_query(query)
        if not self.has_fts or not self.search_index_ready or not fts_query:
            return self._search_like(query, page, per_page, sort, order)

        with self.lock:
            fts_error = None
            try:
                total = conn.execute(
                    "SELECT COUNT(*) FROM gallery_search WHERE gallery_search MATCH ?",
                    (fts_query,)
                ).fetchone()[0]
                offset = (page - 1) * per_page
                rows = conn.execute(f"""
                    SELECT f.path, f.name, f.folder, f.ext, f.size, f.mtime,
                           f.width, f.height, f.has_metadata, f.favorite
                    FROM gallery_search s
                    JOIN files f ON f.path = s.path
                    WHERE gallery_search MATCH ?
                    ORDER BY f.{sort_col} {order_dir}
                    LIMIT ? OFFSET ?
                """, (fts_query, per_page, offset)).fetchall()
                folder_rows = conn.execute("""
                    SELECT f.folder, COUNT(*) as cnt
                    FROM gallery_search s
                    JOIN files f ON f.path = s.path
                    WHERE gallery_search MATCH ?
                    GROUP BY f.folder
                    ORDER BY cnt DESC
                """, (fts_query,)).fetchall()
                folder_info = {}
                if folder_rows:
                    fps = [r[0] for r in folder_rows]
                    ph = ",".join(["?"] * len(fps))
                    name_rows = conn.execute(f"SELECT path, name FROM folders WHERE path IN ({ph})", fps).fetchall()
                    folder_info = {r[0]: r[1] for r in name_rows}
            except sqlite3.OperationalError as e:
                fts_error = e

        if fts_error:
            print(f"[SEARCH] FTS query failed; using legacy search ({fts_error})")
            return self._search_like(query, page, per_page, sort, order)

        pages = max(1, (total + per_page - 1) // per_page)
        files = [{"path": r[0], "name": r[1], "folder": r[2], "ext": r[3], "size": r[4],
                  "mtime": r[5], "width": r[6], "height": r[7], "has_metadata": bool(r[8]), "favorite": bool(r[9])} for r in rows]
        folders = [{"path": r[0], "name": folder_info.get(r[0], r[0] or "Root"), "count": r[1]} for r in folder_rows]
        return {"files": files, "total": total, "page": page, "pages": pages,
                "per_page": per_page, "folders": folders, "query": query}

    def _search_like(self, query, page=1, per_page=DEFAULT_PER_PAGE, sort="date", order="desc"):
        """Legacy global search: tags + filename first, metadata_json LIKE only if needed."""
        conn = self._get_conn()
        sort_col = {"name": "name", "date": "mtime", "size": "size"}.get(sort, "mtime")
        order_dir = "DESC" if order == "desc" else "ASC"
        like = f"%{query.lower()}%"
        cache_key = f"legacy|{query}|{sort}|{order}"

        with self.lock:
            # Check if we have cached results for this exact query+sort
            if self._search_cache_key == cache_key and self._search_cache.get("paths"):
                cached = self._search_cache
            else:
                # Tier 1: tags + filename (fast, indexed)
                fast_where = """(files.path IN (
                    SELECT ft.file_path FROM file_tags ft
                    JOIN tags t ON ft.tag_id = t.id
                    WHERE t.name LIKE ?
                ) OR files.name LIKE ?)"""
                fast_params = (like, like)
                fast_count = conn.execute(f"SELECT COUNT(*) FROM files WHERE {fast_where}", fast_params).fetchone()[0]

                # Tier 2: only if tier 1 found nothing, fall back to metadata_json LIKE
                if fast_count == 0:
                    full_where = """(files.path IN (
                        SELECT ft.file_path FROM file_tags ft
                        JOIN tags t ON ft.tag_id = t.id
                        WHERE t.name LIKE ?
                    ) OR files.name LIKE ? OR files.metadata_json LIKE ?)"""
                    full_params = (like, like, f"%{query}%")
                    use_where = full_where
                    use_params = full_params
                else:
                    use_where = fast_where
                    use_params = fast_params

                # Cache all matching paths for fast pagination
                all_paths = conn.execute(
                    f"SELECT path FROM files WHERE {use_where} ORDER BY {sort_col} {order_dir}",
                    use_params).fetchall()
                cached = {"paths": [r[0] for r in all_paths], "where": use_where, "params": use_params}
                self._search_cache = cached
                self._search_cache_key = cache_key

            # Paginate from cached paths
            total = len(cached["paths"])
            offset = (page - 1) * per_page
            page_paths = cached["paths"][offset:offset + per_page]

            if page_paths:
                ph = ",".join(["?"] * len(page_paths))
                rows = conn.execute(
                    f"SELECT path, name, folder, ext, size, mtime, width, height, has_metadata, favorite FROM files WHERE path IN ({ph}) ORDER BY {sort_col} {order_dir}",
                    page_paths).fetchall()
            else:
                rows = []

            # Folder breakdown (from cached paths, not re-queried)
            folder_rows = conn.execute(
                f"SELECT folder, COUNT(*) as cnt FROM files WHERE {cached['where']} GROUP BY folder ORDER BY cnt DESC",
                cached["params"]).fetchall()
            folder_info = {}
            if folder_rows:
                fps = [r[0] for r in folder_rows]
                ph = ",".join(["?"] * len(fps))
                name_rows = conn.execute(f"SELECT path, name FROM folders WHERE path IN ({ph})", fps).fetchall()
                folder_info = {r[0]: r[1] for r in name_rows}

        pages = max(1, (total + per_page - 1) // per_page)
        files = [{"path": r[0], "name": r[1], "folder": r[2], "ext": r[3], "size": r[4],
                  "mtime": r[5], "width": r[6], "height": r[7], "has_metadata": bool(r[8]), "favorite": bool(r[9])} for r in rows]
        folders = [{"path": r[0], "name": folder_info.get(r[0], r[0] or "Root"), "count": r[1]} for r in folder_rows]
        return {"files": files, "total": total, "page": page, "pages": pages,
                "per_page": per_page, "folders": folders, "query": query}

    def search_in_folder(self, query, folder, page=1, per_page=DEFAULT_PER_PAGE, sort="date", order="desc"):
        """Search within a folder and its subfolders using FTS when available."""
        conn = self._get_conn()
        fts_query = self._fts_query(query)
        if not self.has_fts or not self.search_index_ready or not fts_query:
            return self._search_in_folder_like(query, folder, page, per_page, sort, order)
        folder_like = folder + "/%" if folder else "%"
        sort_col = {"name": "name", "date": "mtime", "size": "size"}.get(sort, "mtime")
        order_dir = "DESC" if order == "desc" else "ASC"

        with self.lock:
            fts_error = None
            try:
                where_folder = "(s.folder=? OR s.folder LIKE ?)"
                total = conn.execute(f"""
                    SELECT COUNT(*)
                    FROM gallery_search s
                    WHERE gallery_search MATCH ? AND {where_folder}
                """, (fts_query, folder, folder_like)).fetchone()[0]
                offset = (page - 1) * per_page
                rows = conn.execute(f"""
                    SELECT f.path, f.name, f.folder, f.ext, f.size, f.mtime,
                           f.width, f.height, f.has_metadata, f.favorite
                    FROM gallery_search s
                    JOIN files f ON f.path = s.path
                    WHERE gallery_search MATCH ? AND {where_folder}
                    ORDER BY f.{sort_col} {order_dir}
                    LIMIT ? OFFSET ?
                """, (fts_query, folder, folder_like, per_page, offset)).fetchall()
            except sqlite3.OperationalError as e:
                fts_error = e

        if fts_error:
            print(f"[SEARCH] Folder FTS query failed; using legacy search ({fts_error})")
            return self._search_in_folder_like(query, folder, page, per_page, sort, order)

        pages = max(1, (total + per_page - 1) // per_page)
        files = [{"path": r[0], "name": r[1], "folder": r[2], "ext": r[3], "size": r[4],
                  "mtime": r[5], "width": r[6], "height": r[7], "has_metadata": bool(r[8]), "favorite": bool(r[9])} for r in rows]
        return {"files": files, "total": total, "page": page, "pages": pages, "per_page": per_page}

    def _search_in_folder_like(self, query, folder, page=1, per_page=DEFAULT_PER_PAGE, sort="date", order="desc"):
        """Legacy folder search. Tags first, LIKE fallback."""
        conn = self._get_conn()
        like = f"%{query.lower()}%"
        folder_like = folder + "/%" if folder else "%"
        sort_col = {"name": "name", "date": "mtime", "size": "size"}.get(sort, "mtime")
        order_dir = "DESC" if order == "desc" else "ASC"

        with self.lock:
            # Tier 1: tags + filename
            fast_where = """(folder=? OR folder LIKE ?) AND (
                files.path IN (
                    SELECT ft.file_path FROM file_tags ft
                    JOIN tags t ON ft.tag_id = t.id
                    WHERE t.name LIKE ?
                ) OR files.name LIKE ?)"""
            fast_params = (folder, folder_like, like, like)
            fast_count = conn.execute(f"SELECT COUNT(*) FROM files WHERE {fast_where}", fast_params).fetchone()[0]

            if fast_count == 0:
                full_where = """(folder=? OR folder LIKE ?) AND (
                    files.path IN (
                        SELECT ft.file_path FROM file_tags ft
                        JOIN tags t ON ft.tag_id = t.id
                        WHERE t.name LIKE ?
                    ) OR files.name LIKE ? OR files.metadata_json LIKE ?)"""
                use_where = full_where
                use_params = (folder, folder_like, like, like, f"%{query}%")
            else:
                use_where = fast_where
                use_params = fast_params

            total = conn.execute(f"SELECT COUNT(*) FROM files WHERE {use_where}", use_params).fetchone()[0]
            offset = (page - 1) * per_page
            rows = conn.execute(
                f"SELECT path, name, folder, ext, size, mtime, width, height, has_metadata, favorite FROM files WHERE {use_where} ORDER BY {sort_col} {order_dir} LIMIT ? OFFSET ?",
                use_params + (per_page, offset)).fetchall()

        pages = max(1, (total + per_page - 1) // per_page)
        files = [{"path": r[0], "name": r[1], "folder": r[2], "ext": r[3], "size": r[4],
                  "mtime": r[5], "width": r[6], "height": r[7], "has_metadata": bool(r[8]), "favorite": bool(r[9])} for r in rows]
        return {"files": files, "total": total, "page": page, "pages": pages, "per_page": per_page}

    def get_tags(self, prefix="", limit=50):
        """Autocomplete: get tags matching prefix, sorted by frequency."""
        conn = self._get_conn()
        with self.lock:
            if prefix:
                rows = conn.execute("SELECT name, count FROM tags WHERE name LIKE ? ORDER BY count DESC LIMIT ?",
                                    (f"%{prefix.lower()}%", limit)).fetchall()
            else:
                rows = conn.execute("SELECT name, count FROM tags ORDER BY count DESC LIMIT ?", (limit,)).fetchall()
        return [{"name": r[0], "count": r[1]} for r in rows]

    def delete_files(self, rel_paths):
        """Delete files: send to OS trash, remove from DB + thumbnails. Returns results per file."""
        conn = self._get_conn()
        results = []
        affected_folders = set()
        for rel_path in rel_paths:
            full_path = self.resolve_path(rel_path)
            if not full_path or not os.path.isfile(full_path):
                results.append({"path": rel_path, "ok": False, "error": "File not found"})
                continue
            try:
                # Send to trash or permanently delete
                if HAS_TRASH:
                    send2trash(full_path)
                else:
                    os.remove(full_path)
                # Remove from DB
                with self.lock:
                    conn.execute("DELETE FROM file_tags WHERE file_path=?", (rel_path,))
                    conn.execute("DELETE FROM file_collections WHERE file_path=?", (rel_path,))
                    conn.execute("DELETE FROM files WHERE path=?", (rel_path,))
                    # Update folder file count
                    folder = os.path.dirname(rel_path)
                    affected_folders.add(folder)
                    conn.execute(
                        "UPDATE folders SET file_count = (SELECT COUNT(*) FROM files WHERE folder=?) WHERE path=?",
                        (folder, folder)
                    )
                    conn.commit()
                # Remove thumbnail
                thumb = get_thumb_path(self.thumb_dir, rel_path)
                if os.path.exists(thumb):
                    try: os.remove(thumb)
                    except Exception: pass
                results.append({"path": rel_path, "ok": True})
                log_debug(f"Deleted: {rel_path}")
            except Exception as e:
                results.append({"path": rel_path, "ok": False, "error": str(e)})
                log_debug(f"Delete failed for {rel_path}: {e}")
        # Update tag counts
        with self.lock:
            pruned_folders = self._prune_empty_folder_records(conn, affected_folders)
            if pruned_folders:
                print(f"[DELETE] Pruned {pruned_folders} empty folder records")
            conn.execute("UPDATE tags SET count = (SELECT COUNT(*) FROM file_tags WHERE file_tags.tag_id = tags.id)")
            conn.execute("DELETE FROM tags WHERE count = 0")
            conn.commit()
        self._search_cache = {}
        self._search_cache_key = None
        return results

    # ─── Collections ──────────────────────────────────────────────────────────

    def create_collection(self, name, color="#4a9eff"):
        conn = self._get_conn()
        with self.lock:
            try:
                conn.execute("INSERT INTO collections (name, color, created) VALUES (?,?,?)",
                             (name.strip(), color, time.time()))
                conn.commit()
                row = conn.execute("SELECT id, name, color, created FROM collections WHERE name=?", (name.strip(),)).fetchone()
                return {"id": row[0], "name": row[1], "color": row[2], "count": 0}
            except sqlite3.IntegrityError:
                return {"error": "Collection already exists"}

    def rename_collection(self, collection_id, new_name):
        conn = self._get_conn()
        with self.lock:
            try:
                conn.execute("UPDATE collections SET name=? WHERE id=?", (new_name.strip(), collection_id))
                conn.commit()
                return {"ok": True}
            except sqlite3.IntegrityError:
                return {"error": "Name already taken"}

    def delete_collection(self, collection_id):
        conn = self._get_conn()
        with self.lock:
            conn.execute("DELETE FROM file_collections WHERE collection_id=?", (collection_id,))
            conn.execute("DELETE FROM collections WHERE id=?", (collection_id,))
            conn.commit()
        return {"ok": True}

    def get_collections(self):
        conn = self._get_conn()
        with self.lock:
            rows = conn.execute("""
                SELECT c.id, c.name, c.color, c.created, COUNT(fc.file_path) as cnt
                FROM collections c
                LEFT JOIN file_collections fc ON c.id = fc.collection_id
                GROUP BY c.id ORDER BY c.name COLLATE NOCASE
            """).fetchall()
        return [{"id": r[0], "name": r[1], "color": r[2], "created": r[3], "count": r[4]} for r in rows]

    def add_to_collection(self, collection_id, paths):
        conn = self._get_conn()
        now = time.time()
        added = 0
        with self.lock:
            for p in paths:
                try:
                    conn.execute("INSERT OR IGNORE INTO file_collections (file_path, collection_id, added) VALUES (?,?,?)",
                                 (p, collection_id, now))
                    added += 1
                except Exception: pass
            conn.commit()
        return {"added": added}

    def remove_from_collection(self, collection_id, paths):
        conn = self._get_conn()
        with self.lock:
            for p in paths:
                conn.execute("DELETE FROM file_collections WHERE file_path=? AND collection_id=?", (p, collection_id))
            conn.commit()
        return {"removed": len(paths)}

    def get_collection_files(self, collection_id, sort="date", order="desc", page=1, per_page=DEFAULT_PER_PAGE):
        conn = self._get_conn()
        sort_col = {"name": "f.name", "date": "fc.added", "size": "f.size"}.get(sort, "fc.added")
        order_dir = "DESC" if order == "desc" else "ASC"
        offset = (page - 1) * per_page
        with self.lock:
            total = conn.execute(
                "SELECT COUNT(*) FROM file_collections fc JOIN files f ON fc.file_path = f.path WHERE fc.collection_id=?",
                (collection_id,)).fetchone()[0]
            rows = conn.execute(
                f"""SELECT f.path, f.name, f.ext, f.size, f.mtime, f.width, f.height, f.has_metadata, f.favorite
                    FROM file_collections fc JOIN files f ON fc.file_path = f.path
                    WHERE fc.collection_id=? ORDER BY {sort_col} {order_dir} LIMIT ? OFFSET ?""",
                (collection_id, per_page, offset)).fetchall()
        pages = max(1, (total + per_page - 1) // per_page)
        return {
            "files": [{"path": r[0], "name": r[1], "ext": r[2], "size": r[3], "mtime": r[4],
                        "width": r[5], "height": r[6], "has_metadata": bool(r[7]), "favorite": bool(r[8])} for r in rows],
            "total": total, "page": page, "pages": pages, "per_page": per_page
        }

    def get_file_collections(self, file_path):
        """Get which collections a file belongs to."""
        conn = self._get_conn()
        with self.lock:
            rows = conn.execute("""
                SELECT c.id, c.name, c.color FROM collections c
                JOIN file_collections fc ON c.id = fc.collection_id
                WHERE fc.file_path=?
            """, (file_path,)).fetchall()
        return [{"id": r[0], "name": r[1], "color": r[2]} for r in rows]

# ─── Thumbnail Generator ─────────────────────────────────────────────────────

def get_thumb_path(thumb_dir, rel_path):
    h = hashlib.md5(rel_path.encode()).hexdigest()
    return os.path.join(thumb_dir, h[:2], h + ".webp")

def migrate_thumb_layout(thumb_dir):
    """One-time migration: move flat thumbs into hash-prefix subfolders."""
    if not os.path.isdir(thumb_dir):
        return
    flat_files = [f for f in os.listdir(thumb_dir) if f.endswith(".webp") and os.path.isfile(os.path.join(thumb_dir, f))]
    if not flat_files:
        return
    print(f"[MIGRATE] Moving {len(flat_files)} thumbnails to subfolder layout...")
    moved = 0
    for fname in flat_files:
        prefix = fname[:2]
        dest_dir = os.path.join(thumb_dir, prefix)
        os.makedirs(dest_dir, exist_ok=True)
        src = os.path.join(thumb_dir, fname)
        dst = os.path.join(dest_dir, fname)
        try:
            os.rename(src, dst)
            moved += 1
        except OSError as e:
            log_debug(f"Thumb migration failed for {fname}: {e}")
    old_jpgs = [f for f in os.listdir(thumb_dir) if f.endswith((".jpg", ".jpg.old")) and os.path.isfile(os.path.join(thumb_dir, f))]
    for fname in old_jpgs:
        try:
            os.remove(os.path.join(thumb_dir, fname))
        except OSError:
            pass
    print(f"[MIGRATE] Done. {moved} thumbnails moved to {len(set(f[:2] for f in flat_files))} subfolders.")

def ensure_thumbnail(thumb_dir, db_path, abs_path):
    """Generate thumbnail if needed. Returns thumb path or fallback to original."""
    if not abs_path or not os.path.isfile(abs_path):
        return None
    thumb_path = get_thumb_path(thumb_dir, db_path)
    if os.path.exists(thumb_path):
        src_mtime = os.path.getmtime(abs_path)
        th_mtime = os.path.getmtime(thumb_path)
        if th_mtime >= src_mtime:
            return thumb_path
    if not HAS_PIL:
        return abs_path
    try:
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        with Image.open(abs_path) as img:
            img.thumbnail(THUMB_SIZE, LANCZOS)
            img.save(thumb_path, "WEBP", quality=80)
        return thumb_path
    except Exception as e:
        log_debug(f"Thumbnail generation failed: {e}")
        return abs_path

# (old ensure_thumbnail removed — using new version above)


def generate_all_thumbs(thumb_dir, roots, workers=None):
    """Pre-generate all thumbnails using a thread pool."""
    import concurrent.futures
    if not HAS_PIL:
        print("[ERROR] Pillow is required for thumbnail generation"); return

    migrate_thumb_layout(thumb_dir)

    print(f"[THUMBS] Scanning {len(roots)} folder(s)...")
    all_images = []  # list of (db_path, abs_path)
    for root_name, root_abs in roots.items():
        if not os.path.isdir(root_abs): continue
        for dirpath, dirnames, filenames in os.walk(root_abs):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fname in filenames:
                if Path(fname).suffix.lower() in IMAGE_EXTENSIONS:
                    fpath = os.path.join(dirpath, fname)
                    db_path = root_name + "/" + os.path.relpath(fpath, root_abs).replace(os.sep, "/")
                    all_images.append((db_path, fpath))

    total = len(all_images)
    if total == 0:
        print("[THUMBS] No images found."); return

    existing = sum(1 for db_path, _ in all_images if os.path.exists(get_thumb_path(thumb_dir, db_path)))
    to_generate = total - existing
    print(f"[THUMBS] {total} images, {existing} thumbnails exist, {to_generate} to generate")
    if to_generate == 0:
        print("[THUMBS] All thumbnails up to date."); return

    if workers is None:
        workers = min(8, (os.cpu_count() or 4))
    print(f"[THUMBS] Generating with {workers} workers...")
    t0 = time.time()
    done = 0; skipped = 0; errors = 0
    lock = threading.Lock()

    def process(item):
        nonlocal done, skipped, errors
        db_path, full_path = item
        thumb_path = get_thumb_path(thumb_dir, db_path)
        if os.path.exists(thumb_path):
            try:
                if os.path.getmtime(full_path) <= os.path.getmtime(thumb_path):
                    with lock: skipped += 1; done += 1
                    return
            except OSError: pass
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        try:
            with Image.open(full_path) as img:
                img.thumbnail(THUMB_SIZE, LANCZOS)
                if img.mode in ("RGBA", "P"): img = img.convert("RGB")
                img.save(thumb_path, "WEBP", quality=80, method=4)
        except Exception:
            with lock: errors += 1; done += 1
            return
        with lock:
            done += 1
            if done % 500 == 0 or done == total:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                pct = done * 100 / total
                eta = (total - done) / rate if rate > 0 else 0
                print(f"[THUMBS] {done}/{total} ({pct:.1f}%) — {rate:.0f}/s — ETA {eta:.0f}s")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(process, item) for item in all_images]
        try:
            concurrent.futures.wait(futures)
        except KeyboardInterrupt:
            print(f"\n[THUMBS] Interrupted at {done}/{total}")
            executor.shutdown(wait=False, cancel_futures=True)
            return

    elapsed = time.time() - t0
    generated = done - skipped - errors
    print(f"[THUMBS] Done in {elapsed:.1f}s. {generated} generated, {skipped} skipped, {errors} errors.")


GALLERY_BODY = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gallery — {HUB_TITLE}</title>
{FONT_LINKS}
<style>
:root {
    --bg-darkest: #0a0c10; --bg-dark: #0f1218; --bg-panel: #141820;
    --bg-card: #1a1f2a; --bg-hover: #222838; --bg-active: #1c2436;
    --bg-input: #1a1f2a; --bg-main: #0a0c10; --bg: #0a0c10;
    --border: #1e2433; --border-light: #2a3040;
    --text: #c8cdd8; --text-dim: #6b7280; --text-bright: #e8ecf4;
    --accent: #4a9eff; --accent-dim: #2d6ab3; --accent-glow: rgba(74,158,255,0.08);
    --green: #3ddc84; --orange: #f59e0b; --red:#ef4444;
    --prompt-text: #a9b1d6; --neg-prompt: #f7768e;
    --setting-key: #9ece6a; --setting-val: #c0caf5;
    --radius: 6px;
    --font: 'IBM Plex Sans', -apple-system, sans-serif;
    --mono: 'JetBrains Mono', 'Consolas', monospace;
    --thumb-size: 180px;
}
body.theme-light {
    --bg-darkest:#f4f6fb; --bg-dark:#eef2f8; --bg-panel:#ffffff;
    --bg-card:#f7f9fd; --bg-hover:#edf3fb; --bg-active:#e5f0ff;
    --bg-input:#ffffff; --bg-main:#f4f6fb; --bg:#f4f6fb;
    --border:#d9e1ee; --border-light:#c7d3e4;
    --text:#334155; --text-dim:#718096; --text-bright:#111827;
    --accent:#2563eb; --accent-dim:#1d4ed8; --accent-glow:rgba(37,99,235,0.10);
    --green:#16a34a; --orange:#d97706; --red:#dc2626; --purple:#7c3aed;
    --prompt-text:#334155; --neg-prompt:#be123c;
    --setting-key:#3f7d20; --setting-val:#243b65;
}
@media (prefers-color-scheme: light) {
    body.theme-system {
        --bg-darkest:#f4f6fb; --bg-dark:#eef2f8; --bg-panel:#ffffff;
        --bg-card:#f7f9fd; --bg-hover:#edf3fb; --bg-active:#e5f0ff;
        --bg-input:#ffffff; --bg-main:#f4f6fb; --bg:#f4f6fb;
        --border:#d9e1ee; --border-light:#c7d3e4;
        --text:#334155; --text-dim:#718096; --text-bright:#111827;
        --accent:#2563eb; --accent-dim:#1d4ed8; --accent-glow:rgba(37,99,235,0.10);
        --green:#16a34a; --orange:#d97706; --red:#dc2626; --purple:#7c3aed;
        --prompt-text:#334155; --neg-prompt:#be123c;
        --setting-key:#3f7d20; --setting-val:#243b65;
    }
}
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--bg-darkest); color:var(--text); font-family:var(--font); font-size:13px; line-height:1.5; overflow:hidden; height:100vh; }

.app { display:grid; grid-template-columns:260px 1fr 370px; grid-template-rows:48px 1fr 28px; height:100vh; transition:grid-template-columns .18s ease; }
.app.meta-collapsed { grid-template-columns:260px 1fr 0; }
.topbar { grid-column:1/-1; background:var(--bg-dark); border-bottom:1px solid var(--border); display:flex; align-items:center; padding:0 16px; gap:16px; z-index:10; }
.sidebar { grid-row:2; background:var(--bg-panel); border-right:1px solid var(--border); overflow-y:auto; overflow-x:hidden; }
.gallery-area { grid-row:2; overflow-y:auto; background:var(--bg-dark); padding:0px 12px 12px 12px; position:relative; display:flex; flex-direction:column; }
.meta-panel { grid-row:2; background:var(--bg-panel); border-left:1px solid var(--border); overflow-y:auto; display:flex; flex-direction:column; min-width:0; transition:opacity .15s ease; }
.app.meta-collapsed .meta-panel { opacity:0; pointer-events:none; border-left:0; overflow:hidden; }
.statusbar { grid-column:1/-1; background:var(--bg-dark); border-top:1px solid var(--border); display:flex; align-items:center; padding:0 12px; font-size:11px; color:var(--text-dim); gap:16px; }

.topbar-title { font-family:var(--mono); font-weight:600; font-size:14px; color:var(--accent); letter-spacing:-0.3px; white-space:nowrap; text-decoration:none; cursor:pointer; }
.topbar-title:hover { color:var(--text-bright); }

/* Hub menu (hamburger + dropdown panel) */
.hub-menu { position:relative; }
.hub-menu-btn { background:none; border:1px solid transparent; color:var(--text-dim); width:32px; height:32px; display:flex; align-items:center; justify-content:center; border-radius:var(--radius); cursor:pointer; transition:all .15s; padding:0; }
.hub-menu-btn:hover { background:var(--bg-hover); color:var(--text); }
.hub-menu.open .hub-menu-btn { background:var(--bg-active); color:var(--accent); border-color:var(--accent-dim); }
.hub-menu-active { font-size:12px; color:var(--text); margin-left:2px; white-space:nowrap; display:flex; align-items:center; gap:5px; pointer-events:none; }
.hub-menu-active .icon { width:14px; height:14px; display:inline-flex; align-items:center; justify-content:center; color:currentColor; }
.hub-menu-panel { position:absolute; top:calc(100% + 4px); left:0; background:var(--bg-panel); border:1px solid var(--border-light); border-radius:var(--radius); min-width:520px; padding:4px; box-shadow:0 8px 24px rgba(0,0,0,.5); z-index:1000; opacity:0; visibility:hidden; transform:translateY(-4px); transition:opacity .12s, transform .12s, visibility .12s; display:grid; grid-template-columns:repeat(2, minmax(240px, 1fr)); gap:1px 4px; }
.hub-menu.open .hub-menu-panel { opacity:1; visibility:visible; transform:translateY(0); }
.hub-menu-item { display:flex; align-items:center; gap:10px; padding:8px 10px; border-radius:4px; text-decoration:none; color:var(--text); font-size:12px; transition:background .1s; cursor:pointer; min-width:0; }
.hub-menu-item:hover { background:var(--bg-hover); }
.hub-menu-item.active { background:var(--bg-active); color:var(--accent); }
.hub-menu-item .icon { width:20px; height:20px; display:inline-flex; align-items:center; justify-content:center; flex-shrink:0; color:currentColor; }
.hub-menu-item .icon svg, .hub-menu-active .icon svg { width:16px; height:16px; display:block; }
.hub-menu-item > span:not(.icon) { min-width:0; overflow:hidden; }
.hub-menu-item .label { display:block; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.hub-menu-item .desc { display:block; font-size:10px; color:var(--text-dim); font-weight:400; margin-top:1px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.hub-menu-sep { grid-column:1 / -1; height:1px; background:var(--border); margin:4px 6px; }
.hub-menu-panel > a:last-child { grid-column:1 / -1; }
@media (max-width:560px) { .hub-menu-panel { grid-template-columns:1fr; min-width:240px; } .hub-menu-panel > a:last-child { grid-column:auto; } }
.breadcrumb { display:flex; align-items:center; gap:4px; font-size:12px; color:var(--text-dim); flex:1; min-width:0; overflow:hidden; }
.breadcrumb span { cursor:pointer; padding:2px 6px; border-radius:3px; white-space:nowrap; transition:background .15s; }
.breadcrumb span:hover { background:var(--bg-hover); color:var(--text-bright); }
.breadcrumb .sep { color:var(--border-light); cursor:default; padding:0 2px; }
.breadcrumb .sep:hover { background:none; color:var(--border-light); }

.search-wrap { display:flex; align-items:center; gap:4px; }
.search-box { display:flex; align-items:center; background:var(--bg-card); border:1px solid var(--border); border-radius:var(--radius); padding:0 10px; gap:6px; width:220px; transition:border-color .2s; }
.search-box:focus-within { border-color:var(--accent); }
.search-box svg { color:var(--text-dim); flex-shrink:0; }
.search-box input { background:none; border:none; color:var(--text); font:inherit; font-size:12px; outline:none; width:100%; padding:5px 0; }
.search-clear { background:none; border:1px solid var(--border); color:var(--text-dim); font-size:11px; padding:4px 8px; border-radius:var(--radius); cursor:pointer; display:none; transition:all .15s; white-space:nowrap; }
.search-clear:hover { background:var(--bg-hover); color:var(--text); }
.search-clear.visible { display:block; }

.topbar-controls { display:flex; gap:4px; align-items:center; }
.btn-icon { background:none; border:1px solid transparent; color:var(--text-dim); width:32px; height:32px; display:flex; align-items:center; justify-content:center; border-radius:var(--radius); cursor:pointer; transition:all .15s; }
.btn-icon:hover { background:var(--bg-hover); color:var(--text); }
.btn-icon.active { background:var(--bg-active); color:var(--accent); border-color:var(--accent-dim); }
.sort-select { background:var(--bg-card); border:1px solid var(--border); color:var(--text); font:inherit; font-size:11px; padding:4px 8px; border-radius:var(--radius); cursor:pointer; outline:none; }

/* Sidebar */
.sidebar-header { padding:12px 14px 8px; font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.8px; color:var(--text-dim); display:flex; justify-content:space-between; align-items:center; }
.sidebar-header .search-badge { background:var(--accent); color:#fff; font-size:9px; padding:1px 6px; border-radius:8px; font-weight:500; }

.folder-item { display:flex; align-items:center; padding:5px 10px; cursor:pointer; border-left:3px solid transparent; transition:all .12s; gap:6px; font-size:12px; color:var(--text); user-select:none; }
.folder-item:hover { background:var(--bg-hover); }
.folder-item.active { background:var(--bg-active); border-left-color:var(--accent); color:var(--text-bright); }

.folder-toggle { width:16px; height:16px; display:flex; align-items:center; justify-content:center; font-size:10px; color:var(--text-dim); transition:transform .15s; flex-shrink:0; }
.folder-toggle.open { transform:rotate(90deg); }
.folder-toggle.empty { visibility:hidden; }

.folder-icon { width:28px; height:28px; border-radius:4px; overflow:hidden; flex-shrink:0; background:var(--bg-card); display:flex; align-items:center; justify-content:center; font-size:14px; color:var(--text-dim); }
.folder-icon svg { width:15px; height:15px; display:block; }
.folder-icon img { width:100%; height:100%; object-fit:cover; }
.folder-name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.folder-count { font-size:10px; color:var(--text-dim); background:var(--bg-card); padding:1px 6px; border-radius:10px; font-family:var(--mono); }
.folder-count.search-count { background:var(--accent-dim); color:#fff; }
.folder-children { display:none; }
.folder-children.open { display:block; }

/* Gallery Grid */
.gallery-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(var(--thumb-size),1fr)); gap:8px; flex:1; align-content:start; }
.thumb-card { position:relative; border-radius:var(--radius); overflow:hidden; background:var(--bg-card); cursor:pointer; border:2px solid transparent; transition:all .15s; aspect-ratio:1; }
.thumb-card:hover { border-color:var(--border-light); transform:translateY(-1px); }
.thumb-card.selected { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent-dim); }
.thumb-card img { width:100%; height:100%; object-fit:cover; display:block; background:var(--bg-darkest); }
.thumb-overlay { position:absolute; bottom:0; left:0; right:0; background:linear-gradient(transparent,rgba(0,0,0,.85)); padding:24px 8px 6px; pointer-events:none; }
.thumb-name { font-size:10px; color:#eee; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.thumb-meta-badge { position:absolute; top:6px; right:6px; width:8px; height:8px; border-radius:50%; background:var(--green); box-shadow:0 0 6px var(--green); }
.thumb-fav { position:absolute; top:5px; right:18px; font-size:14px; cursor:pointer; z-index:3; opacity:0; transition:opacity .15s; line-height:1; filter:drop-shadow(0 1px 2px rgba(0,0,0,.8)); }
.thumb-card:hover .thumb-fav { opacity:1; }
.thumb-fav.active { opacity:1; }
.thumb-dims { position:absolute; top:6px; left:6px; font-size:9px; font-family:var(--mono); color:rgba(255,255,255,.7); background:rgba(0,0,0,.5); padding:1px 4px; border-radius:3px; pointer-events:none; opacity:0; transition:opacity .15s; }
.thumb-card:hover .thumb-dims { opacity:1; }

/* Sidebar special items */
.sidebar-divider { height:1px; background:var(--border); margin:8px 14px; }
.sidebar-special { padding:5px 14px; cursor:pointer; display:flex; align-items:center; gap:8px; font-size:12px; color:var(--text); transition:all .12s; border-left:3px solid transparent; }
.sidebar-special:hover { background:var(--bg-hover); }
.sidebar-special.active { background:var(--bg-active); border-left-color:var(--accent); color:var(--text-bright); }
.sidebar-special .ss-icon { width:16px; height:16px; display:inline-flex; align-items:center; justify-content:center; color:currentColor; }
.sidebar-special .ss-icon svg { width:15px; height:15px; display:block; }
.sidebar-special .ss-count { font-size:10px; color:var(--text-dim); background:var(--bg-card); padding:1px 6px; border-radius:10px; font-family:var(--mono); margin-left:auto; }

/* Meta panel favorite */
.meta-fav-btn { cursor:pointer; font-size:16px; margin:0 10px 0 8px; width:28px; display:flex; align-items:center; justify-content:center; transition:transform .15s, color .15s; flex-shrink:0; }
.meta-fav-btn:hover { transform:scale(1.15); color:var(--orange); }
.meta-delete-btn { color:var(--text-dim); border-color:var(--border); }
.meta-delete-btn:hover { color:var(--red); border-color:var(--red); background:rgba(239,68,68,.10); }

/* Civitai model info */
.meta-civitai { background:var(--bg-card); border:1px solid var(--accent-dim); border-radius:var(--radius); padding:8px 10px; font-size:11px; }
.meta-civitai-name { font-weight:600; color:var(--accent); }
.meta-civitai-name a { color:var(--accent); text-decoration:none; }
.meta-civitai-name a:hover { text-decoration:underline; }
.meta-civitai-detail { color:var(--text-dim); font-family:var(--mono); font-size:10px; margin-top:2px; }
.meta-civitai-words { margin-top:4px; display:flex; flex-wrap:wrap; gap:3px; }
.meta-civitai-words span { background:var(--bg-hover); border:1px solid var(--border); padding:1px 6px; border-radius:8px; font-size:9px; color:var(--text); font-family:var(--mono); }

/* Collections */
.coll-list { padding:0 0 4px 0; }
.coll-item { display:flex; align-items:center; padding:4px 14px; cursor:pointer; gap:6px; font-size:12px; color:var(--text); transition:all .12s; border-left:3px solid transparent; }
.coll-item:hover { background:var(--bg-hover); }
.coll-item.active { background:var(--bg-active); border-left-color:var(--accent); color:var(--text-bright); }
.coll-dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; }
.coll-name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.coll-cnt { font-size:10px; color:var(--text-dim); background:var(--bg-card); padding:1px 6px; border-radius:10px; font-family:var(--mono); }
.coll-del { font-size:11px; color:var(--text-dim); cursor:pointer; opacity:0; transition:all .15s; padding:0 2px; flex-shrink:0; }
.coll-item:hover .coll-del { opacity:1; }
.coll-del:hover { color:var(--red); }
.coll-add-btn { padding:4px 14px; cursor:pointer; font-size:11px; color:var(--accent); transition:all .12s; display:flex; align-items:center; gap:4px; }
.coll-add-btn:hover { background:var(--bg-hover); }

/* Collection picker dropdown in selection bar */
.coll-picker { position:relative; }
.coll-picker-menu { position:absolute; bottom:100%; left:0; background:var(--bg-panel); border:1px solid var(--border); border-radius:var(--radius); min-width:180px; max-height:240px; overflow-y:auto; box-shadow:0 8px 24px rgba(0,0,0,.5); display:none; margin-bottom:4px; z-index:910; }
.coll-picker-menu.open { display:block; }
.coll-picker-item { padding:6px 12px; cursor:pointer; font-size:12px; display:flex; align-items:center; gap:6px; transition:background .1s; }
.coll-picker-item:hover { background:var(--bg-hover); }
.coll-picker-input { width:100%; background:var(--bg-card); border:none; border-bottom:1px solid var(--border); color:var(--text); font:inherit; font-size:12px; padding:8px 12px; outline:none; }
.coll-picker-input::placeholder { color:var(--text-dim); }

/* Meta panel collection badges */
.meta-collections { display:flex; flex-wrap:wrap; gap:4px; margin-top:6px; }
.meta-coll-badge { display:flex; align-items:center; gap:4px; font-size:10px; padding:2px 8px; border-radius:10px; background:var(--bg-card); border:1px solid var(--border); color:var(--text); }
.meta-coll-badge .coll-dot { width:6px; height:6px; }
.meta-coll-badge .coll-remove { cursor:pointer; color:var(--text-dim); margin-left:2px; font-size:12px; line-height:1; transition:color .15s; }
.meta-coll-badge .coll-remove:hover { color:var(--red); }

.gallery-empty { display:flex; align-items:center; justify-content:center; flex:1; color:var(--text-dim); font-size:14px; flex-direction:column; gap:8px; text-align:center; min-height:320px; }
.gallery-empty strong { color:var(--text); font-size:15px; font-weight:600; }
.gallery-empty span { max-width:360px; }

/* Sticky gallery toolbar */
.gallery-toolbar {
    position:sticky; top:0; z-index:5;
    background:var(--bg-dark); border-bottom:1px solid var(--border);
    display:flex; align-items:center; justify-content:space-between;
    padding:15px 4px; margin:0 -12px 8px -12px;
    font-size:11px; font-family:var(--mono); color:var(--text-dim);
    min-height:32px;
}
.gallery-toolbar .gt-info { padding-left:8px; }
.gallery-toolbar .gt-nav { display:flex; align-items:center; gap:4px; padding-right:4px; }
.gallery-toolbar .gt-nav button {
    background:var(--bg-card); border:1px solid var(--border); color:var(--text-dim);
    font:inherit; font-size:12px; padding:3px 10px; border-radius:var(--radius);
    cursor:pointer; transition:all .15s; font-family:var(--mono);
}
.gallery-toolbar .gt-nav button:hover:not(:disabled) { background:var(--bg-hover); color:var(--text); border-color:var(--border-light); }
.gallery-toolbar .gt-nav button:disabled { opacity:.3; cursor:default; }
.gallery-toolbar .gt-nav .gt-page { color:var(--text); padding:0 6px; font-size:11px; cursor:pointer; border-radius:3px; transition:background .15s; }
.gallery-toolbar .gt-nav .gt-page:hover { background:var(--bg-hover); }
.gallery-toolbar .gt-nav .gt-page-input {
    width:48px; background:var(--bg-card); border:1px solid var(--accent); color:var(--text);
    font:inherit; font-size:11px; font-family:var(--mono); padding:2px 4px; border-radius:var(--radius);
    text-align:center; outline:none; -moz-appearance:textfield;
}
.gallery-toolbar .gt-nav .gt-page-input::-webkit-outer-spin-button,
.gallery-toolbar .gt-nav .gt-page-input::-webkit-inner-spin-button { -webkit-appearance:none; margin:0; }

/* Pagination */
.pagination { display:flex; align-items:center; justify-content:center; gap:4px; padding:10px 0 4px; flex-shrink:0; }
.pagination button {
    background:var(--bg-card); border:1px solid var(--border); color:var(--text-dim);
    font:inherit; font-size:11px; padding:4px 10px; border-radius:var(--radius);
    cursor:pointer; transition:all .15s; font-family:var(--mono);
}
.pagination button:hover:not(:disabled) { background:var(--bg-hover); color:var(--text); border-color:var(--border-light); }
.pagination button:disabled { opacity:.3; cursor:default; }
.pagination button.active { background:var(--accent-glow); color:var(--accent); border-color:var(--accent-dim); }
.pagination .page-info { font-size:11px; color:var(--text-dim); padding:0 8px; font-family:var(--mono); }

/* Meta Panel */
.meta-empty { display:flex; align-items:center; justify-content:center; height:100%; color:var(--text-dim); font-size:13px; padding:20px; text-align:center; }
.meta-panel-head { flex-shrink:0; display:flex; align-items:center; justify-content:space-between; gap:8px; padding:8px 10px; background:var(--bg-dark); border-bottom:1px solid var(--border); }
.meta-panel-title { font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.7px; color:var(--text-dim); overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
.meta-panel-controls { display:flex; align-items:center; gap:4px; }
.meta-mini-btn { background:none; border:1px solid transparent; color:var(--text-dim); height:24px; min-width:24px; padding:0 7px; border-radius:var(--radius); font:11px var(--font); cursor:pointer; display:inline-flex; align-items:center; justify-content:center; gap:4px; transition:all .15s; }
.meta-mini-btn:hover { background:var(--bg-hover); color:var(--text); }
.meta-mini-btn.active { background:var(--bg-active); color:var(--accent); border-color:var(--accent-dim); }
.meta-preview { width:100%; max-height:340px; overflow:hidden; background:var(--bg-darkest); flex-shrink:0; display:flex; align-items:center; justify-content:center; cursor:pointer; position:relative; }
.meta-preview img { max-width:100%; max-height:340px; object-fit:contain; }
.meta-tabs { display:flex; align-items:center; background:var(--bg-dark); border-bottom:1px solid var(--border); flex-shrink:0; padding-right:4px; }
.meta-tab { flex:1; padding:8px 12px; text-align:center; font-size:11px; font-weight:500; color:var(--text-dim); cursor:pointer; border-bottom:2px solid transparent; transition:all .15s; background:none; border-top:none; border-left:none; border-right:none; }
.meta-tab:hover { color:var(--text); }
.meta-tab.active { color:var(--accent); border-bottom-color:var(--accent); }
.meta-content { flex:1; overflow-y:auto; padding:12px; }
.meta-section { margin-bottom:14px; }
.meta-section-title { display:flex; align-items:center; gap:6px; font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.6px; color:var(--text-dim); margin-bottom:6px; padding-bottom:4px; border-bottom:1px solid var(--border); }
.meta-section-title .copy-btn { margin-left:auto; cursor:pointer; color:var(--text-dim); font-size:11px; font-weight:400; text-transform:none; letter-spacing:0; transition:color .15s; }
.meta-section-title .copy-btn:hover { color:var(--accent); }
.meta-prompt { font-family:var(--mono); font-size:11px; line-height:1.6; color:var(--prompt-text); word-break:break-word; white-space:pre-wrap; background:var(--bg-card); padding:8px 10px; border-radius:var(--radius); border:1px solid var(--border); }
.meta-prompt.negative { color:var(--neg-prompt); }
.meta-settings-grid { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; font-family:var(--mono); font-size:11px; }
.meta-key { color:var(--setting-key); white-space:nowrap; }
.meta-val { color:var(--setting-val); word-break:break-all; }
.meta-raw { font-family:var(--mono); font-size:10px; line-height:1.6; color:var(--text); white-space:pre-wrap; word-break:break-all; background:var(--bg-card); padding:10px; border-radius:var(--radius); border:1px solid var(--border); }
.meta-file-info { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; font-size:11px; }
.meta-file-info .label { color:var(--text-dim); }
.meta-file-info .value { color:var(--text); font-family:var(--mono); }

/* Lightbox */
.lightbox { position:fixed; inset:0; background:rgba(0,0,0,.92); z-index:1000; display:none; align-items:center; justify-content:center; cursor:zoom-out; }
.lightbox.open { display:flex; }
.lightbox img { max-width:95vw; max-height:95vh; object-fit:contain; }
.lightbox-nav { position:absolute; top:50%; transform:translateY(-50%); background:rgba(255,255,255,.1); border:none; color:white; font-size:28px; width:48px; height:64px; cursor:pointer; display:flex; align-items:center; justify-content:center; border-radius:6px; transition:background .15s; }
.lightbox-nav:hover { background:rgba(255,255,255,.2); }
.lightbox-nav.prev { left:16px; }
.lightbox-nav.next { right:16px; }
.lightbox-close, .lightbox-delete { position:absolute; top:16px; background:rgba(255,255,255,.1); border:none; color:white; font-size:20px; width:40px; height:40px; cursor:pointer; border-radius:6px; display:flex; align-items:center; justify-content:center; transition:background .15s, color .15s; }
.lightbox-close { right:16px; }
.lightbox-delete { right:64px; font-size:18px; }
.lightbox-close:hover { background:rgba(255,255,255,.2); }
.lightbox-delete:hover { background:rgba(239,68,68,.25); color:#fff; }
.lightbox-counter { position:absolute; bottom:20px; left:50%; transform:translateX(-50%); color:rgba(255,255,255,.6); font-family:var(--mono); font-size:12px; }

::-webkit-scrollbar { width:6px; height:6px; }
::-webkit-scrollbar-track { background:transparent; }
::-webkit-scrollbar-thumb { background:var(--border-light); border-radius:3px; }
::-webkit-scrollbar-thumb:hover { background:var(--text-dim); }

.loading-bar { position:absolute; top:0; left:0; right:0; height:2px; background:var(--accent); transform:scaleX(0); transform-origin:left; z-index:100; pointer-events:none; }
.loading-bar.active { animation:loading-pulse 1.5s ease-in-out infinite; }
@keyframes loading-pulse { 0%{transform:scaleX(0);opacity:1} 50%{transform:scaleX(.7);opacity:1} 100%{transform:scaleX(1);opacity:0} }

.thumb-slider { width:80px; -webkit-appearance:none; height:3px; background:var(--border-light); border-radius:2px; outline:none; }
.thumb-slider::-webkit-slider-thumb { -webkit-appearance:none; width:12px; height:12px; border-radius:50%; background:var(--accent); cursor:pointer; }

.toast { position:fixed; bottom:40px; right:16px; background:var(--bg-card); border:1px solid var(--border); color:var(--text); padding:8px 14px; border-radius:var(--radius); font-size:12px; z-index:2000; animation:toast-in .2s ease-out; box-shadow:0 4px 16px rgba(0,0,0,.4); }
@keyframes toast-in { from{opacity:0;transform:translateY(8px)} }

/* Multi-select */
.thumb-card.multi-selected { border-color:var(--orange); box-shadow:0 0 0 1px var(--orange); }
.thumb-card.multi-selected::after {
    content:'\2713'; position:absolute; top:4px; left:4px; width:20px; height:20px;
    background:var(--orange); color:#fff; border-radius:50%; font-size:12px;
    display:flex; align-items:center; justify-content:center; z-index:5;
    pointer-events:none; font-weight:bold;
}

.selection-bar {
    position:fixed; bottom:40px; left:50%; transform:translateX(-50%);
    background:var(--bg-card); border:1px solid var(--orange); border-radius:8px;
    padding:8px 16px; display:none; align-items:center; gap:12px; z-index:900;
    box-shadow:0 8px 32px rgba(0,0,0,.5); font-size:12px;
    animation:toast-in .2s ease-out;
}
.selection-bar.visible { display:flex; }
.selection-bar .sel-count { color:var(--orange); font-family:var(--mono); font-weight:600; }
.selection-bar .sel-btn {
    background:none; border:1px solid var(--border); color:var(--text); font:inherit;
    font-size:11px; padding:4px 12px; border-radius:var(--radius); cursor:pointer; transition:all .15s;
}
.selection-bar .sel-btn:hover { background:var(--bg-hover); }
.selection-bar .sel-btn.danger { border-color:var(--red); color:var(--red); }
.selection-bar .sel-btn.danger:hover { background:rgba(239,68,68,.15); }

/* Confirm dialog */
.confirm-overlay {
    position:fixed; inset:0; background:rgba(0,0,0,.7); z-index:2000;
    display:none; align-items:center; justify-content:center;
}
.confirm-overlay.open { display:flex; }
.confirm-dialog {
    background:var(--bg-panel); border:1px solid var(--border); border-radius:10px;
    padding:24px; max-width:400px; width:90%;
    box-shadow:0 16px 48px rgba(0,0,0,.6);
}
.confirm-dialog h3 { font-size:15px; color:var(--text-bright); margin-bottom:8px; font-weight:600; }
.confirm-dialog p { font-size:12px; color:var(--text-dim); margin-bottom:16px; line-height:1.6; }
.confirm-dialog .btn-row { display:flex; gap:8px; justify-content:flex-end; }
.confirm-dialog button {
    padding:6px 16px; border-radius:var(--radius); font:inherit; font-size:12px; cursor:pointer; border:1px solid var(--border); transition:all .15s;
}
.confirm-dialog .btn-cancel { background:var(--bg-card); color:var(--text); }
.confirm-dialog .btn-cancel:hover { background:var(--bg-hover); }
.confirm-dialog .btn-delete { background:var(--red); color:#fff; border-color:var(--red); }
.confirm-dialog .btn-delete:hover { background:#dc2626; }
.confirm-dialog button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }

/* Forge button */
.meta-forge-btn { width:100%;background:#7c3aed;color:#fff;border:none;border-radius:6px;padding:7px 0;cursor:pointer;font-size:13px;transition:background .15s; }
.meta-forge-btn:hover { background:#6d28d9; }
.meta-forge-btn:disabled { opacity:.5; cursor:not-allowed; }
.meta-forge-status { font-size:11px; margin-top:8px; padding:6px 10px; border-radius:6px; display:none; }
.meta-forge-status.loading { display:block; color:var(--text-dim); }
.meta-forge-status.success { display:block; color:#4ade80; background:rgba(74,222,128,.08); }
.meta-forge-status.error { display:block; color:#f87171; background:rgba(248,113,113,.08); }
</style>
</head>
<body class="{BODY_CLASS}">
<div class="app" id="app">
    <div class="topbar">
        {MODULE_NAV}
        <a href="/" class="topbar-title">&#x2B21; {HUB_TITLE}</a>
        <div class="breadcrumb" id="breadcrumb"></div>
        <div class="search-wrap">
            <div class="search-box">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
                <input type="text" id="searchInput" placeholder="Search files & prompts...">
            </div>
            <button class="search-clear" id="searchClear">&#x2715; Clear</button>
        </div>
        <div class="topbar-controls">
            <select class="sort-select" id="sortSelect">
                <option value="name-asc">Name A-Z</option>
                <option value="name-desc">Name Z-A</option>
                <option value="date-desc" selected>Newest</option>
                <option value="date-asc">Oldest</option>
                <option value="size-desc">Largest</option>
                <option value="size-asc">Smallest</option>
                <option value="favorite-desc">&#x2B50; Favorites first</option>
            </select>
            <input type="range" class="thumb-slider" id="thumbSlider" min="100" max="400" value="180" title="Thumbnail size">
            <button class="btn-icon" id="refreshBtn" title="Re-index">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 2v6h-6"/><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M3 22v-6h6"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/></svg>
            </button>
            <button class="btn-icon" id="metaToggleBtn" title="Toggle metadata panel">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/><path d="M7 8h4"/><path d="M7 12h4"/></svg>
            </button>
            <button class="btn-icon theme-toggle-btn" type="button" title="Toggle dark/light mode" aria-label="Toggle dark/light mode" onclick="hubToggleTheme()">
                <svg class="theme-icon theme-icon-moon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A8.5 8.5 0 1 1 11.2 3 6.6 6.6 0 0 0 21 12.8z"/></svg>
                <svg class="theme-icon theme-icon-sun" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>
            </button>
            <button class="btn-icon help-btn" type="button" id="helpBtn" title="Help for this page" aria-label="Help" data-help-key="gallery" data-help-title="Gallery" onclick="hubOpenHelp()">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg>
            </button>
            <a class="btn-icon" href="/settings" id="settingsLink" title="Settings">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            </a>
        </div>
    </div>
    {HELP_OVERLAY}

    <div class="sidebar" id="sidebar">
        <div id="sidebarSpecial">
            <div class="sidebar-special" id="navFavorites">
                <span class="ss-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l2.7 5.5 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.8 1-6.1-4.4-4.3 6.1-.9L12 3z"/></svg></span>
                <span>Favorites</span>
                <span class="ss-count" id="favCount">0</span>
            </div>
            <div class="sidebar-special" id="navToday">
                <span class="ss-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="5" width="16" height="15" rx="2"/><path d="M8 3v4M16 3v4M4 10h16"/><path d="M9 14h1M14 14h1M9 17h1"/></svg></span>
                <span>Today</span>
            </div>
            <div class="sidebar-special" id="navWeek">
                <span class="ss-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="5" width="16" height="15" rx="2"/><path d="M8 3v4M16 3v4M4 10h16"/><path d="M8 14h8M8 17h5"/></svg></span>
                <span>Last 7 days</span>
            </div>
            <div class="sidebar-special" id="navMonth">
                <span class="ss-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="5" width="16" height="15" rx="2"/><path d="M8 3v4M16 3v4M4 10h16"/><path d="M8 14h8M8 17h8"/></svg></span>
                <span>Last 30 days</span>
            </div>
            <div class="sidebar-divider"></div>
        </div>
        <div class="sidebar-header">Collections <span class="coll-add-btn" id="collAddBtn">+ new</span></div>
        <div class="coll-list" id="collList"></div>
        <div class="sidebar-divider"></div>
        <div class="sidebar-header" id="sidebarHeader">Folders</div>
        <div id="folderTree"></div>
    </div>

    <div class="gallery-area" id="galleryArea">
        <div class="loading-bar" id="loadingBar"></div>
        <div class="gallery-toolbar" id="galleryToolbar" style="display:none">
            <div class="gt-info" id="galleryCount"></div>
            <div class="gt-nav">
                <button id="gtPrev" title="Previous page">&#x2039;</button>
                <span class="gt-page" id="gtPage" title="Click to jump to page"></span>
                <input class="gt-page-input" id="gtPageInput" type="number" min="1" style="display:none">
                <button id="gtNext" title="Next page">&#x203A;</button>
            </div>
        </div>
        <div class="gallery-grid" id="galleryGrid"></div>
        <div class="gallery-empty" id="galleryEmpty">
            <strong id="galleryEmptyTitle">Loading gallery...</strong>
            <span id="galleryEmptyDetail" style="font-size:11px">If the index is running, new images will appear here as they are processed.</span>
        </div>
        <div class="pagination" id="pagination" style="display:none"></div>
    </div>

    <div class="meta-panel" id="metaPanel">
        <div class="meta-empty">Click an image to view its generation metadata</div>
    </div>

    <div class="lightbox" id="lightbox">
        <img id="lightboxImg" src="">
        <button class="lightbox-nav prev" id="lbPrev">&#x276E;</button>
        <button class="lightbox-nav next" id="lbNext">&#x276F;</button>
        <button class="lightbox-delete" id="lbDelete" title="Move current image to trash">&#x1F5D1;</button>
        <button class="lightbox-close" id="lbClose">&#x2715;</button>
        <div class="lightbox-counter" id="lbCounter"></div>
    </div>

    <div class="statusbar">
        <span id="statusFiles">-</span>
        <span id="statusFolders">-</span>
        <span id="statusMeta">-</span>
        <span id="statusFav">-</span>
    </div>
</div>

<div class="selection-bar" id="selectionBar">
    <span class="sel-count" id="selCount">0 selected</span>
    <div class="coll-picker">
        <button class="sel-btn" id="selCollBtn">&#x1F4C2; Add to...</button>
        <div class="coll-picker-menu" id="collPickerMenu">
            <input class="coll-picker-input" id="collPickerInput" placeholder="New collection name..." autocomplete="off">
            <div id="collPickerList"></div>
        </div>
    </div>
    <button class="sel-btn" id="selRemColl" style="display:none">&#x2716; Remove from collection</button>
    <button class="sel-btn" id="selCompare" onclick="openCompare()">&#x1F50D; Compare</button>
    <button class="sel-btn" id="selClear">Clear</button>
    <button class="sel-btn danger" id="selDelete">&#x1F5D1; Delete</button>
</div>

<div class="confirm-overlay" id="confirmOverlay">
    <div class="confirm-dialog">
        <h3 id="confirmTitle">Delete images?</h3>
        <p id="confirmMsg"></p>
        <div class="btn-row">
            <button class="btn-cancel" id="confirmCancel">Cancel</button>
            <button class="btn-delete" id="confirmOk">Delete</button>
        </div>
    </div>
</div>

<script>
/* Forge enabled flag (set from server) */
window._forgeEnabled = {FORGE_ENABLED};

/* Hub hamburger menu: toggle on click, close on outside-click or Esc */
(function() {
    function init() {
        document.addEventListener('click', function(ev) {
            var btn = ev.target.closest('.hub-menu-btn');
            var menu = document.querySelector('.hub-menu');
            if (!menu) return;
            if (btn && menu.contains(btn)) {
                ev.preventDefault();
                menu.classList.toggle('open');
            } else if (!ev.target.closest('.hub-menu-panel')) {
                menu.classList.remove('open');
            }
        });
        document.addEventListener('keydown', function(ev) {
            if (ev.key === 'Escape') {
                var menu = document.querySelector('.hub-menu.open');
                if (menu) menu.classList.remove('open');
            }
        });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();

var API = {
    get: function(url) {
        return fetch(url).then(function(r){
            return r.json().catch(function(){ return {}; }).then(function(body){
                if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
                return body;
            });
        }).catch(function(e){ showToast('Error: '+e.message); return null; });
    },
    post: function(url, data) {
        return fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)}).then(function(r){
            return r.json().catch(function(){ return {}; }).then(function(body){
                if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
                return body;
            });
        }).catch(function(e){ showToast('Error: '+e.message); return null; });
    }
};

var currentFolder = '';
var selectedFile = null;
var currentFiles = [];
var currentMetaTab = 'metadata';
var accordionFolders = {ACCORDION_FOLDERS};
var skipDeleteConfirmation = {SKIP_DELETE_CONFIRMATION};
// Folder-tree expansion is remembered across navigation/sessions.
var expandedFolders = new Set();
try { expandedFolders = new Set(JSON.parse(localStorage.getItem('galleryExpanded') || '[]')); } catch (e) {}
if (accordionFolders && expandedFolders.size > 1) expandedFolders = new Set();
function saveExpandedFolders() { try { localStorage.setItem('galleryExpanded', JSON.stringify(Array.from(expandedFolders))); } catch (e) {} }
var lightboxIndex = 0;
var currentPage = 1;
var totalPages = 1;
var searchMode = false;
var searchQuery = '';
var searchFolderFilter = null;
var multiSelected = new Set(); // Set of file paths
var hasTrash = true; // Updated at init from server
var metaCache = {}; // path -> metadata cache
var lastGalleryTotal = 0;
var specialView = null; // null, 'favorites', 'today', '7days', '30days'
var metaPanelPinned = localStorage.getItem('galleryMetaPinned') === '1';
var metaPanelCollapsed = !metaPanelPinned;
var GALLERY_STATE_KEY = 'cyberdelia.gallery.state';
var pendingRestoreScroll = null;
var galleryStateTimer = null;
var ICON_FOLDER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H10l2 2h6.5A2.5 2.5 0 0 1 21 9.5v7A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5v-9z"/></svg>';
var ICON_SEARCH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>';
var ICON_PIN = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 10.8a2 2 0 0 1-1.1 1.8l-1.8.9A2 2 0 0 0 5 15.2V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.8a2 2 0 0 0-1.1-1.8l-1.8-.9A2 2 0 0 1 15 10.8V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>';

function loadGalleryState() {
    try { return JSON.parse(localStorage.getItem(GALLERY_STATE_KEY) || '{}') || {}; }
    catch (e) { return {}; }
}

function saveGalleryStateNow() {
    try {
        var area = document.getElementById('galleryArea');
        localStorage.setItem(GALLERY_STATE_KEY, JSON.stringify({
            folder: currentFolder || '',
            page: currentPage || 1,
            sort: document.getElementById('sortSelect') ? document.getElementById('sortSelect').value : 'date-desc',
            searchMode: !!searchMode,
            searchQuery: searchQuery || '',
            searchFolderFilter: searchFolderFilter,
            specialView: specialView || '',
            selectedImage: selectedFile || '',
            scrollTop: area ? area.scrollTop : 0
        }));
    } catch (e) {}
}

function scheduleGalleryStateSave() {
    clearTimeout(galleryStateTimer);
    galleryStateTimer = setTimeout(saveGalleryStateNow, 150);
}

function restoreGalleryScrollIfNeeded() {
    if (pendingRestoreScroll === null || pendingRestoreScroll === undefined) return;
    var y = pendingRestoreScroll;
    pendingRestoreScroll = null;
    setTimeout(function() {
        var area = document.getElementById('galleryArea');
        if (area) area.scrollTop = y || 0;
    }, 80);
}

window.addEventListener('pagehide', saveGalleryStateNow);
document.addEventListener('visibilitychange', function() {
    if (document.visibilityState === 'hidden') saveGalleryStateNow();
});

function applyMetaPanelState() {
    var app = document.getElementById('app');
    var toggleBtn = document.getElementById('metaToggleBtn');
    if (!app) return;
    app.classList.toggle('meta-collapsed', metaPanelCollapsed);
    if (toggleBtn) {
        toggleBtn.classList.toggle('active', !metaPanelCollapsed);
        toggleBtn.title = metaPanelCollapsed ? 'Show metadata panel' : 'Hide metadata panel';
    }
}

function setMetaPanelCollapsed(collapsed) {
    metaPanelCollapsed = collapsed;
    applyMetaPanelState();
}

function setMetaPanelPinned(pinned) {
    metaPanelPinned = pinned;
    localStorage.setItem('galleryMetaPinned', pinned ? '1' : '0');
    applyMetaPanelState();
}

// ═══════════════════════════════════════════════════════
// ── Folder Tree (lazy-load)
// ═══════════════════════════════════════════════════════

async function loadFolderTree() {
    var tree = document.getElementById('folderTree');
    tree.innerHTML = '';
    document.getElementById('sidebarHeader').innerHTML = 'Folders';

    var rootWrap = document.createElement('div');
    rootWrap.className = 'folder-wrapper';
    var rootEl = document.createElement('div');
    rootEl.className = 'folder-item active';
    rootEl.dataset.path = '';
    rootEl.innerHTML = '<span class="folder-toggle open">&#x25B6;</span><div class="folder-icon">' + ICON_FOLDER + '</div><span class="folder-name">Root</span>';
    rootEl.addEventListener('click', function() {
        exitSearchMode();
        document.querySelectorAll('.folder-item').forEach(function(f){f.classList.remove('active')});
        rootEl.classList.add('active');
        navigateTo('');
    });
    rootWrap.appendChild(rootEl);
    var rootChildren = document.createElement('div');
    rootChildren.className = 'folder-children open';
    rootWrap.appendChild(rootChildren);
    tree.appendChild(rootWrap);

    var data = await API.get('/api/folders?parent=');
    if (!data) return;
    for (var i = 0; i < data.length; i++) {
        rootChildren.appendChild(createFolderItem(data[i], 1));
    }
}

async function loadChildrenFor(wrapper, parentPath) {
    var childDiv = wrapper.querySelector(':scope > .folder-children');
    if (!childDiv) { childDiv = document.createElement('div'); childDiv.className = 'folder-children'; wrapper.appendChild(childDiv); }
    if (childDiv.dataset.loaded) return;
    childDiv.dataset.loaded = '1';
    var data = await API.get('/api/folders?parent=' + encodeURIComponent(parentPath));
    if (!data) return;
    for (var i = 0; i < data.length; i++) {
        childDiv.appendChild(createFolderItem(data[i], getDepth(data[i].path)));
    }
}

function getDepth(path) { return path ? path.split(/[\\/]/).length : 0; }

function closeSiblingFolders(wrapper, folderPath) {
    if (!accordionFolders || !wrapper || !wrapper.parentElement) return;
    var siblings = wrapper.parentElement.children;
    for (var i = 0; i < siblings.length; i++) {
        var sib = siblings[i];
        if (sib === wrapper || !sib.classList || !sib.classList.contains('folder-wrapper')) continue;
        var sibItem = sib.querySelector(':scope > .folder-item');
        var sibChildren = sib.querySelector(':scope > .folder-children');
        var sibToggle = sibItem ? sibItem.querySelector('.folder-toggle') : null;
        if (sibChildren) sibChildren.classList.remove('open');
        if (sibToggle) sibToggle.classList.remove('open');
        if (sibItem && sibItem.dataset && sibItem.dataset.path) {
            expandedFolders.delete(sibItem.dataset.path);
        }
    }
    if (folderPath) {
        expandedFolders.forEach(function(p) {
            if (p !== folderPath && folderPath.indexOf(p + '/') !== 0 && p.indexOf(folderPath + '/') !== 0) {
                expandedFolders.delete(p);
            }
        });
    }
}

function shouldRestoreFolderOpen(path) {
    if (!path) return false;
    if (!accordionFolders) return expandedFolders.has(path);
    return !!currentFolder && (currentFolder === path || currentFolder.indexOf(path + '/') === 0);
}

function createFolderItem(folder, depth) {
    var wrapper = document.createElement('div');
    wrapper.className = 'folder-wrapper';
    var el = document.createElement('div');
    el.className = 'folder-item';
    el.style.paddingLeft = (8 + depth * 18) + 'px';
    el.dataset.path = folder.path;
    var toggleClass = folder.has_children ? 'folder-toggle' : 'folder-toggle empty';
    var iconHtml = folder.cover
        ? '<div class="folder-icon"><img src="/thumb/' + encodeURIComponent(folder.cover) + '" loading="lazy"></div>'
        : '<div class="folder-icon">' + ICON_FOLDER + '</div>';
    el.innerHTML = '<span class="' + toggleClass + '">&#x25B6;</span>' + iconHtml +
        '<span class="folder-name">' + escHtml(folder.name) + '</span>' +
        (folder.count > 0 ? '<span class="folder-count">' + folder.count + '</span>' : '');
    wrapper.appendChild(el);
    if (folder.has_children) {
        var childDiv = document.createElement('div');
        childDiv.className = 'folder-children';
        wrapper.appendChild(childDiv);
    }
    el.addEventListener('click', async function() {
        document.querySelectorAll('.folder-item').forEach(function(f){f.classList.remove('active')});
        el.classList.add('active');
        if (accordionFolders) closeSiblingFolders(wrapper, folder.path);
        if (searchMode) {
            searchFolderFilter = folder.path;
            currentPage = 1;
            doSearch();
        } else {
            navigateTo(folder.path);
        }
        if (folder.has_children) {
            var toggle = el.querySelector('.folder-toggle');
            var children = wrapper.querySelector(':scope > .folder-children');
            if (children) {
                if (children.classList.contains('open')) {
                    children.classList.remove('open'); toggle.classList.remove('open');
                    expandedFolders.delete(folder.path); saveExpandedFolders();
                } else {
                    await loadChildrenFor(wrapper, folder.path);
                    children.classList.add('open'); toggle.classList.add('open');
                    expandedFolders.add(folder.path); saveExpandedFolders();
                }
            }
        } else if (accordionFolders) {
            saveExpandedFolders();
        }
    });
    // Highlight the folder you're currently browsing.
    if (folder.path === currentFolder) el.classList.add('active');
    // Restore saved expansion: open this folder and load its children (which will
    // themselves auto-expand if they were saved as open).
    if (folder.has_children && shouldRestoreFolderOpen(folder.path)) {
        var savedToggle = el.querySelector('.folder-toggle');
        var savedChildren = wrapper.querySelector(':scope > .folder-children');
        loadChildrenFor(wrapper, folder.path).then(function() {
            if (savedChildren) savedChildren.classList.add('open');
            if (savedToggle) savedToggle.classList.add('open');
        });
    }
    return wrapper;
}

// ═══════════════════════════════════════════════════════
// ── Search with folder filtering
// ═══════════════════════════════════════════════════════

function renderSearchFolders(folders, activeFolder) {
    var tree = document.getElementById('folderTree');
    tree.innerHTML = '';
    document.getElementById('sidebarHeader').innerHTML =
        'Results <span class="search-badge">' + folders.length + ' folders</span>';

    // "All folders" item
    var allEl = document.createElement('div');
    allEl.className = 'folder-item' + (activeFolder === null ? ' active' : '');
    allEl.style.paddingLeft = '8px';
    allEl.innerHTML = '<span class="folder-toggle empty">&#x25B6;</span><div class="folder-icon">' + ICON_SEARCH + '</div>' +
        '<span class="folder-name">All folders</span>';
    allEl.addEventListener('click', function() {
        document.querySelectorAll('.folder-item').forEach(function(f){f.classList.remove('active')});
        allEl.classList.add('active');
        searchFolderFilter = null;
        currentPage = 1;
        doSearch();
    });
    tree.appendChild(allEl);

    for (var i = 0; i < folders.length; i++) {
        var f = folders[i];
        var el = document.createElement('div');
        el.className = 'folder-item' + (activeFolder === f.path ? ' active' : '');
        el.style.paddingLeft = '26px';
        el.dataset.path = f.path;
        el.innerHTML = '<div class="folder-icon">' + ICON_FOLDER + '</div>' +
            '<span class="folder-name">' + escHtml(f.name || f.path || 'Root') + '</span>' +
            '<span class="folder-count search-count">' + f.count + '</span>';
        el.addEventListener('click', (function(fp) {
            return function() {
                document.querySelectorAll('.folder-item').forEach(function(x){x.classList.remove('active')});
                this.classList.add('active');
                searchFolderFilter = fp;
                currentPage = 1;
                doSearch();
            };
        })(f.path));
        tree.appendChild(el);
    }
}

var searchTimeout;
document.getElementById('searchInput').addEventListener('input', function(e) {
    clearTimeout(searchTimeout);
    var q = e.target.value.trim();
    searchTimeout = setTimeout(function() {
        if (q.length < 2) { exitSearchMode(); loadGallery(currentFolder); return; }
        searchQuery = q;
        searchFolderFilter = null;
        currentPage = 1;
        searchMode = true;
        document.getElementById('searchClear').classList.add('visible');
        doSearch();
        scheduleGalleryStateSave();
    }, 300);
});

document.getElementById('searchClear').addEventListener('click', function() {
    document.getElementById('searchInput').value = '';
    exitSearchMode();
    loadFolderTree();
    loadGallery(currentFolder);
});

function exitSearchMode() {
    searchMode = false;
    searchQuery = '';
    searchFolderFilter = null;
    currentPage = 1;
    document.getElementById('searchClear').classList.remove('visible');
    scheduleGalleryStateSave();
}

async function doSearch() {
    var loading = document.getElementById('loadingBar');
    loading.classList.add('active');
    showGalleryPlaceholder('Searching...', 'Large metadata searches can take a moment while the gallery index is being updated.');

    var sort = document.getElementById('sortSelect').value;
    var parts = sort.split('-');
    var sortParam = '&sort=' + parts[0] + '&order=' + parts[1];

    var url;
    if (searchFolderFilter !== null) {
        url = '/api/search_folder?q=' + encodeURIComponent(searchQuery) +
              '&folder=' + encodeURIComponent(searchFolderFilter) +
              '&page=' + currentPage + sortParam;
    } else {
        url = '/api/search?q=' + encodeURIComponent(searchQuery) + '&page=' + currentPage + sortParam;
    }

    var data = await API.get(url);
    loading.classList.remove('active');
    if (!data) return;

    // Update folder sidebar (only on global search, not folder-filtered)
    if (searchFolderFilter === null && data.folders) {
        renderSearchFolders(data.folders, null);
    } else if (searchFolderFilter !== null && !data.folders) {
        // Keep current sidebar but update active state
    }

    currentFiles = data.files || [];
    totalPages = data.pages || 1;
    currentPage = data.page || 1;

    renderGalleryFiles(currentFiles, data.total || 0, true);
    renderPagination();
    updateBreadcrumb(searchFolderFilter !== null ? searchFolderFilter : '');
    scheduleGalleryStateSave();
}

// ═══════════════════════════════════════════════════════
// ── Gallery
// ═══════════════════════════════════════════════════════

async function navigateTo(folder) {
    currentFolder = folder;
    currentPage = 1;
    metaCache = {};
    clearSpecialView();
    updateBreadcrumb(folder);
    try { localStorage.setItem('galleryFolder', folder || ''); } catch (e) {}
    await loadGallery(folder);
    scheduleGalleryStateSave();
}

function updateBreadcrumb(folder) {
    var bc = document.getElementById('breadcrumb');
    var parts = folder ? folder.split(/[\\/]/) : [];
    var html = '<span data-path="">Root</span>';
    var path = '';
    for (var i = 0; i < parts.length; i++) {
        path += (path ? '/' : '') + parts[i];
        html += '<span class="sep">&#x203A;</span><span data-path="' + escAttr(path) + '">' + escHtml(parts[i]) + '</span>';
    }
    if (searchMode) {
        html += '<span class="sep">&#x203A;</span><span style="color:var(--accent);cursor:default">&#x1F50D; "' + escHtml(searchQuery) + '"</span>';
    }
    bc.innerHTML = html;
    bc.querySelectorAll('span[data-path]').forEach(function(s) {
        s.addEventListener('click', function() {
            if (searchMode) return; // don't navigate away during search
            navigateTo(s.dataset.path);
        });
    });
}

async function loadGallery(folder) {
    var sort = document.getElementById('sortSelect').value;
    var parts = sort.split('-');
    var loading = document.getElementById('loadingBar');
    loading.classList.add('active');
    showGalleryPlaceholder('Loading gallery...', 'If the index is running, new images will appear here as they are processed.');

    var data = await API.get('/api/files?folder=' + encodeURIComponent(folder) +
        '&sort=' + parts[0] + '&order=' + parts[1] + '&page=' + currentPage);
    loading.classList.remove('active');
    if (!data) return;

    currentFiles = data.files || [];
    totalPages = data.pages || 1;
    currentPage = data.page || 1;

    renderGalleryFiles(currentFiles, data.total || 0, false);
    renderPagination();
    scheduleGalleryStateSave();
}

function showGalleryPlaceholder(title, detail) {
    var grid = document.getElementById('galleryGrid');
    var empty = document.getElementById('galleryEmpty');
    var titleEl = document.getElementById('galleryEmptyTitle');
    var detailEl = document.getElementById('galleryEmptyDetail');
    if (grid) grid.style.display = 'none';
    if (titleEl) titleEl.textContent = title || 'Loading gallery...';
    if (detailEl) detailEl.textContent = detail || '';
    if (empty) empty.style.display = 'flex';
}

function renderGalleryFiles(files, total, isSearch) {
    var grid = document.getElementById('galleryGrid');
    var empty = document.getElementById('galleryEmpty');
    var toolbar = document.getElementById('galleryToolbar');
    var countEl = document.getElementById('galleryCount');

    if (files.length === 0) {
        showGalleryPlaceholder(
            isSearch ? 'No results found' : 'No images in this folder',
            isSearch
                ? 'Try another search term or wait until the background index has finished.'
                : 'This folder may be empty, or the background index may still be processing new images.'
        );
        toolbar.style.display = 'none';
        document.getElementById('pagination').style.display = 'none';
        return;
    }

    grid.style.display = 'grid';
    empty.style.display = 'none';
    toolbar.style.display = 'flex';
    var label = isSearch ? ' results' : ' images';
    var metaCount = files.filter(function(f){ return f.has_metadata; }).length;
    var pageDetail = files.length < total ? ' · showing ' + files.length : '';
    var metaDetail = metaCount ? ' · ' + metaCount + ' with metadata on page' : '';
    countEl.textContent = total + label + pageDetail + metaDetail;
    updateToolbarNav();
    grid.innerHTML = '';

    var observer = new IntersectionObserver(function(entries) {
        entries.forEach(function(entry) {
            if (entry.isIntersecting) {
                var img = entry.target.querySelector('img');
                if (img && img.dataset.src) { img.src = img.dataset.src; img.removeAttribute('data-src'); }
                observer.unobserve(entry.target);
            }
        });
    }, { rootMargin: '300px' });

    for (var i = 0; i < files.length; i++) {
        var file = files[i];
        var card = document.createElement('div');
        card.className = 'thumb-card';
        card.dataset.path = file.path;
        card.dataset.index = i;
        var dimsText = (file.width && file.height) ? file.width + '\u00D7' + file.height : '';
        var favClass = file.favorite ? 'thumb-fav active' : 'thumb-fav';
        var favStar = file.favorite ? '\u2605' : '\u2606';
        card.innerHTML =
            '<img data-src="/thumb/' + encodeURIComponent(file.path) + '" alt="' + escAttr(file.name) + '" loading="lazy">' +
            '<span class="' + favClass + '" data-path="' + escAttr(file.path) + '">' + favStar + '</span>' +
            (file.has_metadata ? '<div class="thumb-meta-badge" title="Metadata found"></div>' : '') +
            (dimsText ? '<div class="thumb-dims">' + dimsText + '</div>' : '') +
            '<div class="thumb-overlay"><div class="thumb-name">' + escHtml(file.name) + '</div></div>';
        // Star click handler (stop propagation so card click doesn't fire)
        card.querySelector('.thumb-fav').addEventListener('click', (function(fp) {
            return function(e) {
                e.stopPropagation();
                e.preventDefault();
                toggleFavorite(fp);
            };
        })(file.path));
        card.addEventListener('click', (function(fp,c,idx){return function(e){
            if (e.ctrlKey || e.metaKey) {
                // Toggle multi-select
                e.preventDefault();
                toggleMultiSelect(fp, c);
            } else if (e.shiftKey && selectedFile) {
                // Range select
                e.preventDefault();
                rangeSelect(idx);
            } else {
                // Normal click: clear multi-select if any, then select for metadata
                if (multiSelected.size > 0) { clearMultiSelect(); }
                selectImage(fp, c);
            }
        }})(file.path, card, i));
        card.addEventListener('dblclick', (function(idx,fp){return function(){lightboxIndex=idx; openLightbox('/image/'+encodeURIComponent(fp))}})(i, file.path));
        // Restore multi-select state if re-rendering same page
        if (multiSelected.has(file.path)) { card.classList.add('multi-selected'); }
        grid.appendChild(card);
        observer.observe(card);
    }
    // Prefetch next page thumbnails
    restoreGalleryScrollIfNeeded();
    scheduleGalleryStateSave();
    if (currentPage < totalPages) {
        prefetchNextPage();
    }
}

function prefetchNextPage() {
    // Fetch next page file list silently, then preload their thumb URLs
    var nextPage = currentPage + 1;
    var url;
    if (searchMode) {
        var sort = document.getElementById('sortSelect').value;
        var parts = sort.split('-');
        var sortParam = '&sort=' + parts[0] + '&order=' + parts[1];
        if (searchFolderFilter !== null) {
            url = '/api/search_folder?q=' + encodeURIComponent(searchQuery) +
                  '&folder=' + encodeURIComponent(searchFolderFilter) +
                  '&page=' + nextPage + sortParam;
        } else {
            url = '/api/search?q=' + encodeURIComponent(searchQuery) + '&page=' + nextPage + sortParam;
        }
    } else {
        var sort = document.getElementById('sortSelect').value;
        var parts = sort.split('-');
        url = '/api/files?folder=' + encodeURIComponent(currentFolder) +
              '&sort=' + parts[0] + '&order=' + parts[1] + '&page=' + nextPage;
    }
    // Fire and forget — preload thumbnails into browser cache
    fetch(url).then(function(r) { return r.json(); }).then(function(data) {
        if (!data || !data.files) return;
        var files = data.files.slice(0, 30); // prefetch first 30 thumbs
        files.forEach(function(f) {
            var link = document.createElement('link');
            link.rel = 'prefetch';
            link.href = '/thumb/' + encodeURIComponent(f.path);
            link.as = 'image';
            document.head.appendChild(link);
        });
    }).catch(function() {}); // silent fail
}

// ═══════════════════════════════════════════════════════
// ── Pagination
// ═══════════════════════════════════════════════════════

function updateToolbarNav() {
    var pageEl = document.getElementById('gtPage');
    var pageInput = document.getElementById('gtPageInput');
    var prevBtn = document.getElementById('gtPrev');
    var nextBtn = document.getElementById('gtNext');
    pageInput.style.display = 'none';
    pageEl.style.display = '';
    if (totalPages <= 1) {
        pageEl.textContent = '';
        prevBtn.style.display = 'none';
        nextBtn.style.display = 'none';
    } else {
        pageEl.textContent = currentPage + ' / ' + totalPages;
        prevBtn.style.display = '';
        nextBtn.style.display = '';
        prevBtn.disabled = currentPage <= 1;
        nextBtn.disabled = currentPage >= totalPages;
    }
}

document.getElementById('gtPrev').addEventListener('click', function() { if (currentPage > 1) goToPage(currentPage - 1); });
document.getElementById('gtNext').addEventListener('click', function() { if (currentPage < totalPages) goToPage(currentPage + 1); });

// Click page indicator to jump
document.getElementById('gtPage').addEventListener('click', function() {
    if (totalPages <= 1) return;
    var pageEl = document.getElementById('gtPage');
    var pageInput = document.getElementById('gtPageInput');
    pageEl.style.display = 'none';
    pageInput.style.display = '';
    pageInput.max = totalPages;
    pageInput.value = currentPage;
    pageInput.focus();
    pageInput.select();
});

document.getElementById('gtPageInput').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
        var val = parseInt(this.value);
        if (val >= 1 && val <= totalPages && val !== currentPage) {
            goToPage(val);
        } else {
            updateToolbarNav(); // reset display
        }
    } else if (e.key === 'Escape') {
        updateToolbarNav();
    }
});

document.getElementById('gtPageInput').addEventListener('blur', function() {
    updateToolbarNav();
});
function renderPagination() {
    var container = document.getElementById('pagination');
    if (totalPages <= 1) { container.style.display = 'none'; return; }
    container.style.display = 'flex';
    container.innerHTML = '';

    function addBtn(label, page, disabled, active) {
        var b = document.createElement('button');
        b.textContent = label;
        b.disabled = disabled;
        if (active) b.className = 'active';
        if (!disabled) b.addEventListener('click', function() { goToPage(page); });
        container.appendChild(b);
    }

    addBtn('\u00AB', 1, currentPage === 1, false);
    addBtn('\u2039', currentPage - 1, currentPage === 1, false);

    // Page numbers: show window around current page
    var start = Math.max(1, currentPage - 3);
    var end = Math.min(totalPages, currentPage + 3);
    if (start > 1) {
        addBtn('1', 1, false, currentPage === 1);
        if (start > 2) { var s = document.createElement('span'); s.className = 'page-info'; s.textContent = '...'; container.appendChild(s); }
    }
    for (var p = start; p <= end; p++) {
        addBtn('' + p, p, false, p === currentPage);
    }
    if (end < totalPages) {
        if (end < totalPages - 1) { var s = document.createElement('span'); s.className = 'page-info'; s.textContent = '...'; container.appendChild(s); }
        addBtn('' + totalPages, totalPages, false, currentPage === totalPages);
    }

    addBtn('\u203A', currentPage + 1, currentPage === totalPages, false);
    addBtn('\u00BB', totalPages, currentPage === totalPages, false);
}

function goToPage(page) {
    currentPage = page;
    document.getElementById('galleryArea').scrollTop = 0;
    if (searchMode) {
        doSearch();
    } else if (specialView === 'favorites') {
        loadFavorites(document.querySelector('#navFavorites'), false);
    } else if (specialView === 'today' || specialView === '7days' || specialView === '30days') {
        loadTimeline(specialView, document.querySelector('.sidebar-special.active'), false);
    } else if (specialView && specialView.startsWith('collection:')) {
        var colId = parseInt(specialView.split(':')[1]);
        var col = collectionsCache.find(function(c) { return c.id === colId; });
        if (col) viewCollection(col, false);
    } else {
        loadGallery(currentFolder);
    }
    scheduleGalleryStateSave();
}

// ═══════════════════════════════════════════════════════
// ── Image Selection & Metadata
// ═══════════════════════════════════════════════════════

async function selectImage(path, cardEl) {
    selectedFile = path;
    try { localStorage.setItem('gallerySelectedImage', path || ''); } catch (e) {}
    scheduleGalleryStateSave();
    setMetaPanelCollapsed(false);
    document.querySelectorAll('.thumb-card').forEach(function(c){c.classList.remove('selected')});
    if (cardEl) cardEl.classList.add('selected');
    var panel = document.getElementById('metaPanel');
    // Check cache first
    var meta = metaCache[path];
    if (!meta) {
        meta = await API.get('/api/metadata?path=' + encodeURIComponent(path));
        if (meta && (meta.info || meta.parsed)) { metaCache[path] = meta; }
    }
    if (!meta || (!meta.info && !meta.parsed)) { panel.innerHTML = '<div class="meta-empty">No metadata found</div>'; return; }
    renderMetaPanel(meta, path);
}

function renderMetaPanel(meta, path) {
    window._libMeta = meta; window._libPath = path;
    var panel = document.getElementById('metaPanel');
    var parsed = meta.parsed || {}, rawMeta = meta.raw_meta || {}, info = meta.info || {};
    // Find favorite state from currentFiles
    var isFav = false;
    for (var fi = 0; fi < currentFiles.length; fi++) {
        if (currentFiles[fi].path === path) { isFav = currentFiles[fi].favorite; break; }
    }
    var favStar = isFav ? '\u2605' : '\u2606';
    var fileName = info.name || path.split('/').pop().split('\\').pop() || 'Selected image';
    var pinLabel = ICON_PIN;
    var html =
        '<div class="meta-panel-head">' +
            '<div class="meta-panel-title" title="' + escAttr(fileName) + '">' + escHtml(fileName) + '</div>' +
            '<div class="meta-panel-controls">' +
                '<button class="meta-mini-btn meta-delete-btn" id="metaDeleteBtn" title="Move this image to trash (Delete)">&#x1F5D1;</button>' +
                '<button class="meta-mini-btn ' + (metaPanelPinned ? 'active' : '') + '" id="metaPinBtn" title="' + (metaPanelPinned ? 'Unpin panel' : 'Pin panel') + '">' + pinLabel + '</button>' +
                '<button class="meta-mini-btn" id="metaCollapseBtn" title="Hide metadata panel">&#x203A;</button>' +
            '</div>' +
        '</div>' +
        '<div class="meta-preview" id="metaPreview"><img src="/image/' + encodeURIComponent(path) + '" alt=""></div>' +
        '<div class="meta-tabs">' +
        '<button class="meta-tab ' + (currentMetaTab==='metadata'?'active':'') + '" data-tab="metadata">Metadata</button>' +
        '<button class="meta-tab ' + (currentMetaTab==='raw'?'active':'') + '" data-tab="raw">Raw Metadata</button>' +
        '<span class="meta-fav-btn" id="metaFavBtn" title="Toggle favorite (F)">' + favStar + '</span>' +
        '</div>' +
        '<div class="meta-content" id="metaContent">';
    html += (currentMetaTab === 'metadata') ? renderParsedMeta(parsed, info, rawMeta, meta.civitai) : renderRawMeta(rawMeta, info);
    html += '</div>';
    panel.innerHTML = html;
    document.getElementById('metaPinBtn').addEventListener('click', function() {
        setMetaPanelPinned(!metaPanelPinned);
        renderMetaPanel(meta, path);
    });
    document.getElementById('metaDeleteBtn').addEventListener('click', function(e) {
        e.stopPropagation();
        confirmDelete([path]);
    });
    document.getElementById('metaCollapseBtn').addEventListener('click', function() {
        setMetaPanelCollapsed(true);
    });
    panel.querySelectorAll('.meta-tab').forEach(function(tab) {
        tab.addEventListener('click', function() { currentMetaTab = tab.dataset.tab; renderMetaPanel(meta, path); });
    });
    document.getElementById('metaFavBtn').addEventListener('click', function() { toggleFavorite(path); });
    document.getElementById('metaPreview').addEventListener('click', function() {
        var idx = currentFiles.findIndex(function(f){return f.path===path});
        if (idx >= 0) lightboxIndex = idx;
        openLightbox('/image/' + encodeURIComponent(path));
    });
    panel.querySelectorAll('.copy-btn').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            var text = btn.dataset.copy;
            var orig = btn.textContent;
            function onSuccess() { btn.textContent = '\u2713 Copied'; setTimeout(function(){btn.textContent=orig}, 1500); }
            function onFail() { btn.textContent = '\u2717 Failed'; setTimeout(function(){btn.textContent=orig}, 1500); }
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(onSuccess).catch(function() {
                    // Fallback for non-secure contexts
                    copyFallback(text) ? onSuccess() : onFail();
                });
            } else {
                copyFallback(text) ? onSuccess() : onFail();
            }
        });
    });
    // Load collection badges for this file
    API.get('/api/file/collections?path=' + encodeURIComponent(path)).then(function(colls) {
        if (!colls || !colls.length) return;
        var content = document.getElementById('metaContent');
        if (!content) return;
        var bhtml = '<div class="meta-section" id="metaCollSection"><div class="meta-section-title">Collections</div><div class="meta-collections">';
        for (var ci = 0; ci < colls.length; ci++) {
            bhtml += '<span class="meta-coll-badge" data-coll-id="' + colls[ci].id + '">' +
                '<span class="coll-dot" style="background:' + escAttr(colls[ci].color) + '"></span>' +
                escHtml(colls[ci].name) +
                '<span class="coll-remove" title="Remove from ' + escAttr(colls[ci].name) + '">\u00D7</span></span>';
        }
        bhtml += '</div></div>';
        content.insertAdjacentHTML('afterbegin', bhtml);
        // Bind remove buttons
        content.querySelectorAll('.coll-remove').forEach(function(btn) {
            btn.addEventListener('click', function(e) {
                e.stopPropagation();
                var badge = btn.closest('.meta-coll-badge');
                var collId = parseInt(badge.dataset.collId);
                var collName = badge.textContent.replace('\u00D7','').trim();
                API.post('/api/collection/remove', { id: collId, paths: [path] }).then(function(r) {
                    if (!r) return;
                    badge.remove();
                    // Remove section if no badges left
                    var section = document.getElementById('metaCollSection');
                    if (section && section.querySelectorAll('.meta-coll-badge').length === 0) {
                        section.remove();
                    }
                    showToast('Removed from "' + collName + '"');
                    loadCollections();
                });
            });
        });
    });
}

function copyPlainText(text, btn) {
    var orig = btn ? btn.textContent : '';
    function ok() {
        if (!btn) return;
        btn.textContent = '\u2713 Copied';
        setTimeout(function(){ btn.textContent = orig; }, 1400);
    }
    function fail() {
        if (!btn) return;
        btn.textContent = '\u2717 Failed';
        setTimeout(function(){ btn.textContent = orig; }, 1400);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(ok).catch(function(){ copyFallback(text) ? ok() : fail(); });
    } else {
        copyFallback(text) ? ok() : fail();
    }
}

function renderParsedMeta(parsed, info, rawMeta, civitai) {
    var h = '';
    // Prompt first
    if (parsed.prompt) h += '<div class="meta-section"><div class="meta-section-title">Prompt <span class="copy-btn" data-copy="'+escAttr(parsed.prompt)+'">Copy</span></div><div class="meta-prompt">'+escHtml(parsed.prompt)+'</div></div>';
    if (parsed.negative_prompt) h += '<div class="meta-section"><div class="meta-section-title">Negative Prompt <span class="copy-btn" data-copy="'+escAttr(parsed.negative_prompt)+'">Copy</span></div><div class="meta-prompt negative">'+escHtml(parsed.negative_prompt)+'</div></div>';
    if (parsed.prompt) {
        h += '<div class="meta-section" style="padding:6px 0">' +
             '<div style="display:flex; align-items:center; gap:6px; margin-bottom:6px; font-size:11px; color:var(--text-dim,#888)">' +
                 '<span>Target:</span>' +
                 '<select id="saveTargetSelect" style="flex:1; background:var(--bg-card,#1a1a1a); border:1px solid var(--border,#333); border-radius:4px; color:var(--text,#eee); font-size:11px; padding:3px 4px;">' +
                     '<option value="general">General</option>' +
                     '<option value="sdxl">SDXL</option>' +
                     '<option value="zit">Z-Image</option>' +
                     '<option value="flux">Flux</option>' +
                     '<option value="illustrious">Illustrious</option>' +
                     '<option value="pony">Pony</option>' +
                     '<option value="llm">LLM</option>' +
                     '<option value="suno">Suno</option>' +
                 '</select>' +
             '</div>' +
             '<button onclick="saveToLibrary()" style="width:100%;background:var(--accent,#4a9eff);color:#fff;border:none;border-radius:6px;padding:7px 0;cursor:pointer;font-size:13px;margin-bottom:8px">&#x1F4DA; Save to Prompt Library</button>' +
             '<div class="meta-forge-wrapper" style="display:' + (parsed.prompt && window._forgeEnabled ? 'block' : 'none') + '">' +
                 '<button class="meta-forge-btn" onclick="forgeGenerate()" type="button">&#x1F9EA; Generate with Forge</button>' +
                 '<div class="meta-forge-status" id="metaForgeStatus"></div>' +
             '</div></div>';
    }
    // File info
    h += '<div class="meta-section"><div class="meta-section-title">File Info</div><div class="meta-file-info">' +
        '<span class="label">Name</span><span class="value">' + escHtml(info.name||'') + '</span>' +
        '<span class="label">Size</span><span class="value">' + formatSize(info.size) + '</span>' +
        '<span class="label">Dimensions</span><span class="value">' + (info.width||'?') + ' \u00D7 ' + (info.height||'?') + '</span>' +
        '<span class="label">Format</span><span class="value">' + escHtml((info.ext||'').toUpperCase().replace('.','')) + '</span>' +
        '</div></div>';
    // Civitai model info
    if (civitai) {
        h += '<div class="meta-section"><div class="meta-section-title">Model (Civitai)</div><div class="meta-civitai">';
        h += '<div class="meta-civitai-name"><a href="https://civitai.com/models/' + civitai.id + '" target="_blank" rel="noopener">' + escHtml(civitai.model) + '</a></div>';
        var detail = escHtml(civitai.version);
        if (civitai.type) detail += ' \u00B7 ' + escHtml(civitai.type);
        if (civitai.base) detail += ' \u00B7 ' + escHtml(civitai.base);
        if (civitai.creator) detail += ' \u00B7 by ' + escHtml(civitai.creator);
        h += '<div class="meta-civitai-detail">' + detail + '</div>';
        if (civitai.tags && civitai.tags.length > 0) {
            h += '<div class="meta-civitai-words">';
            for (var tg = 0; tg < civitai.tags.length; tg++) {
                h += '<span>' + escHtml(civitai.tags[tg]) + '</span>';
            }
            h += '</div>';
        }
        if (civitai.trained_words && civitai.trained_words.length > 0) {
            h += '<div class="meta-civitai-detail" style="margin-top:4px">Trigger words:</div>';
            h += '<div class="meta-civitai-words">';
            for (var tw = 0; tw < civitai.trained_words.length; tw++) {
                h += '<span>' + escHtml(civitai.trained_words[tw]) + '</span>';
            }
            h += '</div>';
        }
        h += '</div></div>';
    }
    // Settings
    if (parsed.settings && Object.keys(parsed.settings).length > 0) {
        h += '<div class="meta-section"><div class="meta-section-title">Settings</div><div class="meta-settings-grid">';
        var settingsKeys = Object.keys(parsed.settings);
        [
            ['Model', 'Model hash'],
            ['VAE', 'VAE hash']
        ].forEach(function(pair) {
            var first = settingsKeys.indexOf(pair[0]);
            var second = settingsKeys.indexOf(pair[1]);
            if (first >= 0 && second >= 0 && second < first) {
                settingsKeys.splice(second, 1);
                first = settingsKeys.indexOf(pair[0]);
                settingsKeys.splice(first + 1, 0, pair[1]);
            }
        });
        settingsKeys.forEach(function(k) {
            h += '<span class="meta-key">'+escHtml(k)+'</span><span class="meta-val">'+escHtml(parsed.settings[k])+'</span>';
        });
        h += '</div></div>';
    }
    var skip = {parameters:1,prompt:1,'Negative prompt':1};
    for (var rk in rawMeta) { if (skip[rk]) continue; var rv = rawMeta[rk];
        if (typeof rv === 'string' && rv.length > 0) { var d = rv.length > 500 ? rv.slice(0,500)+'\u2026' : rv;
            h += '<div class="meta-section"><div class="meta-section-title">'+escHtml(rk)+' <span class="copy-btn" data-copy="'+escAttr(rv)+'">Copy</span></div><div class="meta-prompt">'+escHtml(d)+'</div></div>'; }}
    if (!parsed.prompt && !parsed.settings && Object.keys(rawMeta).length===0)
        h += '<div style="color:var(--text-dim);text-align:center;padding:20px">No generation metadata found</div>';
    return h;
}

function renderRawMeta(rawMeta, info) {
    var h = '<div class="meta-section"><div class="meta-section-title">File Info</div><div class="meta-file-info">' +
        '<span class="label">Name</span><span class="value">'+escHtml(info.name||'')+'</span>' +
        '<span class="label">Size</span><span class="value">'+formatSize(info.size)+'</span>' +
        '<span class="label">Dimensions</span><span class="value">'+(info.width||'?')+' \u00D7 '+(info.height||'?')+'</span></div></div>';
    if (Object.keys(rawMeta).length === 0) { h += '<div style="color:var(--text-dim);text-align:center;padding:20px">No raw metadata</div>'; }
    else {
        var rt = rawMeta.parameters || '';
        if (!rt) { for (var k in rawMeta) { var v = rawMeta[k]; rt += k+': '+(typeof v==='string'&&v.length>1000?v.slice(0,1000)+'\u2026':v)+'\n\n'; }}
        h += '<div class="meta-section"><div class="meta-section-title">Raw Parameters <span class="copy-btn" data-copy="'+escAttr(rt)+'">Copy</span></div><div class="meta-raw">'+escHtml(rt)+'</div></div>';
    }
    return h;
}

// ═══════════════════════════════════════════════════════
// ── Lightbox
// ═══════════════════════════════════════════════════════

function openLightbox(src) { document.getElementById('lightboxImg').src=src; document.getElementById('lightbox').classList.add('open'); updateLightboxCounter(); }
function closeLightbox() { document.getElementById('lightbox').classList.remove('open'); document.getElementById('lightboxImg').src=''; }
function lightboxNav(delta) {
    if (!currentFiles.length) return;
    lightboxIndex = (lightboxIndex + delta + currentFiles.length) % currentFiles.length;
    var f = currentFiles[lightboxIndex];
    document.getElementById('lightboxImg').src = '/image/' + encodeURIComponent(f.path);
    updateLightboxCounter();
    selectImage(f.path, document.querySelector('.thumb-card[data-index="'+lightboxIndex+'"]'));
}
function updateLightboxCounter() { document.getElementById('lbCounter').textContent = currentFiles.length > 0 ? (lightboxIndex+1)+' / '+currentFiles.length : ''; }
document.getElementById('lbClose').addEventListener('click', function(e){e.stopPropagation();closeLightbox()});
document.getElementById('lbPrev').addEventListener('click', function(e){e.stopPropagation();lightboxNav(-1)});
document.getElementById('lbNext').addEventListener('click', function(e){e.stopPropagation();lightboxNav(1)});
document.getElementById('lbDelete').addEventListener('click', function(e){
    e.stopPropagation();
    if (!currentFiles.length) return;
    var f = currentFiles[lightboxIndex];
    if (f && f.path) confirmDelete([f.path]);
});
document.getElementById('lightbox').addEventListener('click', function(e){ if(e.target===e.currentTarget||e.target.id==='lightboxImg') closeLightbox(); });

// ── Sort ──
document.getElementById('sortSelect').addEventListener('change', function() {
    currentPage = 1;
    if (searchMode) { doSearch(); }
    else if (specialView === 'favorites') { loadFavorites(document.querySelector('#navFavorites'), true); }
    else if (specialView && specialView.startsWith('collection:')) {
        var colId = parseInt(specialView.split(':')[1]);
        var col = collectionsCache.find(function(c) { return c.id === colId; });
        if (col) viewCollection(col, true);
    }
    else if (specialView) { loadTimeline(specialView, document.querySelector('.sidebar-special.active'), true); }
    else { loadGallery(currentFolder); }
    scheduleGalleryStateSave();
});
document.getElementById('thumbSlider').addEventListener('input', function(e) { document.documentElement.style.setProperty('--thumb-size', e.target.value+'px'); });
document.getElementById('galleryArea').addEventListener('scroll', scheduleGalleryStateSave);
document.getElementById('metaToggleBtn').addEventListener('click', function() {
    setMetaPanelCollapsed(!metaPanelCollapsed);
});

// ── Refresh ──
document.getElementById('refreshBtn').addEventListener('click', async function() {
    var btn = document.getElementById('refreshBtn');
    btn.style.opacity = '0.5'; btn.style.pointerEvents = 'none';
    showToast('Starting re-index...');
    var result = await API.post('/api/reindex', {});
    if (searchMode) { doSearch(); }
    else if (specialView) { goToPage(currentPage); }
    else { await loadFolderTree(); await loadGallery(currentFolder); }
    await updateStatus(); await updateFavCount();
    btn.style.opacity = '1'; btn.style.pointerEvents = '';
    showToast(result && result.started ? 'Indexing in background' : 'Index already running');
});

// ── Keyboard ──
document.addEventListener('keydown', function(e) {
    if (e.target.tagName === 'INPUT') {
        if (e.key === 'Escape') { document.getElementById('searchInput').blur(); }
        return;
    }
    // Confirm dialog open? handle Escape/Enter and left/right button choice.
    if (document.getElementById('confirmOverlay').classList.contains('open')) {
        var cancelBtn = document.getElementById('confirmCancel');
        var okBtn = document.getElementById('confirmOk');
        if (e.key === 'Escape') { e.preventDefault(); closeConfirm(); }
        else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
            e.preventDefault();
            if (okBtn.style.display === 'none') cancelBtn.focus();
            else if (document.activeElement === okBtn) cancelBtn.focus();
            else okBtn.focus();
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (document.activeElement === okBtn && okBtn.style.display !== 'none') okBtn.click();
            else cancelBtn.click();
        }
        return;
    }
    var lbOpen = document.getElementById('lightbox').classList.contains('open');
    if (e.key === 'Escape') {
        if (lbOpen) { closeLightbox(); return; }
        if (multiSelected.size > 0) { clearMultiSelect(); return; }
        return;
    }
    if (lbOpen) {
        if (e.key === 'ArrowLeft') { e.preventDefault(); lightboxNav(-1); }
        if (e.key === 'ArrowRight') { e.preventDefault(); lightboxNav(1); }
        if (e.key === 'Delete') {
            e.preventDefault();
            if (currentFiles.length) {
                var f = currentFiles[lightboxIndex];
                if (f && f.path) confirmDelete([f.path]);
            }
        }
        return;
    }
    // Delete key
    if (e.key === 'Delete') {
        e.preventDefault();
        if (multiSelected.size > 0) { confirmDelete(Array.from(multiSelected)); }
        else if (selectedFile) { confirmDelete([selectedFile]); }
        return;
    }
    // F key: toggle favorite on selected image
    if (e.key === 'f' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        if (selectedFile) { toggleFavorite(selectedFile); }
        return;
    }
    // Ctrl+A: select all on current page
    if (e.key === 'a' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        selectAllOnPage();
        return;
    }
    // Page nav shortcuts
    if (e.altKey && e.key === 'PageDown') { e.preventDefault(); if (currentPage < totalPages) goToPage(currentPage+1); return; }
    if (e.altKey && e.key === 'PageUp') { e.preventDefault(); if (currentPage > 1) goToPage(currentPage-1); return; }
    if (e.altKey && e.key === 'Home') { e.preventDefault(); goToPage(1); return; }
    if (e.altKey && e.key === 'End') { e.preventDefault(); goToPage(totalPages); return; }
    if (e.key === 'F6' || (e.key === 'f' && e.ctrlKey)) { e.preventDefault(); document.getElementById('searchInput').focus(); return; }

    var cards = Array.from(document.querySelectorAll('.thumb-card'));
    if (!cards.length) return;
    var idx = cards.findIndex(function(c){return c.classList.contains('selected')});
    if (idx < 0) idx = 0;
    var cols = getGalleryColumnCount(cards);
    var nextIdx = getArrowTargetIndex(cards, idx, cols, e.key);
    if (nextIdx !== idx || ['ArrowRight','ArrowLeft','ArrowDown','ArrowUp'].indexOf(e.key) >= 0) {
        e.preventDefault();
        if (e.shiftKey) extendSelectionWithKeyboard(cards, idx, nextIdx);
        else cards[nextIdx].click();
        cards[nextIdx].scrollIntoView({block:'nearest'});
    } else if (e.key === 'Enter' && selectedFile) {
        var si = currentFiles.findIndex(function(f){return f.path===selectedFile});
        if (si >= 0) lightboxIndex = si;
        openLightbox('/image/' + encodeURIComponent(selectedFile));
    }
});

function getGalleryColumnCount(cards) {
    if (!cards || cards.length < 2) return 1;
    var firstTop = cards[0].offsetTop;
    var cols = 1;
    for (var i = 1; i < cards.length; i++) {
        if (Math.abs(cards[i].offsetTop - firstTop) > 2) break;
        cols++;
    }
    return Math.max(1, cols);
}

function findThumbCard(path) {
    var cards = Array.from(document.querySelectorAll('.thumb-card'));
    return cards.find(function(c){ return c.dataset.path === path; }) || null;
}

function getArrowTargetIndex(cards, idx, cols, key) {
    if (key === 'ArrowRight') return Math.min(idx + 1, cards.length - 1);
    if (key === 'ArrowLeft') return Math.max(idx - 1, 0);
    if (key === 'ArrowDown') return Math.min(idx + cols, cards.length - 1);
    if (key === 'ArrowUp') return Math.max(idx - cols, 0);
    return idx;
}

function extendSelectionWithKeyboard(cards, fromIdx, toIdx) {
    if (!cards.length) return;
    var from = Math.max(0, Math.min(fromIdx, cards.length - 1));
    var to = Math.max(0, Math.min(toIdx, cards.length - 1));
    var start = Math.min(from, to);
    var end = Math.max(from, to);
    for (var i = start; i <= end; i++) {
        multiSelected.add(cards[i].dataset.path);
        cards[i].classList.add('multi-selected');
    }
    selectImage(cards[to].dataset.path, cards[to]);
    updateSelectionBar();
}

// ── Multi-select & Delete ──

function toggleMultiSelect(path, card) {
    if (multiSelected.has(path)) {
        multiSelected.delete(path);
        card.classList.remove('multi-selected');
    } else {
        multiSelected.add(path);
        card.classList.add('multi-selected');
    }
    updateSelectionBar();
}

function rangeSelect(toIdx) {
    var cards = Array.from(document.querySelectorAll('.thumb-card'));
    var fromIdx = cards.findIndex(function(c){return c.dataset.path === selectedFile});
    if (fromIdx < 0) fromIdx = 0;
    var start = Math.min(fromIdx, toIdx);
    var end = Math.max(fromIdx, toIdx);
    for (var i = start; i <= end; i++) {
        var p = cards[i].dataset.path;
        multiSelected.add(p);
        cards[i].classList.add('multi-selected');
    }
    updateSelectionBar();
}

function selectAllOnPage() {
    var cards = Array.from(document.querySelectorAll('.thumb-card'));
    cards.forEach(function(c) {
        multiSelected.add(c.dataset.path);
        c.classList.add('multi-selected');
    });
    updateSelectionBar();
}

function clearMultiSelect() {
    multiSelected.clear();
    document.querySelectorAll('.thumb-card.multi-selected').forEach(function(c){
        c.classList.remove('multi-selected');
    });
    updateSelectionBar();
}

function updateSelectionBar() {
    var bar = document.getElementById('selectionBar');
    var count = multiSelected.size;
    if (count === 0) { bar.classList.remove('visible'); return; }
    bar.classList.add('visible');
    document.getElementById('selCount').textContent = count + ' selected';
    // Show "Remove from collection" only when viewing a collection
    var remBtn = document.getElementById('selRemColl');
    remBtn.style.display = (specialView && specialView.startsWith('collection:')) ? '' : 'none';
    // Show "Compare" only for 2-4 images
    var cmpBtn = document.getElementById('selCompare');
    cmpBtn.style.display = (count >= 2 && count <= 4) ? '' : 'none';
}

var confirmCallback = null;
function confirmDelete(paths) {
    var overlay = document.getElementById('confirmOverlay');
    var n = paths.length;
    if (!hasTrash) {
        /* Backend will reject the delete — don't lie about what will happen. */
        document.getElementById('confirmTitle').textContent = 'Safe delete unavailable';
        document.getElementById('confirmMsg').textContent = 'Install send2trash first ( pip install send2trash ) and restart the hub. Permanent file deletion is intentionally not supported here.';
        var okBtn = document.getElementById('confirmOk');
        okBtn.style.display = 'none';
        overlay.classList.add('open');
        confirmCallback = null;
        setTimeout(function(){ document.getElementById('confirmCancel').focus(); }, 0);
        return;
    }
    if (skipDeleteConfirmation) {
        executeDelete(paths);
        return;
    }
    document.getElementById('confirmOk').style.display = '';
    document.getElementById('confirmTitle').textContent = 'Delete ' + n + ' image' + (n > 1 ? 's' : '') + '?';
    document.getElementById('confirmMsg').textContent = n === 1
        ? paths[0].split('/').pop() + ' will be moved to your system trash.'
        : n + ' files will be moved to your system trash.';
    overlay.classList.add('open');
    confirmCallback = function() { executeDelete(paths); };
    setTimeout(function(){ document.getElementById('confirmCancel').focus(); }, 0);
}

function closeConfirm() {
    document.getElementById('confirmOverlay').classList.remove('open');
    confirmCallback = null;
}

document.getElementById('confirmCancel').addEventListener('click', closeConfirm);
document.getElementById('confirmOk').addEventListener('click', function() {
    if (confirmCallback) confirmCallback();
    closeConfirm();
});
document.getElementById('confirmOverlay').addEventListener('click', function(e) {
    if (e.target === e.currentTarget) closeConfirm();
});

async function executeDelete(paths) {
    showToast('Deleting ' + paths.length + ' file(s)...');
    var result = await API.post('/api/delete', { paths: paths });
    if (!result) return;
    if (result.deleted > 0) {
        showToast(result.deleted + ' deleted' + (result.trash ? ' (moved to trash)' : '') +
                  (result.failed > 0 ? ', ' + result.failed + ' failed' : ''));
        // Remove deleted cards from DOM
        var deleted = new Set(result.results.filter(function(r){return r.ok}).map(function(r){return r.path}));
        var lbOpen = document.getElementById('lightbox').classList.contains('open');
        var lbFile = lbOpen && currentFiles[lightboxIndex] ? currentFiles[lightboxIndex].path : null;
        var lbDeleted = lbFile && deleted.has(lbFile);
        var oldFiles = currentFiles.slice();
        var deletedIndexes = [];
        for (var di = 0; di < oldFiles.length; di++) {
            if (deleted.has(oldFiles[di].path)) deletedIndexes.push(di);
        }
        var selectedDeleted = selectedFile && deleted.has(selectedFile);
        var fallbackIndex = selectedDeleted
            ? oldFiles.findIndex(function(f){ return f.path === selectedFile; })
            : (deletedIndexes.length ? deletedIndexes[0] : -1);
        document.querySelectorAll('.thumb-card').forEach(function(card) {
            if (deleted.has(card.dataset.path)) {
                card.style.transition = 'opacity .2s, transform .2s';
                card.style.opacity = '0';
                card.style.transform = 'scale(0.8)';
                setTimeout(function() { card.remove(); }, 200);
            }
        });
        // Clear from state
        deleted.forEach(function(p) { multiSelected.delete(p); });
        currentFiles = currentFiles.filter(function(f) { return !deleted.has(f.path); });
        if (lbDeleted) {
            if (!currentFiles.length) {
                closeLightbox();
            } else {
                lightboxIndex = Math.min(lightboxIndex, currentFiles.length - 1);
                document.getElementById('lightboxImg').src = '/image/' + encodeURIComponent(currentFiles[lightboxIndex].path);
                updateLightboxCounter();
            }
        }
        if (selectedDeleted || (!selectedFile && fallbackIndex >= 0)) {
            if (currentFiles.length) {
                var nextFile = currentFiles[Math.min(Math.max(fallbackIndex, 0), currentFiles.length - 1)];
                var nextCard = findThumbCard(nextFile.path);
                selectImage(nextFile.path, nextCard);
                if (nextCard) nextCard.scrollIntoView({block:'nearest'});
            } else {
                selectedFile = null;
                document.getElementById('metaPanel').innerHTML = '<div class="meta-empty">Click an image to view its generation metadata</div>';
            }
        } else if (selectedFile && deleted.has(selectedFile)) {
            selectedFile = null;
            document.getElementById('metaPanel').innerHTML = '<div class="meta-empty">Click an image to view its generation metadata</div>';
        }
        updateSelectionBar();
        await loadFolderTree();
        updateStatus();
        // Update gallery count display
        var countEl = document.getElementById('galleryCount');
        if (countEl && countEl.textContent) {
            var oldTotal = parseInt(countEl.textContent) || 0;
            if (oldTotal > 0) {
                countEl.textContent = countEl.textContent.replace(/^\d+/, String(oldTotal - result.deleted));
            }
        }
    } else {
        showToast('Delete failed: ' + (result.results[0] ? result.results[0].error : 'unknown error'));
    }
}

// Selection bar buttons
document.getElementById('selClear').addEventListener('click', clearMultiSelect);
document.getElementById('selDelete').addEventListener('click', function() {
    if (multiSelected.size > 0) confirmDelete(Array.from(multiSelected));
});
document.getElementById('selRemColl').addEventListener('click', async function() {
    if (multiSelected.size === 0 || !specialView || !specialView.startsWith('collection:')) return;
    var colId = parseInt(specialView.split(':')[1]);
    var col = collectionsCache.find(function(c) { return c.id === colId; });
    var paths = Array.from(multiSelected);
    var r = await API.post('/api/collection/remove', { id: colId, paths: paths });
    if (!r) return;
    showToast(paths.length + ' removed from "' + (col ? col.name : 'collection') + '"');
    clearMultiSelect();
    loadCollections();
    // Refresh collection view
    if (col) viewCollection(col);
});

// ═══════════════════════════════════════════════════════
// ── Favorites
// ═══════════════════════════════════════════════════════

async function toggleFavorite(path) {
    var result = await API.post('/api/favorite', { path: path });
    if (!result || result.error) return;
    // Update in currentFiles
    for (var i = 0; i < currentFiles.length; i++) {
        if (currentFiles[i].path === path) {
            currentFiles[i].favorite = result.favorite;
            break;
        }
    }
    // Update star on card
    var star = document.querySelector('.thumb-fav[data-path="' + CSS.escape(path) + '"]');
    if (star) {
        star.textContent = result.favorite ? '\u2605' : '\u2606';
        if (result.favorite) star.classList.add('active'); else star.classList.remove('active');
    }
    // Update meta panel star if this image is selected
    var metaStar = document.getElementById('metaFavBtn');
    if (metaStar && selectedFile === path) {
        metaStar.textContent = result.favorite ? '\u2605' : '\u2606';
    }
    // Update metadata cache
    if (metaCache[path]) { delete metaCache[path]; }
    // Update favorite count in sidebar
    updateFavCount();
    showToast(result.favorite ? 'Added to favorites' : 'Removed from favorites');
}

async function updateFavCount() {
    var s = await API.get('/api/stats');
    if (!s) return;
    document.getElementById('favCount').textContent = s.favorites || 0;
}

// ═══════════════════════════════════════════════════════
// ── Sidebar special views (Favorites, Timeline)
// ═══════════════════════════════════════════════════════

function clearSpecialView() {
    specialView = null;
    document.querySelectorAll('.sidebar-special').forEach(function(el) { el.classList.remove('active'); });
}

function activateSpecialView(view, el, resetPage) {
    if (resetPage === undefined) resetPage = true;
    // Clear search mode if active
    if (searchMode) {
        document.getElementById('searchInput').value = '';
        exitSearchMode();
        loadFolderTree();
    }
    specialView = view;
    if (resetPage) currentPage = 1;
    metaCache = {};
    document.querySelectorAll('.sidebar-special').forEach(function(s) { s.classList.remove('active'); });
    document.querySelectorAll('.folder-item').forEach(function(f) { f.classList.remove('active'); });
    if (el) el.classList.add('active');
}

document.getElementById('navFavorites').addEventListener('click', function() {
    loadFavorites(this, true);
});
document.getElementById('navToday').addEventListener('click', function() { loadTimeline('today', this); });
document.getElementById('navWeek').addEventListener('click', function() { loadTimeline('7days', this); });
document.getElementById('navMonth').addEventListener('click', function() { loadTimeline('30days', this); });

async function loadFavorites(el, resetPage) {
    if (resetPage === undefined) resetPage = true;
    activateSpecialView('favorites', el, resetPage);
    updateBreadcrumb('');
    var sort = document.getElementById('sortSelect').value;
    var parts = sort.split('-');
    var sortField = parts[0] === 'favorite' ? 'date' : parts[0];
    var sortOrder = parts[0] === 'favorite' ? 'desc' : parts[1];
    var data = await API.get('/api/favorites?sort=' + sortField + '&order=' + sortOrder + '&page=' + currentPage);
    if (!data) return;
    currentFiles = data.files || [];
    totalPages = data.pages || 1;
    currentPage = data.page || 1;
    document.getElementById('galleryCount').textContent = (data.total || 0) + ' favorites';
    renderGalleryFiles(currentFiles, data.total || 0, false);
    renderPagination();
    updateToolbarNav();
    scheduleGalleryStateSave();
}

async function loadTimeline(period, el, resetPage) {
    if (resetPage === undefined) resetPage = true;
    activateSpecialView(period, el, resetPage);
    var labels = { today: 'Today', '7days': 'Last 7 days', '30days': 'Last 30 days' };
    updateBreadcrumb('');
    var sort = document.getElementById('sortSelect').value;
    var parts = sort.split('-');
    var data = await API.get('/api/timeline?period=' + period + '&sort=' + parts[0] + '&order=' + parts[1] + '&page=' + currentPage);
    if (!data) return;
    currentFiles = data.files || [];
    totalPages = data.pages || 1;
    currentPage = data.page || 1;
    document.getElementById('galleryCount').textContent = (data.total || 0) + ' images \u2014 ' + labels[period];
    renderGalleryFiles(currentFiles, data.total || 0, false);
    renderPagination();
    updateToolbarNav();
    scheduleGalleryStateSave();
}

// ═══════════════════════════════════════════════════════
// ── Collections
// ═══════════════════════════════════════════════════════

var collectionsCache = [];

async function loadCollections() {
    var data = await API.get('/api/collections');
    if (!data) return;
    collectionsCache = data;
    renderCollectionsSidebar(data);
}

function renderCollectionsSidebar(collections) {
    var list = document.getElementById('collList');
    list.innerHTML = '';
    for (var i = 0; i < collections.length; i++) {
        var c = collections[i];
        var el = document.createElement('div');
        el.className = 'coll-item';
        el.dataset.id = c.id;
        el.innerHTML = '<span class="coll-dot" style="background:' + escAttr(c.color) + '"></span>' +
            '<span class="coll-name">' + escHtml(c.name) + '</span>' +
            '<span class="coll-cnt">' + c.count + '</span>' +
            '<span class="coll-del" title="Delete collection">\u00D7</span>';
        el.querySelector('.coll-name').addEventListener('click', (function(col) { return function() { viewCollection(col); }; })(c));
        el.querySelector('.coll-cnt').addEventListener('click', (function(col) { return function() { viewCollection(col); }; })(c));
        el.querySelector('.coll-dot').addEventListener('click', (function(col) { return function() { viewCollection(col); }; })(c));
        el.querySelector('.coll-del').addEventListener('click', (function(col) { return function(e) {
            e.stopPropagation();
            if (!confirm('Delete collection "' + col.name + '"?\n\nImages will not be deleted, only the collection label.')) return;
            API.post('/api/collection/delete', { id: col.id }).then(function(r) {
                if (!r) return;
                showToast('Collection "' + col.name + '" deleted');
                // If viewing this collection, go back to root
                if (specialView === 'collection:' + col.id) {
                    clearSpecialView();
                    navigateTo('');
                }
                loadCollections();
            });
        }; })(c));
        list.appendChild(el);
    }
}

async function viewCollection(col, resetPage) {
    if (resetPage === undefined) resetPage = true;
    activateSpecialView('collection:' + col.id, null, resetPage);
    document.querySelectorAll('.coll-item').forEach(function(e) { e.classList.remove('active'); });
    var el = document.querySelector('.coll-item[data-id="' + col.id + '"]');
    if (el) el.classList.add('active');
    var sort = document.getElementById('sortSelect').value;
    var parts = sort.split('-');
    var data = await API.get('/api/collection/files?id=' + col.id + '&sort=' + parts[0] + '&order=' + parts[1] + '&page=' + currentPage);
    if (!data) return;
    currentFiles = data.files || [];
    totalPages = data.pages || 1;
    currentPage = data.page || 1;
    document.getElementById('galleryCount').textContent = (data.total || 0) + ' in "' + col.name + '"';
    renderGalleryFiles(currentFiles, data.total || 0, false);
    renderPagination();
    updateToolbarNav();
    scheduleGalleryStateSave();
}

// Create new collection
document.getElementById('collAddBtn').addEventListener('click', function() {
    var name = prompt('New collection name:');
    if (!name || !name.trim()) return;
    API.post('/api/collection/create', { name: name.trim() }).then(function(r) {
        if (!r) return;
        if (r.error) { showToast(r.error); return; }
        showToast('Collection "' + r.name + '" created');
        loadCollections();
    });
});

// Collection picker in selection bar
document.getElementById('selCollBtn').addEventListener('click', function() {
    var menu = document.getElementById('collPickerMenu');
    if (menu.classList.contains('open')) { menu.classList.remove('open'); return; }
    // Populate picker with current collections
    var list = document.getElementById('collPickerList');
    list.innerHTML = '';
    for (var i = 0; i < collectionsCache.length; i++) {
        var c = collectionsCache[i];
        var item = document.createElement('div');
        item.className = 'coll-picker-item';
        item.innerHTML = '<span class="coll-dot" style="background:' + escAttr(c.color) + '"></span>' + escHtml(c.name);
        item.addEventListener('click', (function(col) { return function() {
            addSelectionToCollection(col.id, col.name);
            document.getElementById('collPickerMenu').classList.remove('open');
        }; })(c));
        list.appendChild(item);
    }
    document.getElementById('collPickerInput').value = '';
    menu.classList.add('open');
    document.getElementById('collPickerInput').focus();
});

// Create collection from picker input
document.getElementById('collPickerInput').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
        var name = this.value.trim();
        if (!name) return;
        API.post('/api/collection/create', { name: name }).then(function(r) {
            if (!r || r.error) { showToast(r ? r.error : 'Error'); return; }
            showToast('Created "' + r.name + '"');
            loadCollections().then(function() {
                addSelectionToCollection(r.id, r.name);
            });
            document.getElementById('collPickerMenu').classList.remove('open');
        });
    } else if (e.key === 'Escape') {
        document.getElementById('collPickerMenu').classList.remove('open');
    }
});

// Close picker when clicking outside
document.addEventListener('click', function(e) {
    var picker = document.getElementById('collPickerMenu');
    if (picker.classList.contains('open') && !e.target.closest('.coll-picker')) {
        picker.classList.remove('open');
    }
});

async function addSelectionToCollection(collId, collName) {
    var paths = Array.from(multiSelected);
    if (paths.length === 0 && selectedFile) paths = [selectedFile];
    if (paths.length === 0) return;
    var r = await API.post('/api/collection/add', { id: collId, paths: paths });
    if (!r) return;
    showToast(paths.length + ' added to "' + collName + '"');
    loadCollections();
}

// ── Utils ──
function escHtml(s) { var d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }
function escAttr(s) { return (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;'); }
function formatSize(b) { if(!b) return '?'; if(b<1024) return b+' B'; if(b<1024*1024) return (b/1024).toFixed(1)+' KB'; return (b/1024/1024).toFixed(1)+' MB'; }
function copyFallback(text) {
    try {
        var ta = document.createElement('textarea');
        ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        var ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return ok;
    } catch(e) { return false; }
}
function showToast(msg) { var e=document.querySelector('.toast'); if(e)e.remove(); var t=document.createElement('div'); t.className='toast'; t.textContent=msg; document.body.appendChild(t); setTimeout(function(){t.remove()},2500); }
async function updateStatus() { var s=await API.get('/api/stats'); if(!s) return; document.getElementById('statusFiles').textContent=s.files+' images'; document.getElementById('statusFolders').textContent=s.folders+' folders'; document.getElementById('statusMeta').textContent=s.with_metadata+' with metadata'; document.getElementById('statusFav').textContent='\u2B50 '+(s.favorites||0)+' favorites'; document.getElementById('favCount').textContent=s.favorites||0; }

// Mark the current folder as active in the tree. During restore the deep folder
// item may still be loading (lazy expansion), so retry briefly until it exists.
function setActiveFolder(path) {
    var items = document.querySelectorAll('.folder-item');
    var target = null;
    items.forEach(function(f){ if (f.dataset.path === path) target = f; });
    if (!target) return false;
    items.forEach(function(f){ f.classList.remove('active'); });
    target.classList.add('active');
    return true;
}
function highlightFolderRetry(path) {
    if (setActiveFolder(path)) return;
    var tries = 0;
    var iv = setInterval(function(){ if (setActiveFolder(path) || ++tries > 25) clearInterval(iv); }, 80);
}

// ── Init ──
(async function() {
    applyMetaPanelState();
    var info = await API.get('/api/info');
    if (info) hasTrash = !!info.trash;
    var savedState = loadGalleryState();
    var savedFolder = savedState.folder;
    if (savedFolder === undefined || savedFolder === null) {
        try { savedFolder = localStorage.getItem('galleryFolder') || ''; } catch (e) { savedFolder = ''; }
    }
    var restoringFolderView = !(savedState.searchMode && savedState.searchQuery) && !(savedState.specialView || '');
    if (restoringFolderView) currentFolder = savedFolder || '';
    await loadFolderTree();
    await loadCollections();
    if (savedState.sort && document.getElementById('sortSelect')) {
        document.getElementById('sortSelect').value = savedState.sort;
    }
    currentPage = Math.max(1, parseInt(savedState.page || '1', 10) || 1);
    pendingRestoreScroll = savedState.scrollTop || 0;
    if (savedState.searchMode && savedState.searchQuery) {
        searchMode = true;
        searchQuery = savedState.searchQuery || '';
        searchFolderFilter = savedState.searchFolderFilter || null;
        document.getElementById('searchInput').value = searchQuery;
        document.getElementById('searchClear').classList.add('visible');
        await doSearch();
    } else if (savedState.specialView === 'favorites') {
        await loadFavorites(document.querySelector('#navFavorites'), false);
    } else if (savedState.specialView === 'today' || savedState.specialView === '7days' || savedState.specialView === '30days') {
        await loadTimeline(savedState.specialView, document.querySelector('#nav' + (savedState.specialView === 'today' ? 'Today' : savedState.specialView === '7days' ? 'Week' : 'Month')), false);
    } else if (savedState.specialView && savedState.specialView.startsWith('collection:')) {
        var savedColId = parseInt(savedState.specialView.split(':')[1], 10);
        var savedCol = collectionsCache.find(function(c) { return c.id === savedColId; });
        if (savedCol) await viewCollection(savedCol, false);
        else await loadGallery('');
    } else {
        currentFolder = savedFolder || '';
        updateBreadcrumb(currentFolder);
        await loadGallery(currentFolder);
        if (currentFolder) highlightFolderRetry(currentFolder);
    }
    // Re-select the image you last clicked, if it's on the current page.
    try {
        var savedImg = savedState.selectedImage || localStorage.getItem('gallerySelectedImage') || '';
        if (savedImg) {
            var sidx = currentFiles.findIndex(function(f){ return f.path === savedImg; });
            if (sidx >= 0) {
                var scard = document.querySelector('.thumb-card[data-index="' + sidx + '"]');
                if (scard) selectImage(savedImg, scard);
            }
        }
    } catch (e) {}
    await updateStatus();
    setInterval(updateStatus, 30000);
})();

function openCompare() {
    var selected = Array.from(multiSelected);
    if (selected.length < 2) { alert('Select at least 2 images to compare (Ctrl+Click)'); return; }
    if (selected.length > 4) { alert('Compare supports up to 4 images. You have ' + selected.length + ' selected.'); return; }
    /* Use repeated path= params instead of CSV — a filename can legitimately contain a comma,
       which would otherwise be %2C-decoded and then re-split on the comma boundary. */
    var query = selected.map(function(p){ return 'path=' + encodeURIComponent(p); }).join('&');
    window.location.href = '/compare?' + query;
}

async function saveToLibrary() {
    var m = window._libMeta, p = window._libPath;
    if (!m || !m.parsed || !m.parsed.prompt) return;
    var parsed = m.parsed, info = m.info || {};
    /* The hub's metadata parser nests A1111/Forge settings under parsed.settings with the
       wire-format capitalized keys (Steps, Sampler, "CFG scale", Seed, Model, Size).
       Reading parsed.steps etc. always returned undefined, so source_meta ended up as {}.
       Read the right place; keep parsed.* as fallback for non-A1111 parser output. */
    var st = parsed.settings || {};
    var smeta = {};
    var _model   = st.Model        || parsed.model;     if (_model)   smeta.model = _model;
    var _sampler = st.Sampler      || parsed.sampler;   if (_sampler) smeta.sampler = _sampler;
    var _steps   = st.Steps        || parsed.steps;     if (_steps)   smeta.steps = _steps;
    var _cfg     = st['CFG scale'] || parsed.cfg_scale; if (_cfg)     smeta.cfg = _cfg;
    var _seed    = st.Seed         || parsed.seed;      if (_seed)    smeta.seed = _seed;
    var _size    = st.Size || (info.width && info.height ? info.width + 'x' + info.height : '');
    if (_size) smeta.size = _size;
    var title = prompt('Save to Prompt Library\n\nTitle:', (parsed.prompt || '').substring(0, 60).replace(/,.*/, '').trim() || 'Untitled');
    if (title === null) return;
    var targetSel = document.getElementById('saveTargetSelect');
    var target = (targetSel && targetSel.value) || 'general';
    var data = {
        title: title || 'Untitled',
        type: 'generation',
        target: target,
        content: parsed.prompt || '',
        negative: parsed.negative_prompt || '',
        notes: '',
        source_image: p || '',
        source_meta: JSON.stringify(smeta),
        tags: ''
    };
    // Try to also upload the image as an attachment so the library card is self-contained
    if (p) {
        try {
            var blob = await fetch('/image/' + encodeURIComponent(p)).then(function(r){return r.ok ? r.blob() : null;});
            if (blob) {
                var basename = (p.split('/').pop().split('\\').pop()) || 'image';
                var fd = new FormData();
                fd.append('file', blob, basename);
                var up = await fetch('/api/library/attachment', {method:'POST', body: fd}).then(function(r){return r.json();});
                if (up && up.ok) {
                    data.attachment = up.attachment;
                    data.attachment_type = up.attachment_type;
                    data.attachment_name = up.attachment_name;
                }
            }
        } catch (e) { /* fall through: save card without attachment */ }
    }
    fetch('/api/library/card', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)})
        .then(function(r) { return r.json(); })
        .then(function(r) {
            if (r.ok) { alert('Saved to Prompt Library: ' + title + (data.attachment ? '\n(image attached)' : '')); }
            else { alert('Error: ' + (r.error || 'Unknown')); }
        })
        .catch(function(e) { alert('Error: ' + e); });
        }

        function forgeGenerate() {
            var m = window._libMeta;
            if (!m || !m.parsed || !m.parsed.prompt) return;
            var parsed = m.parsed;
            var statusEl = document.getElementById('metaForgeStatus');
            var btn = document.querySelector('.meta-forge-btn');
            if (!statusEl) return;
            btn.disabled = true;
            btn.textContent = 'Generating...';
            statusEl.className = 'meta-forge-status loading';
            statusEl.textContent = 'Sending to Forge API...';

            var st = parsed.settings || {};

            // Extract width and height from Size: WxH if not available as top-level fields
            var w = parsed.width;
            var h = parsed.height;
            if (!w || !h) {
                var sizeStr = st.Size || '';
                var sizeParts = sizeStr.split('x');
                if (sizeParts.length === 2) {
                    w = w || parseInt(sizeParts[0]);
                    h = h || parseInt(sizeParts[1]);
                }
            }

            // Use nullish coalescing (??) so that 0 is NOT treated as missing
            // Send seed as string to preserve precision for large values (>2^53)
            var rawSeed = st.Seed ?? parsed.seed;

            // Extract sampler name and schedule type
            // Priority: 1) st['Schedule type'] if ComfyUI parser provided it directly
            //           2) Parse from st.Sampler munged format (e.g. "res_2s_beta")
            //           3) parsed.sampler_name fallback
            var rawSampler = st.Sampler ?? parsed.sampler_name;
            var samplerName = 'Euler';
            var scheduleType = 'Beta';

            // Check if ComfyUI parser already extracted Schedule type separately
            var explicitSchedule = st['Schedule type'];
            if (explicitSchedule) {
                scheduleType = explicitSchedule;
            }

            if (rawSampler) {
                var samplerParts = rawSampler.split('_');
                if (samplerParts.length >= 2) {
                    samplerName = samplerParts[0];
                    var schedPart = samplerParts.slice(1).join('_');
                    // Capitalize first letter for Forge API (only if not already explicit)
                    if (!explicitSchedule) {
                        scheduleType = schedPart.charAt(0).toUpperCase() + schedPart.slice(1);
                    }
                } else {
                    samplerName = rawSampler;
                }
            }

            // Extract shift value if present
            var shiftVal = st.Shift ?? parsed.shift;

            var _stepsRaw = st.Steps ?? parsed.steps;
            var _cfgRaw = st['CFG scale'] ?? parsed.cfg_scale;
            var _steps = (typeof _stepsRaw === 'number') ? _stepsRaw : parseInt(_stepsRaw);
            var _cfg = (typeof _cfgRaw === 'number') ? _cfgRaw : parseFloat(_cfgRaw);
            var payload = {
                prompt: parsed.prompt || '',
                negative_prompt: parsed.negative_prompt || '',
                steps: (isNaN(_steps) ? 20 : _steps),
                cfg_scale: (isNaN(_cfg) ? 7.0 : _cfg),
                seed: rawSeed != null ? String(rawSeed) : '-1',
                sampler_name: samplerName,
                schedule_type: scheduleType,
                width: w ? parseInt(w) : 512,
                height: h ? parseInt(h) : 512,
                model: st.Model || parsed.model || ''
            };

            // Only include shift if it has a value
            if (shiftVal) {
                payload.shift = parseFloat(shiftVal);
            }

            // DEBUG: log exact payload sent to server
            console.log('[FORGE DEBUG] Payload to /api/viewer/forge/generate:', JSON.stringify(payload, null, 2));

            fetch('/api/viewer/forge/generate', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            })
            .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
            .then(function(r) {
                if (!r.ok || r.data.error) {
                    statusEl.className = 'meta-forge-status error';
                    statusEl.textContent = r.data.error || 'Generation failed';
                } else {
                    statusEl.className = 'meta-forge-status success';
                    var savedTo = r.data.saved_to ? 'Saved to ' + r.data.saved_to : 'Generation complete.';
                    statusEl.textContent = savedTo;
                    // Navigate to viewer with the saved image path
                    if (r.data.saved_to) {
                        window.location.href = '/viewer?path=' + encodeURIComponent(r.data.saved_to);
                    }
                }
            })
            .catch(function(e) {
                statusEl.className = 'meta-forge-status error';
                statusEl.textContent = 'Network error: ' + e.message;
            })
            .finally(function() {
                btn.disabled = false;
                btn.textContent = '\uD83E\uDEEA Generate with Forge';
            });
        }
        </script>
        """
</html>
'''


# ─── GalleryModule (hub integration) ──────────────────────────────────────────

class GalleryModule(Module):
    """Module wrapper around GalleryDB + the HTML gallery UI."""

    name = "Gallery"
    version = "1.2.4"
    icon = "\U0001F5BC"   # 🖼
    description = "Browse and manage your AI-generated image collection."
    order = 10

    settings_schema = {
        "folders": {
            "type": "folder_list",
            "label": "Image folders",
            "default": [],
            "desc": "Folders to scan for AI-generated images. The first folder is used as the gallery root. Requires restart.",
            "requires_restart": True,
        },
        "per_page": {
            "type": "number", "label": "Images per page", "default": 200,
            "min": 50, "max": 1000,
            "desc": "Pagination size in the grid. Applies on next load.",
        },
        "background_reindex": {
            "type": "bool", "label": "Background re-indexing", "default": False,
            "desc": "Periodically rescan the folder for new files. Off by default; use the Re-index button for a manual rescan. Requires restart to change.",
            "requires_restart": True,
        },
        "reindex_interval": {
            "type": "number", "label": "Re-index interval (seconds)", "default": 30,
            "min": 5, "max": 3600,
            "desc": "How often to rescan. Requires restart.",
            "requires_restart": True,
        },
        "thumb_workers": {
            "type": "number", "label": "Thumbnail workers", "default": 0,
            "min": 0, "max": 32,
            "desc": "Threads used by 'Generate all thumbnails'. 0 = auto (min(8, CPU count)).",
        },
        "accordion_folders": {
            "type": "bool", "label": "Close other folders when opening one", "default": False,
            "desc": "Keeps the folder tree compact by closing sibling branches when you open a folder. Off by default.",
        },
        "skip_delete_confirmation": {
            "type": "bool", "label": "Skip delete confirmation", "default": False,
            "desc": "Delete immediately after pressing Delete or the trash button. Files still go to the system trash.",
        },
        "index_workers": {
            "type": "number", "label": "Index workers", "default": 0,
            "min": 0, "max": 32,
            "desc": "Parallel threads for reading metadata + image dimensions during indexing. "
                    "0 = auto (min(8, CPU count + 2)). Higher can speed up large libraries on fast "
                    "SSDs; lower is gentler on slow/network drives.",
        },
        "verbose": {
            "type": "bool", "label": "Verbose metadata debug log", "default": False,
            "desc": "Print parser debug to the server console.",
        },
    }

    def __init__(self, hub):
        super().__init__(hub)
        self.db = None
        self.root_path = ""
        self._bg_thread = None
        self._thumb_thread = None
        self._index_thread = None
        self._search_thread = None

    # ─── Lifecycle ───────────────────────────────────────────────────────────
    def _start_index(self, force=False, label="Index"):
        if not self.db:
            return False
        if self._index_thread and self._index_thread.is_alive():
            print(f"[GALLERY] {label} skipped; index already running")
            return False

        workers = int(self.setting("index_workers", 0) or 0)

        def run():
            try:
                print(f"[GALLERY] {label} started in background...")
                self.db.index_tree(force=force, workers=workers)
                stats = self.db.get_stats()
                print(f"[GALLERY] {stats['files']} images, {stats['folders']} folders, "
                      f"{stats['with_metadata']} with metadata")
            except Exception as e:
                print(f"[GALLERY] {label} failed: {e}")
                traceback.print_exc()

        self._index_thread = threading.Thread(target=run, daemon=True)
        self._index_thread.start()
        return True

    def on_startup(self):
        global DEFAULT_PER_PAGE
        folders = self.setting("folders", [])
        # Backwards compat: if old "folder" setting exists, migrate it
        if not folders:
            old_folder = self.setting("folder", "")
            if old_folder:
                folders = [old_folder]
                self.hub.settings.set_module_setting("gallery", "folders", folders)
        DEFAULT_PER_PAGE = int(self.setting("per_page", 200) or 200)
        _metadata_mod.VERBOSE = bool(self.setting("verbose", False)) or _metadata_mod.VERBOSE

        # Build roots dict from valid folders
        roots = {}
        for f in (folders or []):
            f = f.strip()
            if f and os.path.isdir(f):
                name = os.path.basename(f.rstrip("/\\"))
                # Disambiguate duplicate basenames
                base_name = name
                counter = 2
                while name in roots:
                    name = f"{base_name}_{counter}"
                    counter += 1
                roots[name] = os.path.abspath(f)

        if not roots:
            print("[GALLERY] No image folder configured. Open Settings to add one.")
            return

        # Central DB and thumbs next to hub.py
        hub_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        db_path = os.path.join(hub_dir, "cyberdelia.db")
        self.thumb_dir = os.path.join(hub_dir, ".thumbs")
        os.makedirs(self.thumb_dir, exist_ok=True)

        for rn, rp in roots.items():
            print(f"[GALLERY] Root: {rn} -> {rp}")
        migrate_thumb_layout(self.thumb_dir)
        self.db = GalleryDB(db_path, self.thumb_dir, roots)
        self._start_index(force=False, label="Startup index")

        if self.setting("background_reindex", False):
            interval = int(self.setting("reindex_interval", DEFAULT_REINDEX_INTERVAL) or DEFAULT_REINDEX_INTERVAL)

            def bg():
                while True:
                    time.sleep(interval)
                    try:
                        self._start_index(force=False, label="Background index")
                    except Exception as e:
                        print(f"[BG-INDEX] {e}")
            self._bg_thread = threading.Thread(target=bg, daemon=True)
            self._bg_thread.start()
            print(f"[GALLERY] Background re-indexing every {interval}s")
        else:
            print("[GALLERY] Background re-indexing disabled")

    def on_settings_changed(self, key, value):
        # Live-apply where safe; others need a restart (flagged in schema).
        if key == "per_page":
            global DEFAULT_PER_PAGE
            try:
                DEFAULT_PER_PAGE = max(50, min(int(value), 1000))
            except (TypeError, ValueError):
                pass
        elif key == "verbose":
            _metadata_mod.VERBOSE = bool(value)

    # ─── Routing ─────────────────────────────────────────────────────────────
    def routes_get(self):
        return {
            "/gallery": self._page,
            "/api/folders": self._api_folders,
            "/api/files": self._api_files,
            "/api/favorites": self._api_favorites,
            "/api/timeline": self._api_timeline,
            "/api/metadata": self._api_metadata,
            "/api/stats": self._api_stats,
            "/api/info": self._api_info,
            "/api/search": self._api_search,
            "/api/search_folder": self._api_search_folder,
            "/api/tags": self._api_tags,
            "/api/collections": self._api_collections,
            "/api/collection/files": self._api_collection_files,
            "/api/file/collections": self._api_file_collections,
        }

    def routes_post(self):
        return {
            "/api/reindex": self._api_reindex,
            "/api/rebuild_search": self._api_rebuild_search,
            "/api/optimize_db": self._api_optimize_db,
            "/api/delete": self._api_delete,
            "/api/favorite": self._api_favorite,
            "/api/collection/create": self._api_collection_create,
            "/api/collection/rename": self._api_collection_rename,
            "/api/collection/delete": self._api_collection_delete,
            "/api/collection/add": self._api_collection_add,
            "/api/collection/remove": self._api_collection_remove,
            "/api/generate_thumbs": self._api_generate_thumbs,
        }

    def prefix_routes(self):
        return {
            "/thumb/": self._handle_thumb,
            "/image/": self._handle_image,
        }

    # ─── Page ────────────────────────────────────────────────────────────────
    def _page(self, handler, qs):
        from html import escape
        from core.server import _font_links
        title = escape(self.hub.settings.get("title", "CyberHub"))
        module_nav = build_module_menu(self.hub.registry, "gallery")
        page = (
            GALLERY_BODY
            .replace("{HUB_TITLE}", title)
            .replace("{MODULE_NAV}", module_nav)
            .replace("{FONT_LINKS}", _font_links())
            .replace("{BODY_CLASS}", theme_body_class(self.hub.settings))
            .replace("{FORGE_ENABLED}", "true" if self.hub.settings.get_path("forge.enabled", False) else "false")
            .replace("{ACCORDION_FOLDERS}", "true" if self.setting("accordion_folders", False) else "false")
            .replace("{SKIP_DELETE_CONFIRMATION}", "true" if self.setting("skip_delete_confirmation", False) else "false")
            .replace("{HELP_OVERLAY}", HELP_OVERLAY_HTML)
        )
        handler.respond_html(page)

    # ─── Image / thumb prefix routes ─────────────────────────────────────────
    def _handle_thumb(self, handler, rel_path):
        if not self._require_db(handler): return
        abs_path = self.db.resolve_path(rel_path)
        thumb = ensure_thumbnail(self.thumb_dir, rel_path, abs_path)
        if thumb:
            handler.serve_file(thumb, immutable=".thumbs" in thumb)
        else:
            handler.send_error(403)

    def _handle_image(self, handler, rel_path):
        if not self.db:
            handler.send_error(503); return
        full = self.db.resolve_path(rel_path)
        if full and os.path.isfile(full):
            handler.serve_file(full)
        else:
            handler.send_error(403)

    # ─── Shared guard ────────────────────────────────────────────────────────
    def _require_db(self, handler):
        if self.db is None:
            handler.respond_json({"error":
                "Gallery not configured. Open Settings and set an image folder."},
                status=503)
            return False
        return True

    # ─── GET API ─────────────────────────────────────────────────────────────
    def _api_folders(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_subfolders(qs.get("parent", [""])[0]))

    def _api_files(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_files(
            folder=qs.get("folder", [""])[0],
            sort=qs.get("sort", ["name"])[0],
            order=qs.get("order", ["asc"])[0],
            page=handler._int(qs, "page"),
            per_page=handler._int(qs, "per_page", DEFAULT_PER_PAGE),
            favorite_only=qs.get("favorites", [""])[0] == "1",
            time_filter=qs.get("time", [None])[0],
        ))

    def _api_favorites(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_all_favorites(
            sort=qs.get("sort", ["date"])[0],
            order=qs.get("order", ["desc"])[0],
            page=handler._int(qs, "page"),
            per_page=handler._int(qs, "per_page", DEFAULT_PER_PAGE),
        ))

    def _api_timeline(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_timeline_files(
            time_filter=qs.get("period", ["today"])[0],
            sort=qs.get("sort", ["date"])[0],
            order=qs.get("order", ["desc"])[0],
            page=handler._int(qs, "page"),
            per_page=handler._int(qs, "per_page", DEFAULT_PER_PAGE),
        ))

    def _api_metadata(self, handler, qs):
        if not self._require_db(handler): return
        data = self.db.get_file_metadata(qs.get("path", [""])[0]) or {}
        # Enrich with Civitai info (this is what gallery.py did in the DB; we do
        # it here so the DB layer doesn't depend on the hub object).
        if data:
            data["civitai"] = self.hub.civitai.lookup(data.get("parsed", {}))
        handler.respond_json(data)

    def _api_stats(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_stats())

    def _api_info(self, handler, qs):
        handler.respond_json({"trash": HAS_TRASH})

    def _api_search(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.search(
            query=qs.get("q", [""])[0],
            page=handler._int(qs, "page"),
            per_page=handler._int(qs, "per_page", DEFAULT_PER_PAGE),
            sort=qs.get("sort", ["date"])[0],
            order=qs.get("order", ["desc"])[0],
        ))

    def _api_search_folder(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.search_in_folder(
            query=qs.get("q", [""])[0],
            folder=qs.get("folder", [""])[0],
            page=handler._int(qs, "page"),
            per_page=handler._int(qs, "per_page", DEFAULT_PER_PAGE),
            sort=qs.get("sort", ["date"])[0],
            order=qs.get("order", ["desc"])[0],
        ))

    def _api_reindex(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        data = handler.read_body_json(content_len)
        if content_len > 0 and data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return
        data = data or {}
        force = bool(data.get("force", False))
        started = self._start_index(force=force, label="Manual index")
        handler.respond_json({"ok": True, "force": force, "started": started})

    def _api_rebuild_search(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        if self._search_thread and self._search_thread.is_alive():
            handler.respond_json({"ok": True, "started": False})
            return

        def run():
            try:
                self.db.rebuild_search_index()
            except Exception as e:
                print(f"[GALLERY] Search rebuild failed: {e}")
                traceback.print_exc()

        self._search_thread = threading.Thread(target=run, daemon=True)
        self._search_thread.start()
        handler.respond_json({"ok": True, "started": True})

    def _api_optimize_db(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        try:
            r = self.db.optimize()
        except Exception as e:
            handler.respond_json({"error": str(e)}, status=500); return
        before, after = r["before"], r["after"]
        saved = max(0, before - after)
        handler.respond_json({
            "ok": True,
            "vacuumed": r["vacuumed"], "fts": r["fts"],
            "before": before, "after": after, "saved": saved,
            "before_mb": round(before / (1024 * 1024), 2),
            "after_mb": round(after / (1024 * 1024), 2),
            "saved_kb": round(saved / 1024, 1),
        })

    def _api_tags(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_tags(
            prefix=qs.get("q", [""])[0],
            limit=handler._int(qs, "limit", 50),
        ))

    def _api_collections(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_collections())

    def _api_collection_files(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_collection_files(
            collection_id=handler._int(qs, "id", 0, min_val=0),
            sort=qs.get("sort", ["date"])[0],
            order=qs.get("order", ["desc"])[0],
            page=handler._int(qs, "page"),
            per_page=handler._int(qs, "per_page", DEFAULT_PER_PAGE),
        ))

    def _api_file_collections(self, handler, qs):
        if not self._require_db(handler): return
        handler.respond_json(self.db.get_file_collections(qs.get("path", [""])[0]))

    # ─── POST API ────────────────────────────────────────────────────────────
    def _api_delete(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        if not HAS_TRASH:
            handler.respond_json({"error": "Safe delete requires send2trash. Install it: pip install send2trash"}, status=400)
            return
        data = handler.read_body_json(content_len)
        if not data:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        paths = data.get("paths", [])
        if not paths or not isinstance(paths, list):
            handler.respond_json({"error": "No paths provided"}, status=400); return
        if len(paths) > 500:
            handler.respond_json({"error": "Too many files (max 500)"}, status=400); return
        results = self.db.delete_files(paths)
        ok_count = sum(1 for r in results if r["ok"])
        handler.respond_json({
            "results": results, "deleted": ok_count,
            "failed": len(results) - ok_count, "trash": HAS_TRASH,
        })

    def _api_favorite(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        data = handler.read_body_json(content_len)
        if not data:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        fpath = data.get("path", "")
        if not fpath:
            handler.respond_json({"error": "No path provided"}, status=400); return
        result = self.db.toggle_favorite(fpath)
        if result is None:
            handler.respond_json({"error": "File not found"}, status=404); return
        handler.respond_json(result)

    def _api_collection_create(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        data = handler.read_body_json(content_len)
        if not data:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        name = data.get("name", "").strip()
        if not name:
            handler.respond_json({"error": "No name provided"}, status=400); return
        handler.respond_json(self.db.create_collection(name, data.get("color", "#4a9eff")))

    def _api_collection_rename(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        data = handler.read_body_json(content_len)
        if not data:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        handler.respond_json(self.db.rename_collection(int(data.get("id", 0)), data.get("name", "")))

    def _api_collection_delete(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        data = handler.read_body_json(content_len)
        if not data:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        handler.respond_json(self.db.delete_collection(int(data.get("id", 0))))

    def _api_collection_add(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        data = handler.read_body_json(content_len)
        if not data:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        handler.respond_json(self.db.add_to_collection(int(data.get("id", 0)), data.get("paths", [])))

    def _api_collection_remove(self, handler, content_len, content_type):
        if not self._require_db(handler): return
        data = handler.read_body_json(content_len)
        if not data:
            handler.respond_json({"error": "Invalid JSON"}, status=400); return
        handler.respond_json(self.db.remove_from_collection(int(data.get("id", 0)), data.get("paths", [])))

    def _api_generate_thumbs(self, handler, content_len, content_type):
        """Kick off thumbnail generation in a background thread; reply immediately."""
        if not self._require_db(handler): return
        if self._thumb_thread and self._thumb_thread.is_alive():
            handler.respond_json({"ok": True, "already_running": True}); return
        workers = self.setting("thumb_workers", 0) or 0
        try:
            workers = int(workers)
        except (TypeError, ValueError):
            workers = 0
        if workers <= 0:
            workers = None  # auto

        # Count work across all roots
        total = 0
        for root_abs in self.db.roots.values():
            if not os.path.isdir(root_abs): continue
            for dirpath, dirnames, filenames in os.walk(root_abs):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fname in filenames:
                    if Path(fname).suffix.lower() in IMAGE_EXTENSIONS:
                        total += 1

        thumb_dir = self.thumb_dir
        roots = self.db.roots

        def _worker():
            try:
                generate_all_thumbs(thumb_dir, roots, workers)
            except Exception as e:
                print(f"[THUMBS] Background job failed: {e}")

        self._thumb_thread = threading.Thread(target=_worker, daemon=True)
        self._thumb_thread.start()
        handler.respond_json({"ok": True, "total": total, "workers": workers or "auto"})
