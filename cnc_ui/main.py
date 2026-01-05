# main.py
"""
NiceGUI-based CNC web controller UI.
Provides manual jogging, homing, file upload, and job execution controls.
"""

from nicegui import ui, app
from cnc.state import machine_state
from cnc.controller import cnc_controller
from cnc.files import file_manager
from pathlib import Path
from fastapi import Request
from fastapi.staticfiles import StaticFiles
import matplotlib.pyplot as plt
from dxf_processing.dxf_processor import DXFProcessor
from toolpath_planning.toolpath_generator import ToolpathGenerator
from toolpath_planning.gcode_visualizer import GCodeVisualizer
import logging
import socket
import subprocess
import os
import math
import json

# Configure logging to see all debug output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Application version
APP_VERSION = "v1.0.0"

# Mount static files directory
app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')


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
jog_params = {
    'xy_step': 10.0,  # mm
    'z_step': 1.0,    # mm
    'a_step': 45.0,   # degrees
    'feed_rate': 4000.0,  # mm/min
}

# Lock screen configuration
LOCK_PIN = "1234"  # 4-digit PIN code
lock_state = {'locked': True}  # Start locked

# DXF processing and toolpath generation
dxf_processor = DXFProcessor()
toolpath_generator = ToolpathGenerator(
    cutting_height=-26.0,  # Z height when cutting (mm)
    safe_height=-15.0,     # Z height when raised (mm)
    corner_angle_threshold=15.0,
    feed_rate=5000.0,      # mm/min (~83 mm/s)
    plunge_rate=3000.0     # mm/min
)

# Global storage for current toolpath visualization data
current_toolpath_shapes = {}
toolpath_canvas = None  # Reference to the canvas element

# API endpoint for jog control from JavaScript
@app.post('/jog')
async def jog_endpoint(request: Request):
    """Handle jog requests."""
    data = await request.json()
    axis = data['axis']
    direction = data['direction']
    
    if axis == 'X' or axis == 'Y':
        distance = jog_params['xy_step'] * direction
    elif axis == 'Z':
        distance = jog_params['z_step'] * direction
    elif axis == 'A':
        distance = jog_params['a_step'] * direction
    else:
        return {'status': 'error', 'message': 'Invalid axis'}
    
    cnc_controller.jog(axis, distance, jog_params['feed_rate'])
    return {'status': 'ok'}

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
    import asyncio
    import concurrent.futures
    
    try:
        data = await request.json()
        
        input_shapes = data.get('shapes', [])
        sheet_width = data.get('sheetWidth', 1375)
        sheet_height = data.get('sheetHeight', 875)
        offset = data.get('offset', 2)  # mm spacing
        rotations = data.get('rotations', 4)  # Try 4 rotations by default
        
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
        
        return result
        
    except Exception as e:
        logger.error(f"Nesting error: {e}")
        return {'status': 'error', 'message': str(e)}


def run_packaide_nesting(input_shapes, sheet_width, sheet_height, offset, rotations):
    """Run Packaide nesting in a separate thread to avoid blocking the event loop."""
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
        result, placed, fails = packaide.pack(
            [sheet_svg],
            shapes_svg,
            tolerance=1.0,  # Curve approximation (lower = more accurate but slower)
            offset=offset,
            partial_solution=True,
            rotations=rotations,
            persist=True
        )
        
        logger.info(f"Packaide result: placed={placed}, fails={fails}")
        
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


def create_header():
    """Create the application header with tabs, position, status, and controls."""
    pos_labels = {}
    tabs = None
    
    with ui.header().classes('items-center justify-between py-1 px-3').style('background: linear-gradient(180deg, #2a2a2a 0%, #232323 100%); min-height: 48px; flex-wrap: nowrap;'):
        # Left side: App name with icon + Tabs
        with ui.row().classes('items-center gap-4').style('flex-shrink: 0;'):
            with ui.row().classes('items-center gap-2'):
                ui.icon('precision_manufacturing', size='24px').classes('text-blue-4')
                ui.label('fabCNC').classes('text-h6 font-bold')
            
            # Tabs in header
            with ui.tabs().props('dense inline-label').classes('header-tabs').style('background: transparent;') as tabs:
                job_tab = ui.tab('Toolpath', icon='route').style('font-size: 12px; min-height: 36px; padding: 0 12px;')
                gcode_tab = ui.tab('GCODE', icon='terminal').style('font-size: 12px; min-height: 36px; padding: 0 12px;')
                wifi_tab = ui.tab('System', icon='settings').style('font-size: 12px; min-height: 36px; padding: 0 12px;')
        
        # Center: Status pill
        with ui.row().classes('items-center justify-center').style('flex: 1; min-width: 0;'):
            with ui.element('div').classes('flex items-center gap-2 px-3 py-1 rounded-full').style('background: #2d4a2d; border: 1px solid #3d5a3d;'):
                ui.icon('radio_button_checked', size='10px').classes('text-green-4')
                status_label = ui.label('Idle').classes('text-caption font-bold text-green-4')
        
        # Right side: Position display + Version
        with ui.row().classes('items-center gap-2').style('flex-shrink: 0; overflow-x: auto;'):
            for axis in ['X', 'Y', 'Z', 'A']:
                with ui.element('div').classes('flex items-center gap-1 px-2 py-1 rounded').style('background: #3a3a3a; border: 1px solid #4a4a4a;'):
                    ui.label(f'{axis}').classes('text-caption font-bold').style('color: #888; width: 12px;')
                    unit = '°' if axis == 'A' else ''
                    pos_labels[axis] = ui.label(f'0.00{unit}').classes('text-body2 font-bold').style('min-width: 65px;')
            
            ui.label(APP_VERSION).classes('text-caption ml-2').style('color: #666;')
    
    return pos_labels, status_label, tabs, job_tab, gcode_tab, wifi_tab


def create_position_display():
    """Create the compact position display."""
    pos_labels = {}
    for axis in ['X', 'Y', 'Z', 'A']:
        with ui.row().classes('items-center gap-1'):
            ui.label(f'{axis}:').classes('text-h6 text-grey-7 font-bold')
            unit = '°' if axis == 'A' else 'mm'
            pos_labels[axis] = ui.label(f'0.00 {unit}').classes('text-h6 font-bold')
    
    return pos_labels


def create_status_display():
    """Create the compact status and progress display."""
    with ui.row().classes('items-center gap-2'):
        ui.label('Status:').classes('text-h6 text-grey-7 font-bold')
        status_label = ui.label('Idle').classes('text-h6 font-bold')
    progress_bar = ui.linear_progress(value=0.0, show_value=False).style('height: 8px')
    
    return status_label, progress_bar


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
    </style>
    ''')
    
    # Main container - inline layout for toolpath panel
    with ui.column().classes('items-center gap-2'):
        
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
                
                # Register event handler
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
                ui.button('XY Zero', on_click=lambda: cnc_controller.send_command("G92 X0 Y0")).props('flat dense').style('background: #2a2a2a; color: #4a9eff; font-size: 14px; width: 236px; height: 36px; margin-top: 4px;') \
                    .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
    
    # Store button references for updating (unused in new design but kept for compatibility)
    jog_params['_buttons'] = {}


def create_homing_controls():
    """Create the compact homing control panel."""
    with ui.column().classes('gap-3'):
        ui.label('Homing').classes('text-h5 font-bold')
        
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
        
        loaded_file_label = ui.label('No file loaded').classes('text-body2').style('color: #777;')
        
        upload = ui.upload(
            label='Load DXF File',
            auto_upload=True,
            on_upload=lambda e: handle_file_upload(e, loaded_file_label)
        ).props('accept=.dxf dense').classes('w-full dxf-upload').style('font-size: 13px;')
        
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
            ui.button('Save', icon='save', on_click=save_canvas_state).props('dense flat').style('flex: 1; background-color: #2a2a2a; font-size: 12px; color: #4a9eff;').tooltip('Save canvas to file')
            ui.button('Load', icon='folder_open', on_click=load_canvas_state).props('dense flat').style('flex: 1; background-color: #2a2a2a; font-size: 12px; color: #4a9eff;').tooltip('Load saved canvas')
            ui.button('Clear', icon='delete', on_click=clear_canvas).props('dense flat').style('flex: 1; background-color: #2a2a2a; font-size: 12px; color: #4a9eff;').tooltip('Clear all shapes')
        
        return loaded_file_label


def create_job_controls():
    """Create the compact job execution control panel."""
    with ui.column().classes('w-full gap-2'):
        ui.label('Job Control').classes('text-body1 font-bold w-full text-center').style('color: #aaa; background-color: #2a2a2a; padding: 6px 10px; border-radius: 4px; height: 48px; display: flex; align-items: center; justify-content: center; box-sizing: border-box;')
        
        # Progress bar
        job_progress = ui.linear_progress(value=0, show_value=False).classes('w-full').style('height: 6px;')
        job_progress.bind_value_from(machine_state, 'job_progress')
        
        ui.button('Start', icon='play_arrow', on_click=start_job) \
            .props('dense flat') \
            .classes('w-full') \
            .style('font-size: 14px; background-color: #2a2a2a; color: #4a9eff;') \
            .bind_enabled_from(machine_state, '_lock',
                             backward=lambda _: machine_state.job_loaded and machine_state.is_idle())
        
        with ui.row().classes('w-full gap-1'):
            ui.button('Pause', icon='pause', on_click=pause_job) \
                .props('dense flat') \
                .classes('flex-1') \
                .style('font-size: 13px; background-color: #2a2a2a; color: #4a9eff;') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.is_running())
            
            ui.button('Resume', icon='play_circle', on_click=resume_job) \
                .props('dense flat') \
                .classes('flex-1') \
                .style('font-size: 13px; background-color: #2a2a2a; color: #4a9eff;') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.paused)
        
        ui.button('Stop', icon='stop', on_click=stop_job) \
            .props('dense flat') \
            .classes('w-full') \
            .style('font-size: 14px; background-color: #2a2a2a; color: #4a9eff;') \
            .bind_enabled_from(machine_state, '_lock',
                             backward=lambda _: machine_state.busy)


# Event handlers

def update_step_buttons():
    """Update step button styling - simplified for new circular design."""
    pass  # No longer needed with new design


def jog_axis(axis: str, distance: float):
    """Handle jog button click."""
    print(f"[DEBUG] jog_axis called: axis={axis}, distance={distance}, feed_rate={jog_params['feed_rate']}")
    cnc_controller.jog(axis, distance, jog_params['feed_rate'])


def jog_diagonal(x_dir: int, y_dir: int):
    """Handle diagonal jog (X and Y simultaneously)."""
    x_distance = x_dir * jog_params['xy_step']
    y_distance = y_dir * jog_params['xy_step']
    cnc_controller.jog_xy(x_distance, y_distance, jog_params['feed_rate'])


def home_axis(axis: str):
    """Handle home axis button click."""
    cnc_controller.home_axis(axis)


def home_all():
    """Handle home all button click."""
    cnc_controller.home_all()


def set_xy_zero():
    """Set current XY position as zero."""
    cnc_controller.send_command("G92 X0 Y0")
    ui.notify("XY position set to zero", type='positive')


def set_z_zero():
    """Set current Z position + 26 as zero (so current position becomes -26)."""
    cnc_controller.send_command("G92 Z-26")
    ui.notify("Z zero set (current = -26)", type='positive')


def set_a_zero():
    """Set current A position as zero."""
    cnc_controller.send_command("G92 A0")
    ui.notify("A position set to zero", type='positive')


def regenerate_toolpath():
    """Regenerate gcode from current shape positions after shapes have been moved."""
    global current_gcode, current_toolpath_shapes
    
    if not current_toolpath_shapes:
        return
    
    logger.info("Regenerating toolpath after shape move...")
    gcode_str = toolpath_generator.generate_toolpath(current_toolpath_shapes, source_filename="moved_shapes")
    current_gcode = gcode_str.split('\n')
    
    # Update state using the correct method
    machine_state.set_job_loaded(True, "shapes (repositioned)")
    ui.notify("Toolpath regenerated with new positions", type='positive')


def add_shapes_to_canvas(shapes: dict, start_color_index: int = 0):
    """Add shapes to canvas without clearing existing ones."""
    global toolpath_canvas
    
    if toolpath_canvas is None:
        logger.warning("toolpath_canvas is None, cannot add shapes")
        return
    
    if shapes:
        for i, (shape_name, points) in enumerate(shapes.items()):
            if points:
                # Log the bounds being sent to JavaScript
                x_vals = [p[0] for p in points]
                y_vals = [p[1] for p in points]
                logger.info(f"  Sending {shape_name} to canvas: {len(points)} pts, X({min(x_vals):.1f}-{max(x_vals):.1f}), Y({min(y_vals):.1f}-{max(y_vals):.1f})")
                
                # Convert points to JSON-safe format
                points_json = json.dumps(points)
                ui.run_javascript(f'''try {{ window.toolpathCanvas.addShape("{shape_name}", {points_json}, {start_color_index + i}); }} catch(e) {{ alert(e.message); }}''')
                logger.info(f"  Added {shape_name}: {len(points)} points")


def update_toolpath_plot(shapes: dict, clear_existing: bool = True):
    """Update the toolpath visualization with new shapes using Fabric.js canvas."""
    global toolpath_canvas, current_toolpath_shapes
    
    if toolpath_canvas is None:
        logger.warning("toolpath_canvas is None, cannot update")
        return
    
    logger.info(f"Updating toolpath canvas with {len(shapes) if shapes else 0} shapes (clear={clear_existing})")
    
    if clear_existing:
        # Clear existing shapes
        ui.run_javascript('window.toolpathCanvas.clearShapes();')
    
    # Add shapes to canvas
    add_shapes_to_canvas(shapes)


async def handle_file_upload(event, label):
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
        
        # Process DXF file
        ui.notify('Processing DXF file...', type='info')
        # min_distance is in inches (DXF units before conversion to mm)
        # 0.1" = 2.54mm spacing - good balance of detail and point count
        shapes = dxf_processor.process_dxf_basic(saved_path, min_distance=0.1)
        current_toolpath_shapes = shapes
        
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
        update_toolpath_plot(shapes, clear_existing=False)
        
        # Update state
        machine_state.set_job_loaded(True, filename)
        label.set_text(f'Loaded: {filename}')
        
        ui.notify(f'File loaded: {filename} ({len(shapes)} shapes)', type='positive')
    except Exception as e:
        ui.notify(f'Error processing DXF: {str(e)}', type='negative')
        import traceback
        traceback.print_exc()


async def save_canvas_state():
    """Save current canvas state to a JSON file with user-provided name."""
    global current_toolpath_shapes
    
    # Create dialog for naming the save
    with ui.dialog() as dialog, ui.card().classes('w-80'):
        ui.label('Save Canvas').classes('text-h5 font-bold')
        
        name_input = ui.input('Filename', value='').props('autofocus outlined').classes('w-full')
        warning_label = ui.label('').classes('text-warning')
        
        async def do_save(overwrite=False):
            try:
                name = name_input.value.strip()
                if not name:
                    ui.notify('Please enter a filename', type='warning')
                    return
                
                # Sanitize name - remove special characters
                safe_name = ''.join(c if c.isalnum() or c in '-_ ' else '' for c in name).strip()
                safe_name = safe_name.replace(' ', '_')
                if not safe_name:
                    ui.notify('Invalid filename', type='warning')
                    return
                
                # Add .json extension if not present
                if not safe_name.endswith('.json'):
                    safe_name += '.json'
                
                filepath = os.path.join(file_manager.upload_dir, safe_name)
                
                # Check if file exists
                if os.path.exists(filepath) and not overwrite:
                    warning_label.set_text(f'File "{safe_name}" exists. Click Save again to overwrite.')
                    # Change save button to confirm overwrite
                    save_btn.on_click.clear()
                    save_btn._props['color'] = 'warning'
                    save_btn.set_text('Overwrite')
                    save_btn.on('click', lambda: do_save(overwrite=True))
                    save_btn.update()
                    return
                
                # Get canvas data from JavaScript
                canvas_json = await ui.run_javascript('window.toolpathCanvas.saveCanvasState()')
                
                if not canvas_json:
                    ui.notify('No shapes to save', type='warning')
                    dialog.close()
                    return
                
                # Write to file
                with open(filepath, 'w') as f:
                    f.write(canvas_json)
                
                ui.notify(f'Saved: {safe_name}', type='positive')
                logger.info(f'Canvas state saved to {filepath}')
                dialog.close()
                
            except Exception as e:
                ui.notify(f'Error saving canvas: {str(e)}', type='negative')
                logger.error(f'Error saving canvas: {e}')
        
        with ui.row().classes('w-full gap-2 mt-4'):
            ui.button('Cancel', on_click=dialog.close).props('flat')
            save_btn = ui.button('Save', on_click=lambda: do_save(), color='primary')
    
    dialog.open()


async def load_canvas_state():
    """Show dialog to load a saved canvas state."""
    import glob
    
    # Find all saved canvas JSON files
    pattern = os.path.join(file_manager.upload_dir, '*.json')
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)  # Most recent first
    
    if not files:
        ui.notify('No saved canvases found', type='warning')
        return
    
    # Create selection dialog
    with ui.dialog() as dialog, ui.card().classes('w-96'):
        ui.label('Load Saved Canvas').classes('text-h5 font-bold')
        
        file_list_container = ui.column().classes('gap-1 w-full').style('max-height: 400px; overflow-y: auto;')
        
        async def load_file(filepath):
            global current_toolpath_shapes
            try:
                with open(filepath, 'r') as f:
                    canvas_json = f.read()
                
                # Load into JavaScript canvas
                await ui.run_javascript(f'window.toolpathCanvas.loadCanvasState({repr(canvas_json)})')
                
                # Parse and update Python state
                import json
                state = json.loads(canvas_json)
                current_toolpath_shapes = {}
                for name, shape_data in state.get('shapes', {}).items():
                    current_toolpath_shapes[name] = [tuple(p) for p in shape_data.get('points', [])]
                
                machine_state.set_job_loaded(True, os.path.basename(filepath))
                ui.notify(f'Canvas loaded: {os.path.basename(filepath)}', type='positive')
                dialog.close()
                
            except Exception as e:
                ui.notify(f'Error loading canvas: {str(e)}', type='negative')
                logger.error(f'Error loading canvas: {e}')
        
        def delete_file(filepath, row):
            """Delete a saved canvas file."""
            try:
                os.remove(filepath)
                row.delete()
                ui.notify(f'Deleted: {os.path.basename(filepath)}', type='info')
            except Exception as e:
                ui.notify(f'Error deleting: {str(e)}', type='negative')
        
        # List files with load and delete buttons
        with file_list_container:
            for filepath in files[:30]:  # Limit to 30 files
                filename = os.path.basename(filepath)
                display_name = filename.replace('.json', '')
                with ui.row().classes('w-full items-center gap-1') as row:
                    ui.button(display_name, on_click=lambda f=filepath: load_file(f)).classes('flex-1').props('flat align=left')
                    ui.button(icon='delete', on_click=lambda f=filepath, r=row: delete_file(f, r)).props('flat color=negative dense')
        
        ui.button('Cancel', on_click=dialog.close).props('flat').classes('mt-2')
    
    dialog.open()


def clear_canvas():
    """Clear all shapes from canvas."""
    global current_toolpath_shapes
    current_toolpath_shapes = {}
    ui.run_javascript('window.toolpathCanvas.clearShapes()')
    machine_state.set_job_loaded(False)
    ui.notify('Canvas cleared', type='info')


def start_job():
    """Handle start job button click - generates toolpath and streams via serial."""
    global current_gcode
    
    if not current_toolpath_shapes:
        ui.notify('No shapes loaded', type='warning')
        return
    
    # Generate toolpath from current shape positions
    ui.notify('Generating toolpath...', type='info')
    print(f"\n--- Toolpath Generation ---")
    gcode_str = toolpath_generator.generate_toolpath(current_toolpath_shapes, source_filename="job")
    current_gcode = gcode_str.split('\n')
    
    # Debug: Show gcode summary
    g0_count = sum(1 for line in current_gcode if line.startswith('G0'))
    g1_count = sum(1 for line in current_gcode if line.startswith('G1'))
    print(f"  Total lines: {len(current_gcode)}")
    print(f"  Rapid moves (G0): {g0_count}")
    print(f"  Cut moves (G1): {g1_count}")
    print(f"{'='*60}\n")
    
    ui.notify('Starting job...', type='info')
    
    # Start job without callback - we'll monitor completion via state
    cnc_controller.start_job(current_gcode)


def pause_job():
    """Handle pause job button click."""
    cnc_controller.pause_job()
    ui.notify('Job paused', type='warning')


def resume_job():
    """Handle resume job button click."""
    cnc_controller.resume_job()
    ui.notify('Job resumed', type='positive')


def stop_job():
    """Handle stop job button click."""
    cnc_controller.stop_job()
    ui.notify('Job stopped', type='negative')


# Track previous status for change detection
_previous_status = {'text': None}

def update_ui(pos_labels, status_label):
    """Update UI with current machine state (called periodically)."""
    # Update position display
    x, y, z, a = machine_state.get_position()
    pos_labels['X'].set_text(f'{x:.2f} mm')
    pos_labels['Y'].set_text(f'{y:.2f} mm')
    pos_labels['Z'].set_text(f'{z:.2f} mm')
    pos_labels['A'].set_text(f'{a:.2f} °')
    
    # Update status
    current_status = machine_state.status_text
    status_label.set_text(current_status)
    
    # Detect status changes and show notifications
    if _previous_status['text'] != current_status:
        if current_status == 'Complete':
            ui.notify('Job completed successfully!', type='positive')
        elif current_status == 'Error':
            ui.notify('Job error!', type='negative')
        _previous_status['text'] = current_status


@ui.page('/')
def main_page():
    """Main application page with responsive tabbed interface optimized for 1280x720 and larger."""
    
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
                --md-bg-primary: #1e1e1e;
                --md-bg-secondary: #252525;
                --md-bg-elevated: #2d2d2d;
                --md-bg-card: #333333;
                --md-border: #404040;
                --md-border-light: #4a4a4a;
                --md-text-primary: #e0e0e0;
                --md-text-secondary: #9e9e9e;
                --md-accent-blue: #4a9eff;
                --md-accent-green: #4caf50;
                --md-accent-orange: #ff9800;
                --md-accent-red: #f44336;
            }
            
            /* Global Background */
            body, .q-page, .q-page-container {
                background-color: var(--md-bg-primary) !important;
            }
            
            /* Compact Header */
            header.q-header {
                background: linear-gradient(180deg, #2a2a2a 0%, #252525 100%) !important;
                border-bottom: 1px solid var(--md-border) !important;
                min-height: 48px !important;
                max-height: 48px !important;
                height: 48px !important;
                padding: 8px 16px !important;
            }
            
            /* Cards - Subtle and tight */
            .q-card {
                background: var(--md-bg-card) !important;
                border: 1px solid var(--md-border) !important;
                border-radius: 8px !important;
                box-shadow: none !important;
            }
            
            /* Dense Buttons - Material Design 3 style */
            .q-btn {
                border-radius: 6px !important;
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
                border-radius: 8px !important;
                padding: 4px !important;
            }
            
            .q-tab {
                min-height: 40px !important;
                padding: 0 16px !important;
                border-radius: 6px !important;
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
                border-radius: 6px !important;
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
                border-radius: 8px !important;
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
                min-height: 36px !important;
                padding: 0 10px !important;
                border-radius: 4px !important;
                margin: 0 2px !important;
                opacity: 0.7;
            }
            
            .header-tabs .q-tab--active {
                background: rgba(255,255,255,0.1) !important;
                opacity: 1;
            }
            
            .header-tabs .q-tabs__content {
                gap: 4px;
            }
            
            /* Labels styling */
            .text-h5, .text-h6 {
                color: var(--md-text-primary) !important;
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
                border-radius: 8px !important;
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
        </style>
        <script>
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
    
    pos_labels, status_label, tabs, job_tab, gcode_tab, wifi_tab = create_header()
    
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
                        # Left column: Job file and controls (fixed width)
                        with ui.column().classes('gap-2').style('flex: 0 0 180px;'):
                            create_file_controls()
                            ui.separator()
                            create_job_controls()
                        
                        # Center column: Toolbar + Interactive Toolpath Canvas (Fabric.js)
                        global toolpath_canvas
                        with ui.column().style('flex: 1 1 0; min-width: 400px; gap: 10px; height: 100%; box-sizing: border-box;'):
                            # Toolbar row above canvas - no wrap
                            with ui.row().classes('items-center gap-2').style('background: #2a2a2a; border-radius: 4px; padding: 6px 10px; width: 100%; flex-wrap: nowrap; flex-shrink: 0;'):
                                # Transform tools
                                ui.button('⬌', on_click=lambda: ui.run_javascript('window.toolpathCanvas.mirrorX()')).props('dense flat').style('min-width: 36px; height: 36px; font-size: 22px; background-color: #2a2a2a; color: #4a9eff; display: flex; align-items: center; justify-content: center;').tooltip('Mirror X')
                                ui.button('⬍', on_click=lambda: ui.run_javascript('window.toolpathCanvas.mirrorY()')).props('dense flat').style('min-width: 36px; height: 36px; font-size: 22px; background-color: #2a2a2a; color: #4a9eff; display: flex; align-items: center; justify-content: center;').tooltip('Mirror Y')
                                
                                ui.element('div').style('width: 1px; height: 24px; background: #4a4a4a; margin: 0 4px;')  # Separator
                                
                                rotate_input = ui.number(value=90, format='%.0f').props('dense outlined').style('width: 60px; font-size: 13px;').classes('toolbar-input')
                                ui.label('°').classes('text-body2').style('margin-right: 2px;')
                                ui.button('↻', on_click=lambda: ui.run_javascript(f'window.toolpathCanvas.rotateByDegrees({rotate_input.value})')).props('dense flat').style('min-width: 36px; height: 36px; font-size: 22px; background-color: #2a2a2a; color: #4a9eff; display: flex; align-items: center; justify-content: center;').tooltip('Rotate CW')
                                ui.button('↺', on_click=lambda: ui.run_javascript(f'window.toolpathCanvas.rotateByDegrees(-{rotate_input.value})')).props('dense flat').style('min-width: 36px; height: 36px; font-size: 22px; background-color: #2a2a2a; color: #4a9eff; display: flex; align-items: center; justify-content: center;').tooltip('Rotate CCW')
                                
                                ui.element('div').style('width: 1px; height: 24px; background: #4a4a4a; margin: 0 4px;')  # Separator
                                
                                scale_input = ui.number(value=100, format='%.0f').props('dense outlined').style('width: 60px; font-size: 13px;').classes('toolbar-input')
                                ui.label('%').classes('text-body2').style('margin-right: 2px;')
                                ui.button('Scale', on_click=lambda: ui.run_javascript(f'window.toolpathCanvas.scaleShape({scale_input.value / 100})')).props('dense flat').style('height: 36px; font-size: 13px; background-color: #2a2a2a; color: #4a9eff;')
                                
                                ui.element('div').style('width: 1px; height: 24px; background: #4a4a4a; margin: 0 4px;')  # Separator
                                
                                # Pattern tools
                                grid_x = ui.number(value=2, format='%.0f', min=1, max=10).props('dense outlined').style('width: 50px; font-size: 13px;').classes('toolbar-input')
                                ui.label('×').classes('text-body2')
                                grid_y = ui.number(value=2, format='%.0f', min=1, max=10).props('dense outlined').style('width: 50px; font-size: 13px;').classes('toolbar-input')
                                ui.button('Grid', on_click=lambda: ui.run_javascript(f'window.toolpathCanvas.gridArray({int(grid_x.value)}, {int(grid_y.value)})')).props('dense flat').style('height: 36px; font-size: 13px; background-color: #2a2a2a; color: #4a9eff;')
                                
                                ui.element('div').style('width: 1px; height: 24px; background: #4a4a4a; margin: 0 4px;')  # Separator
                                
                                keep_orientation = ui.checkbox('Keep Orientation', value=True).props('dense').style('font-size: 12px;')
                                nest_offset = ui.number(value=5, format='%.0f', min=1, max=20).props('dense outlined').style('width: 50px; font-size: 13px;').classes('toolbar-input').tooltip('Gap (mm)')
                                
                                async def do_nest():
                                    offset_val = int(nest_offset.value)
                                    keep_orient = str(keep_orientation.value).lower()
                                    result = await ui.run_javascript(f'window.toolpathCanvas.nestShapes({keep_orient}, {offset_val})')
                                    if result and not result.get('success'):
                                        ui.notify(result.get('error', 'Nesting failed'), type='negative')
                                    elif result and result.get('success'):
                                        ui.notify(f'Nested to {result["width"]:.0f}×{result["height"]:.0f}mm', type='positive')
                                
                                ui.button('Nest', on_click=do_nest).props('dense flat').style('height: 36px; font-size: 13px; background-color: #2a2a2a; color: #4a9eff;')
                            
                            # Load Fabric.js library
                            ui.add_head_html('<script src="https://cdnjs.cloudflare.com/ajax/libs/fabric.js/5.3.1/fabric.min.js"></script>')
                            ui.add_head_html('<script src="/static/toolpath_canvas.js?v=21"></script>')
                            
                            # Create canvas container - flex fills space
                            toolpath_canvas = ui.html('''
                                <div id="canvas-container" style="width: 100%; height: 100%; min-width: 400px; background-color: #1e1e1e; border-radius: 4px; overflow: hidden;">
                                    <canvas id="toolpath-canvas"></canvas>
                                </div>
                            ''', sanitize=False).classes('w-full').style('width: 100%; flex: 1; min-height: 0;')
                            
                            # Initialize canvas after page fully loads
                            async def init_canvas_after_load():
                                await ui.context.client.connected()
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
                                ''')
                            
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
                                    logger.info(f"Shape '{shape_name}' moved to new position")
                            
                            ui.on('shape_moved', on_shape_moved)
                        
                        # Right column: Jog Controls only (fixed width)
                        with ui.column().classes('gap-2 items-center').style('flex: 0 0 320px; padding: 0 0 10px 0; box-sizing: border-box;'):
                            # Control section - Jog wheel and controls
                            ui.label('Control').classes('text-body1 font-bold w-full text-center').style('color: #aaa; background-color: #2a2a2a; padding: 6px 10px; border-radius: 4px; height: 48px; display: flex; align-items: center; justify-content: center; box-sizing: border-box;')
                            create_jog_controls()
            
            # GCODE tab - Manual G-code command interface
            with ui.tab_panel(gcode_tab).classes('tab-content'):
                with ui.card().classes('w-full h-full').style('padding: 12px; box-sizing: border-box;'):
                    ui.label('Manual G-code Commands').classes('text-body1 font-bold mb-2').style('color: #aaa;')
                    
                    # Command input
                    with ui.row().classes('w-full gap-2 items-center mb-3'):
                        gcode_input = ui.input('Enter G-code command').classes('flex-1').props('outlined dense')
                        
                        async def send_gcode():
                            cmd = gcode_input.value.strip()
                            if cmd:
                                response_log.push(f'>>> {cmd}')
                                response = cnc_controller.send_command_with_response(cmd, timeout=10.0)
                                for line in response.split('\n'):
                                    response_log.push(f'<<< {line}')
                                gcode_input.value = ''
                        
                        ui.button('Send', on_click=send_gcode, icon='send').props('color=primary dense')
                    
                    # Common commands
                    ui.label('Quick Commands:').classes('text-body2 mb-1').style('color: #888;')
                    with ui.row().classes('gap-1 mb-3'):
                        ui.button('M115', on_click=lambda: [gcode_input.set_value('M115'), send_gcode()]).props('dense outline').style('font-size: 11px;').tooltip('Firmware')
                        ui.button('M114', on_click=lambda: [gcode_input.set_value('M114'), send_gcode()]).props('dense outline').style('font-size: 11px;').tooltip('Position')
                        ui.button('M503', on_click=lambda: [gcode_input.set_value('M503'), send_gcode()]).props('dense outline').style('font-size: 11px;').tooltip('Settings')
                        ui.button('M999', on_click=lambda: [gcode_input.set_value('M999'), send_gcode()]).props('dense outline color=orange').style('font-size: 11px;').tooltip('Reset')
                    
                    # Response log
                    ui.label('Response Log:').classes('text-body2 mb-1').style('color: #888;')
                    response_log = ui.log().classes('w-full').style('height: 280px; font-family: monospace; font-size: 14px;')
                    
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
                                def restart_service():
                                    ui.notify('Restarting service...', type='warning')
                                    subprocess.Popen(['sudo', 'systemctl', 'restart', 'fabcnc.service'], 
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                
                                ui.button('Restart Service', icon='refresh', on_click=restart_service) \
                                    .props('color=warning dense').style('font-size: 13px;')
                                
                                def reboot_system():
                                    ui.notify('Rebooting system...', type='warning')
                                    subprocess.Popen(['sudo', 'reboot'], 
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                
                                ui.button('Reboot System', icon='restart_alt', on_click=reboot_system) \
                                    .props('color=negative dense').style('font-size: 13px;')
        
        # Start periodic UI update timer (10 Hz = 100ms)
        ui.timer(0.1, lambda: update_ui(pos_labels, status_label))


if __name__ in {"__main__", "__mp_main__"}:
    # Run the NiceGUI app
    # Bind to 0.0.0.0 to allow access from other computers on the network
    ui.run(
        host='0.0.0.0',
        port=8080,
        title='fabCNC Controller',
        favicon='🔧',
        dark=None,  # Auto-detect system preference
        reload=False,
        show=False  # Don't auto-open browser (for kiosk mode)
    )
