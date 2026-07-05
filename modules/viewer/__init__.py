"""Meta Viewer module — drop an image to inspect its generation metadata."""

import json
import os
import struct
import tempfile
import zlib

from core import Module
from core.metadata import get_image_metadata, parse_sd_parameters
from core.server import build_shell


TEXT_CHUNK_TAGS = (b"tEXt", b"iTXt", b"zTXt")


def _png_chunk(tag, payload):
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)


def _png_text_chunk(key, value):
    key = str(key or "").strip()
    if not key or "\x00" in key:
        raise ValueError("Metadata keys must be non-empty and cannot contain null bytes")
    value = "" if value is None else str(value)
    try:
        key_bytes = key.encode("latin-1")
        value_bytes = value.encode("latin-1")
        return b"tEXt", key_bytes + b"\x00" + value_bytes
    except UnicodeEncodeError:
        key_bytes = key.encode("utf-8")
        value_bytes = value.encode("utf-8")
        # iTXt layout: key\0 compression_flag compression_method language\0 translated_key\0 text
        return b"iTXt", key_bytes + b"\x00\x00\x00\x00\x00" + value_bytes


def rewrite_png_metadata(png_bytes, metadata):
    """Replace PNG text metadata chunks while preserving image pixels/chunks."""
    if not isinstance(metadata, dict):
        raise ValueError("Raw metadata must be a JSON object")
    if png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Metadata editing currently supports PNG files only")
    chunks = []
    pos = 8
    while pos + 8 <= len(png_bytes):
        length = struct.unpack(">I", png_bytes[pos:pos + 4])[0]
        tag = png_bytes[pos + 4:pos + 8]
        payload_start = pos + 8
        payload_end = payload_start + length
        crc_end = payload_end + 4
        if crc_end > len(png_bytes):
            raise ValueError("Invalid PNG chunk structure")
        chunks.append((tag, png_bytes[payload_start:payload_end]))
        pos = crc_end
        if tag == b"IEND":
            break
    text_chunks = []
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        text_chunks.append(_png_text_chunk(key, value))
    out = bytearray(png_bytes[:8])
    inserted = False
    for tag, payload in chunks:
        if tag in TEXT_CHUNK_TAGS:
            continue
        out += _png_chunk(tag, payload)
        if tag == b"IHDR" and not inserted:
            for text_tag, text_payload in text_chunks:
                out += _png_chunk(text_tag, text_payload)
            inserted = True
    return bytes(out)


class ViewerModule(Module):
    name = "Viewer"
    version = "1.1"
    icon = "\U0001F50D"   # 🔍
    description = "Drop an image to view its generation metadata."
    order = 20

    settings_schema = {
        # No persistent settings yet — but the module can still be
        # toggled on/off from the Settings page.
    }

    def routes_get(self):
        return {"/viewer": self._page}

    def routes_post(self):
        return {
            "/api/viewer/analyze": self._analyze,
            "/api/viewer/rewrite": self._rewrite,
            "/api/viewer/forge/generate": self._forge_generate,
        }

    def prefix_routes(self):
        return {"/viewer/file/": self._serve_file}

    def _serve_file(self, handler, rel_path):
        """Serve a file from an absolute path encoded in the URL.

        URL pattern: /viewer/file/ABS_PATH
        The rel_path is the URL-decoded absolute filesystem path.
        """
        filepath = rel_path
        if not filepath or not os.path.isfile(filepath):
            handler.send_error(404)
            return
        # Determine MIME type from extension
        ext = os.path.splitext(filepath)[1].lower()
        mime_map = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
            ".tiff": "image/tiff", ".tif": "image/tiff",
        }
        content_type = mime_map.get(ext, "application/octet-stream")
        handler.serve_file(filepath)

    def _page(self, handler, qs):
        html = build_shell(
            self.hub.registry, self.hub.settings,
            active_key="viewer", page_title="Meta Viewer",
            body_html=PAGE_BODY,
        )
        handler.respond_html(html)

    def _analyze(self, handler, content_len, content_type):
        try:
            files = handler.parse_multipart(content_len, content_type)
            file_item = files.get("file")
            if not file_item or not file_item.get("data"):
                handler.respond_json({"error": "No file uploaded"}, status=400)
                return
            # Use original extension for correct metadata reading
            filename = file_item.get("filename", "")
            suffix = os.path.splitext(filename)[1].lower() if filename else ".png"
            if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
                suffix = ".png"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(file_item["data"])
                tmp_path = tmp.name
            try:
                meta = get_image_metadata(tmp_path)
                parsed = {}
                if "parameters" in meta:
                    parsed = parse_sd_parameters(meta["parameters"])
                elif "prompt" in meta:
                    prompt = meta.get("prompt", "")
                    if isinstance(prompt, str) and prompt.lstrip().startswith(("{", "[")):
                        parsed["workflow"] = prompt[:2000] + "..." if len(prompt) > 2000 else prompt
                    else:
                        parsed["prompt"] = prompt
                w, h = 0, 0
                try:
                    from PIL import Image
                    with Image.open(tmp_path) as img:
                        w, h = img.size
                except Exception:
                    pass
                civitai = self.hub.civitai.lookup(parsed)
                handler.respond_json({
                    "parsed": parsed, "raw_meta": meta,
                    "info": {"width": w, "height": h},
                    "civitai": civitai,
                })
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception as e:
            handler.respond_json({"error": str(e)}, status=500)

    def _rewrite(self, handler, content_len, content_type):
        try:
            files = handler.parse_multipart(content_len, content_type)
            file_item = files.get("file")
            meta_item = files.get("raw_meta_json")
            if not file_item or not file_item.get("data"):
                handler.respond_json({"error": "No file uploaded"}, status=400)
                return
            if not meta_item or not meta_item.get("data"):
                handler.respond_json({"error": "No metadata payload provided"}, status=400)
                return
            filename = file_item.get("filename", "") or "image.png"
            if os.path.splitext(filename)[1].lower() != ".png":
                handler.respond_json({"error": "Metadata editing currently supports PNG files only"}, status=400)
                return
            try:
                raw_meta = json.loads(meta_item["data"].decode("utf-8"))
            except Exception as exc:
                handler.respond_json({"error": f"Invalid metadata JSON: {exc}"}, status=400)
                return
            edited = rewrite_png_metadata(file_item["data"], raw_meta)
            stem = os.path.splitext(os.path.basename(filename))[0] or "image"
            handler.respond_binary(edited, "image/png", download_name=f"{stem}_metadata.png")
        except ValueError as e:
            handler.respond_json({"error": str(e)}, status=400)
        except Exception as e:
            handler.respond_json({"error": str(e)}, status=500)

    def _forge_generate(self, handler, content_len, content_type):
        """Send current image's generation parameters to Forge API (txt2img)."""
        import urllib.request
        import urllib.error
        import base64
        import datetime

        data = handler.read_body_json(content_len)
        if data is None:
            handler.respond_json({"error": "Invalid JSON"}, status=400)
            return

        forge_url = self.hub.settings.get_path("forge.api_url", "").strip().rstrip("/")
        forge_enabled = self.hub.settings.get_path("forge.enabled", False)

        if not forge_enabled:
            handler.respond_json({"error": "Forge Connection is not enabled. Enable it in Settings."}, status=400)
            return
        if not forge_url:
            handler.respond_json({"error": "Forge API URL is not configured. Set it in Settings."}, status=400)
            return

        prompt = data.get("prompt", "")
        if not prompt:
            handler.respond_json({"error": "No prompt found in image metadata"}, status=400)
            return

        # Build the Forge API payload
        # Seed can be a large integer string from JS — parse safely
        seed_val = data.get("seed", -1)
        try:
            seed_val = int(seed_val)
        except (ValueError, TypeError):
            seed_val = -1

        # Width/height: client may send them directly or as a "Size" string
        width = data.get("width")
        height = data.get("height")
        if width is None or height is None:
            size_str = data.get("size", "")
            if size_str:
                size_parts = str(size_str).split("x")
                if len(size_parts) == 2:
                    try:
                        width = width or int(size_parts[0])
                        height = height or int(size_parts[1])
                    except ValueError:
                        pass

        # Safely convert to int/float — handle keys that exist but are null in JSON
        steps_val = data.get("steps")
        cfg_val = data.get("cfg_scale")
        payload = {
            "prompt": prompt,
            "negative_prompt": data.get("negative_prompt", ""),
            "steps": int(steps_val) if steps_val is not None else 20,
            "cfg_scale": float(cfg_val) if cfg_val is not None else 7.0,
            "seed": seed_val,
            "sampler_name": data.get("sampler_name", "Euler"),
            "scheduler": data.get("schedule_type", "Beta"),
            "width": int(width) if width else 512,
            "height": int(height) if height else 512,
            "batch_size": 1,
        }

        # Optional: shift parameter (used by newer schedulers)
        shift = data.get("shift")
        model = data.get("model", "")
        if shift is not None or model:
            if "override_settings" not in payload:
                payload["override_settings"] = {}
            if shift is not None:
                try:
                    payload["eta"] = 0.0
                    payload["s_churn"] = 0.0
                except (ValueError, TypeError):
                    pass
            if model:
                payload["override_settings"]["sd_model_checkpoint"] = model

        # DEBUG: log exact payload sent to Forge
        import logging; _forge_logger = logging.getLogger("cyberhub.forge")
        _forge_logger.info("Forge payload: %s", json.dumps(payload, indent=2))
        print("[FORGE DEBUG] Payload:", json.dumps(payload, indent=2))

        endpoint = f"{forge_url}/sdapi/v1/txt2img"
        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            handler.respond_json({
                "error": f"Forge API returned HTTP {e.code}: {err_body or e.reason}",
            }, status=502)
            return
        except urllib.error.URLError as e:
            handler.respond_json({
                "error": f"Could not connect to Forge API at {forge_url}. Check the URL and that Forge is running. ({e.reason})",
            }, status=502)
            return
        except Exception as e:
            handler.respond_json({"error": f"Error calling Forge API: {e}"}, status=500)
            return

        # Extract the generated image(s)
        images = result.get("images", [])
        if not images:
            handler.respond_json({"error": "Forge API returned no images"}, status=500)
            return

        # Decode the first image
        image_data_b64 = images[0]
        try:
            image_bytes = base64.b64decode(image_data_b64)
        except Exception:
            handler.respond_json({"error": "Failed to decode image from Forge API"}, status=500)
            return

        # Save the image to the configured Forge output directory
        output_dir = self.hub.forge_output_dir
        os.makedirs(output_dir, exist_ok=True)

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"forge_gen_{ts}.png"
        saved_path = os.path.join(output_dir, filename)
        try:
            with open(saved_path, "wb") as f:
                f.write(image_bytes)
        except Exception as e:
            handler.respond_json({
                "error": f"Generated image received but could not save to {output_dir}: {e}",
                "image_b64": image_data_b64,
            }, status=500)
            return

        handler.respond_json({
            "ok": True,
            "image_b64": image_data_b64,
            "saved_to": saved_path,
            "forge_params": result.get("parameters", {}),
            "forge_info": result.get("info", {}),
        })


PAGE_BODY = r"""
<style>
.viewer-content { max-width:1180px; margin:0 auto; padding:18px 22px; }
.drop-zone {
    border:1px dashed var(--border-light); border-radius:8px; padding:14px 16px;
    color:var(--text-dim); transition:all .2s; cursor:pointer;
    background:var(--bg-panel); margin-bottom:14px; display:flex; align-items:center; gap:12px;
}
.drop-zone:hover, .drop-zone.dragover { border-color:var(--accent); background:var(--bg-active); color:var(--text); }
.drop-zone .big { width:34px; height:34px; border-radius:8px; background:var(--bg-card); border:1px solid var(--border); display:flex; align-items:center; justify-content:center; color:var(--accent); flex-shrink:0; }
.drop-zone .big svg { width:18px; height:18px; display:block; }
.drop-zone .drop-main { font-size:13px; color:var(--text); }
.drop-zone .drop-sub { font-size:11px; margin-top:2px; color:var(--text-dim); }
.drop-zone input { display:none; }
.viewer-result { display:none; grid-template-columns:minmax(280px, 42%) minmax(0, 1fr); gap:14px; align-items:start; }
.viewer-result.visible { display:grid; }
.viewer-preview { background:var(--bg-panel); border:1px solid var(--border); border-radius:8px; padding:12px; min-width:0; position:sticky; top:12px; }
.viewer-preview img { display:block; max-width:100%; max-height:calc(100vh - 160px); margin:0 auto; border-radius:6px; border:1px solid var(--border); background:var(--bg-card); object-fit:contain; }
.viewer-file-name { margin-top:10px; font:11px var(--mono); color:var(--text-dim); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.viewer-meta { background:var(--bg-panel); border:1px solid var(--border); border-radius:8px; padding:14px; min-width:0; }
.vm-section { margin-bottom:14px; }
.vm-title { font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.6px; color:var(--text-dim); margin-bottom:6px; padding-bottom:4px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; }
.vm-title .copy-btn { cursor:pointer; color:var(--text-dim); font-size:11px; font-weight:400; text-transform:none; letter-spacing:0; transition:color .15s; }
.vm-title .copy-btn:hover { color:var(--accent); }
.vm-prompt { font-family:var(--mono); font-size:11px; line-height:1.6; color:var(--prompt-text); word-break:break-word; white-space:pre-wrap; background:var(--bg-card); padding:8px 10px; border-radius:var(--radius); border:1px solid var(--border); }
.vm-prompt.negative { color:var(--neg-prompt); }
.vm-grid { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; font-family:var(--mono); font-size:11px; }
.vm-key { color:var(--setting-key); white-space:nowrap; }
.vm-val { color:var(--setting-val); word-break:break-all; }
.vm-raw { font-family:var(--mono); font-size:10px; line-height:1.6; color:var(--text); white-space:pre-wrap; word-break:break-all; background:var(--bg-card); padding:10px; border-radius:var(--radius); border:1px solid var(--border); max-height:300px; overflow-y:auto; }
.vm-file-info { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; font-size:11px; }
.vm-file-info .label { color:var(--text-dim); }
.vm-file-info .value { color:var(--text); font-family:var(--mono); }
.vm-empty { text-align:center; color:var(--text-dim); padding:40px 0; }
.vm-actions { display:flex; gap:10px; align-items:center; }
.vm-action { cursor:pointer; color:var(--text-dim); font-size:11px; font-weight:400; text-transform:none; letter-spacing:0; transition:color .15s; }
.vm-action:hover { color:var(--accent); }
.vm-modal { position:fixed; inset:0; background:rgba(0,0,0,.66); display:none; align-items:center; justify-content:center; z-index:7000; padding:18px; }
.vm-modal.open { display:flex; }
.vm-dialog { width:min(920px, 96vw); max-height:90vh; background:var(--bg-panel); border:1px solid var(--border-light); border-radius:8px; display:flex; flex-direction:column; box-shadow:0 14px 42px rgba(0,0,0,.45); }
.vm-dialog-head, .vm-dialog-foot { padding:12px 16px; display:flex; align-items:center; gap:10px; border-bottom:1px solid var(--border); }
.vm-dialog-foot { border-bottom:0; border-top:1px solid var(--border); justify-content:flex-end; }
.vm-dialog-title { color:var(--text-bright); font-weight:600; font-size:14px; }
.vm-dialog-close { margin-left:auto; border:0; background:none; color:var(--text-dim); font-size:20px; cursor:pointer; }
.vm-dialog-body { padding:14px 16px; overflow:auto; display:grid; gap:10px; }
.vm-edit-label { font-size:10px; font-weight:600; letter-spacing:.6px; text-transform:uppercase; color:var(--text-dim); }
.vm-edit-textarea { width:100%; min-height:110px; resize:vertical; background:var(--bg-card); border:1px solid var(--border); color:var(--text); border-radius:6px; padding:10px; outline:none; font:11px/1.55 var(--mono); }
.vm-edit-textarea.raw { min-height:260px; }
.vm-edit-textarea:focus { border-color:var(--accent); }
.vm-btn { border:1px solid var(--border); background:var(--bg-card); color:var(--text); border-radius:6px; padding:8px 12px; font:12px var(--font); cursor:pointer; }
.vm-btn:hover { border-color:var(--accent); color:var(--text-bright); }
.vm-btn.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
.vm-btn.forge { background:#7c3aed; border-color:#7c3aed; color:#fff; }
.vm-btn.forge:hover { background:#6d28d9; border-color:#6d28d9; }
.vm-btn.forge:disabled { opacity:.5; cursor:not-allowed; }
.vm-forge-status { font-size:11px; margin-top:8px; padding:6px 10px; border-radius:6px; display:none; }
.vm-forge-status.loading { display:block; color:var(--text-dim); }
.vm-forge-status.success { display:block; color:#4ade80; background:rgba(74,222,128,.08); }
.vm-forge-status.error { display:block; color:#f87171; background:rgba(248,113,113,.08); }
.vm-edit-error { color:#f87171; font:11px var(--mono); margin-right:auto; }
@media (max-width: 860px) {
    .viewer-result { grid-template-columns:1fr; }
    .viewer-preview { position:static; }
}
</style>
<div class="viewer-content">
    <div class="drop-zone" id="viewerDrop">
        <div class="big"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="8" cy="10" r="1.5"/><path d="M21 15l-5-5L5 19"/></svg></div>
        <div>
            <div class="drop-main">Drop a PNG, JPG, or WEBP here, or click to select</div>
            <div class="drop-sub">Reads embedded generation metadata locally.</div>
        </div>
        <input type="file" id="viewerFile" accept=".png,.jpg,.jpeg,.webp">
    </div>
    <div id="viewerPreview" class="viewer-result">
        <div class="viewer-preview"><img id="viewerImg" src=""><div class="viewer-file-name" id="viewerFileName"></div></div>
        <div class="viewer-meta" id="viewerMeta"></div>
    </div>
</div>
<div id="metaEditModal" class="vm-modal">
    <div class="vm-dialog">
        <div class="vm-dialog-head"><div class="vm-dialog-title">Edit PNG metadata</div><button class="vm-dialog-close" id="editClose" type="button">&times;</button></div>
        <div class="vm-dialog-body">
            <label class="vm-edit-label" for="editPrompt">Prompt</label>
            <textarea id="editPrompt" class="vm-edit-textarea"></textarea>
            <label class="vm-edit-label" for="editNegativePrompt">Negative prompt</label>
            <textarea id="editNegativePrompt" class="vm-edit-textarea"></textarea>
            <button id="editApplyPrompt" class="vm-btn" type="button">Apply prompts to raw metadata</button>
            <label class="vm-edit-label" for="editRaw">Raw metadata JSON</label>
            <textarea id="editRaw" class="vm-edit-textarea raw" spellcheck="false"></textarea>
        </div>
        <div class="vm-dialog-foot"><span id="editError" class="vm-edit-error"></span><button id="editCancel" class="vm-btn" type="button">Cancel</button><button id="editDownload" class="vm-btn primary" type="button">Download edited PNG</button></div>
    </div>
</div>
<script>
(function() {
    var dropZone = document.getElementById('viewerDrop');
    var fileInput = document.getElementById('viewerFile');
    var currentFile = null;
    var currentData = null;
    var currentImageUrl = '';
    var copyPayloads = {};
    dropZone.addEventListener('click', function() { fileInput.click(); });
    dropZone.addEventListener('dragover', function(e) { e.preventDefault(); dropZone.classList.add('dragover'); });
    dropZone.addEventListener('dragleave', function() { dropZone.classList.remove('dragover'); });
    dropZone.addEventListener('drop', function(e) {
        e.preventDefault(); dropZone.classList.remove('dragover');
        if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
    });
    fileInput.addEventListener('change', function() { if (this.files[0]) handleFile(this.files[0]); });

    // Expose handleFile and renderMeta for external use (e.g. auto-load from ?path=)
    window.handleFile = handleFile;
    window.renderMeta = renderMeta;

    function handleFile(file) {
        if (!file) return;
        currentFile = file;
        currentData = null;
        copyPayloads = {};
        if (currentImageUrl) URL.revokeObjectURL(currentImageUrl);
        currentImageUrl = URL.createObjectURL(file);
        document.getElementById('viewerImg').src = currentImageUrl;
        document.getElementById('viewerFileName').textContent = file.name;
        document.getElementById('viewerPreview').classList.add('visible');
        var fd = new FormData(); fd.append('file', file);
        fetch('/api/viewer/analyze', { method:'POST', body:fd })
            .then(function(r) { return r.json(); })
            .then(function(data) { renderMeta(data, file); })
            .catch(function(e) {
                document.getElementById('viewerMeta').innerHTML =
                    '<div class="vm-empty">Error: ' + escHtml(e.message) + '</div>';
            });
    }

    function renderMeta(data, file) {
        var el = document.getElementById('viewerMeta');
        currentData = data || null;
        if (!data || data.error) {
            el.innerHTML = '<div class="vm-empty">' + escHtml(data && data.error ? data.error : 'No metadata found') + '</div>';
            return;
        }
        var h = '', parsed = data.parsed || {}, raw = data.raw_meta || {}, info = data.info || {};
        var isPng = /\.png$/i.test(file.name || '');
        copyPayloads = {};

        h += '<div class="vm-section"><div class="vm-title">File Info</div><div class="vm-file-info">';
        h += '<span class="label">Name</span><span class="value">' + escHtml(file.name) + '</span>';
        h += '<span class="label">Size</span><span class="value">' + formatSize(file.size) + '</span>';
        if (info.width) {
            h += '<span class="label">Dimensions</span><span class="value">' + info.width + ' \u00D7 ' + info.height + '</span>';
        }
        h += '</div></div>';

        if (parsed.prompt) {
            copyPayloads.prompt = parsed.prompt;
            h += '<div class="vm-section"><div class="vm-title">Prompt <span class="vm-action" data-copy-key="prompt">Copy</span></div>';
            h += '<div class="vm-prompt">' + escHtml(parsed.prompt) + '</div></div>';
        }
        if (parsed.negative_prompt) {
            copyPayloads.negative = parsed.negative_prompt;
            h += '<div class="vm-section"><div class="vm-title">Negative Prompt <span class="vm-action" data-copy-key="negative">Copy</span></div>';
            h += '<div class="vm-prompt negative">' + escHtml(parsed.negative_prompt) + '</div></div>';
        }
        if (data.civitai) {
            var c = data.civitai;
            h += '<div class="vm-section"><div class="vm-title">Model (Civitai)</div><div class="vm-prompt">';
            h += '<a href="https://civitai.com/models/' + c.id + '" target="_blank" style="color:var(--accent)">' + escHtml(c.model) + '</a>';
            if (c.version) h += ' \u00B7 ' + escHtml(c.version);
            if (c.base) h += ' \u00B7 ' + escHtml(c.base);
            if (c.creator) h += ' \u00B7 by ' + escHtml(c.creator);
            h += '</div></div>';
        }
        if (parsed.settings && Object.keys(parsed.settings).length) {
            h += '<div class="vm-section"><div class="vm-title">Settings</div><div class="vm-grid">';
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
                h += '<span class="vm-key">' + escHtml(k) + '</span><span class="vm-val">' + escHtml(parsed.settings[k]) + '</span>';
            });
            h += '</div></div>';
        }
        var rawText = '';
        for (var rk in raw) {
            var rv = raw[rk];
            if (rv !== null && typeof rv === 'object') rv = JSON.stringify(rv, null, 2);
            rawText += rk + ': ' + rv + '\n\n';
        }
        if (rawText) {
            copyPayloads.raw = rawText;
            h += '<div class="vm-section"><div class="vm-title"><span>Raw Metadata</span><span class="vm-actions"><span class="vm-action" data-copy-key="raw">Copy All</span>';
            if (isPng) h += '<span class="vm-action" data-action="edit">Edit</span>';
            h += '</span></div>';
            h += '<div class="vm-raw">' + escHtml(rawText) + '</div></div>';
        }
        if (parsed.prompt) {
            h += '<div class="vm-section"><div class="vm-title">Actions</div><div style="display:flex;gap:10px;align-items:center">';
            h += '<button class="vm-btn forge" data-action="forge-generate" type="button">Generate with Forge</button>';
            h += '<div class="vm-forge-status" id="forgeStatus"></div>';
            h += '</div></div>';
        }
        if (!parsed.prompt && !rawText) {
            if (isPng) {
                h += '<div class="vm-section"><div class="vm-title"><span>Raw Metadata</span><span class="vm-actions"><span class="vm-action" data-action="edit">Edit</span></span></div>';
                h += '<div class="vm-empty">No generation metadata found</div></div>';
            } else {
                h += '<div class="vm-empty">No generation metadata found</div>';
            }
        }
        el.innerHTML = h;
        el.querySelectorAll('[data-copy-key]').forEach(function(btn) {
            btn.addEventListener('click', function() { copyText(copyPayloads[this.getAttribute('data-copy-key')] || '', this); });
        });
        el.querySelectorAll('[data-action="edit"]').forEach(function(btn) {
            btn.addEventListener('click', openEditor);
        });
        el.querySelectorAll('[data-action="forge-generate"]').forEach(function(btn) {
            btn.addEventListener('click', function() { forgeGenerate(parsed); });
        });
    }

    function forgeGenerate(parsed) {
        var statusEl = document.getElementById('forgeStatus');
        var btn = document.querySelector('[data-action="forge-generate"]');
        if (!statusEl) return;
        btn.disabled = true;
        btn.textContent = 'Generating...';
        statusEl.className = 'vm-forge-status loading';
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
                statusEl.className = 'vm-forge-status error';
                statusEl.textContent = r.data.error || 'Generation failed';
            } else {
                statusEl.className = 'vm-forge-status success';
                var savedTo = r.data.saved_to ? 'Saved to ' + r.data.saved_to : 'Generation complete.';
                statusEl.textContent = savedTo;
                // Navigate to viewer with the saved image path
                if (r.data.saved_to) {
                    window.location.href = '/viewer?path=' + encodeURIComponent(r.data.saved_to);
                }
            }
        })
        .catch(function(e) {
            statusEl.className = 'vm-forge-status error';
            statusEl.textContent = 'Network error: ' + e.message;
        })
        .finally(function() {
            btn.disabled = false;
            btn.textContent = 'Generate with Forge';
        });
    }

    function openEditor() {
        if (!currentFile || !/\.png$/i.test(currentFile.name || '')) {
            alert('Metadata editing currently supports PNG files only.');
            return;
        }
        var raw = (currentData && currentData.raw_meta) || {};
        document.getElementById('editPrompt').value = (currentData && currentData.parsed && currentData.parsed.prompt) || '';
        document.getElementById('editNegativePrompt').value = (currentData && currentData.parsed && currentData.parsed.negative_prompt) || '';
        document.getElementById('editRaw').value = JSON.stringify(raw, null, 2);
        setEditError('');
        document.getElementById('metaEditModal').classList.add('open');
    }

    function closeEditor() {
        document.getElementById('metaEditModal').classList.remove('open');
    }

    function setEditError(msg) {
        document.getElementById('editError').textContent = msg || '';
    }

    function parseRawEditor() {
        var raw;
        try {
            raw = JSON.parse(document.getElementById('editRaw').value || '{}');
        } catch (e) {
            throw new Error('Raw metadata is not valid JSON: ' + e.message);
        }
        if (!raw || Array.isArray(raw) || typeof raw !== 'object') {
            throw new Error('Raw metadata must be a JSON object.');
        }
        return raw;
    }

    function replacePromptsInParameters(parameters, prompt, negativePrompt) {
        var text = String(parameters || '');
        var lines = text.split('\n');
        var negIdx = -1, settingsIdx = -1;
        for (var i = 0; i < lines.length; i++) {
            if (negIdx < 0 && /^Negative prompt:/i.test(lines[i])) negIdx = i;
            if (/^Steps:\s*/i.test(lines[i])) {
                settingsIdx = i;
                break;
            }
        }
        var out = [prompt || ''];
        if (negativePrompt) out.push('Negative prompt: ' + negativePrompt);
        if (settingsIdx >= 0) out = out.concat(lines.slice(settingsIdx));
        else if (negIdx < 0 && !text.trim() && !negativePrompt) out = [prompt || ''];
        return out.join('\n');
    }

    function applyPromptToRaw() {
        try {
            var raw = parseRawEditor();
            var prompt = document.getElementById('editPrompt').value || '';
            var negativePrompt = document.getElementById('editNegativePrompt').value || '';
            raw.parameters = replacePromptsInParameters(raw.parameters || '', prompt, negativePrompt);
            document.getElementById('editRaw').value = JSON.stringify(raw, null, 2);
            setEditError('');
        } catch (e) {
            setEditError(e.message);
        }
    }

    function downloadEdited() {
        var raw;
        try {
            raw = parseRawEditor();
        } catch (e) {
            setEditError(e.message);
            return;
        }
        setEditError('');
        var btn = document.getElementById('editDownload');
        var old = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Saving...';
        var fd = new FormData();
        fd.append('file', currentFile, currentFile.name);
        fd.append('raw_meta_json', new Blob([JSON.stringify(raw)], {type:'application/json'}), 'metadata.json');
        fetch('/api/viewer/rewrite', {method:'POST', body:fd})
            .then(function(r) {
                if (!r.ok) {
                    return r.json().catch(function(){return {};}).then(function(d){ throw new Error(d.error || ('HTTP ' + r.status)); });
                }
                return r.blob();
            })
            .then(function(blob) {
                var a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = (currentFile.name || 'image.png').replace(/\.png$/i, '') + '_metadata.png';
                a.click();
                setTimeout(function(){ URL.revokeObjectURL(a.href); }, 2000);
                closeEditor();
            })
            .catch(function(e) { setEditError(e.message); })
            .finally(function() {
                btn.disabled = false;
                btn.textContent = old;
            });
    }

    document.getElementById('editClose').addEventListener('click', closeEditor);
    document.getElementById('editCancel').addEventListener('click', closeEditor);
    document.getElementById('editApplyPrompt').addEventListener('click', applyPromptToRaw);
    document.getElementById('editDownload').addEventListener('click', downloadEdited);
    document.getElementById('metaEditModal').addEventListener('click', function(e) {
        if (e.target === this) closeEditor();
    });
})();

// Auto-load image from ?path= query parameter (e.g. after Forge generation)
(function() {
    var params = new URLSearchParams(window.location.search);
    var autoPath = params.get('path');
    if (!autoPath) return;

    // autoPath is already decoded by URLSearchParams; encode it once for the URL path
    var encoded = encodeURIComponent(autoPath);
    var imgSrc = '/viewer/file/' + encoded;

    // Fetch the image as a blob, then feed it into handleFile()
    fetch(imgSrc)
        .then(function(r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.blob();
        })
        .then(function(blob) {
            var name = autoPath.split('/').pop() || 'forge_gen.png';
            var file = new File([blob], name, {type: blob.type || 'image/png'});
            // Reuse the existing handleFile pipeline (exposed on window by the outer IIFE)
            console.log('[FORGE DEBUG] Auto-loading image from Forge:', autoPath);
            if (typeof window.handleFile === 'function') {
                window.handleFile(file);
            } else {
                // Fallback: directly set image and show preview
                console.warn('[FORGE DEBUG] handleFile not available, using fallback');
                document.getElementById('viewerImg').src = URL.createObjectURL(file);
                document.getElementById('viewerFileName').textContent = name;
                document.getElementById('viewerPreview').classList.add('visible');
                // Analyze metadata
                var fd = new FormData(); fd.append('file', file);
                fetch('/api/viewer/analyze', {method: 'POST', body: fd})
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        if (typeof window.renderMeta === 'function') window.renderMeta(data, file);
                    });
            }
        })
        .catch(function(e) {
            console.error('[FORGE DEBUG] Failed to load Forge output:', autoPath, e);
        });
})();
</script>
"""