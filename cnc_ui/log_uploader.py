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


def _get_device_id() -> str:
    """Return a stable, unique identifier for this device.

    Priority:
    1. logging_config.json upload.device_id (user-set)
    2. /sys/firmware/devicetree/base/serial-number  (Pi 4/5, unique per board)
    3. /proc/cpuinfo Serial line (older Pi)
    4. Last 6 hex digits of the primary MAC address
    5. hostname
    """
    # Read device_id directly from the config file to avoid load_config() filling
    # in the hostname when device_id is blank.
    try:
        import json as _json
        raw = _json.loads(logging_setup.CONFIG_PATH.read_text())
        cfg_id = (raw.get("upload", {}).get("device_id") or "").strip()
    except Exception:
        cfg_id = ""
    if cfg_id:
        return cfg_id

    try:
        serial = open("/sys/firmware/devicetree/base/serial-number").read().strip().rstrip("\x00").lstrip("0")
        if serial:
            return f"pi-{serial[-8:]}"
    except OSError:
        pass

    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Serial"):
                    serial = line.split(":")[-1].strip().lstrip("0")
                    if serial:
                        return f"pi-{serial[-8:]}"
    except OSError:
        pass

    try:
        import uuid as _uuid
        mac = f"{_uuid.getnode():012x}"
        return f"pi-{mac[-6:]}"
    except Exception:
        pass

    return socket.gethostname()

_uploader_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_state_lock = threading.Lock()

# Set to True by notify_job_run(); the periodic uploader only fires when True.
_job_run_since_last_periodic: bool = False


def notify_job_run() -> None:
    """Call this when a job starts or completes to allow the next periodic upload."""
    global _job_run_since_last_periodic
    _job_run_since_last_periodic = True


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


# ── System health snapshot ────────────────────────────────────────────────────
def _run(cmd: list[str], timeout: int = 5) -> str:
    """Run a shell command and return stdout+stderr, or an error note on failure."""
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = r.stdout.strip()
        if r.returncode != 0 and r.stderr.strip():
            out = (out + "\n" + r.stderr.strip()).strip()
        return out or "(no output)"
    except FileNotFoundError:
        return f"(command not found: {cmd[0]})"
    except subprocess.TimeoutExpired:
        return f"(timed out after {timeout}s)"
    except Exception as e:
        return f"(error: {e})"


def _decode_throttled(hex_val: str) -> list[str]:
    """Decode vcgencmd get_throttled bitmask into human-readable flags."""
    try:
        val = int(hex_val, 16)
    except ValueError:
        return []
    flags = []
    bits = {
        0:  "under-voltage NOW",
        1:  "arm-freq capped NOW",
        2:  "throttled NOW",
        3:  "soft-temp limit NOW",
        16: "under-voltage occurred",
        17: "arm-freq capping occurred",
        18: "throttling occurred",
        19: "soft-temp limit occurred",
    }
    for bit, label in bits.items():
        if val & (1 << bit):
            flags.append(label)
    return flags


def collect_system_info() -> dict:
    """
    Collect a point-in-time snapshot of Pi system health.
    Returns a dict suitable for structured logging and/or inclusion in a bundle.
    Safe to call on non-Pi platforms — commands that don't exist return a note.
    """
    # vcgencmd get_throttled — sticky bits survive reboots until cleared
    throttled_raw = _run(["vcgencmd", "get_throttled"])
    throttled_hex = ""
    throttled_flags: list[str] = []
    if throttled_raw.startswith("throttled="):
        throttled_hex = throttled_raw.split("=", 1)[1].strip()
        throttled_flags = _decode_throttled(throttled_hex)

    # dmesg: run without -n (that sets console level, not line count), tail in Python
    dmesg_out = _run(["dmesg", "--color=never", "-T", "--level=err,warn,info"], timeout=10)
    dmesg_tail = "\n".join(dmesg_out.splitlines()[-200:])

    # journalctl: last 500 lines from current boot
    journal_tail = _run(["journalctl", "-b", "--no-pager", "-n", "500", "--output=short-iso"],
                        timeout=10)

    return {
        "throttled_raw":   throttled_raw,
        "throttled_hex":   throttled_hex,
        "throttled_flags": throttled_flags,
        "temperature":     _run(["vcgencmd", "measure_temp"]),
        "voltage_core":    _run(["vcgencmd", "measure_volts", "core"]),
        "voltage_sdram":   _run(["vcgencmd", "measure_volts", "sdram_c"]),
        "uname":           _run(["uname", "-a"]),
        "uptime":          _run(["uptime"]),
        "free":            _run(["free", "-h"]),
        "df":              _run(["df", "-h", "/"]),
        "lsusb":           _run(["lsusb"]),
        "dmesg_tail":      dmesg_tail,
        "journal_tail":    journal_tail,
    }


def _format_system_info(info: dict) -> str:
    """Format the collect_system_info() dict as a human-readable text block."""
    lines = [
        "=== fabCNC system snapshot ===",
        f"uname       : {info.get('uname', 'n/a')}",
        f"uptime      : {info.get('uptime', 'n/a')}",
        f"free memory : {info.get('free', 'n/a')}",
        f"disk (/)    : {info.get('df', 'n/a')}",
        "",
        "--- Raspberry Pi health ---",
        f"throttled   : {info.get('throttled_raw', 'n/a')}",
    ]
    flags = info.get("throttled_flags", [])
    if flags:
        for f in flags:
            lines.append(f"              ⚠ {f}")
    else:
        lines.append("              (no throttle/undervoltage events)")
    lines += [
        f"temperature : {info.get('temperature', 'n/a')}",
        f"voltage core: {info.get('voltage_core', 'n/a')}",
        f"voltage sdram:{info.get('voltage_sdram', 'n/a')}",
        "",
        "--- USB devices ---",
        info.get("lsusb", "n/a"),
        "",
        "--- dmesg (last 200 err/warn/info lines) ---",
        info.get("dmesg_tail", "n/a"),
        "",
        "--- journalctl (last 500 lines, current boot) ---",
        info.get("journal_tail", "n/a"),
    ]
    return "\n".join(lines)


def log_system_snapshot(trigger: str = "manual") -> None:
    """
    Collect system health info, write it to controller.jsonl, and log a
    human-readable summary to app.log.  Call this on disconnect or on demand.
    """
    info = collect_system_info()
    flags = info.get("throttled_flags", [])
    logging_setup.log_controller_event(
        "system_snapshot",
        trigger=trigger,
        throttled_hex=info.get("throttled_hex"),
        throttled_flags=flags,
        temperature=info.get("temperature"),
        voltage_core=info.get("voltage_core"),
        voltage_sdram=info.get("voltage_sdram"),
        uptime=info.get("uptime"),
        lsusb=info.get("lsusb"),
    )
    if flags:
        logger.warning(f"Pi health ({trigger}): throttle flags set — {', '.join(flags)}")
    else:
        logger.info(f"Pi health ({trigger}): no throttle/undervoltage flags")


# ── Bundle building ───────────────────────────────────────────────────────────
def build_bundle(*, full: bool = False, trigger: str = "retry") -> tuple[bytes, str, dict]:
    """
    Create an in-memory zip of logs + (optionally) recent artefacts.

    *trigger* is embedded in the filename so bundles are easy to distinguish
    in Discord/storage. Common values: ``"auto"``, ``"manual"``,
    ``"disconnect"``, ``"job_complete"``, ``"job_abort"``.

    Returns ``(zip_bytes, filename, manifest)``. When ``full`` is False the
    bundle only contains the log bytes appended since the last upload (the
    state file tracks offsets per filename).
    """
    cfg = logging_setup.load_config()
    log_dir = logging_setup.get_log_dir()
    upload_cfg = cfg["upload"]
    device_id = _get_device_id()

    state = _read_state() if not full else {}
    offsets: dict[str, int] = state.get("offsets", {})

    manifest = {
        "device_id": device_id,
        "bundle_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "full": full,
        "trigger": trigger,
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

        # System health snapshot (always included — invaluable for debugging)
        try:
            sys_info = collect_system_info()
            sys_text = _format_system_info(sys_info)
            zf.writestr("system_info.txt", sys_text)
            manifest["files"].append({"name": "system_info.txt", "bytes": len(sys_text)})
            manifest["throttled_flags"] = sys_info.get("throttled_flags", [])
            # Raw system logs as separate files for easy viewing
            dmesg_full = _run(["dmesg", "--color=never", "-T"], timeout=10)
            zf.writestr("system/dmesg.txt", dmesg_full)
            manifest["files"].append({"name": "system/dmesg.txt", "bytes": len(dmesg_full)})
            journal_full = _run(
                ["journalctl", "-b", "--no-pager", "--output=short-iso"], timeout=15
            )
            zf.writestr("system/journal.txt", journal_full)
            manifest["files"].append({"name": "system/journal.txt", "bytes": len(journal_full)})
        except Exception as e:
            logger.warning(f"Could not collect system info for bundle: {e}")

        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    data = buf.getvalue()
    if len(data) > max_bytes:
        logger.warning(
            f"Log bundle is {len(data)/1e6:.1f}MB which exceeds "
            f"max_bundle_mb={upload_cfg.get('max_bundle_mb')}; sending anyway."
        )

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"fabcnc_{device_id}_{ts}_{trigger}.zip"
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

    device_id = _get_device_id()
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
    headers = {"X-Device-Id": _get_device_id()}
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
def upload_now(full: bool = False, trigger: str = "retry") -> dict:
    """Build and upload a bundle synchronously. Returns a status dict."""
    cfg = logging_setup.load_config()["upload"]
    if not cfg.get("url"):
        return {"ok": False, "error": "upload.url not configured"}

    with _state_lock:
        zip_bytes, filename, manifest = build_bundle(full=full, trigger=trigger)
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
        global _job_run_since_last_periodic
        if _job_run_since_last_periodic:
            _job_run_since_last_periodic = False
            try:
                upload_now(full=False, trigger="retry")
            except Exception as e:
                logger.exception(f"Uploader iteration crashed: {e}")
        else:
            logger.debug("Periodic upload skipped — no job run since last upload")
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
