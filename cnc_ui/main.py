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

# Current loaded G-code
current_gcode = []


def create_header():
    """Create the application header with position, status, connection info and controls."""
    pos_labels = {}
    
    with ui.header().classes('items-center justify-between bg-primary text-white py-2 px-4'):
        # Left side: App name
        ui.label('fabCNC Controller').classes('text-h4 font-bold')
        
        # Center: Position display
        with ui.row().classes('items-center gap-4'):
            for axis in ['X', 'Y', 'Z', 'A']:
                with ui.row().classes('items-center gap-1'):
                    ui.label(f'{axis}:').classes('text-h6 text-white/70')
                    unit = '°' if axis == 'A' else 'mm'
                    pos_labels[axis] = ui.label(f'0.00 {unit}').classes('text-h6 font-bold')
            
            ui.separator().props('vertical dark').classes('mx-2')
            
            # Status display
            with ui.row().classes('items-center gap-1'):
                ui.label('Status:').classes('text-h6 text-white/70')
                status_label = ui.label('Idle').classes('text-h6 font-bold')
        
        # Right side: Lock and Version
        with ui.row().classes('items-center gap-4'):
            # Lock button
            def lock_screen():
                lock_state['locked'] = True
                ui.navigate.reload()
            
            ui.button(icon='lock', on_click=lock_screen).props('flat round color=white').tooltip('Lock Screen')
            
            # Version
            ui.label(APP_VERSION).classes('text-h6')
    
    return pos_labels, status_label


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
    """Create jog controls with native buttons in a flexible column-based layout."""
    btn_step = 'font-size: 18px; opacity: 0.7;'
    btn_tall = 'font-size: 24px; font-weight: bold;'
    btn_home_zero = 'font-size: 16px;'
    btn_xy = 'font-size: 32px; aspect-ratio: 1; width: 100%; height: 100%;'
    
    # Main container - use 100% height of parent (card) with max-height constraint
    with ui.row().classes('w-full h-full gap-2 justify-center items-stretch').style('max-height: 520px;'):
        # Column 1: XY step buttons (1mm, 10mm, 100mm) - blue-grey to match XY grid
        with ui.column().classes('gap-2').style('flex: 1; min-width: 60px;'):
            xy_1 = ui.button('1MM', on_click=lambda: [jog_params.update({'xy_step': 1.0}), update_step_buttons()]) \
                .props('outline color=blue-grey-6').classes('flex-1 w-full').style(btn_step)
            xy_10 = ui.button('10MM', on_click=lambda: [jog_params.update({'xy_step': 10.0}), update_step_buttons()]) \
                .props('unelevated color=blue-grey-6').classes('flex-1 w-full').style(btn_step)
            xy_100 = ui.button('100MM', on_click=lambda: [jog_params.update({'xy_step': 100.0}), update_step_buttons()]) \
                .props('outline color=blue-grey-6').classes('flex-1 w-full').style(btn_step)
        
        # Column 2-4: XY directional pad (3x3 grid) - fixed square based on available height
        with ui.element('div').style('display: grid; grid-template-columns: repeat(3, 1fr); grid-template-rows: repeat(3, 1fr); gap: 8px; aspect-ratio: 1; height: 100%; flex: 0 0 auto;'):
            # Row 1
            ui.button(icon='north_west').props('color=blue-grey-6').style(btn_xy) \
                .on('click', lambda: jog_diagonal(-1, 1)) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            ui.button(icon='north').props('color=blue-grey-6').style(btn_xy) \
                .on('click', lambda: jog_axis('Y', jog_params['xy_step'])) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            ui.button(icon='north_east').props('color=blue-grey-6').style(btn_xy) \
                .on('click', lambda: jog_diagonal(1, 1)) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            # Row 2
            ui.button(icon='west').props('color=blue-grey-6').style(btn_xy) \
                .on('click', lambda: jog_axis('X', -jog_params['xy_step'])) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            ui.button(icon='home').props('color=red-6').style(btn_xy) \
                .on('click', lambda: home_all()) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            ui.button(icon='east').props('color=blue-grey-6').style(btn_xy) \
                .on('click', lambda: jog_axis('X', jog_params['xy_step'])) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            # Row 3
            ui.button(icon='south_west').props('color=blue-grey-6').style(btn_xy) \
                .on('click', lambda: jog_diagonal(-1, -1)) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            ui.button(icon='south').props('color=blue-grey-6').style(btn_xy) \
                .on('click', lambda: jog_axis('Y', -jog_params['xy_step'])) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            ui.button(icon='south_east').props('color=blue-grey-6').style(btn_xy) \
                .on('click', lambda: jog_diagonal(1, -1)) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
        
        # Column 5: Z step buttons (0.1, 1mm, 10mm) - green to match Z+/Z-
        with ui.column().classes('gap-2').style('flex: 1; min-width: 60px;'):
            z_01 = ui.button('0.1', on_click=lambda: [jog_params.update({'z_step': 0.1}), update_step_buttons()]) \
                .props('outline color=green-6').classes('flex-1 w-full').style(btn_step)
            z_1 = ui.button('1MM', on_click=lambda: [jog_params.update({'z_step': 1.0}), update_step_buttons()]) \
                .props('unelevated color=green-6').classes('flex-1 w-full').style(btn_step)
            z_10 = ui.button('10MM', on_click=lambda: [jog_params.update({'z_step': 10.0}), update_step_buttons()]) \
                .props('outline color=green-6').classes('flex-1 w-full').style(btn_step)
        
        # Column 6: Z+/Z- (2 buttons spanning full height)
        with ui.column().classes('gap-2').style('flex: 1; min-width: 60px;'):
            ui.button('Z+').props('color=green-6').classes('flex-1 w-full').style(btn_tall) \
                .on('click', lambda: jog_axis('Z', jog_params['z_step'])) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            ui.button('Z-').props('color=green-6').classes('flex-1 w-full').style(btn_tall) \
                .on('click', lambda: jog_axis('Z', -jog_params['z_step'])) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
        
        # Column 7: A step buttons (1°, 45°, 90°) - orange to match A+/A-
        with ui.column().classes('gap-2').style('flex: 1; min-width: 60px;'):
            a_1 = ui.button('1°', on_click=lambda: [jog_params.update({'a_step': 1.0}), update_step_buttons()]) \
                .props('outline color=orange-6').classes('flex-1 w-full').style(btn_step)
            a_45 = ui.button('45°', on_click=lambda: [jog_params.update({'a_step': 45.0}), update_step_buttons()]) \
                .props('unelevated color=orange-6').classes('flex-1 w-full').style(btn_step)
            a_90 = ui.button('90°', on_click=lambda: [jog_params.update({'a_step': 90.0}), update_step_buttons()]) \
                .props('outline color=orange-6').classes('flex-1 w-full').style(btn_step)
        
        # Column 8: A+/A- (2 buttons spanning full height)
        with ui.column().classes('gap-2').style('flex: 1; min-width: 60px;'):
            ui.button('A+').props('color=orange-6').classes('flex-1 w-full').style(btn_tall) \
                .on('click', lambda: jog_axis('A', jog_params['a_step'])) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            ui.button('A-').props('color=orange-6').classes('flex-1 w-full').style(btn_tall) \
                .on('click', lambda: jog_axis('A', -jog_params['a_step'])) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
        
        # Column 9: Home buttons (4 buttons spanning full height) - icon + axis letter
        with ui.column().classes('gap-2').style('flex: 1; min-width: 60px;'):
            ui.button('X', icon='home', on_click=lambda: home_axis('X')).props('color=red-6').classes('flex-1 w-full').style(btn_home_zero) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            ui.button('Y', icon='home', on_click=lambda: home_axis('Y')).props('color=red-6').classes('flex-1 w-full').style(btn_home_zero) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            ui.button('Z', icon='home', on_click=lambda: home_axis('Z')).props('color=red-6').classes('flex-1 w-full').style(btn_home_zero) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            ui.button('A', icon='home', on_click=lambda: home_axis('A')).props('color=red-6').classes('flex-1 w-full').style(btn_home_zero) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
        
        # Column 10: Zero buttons (4 buttons spanning full height)
        with ui.column().classes('gap-2').style('flex: 1; min-width: 60px;'):
            ui.button('Zero X', on_click=lambda: cnc_controller.send_command("G92 X0")).props('color=blue-6').classes('flex-1 w-full').style(btn_home_zero) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            ui.button('Zero Y', on_click=lambda: cnc_controller.send_command("G92 Y0")).props('color=blue-6').classes('flex-1 w-full').style(btn_home_zero) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            ui.button('Zero Z', on_click=set_z_zero).props('color=blue-6').classes('flex-1 w-full').style(btn_home_zero) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            ui.button('Zero A', on_click=set_a_zero).props('color=blue-6').classes('flex-1 w-full').style(btn_home_zero) \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
        
        # Store button references for updating
        jog_params['_buttons'] = {
            'xy': {1.0: xy_1, 10.0: xy_10, 100.0: xy_100},
            'z': {0.1: z_01, 1.0: z_1, 10.0: z_10},
            'a': {1.0: a_1, 45.0: a_45, 90.0: a_90}
        }


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
        ui.label('Job File').classes('text-h6 font-bold')
        
        loaded_file_label = ui.label('No file loaded').classes('text-body1 text-grey-7')
        
        upload = ui.upload(
            label='Load DXF File',
            auto_upload=True,
            on_upload=lambda e: handle_file_upload(e, loaded_file_label)
        ).props('accept=.dxf').classes('w-full dxf-upload').style('font-size: 16px;')
        
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
        with ui.row().classes('w-full gap-2'):
            ui.button('Save', icon='save', on_click=save_canvas_state).style('flex: 1; background-color: #333;').tooltip('Save canvas to file')
            ui.button('Load', icon='folder_open', on_click=load_canvas_state).style('flex: 1; background-color: #333;').tooltip('Load saved canvas')
            ui.button('Clear', icon='delete', on_click=clear_canvas).style('flex: 1; background-color: #333;').tooltip('Clear all shapes')
        
        return loaded_file_label


def create_job_controls():
    """Create the compact job execution control panel."""
    with ui.column().classes('w-full gap-2'):
        ui.label('Job Control').classes('text-h6 font-bold')
        
        ui.button('Start', on_click=start_job, color='positive') \
            .props('size=lg') \
            .classes('w-full') \
            .style('font-size: 18px;') \
            .bind_enabled_from(machine_state, '_lock',
                             backward=lambda _: machine_state.job_loaded and machine_state.is_idle())
        
        ui.button('Pause', on_click=pause_job, color='warning') \
            .props('size=lg') \
            .classes('w-full') \
            .style('font-size: 18px;') \
            .bind_enabled_from(machine_state, '_lock',
                             backward=lambda _: machine_state.is_running())
        
        ui.button('Resume', on_click=resume_job, color='positive') \
            .props('size=lg') \
            .classes('w-full') \
            .style('font-size: 18px;') \
            .bind_enabled_from(machine_state, '_lock',
                             backward=lambda _: machine_state.paused)
        
        ui.button('Stop', on_click=stop_job, color='negative') \
            .props('size=lg') \
            .classes('w-full') \
            .style('font-size: 18px;') \
            .bind_enabled_from(machine_state, '_lock',
                             backward=lambda _: machine_state.busy)


# Event handlers

def update_step_buttons():
    """Update step button styling to show active selection."""
    if '_buttons' not in jog_params:
        return
    
    # Update XY buttons (blue-grey)
    for val, btn in jog_params['_buttons']['xy'].items():
        if val == jog_params['xy_step']:
            btn.props(remove='outline')
            btn.props('unelevated color=blue-grey-6')
        else:
            btn.props(remove='unelevated')
            btn.props('outline color=blue-grey-6')
        btn.update()
    
    # Update Z buttons (green)
    for val, btn in jog_params['_buttons']['z'].items():
        if val == jog_params['z_step']:
            btn.props(remove='outline')
            btn.props('unelevated color=green-6')
        else:
            btn.props(remove='unelevated')
            btn.props('outline color=green-6')
        btn.update()
    
    # Update A buttons (orange)
    for val, btn in jog_params['_buttons']['a'].items():
        if val == jog_params['a_step']:
            btn.props(remove='outline')
            btn.props('unelevated color=orange-6')
        else:
            btn.props(remove='unelevated')
            btn.props('outline color=orange-6')
        btn.update()


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
    
    # Lock screen overlay
    if lock_state['locked']:
        with ui.dialog().props('persistent full-width full-height') as lock_dialog:
            with ui.card().classes('items-center').style('padding: 32px 40px; position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);'):
                ui.icon('lock', size='64px', color='primary').classes('mb-3')
                ui.label('Enter 4-digit PIN').classes('text-h6 text-grey-7 mb-3')
                
                pin_display = ui.label('').classes('text-h4 font-bold mb-1').style('letter-spacing: 10px; min-height: 36px;')
                entered_pin = {'value': ''}
                error_label = ui.label('').classes('text-negative text-h6').style('min-height: 24px;')
                
                def add_digit(digit):
                    if len(entered_pin['value']) < 4:
                        entered_pin['value'] += str(digit)
                        pin_display.set_text('●' * len(entered_pin['value']))
                        error_label.set_text('')
                        
                        # Check PIN when 4 digits entered
                        if len(entered_pin['value']) == 4:
                            if entered_pin['value'] == LOCK_PIN:
                                lock_state['locked'] = False
                                lock_dialog.close()
                                ui.navigate.reload()
                            else:
                                error_label.set_text('Incorrect PIN')
                                entered_pin['value'] = ''
                                pin_display.set_text('')
                
                def clear_pin():
                    entered_pin['value'] = ''
                    pin_display.set_text('')
                    error_label.set_text('')
                
                def backspace():
                    entered_pin['value'] = entered_pin['value'][:-1]
                    pin_display.set_text('●' * len(entered_pin['value']))
                    error_label.set_text('')
                
                # Number pad - 4 rows x 3 columns
                with ui.grid(columns=3).classes('gap-2'):
                    for digit in [1, 2, 3, 4, 5, 6, 7, 8, 9]:
                        ui.button(str(digit), on_click=lambda d=digit: add_digit(d)) \
                            .props('size=xl').style('width: 90px; height: 90px; font-size: 32px;')
                    ui.button('C', on_click=clear_pin) \
                        .props('size=xl color=negative').style('width: 90px; height: 90px; font-size: 28px;')
                    ui.button('0', on_click=lambda: add_digit(0)) \
                        .props('size=xl').style('width: 90px; height: 90px; font-size: 32px;')
                    ui.button('⌫', on_click=backspace) \
                        .props('size=xl color=warning').style('width: 90px; height: 90px; font-size: 28px;')
        
        lock_dialog.open()
    
    # Disable scrolling on body and html
    ui.add_head_html('''
        <style>
            html, body {
                overflow: hidden !important;
                height: 100vh !important;
                margin: 0 !important;
                padding: 0 !important;
            }
        </style>
    ''')
    
    pos_labels, status_label = create_header()
    
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
    
    with ui.column().classes('w-full mx-auto gap-2').style('height: calc(100vh - 80px); overflow: hidden; padding: 12px;'):
        # Tabbed interface for different control sections
        with ui.tabs().classes('w-full') as tabs:
            control_tab = ui.tab('Control', icon='gamepad').style('font-size: 16px; min-height: 50px')
            job_tab = ui.tab('Toolpath', icon='route').style('font-size: 16px; min-height: 50px')
            gcode_tab = ui.tab('GCODE', icon='terminal').style('font-size: 16px; min-height: 50px')
            wifi_tab = ui.tab('System', icon='settings').style('font-size: 16px; min-height: 50px')
        
        with ui.tab_panels(tabs, value=control_tab).classes('w-full').style('flex: 1; min-height: 0; overflow: hidden;'):
            # Control tab - Manual jogging and homing
            with ui.tab_panel(control_tab).classes('w-full').style('height: 100%; overflow: hidden;'):
                with ui.card().classes('w-full h-full').style('padding: 12px; overflow: hidden;'):
                    create_jog_controls()
            
            # Job tab - File loading, job execution, and toolpath visualization
            with ui.tab_panel(job_tab).style('height: 100%; overflow: hidden;'):
                with ui.card().classes('w-full h-full').style('padding: 12px; overflow: hidden; display: flex; flex-direction: column;'):
                    with ui.row().classes('gap-3 w-full').style('flex: 1; min-height: 0;'):
                        # Left column: Job file and controls (flexible width)
                        with ui.column().classes('gap-2').style('flex: 1; min-width: 180px; max-width: 280px;'):
                            create_file_controls()
                            ui.separator()
                            create_job_controls()
                        
                        # Center column: Interactive Toolpath Canvas (Fabric.js)
                        global toolpath_canvas
                        with ui.element('div').style('flex: 0 0 auto; display: flex; flex-direction: column;'):
                            # Load Fabric.js library
                            ui.add_head_html('<script src="https://cdnjs.cloudflare.com/ajax/libs/fabric.js/5.3.1/fabric.min.js"></script>')
                            ui.add_head_html('<script src="/static/toolpath_canvas.js?v=12"></script>')
                            
                            # Create canvas container - fixed aspect ratio based on work area
                            toolpath_canvas = ui.html('''
                                <div id="canvas-container" style="width: calc((100vh - 224px) * 1.57); height: calc(100vh - 224px); min-height: 400px; background-color: #1e1e1e; border-radius: 4px; overflow: hidden;">
                                    <canvas id="toolpath-canvas"></canvas>
                                </div>
                            ''', sanitize=False)
                            
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
                        
                        # Right column: Shape Tools (flexible width)
                        with ui.column().classes('gap-2').style('flex: 1; min-width: 150px; max-width: 220px;'):
                            ui.label('Transform').classes('text-h6 font-bold')
                            with ui.grid(columns=2).classes('gap-2 w-full'):
                                ui.button('⬌', on_click=lambda: ui.run_javascript('window.toolpathCanvas.mirrorX()')).classes('text-h4').style('min-height: 48px; background-color: #333;').tooltip('Mirror X')
                                ui.button('⬍', on_click=lambda: ui.run_javascript('window.toolpathCanvas.mirrorY()')).classes('text-h4').style('min-height: 48px; background-color: #333;').tooltip('Mirror Y')
                            
                            # Rotation input
                            with ui.row().classes('w-full gap-1 items-center'):
                                rotate_input = ui.number(value=90, format='%.0f').props('dense outlined').style('width: 70px;')
                                ui.label('°').classes('text-body1')
                                ui.button('↻', on_click=lambda: ui.run_javascript(f'window.toolpathCanvas.rotateByDegrees({rotate_input.value})')).classes('text-h5').style('min-height: 40px; background-color: #333;').tooltip('Rotate CW')
                                ui.button('↺', on_click=lambda: ui.run_javascript(f'window.toolpathCanvas.rotateByDegrees(-{rotate_input.value})')).classes('text-h5').style('min-height: 40px; background-color: #333;').tooltip('Rotate CCW')
                            
                            # Scale input
                            with ui.row().classes('w-full gap-1 items-center'):
                                scale_input = ui.number(value=100, format='%.0f').props('dense outlined').style('width: 70px;')
                                ui.label('%').classes('text-body1')
                                ui.button('Scale', on_click=lambda: ui.run_javascript(f'window.toolpathCanvas.scaleShape({scale_input.value / 100})')).classes('flex-1').style('min-height: 40px; background-color: #333;')
                            
                            ui.label('Pattern').classes('text-h6 font-bold mt-3')
                            # Grid inputs (auto-spacing with 15mm buffer)
                            with ui.row().classes('w-full gap-1 items-center'):
                                grid_x = ui.number(value=2, format='%.0f', min=1, max=10).props('dense outlined').style('width: 50px;')
                                ui.label('×').classes('text-body1')
                                grid_y = ui.number(value=2, format='%.0f', min=1, max=10).props('dense outlined').style('width: 50px;')
                                ui.button('Grid', on_click=lambda: ui.run_javascript(f'window.toolpathCanvas.gridArray({int(grid_x.value)}, {int(grid_y.value)})')).classes('flex-1').style('min-height: 40px; background-color: #333;')
            
            # GCODE tab - Manual G-code command interface
            with ui.tab_panel(gcode_tab).style('height: 100%; overflow: hidden;'):
                with ui.card().classes('w-full h-full').style('padding: 16px;'):
                    ui.label('Manual G-code Commands').classes('text-h5 font-bold mb-2')
                    
                    # Command input
                    with ui.row().classes('w-full gap-2 items-center mb-4'):
                        gcode_input = ui.input('Enter G-code command').classes('flex-1').props('outlined')
                        
                        async def send_gcode():
                            cmd = gcode_input.value.strip()
                            if cmd:
                                response_log.push(f'>>> {cmd}')
                                response = cnc_controller.send_command_with_response(cmd, timeout=10.0)
                                for line in response.split('\n'):
                                    response_log.push(f'<<< {line}')
                                gcode_input.value = ''
                        
                        ui.button('Send', on_click=send_gcode, icon='send').props('color=primary')
                    
                    # Common commands
                    ui.label('Quick Commands:').classes('text-h6 text-grey-7 mb-1')
                    with ui.row().classes('gap-2 mb-4'):
                        ui.button('M115 (Firmware)', on_click=lambda: [gcode_input.set_value('M115'), send_gcode()]).props('size=md outline')
                        ui.button('M114 (Position)', on_click=lambda: [gcode_input.set_value('M114'), send_gcode()]).props('size=md outline')
                        ui.button('M503 (Settings)', on_click=lambda: [gcode_input.set_value('M503'), send_gcode()]).props('size=md outline')
                        ui.button('M999 (Reset)', on_click=lambda: [gcode_input.set_value('M999'), send_gcode()]).props('size=md outline color=orange')
                    
                    # Response log
                    ui.label('Response Log:').classes('text-h6 text-grey-7 mb-1')
                    response_log = ui.log().classes('w-full').style('height: 280px; font-family: monospace; font-size: 20px;')
                    
                    # Allow enter key to send command
                    gcode_input.on('keydown.enter', send_gcode)
            
            # System tab - WiFi, connection info, and system controls
            with ui.tab_panel(wifi_tab).style('height: 100%; overflow: hidden;'):
                with ui.card().classes('w-full h-full').style('padding: 16px;'):
                    with ui.row().classes('w-full gap-8'):
                        # Left column: Connection info
                        with ui.column().classes('gap-4').style('flex: 1;'):
                            ui.label('Connection Info').classes('text-h5 font-bold mb-2')
                            
                            # Connection status
                            with ui.row().classes('items-center gap-2'):
                                ui.label('CNC Status:').classes('text-h6 text-grey-7')
                                sys_connection_icon = ui.icon('check_circle', color='green').classes('text-h5')
                                sys_connection_label = ui.label('Connected').classes('text-h6 font-bold')
                                
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
                                ui.label('IP Address:').classes('text-h6 text-grey-7')
                                ui.label(f'http://{local_ip}:8080').classes('text-h6 font-bold bg-grey-2 px-3 py-1 rounded')
                            
                            ui.separator().classes('my-4')
                            
                            ui.label('System Controls').classes('text-h5 font-bold mb-2')
                            
                            with ui.row().classes('gap-4'):
                                def restart_service():
                                    ui.notify('Restarting service...', type='warning')
                                    subprocess.Popen(['sudo', 'systemctl', 'restart', 'fabcnc.service'], 
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                
                                ui.button('Restart Service', icon='refresh', on_click=restart_service) \
                                    .props('color=warning size=lg').style('font-size: 20px;')
                                
                                def reboot_system():
                                    ui.notify('Rebooting system...', type='warning')
                                    subprocess.Popen(['sudo', 'reboot'], 
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                
                                ui.button('Reboot System', icon='restart_alt', on_click=reboot_system) \
                                    .props('color=negative size=lg').style('font-size: 20px;')
        
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
