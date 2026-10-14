# main.py
"""
NiceGUI-based CNC web controller UI.
Provides manual jogging, homing, file upload, and job execution controls.
"""

# main.py
"""
NiceGUI-based CNC web controller UI.
Provides manual jogging, homing, file upload, and job execution controls.
"""

# Initialise structured logging (file rotation + JSONL channels) FIRST so the
# controller's serial I/O and every other module use the configured handlers
# from the moment they're imported.
from logging_setup import (
    setup_logging,
    log_event,
    log_toolpath,
    get_log_dir,
    APP_VERSION,
    load_config as load_logging_config,
)
import log_uploader
from auth import set_password, verify_password, verify_recovery_code

setup_logging()

from nicegui import ui, app
from cnc.state import machine_state
from cnc.controller import cnc_controller
from cnc.controller import parse_homing_sensitivity_response
from cnc.files import file_manager
from pathlib import Path
from fastapi import Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import matplotlib.pyplot as plt
from dxf_processing.dxf_processor import DXFProcessor
from toolpath_planning.toolpath_generator import ToolpathGenerator
from toolpath_planning.gcode_visualizer import GCodeVisualizer
from toolpath_planning.overlap_detector import find_overlapping_pairs
import logging
import socket
import subprocess
import os
import math
import json
import asyncio
import hashlib
import hmac
import io
import zipfile
import re
import fcntl
import termios
import struct
import secrets
import time
from collections import defaultdict, deque
from collections.abc import MutableMapping
from copy import deepcopy
from datetime import datetime

logger = logging.getLogger(__name__)

_APP_PASSWORD = os.environ.get('FABCNC_APP_PASSWORD', '2026')
_STORAGE_SECRET = os.environ.get('FABCNC_STORAGE_SECRET') or secrets.token_urlsafe(32)
_BOOT_TOKEN = secrets.token_hex(16)
_RECOVERY_FAILURES = defaultdict(deque)
_RECOVERY_LIMIT = 5
_RECOVERY_WINDOW_SECONDS = 15 * 60


def _parse_homing_sensitivity_value(value, axis: str) -> int:
    """Validate a TMC5160 StallGuard threshold entered in the settings UI."""
    if value is None or isinstance(value, bool):
        raise ValueError(f'Enter a homing sensitivity for {axis}.')
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{axis} homing sensitivity must be an integer.') from exc
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise ValueError(f'{axis} homing sensitivity must be an integer.')
    parsed = int(numeric)
    if not -64 <= parsed <= 63:
        raise ValueError(f'{axis} homing sensitivity must be between -64 and 63.')
    return parsed


def _recovery_client_key() -> str:
    request = ui.context.client.request
    return request.client.host if request.client else "unknown"


def _recovery_rate_limited(client_key: str) -> bool:
    now = time.monotonic()
    failures = _RECOVERY_FAILURES[client_key]
    while failures and now - failures[0] >= _RECOVERY_WINDOW_SECONDS:
        failures.popleft()
    return len(failures) >= _RECOVERY_LIMIT


def _request_authenticated(request: Request) -> bool:
    """Validate an HTTP route against the same signed session as the UI."""
    session_id = request.session.get('id')
    if not session_id:
        return False
    user_storage = app.storage._users.get(session_id)  # NiceGUI session middleware owns this mapping.
    return bool(
        user_storage
        and user_storage.get('authenticated')
        and hmac.compare_digest(str(user_storage.get('boot_token', '')), _BOOT_TOKEN)
    )


def _unauthorized() -> JSONResponse:
    return JSONResponse({'error': 'Authentication required'}, status_code=401)


class _ClientDict(MutableMapping):
    """Dict-compatible view backed by the authenticated user's server storage."""

    def __init__(self, key: str, defaults: dict | None = None):
        self.key = key
        self.defaults = defaults or {}

    def _data(self) -> dict:
        storage = app.storage.user
        if self.key not in storage:
            storage[self.key] = deepcopy(self.defaults)
        return storage[self.key]

    def __getitem__(self, key):
        return self._data()[key]

    def __setitem__(self, key, value):
        data = dict(self._data())
        data[key] = value
        app.storage.user[self.key] = data

    def __delitem__(self, key):
        data = dict(self._data())
        del data[key]
        app.storage.user[self.key] = data

    def __iter__(self):
        return iter(self._data())

    def __len__(self):
        return len(self._data())

    def clear(self):
        app.storage.user[self.key] = {}

    def copy(self):
        return deepcopy(dict(self._data()))

    def update(self, *args, **kwargs):
        data = dict(self._data())
        data.update(*args, **kwargs)
        app.storage.user[self.key] = data

# Repository root (one level above cnc_ui/)
REPO_DIR = Path(__file__).parent.parent
_TOOLPATH_CANVAS_PATH = Path(__file__).parent / 'static' / 'toolpath_canvas.js'
# Cache-bust the browser asset whenever its contents change. APP_VERSION is not
# bumped for every frontend-only commit, which can otherwise leave a new Python
# toolbar calling functions from an older cached canvas script.
TOOLPATH_CANVAS_ASSET_VERSION = hashlib.sha256(
    _TOOLPATH_CANVAS_PATH.read_bytes()
).hexdigest()[:12]

# Update check state
update_state = {'available': False, 'acknowledged': False}

# Set to True by any control/move command when the controller isn't connected;
# polled by the per-page UI timer to (re)open the disconnect alert popup.
disconnect_alert_state = {'requested': False}

# User-facing copy and dialog styling live here so Python dialogs and transient
# messages use the same visual language. Technical details still belong in logs.
_DIALOG_CARD_STYLE = (
    'width: 420px; max-width: 95vw; background: var(--md-bg-card, #333333); '
    'padding: var(--space-5, 20px); border-radius: var(--radius-lg, 10px);'
)


def _safe_error_message(action: str) -> str:
    """Return stable operator copy; callers should log the underlying error."""
    return f'{action} Try again. If the problem continues, send the logs to support.'


def _cloudflare_request_details(headers) -> dict:
    """Return trusted Cloudflare request markers from an incoming header mapping."""
    normalized = {str(key).lower(): str(value).strip() for key, value in headers.items()}
    cf_ip = normalized.get('cf-connecting-ip', '')
    cf_ray = normalized.get('cf-ray', '')
    access_email = normalized.get('cf-access-authenticated-user-email', '')
    return {
        'via_cloudflare': bool(cf_ip or cf_ray or access_email),
        'cloudflare_ip': cf_ip,
        'cloudflare_ray': cf_ray,
        'access_email': access_email,
    }


def _current_cloudflare_request_details() -> dict:
    """Inspect the original HTTP request associated with the current NiceGUI client."""
    try:
        return _cloudflare_request_details(ui.context.client.request.headers)
    except (AttributeError, RuntimeError):
        return _cloudflare_request_details({})


def _deny_cloudflare_control() -> bool:
    """Block machine/system actions from Cloudflare-proxied browser sessions."""
    details = _current_cloudflare_request_details()
    if not details['via_cloudflare']:
        return False
    log_event(
        'security', 'cloudflare_control_blocked',
        cloudflare_ip=details['cloudflare_ip'],
        access_email=details['access_email'],
    )
    ui.notify('Controls are disabled for Cloudflare remote sessions.', type='warning')
    return True


def _require_controller_connected() -> bool:
    """Return True if the controller is connected; otherwise flag the disconnect alert."""
    if _deny_cloudflare_control():
        return False
    if cnc_controller.connected:
        return True
    disconnect_alert_state['requested'] = True
    return False


# Mount static files directory
app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')


# === SOCKET.IO LIFECYCLE LOGGING =====================================
# Log every NiceGUI client connect/disconnect so we can correlate browser
# socket drops with nest crashes. NiceGUI exposes app.on_connect /
# app.on_disconnect; these fire on the underlying Socket.IO session.
def _on_client_connect(client):
    try:
        cid = getattr(client, 'id', '?')
        details = _cloudflare_request_details(client.request.headers)
        logger.info(
            "[WS] client connect id=%s via_cloudflare=%s access_email=%s cf_ip=%s",
            cid, details['via_cloudflare'], details['access_email'], details['cloudflare_ip'],
        )
        log_event('system', 'ws_connect', client_id=str(cid), **details)
    except Exception as exc:
        logger.warning(f"[WS] connect logging failed: {exc}")

def _on_client_disconnect(client):
    try:
        cid = getattr(client, 'id', '?')
        details = _cloudflare_request_details(client.request.headers)
        logger.warning(
            "[WS] client DISCONNECT id=%s via_cloudflare=%s access_email=%s cf_ip=%s",
            cid, details['via_cloudflare'], details['access_email'], details['cloudflare_ip'],
        )
        log_event('system', 'ws_disconnect', client_id=str(cid), **details)
    except Exception as exc:
        logger.warning(f"[WS] disconnect logging failed: {exc}")

try:
    app.on_connect(_on_client_connect)
    app.on_disconnect(_on_client_disconnect)
    logger.info("[WS] lifecycle hooks installed")
except Exception as exc:
    logger.warning(f"Could not install WS lifecycle hooks: {exc}")


@app.post('/client-log')
async def client_log(request: Request):
    """Receive a batch of client-side diagnostic events buffered in localStorage.

    Used to recover logs after the browser drops its WebSocket (so emitEvent
    calls were lost) and reloads the page. The JS posts whatever it has in
    localStorage on every page load.
    """
    if not _request_authenticated(request):
        return _unauthorized()
    try:
        payload = await request.json()
        events = payload.get('events') or []
        reason = payload.get('reason', 'unknown')
        logger.warning(f"[CLIENT-LOG] flushing {len(events)} buffered events reason={reason}")
        for ev in events:
            try:
                cp = ev.get('checkpoint', '?') if isinstance(ev, dict) else '?'
                logger.info(f"[NEST JS RECOVERED] {cp}: {ev}")
                if isinstance(ev, dict):
                    log_event('transform', 'nest_diagnostic_recovered', **ev)
            except Exception:
                pass
        return {'status': 'ok', 'received': len(events)}
    except Exception as exc:
        logger.exception(f"[CLIENT-LOG] failed: {exc}")
        return {'status': 'error', 'message': str(exc)}


@app.get('/debug-bundle')
def debug_bundle(request: Request):
    """Generate and stream a zip of all uploads, canvas saves, recent gcode AND structured logs."""
    if not _request_authenticated(request):
        return _unauthorized()
    upload_dir = file_manager.upload_dir
    gcode_dir = upload_dir / 'gcode_output'
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(upload_dir.glob('*.dxf')):
            zf.write(f, f'uploads/{f.name}')
        for f in sorted(upload_dir.glob('*.json')):
            zf.write(f, f'canvases/{f.name}')
        if gcode_dir.exists():
            for f in sorted(gcode_dir.glob('*.gcode'), key=lambda x: x.stat().st_mtime, reverse=True)[:30]:
                zf.write(f, f'toolpaths/{f.name}')
        # Include structured logs so a single download captures everything.
        log_dir = get_log_dir()
        if log_dir.exists():
            for f in sorted(log_dir.glob('*.log')) + sorted(log_dir.glob('*.jsonl')):
                zf.write(f, f'logs/{f.name}')
            for f in sorted(log_dir.glob('*.log.*')) + sorted(log_dir.glob('*.jsonl.*')):
                zf.write(f, f'logs/backups/{f.name}')
    buf.seek(0)

    filename = f'fabcnc_logs_{timestamp}.zip'
    log_event('system', 'debug_bundle_downloaded', filename=filename, bytes=buf.getbuffer().nbytes)
    return StreamingResponse(
        buf,
        media_type='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@app.get('/canvas/download/{filename}')
def canvas_download(filename: str, request: Request):
    """Download a saved canvas JSON file to the browser."""
    if not _request_authenticated(request):
        return _unauthorized()
    # Prevent path traversal — only allow plain filenames with .json extension
    safe_name = Path(filename).name
    if not safe_name.endswith('.json') or safe_name != filename or '..' in filename:
        return JSONResponse({'error': 'Invalid filename'}, status_code=400)
    filepath = file_manager.upload_dir / safe_name
    if not filepath.exists():
        return JSONResponse({'error': 'File not found'}, status_code=404)
    content = filepath.read_bytes()
    return StreamingResponse(
        io.BytesIO(content),
        media_type='application/json',
        headers={'Content-Disposition': f'attachment; filename="{safe_name}"'}
    )


@app.post('/logs/upload-now')
def logs_upload_now(request: Request, full: bool = False):
    """Trigger an immediate remote log upload using the configured webhook."""
    if not _request_authenticated(request):
        return _unauthorized()
    result = log_uploader.upload_now(full=full, trigger="manual")
    return JSONResponse(result, status_code=200 if result.get('ok') else 500)


@app.get('/logs/status')
def logs_status(request: Request):
    """Inspect the current logging configuration + last upload state."""
    if not _request_authenticated(request):
        return _unauthorized()
    cfg = load_logging_config()
    log_dir = get_log_dir()
    files = []
    if log_dir.exists():
        for f in sorted(log_dir.iterdir()):
            if f.is_file():
                files.append({'name': f.name, 'bytes': f.stat().st_size,
                              'mtime': datetime.fromtimestamp(f.stat().st_mtime).isoformat()})
    state_file = log_dir / '.uploader_state.json'
    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except Exception:
            pass
    # Strip the auth header from the response.
    safe_cfg = json.loads(json.dumps(cfg))
    safe_cfg['upload']['auth_header'] = '***' if safe_cfg['upload'].get('auth_header') else ''
    return JSONResponse({'config': safe_cfg, 'files': files, 'uploader_state': state})


def get_local_ip():
    """Get the local IP address of this machine."""
    try:
        # Create a socket to determine the local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# Jog parameters (user-adjustable)
jog_params = _ClientDict('jog_params', {
    'xy_step': 10.0,  # mm
    'z_step': 1.0,    # mm
    'a_step': 45.0,   # degrees
    'feed_rate': 4000.0,  # mm/min
})

# Z cut height parameter (used by toolpath generator)
z_cut_height = _ClientDict('z_cut_height', {'value': -35.0})

# Cut settings state — persisted with canvas saves
PRESSURE_MAP = {'Trace': -20.0, 'Light': -32.5, 'Medium': -35.0, 'Hard': -37.5, 'Very Hard': -40.0}
SPEED_MAP    = {'Slow': (5000.0, 8000.0), 'Medium': (10000.0, 10000.0), 'Fast': (15000.0, 18000.0)}
cut_settings = _ClientDict('cut_settings', {'pressure': 'Medium', 'speed': 'Medium'})
def _mark_canvas_dirty() -> None:
    """Record that the in-memory canvas no longer matches its saved file."""
    client_job_state['canvas_dirty'] = True


def apply_cut_pressure(name: str) -> None:
    """Apply a named pressure setting and record it in cut_settings."""
    changed = cut_settings.get('pressure') != name
    z_cut_height.update({'value': PRESSURE_MAP[name]})
    cut_settings['pressure'] = name
    if changed:
        _mark_canvas_dirty()

def apply_cut_speed(name: str) -> None:
    """Apply a named speed setting and record it in cut_settings."""
    changed = cut_settings.get('speed') != name
    cut_settings['speed'] = name
    if changed:
        _mark_canvas_dirty()

# DXF processing and toolpath generation
dxf_processor = DXFProcessor()
toolpath_generator = ToolpathGenerator(
    cutting_height=-30.0,  # Z height when cutting (mm)
    safe_height=-15.0,     # Z height when raised (mm)
    corner_angle_threshold=20.0,  # Standardized threshold across all corner detection
    feed_rate=15000.0,     # mm/min (250 mm/s)
    plunge_rate=12000.0,    # mm/min
    rapid_rate=18000.0,    # mm/min (300 mm/s) - rapid/jog moves
    min_curve_feed_rate=1000.0,  # mm/min - slow down for tight curves
    curve_slowdown_radius=75.0   # Start slowing below this radius (mm)
)

# Global storage for current toolpath visualization data.
# These survive a browser refresh / Socket.IO reconnect — the JS canvas
# is rebuilt from these dicts in init_canvas_after_load().
current_toolpath_shapes = _ClientDict('toolpath_shapes')
current_toolpath_breaks = _ClientDict('toolpath_breaks')
current_toolpath_types = _ClientDict('toolpath_types')
current_toolpath_notches = _ClientDict('toolpath_notches')
client_job_state = _ClientDict(
    'client_job_state',
    {'toolpath_generated': False, 'loaded_filename': None, 'canvas_dirty': False},
)


def _canvas_job_label() -> str:
    """Return the truthful identity to freeze into a generated machine job."""
    filename = client_job_state.get('loaded_filename')
    if not filename:
        return 'Untitled canvas'
    label = Path(filename).name
    if client_job_state.get('canvas_dirty', False):
        label += ' (modified)'
    return label


_CANVAS_MUTATING_ACTIONS = {
    'mirrorX', 'mirrorY', 'alignCentersVertical', 'alignCentersHorizontal',
    'distributeHorizontally', 'distributeVertically', 'ensureSpacing',
    'rotate90', 'rotateByDegrees', 'scaleShape', 'moveToOrigin',
    'centerOnBed', 'linearArray', 'gridArray', 'mirrorCopy', 'nestShapes',
    'duplicate', 'deleteShape', 'pasteShape', 'undo',
}
toolpath_canvas = None  # Reference to the canvas element

# API endpoint for jog control from JavaScript
@app.post('/jog')
async def jog_endpoint(request: Request):
    """Handle jog requests."""
    if not _request_authenticated(request):
        return _unauthorized()
    details = _cloudflare_request_details(request.headers)
    if details['via_cloudflare']:
        log_event(
            'security', 'cloudflare_control_blocked',
            control='jog_api', cloudflare_ip=details['cloudflare_ip'],
            access_email=details['access_email'],
        )
        return JSONResponse(
            {'status': 'error', 'message': 'CNC controls are disabled for remote sessions.'},
            status_code=403,
        )
    try:
        data = await request.json()
        axis = str(data['axis']).upper()
        direction = data['direction']
    except (KeyError, TypeError, ValueError):
        return JSONResponse(
            {'status': 'error', 'message': 'Malformed jog request'},
            status_code=400,
        )
    if isinstance(direction, bool) or not isinstance(direction, (int, float)) or direction not in (-1, 1):
        return JSONResponse(
            {'status': 'error', 'message': 'Direction must be -1 or 1'},
            status_code=400,
        )
    
    if axis == 'X' or axis == 'Y':
        distance = jog_params['xy_step'] * direction
    elif axis == 'Z':
        distance = jog_params['z_step'] * direction
    elif axis == 'A':
        distance = jog_params['a_step'] * direction
    else:
        return {'status': 'error', 'message': 'Invalid axis'}
    
    await asyncio.to_thread(cnc_controller.jog, axis, distance, jog_params['feed_rate'])
    log_event('jog', 'jog_request', axis=axis, direction=direction, distance=distance,
              feed_rate=jog_params['feed_rate'], source='js_endpoint')
    return {'status': 'ok'}

# Stores the latest toolpath visualization data for /toolpath-preview
_pending_viz_data = _ClientDict('pending_viz_data')

@app.get('/toolpath-preview')
def toolpath_preview(request: Request):
    """Serve latest toolpath visualization data as JSON (avoids embedding in WebSocket message)."""
    if not _request_authenticated(request):
        return _unauthorized()
    return JSONResponse(dict(_pending_viz_data))

# API endpoint for Packaide nesting
@app.post('/nest')
async def nest_endpoint(request: Request):
    """
    Handle nesting requests using Packaide library.
    Expects JSON with:
    - shapes: list of {name, points: [[x,y], ...], closed: bool}
    - sheetWidth: number (mm)
    - sheetHeight: number (mm)
    - offset: number (mm) - spacing between shapes
    - rotations: number - how many rotations to try (1=no rotation, 4=90° increments)
    """
    if not _request_authenticated(request):
        return _unauthorized()
    import asyncio
    import concurrent.futures
    import time
    
    _t0 = time.monotonic()
    try:
        data = await request.json()
        
        input_shapes = data.get('shapes', [])
        sheet_width = data.get('sheetWidth', 1720)
        sheet_height = data.get('sheetHeight', 1660)
        offset = data.get('offset', 2)  # mm spacing
        rotations = data.get('rotations', 4)  # Try 4 rotations by default
        total_points = sum(len(s.get('points') or []) for s in input_shapes)
        logger.info(f"[NEST] /nest received: shapes={len(input_shapes)} total_points={total_points} "
                    f"sheet={sheet_width}x{sheet_height} offset={offset} rotations={rotations}")
        try:
            log_event('transform', 'nest_request', shape_count=len(input_shapes),
                      total_points=total_points, sheet=[sheet_width, sheet_height],
                      offset=offset, rotations=rotations)
        except Exception:
            pass
        
        if not input_shapes:
            return {'status': 'error', 'message': 'No shapes provided'}
        
        # Run the CPU-intensive nesting in a thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            result = await loop.run_in_executor(
                executor,
                run_packaide_nesting,
                input_shapes, sheet_width, sheet_height, offset, rotations
            )
        
        elapsed = time.monotonic() - _t0
        logger.info(f"[NEST] /nest done in {elapsed:.2f}s status={result.get('status')} "
                    f"placed={result.get('placed')} failed={result.get('failed')}")
        try:
            log_event('transform', 'nest_response', duration_s=round(elapsed, 2),
                      status=result.get('status'), placed=result.get('placed'),
                      failed=result.get('failed'),
                      placement_count=len(result.get('placements') or []))
        except Exception:
            pass
        return result
        
    except Exception as e:
        elapsed = time.monotonic() - _t0
        logger.exception(f"[NEST] /nest error after {elapsed:.2f}s: {e}")
        try:
            log_event('transform', 'nest_error', duration_s=round(elapsed, 2), error=str(e))
        except Exception:
            pass
        return {'status': 'error', 'message': str(e)}


def run_packaide_nesting(input_shapes, sheet_width, sheet_height, offset, rotations):
    """Run Packaide nesting in a separate thread to avoid blocking the event loop."""
    import time
    _t_pack_start = time.monotonic()
    try:
        import packaide
        from xml.dom import minidom
        import re
        
        # Store original points by name for later transformation
        original_points = {}
        
        # Convert shapes to SVG paths
        svg_paths = []
        shape_ids = []
        for shape in input_shapes:
            name = shape.get('name', 'shape')
            points = shape.get('points', [])
            if len(points) < 2:
                continue
            
            original_points[name] = points
            
            # Create SVG path data
            path_d = f"M {points[0][0]},{points[0][1]}"
            for pt in points[1:]:
                path_d += f" L {pt[0]},{pt[1]}"
            if shape.get('closed', True):
                path_d += " Z"
            
            svg_paths.append(f'<path id="{name}" d="{path_d}" />')
            shape_ids.append(name)
        
        if not svg_paths:
            return {'status': 'error', 'message': 'No valid shapes to nest'}
        
        # Build shapes SVG
        shapes_svg = f'''<svg viewBox="0 0 {sheet_width} {sheet_height}">
            {''.join(svg_paths)}
        </svg>'''
        
        # Build empty sheet SVG
        sheet_svg = f'''<svg width="{sheet_width}" height="{sheet_height}" viewBox="0 0 {sheet_width} {sheet_height}">
        </svg>'''
        
        logger.info(f"Nesting {len(shape_ids)} shapes on {sheet_width}x{sheet_height} sheet with offset={offset}, rotations={rotations}")
        
        # Run Packaide
        _t_call = time.monotonic()
        result, placed, fails = packaide.pack(
            [sheet_svg],
            shapes_svg,
            tolerance=1.0,  # Curve approximation (lower = more accurate but slower)
            offset=offset,
            partial_solution=True,
            rotations=rotations,
            persist=True
        )
        
        logger.info(f"Packaide result: placed={placed}, fails={fails} (packaide.pack took {time.monotonic() - _t_call:.2f}s)")
        
        # Parse the result SVG to extract new positions
        # Packaide returns SVG with transforms that need to be applied
        placements = []
        if result:
            from xml.dom import minidom
            import re
            
            for sheet_idx, out_svg in result:
                try:
                    doc = minidom.parseString(out_svg)
                    paths = doc.getElementsByTagName('path')
                    
                    for path in paths:
                        path_id = path.getAttribute('id')
                        transform = path.getAttribute('transform')
                        
                        # Get original points for this shape
                        if path_id not in original_points:
                            continue
                        orig_pts = original_points[path_id]
                        
                        # Parse transform to get translation and rotation
                        tx, ty, angle = 0, 0, 0
                        rot_cx, rot_cy = 0, 0
                        if transform:
                            # Parse translate(x, y)
                            translate_match = re.search(r'translate\(([-\d.]+),\s*([-\d.]+)\)', transform)
                            if translate_match:
                                tx = float(translate_match.group(1))
                                ty = float(translate_match.group(2))
                            
                            # Parse rotate(angle, cx, cy) - Packaide uses this format
                            rotate_match = re.search(r'rotate\(([-\d.]+),\s*([-\d.]+),\s*([-\d.]+)\)', transform)
                            if rotate_match:
                                angle = float(rotate_match.group(1))
                                rot_cx = float(rotate_match.group(2))
                                rot_cy = float(rotate_match.group(3))
                        
                        # Apply transformation to original points
                        # Transform order: translate, then rotate around (rot_cx, rot_cy)
                        transformed_points = []
                        rad = math.radians(angle)
                        cos_a = math.cos(rad)
                        sin_a = math.sin(rad)
                        
                        for px, py in orig_pts:
                            # First translate
                            x = px + tx
                            y = py + ty
                            
                            # Then rotate around center (rot_cx, rot_cy) relative to translated position
                            if angle != 0:
                                # Rotation center is relative to translated origin
                                cx = tx + rot_cx
                                cy = ty + rot_cy
                                # Rotate point around center
                                dx = x - cx
                                dy = y - cy
                                x = cx + dx * cos_a - dy * sin_a
                                y = cy + dx * sin_a + dy * cos_a
                            
                            transformed_points.append([x, y])
                        
                        if transformed_points:
                            # Calculate bounding box center
                            xs = [p[0] for p in transformed_points]
                            ys = [p[1] for p in transformed_points]
                            center_x = (min(xs) + max(xs)) / 2
                            center_y = (min(ys) + max(ys)) / 2
                            
                            placements.append({
                                'name': path_id,
                                'points': transformed_points,
                                'centerX': center_x,
                                'centerY': center_y,
                                'angle': angle
                            })
                except Exception as e:
                    logger.error(f"Error parsing Packaide output: {e}")
        
        return {
            'status': 'ok',
            'placed': placed,
            'failed': fails,
            'placements': placements
        }
        
    except ImportError:
        return {'status': 'error', 'message': 'Packaide library not installed'}
    except Exception as e:
        logger.error(f"Nesting error: {e}")
        return {'status': 'error', 'message': str(e)}

# Current loaded G-code
current_gcode = []


def check_for_updates():
    """Fetch origin and return True if new commits are available on main."""
    try:
        subprocess.run(
            ['git', '-C', str(REPO_DIR), 'fetch', 'origin', 'main'],
            capture_output=True, timeout=15
        )
        # Count commits on origin/main that are not in HEAD
        result = subprocess.run(
            ['git', '-C', str(REPO_DIR), 'rev-list', '--count', 'HEAD..origin/main'],
            capture_output=True, text=True, timeout=5
        )
        count = int(result.stdout.strip() or '0')
        return count > 0
    except Exception as e:
        logger.warning(f"Update check failed: {e}")
        return False


def acknowledge_software_update():
    """Dismiss the current software update prompt until availability changes."""
    update_state['acknowledged'] = True
    log_event('system', 'software_update_acknowledged', current_version=APP_VERSION)


def _get_current_wifi_status():
    """Return (ssid, signal_percent) for the active Wi-Fi connection, or (None, None)."""
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'active,ssid,signal', 'dev', 'wifi', 'list', '--rescan', 'no'],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            # Terse mode escapes ':' inside fields as '\:', so split on unescaped colons only.
            parts = re.split(r'(?<!\\):', line)
            if len(parts) >= 3 and parts[0] == 'yes':
                ssid = parts[1].replace('\\:', ':')
                signal = int(parts[2]) if parts[2].isdigit() else None
                return ssid, signal
    except Exception as e:
        logger.warning(f"Wi-Fi status check failed: {e}")
    return None, None


def create_header():
    """Create the application header with tabs, position, status, and controls."""
    pos_labels = {}
    tabs = None
    
    with ui.header().classes('items-center justify-between py-1 px-3').style('background: linear-gradient(180deg, #2a2a2a 0%, #232323 100%); min-height: 48px; flex-wrap: nowrap; padding-top: 8px;'):
        # Left side: App name with icon + Tabs
        with ui.row().classes('items-center gap-4').style('flex-shrink: 0;'):
            with ui.row().classes('items-center gap-2'):
                ui.image('/static/favicon.svg').style('width: 24px; height: 24px;')
                ui.label('fabCNC').classes('text-h6 font-bold')
            
            # Tabs in header
            with ui.tabs().props('dense inline-label').classes('header-tabs').style('background: transparent;') as tabs:
                job_tab = ui.tab('Toolpath', icon='route').classes('nav-tab nav-toolpath').tooltip('Open toolpath setup and job controls')
                gcode_tab = ui.tab('GCODE', icon='terminal').classes('nav-tab nav-gcode').tooltip('Open manual G-code controls and response log')
                wifi_tab = ui.tab('System', icon='settings').classes('nav-tab nav-system').tooltip('Open connection, system, security, and diagnostic settings')
        
        # Center: Status pill + time estimate
        with ui.row().classes('items-center justify-center').style('flex: 1; min-width: 0;'):
            status_pill = ui.element('div').classes('flex items-center gap-2 px-3 py-1 rounded-full').style('background: #2d4a2d; border: 1px solid #3d5a3d;')
            status_pill.tooltip('Shows the current machine state; select it to reopen connection or recovery guidance when attention is required')
            with status_pill:
                status_icon = ui.icon('radio_button_checked', size='10px').classes('text-green-4')
                status_label = ui.label('Idle').classes('text-caption font-bold text-green-4').style(
                    'max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;'
                )
        
        # Right side: Position display + Update button + Version
        with ui.row().classes('items-center gap-2').style('flex-shrink: 0; overflow-x: auto;'):
            for axis in ['X', 'Y', 'Z', 'A']:
                with ui.element('div').classes('flex items-center gap-1 px-2 py-1 rounded').style('background: #3a3a3a; border: 1px solid #4a4a4a;'):
                    ui.label(f'{axis}').classes('text-caption font-bold').style('color: #888; width: 12px;')
                    unit = '°' if axis == 'A' else ''
                    pos_labels[axis] = ui.label(f'0.00{unit}').classes('text-body2 font-bold').style('min-width: 65px;')
            
            update_btn = ui.button('Software Up To Date', icon='check_circle') \
                .props('dense flat no-caps color=grey-6') \
                .style('font-size: 11px; min-width: 140px;')
            
            with ui.element('div').classes('flex items-center px-2 py-1 rounded ml-2').style('background: #3a3a3a; border: 1px solid #4a4a4a; cursor: pointer;') as wifi_widget:
                wifi_status_icon = ui.icon('signal_wifi_off', size='16px').style('color: #888;')
                ui.element('div').style('width: 10px; flex-shrink: 0;')
                wifi_status_text = ui.label('Wi-Fi: —').classes('text-caption').style('color: #aaa; white-space: nowrap;')
            wifi_widget.tooltip('View Wi-Fi status, scan for networks, or manage saved connections')

            ui.label(APP_VERSION).classes('text-caption ml-2').style('color: #666;')
    
    return (pos_labels, status_label, tabs, job_tab, gcode_tab, wifi_tab, update_btn,
            status_pill, status_icon, wifi_status_icon, wifi_status_text, wifi_widget)


def create_jog_controls():
    """Create jog controls with circular wheel design like Bambu Studio."""
    
    # Add CSS for SVG hover effects and round home button
    ui.add_head_html('''
    <style>
    .jog-segment {
        cursor: pointer;
        transition: fill 0.15s ease;
    }
    .jog-segment:hover {
        fill: #4a5a4a !important;
    }
    .jog-segment:active {
        fill: #5a6a5a !important;
    }
    .home-btn {
        border-radius: 50% !important;
    }
    .home-btn:hover {
        background: #4a5a4a !important;
    }
    /* Grey out move/control buttons when the controller is disconnected, but
       keep them clickable so a click still surfaces the disconnect popup. */
    body.controller-disconnected .motion-controls button,
    body.controller-disconnected .motion-controls .home-btn,
    body.controller-disconnected .motion-controls .jog-segment {
        opacity: 0.35 !important;
        filter: grayscale(70%);
    }
    </style>
    ''')
    
    # Main container - inline layout for toolpath panel
    with ui.column().classes('items-center gap-2 motion-controls'):
        
        # XY Circular Wheel
        with ui.column().classes('items-center gap-2'):
            # Circular jog wheel using SVG for proper arcs
            # Center at 154,154 (scaled 10% from 140). Equal width rings:
            # Home button: r=31, Inner: r=31-70, Middle: r=70-110, Outer: r=110-151
            # Each ring ~40px wide for equal visible thickness
            with ui.element('div').classes('jog-wheel').style('''
                position: relative;
                width: 308px;
                height: 308px;
            '''):
                # Create SVG wheel with proper arc segments
                wheel_svg = ui.element('div').style('position: absolute; top: 0; left: 0;')
                
                # SVG paths for each segment - 3 equal-width rings per quadrant (scaled 10%)
                # Outer ring: 100mm (r=151 to r=110)
                # Middle ring: 10mm (r=110 to r=70)
                # Inner ring: 1mm (r=70 to r=31)
                # Home button: r=31
                # Diagonal points at 45°: r*0.707
                # r=151: 106.8 -> (260.8, 47.2)
                # r=110: 77.8 -> (231.8, 76.2)
                # r=70: 49.5 -> (203.5, 104.5)
                # r=31: 21.9 -> (175.9, 132.1)
                
                svg_content = '''
                    <svg width="308" height="308" viewBox="0 0 308 308">
                        <!-- Background circle -->
                        <circle cx="154" cy="154" r="152" fill="#2a2a2a" stroke="#4a4a4a" stroke-width="2"/>
                        
                        <!-- Y+ OUTER (top, +100) -->
                        <path id="y-plus-100" class="jog-segment" fill="#3a3a3a"
                            d="M 154 3
                               A 151 151 0 0 1 260.8 47.2
                               L 231.8 76.2
                               A 110 110 0 0 0 154 44
                               A 110 110 0 0 0 76.2 76.2
                               L 47.2 47.2
                               A 151 151 0 0 1 154 3
                               Z"/>
                        
                        <!-- Y+ MIDDLE (top, +10) -->
                        <path id="y-plus-10" class="jog-segment" fill="#353535"
                            d="M 154 44
                               A 110 110 0 0 1 231.8 76.2
                               L 203.5 104.5
                               A 70 70 0 0 0 154 84
                               A 70 70 0 0 0 104.5 104.5
                               L 76.2 76.2
                               A 110 110 0 0 1 154 44
                               Z"/>
                        
                        <!-- Y+ INNER (top, +1) -->
                        <path id="y-plus-1" class="jog-segment" fill="#303030"
                            d="M 154 84
                               A 70 70 0 0 1 203.5 104.5
                               L 175.9 132.1
                               A 31 31 0 0 0 154 123
                               A 31 31 0 0 0 132.1 132.1
                               L 104.5 104.5
                               A 70 70 0 0 1 154 84
                               Z"/>
                        
                        <!-- X+ OUTER (right, +100) -->
                        <path id="x-plus-100" class="jog-segment" fill="#3a3a3a"
                            d="M 305 154
                               A 151 151 0 0 1 260.8 260.8
                               L 231.8 231.8
                               A 110 110 0 0 0 264 154
                               A 110 110 0 0 0 231.8 76.2
                               L 260.8 47.2
                               A 151 151 0 0 1 305 154
                               Z"/>
                        
                        <!-- X+ MIDDLE (right, +10) -->
                        <path id="x-plus-10" class="jog-segment" fill="#353535"
                            d="M 264 154
                               A 110 110 0 0 1 231.8 231.8
                               L 203.5 203.5
                               A 70 70 0 0 0 224 154
                               A 70 70 0 0 0 203.5 104.5
                               L 231.8 76.2
                               A 110 110 0 0 1 264 154
                               Z"/>
                        
                        <!-- X+ INNER (right, +1) -->
                        <path id="x-plus-1" class="jog-segment" fill="#303030"
                            d="M 224 154
                               A 70 70 0 0 1 203.5 203.5
                               L 175.9 175.9
                               A 31 31 0 0 0 185 154
                               A 31 31 0 0 0 175.9 132.1
                               L 203.5 104.5
                               A 70 70 0 0 1 224 154
                               Z"/>
                        
                        <!-- Y- OUTER (bottom, -100) -->
                        <path id="y-minus-100" class="jog-segment" fill="#3a3a3a"
                            d="M 154 305
                               A 151 151 0 0 1 47.2 260.8
                               L 76.2 231.8
                               A 110 110 0 0 0 154 264
                               A 110 110 0 0 0 231.8 231.8
                               L 260.8 260.8
                               A 151 151 0 0 1 154 305
                               Z"/>
                        
                        <!-- Y- MIDDLE (bottom, -10) -->
                        <path id="y-minus-10" class="jog-segment" fill="#353535"
                            d="M 154 264
                               A 110 110 0 0 1 76.2 231.8
                               L 104.5 203.5
                               A 70 70 0 0 0 154 224
                               A 70 70 0 0 0 203.5 203.5
                               L 231.8 231.8
                               A 110 110 0 0 1 154 264
                               Z"/>
                        
                        <!-- Y- INNER (bottom, -1) -->
                        <path id="y-minus-1" class="jog-segment" fill="#303030"
                            d="M 154 224
                               A 70 70 0 0 1 104.5 203.5
                               L 132.1 175.9
                               A 31 31 0 0 0 154 185
                               A 31 31 0 0 0 175.9 175.9
                               L 203.5 203.5
                               A 70 70 0 0 1 154 224
                               Z"/>
                        
                        <!-- X- OUTER (left, -100) -->
                        <path id="x-minus-100" class="jog-segment" fill="#3a3a3a"
                            d="M 3 154
                               A 151 151 0 0 1 47.2 47.2
                               L 76.2 76.2
                               A 110 110 0 0 0 44 154
                               A 110 110 0 0 0 76.2 231.8
                               L 47.2 260.8
                               A 151 151 0 0 1 3 154
                               Z"/>
                        
                        <!-- X- MIDDLE (left, -10) -->
                        <path id="x-minus-10" class="jog-segment" fill="#353535"
                            d="M 44 154
                               A 110 110 0 0 1 76.2 76.2
                               L 104.5 104.5
                               A 70 70 0 0 0 84 154
                               A 70 70 0 0 0 104.5 203.5
                               L 76.2 231.8
                               A 110 110 0 0 1 44 154
                               Z"/>
                        
                        <!-- X- INNER (left, -1) -->
                        <path id="x-minus-1" class="jog-segment" fill="#303030"
                            d="M 84 154
                               A 70 70 0 0 1 104.5 104.5
                               L 132.1 132.1
                               A 31 31 0 0 0 123 154
                               A 31 31 0 0 0 132.1 175.9
                               L 104.5 203.5
                               A 70 70 0 0 1 84 154
                               Z"/>
                        
                        <!-- Dividing lines -->
                        <line x1="47.2" y1="47.2" x2="260.8" y2="260.8" stroke="#4a4a4a" stroke-width="1"/>
                        <line x1="260.8" y1="47.2" x2="47.2" y2="260.8" stroke="#4a4a4a" stroke-width="1"/>
                        
                        <!-- Ring dividers -->
                        <circle cx="154" cy="154" r="110" fill="none" stroke="#4a4a4a" stroke-width="1"/>
                        <circle cx="154" cy="154" r="70" fill="none" stroke="#4a4a4a" stroke-width="1"/>
                        
                        <!-- Center circle background -->
                        <circle cx="154" cy="154" r="31" fill="#2a2a2a" stroke="#4a4a4a" stroke-width="2"/>
                        
                        <!-- Labels along top-right diagonal only -->
                        <text x="238" y="63" text-anchor="middle" fill="#666" font-size="11">100</text>
                        <text x="210" y="91" text-anchor="middle" fill="#666" font-size="11">10</text>
                        <text x="183" y="118" text-anchor="middle" fill="#666" font-size="11">1</text>
                        
                        <!-- Axis labels -->
                        <text x="154" y="20" text-anchor="middle" fill="#9e9e9e" font-size="15" font-weight="bold">Y</text>
                        <text x="293" y="159" text-anchor="middle" fill="#9e9e9e" font-size="15" font-weight="bold">X</text>
                        <text x="154" y="301" text-anchor="middle" fill="#9e9e9e" font-size="15" font-weight="bold">-Y</text>
                        <text x="15" y="159" text-anchor="middle" fill="#9e9e9e" font-size="15" font-weight="bold">-X</text>
                    </svg>
                '''
                
                wheel_svg._props['innerHTML'] = svg_content
                
                # Add click handlers via JavaScript
                ui.run_javascript('''
                    // Y+ handlers
                    document.getElementById('y-plus-100')?.addEventListener('click', () => {
                        emitEvent('jog', {axis: 'Y', distance: 100});
                    });
                    document.getElementById('y-plus-10')?.addEventListener('click', () => {
                        emitEvent('jog', {axis: 'Y', distance: 10});
                    });
                    document.getElementById('y-plus-1')?.addEventListener('click', () => {
                        emitEvent('jog', {axis: 'Y', distance: 1});
                    });
                    // X+ handlers
                    document.getElementById('x-plus-100')?.addEventListener('click', () => {
                        emitEvent('jog', {axis: 'X', distance: 100});
                    });
                    document.getElementById('x-plus-10')?.addEventListener('click', () => {
                        emitEvent('jog', {axis: 'X', distance: 10});
                    });
                    document.getElementById('x-plus-1')?.addEventListener('click', () => {
                        emitEvent('jog', {axis: 'X', distance: 1});
                    });
                    // Y- handlers
                    document.getElementById('y-minus-100')?.addEventListener('click', () => {
                        emitEvent('jog', {axis: 'Y', distance: -100});
                    });
                    document.getElementById('y-minus-10')?.addEventListener('click', () => {
                        emitEvent('jog', {axis: 'Y', distance: -10});
                    });
                    document.getElementById('y-minus-1')?.addEventListener('click', () => {
                        emitEvent('jog', {axis: 'Y', distance: -1});
                    });
                    // X- handlers
                    document.getElementById('x-minus-100')?.addEventListener('click', () => {
                        emitEvent('jog', {axis: 'X', distance: -100});
                    });
                    document.getElementById('x-minus-10')?.addEventListener('click', () => {
                        emitEvent('jog', {axis: 'X', distance: -10});
                    });
                    document.getElementById('x-minus-1')?.addEventListener('click', () => {
                        emitEvent('jog', {axis: 'X', distance: -1});
                    });
                ''')
                
                # Register event handler (jog_axis is async — NiceGUI awaits coroutine results)
                ui.on('jog', lambda e: jog_axis(e.args['axis'], e.args['distance']))
                
                # Home button in center (r=31, so diameter=62)
                with ui.element('div').style('''
                    position: absolute;
                    width: 58px;
                    height: 58px;
                    top: 50%;
                    left: 50%;
                    transform: translate(-50%, -50%);
                    border-radius: 50%;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    z-index: 10;
                '''):
                    ui.button(icon='home', on_click=home_all).props('flat round').classes('home-btn').style('color: #4caf50; font-size: 22px; width: 54px; height: 54px;') \
                        .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            
            # Z/A Step buttons below wheel
            with ui.column().classes('items-center gap-1'):
                with ui.row().classes('gap-1 items-center'):
                    ui.button('+10', on_click=lambda: jog_axis('Z', 10)).props('flat dense').style('background: #2a2a2a; color: #4caf50; font-size: 14px; width: 44px; height: 44px;') \
                        .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                    ui.button('+1', on_click=lambda: jog_axis('Z', 1)).props('flat dense').style('background: #2a2a2a; color: #4caf50; font-size: 14px; width: 44px; height: 44px;') \
                        .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                    ui.label('Z').style('color: #4caf50; font-size: 15px; width: 44px; height: 44px; text-align: center; font-weight: bold; display: flex; align-items: center; justify-content: center;')
                    ui.button('-1', on_click=lambda: jog_axis('Z', -1)).props('flat dense').style('background: #2a2a2a; color: #4caf50; font-size: 14px; width: 44px; height: 44px;') \
                        .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                    ui.button('-10', on_click=lambda: jog_axis('Z', -10)).props('flat dense').style('background: #2a2a2a; color: #4caf50; font-size: 14px; width: 44px; height: 44px;') \
                        .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                
                # A controls
                with ui.row().classes('gap-1 items-center'):
                    ui.button('+90', on_click=lambda: jog_axis('A', 90)).props('flat dense').style('background: #2a2a2a; color: #ff9800; font-size: 14px; width: 44px; height: 44px;') \
                        .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                    ui.button('+45', on_click=lambda: jog_axis('A', 45)).props('flat dense').style('background: #2a2a2a; color: #ff9800; font-size: 14px; width: 44px; height: 44px;') \
                        .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                    ui.label('A').style('color: #ff9800; font-size: 15px; width: 44px; height: 44px; text-align: center; font-weight: bold; display: flex; align-items: center; justify-content: center;')
                    ui.button('-45', on_click=lambda: jog_axis('A', -45)).props('flat dense').style('background: #2a2a2a; color: #ff9800; font-size: 14px; width: 44px; height: 44px;') \
                        .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                    ui.button('-90', on_click=lambda: jog_axis('A', -90)).props('flat dense').style('background: #2a2a2a; color: #ff9800; font-size: 14px; width: 44px; height: 44px;') \
                        .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                
                # XY Zero button - spans full width (5 * 44px + 4 gaps * 4px = 236px)
                ui.button('XY Zero', on_click=xy_zero).props('flat dense').style('background: #2a2a2a; color: #4a9eff; font-size: 14px; width: 236px; height: 36px; margin-top: 4px;') \
                    .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())

                # Tape Fabric button — homes then moves to center of work area
                ui.button('Tape Fabric', icon='straighten', on_click=tape_fabric).props('flat dense').style('background: #2a2a2a; color: #ce93d8; font-size: 13px; width: 236px; height: 36px; margin-top: 4px;') \
                    .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())

                # Change Cutting Wheel button — homes then lowers Z to wheel-change position
                ui.button('Change Cutting Wheel', icon='build', on_click=change_cutting_wheel).props('flat dense').style('background: #2a2a2a; color: #FFB300; font-size: 13px; width: 236px; height: 36px; margin-top: 4px;') \
                    .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
    



def create_homing_controls():
    """Create the compact homing control panel."""
    with ui.column().classes('gap-3 remote-control-lock'):
        ui.label('Homing').classes('text-h6 font-bold')
        
        with ui.row().classes('gap-2'):
            ui.button('X', on_click=lambda: home_axis('X')) \
                .props('size=lg') \
                .classes('w-20') \
                .style('font-size: 18px; padding: 12px 16px') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.is_idle())
            ui.button('Y', on_click=lambda: home_axis('Y')) \
                .props('size=lg') \
                .classes('w-20') \
                .style('font-size: 18px; padding: 12px 16px') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.is_idle())
            ui.button('Z', on_click=lambda: home_axis('Z')) \
                .props('size=lg') \
                .classes('w-20') \
                .style('font-size: 18px; padding: 12px 16px') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.is_idle())
            ui.button('A', on_click=lambda: home_axis('A')) \
                .props('size=lg') \
                .classes('w-20') \
                .style('font-size: 18px; padding: 12px 16px') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.is_idle())
        
        ui.button('Home All', on_click=home_all, color='primary') \
            .props('size=lg') \
            .classes('w-full') \
            .style('font-size: 18px; padding: 12px 16px') \
            .bind_enabled_from(machine_state, '_lock',
                             backward=lambda _: machine_state.is_idle())


def create_file_controls():
    """Create the compact file upload and management panel."""
    with ui.column().classes('w-full gap-2'):
        ui.label('Job File').classes('text-body1 font-bold w-full text-center').style('color: #aaa; background-color: #2a2a2a; padding: 6px 10px; border-radius: 4px; height: 48px; display: flex; align-items: center; justify-content: center; box-sizing: border-box;')
        
        upload = ui.upload(
            label='Load DXF Files',
            auto_upload=True,
            multiple=True,
            on_upload=lambda e: handle_file_upload(e)
        ).props('accept=.dxf dense multiple').classes('w-full dxf-upload').style('font-size: 13px;') \
            .tooltip('Select one or more DXF files to add their shapes to the canvas')
        
        # Use JavaScript to reset on click (before file picker opens)
        # This clears the old file, then user picks new file which shows up
        ui.add_head_html('''
            <script>
                document.addEventListener('click', function(e) {
                    // Check if click is on the upload header/button area (not the file list)
                    const uploader = e.target.closest('.dxf-upload');
                    if (uploader && (e.target.closest('.q-uploader__header') || e.target.closest('.q-btn'))) {
                        // Find and clear the file list
                        const list = uploader.querySelector('.q-uploader__list');
                        if (list) list.innerHTML = '';
                    }
                }, true);
            </script>
        ''')
        
        # Save/Load canvas buttons
        with ui.row().classes('w-full gap-1'):
            ui.button('Save', icon='save', on_click=save_canvas_state).props('dense flat stack').style('flex: 1; background-color: #2a2a2a; font-size: 12px; color: #4a9eff;').tooltip('Save the current canvas layout as a reusable file')
            ui.button('Load', icon='folder_open', on_click=load_canvas_state).props('dense flat stack').style('flex: 1; background-color: #2a2a2a; font-size: 12px; color: #4a9eff;').tooltip('Replace the current canvas with a previously saved layout')
            ui.button('Clear', icon='delete', on_click=clear_canvas).props('dense flat stack').style('flex: 1; background-color: #2a2a2a; font-size: 12px; color: #4a9eff;').tooltip('Remove every shape from the current canvas')


def create_job_controls():
    """Create the compact job execution control panel."""
    with ui.column().classes('w-full gap-1'):
        ui.label('Job Control').classes('text-body1 font-bold w-full text-center').style('color: #aaa; background-color: #2a2a2a; padding: 6px 10px; border-radius: 4px; height: 48px; display: flex; align-items: center; justify-content: center; box-sizing: border-box;')
        
        # Cut pressure + speed selectors (grid keeps dropdowns aligned)
        with ui.grid(columns='auto 1fr').classes('w-full items-center gap-x-2 gap-y-1'):
            ui.label('Cut Pressure:').style('color: #aaa; font-size: 13px;')
            _pressure_select = ui.select(
                options=list(PRESSURE_MAP.keys()),
                value=cut_settings['pressure'],
                on_change=lambda e: apply_cut_pressure(e.value)
            ).props('dense outlined').classes('w-full').style('font-size: 13px;') \
                .tooltip('Select the cutting-wheel pressure used when generating the toolpath')
            ui.context.client.storage['pressure_select'] = _pressure_select
            ui.label('Cut Speed:').style('color: #aaa; font-size: 13px;')
            _speed_select = ui.select(
                options=list(SPEED_MAP.keys()),
                value=cut_settings['speed'],
                on_change=lambda e: apply_cut_speed(e.value)
            ).props('dense outlined').classes('w-full').style('font-size: 13px;') \
                .tooltip('Select the machine feed rate used for cutting moves')
            ui.context.client.storage['speed_select'] = _speed_select
        apply_cut_pressure(cut_settings['pressure'])
        apply_cut_speed(cut_settings['speed'])
        
        # Generate Toolpath / Clear Toolpath toggle button
        toolpath_btn = ui.button('Generate Toolpath', icon='route', on_click=lambda: toggle_toolpath(toolpath_btn)) \
            .props('dense flat') \
            .classes('w-full remote-control-lock') \
            .style('font-size: 14px; background-color: #2a2a2a; color: #66BB6A;')

        ui.button('Outline Job', icon='crop_free', on_click=outline_job) \
            .props('dense flat') \
            .classes('w-full motion-action') \
            .style('font-size: 14px; background-color: #2a2a2a; color: #FFB300;') \
            .bind_enabled_from(machine_state, '_lock',
                             backward=lambda _: machine_state.job_loaded and machine_state.is_idle())

        ui.button('Start', icon='play_arrow', on_click=start_job) \
            .props('dense flat') \
            .classes('w-full motion-action') \
            .style('font-size: 14px; background-color: #2a2a2a; color: #4a9eff;') \
            .bind_enabled_from(machine_state, '_lock',
                             backward=lambda _: machine_state.is_idle())
        
        with ui.row().classes('w-full gap-1'):
            ui.button('Pause', icon='pause', on_click=pause_job) \
                .props('dense flat no-wrap') \
                .classes('flex-1 motion-action') \
                .style('font-size: 13px; background-color: #2a2a2a; color: #4a9eff; height: 36px; white-space: nowrap; overflow: hidden;') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.is_running())
            
            ui.button('Resume', icon='play_arrow', on_click=resume_job) \
                .props('dense flat no-wrap') \
                .classes('flex-1 motion-action') \
                .style('font-size: 13px; background-color: #2a2a2a; color: #4a9eff; height: 36px; white-space: nowrap; overflow: hidden;') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.paused)
        
        ui.button('Stop', icon='stop', on_click=stop_job) \
            .props('dense flat') \
            .classes('w-full motion-action') \
            .style('font-size: 14px; background-color: #2a2a2a; color: #4a9eff;') \
            .bind_enabled_from(machine_state, '_lock',
                             backward=lambda _: machine_state.busy)

        # Progress bar
        job_progress = ui.linear_progress(value=0, show_value=False).classes('w-full').style('height: 6px; margin-top: 8px;')
        job_progress.bind_value_from(machine_state, 'job_progress')

        return None  # resume button removed — resume is now a popup on reconnect


# Event handlers

async def xy_zero():
    """Handle XY Zero button click."""
    if not _require_controller_connected():
        return
    if not machine_state.is_idle():
        ui.notify('Cannot change XY zero while the machine is active.', type='warning')
        return
    log_event('control', 'xy_zero')
    response = await asyncio.to_thread(
        cnc_controller.send_manual_command, "G92 X0 Y0", timeout=10.0
    )
    if not any(line.strip().lower().startswith('ok') for line in response.splitlines()):
        logger.warning('XY zero was not accepted: %s', response)
        ui.notify('The controller did not accept XY Zero. Verify the controller, then try again.', type='negative', timeout=8000)


async def jog_axis(axis: str, distance: float):
    """Handle jog button click."""
    if not _require_controller_connected():
        return
    if not await safety_confirm():
        log_event('jog', 'jog_cancelled', axis=axis, distance=distance)
        return
    print(f"[DEBUG] jog_axis called: axis={axis}, distance={distance}, feed_rate={jog_params['feed_rate']}")
    log_event('jog', 'jog_button', axis=axis, distance=distance, feed_rate=jog_params['feed_rate'])
    await asyncio.to_thread(cnc_controller.jog, axis, distance, jog_params['feed_rate'])


async def home_axis(axis: str):
    """Handle home axis button click."""
    if not _require_controller_connected():
        return
    log_event('home', 'home_axis', axis=axis)
    if not await asyncio.to_thread(cnc_controller.home_axis, axis):
        ui.notify(
            'Homing blocked or failed. If Stop was used, confirm motion has stopped, '
            'run Controller Recovery, then home again.',
            type='negative', timeout=8000,
        )


async def home_all():
    """Handle home all button click."""
    if not _require_controller_connected():
        return
    log_event('home', 'home_all')
    if not await asyncio.to_thread(cnc_controller.home_all):
        ui.notify(
            'Homing blocked or failed. If Stop was used, confirm motion has stopped, '
            'run Controller Recovery, then home again.',
            type='negative', timeout=8000,
        )


async def tape_fabric():
    """Home the machine then move toolhead to center of work area for fabric taping."""
    if not _require_controller_connected():
        return
    if not await safety_confirm():
        return
    rapid = SPEED_MAP['Medium'][1]  # 10000 mm/min — medium rapid rate
    center_x = 860
    center_y = 830
    gcode = [
        'G90',
        'G28',
        f'G0 X{center_x} Y{center_y} F{rapid:.0f}',
        'M114',
    ]
    log_event('control', 'tape_fabric', center_x=center_x, center_y=center_y)
    ui.notify('Homing and moving to the fabric-taping position…', type='info')
    if not await _run_utility_sequence(gcode):
        ui.notify('Could not move to the fabric-taping position. Verify the controller, then try again.', type='negative', timeout=8000)


async def _run_utility_sequence(gcode: list[str]) -> bool:
    """Start a utility sequence and wait for its verified completion barrier."""
    worker = cnc_controller.run_utility_sequence(gcode)
    if worker is None:
        return False
    await asyncio.to_thread(worker.join)
    state = machine_state.snapshot()
    return (
        cnc_controller.connected
        and not cnc_controller.serial_uncertain
        and state.status_text == 'Idle'
        and not state.busy
    )


async def _wheel_step_confirm(step: str, instruction: str, confirm_label: str = 'Next Step', icon: str = 'build') -> bool:
    """Show a cutting wheel wizard step dialog. Returns True if user pressed the confirm button."""
    confirmed = False
    with ui.dialog() as dialog, ui.card().style(_DIALOG_CARD_STYLE):
        with ui.row().classes('items-center gap-2'):
            ui.icon(icon, size='28px').style('color: #FFB300;')
            ui.label(step).classes('text-h6 font-bold')
        ui.separator()
        ui.label(instruction).classes('text-body1').style('margin: 12px 0; white-space: pre-wrap;')
        with ui.row().classes('w-full justify-end gap-2').style('margin-top: 8px;'):
            ui.button('Cancel', on_click=dialog.close).props('flat').style('color: #aaa;')

            def _confirm():
                nonlocal confirmed
                confirmed = True
                dialog.close()

            ui.button(confirm_label, on_click=_confirm, icon='check') \
                .style('background-color: #FFB300; color: #111;')
    await dialog
    return confirmed


async def change_cutting_wheel():
    """Multi-step guided cutting wheel replacement."""
    if not _require_controller_connected():
        return
    if not await safety_confirm():
        return

    plunge = toolpath_generator.plunge_rate
    wheel_z_low = -20.0
    wheel_z_high = -10.0
    wheel_a = 90

    # ── Step 1: Home, lower to -20, rotate A to -90, disable steppers ──
    ui.notify('Step 1: Homing and moving to the screw-access position…', type='info')
    if not await _run_utility_sequence([
        'G90', 'G28',
        f'G1 Z{wheel_z_low} F{plunge:.0f}',
        f'G0 A{wheel_a}',
        'M400', 'M18',
    ]):
        ui.notify('Could not reach the screw-access position. Verify the controller before continuing.', type='negative', timeout=8000)
        return

    if not await _wheel_step_confirm('Step 1 of 3', 'Remove cutting wheel bolt', icon='hardware'):
        return

    # ── Step 2: Raise to -15, disable steppers ──
    ui.notify('Step 2: Raising the toolhead to the wheel-access height…', type='info')
    if not await _run_utility_sequence([
        'G90',
        f'G1 Z{wheel_z_high} F{plunge:.0f}',
        'M400', 'M18',
    ]):
        ui.notify('Could not reach the wheel-access height. Verify the controller before continuing.', type='negative', timeout=8000)
        return

    if not await _wheel_step_confirm(
        'Step 2 of 3',
        'Remove old cutting wheel and insert new cutting wheel.\n\nEnsure all hands are clear before continuing.',
        confirm_label='Hands Clear — Continue',
        icon='autorenew',
    ):
        return

    # ── Step 3: Lower back to -20, disable steppers ──
    ui.notify('Step 3: Lowering to the bolt-insertion position…', type='info')
    if not await _run_utility_sequence([
        'G90',
        f'G1 Z{wheel_z_low} F{plunge:.0f}',
        'M400', 'M18',
    ]):
        ui.notify('Could not reach the bolt-insertion position. Verify the controller before continuing.', type='negative', timeout=8000)
        return

    if not await _wheel_step_confirm(
        'Step 3 of 3',
        'Insert cutting wheel bolt',
        confirm_label='Done — Return to Home',
        icon='build',
    ):
        return

    # ── Step 4: Return to home ──
    ui.notify('Returning to the home position…', type='info')
    if not await _run_utility_sequence(['G90', 'G28']):
        ui.notify('Could not verify the return home. Keep clear and verify the controller.', type='negative', timeout=8000)
        return
    log_event('control', 'change_cutting_wheel_complete', wheel_z=wheel_z_low, wheel_a=wheel_a)
    ui.notify('Cutting wheel replacement complete.', type='positive')


def add_shapes_to_canvas(shapes: dict, start_color_index: int = 0, breaks: dict = None, entity_types: dict = None):
    """Add shapes to canvas without clearing existing ones."""
    if shapes:
        for i, (shape_name, points) in enumerate(shapes.items()):
            if points:
                # Log the bounds being sent to JavaScript
                x_vals = [p[0] for p in points]
                y_vals = [p[1] for p in points]
                logger.info(f"  Sending {shape_name} to canvas: {len(points)} pts, X({min(x_vals):.1f}-{max(x_vals):.1f}), Y({min(y_vals):.1f}-{max(y_vals):.1f})")
                
                # Convert points, segment breaks and entity types to JSON-safe format
                points_json = json.dumps(points)
                seg_breaks = breaks.get(shape_name, [0]) if breaks else [0]
                breaks_json = json.dumps(seg_breaks)
                seg_types = entity_types.get(shape_name, []) if entity_types else []
                types_json = json.dumps(seg_types)
                shape_name_json = json.dumps(shape_name)
                ui.run_javascript(f'''try {{
                    window.toolpathCanvas.addShape({shape_name_json}, {points_json}, {start_color_index + i}, {breaks_json}, {types_json});
                }} catch(e) {{
                    console.error('Could not add shape to canvas:', e);
                    if (typeof showToast === 'function') {{
                        showToast('A shape could not be added to the canvas. Reload the page and try again.', 'error', 8000);
                    }}
                }}''')
                logger.info(f"  Added {shape_name}: {len(points)} points, {len(seg_breaks)} segments, types={seg_types}")


def update_toolpath_plot(shapes: dict, clear_existing: bool = True):
    """Update the toolpath visualization with new shapes using Fabric.js canvas."""
    logger.info(f"Updating toolpath canvas with {len(shapes) if shapes else 0} shapes (clear={clear_existing})")
    
    if clear_existing:
        # Clear existing shapes
        ui.run_javascript('window.toolpathCanvas.clearShapes();')
    
    # Add shapes to canvas
    add_shapes_to_canvas(shapes)


async def handle_file_upload(event):
    """Handle file upload event."""
    global current_gcode, current_toolpath_shapes, toolpath_canvas
    
    import os
    import tempfile
    
    try:
        # NiceGUI UploadEventArguments has a .file attribute containing the SmallFileUpload
        uploaded_file = event.file
        filename = uploaded_file.name
        
        print(f"\n{'='*60}")
        print(f"DXF IMPORT DEBUG: {filename}")
        print(f"{'='*60}")
        
        # SmallFileUpload.read() is async
        with tempfile.NamedTemporaryFile(delete=False, suffix='.dxf') as tmp:
            content = await uploaded_file.read()
            tmp.write(content)
            tmp_path = tmp.name
        
        # Save to uploads directory
        saved_path = file_manager.save_uploaded_file(tmp_path, filename)
        os.unlink(tmp_path)
        print(f"Saved to: {saved_path}")
        log_event('file', 'dxf_import_started', filename=filename, saved_path=str(saved_path),
                  size_bytes=len(content))
        
        # Process DXF file
        ui.notify('Processing the DXF file…', type='info')
        # min_distance is in inches (DXF units before conversion to mm)
        # 0.1" = 2.54mm spacing - good balance of detail and point count
        shapes, shape_breaks, shape_types = dxf_processor.process_dxf_basic(saved_path, min_distance=0.1)
        current_toolpath_shapes.update(shapes)
        # Persist segment metadata so notch nodes can be regenerated after a
        # browser refresh (otherwise breaks=[0], types=[], and computeCardinalNodes
        # collapses the whole shape into a single midpoint node).
        if shape_breaks:
            current_toolpath_breaks.update(shape_breaks)
        if shape_types:
            current_toolpath_types.update(shape_types)
        
        # Debug: Print shape details
        print(f"\n--- DXF Processing Results ---")
        print(f"Total shapes extracted: {len(shapes)}")
        
        for shape_name, points in shapes.items():
            if not points:
                print(f"  {shape_name}: EMPTY")
                continue
                
            # Calculate bounds
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            width = max_x - min_x
            height = max_y - min_y
            
            # Check if closed
            first, last = points[0], points[-1]
            gap = math.sqrt((first[0] - last[0])**2 + (first[1] - last[1])**2)
            is_closed = gap < 3.81  # 0.15" in mm
            
            print(f"\n  {shape_name}:")
            print(f"    Points: {len(points)}")
            print(f"    Bounds: X({min_x:.1f} to {max_x:.1f}), Y({min_y:.1f} to {max_y:.1f})")
            print(f"    Size: {width:.1f}mm x {height:.1f}mm")
            print(f"    Start: ({first[0]:.2f}, {first[1]:.2f})")
            print(f"    End: ({last[0]:.2f}, {last[1]:.2f})")
            print(f"    Gap: {gap:.2f}mm {'(CLOSED)' if is_closed else '(OPEN)'}")
        
        # Update visualization without clearing existing shapes
        # This allows importing multiple DXF files
        # Offset color index by current shape count so each file gets distinct colors
        add_shapes_to_canvas(shapes, start_color_index=len(current_toolpath_shapes), breaks=shape_breaks, entity_types=shape_types)
        
        # Update state - clear any generated toolpath since shapes changed
        # A DXF import creates a new, unsaved canvas; the DXF filename is not
        # the identity of a saved canvas project.
        machine_state.set_job_loaded(True, 'Untitled canvas')
        machine_state.set_toolpath_generated(False)
        client_job_state['loaded_filename'] = None
        client_job_state['canvas_dirty'] = True
        client_job_state['toolpath_generated'] = False
        
        ui.notify(f'File loaded: {filename} ({len(shapes)} shapes)', type='positive')
        log_event('file', 'dxf_import_complete', filename=filename, shape_count=len(shapes),
                  shape_names=list(shapes.keys()))
    except Exception as e:
        ui.notify(_safe_error_message('The DXF file could not be processed.'), type='negative', timeout=8000)
        log_event('file', 'dxf_import_error', filename=locals().get('filename'), error=str(e))
        import traceback
        traceback.print_exc()


async def save_canvas_state():
    """Save current canvas state — shows all existing canvases with overwrite, download, and delete options."""
    import glob

    pattern = os.path.join(file_manager.upload_dir, '*.json')
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

    overwrite_confirmed = {'value': False}
    save_btn_ref = {}

    with ui.dialog() as dialog, ui.card().style(_DIALOG_CARD_STYLE + 'width: 540px;'):
        ui.label('Save Canvas').classes('text-h6 font-bold').style('color: #fff;')

        # ── Existing saves list ──────────────────────────────────────────
        if files:
            ui.label('Existing saves — click a name to fill the field below').style(
                'color: #aaa; font-size: 12px; margin-top: 4px;'
            )
            with ui.column().classes('w-full gap-0').style(
                'max-height: 260px; overflow-y: auto; border: 1px solid #333; border-radius: 4px;'
            ):
                for filepath in files[:50]:
                    filename = os.path.basename(filepath)
                    stem = filename[:-5] if filename.endswith('.json') else filename
                    try:
                        mtime = datetime.fromtimestamp(os.path.getmtime(filepath)).strftime('%m/%d %H:%M')
                    except Exception:
                        mtime = ''

                    def _fill(s=stem):
                        name_input.set_value(s)
                        warning_label.set_text('')
                        overwrite_confirmed['value'] = False
                        if save_btn_ref.get('btn'):
                            save_btn_ref['btn'].props('color=primary')
                            save_btn_ref['btn'].set_text('Save')

                    with ui.row().classes('w-full items-center gap-1').style(
                        'padding: 2px 6px; border-bottom: 1px solid #2a2a2a;'
                    ):
                        ui.button(stem, on_click=_fill) \
                            .classes('flex-1').props('flat align=left dense') \
                            .style('color: #4a9eff; font-size: 13px; text-transform: none; justify-content: flex-start;')
                        ui.label(mtime).style('color: #666; font-size: 11px; min-width: 72px; text-align: right;')

        ui.separator().style('margin: 10px 0 4px;')

        # ── Save-as input ────────────────────────────────────────────────
        name_input = ui.input('Save as…', value='').props('autofocus outlined').classes('w-full')
        warning_label = ui.label('').style('color: #FFA726; font-size: 12px; min-height: 18px;')

        async def do_save():
            try:
                name = name_input.value.strip()
                if not name:
                    ui.notify('Enter a filename.', type='warning')
                    return

                safe_name = ''.join(c if c.isalnum() or c in '-_ ' else '' for c in name).strip()
                safe_name = safe_name.replace(' ', '_')
                if not safe_name:
                    ui.notify('Enter a filename containing letters, numbers, spaces, hyphens, or underscores.', type='warning')
                    return

                if not safe_name.endswith('.json'):
                    safe_name = safe_name[:250] + '.json'
                else:
                    safe_name = safe_name[:255]

                filepath = os.path.join(file_manager.upload_dir, safe_name)

                if os.path.exists(filepath) and not overwrite_confirmed['value']:
                    warning_label.set_text(f'"{safe_name}" already exists. Click Save again to overwrite.')
                    overwrite_confirmed['value'] = True
                    save_btn_ref['btn'].props('color=warning')
                    save_btn_ref['btn'].set_text('Overwrite')
                    return

                overwrite_confirmed['value'] = False

                canvas_json = await ui.run_javascript('window.toolpathCanvas.saveCanvasState()', timeout=15.0)
                if not canvas_json:
                    ui.notify('There is no canvas data to save.', type='warning')
                    dialog.close()
                    return

                state = json.loads(canvas_json)
                state['cut_settings'] = cut_settings.copy()
                canvas_json = json.dumps(state, indent=2)

                with open(filepath, 'w') as f:
                    f.write(canvas_json)

                # Use the saved canvas name as the identity of the next job
                # generated from it.  Marlin always runs FABJOB.GCO, so the
                # human-readable canvas name must be retained separately.
                client_job_state['loaded_filename'] = safe_name
                client_job_state['canvas_dirty'] = False
                ui.notify(f'Saved: {safe_name}', type='positive')
                logger.info(f'Canvas state saved to {filepath}')
                log_event('canvas', 'canvas_saved', filename=safe_name,
                          shape_count=len(state.get('shapes', {})),
                          cut_settings=cut_settings.copy())
                dialog.close()

            except Exception as e:
                ui.notify(_safe_error_message('The canvas could not be saved.'), type='negative', timeout=8000)
                logger.exception('Error saving canvas')

        with ui.row().classes('w-full gap-2 mt-2'):
            ui.button('Cancel', on_click=dialog.close).props('flat').style('color: #aaa;')
            btn = ui.button('Save', on_click=do_save, color='primary')
            save_btn_ref['btn'] = btn

    dialog.open()


async def load_canvas_state():
    """Show dialog to load a saved canvas state."""
    import glob
    
    # Find all saved canvas JSON files
    pattern = os.path.join(file_manager.upload_dir, '*.json')
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)  # Most recent first
    
    if not files:
        ui.notify('No saved canvases were found.', type='warning')
        return
    
    with ui.dialog() as dialog, ui.card().style(_DIALOG_CARD_STYLE + 'width: 540px;'):
        ui.label('Load Canvas').classes('text-h6 font-bold').style('color: #fff;')

        ui.label('Click a canvas to load it').style('color: #aaa; font-size: 12px; margin-top: 4px;')
        with ui.column().classes('w-full gap-0').style(
            'max-height: 400px; overflow-y: auto; border: 1px solid #333; border-radius: 4px;'
        ):
            for filepath in files:
                filename = os.path.basename(filepath)
                stem = filename[:-5] if filename.endswith('.json') else filename
                try:
                    mtime = datetime.fromtimestamp(os.path.getmtime(filepath)).strftime('%m/%d %H:%M')
                except Exception:
                    mtime = ''

                def _download(fp=filepath):
                    safe = Path(fp).name
                    ui.run_javascript(
                        f"(function(){{const a=document.createElement('a');"
                        f"a.href='/canvas/download/{safe}';"
                        f"a.download='{safe}';"
                        f"document.body.appendChild(a);a.click();document.body.removeChild(a);}})();"
                    )

                async def _load(fp=filepath):
                    global current_toolpath_shapes, current_toolpath_breaks, current_toolpath_types, current_toolpath_notches
                    try:
                        with open(fp, 'r') as f:
                            canvas_json = f.read()
                        await ui.run_javascript(
                            f'window.toolpathCanvas.loadCanvasState({repr(canvas_json)})', timeout=15.0
                        )
                        state = json.loads(canvas_json)
                        current_toolpath_shapes.clear()
                        current_toolpath_breaks.clear()
                        current_toolpath_types.clear()
                        current_toolpath_notches.clear()
                        for name, shape_data in state.get('shapes', {}).items():
                            current_toolpath_shapes[name] = [tuple(p) for p in shape_data.get('points', [])]
                            current_toolpath_breaks[name] = list(shape_data.get('segmentBreaks', [0]) or [0])
                            current_toolpath_types[name] = list(shape_data.get('segmentTypes', []) or [])
                        for name, notches in (state.get('notches') or {}).items():
                            if notches:
                                current_toolpath_notches[name] = [dict(n) for n in notches]
                        saved_cut = state.get('cut_settings', {})
                        if saved_cut.get('pressure') in PRESSURE_MAP:
                            apply_cut_pressure(saved_cut['pressure'])
                            pressure_select = ui.context.client.storage.get('pressure_select')
                            if pressure_select:
                                pressure_select.set_value(saved_cut['pressure'])
                        if saved_cut.get('speed') in SPEED_MAP:
                            apply_cut_speed(saved_cut['speed'])
                            speed_select = ui.context.client.storage.get('speed_select')
                            if speed_select:
                                speed_select.set_value(saved_cut['speed'])
                        machine_state.set_job_loaded(True, os.path.basename(fp))
                        machine_state.set_toolpath_generated(False)
                        client_job_state['loaded_filename'] = os.path.basename(fp)
                        client_job_state['canvas_dirty'] = False
                        client_job_state['toolpath_generated'] = False
                        ui.notify(f'Canvas loaded: {os.path.basename(fp)}', type='positive')
                        log_event('canvas', 'canvas_loaded', filename=os.path.basename(fp),
                                  shape_count=len(current_toolpath_shapes),
                                  cut_settings=saved_cut)
                        dialog.close()
                    except Exception as e:
                        ui.notify(_safe_error_message('The canvas could not be loaded.'), type='negative', timeout=8000)
                        logger.exception('Error loading canvas %s', fp)

                with ui.row().classes('w-full items-center gap-1').style(
                    'padding: 2px 6px; border-bottom: 1px solid #2a2a2a;'
                ) as row:
                    ui.button(stem, on_click=_load) \
                        .classes('flex-1').props('flat align=left dense') \
                        .style('color: #4a9eff; font-size: 13px; text-transform: none; justify-content: flex-start;')
                    ui.label(mtime).style('color: #666; font-size: 11px; min-width: 72px; text-align: right;')
                    ui.button(icon='download', on_click=_download) \
                        .props('flat dense').style('color: #aaa;').tooltip('Download this saved canvas file to your computer')

                    def _delete(fp=filepath, r=row):
                        try:
                            os.remove(fp)
                            r.delete()
                            log_event('canvas', 'canvas_deleted', filename=os.path.basename(fp))
                            ui.notify(f'Deleted: {os.path.basename(fp)}', type='info')
                        except Exception as ex:
                            logger.exception('Could not delete saved canvas %s', fp)
                            ui.notify(_safe_error_message('The saved canvas could not be deleted.'), type='negative', timeout=8000)

                    ui.button(icon='delete', on_click=_delete) \
                        .props('flat dense color=negative').tooltip('Permanently delete this saved canvas file')

        with ui.row().classes('w-full mt-2'):
            ui.button('Cancel', on_click=dialog.close).props('flat').style('color: #aaa;')

    dialog.open()


def clear_canvas():
    """Clear all shapes from canvas."""
    global current_toolpath_shapes, current_toolpath_breaks, current_toolpath_types, current_toolpath_notches
    log_event('canvas', 'canvas_cleared', shape_count=len(current_toolpath_shapes),
              shape_names=list(current_toolpath_shapes.keys()))
    current_toolpath_shapes.clear()
    current_toolpath_breaks.clear()
    current_toolpath_types.clear()
    current_toolpath_notches.clear()
    ui.run_javascript('window.toolpathCanvas.clearShapes()')
    machine_state.set_job_loaded(False)
    machine_state.set_toolpath_generated(False)
    client_job_state['loaded_filename'] = None
    client_job_state['canvas_dirty'] = False
    client_job_state['toolpath_generated'] = False
    ui.notify('Canvas cleared.', type='info')


async def toggle_toolpath(button):
    """Toggle between Generate Toolpath and Clear Toolpath modes."""
    global current_gcode
    
    if client_job_state['toolpath_generated']:
        # Clear toolpath mode
        ui.run_javascript('window.toolpathCanvas.clearToolpath()')
        machine_state.set_toolpath_generated(False)
        client_job_state['toolpath_generated'] = False
        button.props('icon=route')
        button.set_text('Generate Toolpath')
        button.style('font-size: 14px; background-color: #2a2a2a; color: #66BB6A;')
        current_gcode = []
        try:
            app.storage.user.pop('generated_job', None)
        except Exception:
            pass
        log_event('toolpath', 'toolpath_cleared')
        ui.notify('Toolpath cleared. Shapes are now editable.', type='info')
    else:
        # Generate toolpath mode
        if _deny_cloudflare_control():
            return

        if not current_toolpath_shapes:
            ui.notify('No shapes are loaded.', type='warning')
            return
        
        ui.notify('Generating the toolpath…', type='info')
        
        # Fetch current shape positions from JavaScript canvas (in case shapes were moved)
        print("=== FETCHING POSITIONS FROM CANVAS ===")
        try:
            positions_json = await ui.run_javascript('JSON.stringify(window.toolpathCanvas.getPositions())', timeout=10.0)
            print(f"positions_json type: {type(positions_json)}, value: {str(positions_json)[:200] if positions_json else 'None'}")
            if positions_json:
                positions = json.loads(positions_json)
                print(f"Canvas shapes: {list(positions.keys())}")
                print(f"Python shapes before update: {list(current_toolpath_shapes.keys())}")
                # Replace ALL shapes with canvas positions
                current_toolpath_shapes.clear()
                for name, points in positions.items():
                    current_toolpath_shapes[name] = [tuple(p) for p in points]
                    print(f"  Added '{name}' with {len(points)} points")
                print(f"Python shapes after update: {list(current_toolpath_shapes.keys())}")
        except Exception as e:
            print(f"ERROR fetching positions: {e}")
            import traceback
            traceback.print_exc()

        # Warn (dismissible) if any shapes overlap so the user doesn't cut into
        # already-cut material or stack the blade on top of another piece.
        try:
            overlapping_pairs = find_overlapping_pairs(current_toolpath_shapes)
        except Exception as e:
            print(f"Overlap detection failed: {e}")
            overlapping_pairs = []
        if overlapping_pairs:
            log_event('toolpath', 'overlap_warning_shown',
                      pair_count=len(overlapping_pairs),
                      pairs=overlapping_pairs[:20])
            if not await overlap_warning_confirm(overlapping_pairs):
                log_event('toolpath', 'overlap_warning_cancelled',
                          pair_count=len(overlapping_pairs))
                ui.notify('Toolpath generation cancelled.', type='info')
                return
            log_event('toolpath', 'overlap_warning_dismissed',
                      pair_count=len(overlapping_pairs))

        # Snapshot every setting into a per-request generator. The shared UI can
        # serve multiple browsers, so a mutable global generator would allow one
        # client's settings/current-A state to alter another client's G-code.
        selected_feed, selected_rapid = SPEED_MAP[cut_settings['speed']]
        generator = ToolpathGenerator(
            cutting_height=z_cut_height['value'],
            safe_height=toolpath_generator.safe_height,
            corner_angle_threshold=math.degrees(toolpath_generator.corner_angle_threshold_radians),
            feed_rate=selected_feed,
            plunge_rate=toolpath_generator.plunge_rate,
            rapid_rate=selected_rapid,
            min_curve_feed_rate=toolpath_generator.min_curve_feed_rate,
            curve_slowdown_radius=toolpath_generator.curve_slowdown_radius,
        )
        generator.stealthchop = cut_settings['speed'] in ('Slow', 'Medium')
        
        # Fetch notch data from canvas (in mm coords, geometry pre-computed)
        notches = {}
        try:
            notches_json = await ui.run_javascript('JSON.stringify(window.toolpathCanvas.getNotches())', timeout=5.0)
            if notches_json:
                notches = json.loads(notches_json)
                total_notches = sum(len(v) for v in notches.values())
                if total_notches:
                    print(f"Fetched {total_notches} notch(es) from canvas")
        except Exception as e:
            print(f"Could not fetch notches: {e}")
        
        # Generate visualization data for the canvas
        # Run CPU-intensive toolpath generation off the asyncio event loop to avoid
        # blocking Socket.IO keepalives (which would cause the client to disconnect).
        loop = asyncio.get_event_loop()
        import concurrent.futures as _cf
        shapes_snapshot = dict(current_toolpath_shapes)
        notches_snapshot = dict(notches)
        with _cf.ThreadPoolExecutor(max_workers=1) as _exec:
            viz_data, gcode_str = await loop.run_in_executor(
                _exec,
                lambda: (
                    generator.generate_visualization_data(shapes_snapshot),
                    generator.generate_toolpath(shapes_snapshot, source_filename="preview", notches=notches_snapshot)
                )
            )
        current_gcode = gcode_str.split('\n')
        gcode_digest = hashlib.sha256(gcode_str.encode('utf-8')).hexdigest()
        app.storage.user['generated_job'] = {
            'gcode': list(current_gcode),
            'sha256': gcode_digest,
            'piece_count': len(shapes_snapshot),
            'source_filename': _canvas_job_label(),
        }

        # Store viz data server-side; JS fetches it via GET /toolpath-preview so we
        # avoid embedding (potentially megabytes of) JSON inline in the WebSocket message.
        _pending_viz_data.clear()
        _pending_viz_data.update(viz_data)
        preview_rendered = False
        for attempt in range(2):
            try:
                preview_rendered = await ui.run_javascript(
                    'window.toolpathCanvas.fetchAndShowToolpath()', timeout=20.0
                )
            except TimeoutError:
                logger.warning('Toolpath preview render timed out (attempt %d/2)', attempt + 1)
            if preview_rendered:
                break
            if attempt == 0:
                await asyncio.sleep(0.5)

        if not preview_rendered:
            log_event(
                'toolpath', 'toolpath_preview_render_failed',
                shape_count=len(current_toolpath_shapes),
                visualization_shape_count=len(viz_data.get('shapes', {})),
            )
            ui.notify(
                'Toolpath generated, but the preview did not render. Refresh the GUI and try again.',
                type='negative', timeout=10000,
            )
            return

        
        # Count corners and segments for info
        total_segments = sum(len(shape['segments']) for shape in viz_data['shapes'].values())
        total_corners = sum(1 for shape in viz_data['shapes'].values() 
                          for seg in shape['segments'] if seg.get('isCorner'))
        
        machine_state.set_toolpath_generated(True)
        client_job_state['toolpath_generated'] = True
        button.props('icon=close')
        button.set_text('Clear Toolpath')
        button.style('font-size: 14px; background-color: #2a2a2a; color: #FF6600;')
        
        log_toolpath('toolpath_generated',
                     shape_count=len(current_toolpath_shapes),
                     shape_names=list(current_toolpath_shapes.keys()),
                     segments=total_segments,
                     corners=total_corners,
                     gcode_lines=len(current_gcode),
                     z_cut_height=z_cut_height['value'],
                     cut_settings=cut_settings.copy(),
                     notch_count=sum(len(v) for v in notches.values()) if notches else 0)
        ui.notify(f'Toolpath generated: {total_segments} segments and {total_corners} corners.', type='positive')


async def safety_confirm() -> bool:
    """Show safety check dialog. Returns True if the user confirmed, False if cancelled."""
    confirmed = False
    with ui.dialog() as dialog, ui.card().style(_DIALOG_CARD_STYLE):
        with ui.row().classes('items-center gap-2'):
            ui.icon('warning', size='28px').style('color: #FFA726;')
            ui.label('Safety Check').classes('text-h6 font-bold')
        ui.separator()
        ui.label('Are all personnel and limbs clear of the cutting table?') \
            .classes('text-body1').style('margin: 12px 0;')
        with ui.row().classes('w-full justify-end gap-2').style('margin-top: 8px;'):
            ui.button('Cancel', on_click=dialog.close).props('flat').style('color: #aaa;')

            def _confirm():
                nonlocal confirmed
                confirmed = True
                dialog.close()

            ui.button('Confirm', on_click=_confirm, icon='check') \
                .style('background-color: #e53935; color: white;')

    await dialog
    return confirmed


async def overlap_warning_confirm(pairs):
    """Warn about overlapping shapes. Returns True to proceed, False to cancel.

    `pairs` is a list of (name_a, name_b) tuples reported by the overlap
    detector. The dialog lists up to 10 pairs and lets the user dismiss the
    warning (continue) or cancel toolpath generation."""
    proceed = False
    shown = pairs[:10]
    extra = len(pairs) - len(shown)
    with ui.dialog() as dialog, ui.card().style(_DIALOG_CARD_STYLE + 'width: 448px;'):
        with ui.row().classes('items-center gap-2'):
            ui.icon('warning', size='28px').style('color: #FFA726;')
            ui.label('Overlapping Shapes Detected').classes('text-h6 font-bold')
        ui.separator()
        ui.label(
            f'{len(pairs)} pair{"s" if len(pairs) != 1 else ""} of shapes appear '
            'to overlap. Cutting overlapping shapes may damage the material or '
            'the blade.'
        ).classes('text-body2').style('margin: 8px 0; color: #ddd;')
        with ui.column().classes('w-full').style(
                'max-height: 180px; overflow-y: auto; '
                'background: #1a1a1a; padding: 8px; border-radius: 4px;'):
            for a, b in shown:
                ui.label(f'• {a}  ↔  {b}').style('font-size: 12px; color: #FFCC80;')
            if extra > 0:
                ui.label(f'… and {extra} more').style('font-size: 12px; color: #888;')
        with ui.row().classes('w-full justify-end gap-2').style('margin-top: 8px;'):
            ui.button('Cancel', on_click=dialog.close).props('flat').style('color: #aaa;')

            def _dismiss():
                nonlocal proceed
                proceed = True
                dialog.close()

            ui.button('Generate Anyway', on_click=_dismiss, icon='check') \
                .style('background-color: #FFA726; color: #1a1a1a;')

    await dialog
    return proceed


async def outline_job():
    """Trace the bounding box of loaded shapes at safe height to show material placement."""
    global current_toolpath_shapes

    if not _require_controller_connected():
        return

    if not current_toolpath_shapes:
        ui.notify('No shapes are loaded.', type='warning')
        return

    if not cnc_controller.homed:
        ui.notify('Machine not homed — click Home All before outlining a job.', type='warning')
        return

    if not await safety_confirm():
        return

    # Fetch latest canvas positions (shapes may have been moved)
    try:
        positions_json = await ui.run_javascript('JSON.stringify(window.toolpathCanvas.getPositions())')
        if positions_json:
            positions = json.loads(positions_json)
            if positions:
                current_toolpath_shapes.clear()
                current_toolpath_shapes.update(
                    {name: [tuple(p) for p in pts] for name, pts in positions.items()}
                )
    except Exception as e:
        logger.warning(f'Could not fetch canvas positions for outline: {e}')

    # Compute bounding box across all shapes
    all_x = [p[0] for pts in current_toolpath_shapes.values() for p in pts]
    all_y = [p[1] for pts in current_toolpath_shapes.values() for p in pts]
    if not all_x:
        ui.notify('No valid shape points were found.', type='warning')
        return

    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)

    trace_z = -15.0  # Fixed trace height for outline (never cuts material)
    safe_z = toolpath_generator.safe_height
    _, rapid = SPEED_MAP[cut_settings['speed']]
    plunge = toolpath_generator.plunge_rate

    outline_gcode = [
        'G90',                                          # absolute positioning
        f'G0 Z{safe_z:.3f} F{plunge:.0f}',            # raise to safe height first
        f'G0 X{min_x:.3f} Y{min_y:.3f} F{rapid:.0f}', # move to corner 1
        f'G1 Z{trace_z:.3f} F{plunge:.0f}',           # lower to trace height
        f'G1 X{max_x:.3f} Y{min_y:.3f} F{rapid:.0f}', # corner 2
        f'G1 X{max_x:.3f} Y{max_y:.3f}',              # corner 3
        f'G1 X{min_x:.3f} Y{max_y:.3f}',              # corner 4
        f'G1 X{min_x:.3f} Y{min_y:.3f}',              # back to start
        f'G0 Z{safe_z:.3f} F{plunge:.0f}',            # raise before homing
        'G28 X Y',                                     # return home after outline
    ]

    w = max_x - min_x
    h = max_y - min_y
    ui.notify(f'Outlining the job area: {w:.0f} × {h:.0f} mm.', type='info')
    log_event('job', 'outline_clicked', width_mm=w, height_mm=h,
              min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y)
    cnc_controller.start_job(outline_gcode)


async def start_job():
    """Handle start job button click - uploads generated G-code to controller SD."""
    global current_gcode

    if not _require_controller_connected():
        return

    generated_job = app.storage.user.get('generated_job')
    job_gcode = list(generated_job.get('gcode', [])) if generated_job else []
    if not job_gcode:
        ui.notify('Generate a toolpath before starting the job.', type='warning')
        return

    if not await safety_confirm():
        return

    # Debug: Show gcode summary
    print(f"\n--- Starting Job ---")
    g0_count = sum(1 for line in job_gcode if line.startswith('G0'))
    g1_count = sum(1 for line in job_gcode if line.startswith('G1'))
    print(f"  Total lines: {len(job_gcode)}")
    print(f"  Rapid moves (G0): {g0_count}")
    print(f"  Cut moves (G1): {g1_count}")
    print(f"{'='*60}\n")
    
    ui.notify('Starting the job…', type='info')
    log_event('job', 'job_start_clicked', gcode_lines=len(job_gcode),
              g0_count=g0_count, g1_count=g1_count,
              filename=machine_state.loaded_filename,
              gcode_sha256=generated_job.get('sha256'))
    
    # Start job without callback - we'll monitor completion via state
    cnc_controller.start_job(
        job_gcode,
        piece_count=int(generated_job.get('piece_count', 0)),
        source_filename=generated_job.get('source_filename'),
    )


async def pause_job():
    """Handle pause job button click."""
    if not _require_controller_connected():
        return
    if cnc_controller.sd_uploading:
        ui.notify('Wait for the SD-card upload to finish before pausing.', type='warning')
        return
    log_event('job', 'pause_clicked')
    await asyncio.to_thread(cnc_controller.pause_job)
    if machine_state.paused:
        ui.notify('Job paused.', type='warning')
    else:
        ui.notify('Pause command was not accepted.', type='negative')


async def resume_job():
    """Handle resume job button click."""
    if not _require_controller_connected():
        return
    log_event('job', 'resume_clicked')
    await asyncio.to_thread(cnc_controller.resume_job)
    if machine_state.is_running():
        ui.notify('Job resumed.', type='positive')
    else:
        ui.notify('Resume command was not accepted.', type='negative')


async def stop_job():
    """Handle stop job button click."""
    if not _require_controller_connected():
        return
    # Give immediate visual feedback while the controller performs and verifies
    # the asynchronous SD abort.
    machine_state.set_status("Stopping", busy=True, paused=False)
    log_event('job', 'stop_clicked')
    if await asyncio.to_thread(cnc_controller.stop_job):
        ui.notify('Job stopped — full re-home required before motion.', type='warning', timeout=8000)
    else:
        ui.notify('SD stop failed — use the physical E-stop.', type='negative', timeout=8000)


async def resume_disconnect_job():
    """Home the machine then resume a job interrupted by a controller disconnect."""
    if not _require_controller_connected():
        return
    log_event('job', 'resume_disconnect_clicked')
    ui.notify('Homing before resume…', type='info')
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, cnc_controller.home_all)
    if not cnc_controller.homed:
        ui.notify('Homing failed — cannot resume.', type='negative')
        return
    if not cnc_controller.resume_from_disconnect():
        ui.notify('Resume failed — no saved state found.', type='negative')
        return
    ui.notify('Resuming from last safe point…', type='positive')


async def retry_controller_connection():
    """Retry Marlin controller discovery after power or USB is restored."""
    if _deny_cloudflare_control():
        return
    log_event('system', 'controller_reconnect_clicked')
    ui.notify('Retrying the controller connection…', type='info')
    await asyncio.to_thread(cnc_controller.attempt_reconnect)


async def force_controller_recovery():
    """Recover a stopped machine using M999 or one deliberate USB DTR reset."""
    if _deny_cloudflare_control():
        return
    if cnc_controller.connected:
        if cnc_controller.serial_uncertain and not cnc_controller.reset_required:
            if await asyncio.to_thread(cnc_controller.recover_uncertain_state):
                ui.notify('Communication recovered. Re-home before motion.', type='warning')
            else:
                ui.notify('Recovery failed. Use the physical E-stop and inspect the controller.', type='negative')
            return
        if not cnc_controller.reset_required:
            ui.notify('Controller is already connected.', type='info')
            return
        if await asyncio.to_thread(cnc_controller.reset_controller_after_estop):
            ui.notify('Controller reset. Re-home before the next job.', type='positive')
        else:
            logger.error('Controller reset failed: %s', cnc_controller.last_controller_error or 'No M999 response')
            ui.notify('The controller reset failed. Use the physical E-stop and inspect the controller.', type='negative', timeout=8000)
        return

    log_event('system', 'controller_force_recovery_clicked')
    ui.notify('Trying one USB reset, then reconnecting…', type='warning', timeout=6000)
    await asyncio.to_thread(cnc_controller.attempt_reconnect, force_dtr_reset=True)


async def update_ui(pos_labels, status_label, status_pill=None, status_icon=None):
    """Update UI with current machine state (called periodically)."""
    state = machine_state.snapshot()
    x, y, z, a = state.x, state.y, state.z, state.a
    previous_status = ui.context.client.storage.get('previous_machine_status')
    pos_labels['X'].set_text(f'{x:.2f} mm')
    pos_labels['Y'].set_text(f'{y:.2f} mm')
    pos_labels['Z'].set_text(f'{z:.2f} mm')
    pos_labels['A'].set_text(f'{a:.2f} °')
    
    # Update toolhead position on canvas only while machine is running (avoid blocking JS thread when idle)
    if state.busy:
        try:
            await ui.run_javascript(f'if(window.toolpathCanvas) window.toolpathCanvas.updateToolhead({x}, {y})', timeout=0.5)
        except Exception:
            pass
    
    # Update status
    current_status = state.status_text
    display_status = current_status
    sd_verification_status = 'SD job ended while disconnected — verify machine'
    if (
        cnc_controller.sd_job_active
        and not state.paused
        and current_status.startswith('Running')
        and state.loaded_filename
    ):
        canvas_name = Path(state.loaded_filename).name
        display_status = f'{current_status}: {canvas_name}'
    if current_status == sd_verification_status:
        display_status = 'Verify SD job'
    elif current_status == 'Retrying connection...':
        display_status = 'Reconnecting…'
    elif current_status == 'Disconnected':
        display_status = 'Controller disconnected'
    elif current_status == 'E-Stop Reset Required':
        display_status = 'Controller reset required'
    elif (
        not cnc_controller.connected
        or cnc_controller.reset_required
        or current_status == 'E-Stop Reset Required'
    ):
        display_status = 'Controller disconnected'
    status_label.set_text(display_status)
    # Update status pill appearance
    if status_pill and status_icon:
        if display_status in ('Controller disconnected', 'Controller reset required', 'Reconnecting…'):
            status_pill.style('background: #4a2d2d; border: 1px solid #7a3d3d;')
            status_icon.classes(add='text-red-5', remove='text-green-4 text-yellow-4')
            status_label.classes(add='text-red-5', remove='text-green-4 text-yellow-4')
        elif current_status == sd_verification_status:
            status_pill.style('background: #3d3a2d; border: 1px solid #6a5a3d;')
            status_icon.classes(add='text-yellow-4', remove='text-green-4 text-red-5')
            status_label.classes(add='text-yellow-4', remove='text-green-4 text-red-5')
        elif state.busy:
            status_pill.style('background: #3d3a2d; border: 1px solid #6a5a3d;')
            status_icon.classes(add='text-yellow-4', remove='text-green-4 text-red-5 text-orange-4')
            status_label.classes(add='text-yellow-4', remove='text-green-4 text-red-5 text-orange-4')
        else:
            status_pill.style('background: #2d4a2d; border: 1px solid #3d5a3d;')
            status_icon.classes(add='text-green-4', remove='text-red-5 text-yellow-4 text-orange-4')
            status_label.classes(add='text-green-4', remove='text-red-5 text-yellow-4 text-orange-4')

    # Detect status changes and show notifications (Complete/Error only — 
    # disconnect/reconnect handled per-page in _update_ui_timer)
    if previous_status != current_status:
        if current_status == 'Complete':
            ui.notify('Job completed successfully.', type='positive')
        elif current_status == 'Error':
            ui.notify('The job stopped because of an error. Verify the machine and controller before continuing.', type='negative', timeout=8000)
        ui.context.client.storage['previous_machine_status'] = current_status


@ui.page('/login')
def login_page():
    """Password splash screen shown on first access."""
    ui.dark_mode().enable()
    ui.add_head_html('''
        <style>
            :root {
                --font-ui: "Roboto", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                --md-bg-card: #333333;
                --radius-lg: 10px;
                --space-5: 20px;
            }
            html, body {
                margin: 0;
                padding: 0;
                height: 100vh;
                background: #121212;
                font-family: var(--font-ui);
            }
            button, input, .q-btn, .q-field, .q-dialog {
                font-family: var(--font-ui) !important;
            }
        </style>
    ''')

    async def try_login():
        if verify_password(password_input.value or '', _APP_PASSWORD):
            app.storage.user['authenticated'] = True
            app.storage.user['boot_token'] = _BOOT_TOKEN
            ui.navigate.to('/')
        else:
            ui.notify('Incorrect password.', type='negative')
            password_input.value = ''
            password_input.run_method('focus')

    async def forgot_password():
        with ui.dialog() as recovery_dialog, ui.card().style(_DIALOG_CARD_STYLE):
            ui.label('Recover UI Password').classes('text-h6 font-bold')
            ui.label(
                'Enter the recovery code shipped with this machine. '
                'The password will be restored to 2026.'
            ).classes('text-body2 text-grey-5')
            recovery_input = ui.input('Machine recovery code').props(
                'outlined dense autocomplete=off'
            ).classes('w-full').tooltip('Enter the recovery code provided with this machine')

            async def recover():
                global _BOOT_TOKEN
                client_key = _recovery_client_key()
                if _recovery_rate_limited(client_key):
                    ui.notify('Too many attempts. Try again in 15 minutes.', type='negative')
                    return
                supplied_code = recovery_input.value or ''
                valid = await asyncio.get_event_loop().run_in_executor(
                    None, verify_recovery_code, supplied_code,
                )
                if not valid:
                    _RECOVERY_FAILURES[client_key].append(time.monotonic())
                    log_event('security', 'ui_password_recovery_failed', client=client_key)
                    recovery_input.value = ''
                    ui.notify('Invalid recovery code.', type='negative')
                    return
                try:
                    await asyncio.get_event_loop().run_in_executor(None, set_password, '2026')
                except (OSError, ValueError):
                    logger.exception('Could not persist recovered UI password')
                    ui.notify('Could not reset the password. Contact service support.', type='negative')
                    return
                _RECOVERY_FAILURES.pop(client_key, None)
                _BOOT_TOKEN = secrets.token_hex(16)
                app.storage.user['authenticated'] = False
                app.storage.user['boot_token'] = ''
                log_event('security', 'ui_password_recovered', client=client_key)
                recovery_dialog.close()
                password_input.value = ''
                ui.notify('Password reset to 2026. Sign in and choose a new password.', type='positive', timeout=8000)

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Cancel', on_click=recovery_dialog.close).props('flat').tooltip('Close without resetting the password')
                ui.button('Reset Password', on_click=recover, icon='lock_reset').props('color=primary').tooltip('Verify the recovery code and reset the UI password')

        recovery_dialog.open()
        await recovery_dialog

    with ui.column().classes('items-center justify-center').style('height: 100vh; width: 100%;'):
        with ui.card().style('min-width: 320px; padding: 32px; background: var(--md-bg-card, #333333); border-radius: var(--radius-lg, 10px);'):
            ui.image('/static/favicon.svg').style('width: 72px; height: 72px; margin: 0 auto 0.5rem;')
            ui.label('fabCNC').style('font-size: 28px; font-weight: bold; color: #4a9eff; text-align: center; width: 100%;')
            ui.label('Enter password to continue').style('color: #888; text-align: center; margin-bottom: 1.5rem; width: 100%;')
            password_input = (
                ui.input(placeholder='Password', password=True)
                .props('outlined dense autofocus')
                .style('width: 100%; margin-bottom: 1rem;')
                .tooltip('Enter the fabCNC UI password')
            )
            password_input.on('keydown.enter', try_login)
            ui.button('Unlock', on_click=try_login) \
                .props('color=primary').style('width: 100%;').tooltip('Sign in to the fabCNC controller')
            ui.button('Forgot password?', on_click=forgot_password) \
                .props('flat color=grey-5 no-caps').style('width: 100%; margin-top: 0.5rem;').tooltip('Open password recovery')


@ui.page('/')
def main_page():
    """Main application page with responsive tabbed interface optimized for 1280x720 and larger."""
    
    # Redirect to login if not authenticated for this boot session
    if not app.storage.user.get('authenticated') or app.storage.user.get('boot_token') != _BOOT_TOKEN:
        ui.navigate.to('/login')
        return

    remote_details = _current_cloudflare_request_details()
    if remote_details['via_cloudflare']:
        log_event(
            'security', 'cloudflare_remote_access',
            client_id=str(ui.context.client.id),
            cloudflare_ip=remote_details['cloudflare_ip'],
            cloudflare_ray=remote_details['cloudflare_ray'],
            access_email=remote_details['access_email'],
        )
        ui.add_head_html('''
            <style>
                body.cloudflare-remote .motion-controls,
                body.cloudflare-remote .motion-action,
                body.cloudflare-remote .remote-control-lock {
                    pointer-events: none !important;
                    opacity: 0.45 !important;
                }
            </style>
            <script>
                (() => {
                    const lockRemoteControls = () => {
                        document.body?.classList.add('cloudflare-remote');
                        document.querySelectorAll(
                            '.motion-controls button, .motion-controls input, '
                            + '.motion-action button, .motion-action input, '
                            + '.motion-action select, .motion-action textarea, '
                            + 'button.motion-action, input.motion-action, '
                            + 'select.motion-action, textarea.motion-action, '
                            + '.remote-control-lock button, .remote-control-lock input, '
                            + '.remote-control-lock select, .remote-control-lock textarea, '
                            + 'button.remote-control-lock, input.remote-control-lock'
                        ).forEach((element) => { element.disabled = true; });
                    };
                    lockRemoteControls();
                    new MutationObserver(lockRemoteControls).observe(
                        document.documentElement,
                        {childList: true, subtree: true}
                    );
                })();
            </script>
        ''')
        ui.notify('Remote Cloudflare session: CNC and system controls are disabled.', type='warning')

    # Enforce dark mode
    ui.dark_mode().enable()
    
    # Disable scrolling on body and html + Material Design dark theme
    ui.add_head_html('''
        <style>
            /* Base Layout - no scroll at normal sizes */
            html, body {
                overflow: hidden !important;
                height: 100vh !important;
                margin: 0 !important;
                padding: 0 !important;
                min-width: 1440px;
            }
            
            /* Enable scrolling when window is too small */
            @media (max-height: 600px) {
                html, body {
                    overflow: auto !important;
                }
            }
            @media (max-width: 1440px) {
                html, body {
                    overflow-x: auto !important;
                }
            }
            
            /* Minimum app dimensions - enables scroll below this */
            .q-page, .q-page-container, .q-layout {
                min-width: 1440px;
                overflow: hidden !important;
            }
            
            /* Remove default Quasar tab panel padding to prevent overflow */
            .q-tab-panels, .q-tab-panel {
                padding: 0 !important;
            }
            
            /* Single source of truth for tab content spacing */
            .tab-content {
                padding: 8px 8px 35px 8px !important;
                height: 100% !important;
                box-sizing: border-box !important;
            }
            
            /* Material Design Dark Theme - Bambu Studio Inspired */
            :root {
                --font-ui: "Roboto", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                --font-mono: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
                --md-bg-primary: #1e1e1e;
                --md-bg-secondary: #252525;
                --md-bg-elevated: #2d2d2d;
                --md-bg-card: #333333;
                --md-border: #404040;
                --md-border-light: #4a4a4a;
                --md-text-primary: #e0e0e0;
                --md-text-secondary: #9e9e9e;
                --md-text-muted: #757575;
                --md-accent-blue: #4a9eff;
                --md-accent-green: #4caf50;
                --md-accent-orange: #ffa726;
                --md-accent-red: #f44336;
                --md-overlay: rgba(0, 0, 0, 0.6);
                --radius-sm: 4px;
                --radius-md: 6px;
                --radius-lg: 10px;
                --space-1: 4px;
                --space-2: 8px;
                --space-3: 12px;
                --space-4: 16px;
                --space-5: 20px;
                --type-caption: 12px;
                --type-control: 13px;
                --type-body: 14px;
                --type-section: 16px;
                --type-title: 20px;
            }

            html, body, button, input, select, textarea,
            .q-btn, .q-field, .q-tab, .q-menu, .q-dialog, .q-notification {
                font-family: var(--font-ui) !important;
            }

            .q-log, .q-log *, code, pre {
                font-family: var(--font-mono) !important;
            }
            
            /* Global Background */
            body, .q-page, .q-page-container {
                background-color: var(--md-bg-primary) !important;
            }
            
            /* Compact Header */
            header.q-header {
                background: linear-gradient(180deg, #2a2a2a 0%, #252525 100%) !important;
                border-bottom: 1px solid var(--md-border) !important;
                min-height: 56px !important;
                max-height: 56px !important;
                height: 56px !important;
                padding: 7px 16px !important;
            }
            
            /* Cards - Subtle and tight */
            .q-card {
                background: var(--md-bg-card) !important;
                border: 1px solid var(--md-border) !important;
                border-radius: var(--radius-lg) !important;
                box-shadow: none !important;
            }
            
            /* Dense Buttons - Material Design 3 style */
            .q-btn {
                border-radius: var(--radius-md) !important;
                font-size: var(--type-control);
                text-transform: none !important;
                font-weight: 500 !important;
                letter-spacing: 0.01em !important;
                transition: all 0.15s ease !important;
            }
            
            .q-btn:hover {
                filter: brightness(1.1) !important;
            }
            
            .q-btn--dense {
                padding: 4px 12px !important;
                min-height: 32px !important;
            }
            
            /* Compact Tabs - Match sidebar style */
            .q-tabs {
                background: var(--md-bg-secondary) !important;
                border-radius: var(--radius-lg) !important;
                padding: var(--space-1) !important;
            }
            
            .q-tab {
                min-height: 40px !important;
                padding: 0 16px !important;
                border-radius: var(--radius-md) !important;
                margin: 2px !important;
                text-transform: none !important;
                font-weight: 500 !important;
            }
            
            .q-tab--active {
                background: var(--md-bg-elevated) !important;
            }
            
            .q-tab-panels {
                background: transparent !important;
            }
            
            /* Hide number input spinners */
            input[type=number]::-webkit-inner-spin-button,
            input[type=number]::-webkit-outer-spin-button {
                -webkit-appearance: none;
                margin: 0;
            }
            input[type=number] {
                -moz-appearance: textfield;
            }

            /* Toolbar input fields - fixed height to match buttons */
            .toolbar-input .q-field__control {
                height: 36px !important;
                min-height: 36px !important;
            }
            .toolbar-input .q-field__native {
                padding-top: 0 !important;
                padding-bottom: 0 !important;
            }
            
            /* Inputs - Clean and compact */
            .q-field--outlined .q-field__control {
                border-radius: var(--radius-md) !important;
                background: var(--md-bg-secondary) !important;
            }
            
            .q-field--outlined .q-field__control:before {
                border-color: var(--md-border) !important;
            }
            
            .q-field--outlined.q-field--focused .q-field__control:after {
                border-color: var(--md-accent-blue) !important;
            }
            
            /* Upload Component */
            .q-uploader {
                background: var(--md-bg-secondary) !important;
                border: 1px dashed var(--md-border) !important;
                border-radius: var(--radius-lg) !important;
            }
            
            .q-uploader__header {
                background: var(--md-bg-elevated) !important;
                border-bottom: 1px solid var(--md-border) !important;
            }
            
            /* Separators */
            .q-separator {
                background: var(--md-border) !important;
            }
            
            /* Header tabs styling */
            .header-tabs {
                background: transparent !important;
                padding: 0 !important;
                border-radius: 0 !important;
            }
            
            .header-tabs .q-tab {
                min-height: 40px !important;
                height: 40px !important;
                padding: 0 14px !important;
                border: 1px solid transparent;
                border-radius: var(--radius-md) !important;
                margin: 0 2px !important;
                color: var(--md-text-secondary) !important;
                opacity: 0.82;
                transition: background 0.15s ease, border-color 0.15s ease,
                            color 0.15s ease, opacity 0.15s ease,
                            transform 0.15s ease !important;
            }

            .header-tabs .q-tab:hover:not(.q-tab--active) {
                background: rgba(255, 255, 255, 0.06) !important;
                border-color: var(--md-border-light);
                color: var(--md-text-primary) !important;
                opacity: 1;
            }

            .header-tabs .q-tab--active {
                color: #fff !important;
                opacity: 1;
                box-shadow: 0 3px 10px rgba(0, 0, 0, 0.3);
            }

            .header-tabs .nav-toolpath .q-icon { color: #66bb6a; }
            .header-tabs .nav-gcode .q-icon { color: #7eafff; }
            .header-tabs .nav-system .q-icon { color: #ffb74d; }

            .header-tabs .nav-toolpath.q-tab--active {
                background: rgba(76, 175, 80, 0.22) !important;
                border-color: rgba(102, 187, 106, 0.8);
                box-shadow: inset 0 -3px #66bb6a, 0 3px 10px rgba(0, 0, 0, 0.3);
            }

            .header-tabs .nav-gcode.q-tab--active {
                background: rgba(74, 158, 255, 0.22) !important;
                border-color: rgba(126, 175, 255, 0.8);
                box-shadow: inset 0 -3px #7eafff, 0 3px 10px rgba(0, 0, 0, 0.3);
            }

            .header-tabs .nav-system.q-tab--active {
                background: rgba(255, 167, 38, 0.22) !important;
                border-color: rgba(255, 183, 77, 0.8);
                box-shadow: inset 0 -3px #ffb74d, 0 3px 10px rgba(0, 0, 0, 0.3);
            }

            .header-tabs .q-tab__indicator {
                display: none !important;
            }

            .header-tabs .q-tab__label {
                font-size: 13px;
                font-weight: 600;
                letter-spacing: 0.01em;
            }
            
            .header-tabs .q-tabs__content {
                gap: 4px;
            }
            
            /* Labels styling */
            .text-h5, .text-h6 {
                color: var(--md-text-primary) !important;
                font-size: var(--type-title) !important;
                font-weight: 600 !important;
                line-height: 1.3 !important;
                letter-spacing: 0 !important;
            }

            .text-subtitle1, .text-body1 {
                font-size: var(--type-section) !important;
                line-height: 1.4 !important;
            }

            .text-body2 {
                font-size: var(--type-body) !important;
                line-height: 1.45 !important;
            }

            .text-caption {
                font-size: var(--type-caption) !important;
                line-height: 1.4 !important;
            }
            
            .text-grey-7 {
                color: var(--md-text-secondary) !important;
            }
            
            /* Linear Progress */
            .q-linear-progress {
                border-radius: 4px !important;
                background: var(--md-bg-secondary) !important;
            }
            
            /* Dialog styling */
            .q-dialog__inner > .q-card {
                background: var(--md-bg-card) !important;
                border: 1px solid var(--md-border-light) !important;
            }
            
            /* Notification styling */
            .q-notification {
                border-radius: var(--radius-lg) !important;
            }
            
            /* Log area */
            .q-log {
                background: var(--md-bg-secondary) !important;
                border: 1px solid var(--md-border) !important;
                border-radius: 6px !important;
            }
            
            /* Checkbox styling */
            .q-checkbox__inner {
                color: var(--md-accent-blue) !important;
            }
            
            /* Scrollbar styling */
            ::-webkit-scrollbar {
                width: 8px;
                height: 8px;
            }
            
            ::-webkit-scrollbar-track {
                background: var(--md-bg-secondary);
                border-radius: 4px;
            }
            
            ::-webkit-scrollbar-thumb {
                background: var(--md-border-light);
                border-radius: 4px;
            }
            
            ::-webkit-scrollbar-thumb:hover {
                background: #5a5a5a;
            }
            
            /* Tooltip styling */
            .q-tooltip {
                background: #484848 !important;
                color: var(--md-text-primary) !important;
                border-radius: 4px !important;
                font-size: 12px !important;
            }

            .fallback-control-tooltip {
                position: fixed;
                z-index: 100000;
                max-width: 320px;
                padding: 6px 9px;
                border-radius: var(--radius-sm);
                background: #484848;
                color: var(--md-text-primary);
                box-shadow: 0 3px 10px rgba(0, 0, 0, 0.35);
                font-size: var(--type-caption);
                line-height: 1.4;
                pointer-events: none;
                opacity: 0;
                transform: translateY(2px);
                transition: opacity 0.1s ease, transform 0.1s ease;
            }

            .fallback-control-tooltip.visible {
                opacity: 1;
                transform: translateY(0);
            }
        </style>
        <script>
            // Delegated tooltip coverage for every standard and dynamic control.
            // Explicit NiceGUI tooltips win; this supplies the remainder without
            // relying on browser-native title handling or mutation timing.
            const controlSelector = [
                'button', 'a[href]', '.q-btn', 'input', 'textarea', 'select',
                '[role="button"]', '[role="tab"]', '[role="checkbox"]',
                '[role="radio"]', '[role="switch"]', '[role="slider"]',
                '.q-field', '.q-uploader', '.jog-segment'
            ].join(', ');

            function tooltipControlFrom(target) {
                const field = target.closest?.('.q-field');
                return field || target.closest?.(controlSelector);
            }

            function fallbackControlTooltipText(el) {
                const iconNames = {
                    home: 'Home all machine axes', download: 'Download this file',
                    delete: 'Delete this item', close: 'Close this message',
                    play_arrow: 'Start or resume this operation', pause: 'Pause the active job',
                    stop: 'Stop the active job', clear_all: 'Clear the displayed output',
                    restart_alt: 'Restart or recover the selected service',
                    check: 'Confirm', password: 'Change password', cloud_upload: 'Send logs',
                    wifi: 'Connect to Wi-Fi', wifi_find: 'Scan for Wi-Fi networks',
                    tune: 'Apply settings', manage_search: 'Read current settings',
                    refresh: 'Restart the fabCNC service', send: 'Send this command to the controller',
                    folder_open: 'Open a saved canvas layout', save: 'Save the current canvas layout',
                    crop_free: 'Trace the job boundary without cutting',
                    build: 'Move the machine to the cutting-wheel change position',
                    straighten: 'Move the machine to the fabric-taping position',
                    wifi_off: 'Remove saved Wi-Fi connections',
                    lock_reset: 'Reset the UI password', article: 'Show recent service logs',
                    info: 'Show fabCNC service status', usb: 'List attached USB devices',
                    cable: 'List available serial ports',
                    align_horizontal_center: 'Align horizontal centers',
                    align_vertical_center: 'Align vertical centers',
                    horizontal_distribute: 'Distribute horizontally',
                    vertical_distribute: 'Distribute vertically'
                };
                const actionNames = {
                    Start: 'Start the generated cutting job on the machine',
                    Stop: 'Stop the active machine job',
                    Pause: 'Pause the active job', Resume: 'Resume the paused job',
                    Save: 'Save the current canvas layout as a reusable file',
                    Load: 'Replace the canvas with a saved layout',
                    Clear: 'Clear the current contents',
                    Nest: 'Arrange shapes automatically to reduce material usage',
                    Apply: 'Apply these settings', Cancel: 'Close without applying changes',
                    Run: 'Run the entered shell command on the fabCNC computer',
                    Rescan: 'Scan for available Wi-Fi networks again',
                    Send: 'Send the entered G-code command directly to the controller',
                    'Generate Toolpath': 'Generate the cutting path from the current canvas shapes',
                    'Clear Toolpath': 'Remove the generated cutting path while keeping the canvas shapes',
                    'Outline Job': 'Move around the job boundary without lowering the cutting wheel',
                    Scale: 'Resize the selected shapes using the entered percentage',
                    Grid: 'Duplicate the selected shapes into the specified rows and columns',
                    'XY Zero': 'Set the current X and Y positions as zero without moving the machine',
                    'Tape Fabric': 'Home the machine, then move to the fabric-taping position',
                    'Change Cutting Wheel': 'Home the machine, then lower Z to the cutting-wheel change position',
                    'Home All': 'Home every machine axis',
                    Connect: 'Connect to the selected Wi-Fi network',
                    'Forget Others': 'Delete all saved Wi-Fi networks except the current connection',
                    'Forget All & Reboot': 'Delete all saved Wi-Fi networks and reboot into setup mode',
                    'Forget & Reboot': 'Delete saved Wi-Fi networks and reboot into setup mode',
                    'Restart Service': 'Restart the fabCNC application service',
                    'Reboot System': 'Reboot the fabCNC computer',
                    'Change UI Password': 'Open the form for changing the fabCNC UI password',
                    'Read Current': 'Read homing sensitivity values from the controller',
                    'Send Logs to Dev': 'Package and securely send diagnostic logs to the developer',
                    'Download Logs': 'Download a diagnostic log bundle to this computer',
                    'Retry Connection': 'Try connecting to the machine controller again',
                    'Force Recovery': 'Open guarded recovery for an uncertain controller state',
                    Acknowledge: 'Dismiss this update notice without installing now',
                    'Update Now': 'Install the available fabCNC software update now',
                    'Software Up To Date': 'The installed fabCNC software is current',
                    Unlock: 'Sign in to unlock the fabCNC controls',
                    'Forgot password?': 'Open password recovery using the machine recovery code',
                    Discard: 'Discard the interrupted job recovery record',
                    'Reset Zoom': 'Fit the complete work area in the canvas'
                };
                let text = el.dataset.tooltip || el.getAttribute('aria-label')
                    || el.getAttribute('title') || '';
                if (!text && el.classList.contains('jog-segment')) {
                    const parts = (el.id || '').split('-');
                    const axis = (parts[0] || '').toUpperCase();
                    const direction = parts[1] === 'plus' ? '+' : '−';
                    text = `Jog ${axis} axis ${direction}${parts[2] || ''} mm`;
                }
                if (!text && el.classList.contains('q-field')) {
                    text = el.querySelector('.q-field__label')?.textContent
                        || el.querySelector('input, textarea')?.placeholder || '';
                    const fieldDescriptions = {
                        'Available networks': 'Select an available Wi-Fi network',
                        'Enter G-code command': 'Enter one G-code command to send directly to the controller',
                        'Password': 'Enter the password for the selected Wi-Fi network',
                        'Current password': 'Enter the current fabCNC UI password',
                        'New password': 'Enter a new fabCNC UI password',
                        'Confirm password': 'Enter the new password again to confirm it',
                        'X Stall': 'Set the X-axis sensorless homing sensitivity',
                        'Y Stall': 'Set the Y-axis sensorless homing sensitivity',
                        'Type a command…': 'Enter a shell command to run on the fabCNC computer'
                    };
                    text = fieldDescriptions[String(text).trim()] || text;
                }
                if (!text && (el.matches('input, textarea, select'))) {
                    text = el.placeholder || el.closest('.q-field')?.querySelector('.q-field__label')?.textContent || '';
                }
                const visibleText = String(el.innerText || '').replace(/\\s+/g, ' ').trim();
                if (!text && /^[+-]\\d+(?:\\.\\d+)?$/.test(visibleText)) {
                    const rowText = String(el.parentElement?.innerText || '');
                    const axis = rowText.match(/(?:^|\\s)([ZA])(?:\\s|$)/)?.[1];
                    if (axis === 'Z') text = `Jog the Z axis ${visibleText} millimeters`;
                    if (axis === 'A') text = `Rotate the A axis ${visibleText} degrees`;
                }
                if (!text && /^[XYZA]$/.test(visibleText)
                        && String(el.closest('.q-column')?.innerText || '').includes('Homing')) {
                    text = `Home the ${visibleText} axis`;
                }
                if (!text && visibleText && visibleText.length <= 80) {
                    text = actionNames[visibleText] || visibleText;
                }
                if (!text) {
                    const icon = el.querySelector?.('.q-icon')?.textContent?.trim();
                    text = iconNames[icon] || (icon ? icon.replaceAll('_', ' ') : 'Control');
                }
                text = String(text).replace(/\\s+/g, ' ').trim();
                return text ? text.charAt(0).toUpperCase() + text.slice(1) : 'Control';
            }

            window.addEventListener('load', () => {
                const tooltip = document.createElement('div');
                tooltip.className = 'fallback-control-tooltip';
                tooltip.setAttribute('role', 'tooltip');
                document.body.appendChild(tooltip);
                let activeControl = null;
                let showTimer = null;

                function hideFallbackTooltip() {
                    clearTimeout(showTimer);
                    activeControl = null;
                    tooltip.classList.remove('visible');
                }

                function hasVisibleExplicitTooltip() {
                    return [...document.querySelectorAll('.q-tooltip')].some(candidate => {
                        const style = window.getComputedStyle(candidate);
                        const rect = candidate.getBoundingClientRect();
                        return candidate !== tooltip
                            && style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && Number(style.opacity || 1) > 0
                            && rect.width > 0
                            && rect.height > 0;
                    });
                }

                function showFallbackTooltip(control) {
                    if (!control || control.querySelector?.('.q-tooltip')
                            || hasVisibleExplicitTooltip()) return;
                    activeControl = control;
                    clearTimeout(showTimer);
                    showTimer = setTimeout(() => {
                        if (activeControl !== control || hasVisibleExplicitTooltip()) return;
                        tooltip.textContent = fallbackControlTooltipText(control);
                        const rect = control.getBoundingClientRect();
                        tooltip.style.left = `${Math.max(8, Math.min(
                            window.innerWidth - tooltip.offsetWidth - 8,
                            rect.left + rect.width / 2 - tooltip.offsetWidth / 2
                        ))}px`;
                        tooltip.style.top = `${Math.min(window.innerHeight - 36, rect.bottom + 7)}px`;
                        tooltip.classList.add('visible');
                    }, 650);
                }

                document.addEventListener('pointerover', event => {
                    const control = tooltipControlFrom(event.target);
                    if (control && !control.contains(event.relatedTarget)) showFallbackTooltip(control);
                });
                document.addEventListener('pointerout', event => {
                    const control = tooltipControlFrom(event.target);
                    if (control && !control.contains(event.relatedTarget)) hideFallbackTooltip();
                });
                document.addEventListener('focusin', event => {
                    const control = tooltipControlFrom(event.target);
                    if (control) showFallbackTooltip(control);
                });
                document.addEventListener('focusout', hideFallbackTooltip);
            });

            // Reset scroll position when window is resized to normal size
            window.addEventListener('resize', function() {
                if (window.innerWidth >= 1440 && window.innerHeight >= 600) {
                    window.scrollTo(0, 0);
                    document.documentElement.scrollTop = 0;
                    document.body.scrollTop = 0;
                }
            });
        </script>
    ''')
    
    (pos_labels, status_label, tabs, job_tab, gcode_tab, wifi_tab, update_btn,
     status_pill, status_icon, wifi_status_icon, wifi_status_text, wifi_widget) = create_header()
    
    # Update button click handler
    async def do_software_update():
        import asyncio
        import concurrent.futures
        update_btn.set_text('Updating…')
        update_btn.props('dense flat no-caps icon=hourglass_top')
        update_btn.style('font-size: 11px; min-width: 140px; color: #ffa726;')
        update_btn.disable()
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            await loop.run_in_executor(
                executor,
                lambda: subprocess.run(
                    ['git', '-C', str(REPO_DIR), 'fetch', 'origin', 'main'],
                    capture_output=True, timeout=60
                )
            )
            await loop.run_in_executor(
                executor,
                lambda: subprocess.run(
                    ['git', '-C', str(REPO_DIR), 'reset', '--hard', 'origin/main'],
                    capture_output=True, timeout=30
                )
            )
            setup_result = await loop.run_in_executor(
                executor,
                lambda: subprocess.run(
                    ['bash', str(REPO_DIR / 'setup.sh'), '--post-update'],
                    capture_output=True, text=True, timeout=300
                )
            )

        if setup_result.returncode != 0:
            msg = (setup_result.stderr or setup_result.stdout or '').strip()
            log_event('system', 'post_update_setup_failed',
                      returncode=setup_result.returncode,
                      output=msg[:1000])
            ui.notify('Update applied, but post-update setup failed. Run: bash setup.sh',
                      type='warning', timeout=12000)
        else:
            log_event('system', 'post_update_setup_ok')

        # Tell the browser to reload after a delay, then exit.
        # systemd Restart=always will relaunch the service automatically — no sudo needed.
        await ui.run_javascript('setTimeout(() => window.location.reload(), 8000)')
        import sys
        sys.exit(0)

    update_btn.on_click(do_software_update)

    # Periodic update check (every 30 seconds)
    async def _check_update_timer():
        import asyncio
        import concurrent.futures
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            available = await loop.run_in_executor(executor, check_for_updates)
        previous = update_state['available']
        update_state['available'] = available
        if available and not previous:
            update_state['acknowledged'] = False
            log_event('system', 'software_update_available', current_version=APP_VERSION)
        elif not available:
            update_state['acknowledged'] = False
        if available:
            update_btn.set_text('Update Software')
            update_btn.props('dense flat no-caps icon=system_update_alt color=green-5')
            update_btn.style('font-size: 11px; min-width: 140px; background: #2d4a2d; border: 1px solid #3d5a3d; border-radius: 9999px;')
            update_btn.enable()
        else:
            update_btn.set_text('Software Up To Date')
            update_btn.props('dense flat no-caps icon=check_circle color=grey-6')
            update_btn.style('font-size: 11px; min-width: 140px; background: none; border: none;')
    ui.timer(0.1, _check_update_timer, once=True)
    ui.timer(300.0, _check_update_timer)  # Every 5 min — was 30 s; frequent git fetch stresses WiFi

    # Periodic Wi-Fi status check (SSID + signal strength shown in header)
    async def _check_wifi_status_timer():
        import asyncio
        import concurrent.futures
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            ssid, signal = await loop.run_in_executor(executor, _get_current_wifi_status)
        if ssid is None:
            wifi_status_icon.props('name=signal_wifi_off color=grey-6')
            wifi_status_text.set_text('Wi-Fi: —')
        else:
            # Single consistent icon family (network_wifi_*) so only the number of
            # filled bars changes with signal strength, like macOS's Wi-Fi icon.
            if signal is None or signal >= 80:
                bar_icon, color = 'network_wifi', 'green-5'
            elif signal >= 55:
                bar_icon, color = 'network_wifi_3_bar', 'green-5'
            elif signal >= 30:
                bar_icon, color = 'network_wifi_2_bar', 'amber-5'
            else:
                bar_icon, color = 'network_wifi_1_bar', 'red-5'
            wifi_status_icon.props(f'name={bar_icon} color={color}')
            wifi_status_text.set_text(ssid)
    ui.timer(0.1, _check_wifi_status_timer, once=True)
    ui.timer(30.0, _check_wifi_status_timer)

    # Wi-Fi settings dropdown, anchored to the header Wi-Fi widget
    with wifi_widget:
        with ui.menu() as wifi_menu:
            with ui.column().classes('gap-2 p-3').style('min-width: 300px;'):
                ui.label('Wi-Fi').classes('text-body1 font-bold').style('color: #aaa;')
                wifi_status_label = ui.label('Click to scan…').classes('text-caption').style('color: #888;')
                wifi_select = ui.select(options={}, label='Available networks') \
                    .props('dense').style('min-width: 260px;')

                def _parse_wifi_networks():
                    """Return (networks_list, raw_output) using multiline nmcli mode."""
                    import collections, time
                    # Force a fresh scan; goodpigeon has NOPASSWD sudo
                    try:
                        subprocess.run(
                            ['sudo', 'nmcli', 'device', 'wifi', 'rescan'],
                            capture_output=True, text=True, timeout=10
                        )
                        time.sleep(4)
                    except Exception:
                        pass
                    result = subprocess.run(
                        ['nmcli', '--mode', 'multiline', '-f',
                         'IN-USE,SSID,SIGNAL,SECURITY', 'device', 'wifi', 'list'],
                        capture_output=True, text=True, timeout=15
                    )
                    raw = result.stdout
                    # multiline format: indexed "FIELD[N]: value" or non-indexed "FIELD: value"
                    # Non-indexed: nmcli omits [N] on all records on some versions/configs;
                    # detect record boundaries by watching for IN-USE repeating.
                    entries = collections.defaultdict(dict)
                    current_idx = 0
                    for line in raw.splitlines():
                        # indexed form: FIELD[N]: value
                        m = re.match(r'^([A-Z_-]+)\[(\d+)\]:\s*(.*)', line)
                        if m:
                            field, idx, val = m.group(1), m.group(2), m.group(3).strip()
                            entries[idx][field.lower().replace('-', '_')] = val
                            continue
                        # non-indexed form: FIELD: value
                        m2 = re.match(r'^([A-Z_-]+):\s*(.*)', line)
                        if m2:
                            field, val = m2.group(1), m2.group(2).strip()
                            field_key = field.lower().replace('-', '_')
                            # IN-USE marks the start of each record
                            if field_key == 'in_use' and 'in_use' in entries[str(current_idx)]:
                                current_idx += 1
                            entries[str(current_idx)][field_key] = val
                    networks = []
                    seen = set()
                    for idx in sorted(entries, key=lambda x: int(x)):
                        e = entries[idx]
                        ssid = e.get('ssid', '').strip()
                        if not ssid or ssid in seen:
                            continue
                        seen.add(ssid)
                        networks.append({
                            'ssid': ssid,
                            'signal': e.get('signal', '?'),
                            'security': e.get('security', ''),
                            'in_use': e.get('in_use', '') == '*',
                        })
                    networks.sort(
                        key=lambda x: int(x['signal']) if x['signal'].isdigit() else 0,
                        reverse=True
                    )
                    return networks, raw

                async def scan_wifi():
                    wifi_status_label.set_text('Scanning…')
                    wifi_select.set_options({})
                    loop = asyncio.get_event_loop()
                    try:
                        networks, raw = await loop.run_in_executor(None, _parse_wifi_networks)
                    except Exception as exc:
                        logger.exception('Wi-Fi scan failed')
                        wifi_status_label.set_text('The Wi-Fi scan failed. Try again.')
                        return
                    if not networks:
                        logger.warning('No Wi-Fi networks parsed. Raw output: %r', raw[:500])
                        wifi_status_label.set_text('No Wi-Fi networks were found. Try scanning again.')
                        return
                    options = {}
                    for n in networks:
                        label = f"{'★ ' if n['in_use'] else ''}{n['ssid']}  ({n['signal']}%{', ' + n['security'] if n['security'] else ''})"
                        options[n['ssid']] = label
                    wifi_select.set_options(options)
                    wifi_status_label.set_text(f"Found {len(networks)} network(s).")

                def open_connect_dialog():
                    if _deny_cloudflare_control():
                        return
                    ssid = wifi_select.value
                    if not ssid:
                        ui.notify('Select a network first.', type='warning')
                        return
                    with ui.dialog() as conn_dlg, ui.card().style(_DIALOG_CARD_STYLE):
                        ui.label(f'Connect to "{ssid}"').classes('text-h6 font-bold')
                        pwd_input = ui.input('Password', password=True, password_toggle_button=True).classes('w-full')
                        conn_status = ui.label('').classes('text-caption').style('color: #f88;')
                        with ui.row().classes('gap-2 justify-end w-full mt-2'):
                            ui.button('Cancel', on_click=conn_dlg.close).props('flat dense')
                            async def do_connect():
                                if _deny_cloudflare_control():
                                    return
                                conn_status.set_text('Connecting…')
                                pwd = pwd_input.value
                                loop = asyncio.get_event_loop()
                                def run_connect():
                                    # Delete any stale/incomplete profile for this SSID
                                    # (avoids "key-mgmt: property is missing" from old profiles)
                                    subprocess.run(
                                        ['sudo', 'nmcli', 'connection', 'delete', ssid],
                                        capture_output=True, text=True, timeout=10
                                    )
                                    cmd = ['sudo', 'nmcli', 'device', 'wifi', 'connect', ssid]
                                    if pwd:
                                        cmd += ['password', pwd]
                                    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                                result = await loop.run_in_executor(None, run_connect)
                                if result.returncode == 0:
                                    conn_dlg.close()
                                    ui.notify(f'Connected to {ssid}.', type='positive')
                                    wifi_status_label.set_text(f'Connected to {ssid}')
                                else:
                                    err = result.stderr.strip() or result.stdout.strip()
                                    logger.warning('Wi-Fi connection to %s failed: %s', ssid, err)
                                    conn_status.set_text('Connection failed. Check the password and try again.')
                            ui.button('Connect', on_click=do_connect).props('color=primary dense')
                    conn_dlg.open()

                def confirm_forget_wifi():
                    if _deny_cloudflare_control():
                        return
                    with ui.dialog() as dlg, ui.card().style(_DIALOG_CARD_STYLE):
                        ui.label('Forget all Wi-Fi networks?').classes('text-h6 font-bold')
                        ui.label(
                            'This will delete all saved Wi-Fi connections and reboot the Pi. '
                            'On next boot it will create a "fabCNC Setup" hotspot.'
                        ).classes('text-body2').style('color: #aaa; max-width: 340px;')
                        with ui.row().classes('gap-2 justify-end w-full mt-4'):
                            ui.button('Cancel', on_click=dlg.close).props('flat dense')
                            async def do_forget():
                                if _deny_cloudflare_control():
                                    return
                                dlg.close()
                                ui.notify('Removing Wi-Fi connections and rebooting…', type='warning')
                                loop = asyncio.get_running_loop()
                                try:
                                    result = await loop.run_in_executor(
                                        None,
                                        lambda: subprocess.run(
                                            ['sudo', '/usr/local/sbin/fabcnc-forget-wifi'],
                                            capture_output=True,
                                            text=True,
                                            timeout=30,
                                        ),
                                    )
                                except (OSError, subprocess.TimeoutExpired) as exc:
                                    logger.exception('Wi-Fi reset helper failed')
                                    ui.notify(_safe_error_message('The Wi-Fi reset failed.'), type='negative', timeout=8000)
                                    return
                                if result.returncode != 0:
                                    error = result.stderr.strip() or result.stdout.strip()
                                    logger.error('Wi-Fi reset helper returned %s: %s', result.returncode, error)
                                    ui.notify(_safe_error_message('The Wi-Fi reset failed.'), type='negative', timeout=8000)
                            ui.button('Forget & Reboot', on_click=do_forget).props('color=negative dense')
                    dlg.open()

                def confirm_forget_wifi_except_current():
                    if _deny_cloudflare_control():
                        return
                    with ui.dialog() as dlg, ui.card().style(_DIALOG_CARD_STYLE):
                        ui.label('Forget all other Wi-Fi networks?').classes('text-h6 font-bold')
                        ui.label(
                            'This deletes every saved Wi-Fi profile except the one currently '
                            'connected. No reboot needed.'
                        ).classes('text-body2').style('color: #aaa; max-width: 340px;')
                        with ui.row().classes('gap-2 justify-end w-full mt-4'):
                            ui.button('Cancel', on_click=dlg.close).props('flat dense')
                            def do_forget_others():
                                if _deny_cloudflare_control():
                                    return
                                dlg.close()
                                ui.notify('Removing other saved Wi-Fi networks…', type='warning')
                                subprocess.Popen(
                                    'bash -c \''
                                    'active=$(nmcli -t -f NAME,TYPE connection show --active'
                                    ' | grep ":802-11-wireless$" | cut -d: -f1);'
                                    'nmcli -t -f NAME,TYPE connection show'
                                    ' | grep ":802-11-wireless$"'
                                    ' | cut -d: -f1'
                                    ' | while IFS= read -r n; do'
                                    ' [ "$n" != "$active" ] && nmcli connection delete "$n"; done\'',
                                    shell=True,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                )
                            ui.button('Forget Others', on_click=do_forget_others).props('color=warning dense')
                    dlg.open()

                with ui.row().classes('gap-2 items-center remote-control-lock'):
                    ui.button('Rescan', icon='wifi_find', on_click=scan_wifi).props('dense outline').style('font-size: 12px;')
                    ui.button('Connect', icon='wifi', on_click=open_connect_dialog).props('color=primary dense').style('font-size: 12px;')
                ui.separator()
                with ui.row().classes('gap-2 items-center remote-control-lock'):
                    ui.button('Forget Others', icon='wifi_off', on_click=confirm_forget_wifi_except_current) \
                        .props('color=warning dense').style('font-size: 12px;')
                    ui.button('Forget All & Reboot', icon='wifi_off', on_click=confirm_forget_wifi) \
                        .props('color=negative dense').style('font-size: 12px;')

    async def _open_wifi_menu():
        wifi_menu.open()
        await scan_wifi()

    wifi_widget.on('click', _open_wifi_menu)
    
    # Register JavaScript functions for jog control
    ui.run_javascript('''
        window.jogAxis = async (axis, direction) => {
            await fetch('/jog', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ axis: axis, direction: direction })
            });
        };
    ''')
    
    # Main content area - 48px header + 1px border = 49px, use 50px for safety
    with ui.column().classes('w-full mx-auto').style('height: calc(100vh - 50px); min-height: 600px; overflow: hidden;'):
        with ui.tab_panels(tabs, value=job_tab).classes('w-full').style('height: 100%;'):
            # Job tab - File loading, job execution, and toolpath visualization
            with ui.tab_panel(job_tab).classes('tab-content'):
                # Card: full height
                with ui.card().classes('w-full h-full').style('padding: 10px; box-sizing: border-box;'):
                    with ui.row().classes('gap-2 w-full').style('height: 100%; flex-wrap: nowrap;'):
                        # Left column: Job file and controls (fixed width, scrollable)
                        with ui.column().classes('gap-2').style('flex: 0 0 200px; max-height: 100%; overflow-y: auto;'):
                            create_file_controls()
                            ui.separator()
                            create_job_controls()
                        
                        # Center column: Toolbar + Interactive Toolpath Canvas (Fabric.js)
                        global toolpath_canvas
                        with ui.column().style('flex: 1 1 0; min-width: 400px; gap: 10px; height: 100%; box-sizing: border-box;'):
                            # Toolbar row above canvas - no wrap
                            with ui.row().classes('items-center gap-2').style('background: #2a2a2a; border-radius: 4px; padding: 6px 10px; width: 100%; flex-wrap: nowrap; flex-shrink: 0;'):
                                def _run_js_logged(action: str, js: str, **extras):
                                    log_event('transform', action, **extras)
                                    ui.run_javascript(js)

                                # Transform tools
                                ui.button('⬌', on_click=lambda: _run_js_logged('mirror_x', 'window.toolpathCanvas.mirrorX()')).props('dense flat').style('min-width: 36px; height: 36px; font-size: 22px; background-color: #2a2a2a; color: #4a9eff; display: flex; align-items: center; justify-content: center;').tooltip('Mirror the selected shapes left-to-right across the X axis')
                                ui.button('⬍', on_click=lambda: _run_js_logged('mirror_y', 'window.toolpathCanvas.mirrorY()')).props('dense flat').style('min-width: 36px; height: 36px; font-size: 22px; background-color: #2a2a2a; color: #4a9eff; display: flex; align-items: center; justify-content: center;').tooltip('Mirror the selected shapes top-to-bottom across the Y axis')
                                
                                ui.element('div').style('width: 1px; height: 24px; background: #4a4a4a; margin: 0 4px;')  # Separator
                                
                                rotate_input = ui.number(value=90, format='%.0f').props('dense outlined').style('width: 60px; font-size: 13px;').classes('toolbar-input').tooltip('Set the rotation angle in degrees')
                                ui.label('°').classes('text-body2').style('margin-right: 2px;')
                                ui.button('↻', on_click=lambda: _run_js_logged('rotate', f'window.toolpathCanvas.rotateByDegrees({rotate_input.value})', degrees=rotate_input.value)).props('dense flat').style('min-width: 36px; height: 36px; font-size: 22px; background-color: #2a2a2a; color: #4a9eff; display: flex; align-items: center; justify-content: center;').tooltip('Rotate the selected shapes clockwise by the entered angle')
                                ui.button('↺', on_click=lambda: _run_js_logged('rotate', f'window.toolpathCanvas.rotateByDegrees(-{rotate_input.value})', degrees=-rotate_input.value)).props('dense flat').style('min-width: 36px; height: 36px; font-size: 22px; background-color: #2a2a2a; color: #4a9eff; display: flex; align-items: center; justify-content: center;').tooltip('Rotate the selected shapes counterclockwise by the entered angle')
                                
                                ui.element('div').style('width: 1px; height: 24px; background: #4a4a4a; margin: 0 4px;')  # Separator
                                
                                scale_input = ui.number(value=100, format='%.0f').props('dense outlined').style('width: 60px; font-size: 13px;').classes('toolbar-input').tooltip('Set the selected shapes’ size as a percentage of their current size')
                                ui.label('%').classes('text-body2').style('margin-right: 2px;')
                                ui.button('Scale', on_click=lambda: _run_js_logged('scale', f'window.toolpathCanvas.scaleShape({scale_input.value / 100})', factor=scale_input.value / 100)).props('dense flat').style('height: 36px; font-size: 13px; background-color: #2a2a2a; color: #4a9eff;')
                                
                                ui.element('div').style('width: 1px; height: 24px; background: #4a4a4a; margin: 0 4px;')  # Separator
                                
                                # Pattern tools
                                grid_x = ui.number(value=2, format='%.0f', min=1, max=1000).props('dense outlined').style('width: 50px; font-size: 13px;').classes('toolbar-input').tooltip('Set the number of columns in the shape grid')
                                ui.label('×').classes('text-body2')
                                grid_y = ui.number(value=2, format='%.0f', min=1, max=1000).props('dense outlined').style('width: 50px; font-size: 13px;').classes('toolbar-input').tooltip('Set the number of rows in the shape grid')
                                ui.button('Grid', on_click=lambda: _run_js_logged('grid_array', f'window.toolpathCanvas.gridArray({int(grid_x.value)}, {int(grid_y.value)})', count_x=int(grid_x.value), count_y=int(grid_y.value))).props('dense flat').style('height: 36px; font-size: 13px; background-color: #2a2a2a; color: #4a9eff;')
                                
                                ui.element('div').style('width: 1px; height: 24px; background: #4a4a4a; margin: 0 4px;')  # Separator
                                
                                keep_orientation = ui.checkbox('Keep Orientation', value=True).props('dense').style('font-size: 12px;').tooltip('Keep each shape at its current rotation while nesting')
                                nest_offset = ui.number(
                                    value=15.0,
                                    format='%.1f',
                                    min=1.0,
                                    max=100.0,
                                    step=0.1,
                                ).props('dense outlined').style('width: 60px; font-size: 13px;').classes('toolbar-input').tooltip('Set the minimum spacing between nested shape outlines, in millimeters')
                                
                                async def do_nest():
                                    offset_val = float(nest_offset.value)
                                    keep_orient = str(keep_orientation.value).lower()
                                    logger.info(f"[NEST] do_nest start: offset={offset_val} keep={keep_orient} "
                                                f"py_shape_count={len(current_toolpath_shapes)}")
                                    log_event('transform', 'nest_clicked', offset_mm=offset_val,
                                              keep_orientation=keep_orientation.value,
                                              shape_count=len(current_toolpath_shapes))
                                    import time as _time
                                    _t_js = _time.monotonic()
                                    try:
                                        result = await ui.run_javascript(
                                            f'window.toolpathCanvas.nestShapes({keep_orient}, {offset_val})',
                                            timeout=30.0
                                        )
                                        logger.info(f"[NEST] do_nest JS sync-return after {_time.monotonic() - _t_js:.2f}s: {result!r}")
                                    except Exception as e:
                                        logger.exception(f"[NEST] do_nest run_javascript failed after {_time.monotonic() - _t_js:.2f}s: {e}")
                                        try:
                                            log_event('transform', 'nest_js_error',
                                                      duration_s=round(_time.monotonic() - _t_js, 2),
                                                      error=str(e))
                                        except Exception:
                                            pass
                                        ui.notify('Nesting timed out. Try again with fewer shapes.', type='warning')
                                        return
                                    if result and isinstance(result, dict):
                                        log_event('transform', 'nest_result',
                                                  success=result.get('success'),
                                                  width=result.get('width'),
                                                  height=result.get('height'),
                                                  error=result.get('error'))
                                
                                ui.button('Nest', on_click=do_nest).props('dense flat').style('height: 36px; font-size: 13px; background-color: #2a2a2a; color: #4a9eff;')
                            
                            # Second toolbar row: inspection/editing tools
                            notch_mode_state = {'active': False, 'btn': None}
                            measure_mode_state = {'active': False, 'btn': None}

                            def toggle_notch_mode():
                                notch_mode_state['active'] = not notch_mode_state['active']
                                btn = notch_mode_state['btn']
                                if notch_mode_state['active']:
                                    deactivate_measure_btn()
                                    btn.props('dense unelevated')
                                    btn.style('height: 36px; font-size: 13px; background-color: #FF6B35 !important; color: #1a1a1a !important; font-weight: 700;')
                                    btn.set_text('V Notch  (ON)')
                                else:
                                    btn.props('dense flat')
                                    btn.style('height: 36px; font-size: 13px; background-color: #2a2a2a; color: #FF6B35;')
                                    btn.set_text('V Notch')
                                ui.run_javascript(f"window.toolpathCanvas.setNotchMode({str(notch_mode_state['active']).lower()})")

                            def deactivate_notch_btn():
                                """Reset the notch button to OFF state (called when JS auto-disables notch mode)."""
                                notch_mode_state['active'] = False
                                btn = notch_mode_state['btn']
                                if btn:
                                    btn.props('dense flat')
                                    btn.style('height: 36px; font-size: 13px; background-color: #2a2a2a; color: #FF6B35;')
                                    btn.set_text('V Notch')

                            def deactivate_measure_btn():
                                measure_mode_state['active'] = False
                                btn = measure_mode_state['btn']
                                if btn:
                                    btn.props('dense flat')
                                    btn.style('height: 36px; font-size: 13px; background-color: #2a2a2a; color: #FFD54F;')
                                    btn.set_text('Measure')

                            def toggle_measure_mode():
                                measure_mode_state['active'] = not measure_mode_state['active']
                                btn = measure_mode_state['btn']
                                if measure_mode_state['active']:
                                    deactivate_notch_btn()
                                    btn.props('dense unelevated')
                                    btn.style('height: 36px; font-size: 13px; background-color: #FFD54F !important; color: #1a1a1a !important; font-weight: 700;')
                                    btn.set_text('Measure  (ON)')
                                else:
                                    deactivate_measure_btn()
                                ui.run_javascript(f"window.toolpathCanvas.setMeasureMode({str(measure_mode_state['active']).lower()})")

                            with ui.row().classes('items-center gap-2').style('background: #2a2a2a; border-radius: 4px; padding: 4px 10px; width: 100%; flex-shrink: 0;'):
                                notch_btn = ui.button('V Notch', on_click=toggle_notch_mode).props('dense flat').style('height: 36px; font-size: 13px; background-color: #2a2a2a; color: #FF6B35;').tooltip('Toggle notch tool — click nodes on shapes to add/remove V-notches')
                                notch_mode_state['btn'] = notch_btn
                                measure_btn = ui.button('Measure', icon='straighten', on_click=toggle_measure_mode).props('dense flat').style('height: 36px; font-size: 13px; background-color: #2a2a2a; color: #FFD54F;').tooltip('Measure between endpoints, corners, and cardinal extrema — click a start point, then an end point')
                                measure_mode_state['btn'] = measure_btn
                                ui.element('div').style('width: 1px; height: 24px; background: #4a4a4a; margin: 0 4px;')
                                ui.button(icon='align_horizontal_center', on_click=lambda: ui.run_javascript('window.toolpathCanvas.alignCentersVertical()')).props('dense flat').style('min-width: 36px; height: 36px; background-color: #2a2a2a; color: #4a9eff;').tooltip('Align Center — same X centerpoint')
                                ui.button(icon='align_vertical_center', on_click=lambda: ui.run_javascript('window.toolpathCanvas.alignCentersHorizontal()')).props('dense flat').style('min-width: 36px; height: 36px; background-color: #2a2a2a; color: #4a9eff;').tooltip('Align Middle — same Y centerpoint')
                                ui.button(icon='horizontal_distribute', on_click=lambda: ui.run_javascript('window.toolpathCanvas.distributeHorizontally()')).props('dense flat').style('min-width: 36px; height: 36px; background-color: #2a2a2a; color: #4a9eff;').tooltip('Distribute Horizontally — equal X spacing (need 3+ shapes)')
                                ui.button(icon='vertical_distribute', on_click=lambda: ui.run_javascript('window.toolpathCanvas.distributeVertically()')).props('dense flat').style('min-width: 36px; height: 36px; background-color: #2a2a2a; color: #4a9eff;').tooltip('Distribute Vertically — equal Y spacing (need 3+ shapes)')
                                ui.element('div').style('width: 1px; height: 24px; background: #4a4a4a; margin: 0 4px;')
                                spread_spacing = ui.number(value=15, format='%.0f', min=1, max=200).props('dense outlined').style('width: 50px; font-size: 13px;').classes('toolbar-input').tooltip('Min gap between shape outlines (mm)')
                                ui.button('Spread', on_click=lambda: ui.run_javascript(f'window.toolpathCanvas.ensureSpacing({int(spread_spacing.value)})')).props('dense flat').style('height: 36px; font-size: 13px; background-color: #2a2a2a; color: #4a9eff;').tooltip('Push shapes apart so every outline gap ≥ spacing value')
                                ui.element('div').style('width: 1px; height: 24px; background: #4a4a4a; margin: 0 4px;')
                                ui.element('div').style('flex: 1;')
                                ui.button('⌖ Reset Zoom', on_click=lambda: ui.run_javascript('window.toolpathCanvas.resetZoom()')).props('dense flat').style('height: 36px; font-size: 13px; background-color: #2a2a2a; color: #aaaaaa;').tooltip('Reset zoom & pan to fit the full work area (or scroll to zoom, Alt+drag to pan)')
                                ui.element('div').style('width: 1px; height: 24px; background: #4a4a4a; margin: 0 4px;')

                                # Units toggle: mm ↔ in
                                units_state = {'unit': 'mm'}

                                async def toggle_units():
                                    if units_state['unit'] == 'mm':
                                        units_state['unit'] = 'in'
                                        units_btn.set_text('in')
                                    else:
                                        units_state['unit'] = 'mm'
                                        units_btn.set_text('mm')
                                    unit = units_state['unit']
                                    await ui.run_javascript(f"window.toolpathCanvas.setUnits('{unit}')")

                                units_btn = ui.button('mm', on_click=toggle_units).props('dense flat').style('height: 36px; font-size: 13px; background-color: #2a2a2a; color: #aaaaaa; min-width: 52px;').tooltip('Toggle axis units between mm and inches')
                            
                            # Load Fabric.js library
                            ui.add_head_html('<script src="https://cdnjs.cloudflare.com/ajax/libs/fabric.js/5.3.1/fabric.min.js"></script>')
                            ui.add_head_html(
                                f'<script src="/static/toolpath_canvas.js?v={TOOLPATH_CANVAS_ASSET_VERSION}"></script>'
                            )
                            
                            # Create canvas container - flex fills space
                            toolpath_canvas = ui.html('''
                                <div id="canvas-container" style="width: 100%; height: 100%; min-width: 400px; background-color: #1e1e1e; border-radius: 4px; overflow: hidden;">
                                    <canvas id="toolpath-canvas"></canvas>
                                </div>
                            ''', sanitize=False).classes('w-full').style('width: 100%; flex: 1; min-height: 0;')
                            
                            # Initialize canvas after page fully loads
                            async def init_canvas_after_load():
                                await ui.context.client.connected()
                                for _ in range(10):  # retry up to 10x if JS not ready
                                    try:
                                        await ui.run_javascript('''
                                            // Wait for Fabric.js and our canvas module to be ready
                                            function initWhenReady() {
                                                const container = document.getElementById('canvas-container');
                                                const canvasEl = document.getElementById('toolpath-canvas');
                                                if (typeof fabric !== 'undefined' && window.toolpathCanvas && container && canvasEl) {
                                                    console.log('Initializing toolpath canvas...');
                                                    window.toolpathCanvas.init("toolpath-canvas");
                                                    return true;
                                                } else {
                                                    console.log('Waiting for dependencies... fabric:', typeof fabric, 'toolpathCanvas:', !!window.toolpathCanvas, 'container:', !!container);
                                                    setTimeout(initWhenReady, 100);
                                                    return false;
                                                }
                                            }
                                            initWhenReady();
                                        ''', timeout=3.0)
                                        break  # success
                                    except Exception:
                                        await asyncio.sleep(0.5)

                                # Restore shapes and toolpath if they already exist server-side
                                # (handles page reload mid-session due to WebSocket reconnect)
                                if current_toolpath_shapes:
                                    await asyncio.sleep(0.4)  # let canvas finish initialising
                                    add_shapes_to_canvas(
                                        current_toolpath_shapes,
                                        breaks=current_toolpath_breaks,
                                        entity_types=current_toolpath_types,
                                    )
                                    # Re-apply any notches the user toggled before the reconnect.
                                    if current_toolpath_notches:
                                        await asyncio.sleep(0.1)
                                        try:
                                            await ui.run_javascript(
                                                f'window.toolpathCanvas.restoreNotches({json.dumps(current_toolpath_notches)})',
                                                timeout=5.0,
                                            )
                                        except Exception as exc:
                                            logger.warning(f'Could not restore notches after reconnect: {exc}')
                                    # Also restore from the browser's localStorage snapshot.
                                    # This catches notches the user toggled while the socket
                                    # was DOWN (events were lost), since the JS writes to
                                    # localStorage on every change regardless of socket state.
                                    try:
                                        await ui.run_javascript(
                                            'window.toolpathCanvas.restoreNotchesFromLocalStorage()',
                                            timeout=5.0,
                                        )
                                    except Exception as exc:
                                        logger.warning(f'Could not restore notches from localStorage: {exc}')
                                    if client_job_state['toolpath_generated']:
                                        await asyncio.sleep(0.2)
                                        try:
                                            await ui.run_javascript(
                                                'window.toolpathCanvas.fetchAndShowToolpath()',
                                                timeout=15.0
                                            )
                                        except Exception:
                                            pass
                            
                            # Schedule initialization
                            ui.timer(0.5, init_canvas_after_load, once=True)
                            
                            # Handle shape moved events from JavaScript
                            def on_shape_moved(e):
                                global current_toolpath_shapes
                                # e.args contains the data passed from emitEvent
                                data = e.args if isinstance(e.args, dict) else (e.args[0] if e.args else {})
                                shape_name = data.get('shapeName') if isinstance(data, dict) else None
                                new_points = data.get('newPoints') if isinstance(data, dict) else None
                                
                                # Debug logging
                                if new_points:
                                    x_vals = [p[0] for p in new_points]
                                    y_vals = [p[1] for p in new_points]
                                    logger.info(f"SHAPE MOVE DEBUG: Received {len(new_points)} points")
                                    logger.info(f"SHAPE MOVE DEBUG: X range: {min(x_vals):.1f} to {max(x_vals):.1f}")
                                    logger.info(f"SHAPE MOVE DEBUG: Y range: {min(y_vals):.1f} to {max(y_vals):.1f}")
                                
                                if shape_name and new_points:
                                    # Update the stored shapes with new positions
                                    current_toolpath_shapes[shape_name] = [tuple(p) for p in new_points]
                                    _mark_canvas_dirty()
                                    logger.info(f"Shape '{shape_name}' moved to new position")
                                    try:
                                        xs = [p[0] for p in new_points]
                                        ys = [p[1] for p in new_points]
                                        log_event('canvas', 'shape_moved', shape=shape_name,
                                                  bbox=[min(xs), min(ys), max(xs), max(ys)],
                                                  point_count=len(new_points))
                                    except Exception:
                                        log_event('canvas', 'shape_moved', shape=shape_name)
                            
                            ui.on('shape_moved', on_shape_moved)
                            
                            # Handle nest diagnostic events from JavaScript (timing checkpoints,
                            # uncaught errors, etc.). All logged to app.log for post-mortem.
                            def on_nest_diagnostic(e):
                                try:
                                    data = e.args if isinstance(e.args, dict) else (e.args[0] if e.args else {})
                                except Exception:
                                    data = {}
                                checkpoint = data.get('checkpoint', '?') if isinstance(data, dict) else '?'
                                logger.info(f"[NEST JS] {checkpoint}: {data}")
                                try:
                                    log_event('transform', 'nest_diagnostic', **data) if isinstance(data, dict) else None
                                except Exception:
                                    pass
                            
                            ui.on('nest_diagnostic', on_nest_diagnostic)
                            
                            # Handle shape deleted events from JavaScript
                            def on_shape_deleted(e):
                                global current_toolpath_shapes
                                data = e.args if isinstance(e.args, dict) else (e.args[0] if e.args else {})
                                shape_name = data.get('shapeName') if isinstance(data, dict) else None
                                
                                if shape_name and shape_name in current_toolpath_shapes:
                                    del current_toolpath_shapes[shape_name]
                                    current_toolpath_breaks.pop(shape_name, None)
                                    current_toolpath_types.pop(shape_name, None)
                                    current_toolpath_notches.pop(shape_name, None)
                                    _mark_canvas_dirty()
                                    logger.info(f"Shape '{shape_name}' deleted from toolpath shapes")
                                    log_event('canvas', 'shape_deleted', shape=shape_name)
                                    # Clear toolpath if it was generated since shapes changed
                                    if client_job_state['toolpath_generated']:
                                        machine_state.set_toolpath_generated(False)
                                        client_job_state['toolpath_generated'] = False
                                        ui.run_javascript('window.toolpathCanvas.clearToolpath()')
                                        ui.notify('Toolpath cleared because a shape was deleted.', type='info')
                            
                            ui.on('shape_deleted', on_shape_deleted)

                            # Generic canvas action emitter from JavaScript — catches
                            # transforms, copy/paste, undo, zoom, notch add/remove, etc.
                            def on_canvas_action(e):
                                data = e.args if isinstance(e.args, dict) else (e.args[0] if e.args else {})
                                if not isinstance(data, dict):
                                    data = {}
                                action = data.pop('action', 'unknown')
                                if action in _CANVAS_MUTATING_ACTIONS:
                                    _mark_canvas_dirty()
                                log_event('canvas', action, **data)

                            ui.on('canvas_action', on_canvas_action)

                            # Handle notch mode auto-disabled from JS (clear/delete/toolpath)
                            def on_notch_mode_changed(e):
                                data = e.args if isinstance(e.args, dict) else (e.args[0] if e.args else {})
                                if isinstance(data, dict) and not data.get('active', True):
                                    deactivate_notch_btn()
                                log_event('canvas', 'notch_mode_changed',
                                          active=bool(data.get('active', False)) if isinstance(data, dict) else False)

                            ui.on('notch_mode_changed', on_notch_mode_changed)

                            def on_measure_mode_changed(e):
                                data = e.args if isinstance(e.args, dict) else (e.args[0] if e.args else {})
                                if isinstance(data, dict) and not data.get('active', True):
                                    deactivate_measure_btn()
                                log_event('canvas', 'measure_mode_changed',
                                          active=bool(data.get('active', False)) if isinstance(data, dict) else False)

                            ui.on('measure_mode_changed', on_measure_mode_changed)

                            # Persist notch toggles server-side so they survive a
                            # browser refresh / Socket.IO reconnect. The JS emits
                            # the FULL current notch list for the shape on every
                            # toggle (simpler than diffing).
                            def on_notches_changed(e):
                                data = e.args if isinstance(e.args, dict) else (e.args[0] if e.args else {})
                                if not isinstance(data, dict):
                                    return
                                shape_name = data.get('shapeName')
                                notches = data.get('notches') or []
                                if not shape_name:
                                    return
                                if notches:
                                    current_toolpath_notches[shape_name] = [dict(n) for n in notches]
                                else:
                                    current_toolpath_notches.pop(shape_name, None)
                                _mark_canvas_dirty()

                            ui.on('notches_changed', on_notches_changed)
                        
                        # Right column: Jog Controls only (fixed width)
                        with ui.column().classes('gap-2 items-center').style('flex: 0 0 320px; padding: 0 0 10px 0; box-sizing: border-box;'):
                            # Control section - Jog wheel and controls
                            ui.label('Control').classes('text-body1 font-bold w-full text-center').style('color: #aaa; background-color: #2a2a2a; padding: 6px 10px; border-radius: 4px; height: 48px; display: flex; align-items: center; justify-content: center; box-sizing: border-box;')
                            create_jog_controls()
            
            # GCODE tab - Manual G-code command interface
            with ui.tab_panel(gcode_tab).classes('tab-content remote-control-lock'):
                with ui.card().classes('w-full h-full').style('padding: 12px; box-sizing: border-box;'):
                    ui.label('Manual G-code Commands').classes('text-body1 font-bold mb-2').style('color: #aaa;')
                    
                    # Command input
                    with ui.row().classes('w-full gap-2 items-center mb-3'):
                        gcode_input = ui.input('Enter G-code command').classes('flex-1').props('outlined dense')
                        
                        async def send_gcode():
                            if _deny_cloudflare_control():
                                return
                            cmd = gcode_input.value.strip()
                            if cmd:
                                response_log.push(f'>>> {cmd}')
                                log_event('manual_gcode', 'send', command=cmd)
                                response = cnc_controller.send_manual_command(cmd, timeout=10.0)
                                for line in response.split('\n'):
                                    response_log.push(f'<<< {line}')
                                log_event('manual_gcode', 'response', command=cmd,
                                          response=response[:500])
                                gcode_input.value = ''
                        
                        ui.button('Send', on_click=send_gcode, icon='send').props('color=primary dense')
                    
                    # Common commands
                    ui.label('Quick Commands:').classes('text-body2 mb-1').style('color: #888;')
                    with ui.row().classes('gap-1 mb-3'):
                        ui.button('M115', on_click=lambda: [gcode_input.set_value('M115'), send_gcode()]).props('dense outline').style('font-size: 11px;').tooltip('Send M115 to request firmware identity and capabilities')
                        ui.button('M114', on_click=lambda: [gcode_input.set_value('M114'), send_gcode()]).props('dense outline').style('font-size: 11px;').tooltip('Send M114 to report the controller’s current axis positions')
                        ui.button('M503', on_click=lambda: [gcode_input.set_value('M503'), send_gcode()]).props('dense outline').style('font-size: 11px;').tooltip('Send M503 to report the controller’s active configuration')
                    
                    # Response log
                    ui.label('Response Log:').classes('text-body2 mb-1').style('color: #888;')
                    response_log = ui.log().classes('w-full').style('height: 280px; font-family: var(--font-mono); font-size: 14px;')
                    
                    # Allow enter key to send command
                    gcode_input.on('keydown.enter', send_gcode)
            
            # System tab - WiFi, connection info, and system controls
            with ui.tab_panel(wifi_tab).classes('tab-content'):
                with ui.card().classes('w-full h-full').style('padding: 12px; box-sizing: border-box;'):
                    with ui.row().classes('w-full gap-6'):
                        # Left column: Connection info
                        with ui.column().classes('gap-3').style('flex: 1;'):
                            ui.label('Connection Info').classes('text-body1 font-bold mb-1').style('color: #aaa;')
                            
                            # Connection status
                            with ui.row().classes('items-center gap-2'):
                                ui.label('CNC Status:').classes('text-body2').style('color: #888;')
                                sys_connection_icon = ui.icon('check_circle', color='green', size='20px')
                                sys_connection_label = ui.label('Connected').classes('text-body1 font-bold')
                                
                                def update_sys_connection():
                                    if cnc_controller.connected:
                                        sys_connection_icon.props('name=check_circle color=green')
                                        sys_connection_label.set_text('Connected')
                                    else:
                                        sys_connection_icon.props('name=cancel color=red')
                                        sys_connection_label.set_text('Disconnected')
                                
                                ui.timer(1.0, update_sys_connection)
                            
                            # IP Address
                            local_ip = get_local_ip()
                            with ui.row().classes('items-center gap-2'):
                                ui.label('IP Address:').classes('text-body2').style('color: #888;')
                                ui.label(f'http://{local_ip}:8080').classes('text-body1 font-bold px-2 py-1 rounded').style('background: #2a2a2a;')
                            
                            ui.separator().classes('my-3')
                            
                            ui.label('System Controls').classes('text-body1 font-bold mb-1').style('color: #aaa;')
                            
                            with ui.row().classes('gap-2'):
                                async def restart_service():
                                    if _deny_cloudflare_control():
                                        return
                                    ui.notify('Restarting the service…', type='warning')
                                    await ui.run_javascript('setTimeout(() => window.location.reload(), 5000)')
                                    import sys
                                    sys.exit(0)
                                
                                ui.button('Restart Service', icon='refresh', on_click=restart_service) \
                                    .props('color=warning dense').style('font-size: 13px;').classes('remote-control-lock')
                                
                                def reboot_system():
                                    if _deny_cloudflare_control():
                                        return
                                    ui.notify('Rebooting the system…', type='warning')
                                    subprocess.Popen(['sudo', 'reboot'], 
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                
                                ui.button('Reboot System', icon='restart_alt', on_click=reboot_system) \
                                    .props('color=negative dense').style('font-size: 13px;').classes('remote-control-lock')

                            ui.separator().classes('my-3')

                            ui.label('Security').classes('text-body1 font-bold mb-1').style('color: #aaa;')

                            async def change_ui_password():
                                with ui.dialog() as password_dialog, ui.card().style(_DIALOG_CARD_STYLE):
                                    ui.label('Change UI Password').classes('text-h6 font-bold')
                                    current_input = ui.input(
                                        'Current password', password=True, password_toggle_button=True,
                                    ).props('outlined dense').classes('w-full')
                                    new_input = ui.input(
                                        'New password', password=True, password_toggle_button=True,
                                    ).props('outlined dense').classes('w-full')
                                    confirm_input = ui.input(
                                        'Confirm new password', password=True, password_toggle_button=True,
                                    ).props('outlined dense').classes('w-full')

                                    async def save_new_password():
                                        global _BOOT_TOKEN
                                        if not verify_password(current_input.value or '', _APP_PASSWORD):
                                            ui.notify('Current password is incorrect.', type='negative')
                                            return
                                        new_password = new_input.value or ''
                                        if new_password != (confirm_input.value or ''):
                                            ui.notify('New passwords do not match.', type='negative')
                                            return
                                        try:
                                            await asyncio.get_event_loop().run_in_executor(
                                                None, set_password, new_password,
                                            )
                                        except (OSError, ValueError) as exc:
                                            ui.notify(str(exc), type='negative')
                                            return

                                        # Invalidate other signed login sessions. Keep this
                                        # browser authenticated under the new boot token.
                                        _BOOT_TOKEN = secrets.token_hex(16)
                                        app.storage.user['authenticated'] = True
                                        app.storage.user['boot_token'] = _BOOT_TOKEN
                                        log_event('security', 'ui_password_changed')
                                        password_dialog.close()
                                        ui.notify('UI password changed.', type='positive')

                                    with ui.row().classes('w-full justify-end gap-2'):
                                        ui.button('Cancel', on_click=password_dialog.close).props('flat')
                                        ui.button('Change Password', on_click=save_new_password, icon='password') \
                                            .props('color=primary')

                                password_dialog.open()
                                await password_dialog

                            ui.button('Change UI Password', icon='password', on_click=change_ui_password) \
                                .props('color=primary dense outline').style('font-size: 13px;')

                            ui.separator().classes('my-3')

                            ui.label('Homing Sensitivity').classes('text-body1 font-bold mb-1').style('color: #aaa;')
                            ui.label('Sensorless stall sensitivity (M914, TMC5160): negative = more sensitive, positive = less sensitive.').classes('text-caption').style('color: #666;')

                            with ui.row().classes('w-full gap-2 items-end'):
                                homing_x_input = ui.number('X Stall', value=None, format='%.0f') \
                                    .props('outlined dense') \
                                    .style('width: 110px;')
                                homing_y_input = ui.number('Y Stall', value=None, format='%.0f') \
                                    .props('outlined dense') \
                                    .style('width: 110px;')
                            ui.label('Applied values are automatically saved to EEPROM (M500).').classes('text-caption').style('color: #666;')

                            homing_response_log = ui.log().classes('w-full').style(
                                'height: 96px; font-family: var(--font-mono); font-size: 12px; '
                                'background: #111; color: #d4d4d4; border-radius: 6px; padding: 6px;'
                            )

                            async def read_homing_sensitivity():
                                if not _require_controller_connected():
                                    ui.notify('The controller is not connected.', type='warning')
                                    return
                                ui.notify('Reading homing sensitivity…', type='info')
                                loop = asyncio.get_event_loop()
                                response = await loop.run_in_executor(None, cnc_controller.read_homing_sensitivity)
                                homing_response_log.push('>>> M914')
                                for line in response.split('\n'):
                                    if line.strip():
                                        homing_response_log.push(f'<<< {line}')
                                values = parse_homing_sensitivity_response(response)
                                if 'X' in values:
                                    homing_x_input.set_value(values['X'])
                                if 'Y' in values:
                                    homing_y_input.set_value(values['Y'])
                                if not {'X', 'Y'}.issubset(values):
                                    ui.notify(
                                        'Controller response did not include both X and Y sensitivity values.',
                                        type='warning', timeout=8000,
                                    )
                                log_event('system', 'homing_sensitivity_read', response=response[:800])

                            async def apply_homing_sensitivity():
                                if not _require_controller_connected():
                                    ui.notify('The controller is not connected.', type='warning')
                                    return
                                try:
                                    x_val = _parse_homing_sensitivity_value(homing_x_input.value, 'X')
                                    y_val = _parse_homing_sensitivity_value(homing_y_input.value, 'Y')
                                except ValueError as e:
                                    ui.notify(str(e), type='warning')
                                    return

                                persist = True
                                log_event(
                                    'system',
                                    'homing_sensitivity_apply_clicked',
                                    x=x_val,
                                    y=y_val,
                                    persist=persist,
                                )
                                ui.notify('Applying homing sensitivity…', type='info')
                                loop = asyncio.get_event_loop()
                                ok, response = await loop.run_in_executor(
                                    None,
                                    lambda: cnc_controller.set_homing_sensitivity(
                                        x=x_val,
                                        y=y_val,
                                        persist_to_eeprom=persist,
                                    ),
                                )
                                homing_response_log.push(f'>>> M914 X{x_val} Y{y_val}')
                                if persist:
                                    homing_response_log.push('>>> M500')
                                for line in response.split('\n'):
                                    if line.strip():
                                        homing_response_log.push(f'<<< {line}')

                                if ok:
                                    ui.notify('Homing sensitivity applied.', type='positive')
                                    log_event(
                                        'system',
                                        'homing_sensitivity_applied',
                                        x=x_val,
                                        y=y_val,
                                        persist=persist,
                                    )
                                else:
                                    logger.warning('Failed to apply homing sensitivity: %s', response)
                                    ui.notify('Homing sensitivity could not be applied. Review the controller response, then try again.', type='negative', timeout=8000)
                                    log_event(
                                        'system',
                                        'homing_sensitivity_apply_failed',
                                        x=x_val,
                                        y=y_val,
                                        persist=persist,
                                        response=response[:800],
                                    )

                            with ui.row().classes('gap-2'):
                                ui.button('Read Current', icon='manage_search', on_click=read_homing_sensitivity) \
                                    .props('dense outline').style('font-size: 12px;')
                                ui.button('Apply', icon='tune', on_click=apply_homing_sensitivity) \
                                    .props('dense color=warning').style('font-size: 12px; color: #111;')

                            ui.separator().classes('my-3')

                            ui.label('Debug').classes('text-body1 font-bold mb-1').style('color: #aaa;')

                            async def send_logs_to_dev():
                                ui.notify('Sending logs…', type='info')
                                loop = asyncio.get_event_loop()
                                result = await loop.run_in_executor(None, log_uploader.upload_now, False, "manual")
                                if result.get('ok'):
                                    ui.notify(
                                        f"Logs sent — {result.get('bytes', 0)//1024} KB",
                                        type='positive',
                                    )
                                else:
                                    logger.warning('Log upload failed: %s', result.get('error'))
                                    ui.notify(_safe_error_message('The logs could not be sent.'), type='negative', timeout=8000)

                            with ui.row().classes('gap-2'):
                                ui.button('Send Logs to Dev', icon='cloud_upload', on_click=send_logs_to_dev) \
                                    .props('color=primary dense outline').style('font-size: 13px;')
                                ui.button('Download Logs', icon='download', on_click=lambda: ui.navigate.to('/debug-bundle', new_tab=True)) \
                                    .props('color=secondary dense outline').style('font-size: 13px;')

                            log_cfg = load_logging_config()
                            ui.label(f"Log dir: {log_cfg['log_dir']}").classes('text-caption').style('color: #666; margin-top: 6px;')

                        # Right column: live Pi terminal
                        with ui.column().classes('gap-2 remote-control-lock').style('flex: 1;'):
                            ui.label('Terminal').classes('text-body1 font-bold mb-1').style('color: #aaa;')
                            ui.label('Runs shell commands directly on the Pi.').classes('text-caption').style('color: #666;')

                            terminal_log = ui.log().classes('w-full').style(
                                'height: 320px; font-family: var(--font-mono); font-size: 13px; '
                                'background: #111; color: #d4d4d4; border-radius: 6px; padding: 8px;'
                            )

                            # PTY-backed persistent shell. A pseudo-terminal gives bash
                            # a real controlling TTY so sudo password prompts and other
                            # interactive programs work correctly.
                            #
                            # Full ANSI/VT strip:
                            #   - OSC  \x1B ] ... BEL  — window title, colour palette, etc.
                            #   - CSI  \x1B [ ...      — colours, cursor movement
                            #   - Two-char \x1B X      — everything else (SS2, SS3, …)
                            #   - Standalone BEL / DEL
                            _ANSI_ESC = re.compile(
                                r'\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)'  # OSC (title etc.)
                                r'|\x1B\[[0-?]*[ -/]*[@-~]'            # CSI (colors, cursor)
                                r'|\x1B[@-Z\\-_]'                      # other 2-char escapes
                                r'|[\x07\x08\x7f]'                     # BEL, BS, DEL
                            )
                            # Matches a typical shell prompt tail: ends with "$ " or "# "
                            _PROMPT_RE = re.compile(r'[$#]\s*$')

                            shell_state: dict = {
                                'proc': None, 'master_fd': None,
                                '_buf': '',             # partial-line accumulator
                                '_prompt': '',          # pending shell prompt held for combining
                                '_suppress_echo': None, # command whose PTY echo should be dropped
                                '_flush_handle': None,  # call_later handle for non-prompt flush
                            }

                            def _start_shell():
                                # Clean up any dead previous session
                                old_fd = shell_state.get('master_fd')
                                if old_fd is not None:
                                    try:
                                        asyncio.get_event_loop().remove_reader(old_fd)
                                        os.close(old_fd)
                                    except OSError:
                                        pass
                                h = shell_state.get('_flush_handle')
                                if h:
                                    h.cancel()

                                master_fd, slave_fd = os.openpty()
                                # 24 rows × 160 cols — generous width avoids line-wrap artifacts
                                fcntl.ioctl(slave_fd, termios.TIOCSWINSZ,
                                            struct.pack('HHHH', 24, 160, 0, 0))

                                proc = subprocess.Popen(
                                    ['bash'],
                                    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                                    close_fds=True,
                                    start_new_session=True,
                                    cwd=str(REPO_DIR),
                                    env={
                                        **os.environ,
                                        'TERM': 'xterm-256color',
                                        'PS1': r'\u@\h:\w\$ ',
                                        # Prevent .bashrc from reinstalling title sequences
                                        'PROMPT_COMMAND': '',
                                    },
                                )
                                os.close(slave_fd)  # parent only needs master end

                                # Non-blocking reads so add_reader never stalls
                                flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
                                fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

                                shell_state.update({
                                    'proc': proc, 'master_fd': master_fd,
                                    '_buf': '', '_prompt': '',
                                    '_suppress_echo': None, '_flush_handle': None,
                                })

                                def _on_readable():
                                    try:
                                        data = os.read(master_fd, 4096)
                                    except (BlockingIOError, OSError):
                                        return

                                    # Cancel any pending non-prompt flush
                                    h = shell_state['_flush_handle']
                                    if h:
                                        h.cancel()
                                        shell_state['_flush_handle'] = None

                                    shell_state['_buf'] += data.decode('utf-8', errors='replace')
                                    lines = shell_state['_buf'].split('\n')
                                    shell_state['_buf'] = lines[-1]  # keep partial tail

                                    for raw in lines[:-1]:
                                        clean = _ANSI_ESC.sub('', raw).rstrip('\r')
                                        # Drop the PTY echo of the last sent command
                                        if (shell_state['_suppress_echo'] is not None
                                                and clean == shell_state['_suppress_echo']):
                                            shell_state['_suppress_echo'] = None
                                            continue
                                        if clean:
                                            terminal_log.push(clean)

                                    # Classify the partial line sitting in the buffer
                                    partial = _ANSI_ESC.sub(
                                        '', shell_state['_buf']).rstrip('\r').rstrip()
                                    if not partial:
                                        return

                                    if _PROMPT_RE.search(partial):
                                        # Looks like a shell prompt — hold it so we can
                                        # combine it with the next command on one line.
                                        shell_state['_prompt'] = partial
                                        shell_state['_buf'] = ''
                                    else:
                                        # Other partial line (sudo password prompt, etc.)
                                        # — flush to the log after a short delay.
                                        def _do_flush():
                                            buf = shell_state['_buf']
                                            shell_state['_buf'] = ''
                                            shell_state['_flush_handle'] = None
                                            clean = _ANSI_ESC.sub('', buf).rstrip('\r')
                                            if clean:
                                                terminal_log.push(clean)
                                        shell_state['_flush_handle'] = (
                                            asyncio.get_event_loop().call_later(0.25, _do_flush)
                                        )

                                asyncio.get_event_loop().add_reader(master_fd, _on_readable)

                            def _ensure_shell():
                                proc = shell_state.get('proc')
                                if proc is None or proc.poll() is not None:
                                    _start_shell()

                            async def run_terminal_command():
                                if _deny_cloudflare_control():
                                    return
                                cmd = term_input.value.rstrip('\n')
                                _ensure_shell()

                                # Cancel any pending non-prompt flush and grab its buffer
                                h = shell_state.get('_flush_handle')
                                if h:
                                    h.cancel()
                                    shell_state['_flush_handle'] = None
                                extra = shell_state.get('_buf', '').strip()
                                if extra:
                                    terminal_log.push(_ANSI_ESC.sub('', shell_state['_buf']).rstrip('\r'))
                                    shell_state['_buf'] = ''

                                # Combine the pending shell prompt with the typed command
                                # so the log reads:  user@host:~$ ls
                                prompt = shell_state.get('_prompt', '')
                                shell_state['_prompt'] = ''

                                if prompt:
                                    terminal_log.push(f'{prompt}{cmd}')
                                    # Suppress the PTY line-discipline echo that will follow
                                    shell_state['_suppress_echo'] = cmd if cmd.strip() else None
                                # If no prompt is pending (e.g. sudo password input) we show
                                # nothing — avoids echoing sensitive text into the log.

                                log_event('system', 'terminal_command',
                                          command=cmd[:500] if cmd.strip() else '[enter]')
                                try:
                                    os.write(shell_state['master_fd'], (cmd + '\n').encode())
                                except OSError as exc:
                                    terminal_log.push(f'[terminal error] {exc}')
                                term_input.value = ''

                            with ui.row().classes('w-full gap-2 items-center'):
                                ui.label('$').style('color: var(--md-accent-green); font-family: var(--font-mono); font-size: 16px;')
                                term_input = ui.input(placeholder='Type a command…') \
                                    .classes('flex-1').props('outlined dense autocomplete=off autocapitalize=off spellcheck=false')
                                ui.button('Run', icon='play_arrow', on_click=run_terminal_command) \
                                    .props('color=primary dense')

                            async def _run_quick_command(cmd: str):
                                term_input.value = cmd
                                await run_terminal_command()

                            quick_commands = [
                                ('View Logs', 'sudo journalctl -u fabcnc -n 200 --no-pager', 'article'),
                                ('Service Status', 'sudo systemctl status fabcnc', 'info'),
                                ('Restart Service', 'sudo systemctl restart fabcnc', 'restart_alt'),
                                ('List USB', 'lsusb', 'usb'),
                                ('Serial Ports', 'ls -la /dev/ttyACM* /dev/ttyUSB* 2>/dev/null', 'cable'),
                            ]
                            with ui.row().classes('gap-1 flex-wrap'):
                                for label, cmd, icon in quick_commands:
                                    ui.button(label, icon=icon, on_click=lambda c=cmd: _run_quick_command(c)) \
                                        .props('dense outline').style('font-size: 11px;')

                            with ui.row().classes('gap-1'):
                                ui.button('Clear', icon='clear_all', on_click=lambda: terminal_log.clear()) \
                                    .props('dense outline').style('font-size: 11px;')

                            term_input.on('keydown.enter', run_terminal_command)

                            # Start the shell immediately so the prompt appears on page load
                            ui.timer(0.1, _start_shell, once=True)

        # Per-page state for disconnect recovery and resume dialog
        _prev_status_local: list = [None]
        _resume_dialog_shown: list = [False]
        _was_disconnected: list = [False]   # set on any 'Disconnected' status, cleared on resume/discard
        _connection_alert_dismissed: list = [False]   # user closed the popup; reopen via the status pill
        _prev_controls_disabled_look: list = [False]  # last CSS-toggled disconnected-look state sent to browser

        # Dimmed backdrop so the hand-rolled alert cards below stand out from the page
        # instead of blending into the dark theme.
        alert_backdrop = ui.element('div').style(
            'position: fixed; inset: 0; background: var(--md-overlay); z-index: 5000; '
            'display: none; pointer-events: none;'
        )

        def _set_alert_backdrop(visible: bool) -> None:
            alert_backdrop.style(f'display: {"block" if visible else "none"};')

        connection_alert = ui.card().style(
            'position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 430px; z-index: 5001; '
            'background: #3a1414; border: 2px solid #e05252; color: #fff; '
            'padding: 16px 18px; border-radius: 10px; box-shadow: 0 12px 32px rgba(0,0,0,0.65);'
        )
        with connection_alert:
            with ui.row().classes('items-start gap-3 no-wrap w-full'):
                connection_alert_icon = ui.icon('usb_off', size='28px').style('color: #ff6b6b; margin-top: 2px;')
                with ui.column().classes('gap-1 w-full'):
                    connection_alert_title = ui.label('Controller disconnected') \
                        .classes('text-h6').style('color: var(--md-text-primary);')
                    connection_alert_subtitle = ui.label('') \
                        .classes('text-caption').style('color: #ffb4b4; white-space: normal;')

                def _dismiss_connection_alert():
                    _connection_alert_dismissed[0] = True
                    connection_alert.set_visibility(False)
                    if not update_alert.visible:
                        _set_alert_backdrop(False)

                ui.button(icon='close', on_click=_dismiss_connection_alert) \
                    .props('flat dense round').style('color: #aaa; margin: -4px -4px 0 0;')
            connection_alert_steps = ui.label('').style(
                'white-space: pre-line; color: var(--md-text-primary); font-size: 14px; line-height: 1.45; margin-top: 10px;'
            )
            with ui.row().classes('w-full justify-end gap-2').style('margin-top: 14px;'):
                retry_connection_button = ui.button('Retry Connection', on_click=retry_controller_connection) \
                    .props('dense outline color=warning').style('font-size: 12px;').classes('remote-control-lock')

                def _confirm_force_controller_recovery():
                    with ui.dialog().props('persistent') as recovery_dialog, \
                         ui.card().style(_DIALOG_CARD_STYLE):
                        ui.label('Recover the controller?') \
                            .classes('text-h6 font-bold').style('margin-bottom:6px;')
                        ui.label(
                            'Confirm the cutter is fully stopped and the physical E-stop is released. '
                            'This may reset Marlin and abort any SD-card job still in progress.'
                        ).classes('text-body2').style('color:#ccc; white-space:normal;')
                        with ui.row().classes('gap-2 justify-end w-full').style('margin-top:16px;'):
                            ui.button('Cancel', on_click=lambda: recovery_dialog.close()).props('flat') \
                                .style('color:#aaa;')

                            async def _run_forced_recovery():
                                recovery_dialog.close()
                                force_recovery_button.disable()
                                try:
                                    await force_controller_recovery()
                                finally:
                                    force_recovery_button.enable()

                            ui.button(
                                'Cutter Stopped — Continue',
                                icon='restart_alt',
                                on_click=_run_forced_recovery,
                            ).props('color=negative dense')
                    recovery_dialog.open()

                force_recovery_button = ui.button(
                    'Force Recovery',
                    icon='restart_alt',
                    on_click=_confirm_force_controller_recovery,
                ).props('dense color=negative').style('font-size: 12px;').classes('remote-control-lock')
                force_recovery_button.set_visibility(False)
        connection_alert.set_visibility(False)

        update_alert = ui.card().style(
            'position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 430px; z-index: 9998; '
            'background: #12301c; border: 2px solid #4fd07a; color: #fff; '
            'padding: 16px 18px; border-radius: 10px; box-shadow: 0 12px 32px rgba(0,0,0,0.65);'
        )
        with update_alert:
            with ui.row().classes('items-start gap-3 no-wrap w-full'):
                ui.icon('system_update_alt', size='28px').style('color: #66bb6a; margin-top: 2px;')
                with ui.column().classes('gap-1 w-full'):
                    ui.label('Software update available') \
                        .classes('text-h6').style('color: var(--md-text-primary);')
                    ui.label('A software update is ready to install.') \
                        .classes('text-caption').style('color: #b7dfc1; white-space: normal;')
            ui.label('Acknowledge this notice or install the update now.') \
                .style('white-space: pre-line; color: var(--md-text-primary); font-size: 14px; line-height: 1.45; margin-top: 10px;')
            with ui.row().classes('w-full justify-end gap-2').style('margin-top: 14px;'):
                acknowledge_update_button = ui.button('Acknowledge', on_click=acknowledge_software_update).props('flat dense') \
                    .style('color: #aaa;')
                update_now_button = ui.button('Update Now').props('dense color=positive').style('font-size: 12px; color: #111;')

                async def _do_update_now():
                    update_now_button.set_text('Updating…')
                    update_now_button.props('icon=hourglass_top')
                    update_now_button.disable()
                    acknowledge_update_button.disable()
                    await do_software_update()

                update_now_button.on_click(_do_update_now)
        update_alert.set_visibility(False)

        def _show_connection_alert() -> None:
            connection_alert.style('background: #3a1414; border: 2px solid #e05252;')
            if cnc_controller.connected and cnc_controller.serial_uncertain:
                connection_alert_icon.props('name=warning color=orange-4')
                connection_alert_title.set_text('Machine state uncertain')
                connection_alert_subtitle.set_text('Automatic Stop verification did not complete:')
                connection_alert_steps.set_text(
                    '1. Confirm the cutter is fully stopped.\n'
                    '2. Click "Verify Controller" below.\n'
                    '3. After verification succeeds, run Home All.'
                )
                retry_connection_button.set_visibility(False)
                force_recovery_button.set_text('Verify Controller')
            elif cnc_controller.sd_job_disconnected:
                connection_alert_icon.props('name=usb_off color=red-4')
                connection_alert_title.set_text('Controller disconnected')
                connection_alert_subtitle.set_text('The SD-card job may still be running:')
                connection_alert_steps.set_text(
                    'Do not power-cycle the controller while the cutter may be moving.\n'
                    'Check the machine, restore the USB connection when safe, then verify the job state.'
                )
                retry_connection_button.set_visibility(True)
                force_recovery_button.set_text('Force Reconnect')
            else:
                connection_alert_icon.props('name=usb_off color=red-4')
                connection_alert_title.set_text('Controller disconnected')
                connection_alert_subtitle.set_text('Follow these steps to reconnect:')
                connection_alert_steps.set_text(
                    '1. Disengage E-Stop.\n'
                    '2. Power cycle the fabCNC control box (red switch).\n'
                    '3. Click "Retry Connection".'
                )
                retry_connection_button.set_visibility(True)
                force_recovery_button.set_text(
                    'Reset Controller' if cnc_controller.reset_required else 'Force Reconnect'
                )
            force_recovery_button.set_visibility(True)
            connection_alert.set_visibility(True)
            # Keep the dimming backdrop visible, but click-through so the canvas
            # and file controls remain usable until the alert is dismissed.
            _set_alert_backdrop(True)

        def _reopen_connection_alert():
            """Let the operator bring back a dismissed disconnect popup via the status pill."""
            current_status = machine_state.snapshot().status_text
            if current_status == 'SD job ended while disconnected — verify machine':
                ui.notify(current_status, type='warning', timeout=8000)
                return
            if (
                not cnc_controller.connected
                or current_status in ('Disconnected', 'Retrying connection...')
                or cnc_controller.reset_required
                or cnc_controller.serial_uncertain
                or current_status == 'E-Stop Reset Required'
            ):
                _connection_alert_dismissed[0] = False
                _show_connection_alert()

        status_pill.style('cursor: pointer;')
        status_pill.on('click', _reopen_connection_alert)

        def _show_resume_dialog():
            _resume_dialog_shown[0] = True
            with ui.dialog().props('persistent') as dlg, \
                 ui.card().style(_DIALOG_CARD_STYLE):
                ui.label('Resume interrupted job?') \
                    .classes('text-h6 font-bold').style('margin-bottom:6px;')
                ui.label(
                    'The controller reconnected. The machine will re-home then continue from the last safe position.'
                ).classes('text-body2').style('color:#aaa;')
                with ui.row().classes('gap-2 justify-end w-full').style('margin-top:16px;'):
                    ui.button('Discard', on_click=lambda: (
                        cnc_controller.clear_resume_state(), dlg.close()
                    )).props('flat').style('color:#aaa;')

                    async def _do_resume():
                        dlg.close()
                        await resume_disconnect_job()

                    ui.button(
                        'Resume Job', icon='settings_backup_restore',
                        on_click=_do_resume,
                    ).style('background:#FFA726; color:#000;').classes('remote-control-lock')
            dlg.open()

        # Start periodic UI update timer (10 Hz = 100ms)
        async def _update_ui_timer():
            await update_ui(pos_labels, status_label, status_pill, status_icon)

            # A move/control command was attempted while disconnected — force the
            # alert back open even if the operator had previously dismissed it.
            if disconnect_alert_state['requested']:
                disconnect_alert_state['requested'] = False
                _connection_alert_dismissed[0] = False

            current_status = machine_state.status_text
            prev = _prev_status_local[0]
            needs_disconnect_alert = (
                (not cnc_controller.connected)
                or current_status in ('Disconnected', 'Retrying connection...')
                or cnc_controller.reset_required
                or cnc_controller.serial_uncertain
                or current_status == 'E-Stop Reset Required'
            )
            showing_connection_alert = False

            # Grey out move/control buttons while disconnected (CSS class toggle,
            # only sent to the browser when the state actually changes).
            if needs_disconnect_alert != _prev_controls_disabled_look[0]:
                _prev_controls_disabled_look[0] = needs_disconnect_alert
                await ui.run_javascript(
                    f"document.body.classList.toggle('controller-disconnected', {str(needs_disconnect_alert).lower()})"
                )

            # Track whether we've ever seen a disconnect since this page loaded.
            if needs_disconnect_alert:
                _was_disconnected[0] = True
                _resume_dialog_shown[0] = False
                if not _connection_alert_dismissed[0]:
                    _show_connection_alert()
                    showing_connection_alert = True
            else:
                connection_alert.set_visibility(False)
                _connection_alert_dismissed[0] = False  # reset so the next disconnect shows fresh

            if update_state['available'] and not update_state['acknowledged'] and not showing_connection_alert:
                update_alert.set_visibility(True)
                _set_alert_backdrop(True)
            else:
                update_alert.set_visibility(False)
                if not showing_connection_alert:
                    _set_alert_backdrop(False)

            if prev != current_status:
                _prev_status_local[0] = current_status

            # Show resume dialog only once, and only when:
            #   - we previously saw a disconnect on this page
            #   - the controller is actually reconnected and idle now
            #   - a resume state file exists
            #   - we haven't already shown the dialog
            if (
                _was_disconnected[0]
                and not _resume_dialog_shown[0]
                and cnc_controller.connected
                and current_status == 'Idle'
            ):
                connection_alert.set_visibility(False)
                ui.notify('Controller reconnected.', type='positive')
                _was_disconnected[0] = False
                if cnc_controller.has_resume_state():
                    _show_resume_dialog()

        ui.timer(0.1, _update_ui_timer)


if __name__ in {"__main__", "__mp_main__"}:
    # Kick off the periodic log uploader (no-op when disabled in config).
    log_uploader.start_uploader()
    # Run the NiceGUI app
    # Bind to 0.0.0.0 to allow access from other computers on the network
    ui.run(
        host='0.0.0.0',
        port=8080,
        title='fabCNC Controller',
        favicon=Path(__file__).parent / 'static' / 'favicon.svg',
        dark=None,  # Auto-detect system preference
        reload=False,
        show=False,  # Don't auto-open browser (for kiosk mode)
        storage_secret=_STORAGE_SECRET,
        session_middleware_kwargs={
            'same_site': 'strict',
            'https_only': os.environ.get('FABCNC_SECURE_COOKIES', '').lower() in {'1', 'true', 'yes'},
        },
    )
