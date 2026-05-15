# cnc_ui/logging_setup.py
"""
Centralised logging configuration for fabCNC.

Creates four rotating file handlers under <repo>/cnc_ui/logs/:

  app.log         — root logger (everything, human-readable)
  events.jsonl    — user/GUI/file events as one JSON object per line
  controller.jsonl— every serial TX/RX, job state change (JSON)
  toolpath.jsonl  — toolpath generation summaries + emitted gcode meta (JSON)

Public helpers:
  setup_logging()                  Initialise once at app start
  log_event(category, action, **)  Append a structured event line
  log_serial_tx(cmd, **)           Record a serial command sent
  log_serial_rx(line, **)          Record a serial response received
  log_toolpath(action, **)         Record a toolpath-generation event
  get_log_dir()                    Path to logs/ for bundling
  load_config()                    Read logging_config.json

The whole system is configured by ``logging_config.json`` (next to the
repo root, falls back to defaults). Edit that file to change log levels,
rotation, the upload destination, etc. — no SSH needed.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = REPO_DIR / "cnc_ui" / "logs"
CONFIG_PATH = REPO_DIR / "logging_config.json"

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "log_dir": str(DEFAULT_LOG_DIR),
    "console_level": "INFO",
    "file_level": "DEBUG",
    "max_file_size_mb": 10,
    "backup_count": 10,
    # Optional remote shipping of the rolling log bundle.
    "upload": {
        "enabled": False,
        "url": "",                  # HTTPS endpoint that accepts POST multipart
        "method": "POST",           # POST (multipart) or PUT (raw zip body)
        "interval_minutes": 60,     # 0 disables the periodic uploader
        "device_id": "",            # Defaults to hostname when blank
        "auth_header": "",          # e.g. "Bearer abc123" (optional)
        "include_gcode": True,
        "include_uploads": False,   # DXFs can be big; off by default
        "max_bundle_mb": 50,
    },
}


# ── Config loading ────────────────────────────────────────────────────────────
_config_cache: Optional[dict[str, Any]] = None


def load_config(force: bool = False) -> dict[str, Any]:
    """Load logging_config.json, deep-merged onto defaults."""
    global _config_cache
    if _config_cache is not None and not force:
        return _config_cache

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r") as f:
                user = json.load(f)
            _deep_merge(cfg, user)
        except Exception as e:
            print(f"[logging_setup] WARNING: could not read {CONFIG_PATH}: {e}")

    # Resolve relative log_dir against repo root
    log_dir = Path(cfg["log_dir"])
    if not log_dir.is_absolute():
        log_dir = REPO_DIR / log_dir
    cfg["log_dir"] = str(log_dir)

    if not cfg["upload"].get("device_id"):
        cfg["upload"]["device_id"] = socket.gethostname()

    _config_cache = cfg
    return cfg


def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def get_log_dir() -> Path:
    return Path(load_config()["log_dir"])


# ── Internal state ────────────────────────────────────────────────────────────
_initialised = False
_init_lock = threading.Lock()

event_logger: logging.Logger = logging.getLogger("fabcnc.events")
controller_logger: logging.Logger = logging.getLogger("fabcnc.controller_io")
toolpath_logger: logging.Logger = logging.getLogger("fabcnc.toolpath_io")


# ── JSON formatter for structured channels ────────────────────────────────────
class _JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
        }
        msg = record.getMessage()
        # Records emitted by log_event/log_serial_* attach `extra={"event": {...}}`
        ev = getattr(record, "event", None)
        if isinstance(ev, dict):
            payload.update(ev)
        else:
            payload["msg"] = msg
        return json.dumps(payload, default=str, ensure_ascii=False)


# ── Setup entry point ────────────────────────────────────────────────────────-
def setup_logging() -> dict[str, Any]:
    """Configure root + structured loggers. Idempotent."""
    global _initialised
    with _init_lock:
        if _initialised:
            return load_config()

        cfg = load_config(force=True)
        log_dir = Path(cfg["log_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)

        max_bytes = int(cfg["max_file_size_mb"]) * 1024 * 1024
        backups = int(cfg["backup_count"])

        # Root logger: human-readable, captures EVERYTHING from existing
        # `logger = logging.getLogger(__name__)` usage across the codebase.
        root = logging.getLogger()
        # Remove handlers that nicegui / basicConfig may have attached so we
        # don't get duplicate console lines.
        for h in list(root.handlers):
            root.removeHandler(h)

        root.setLevel(logging.DEBUG)

        human_fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        console = logging.StreamHandler()
        console.setLevel(getattr(logging, cfg["console_level"].upper(), logging.INFO))
        console.setFormatter(human_fmt)
        root.addHandler(console)

        app_handler = logging.handlers.RotatingFileHandler(
            log_dir / "app.log",
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
        )
        app_handler.setLevel(getattr(logging, cfg["file_level"].upper(), logging.DEBUG))
        app_handler.setFormatter(human_fmt)
        root.addHandler(app_handler)

        # Structured channels — independent of root output.
        def _attach_json(logger: logging.Logger, filename: str) -> None:
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
            for h in list(logger.handlers):
                logger.removeHandler(h)
            handler = logging.handlers.RotatingFileHandler(
                log_dir / filename,
                maxBytes=max_bytes,
                backupCount=backups,
                encoding="utf-8",
            )
            handler.setFormatter(_JsonLineFormatter())
            logger.addHandler(handler)

        _attach_json(event_logger, "events.jsonl")
        _attach_json(controller_logger, "controller.jsonl")
        _attach_json(toolpath_logger, "toolpath.jsonl")

        # Tag the session so uploaded bundles can be correlated.
        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        boot_event = {
            "category": "system",
            "action": "session_start",
            "session_id": session_id,
            "device_id": cfg["upload"]["device_id"],
            "pid": os.getpid(),
        }
        event_logger.info("session_start", extra={"event": boot_event})
        logging.getLogger(__name__).info(
            f"Logging initialised — dir={log_dir} session={session_id}"
        )

        _initialised = True
        return cfg


# ── Public helpers ────────────────────────────────────────────────────────────
def log_event(category: str, action: str, **details: Any) -> None:
    """Record a user / GUI / file event as a JSON line and a readable INFO line."""
    if not _initialised:
        setup_logging()
    payload = {"category": category, "action": action, **details}
    event_logger.info(action, extra={"event": payload})
    logging.getLogger("fabcnc.events").debug(
        f"{category}.{action} {details}" if details else f"{category}.{action}"
    )


def log_serial_tx(command: str, **details: Any) -> None:
    if not _initialised:
        setup_logging()
    payload = {"dir": "tx", "command": command, **details}
    controller_logger.info("tx", extra={"event": payload})


def log_serial_rx(line: str, **details: Any) -> None:
    if not _initialised:
        setup_logging()
    payload = {"dir": "rx", "line": line, **details}
    controller_logger.info("rx", extra={"event": payload})


def log_controller_event(action: str, **details: Any) -> None:
    """Job-level controller event (start/pause/resume/stop/complete/error)."""
    if not _initialised:
        setup_logging()
    payload = {"category": "controller", "action": action, **details}
    controller_logger.info(action, extra={"event": payload})


def log_toolpath(action: str, **details: Any) -> None:
    if not _initialised:
        setup_logging()
    payload = {"category": "toolpath", "action": action, **details}
    toolpath_logger.info(action, extra={"event": payload})


__all__ = [
    "setup_logging",
    "load_config",
    "get_log_dir",
    "log_event",
    "log_serial_tx",
    "log_serial_rx",
    "log_controller_event",
    "log_toolpath",
    "REPO_DIR",
    "CONFIG_PATH",
]
