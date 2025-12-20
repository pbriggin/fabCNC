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
import matplotlib.pyplot as plt
from dxf_processing.dxf_processor import DXFProcessor
from toolpath_planning.toolpath_generator import ToolpathGenerator
from toolpath_planning.gcode_visualizer import GCodeVisualizer
import logging

# Configure logging to see all debug output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)

# Application version
APP_VERSION = "v1.0.0"


# Jog parameters (user-adjustable)
jog_params = {
    'xy_step': 10.0,  # mm
    'z_step': 1.0,    # mm
    'a_step': 45.0,   # degrees
    'feed_rate': 4000.0,  # mm/min
}

# DXF processing and toolpath generation
dxf_processor = DXFProcessor()
toolpath_generator = ToolpathGenerator(
    cutting_height=-20.0,  # Z height when cutting (mm)
    safe_height=-15.0,     # Z height when raised (mm)
    corner_angle_threshold=15.0,
    feed_rate=3000.0,      # mm/min
    plunge_rate=3000.0     # mm/min
)

# Global storage for current toolpath visualization data
current_toolpath_shapes = {}
toolpath_plot = None  # Reference to the pyplot element for updates

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
    """Create the application header."""
    with ui.header().classes('items-center justify-between bg-primary text-white py-2 px-4'):
        ui.label('fabCNC Controller').classes('text-h4 font-bold')
        with ui.row().classes('items-center gap-4'):
            ui.label(APP_VERSION).classes('text-h6')


def create_position_display():
    """Create the compact position display."""
    pos_labels = {}
    for axis in ['X', 'Y', 'Z', 'A']:
        with ui.row().classes('items-center gap-1'):
            ui.label(f'{axis}:').classes('text-h6 text-grey-7')
            unit = '°' if axis == 'A' else 'mm'
            pos_labels[axis] = ui.label(f'0.00 {unit}').classes('text-h5 font-bold')
    
    return pos_labels


def create_status_display():
    """Create the compact status and progress display."""
    with ui.row().classes('items-center gap-2'):
        ui.label('Status:').classes('text-h6 text-grey-7')
        status_label = ui.label('Idle').classes('text-h5 font-bold')
    progress_bar = ui.linear_progress(value=0.0, show_value=False).style('height: 8px')
    
    return status_label, progress_bar


def create_jog_controls():
    """Create jog controls with native buttons."""
    with ui.row().classes('w-full gap-4 items-start'):
        # Step size selectors stacked vertically on the left
        with ui.column().classes('gap-3').style('height: 398px; justify-content: space-between'):
            # XY step selector
            with ui.column().classes('gap-2 flex-1').style('justify-content: center'):
                ui.label('XY Step').classes('text-body1 font-bold text-center mb-1')
                with ui.row().classes('gap-2'):
                    xy_1 = ui.button('1mm', on_click=lambda: [jog_params.update({'xy_step': 1.0}), update_step_buttons()]) \
                        .props('outline').style('min-width: 77px; font-size: 14px; padding: 34px 12px')
                    xy_10 = ui.button('10mm', on_click=lambda: [jog_params.update({'xy_step': 10.0}), update_step_buttons()]) \
                        .props('unelevated color=primary').style('min-width: 77px; font-size: 14px; padding: 34px 12px')
                    xy_100 = ui.button('100mm', on_click=lambda: [jog_params.update({'xy_step': 100.0}), update_step_buttons()]) \
                        .props('outline').style('min-width: 77px; font-size: 14px; padding: 34px 12px')
            
            # Z step selector
            with ui.column().classes('gap-2 flex-1').style('justify-content: center'):
                ui.label('Z Step').classes('text-body1 font-bold text-center')
                with ui.row().classes('gap-2'):
                    z_01 = ui.button('0.1mm', on_click=lambda: [jog_params.update({'z_step': 0.1}), update_step_buttons()]) \
                        .props('outline').style('min-width: 77px; font-size: 14px; padding: 34px 12px')
                    z_1 = ui.button('1mm', on_click=lambda: [jog_params.update({'z_step': 1.0}), update_step_buttons()]) \
                        .props('unelevated color=primary').style('min-width: 77px; font-size: 14px; padding: 34px 12px')
                    z_10 = ui.button('10mm', on_click=lambda: [jog_params.update({'z_step': 10.0}), update_step_buttons()]) \
                        .props('outline').style('min-width: 77px; font-size: 14px; padding: 34px 12px')
            
            # A step selector
            with ui.column().classes('gap-2 flex-1').style('justify-content: center'):
                ui.label('A Step').classes('text-body1 font-bold text-center')
                with ui.row().classes('gap-2'):
                    a_1 = ui.button('1°', on_click=lambda: [jog_params.update({'a_step': 1.0}), update_step_buttons()]) \
                        .props('outline').style('min-width: 77px; font-size: 14px; padding: 34px 12px')
                    a_45 = ui.button('45°', on_click=lambda: [jog_params.update({'a_step': 45.0}), update_step_buttons()]) \
                        .props('unelevated color=primary').style('min-width: 77px; font-size: 14px; padding: 34px 12px')
                    a_90 = ui.button('90°', on_click=lambda: [jog_params.update({'a_step': 90.0}), update_step_buttons()]) \
                        .props('outline').style('min-width: 77px; font-size: 14px; padding: 34px 12px')
                    
            # Store button references for updating
            jog_params['_buttons'] = {
                'xy': {1.0: xy_1, 10.0: xy_10, 100.0: xy_100},
                'z': {0.1: z_01, 1.0: z_1, 10.0: z_10},
                'a': {1.0: a_1, 45.0: a_45, 90.0: a_90}
            }
        
        # 3x3 XY Grid with Home in center
        with ui.column().classes('gap-2').style('height: 398px'):
                    ui.label('XY Control').classes('text-body1 font-bold text-center mb-1')
                    # Row 1
                    with ui.row().classes('gap-2'):
                        ui.button(icon='north_west') \
                            .props('color=blue-grey-6 size=xl') \
                            .style('width: 116px; height: 116px; font-size: 34px') \
                            .on('click', lambda: jog_diagonal(-1, 1)) \
                            .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                        ui.button(icon='north') \
                            .props('color=blue-grey-6 size=xl') \
                            .style('width: 116px; height: 116px; font-size: 34px') \
                            .on('click', lambda: jog_axis('Y', jog_params['xy_step'])) \
                            .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                        ui.button(icon='north_east') \
                            .props('color=blue-grey-6 size=xl') \
                            .style('width: 116px; height: 116px; font-size: 34px') \
                            .on('click', lambda: jog_diagonal(1, 1)) \
                            .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                    
                    # Row 2
                    with ui.row().classes('gap-2'):
                        ui.button(icon='west') \
                            .props('color=blue-grey-6 size=xl') \
                            .style('width: 116px; height: 116px; font-size: 34px') \
                            .on('click', lambda: jog_axis('X', -jog_params['xy_step'])) \
                            .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                        ui.button(icon='home') \
                            .props('color=red-6 size=xl') \
                            .style('width: 116px; height: 116px; font-size: 34px') \
                            .on('click', lambda: home_all()) \
                            .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                        ui.button(icon='east') \
                            .props('color=blue-grey-6 size=xl') \
                            .style('width: 116px; height: 116px; font-size: 34px') \
                            .on('click', lambda: jog_axis('X', jog_params['xy_step'])) \
                            .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                    
                    # Row 3
                    with ui.row().classes('gap-2'):
                        ui.button(icon='south_west') \
                            .props('color=blue-grey-6 size=xl') \
                            .style('width: 116px; height: 116px; font-size: 34px') \
                            .on('click', lambda: jog_diagonal(-1, -1)) \
                            .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                        ui.button(icon='south') \
                            .props('color=blue-grey-6 size=xl') \
                            .style('width: 116px; height: 116px; font-size: 34px') \
                            .on('click', lambda: jog_axis('Y', -jog_params['xy_step'])) \
                            .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                        ui.button(icon='south_east') \
                            .props('color=blue-grey-6 size=xl') \
                            .style('width: 116px; height: 116px; font-size: 34px') \
                            .on('click', lambda: jog_diagonal(1, -1)) \
                            .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                
        # 2x2 Z/A Grid - taller buttons to match XY grid height
        with ui.column().classes('gap-2').style('height: 398px'):
            ui.label('Z / A Control').classes('text-body1 font-bold text-center mb-1')
            # Row 1
            with ui.row().classes('gap-2'):
                ui.button('Z+') \
                    .props('color=green-6 size=xl') \
                    .style('width: 116px; height: 179px; font-size: 25px; font-weight: bold') \
                    .on('click', lambda: jog_axis('Z', jog_params['z_step'])) \
                    .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                ui.button('A+') \
                    .props('color=orange-6 size=xl') \
                    .style('width: 116px; height: 179px; font-size: 25px; font-weight: bold') \
                    .on('click', lambda: jog_axis('A', jog_params['a_step'])) \
                    .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            
            # Row 2
            with ui.row().classes('gap-2'):
                ui.button('Z-') \
                    .props('color=green-6 size=xl') \
                    .style('width: 116px; height: 179px; font-size: 25px; font-weight: bold') \
                    .on('click', lambda: jog_axis('Z', -jog_params['z_step'])) \
                    .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
                ui.button('A-') \
                    .props('color=orange-6 size=xl') \
                    .style('width: 116px; height: 179px; font-size: 25px; font-weight: bold') \
                    .on('click', lambda: jog_axis('A', -jog_params['a_step'])) \
                    .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
        
        # Individual Homing Controls stacked vertically
        with ui.column().classes('gap-2').style('height: 400px'):
            ui.label('Homing').classes('text-body1 font-bold text-center mb-1')
            ui.button('Home X', on_click=lambda: home_axis('X')) \
                .props('color=red-6') \
                .style('min-width: 116px; height: calc((100% - 32px - 24px) / 4); font-size: 17px') \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            
            ui.button('Home Y', on_click=lambda: home_axis('Y')) \
                .props('color=red-6') \
                .style('min-width: 116px; height: calc((100% - 32px - 24px) / 4); font-size: 17px') \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            
            ui.button('Home Z', on_click=lambda: home_axis('Z')) \
                .props('color=red-6') \
                .style('min-width: 116px; height: calc((100% - 32px - 24px) / 4); font-size: 17px') \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())
            
            ui.button('Home A', on_click=lambda: home_axis('A')) \
                .props('color=red-6') \
                .style('min-width: 116px; height: calc((100% - 32px - 24px) / 4); font-size: 17px') \
                .bind_enabled_from(machine_state, '_lock', backward=lambda _: machine_state.is_idle())


def create_homing_controls():
    """Create the compact homing control panel."""
    with ui.column().classes('gap-3'):
        ui.label('Homing').classes('text-h6 text-grey-7 font-bold')
        
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
    with ui.column().classes('w-full gap-3'):
        ui.label('Job File').classes('text-h5 font-bold')
        
        loaded_file_label = ui.label('No file loaded').classes('text-body1 text-grey-7')
        
        upload = ui.upload(
            label='Load DXF File',
            auto_upload=True,
            on_upload=lambda e: handle_file_upload(e, loaded_file_label)
        ).props('accept=.dxf').classes('w-full').style('font-size: 16px')
        
        return loaded_file_label


def create_job_controls():
    """Create the compact job execution control panel."""
    with ui.column().classes('w-full gap-3'):
        ui.label('Job Control').classes('text-h5 font-bold')
        
        with ui.row().classes('gap-2'):
            start_btn = ui.button('Start', on_click=start_job, color='positive') \
                .props('size=lg') \
                .classes('flex-1') \
                .style('font-size: 18px; padding: 12px 16px') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.job_loaded and machine_state.is_idle())
            
            pause_btn = ui.button('Pause', on_click=pause_job, color='warning') \
                .props('size=lg') \
                .classes('flex-1') \
                .style('font-size: 18px; padding: 12px 16px') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.is_running())
            
            resume_btn = ui.button('Resume', on_click=resume_job, color='positive') \
                .props('size=lg') \
                .classes('flex-1') \
                .style('font-size: 18px; padding: 12px 16px') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.paused)
            
            stop_btn = ui.button('Stop', on_click=stop_job, color='negative') \
                .props('size=lg') \
                .classes('flex-1') \
                .style('font-size: 18px; padding: 12px 16px') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.busy)


# Event handlers

def update_step_buttons():
    """Update step button styling to show active selection."""
    if '_buttons' not in jog_params:
        return
    
    # Update XY buttons
    for val, btn in jog_params['_buttons']['xy'].items():
        if val == jog_params['xy_step']:
            btn.props(remove='outline')
            btn.props('unelevated color=primary')
        else:
            btn.props(remove='unelevated color')
            btn.props('outline')
        btn.update()
    
    # Update Z buttons
    for val, btn in jog_params['_buttons']['z'].items():
        if val == jog_params['z_step']:
            btn.props(remove='outline')
            btn.props('unelevated color=primary')
        else:
            btn.props(remove='unelevated color')
            btn.props('outline')
        btn.update()
    
    # Update A buttons
    for val, btn in jog_params['_buttons']['a'].items():
        if val == jog_params['a_step']:
            btn.props(remove='outline')
            btn.props('unelevated color=primary')
        else:
            btn.props(remove='unelevated color')
            btn.props('outline')
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


def update_toolpath_plot(shapes: dict):
    """Update the toolpath visualization with new shapes."""
    global toolpath_plot
    
    if toolpath_plot is None:
        return
    
    # Clear and redraw
    toolpath_plot.clear()
    
    with toolpath_plot:
        fig = plt.gcf()
        fig.clear()
        ax = fig.add_subplot(111)
        fig.set_size_inches(10, 6)
        fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
        ax.set_xlim(0, 1365)
        ax.set_ylim(0, 875)
        ax.set_aspect('auto')
        ax.axis('off')
        
        # Add light grey grid manually (crosshatch pattern)
        grid_color = '#E0E0E0'
        grid_spacing = 35  # mm (divides evenly: 1365/35=39, 875/35=25)
        for x in range(0, 1366, grid_spacing):
            ax.axvline(x, color=grid_color, linewidth=1.5, alpha=0.7)
        for y in range(0, 876, grid_spacing):
            ax.axhline(y, color=grid_color, linewidth=1.5, alpha=0.7)
        
        # Add light grey border
        ax.plot([0, 1365, 1365, 0, 0], [0, 0, 875, 875, 0], color='#E0E0E0', linewidth=2)
        
        # Plot shapes if available
        if shapes:
            colors = ['#2196F3', '#4CAF50', '#FF9800', '#E91E63', '#9C27B0', '#00BCD4']
            for i, (shape_name, points) in enumerate(shapes.items()):
                if points:
                    xs = [p[0] for p in points]
                    ys = [p[1] for p in points]
                    color = colors[i % len(colors)]
                    ax.plot(xs, ys, color=color, linewidth=2, label=shape_name)
    
    toolpath_plot.update()


async def handle_file_upload(event, label):
    """Handle file upload event."""
    global current_gcode, current_toolpath_shapes
    
    import os
    import tempfile
    import math
    
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
        
        # Generate toolpath
        ui.notify('Generating toolpath...', type='info')
        print(f"\n--- Toolpath Generation ---")
        gcode_str = toolpath_generator.generate_toolpath(shapes, source_filename=filename)
        current_gcode = gcode_str.split('\n')
        
        # Debug: Show gcode summary
        g0_count = sum(1 for line in current_gcode if line.startswith('G0'))
        g1_count = sum(1 for line in current_gcode if line.startswith('G1'))
        corner_count = sum(1 for line in current_gcode if 'corner' in line.lower())
        print(f"  Total lines: {len(current_gcode)}")
        print(f"  Rapid moves (G0): {g0_count}")
        print(f"  Cut moves (G1): {g1_count}")
        print(f"  Corners detected: {corner_count}")
        
        # Show A-axis range
        a_values = []
        for line in current_gcode:
            import re
            a_match = re.search(r'A([-\d.]+)', line)
            if a_match:
                a_values.append(float(a_match.group(1)))
        if a_values:
            print(f"  A-axis range: {min(a_values):.1f}° to {max(a_values):.1f}°")
        
        print(f"{'='*60}\n")
        
        # Update visualization
        update_toolpath_plot(shapes)
        
        # Update state
        machine_state.set_job_loaded(True, filename)
        label.set_text(f'Loaded: {filename}')
        
        ui.notify(f'File loaded: {filename} ({len(shapes)} shapes, {len(current_gcode)} lines of G-code)', type='positive')
    except Exception as e:
        ui.notify(f'Error processing DXF: {str(e)}', type='negative')
        import traceback
        traceback.print_exc()


def start_job():
    """Handle start job button click - streams gcode via serial."""
    if current_gcode:
        ui.notify('Starting job...', type='info')
        cnc_controller.start_job(current_gcode, use_sd=False)
        ui.notify('Job started', type='positive')


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


def update_ui(pos_labels, status_label, progress_bar):
    """Update UI with current machine state (called periodically)."""
    # Update position display
    x, y, z, a = machine_state.get_position()
    pos_labels['X'].set_text(f'{x:.2f} mm')
    pos_labels['Y'].set_text(f'{y:.2f} mm')
    pos_labels['Z'].set_text(f'{z:.2f} mm')
    pos_labels['A'].set_text(f'{a:.2f} °')
    
    # Update status
    status_label.set_text(machine_state.status_text)
    
    # Update progress
    progress_bar.set_value(machine_state.job_progress)


@ui.page('/')
def main_page():
    """Main application page with responsive tabbed interface optimized for 1280x720 and larger."""
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
    
    create_header()
    
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
    
    with ui.column().classes('w-full max-w-7xl mx-auto gap-2').style('height: 100vh; overflow: hidden; padding: 12px;'):
        # Top status bar - position and status on same row
        with ui.card().classes('w-full').style('padding: 8px 12px'):
            with ui.row().classes('w-full items-center gap-4 justify-between'):
                with ui.row().classes('items-center gap-3'):
                    pos_labels = create_position_display()
                ui.separator().props('vertical')
                status_label, progress_bar = create_status_display()
            progress_bar.classes('w-full mt-2')
        
        # Tabbed interface for different control sections
        with ui.tabs().classes('w-full') as tabs:
            control_tab = ui.tab('Control', icon='gamepad').style('font-size: 16px; min-height: 50px')
            job_tab = ui.tab('Toolpath', icon='route').style('font-size: 16px; min-height: 50px')
            gcode_tab = ui.tab('GCODE', icon='terminal').style('font-size: 16px; min-height: 50px')
        
        with ui.tab_panels(tabs, value=control_tab).classes('w-full').style('flex: 1; min-height: 0; overflow: hidden;'):
            # Control tab - Manual jogging and homing
            with ui.tab_panel(control_tab).classes('w-full').style('height: 100%; overflow: hidden; max-height: 475px;'):
                with ui.card().classes('w-full h-full').style('padding: 12px 16px; overflow: hidden;'):
                    create_jog_controls()
            
            # Job tab - File loading, job execution, and toolpath visualization
            with ui.tab_panel(job_tab).style('height: 100%; overflow: hidden; max-height: 475px;'):
                with ui.card().classes('w-full h-full').style('padding: 12px 16px; overflow: hidden;'):
                    with ui.row().classes('gap-4 w-full h-full'):
                        # Left column: Job file and controls
                        with ui.column().classes('gap-4').style('flex: 0 0 400px;'):
                            create_file_controls()
                            ui.separator()
                            create_job_controls()
                        
                        # Right column: Toolpath visualization
                        global toolpath_plot
                        with ui.column().style('flex: 1; min-width: 0; height: 100%; display: flex;'):
                            # Create matplotlib plot for toolpath
                            toolpath_plot = ui.pyplot(close=False).style('width: 100%; height: 100%; flex: 1;')
                            with toolpath_plot:
                                fig = plt.gcf()
                                fig.set_size_inches(10, 6)
                                fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
                                ax = fig.add_subplot(111)
                                ax.set_xlim(0, 1365)
                                ax.set_ylim(0, 875)
                                ax.set_aspect('auto')
                                ax.axis('off')
                                # Add light grey grid manually (crosshatch pattern)
                                grid_color = '#E0E0E0'
                                grid_spacing = 35  # mm (divides evenly: 1365/35=39, 875/35=25)
                                for x in range(0, 1366, grid_spacing):
                                    ax.axvline(x, color=grid_color, linewidth=1.5, alpha=0.7)
                                for y in range(0, 876, grid_spacing):
                                    ax.axhline(y, color=grid_color, linewidth=1.5, alpha=0.7)
                                # Add light grey border
                                ax.plot([0, 1365, 1365, 0, 0], [0, 0, 875, 875, 0], color='#E0E0E0', linewidth=2)
            
            # GCODE tab - Manual G-code command interface
            with ui.tab_panel(gcode_tab).style('height: 100%; overflow: hidden; max-height: 475px;'):
                with ui.card().classes('w-full h-full').style('padding: 16px;'):
                    ui.label('Manual G-code Commands').classes('text-h6 mb-2')
                    
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
                    ui.label('Quick Commands:').classes('text-subtitle2 text-grey-7 mb-1')
                    with ui.row().classes('gap-2 mb-4'):
                        ui.button('M115 (Firmware)', on_click=lambda: [gcode_input.set_value('M115'), send_gcode()]).props('size=sm outline')
                        ui.button('M114 (Position)', on_click=lambda: [gcode_input.set_value('M114'), send_gcode()]).props('size=sm outline')
                        ui.button('M503 (Settings)', on_click=lambda: [gcode_input.set_value('M503'), send_gcode()]).props('size=sm outline')
                        ui.button('M999 (Reset)', on_click=lambda: [gcode_input.set_value('M999'), send_gcode()]).props('size=sm outline color=orange')
                    
                    # Response log
                    ui.label('Response Log:').classes('text-subtitle2 text-grey-7 mb-1')
                    response_log = ui.log().classes('w-full').style('height: 280px; font-family: monospace; font-size: 13px;')
                    
                    # Allow enter key to send command
                    gcode_input.on('keydown.enter', send_gcode)
        
        # Start periodic UI update timer (10 Hz = 100ms)
        ui.timer(0.1, lambda: update_ui(pos_labels, status_label, progress_bar))


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
