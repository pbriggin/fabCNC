# cnc_ui/log_uploader.py
"""
Periodic + on-demand log uploader for fabCNC.

Bundles the contents of <log_dir>/ (and optionally recent gcode + canvas
saves) into a single zip and POSTs it to the URL configured in
``logging_config.json``. Designed for headless Pis where SSH is unavailable —
the device pushes its logs to a webhook you control.

Configure in logging_config.json:

    "upload": {
      "enabled": true,
      "url": "https://example.com/fabcnc-logs",
      "method": "POST",             # POST (multipart) | PUT (raw zip body)
      "interval_minutes": 60,
      "device_id": "shop-pi",       # blank = hostname
      "auth_header": "Bearer xyz",  # optional
      "include_gcode": true,
      "include_uploads": false,
      "max_bundle_mb": 50
    }

Compatible endpoints out of the box:
  * Any HTTPS service accepting a multipart "file" field (most webhooks,
    requestbin.com, ngrok, FastAPI receiver, n8n, Zapier "Catch Hook").
  * A pre-signed S3 / GCS PUT URL — set method=PUT.
"""

from __future__ import annotations

import io
import json
import logging
import socket
import ssl
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from urllib import error as _urlerror
from pathlib import Path
from typing import Optional
from urllib import request as _urlrequest

import logging_setup

logger = logging.getLogger(__name__)

# Build an SSL context that works on macOS (no system keychain) and Pi alike.
def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    return ssl.create_default_context()

_uploader_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_state_lock = threading.Lock()


# ── State file (tracks last uploaded bundle, byte offsets per log) ────────────
def _state_path() -> Path:
    return logging_setup.get_log_dir() / ".uploader_state.json"


def _read_state() -> dict:
    p = _state_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def _write_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


# ── Bundle building ───────────────────────────────────────────────────────────
def build_bundle(*, full: bool = False) -> tuple[bytes, str, dict]:
    """
    Create an in-memory zip of logs + (optionally) recent artefacts.

    Returns ``(zip_bytes, filename, manifest)``. When ``full`` is False the
    bundle only contains the log bytes appended since the last upload (the
    state file tracks offsets per filename).
    """
    cfg = logging_setup.load_config()
    log_dir = logging_setup.get_log_dir()
    upload_cfg = cfg["upload"]
    device_id = upload_cfg.get("device_id") or socket.gethostname()

    state = _read_state() if not full else {}
    offsets: dict[str, int] = state.get("offsets", {})

    manifest = {
        "device_id": device_id,
        "bundle_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "full": full,
        "hostname": socket.gethostname(),
        "files": [],
    }

    buf = io.BytesIO()
    max_bytes = int(upload_cfg.get("max_bundle_mb", 50)) * 1024 * 1024
    new_offsets = dict(offsets)
    total_log_bytes = 0

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Logs (incremental tail since last upload)
        if log_dir.exists():
            for f in sorted(log_dir.glob("*.log")) + sorted(log_dir.glob("*.jsonl")):
                try:
                    size = f.stat().st_size
                except OSError:
                    continue
                start = offsets.get(f.name, 0) if not full else 0
                if start > size:
                    # File rotated/truncated since we last looked.
                    start = 0
                if start >= size:
                    continue
                with f.open("rb") as fh:
                    fh.seek(start)
                    data = fh.read()
                arcname = f"logs/{f.name}"
                if start > 0:
                    arcname = f"logs/{f.name}.from{start}"
                zf.writestr(arcname, data)
                new_offsets[f.name] = size
                total_log_bytes += len(data)
                manifest["files"].append({"name": arcname, "bytes": len(data)})

            # Include rotated backups (.1, .2, …) once each (size as key).
            seen_backups = set(state.get("backups", []))
            backup_record = list(seen_backups)
            for f in sorted(log_dir.glob("*.log.*")) + sorted(log_dir.glob("*.jsonl.*")):
                key = f"{f.name}:{f.stat().st_size}"
                if key in seen_backups and not full:
                    continue
                zf.write(f, f"logs/backups/{f.name}")
                manifest["files"].append({"name": f"logs/backups/{f.name}", "bytes": f.stat().st_size})
                backup_record.append(key)
            state["backups"] = backup_record

        # Optional gcode + canvas saves
        uploads_root = Path(__file__).resolve().parent / "uploads"
        if upload_cfg.get("include_gcode", True):
            gdir = uploads_root / "gcode_output"
            if gdir.exists():
                for f in sorted(
                    gdir.glob("*.gcode"),
                    key=lambda x: x.stat().st_mtime,
                    reverse=True,
                )[:30]:
                    zf.write(f, f"gcode/{f.name}")
        if upload_cfg.get("include_uploads", False) and uploads_root.exists():
            for f in sorted(uploads_root.glob("*.dxf")):
                zf.write(f, f"uploads/{f.name}")
            for f in sorted(uploads_root.glob("*.json")):
                zf.write(f, f"canvases/{f.name}")

        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    data = buf.getvalue()
    if len(data) > max_bytes:
        logger.warning(
            f"Log bundle is {len(data)/1e6:.1f}MB which exceeds "
            f"max_bundle_mb={upload_cfg.get('max_bundle_mb')}; sending anyway."
        )

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"fabcnc_{device_id}_{ts}.zip"
    state["offsets"] = new_offsets
    state["last_bundle"] = {
        "filename": filename,
        "bundle_id": manifest["bundle_id"],
        "bytes": len(data),
        "ts": manifest["created_at"],
    }
    # State is only persisted on a successful upload (caller does that).
    manifest["_state"] = state
    return data, filename, manifest


# ── Upload transport ──────────────────────────────────────────────────────────
def _do_discord_upload(zip_bytes: bytes, filename: str, manifest: dict) -> dict:
    """Upload a log zip as a Discord file attachment via a webhook URL."""
    cfg = logging_setup.load_config()["upload"]
    url = cfg["url"]
    if "?" not in url:
        url += "?wait=true"

    device_id = cfg.get("device_id") or socket.gethostname()
    ts = manifest.get("created_at", "")[:19].replace("T", " ")
    content = (
        f"\U0001f4cb **fabCNC log bundle**\n"
        f"`device:` {device_id}   `time:` {ts} UTC   `size:` {len(zip_bytes)//1024} KB"
    )
    payload_json = json.dumps({"content": content}).encode("utf-8")

    boundary = f"WebKitFormBoundary{uuid.uuid4().hex}"

    # Build each multipart part explicitly to avoid implicit-concat ambiguity.
    part_json = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="payload_json"\r\n'
        f"Content-Type: application/json\r\n\r\n"
    ).encode("utf-8") + payload_json

    part_file = (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n'
        f"Content-Type: application/zip\r\n\r\n"
    ).encode("utf-8") + zip_bytes

    body = part_json + part_file + f"\r\n--{boundary}--\r\n".encode("utf-8")

    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
               "User-Agent": "DiscordBot (https://github.com/pbriggin/fabCNC, 1.0)"}
    req = _urlrequest.Request(url, data=body, method="POST", headers=headers)
    started = time.time()
    try:
        with _urlrequest.urlopen(req, timeout=60, context=_ssl_context()) as resp:
            status = resp.status
            resp_body = resp.read(2048).decode("utf-8", errors="replace")
    except _urlerror.HTTPError as exc:
        discord_error = exc.read(2048).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from Discord: {discord_error}") from exc

    return {
        "status": status,
        "duration_s": round(time.time() - started, 2),
        "response": resp_body[:512],
        "bytes": len(zip_bytes),
        "filename": filename,
    }


def _do_upload(zip_bytes: bytes, filename: str, manifest: dict) -> dict:
    cfg = logging_setup.load_config()["upload"]
    url = cfg["url"]
    if not url:
        raise RuntimeError("upload.url not configured")

    method = cfg.get("method", "POST").upper()

    # Discord webhook — use the dedicated formatter.
    if method == "DISCORD" or "discord.com/api/webhooks" in url:
        return _do_discord_upload(zip_bytes, filename, manifest)

    auth = cfg.get("auth_header", "")
    headers = {"X-Device-Id": cfg.get("device_id", socket.gethostname())}
    if auth:
        headers["Authorization"] = auth

    if method == "PUT":
        body = zip_bytes
        headers["Content-Type"] = "application/zip"
    else:  # POST multipart/form-data
        boundary = f"----fabcnc{uuid.uuid4().hex}"
        manifest_for_wire = {k: v for k, v in manifest.items() if not k.startswith("_")}
        parts = [
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="manifest"\r\n'
            f"Content-Type: application/json\r\n\r\n"
            f"{json.dumps(manifest_for_wire)}\r\n".encode("utf-8"),
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/zip\r\n\r\n".encode("utf-8"),
            zip_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
        body = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

    req = _urlrequest.Request(url, data=body, method=method, headers=headers)
    started = time.time()
    with _urlrequest.urlopen(req, timeout=60, context=_ssl_context()) as resp:
        status = resp.status
        resp_body = resp.read(2048).decode("utf-8", errors="replace")
    duration = time.time() - started
    return {
        "status": status,
        "duration_s": round(duration, 2),
        "response": resp_body[:512],
        "bytes": len(zip_bytes),
        "filename": filename,
    }


# ── Public API ────────────────────────────────────────────────────────────────
def upload_now(full: bool = False) -> dict:
    """Build and upload a bundle synchronously. Returns a status dict."""
    cfg = logging_setup.load_config()["upload"]
    if not cfg.get("url"):
        return {"ok": False, "error": "upload.url not configured"}

    with _state_lock:
        zip_bytes, filename, manifest = build_bundle(full=full)
        try:
            result = _do_upload(zip_bytes, filename, manifest)
            _write_state(manifest.pop("_state"))
            logging_setup.log_event(
                "system",
                "log_upload",
                ok=True,
                filename=filename,
                bytes=result["bytes"],
                status=result["status"],
                duration_s=result["duration_s"],
            )
            logger.info(
                f"Uploaded {filename} ({result['bytes']/1024:.1f} KB) "
                f"-> HTTP {result['status']} in {result['duration_s']}s"
            )
            return {"ok": True, **result}
        except Exception as e:
            logger.error(f"Log upload failed: {e}")
            logging_setup.log_event(
                "system", "log_upload", ok=False, error=str(e), filename=filename
            )
            return {"ok": False, "error": str(e), "filename": filename, "bytes": len(zip_bytes)}


def _uploader_loop(interval_s: float) -> None:
    logger.info(f"Log uploader thread started (interval={interval_s/60:.1f} min)")
    # Stagger first run slightly so the app finishes booting first.
    if _stop_event.wait(min(30.0, interval_s)):
        return
    while not _stop_event.is_set():
        try:
            upload_now(full=False)
        except Exception as e:
            logger.exception(f"Uploader iteration crashed: {e}")
        if _stop_event.wait(interval_s):
            break
    logger.info("Log uploader thread stopped")


def start_uploader() -> None:
    """Start the background uploader if enabled in config."""
    global _uploader_thread
    cfg = logging_setup.load_config()["upload"]
    if not cfg.get("enabled"):
        logger.info("Log uploader disabled (upload.enabled=false)")
        return
    if not cfg.get("url"):
        logger.warning("Log uploader enabled but upload.url is empty — not starting")
        return
    interval_min = float(cfg.get("interval_minutes", 60) or 0)
    if interval_min <= 0:
        logger.info("Log uploader periodic interval is 0 — manual uploads only")
        return
    if _uploader_thread and _uploader_thread.is_alive():
        return
    _stop_event.clear()
    _uploader_thread = threading.Thread(
        target=_uploader_loop, args=(interval_min * 60,), daemon=True, name="log-uploader"
    )
    _uploader_thread.start()


def stop_uploader() -> None:
    _stop_event.set()


def restart_uploader() -> None:
    """Stop and re-launch the uploader thread (picks up new config)."""
    global _uploader_thread
    stop_uploader()
    if _uploader_thread and _uploader_thread.is_alive():
        _uploader_thread.join(timeout=5.0)
    _uploader_thread = None
    # Force a fresh config read on next call.
    logging_setup.load_config(force=True)
    start_uploader()
