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


# Jog parameters (user-adjustable)
jog_params = {
    'xy_step': 10.0,  # mm
    'z_step': 1.0,    # mm
    'a_step': 45.0,   # degrees
    'feed_rate': 1000.0,  # mm/min
}

# Current loaded G-code
current_gcode = []


def create_header():
    """Create the application header."""
    with ui.header().classes('items-center justify-between bg-primary text-white py-2 px-4'):
        ui.label('fabCNC Controller').classes('text-h5 sm:text-h4')
        with ui.row().classes('items-center gap-4'):
            ui.label('Raspberry Pi 5 CNC').classes('text-body2 sm:text-body1')


def create_position_display():
    """Create the compact position display."""
    with ui.row().classes('w-full items-center gap-2 sm:gap-4 flex-wrap'):
        pos_labels = {}
        for axis in ['X', 'Y', 'Z', 'A']:
            with ui.row().classes('items-center gap-1 sm:gap-2'):
                ui.label(f'{axis}:').classes('text-body2 sm:text-body1 text-grey-7')
                unit = '°' if axis == 'A' else 'mm'
                pos_labels[axis] = ui.label(f'0.00 {unit}').classes('text-body1 sm:text-h6 font-bold')
    
    return pos_labels


def create_status_display():
    """Create the compact status and progress display."""
    with ui.column().classes('w-full gap-2'):
        with ui.row().classes('items-center gap-2'):
            ui.label('Status:').classes('text-body2 sm:text-body1 text-grey-7')
            status_label = ui.label('Idle').classes('text-body1 sm:text-h6 font-bold')
        progress_bar = ui.linear_progress(value=0.0, show_value=False).classes('w-full')
    
    return status_label, progress_bar


def create_jog_controls():
    """Create the compact jog control panel."""
    with ui.row().classes('w-full items-start gap-2 sm:gap-4 md:gap-6 flex-wrap'):
        # XY jogging
        with ui.column().classes('items-center gap-1'):
            ui.label('XY').classes('text-caption text-grey-7')
            
            with ui.column().classes('items-center gap-1'):
                # Y+
                ui.button('Y+', on_click=lambda: jog_axis('Y', jog_params['xy_step'])) \
                    .props('dense size=sm') \
                    .classes('w-16 sm:w-20') \
                    .bind_enabled_from(machine_state, '_lock', 
                                     backward=lambda _: machine_state.is_idle())
                
                # X-, X+
                with ui.row().classes('gap-1'):
                    ui.button('X-', on_click=lambda: jog_axis('X', -jog_params['xy_step'])) \
                        .props('dense size=sm') \
                        .classes('w-16 sm:w-20') \
                        .bind_enabled_from(machine_state, '_lock',
                                         backward=lambda _: machine_state.is_idle())
                    ui.button('X+', on_click=lambda: jog_axis('X', jog_params['xy_step'])) \
                        .props('dense size=sm') \
                        .classes('w-16 sm:w-20') \
                        .bind_enabled_from(machine_state, '_lock',
                                         backward=lambda _: machine_state.is_idle())
                
                # Y-
                ui.button('Y-', on_click=lambda: jog_axis('Y', -jog_params['xy_step'])) \
                    .props('dense size=sm') \
                    .classes('w-16 sm:w-20') \
                    .bind_enabled_from(machine_state, '_lock',
                                     backward=lambda _: machine_state.is_idle())
        
        # Z jogging
        with ui.column().classes('items-center gap-1'):
            ui.label('Z').classes('text-caption text-grey-7')
            
            with ui.column().classes('gap-1'):
                ui.button('Z+', on_click=lambda: jog_axis('Z', jog_params['z_step'])) \
                    .props('dense size=sm') \
                    .classes('w-16 sm:w-20') \
                    .bind_enabled_from(machine_state, '_lock',
                                     backward=lambda _: machine_state.is_idle())
                ui.button('Z-', on_click=lambda: jog_axis('Z', -jog_params['z_step'])) \
                    .props('dense size=sm') \
                    .classes('w-16 sm:w-20') \
                    .bind_enabled_from(machine_state, '_lock',
                                     backward=lambda _: machine_state.is_idle())
        
        # A jogging
        with ui.column().classes('items-center gap-1'):
            ui.label('A').classes('text-caption text-grey-7')
            
            with ui.column().classes('gap-1'):
                ui.button('A+', on_click=lambda: jog_axis('A', jog_params['a_step'])) \
                    .props('dense size=sm') \
                    .classes('w-16 sm:w-20') \
                    .bind_enabled_from(machine_state, '_lock',
                                     backward=lambda _: machine_state.is_idle())
                ui.button('A-', on_click=lambda: jog_axis('A', -jog_params['a_step'])) \
                    .props('dense size=sm') \
                    .classes('w-16 sm:w-20') \
                    .bind_enabled_from(machine_state, '_lock',
                                     backward=lambda _: machine_state.is_idle())
        
        # Jog parameters - compact vertical layout
        with ui.column().classes('gap-1'):
            ui.label('Steps & Speed').classes('text-caption text-grey-7')
            
            ui.number('XY', value=jog_params['xy_step'], min=0.1, max=100, step=0.1,
                     on_change=lambda e: jog_params.update({'xy_step': e.value})) \
                .props('dense suffix=mm') \
                .classes('w-32 sm:w-40')
            
            ui.number('Z', value=jog_params['z_step'], min=0.1, max=50, step=0.1,
                     on_change=lambda e: jog_params.update({'z_step': e.value})) \
                .props('dense suffix=mm') \
                .classes('w-32 sm:w-40')
            
            ui.number('A', value=jog_params['a_step'], min=1, max=360, step=1,
                     on_change=lambda e: jog_params.update({'a_step': e.value})) \
                .props('dense suffix=°') \
                .classes('w-32 sm:w-40')
            
            ui.number('Feed', value=jog_params['feed_rate'], min=10, max=5000, step=10,
                     on_change=lambda e: jog_params.update({'feed_rate': e.value})) \
                .props('dense suffix=mm/min') \
                .classes('w-32 sm:w-40')


def create_homing_controls():
    """Create the compact homing control panel."""
    with ui.column().classes('gap-2'):
        ui.label('Homing').classes('text-caption text-grey-7')
        
        with ui.row().classes('gap-1 sm:gap-2'):
            ui.button('X', on_click=lambda: home_axis('X')) \
                .props('dense size=sm') \
                .classes('w-12 sm:w-16') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.is_idle())
            ui.button('Y', on_click=lambda: home_axis('Y')) \
                .props('dense size=sm') \
                .classes('w-12 sm:w-16') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.is_idle())
            ui.button('Z', on_click=lambda: home_axis('Z')) \
                .props('dense size=sm') \
                .classes('w-12 sm:w-16') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.is_idle())
            ui.button('A', on_click=lambda: home_axis('A')) \
                .props('dense size=sm') \
                .classes('w-12 sm:w-16') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.is_idle())
        
        ui.button('Home All', on_click=home_all, color='primary') \
            .props('dense size=sm') \
            .classes('w-full') \
            .bind_enabled_from(machine_state, '_lock',
                             backward=lambda _: machine_state.is_idle())


def create_file_controls():
    """Create the compact file upload and management panel."""
    with ui.column().classes('w-full gap-2'):
        ui.label('Job File').classes('text-body2 font-bold')
        
        loaded_file_label = ui.label('No file loaded').classes('text-caption text-grey-7')
        
        upload = ui.upload(
            label='Load DXF File',
            auto_upload=True,
            on_upload=lambda e: handle_file_upload(e, loaded_file_label)
        ).props('accept=.dxf dense').classes('w-full')
        
        return loaded_file_label


def create_job_controls():
    """Create the compact job execution control panel."""
    with ui.column().classes('w-full gap-2'):
        ui.label('Job Control').classes('text-body2 font-bold')
        
        with ui.row().classes('gap-1'):
            start_btn = ui.button('Start', on_click=start_job, color='positive') \
                .props('dense size=sm') \
                .classes('flex-1') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.job_loaded and machine_state.is_idle())
            
            pause_btn = ui.button('Pause', on_click=pause_job, color='warning') \
                .props('dense size=sm') \
                .classes('flex-1') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.is_running())
            
            resume_btn = ui.button('Resume', on_click=resume_job, color='positive') \
                .props('dense size=sm') \
                .classes('flex-1') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.paused)
            
            stop_btn = ui.button('Stop', on_click=stop_job, color='negative') \
                .props('dense size=sm') \
                .classes('flex-1') \
                .bind_enabled_from(machine_state, '_lock',
                                 backward=lambda _: machine_state.busy)


# Event handlers

def jog_axis(axis: str, distance: float):
    """Handle jog button click."""
    cnc_controller.jog(axis, distance, jog_params['feed_rate'])


def home_axis(axis: str):
    """Handle home axis button click."""
    cnc_controller.home_axis(axis)


def home_all():
    """Handle home all button click."""
    cnc_controller.home_all()


def handle_file_upload(event, label):
    """Handle file upload event."""
    global current_gcode
    
    # Save uploaded file - event.content contains the file data
    # event has 'name' attribute for filename
    filename = event.name if hasattr(event, 'name') else 'uploaded.dxf'
    
    # Write the uploaded content to a temporary location
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False, suffix='.dxf') as tmp:
        tmp.write(event.content.read())
        tmp_path = tmp.name
    
    # Save to uploads directory
    saved_path = file_manager.save_uploaded_file(tmp_path, filename)
    
    # Clean up temp file
    import os
    os.unlink(tmp_path)
    
    # Generate stub G-code
    current_gcode = file_manager.get_gcode_stub(saved_path)
    
    # Update state
    machine_state.set_job_loaded(True, filename)
    label.set_text(f'Loaded: {filename}')
    
    ui.notify(f'File loaded: {filename}', type='positive')


def start_job():
    """Handle start job button click."""
    if current_gcode:
        cnc_controller.start_job(current_gcode)
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
    create_header()
    
    with ui.column().classes('w-full max-w-7xl mx-auto h-screen p-2 sm:p-4 gap-2 sm:gap-3'):
        # Top status bar - always visible
        with ui.card().classes('w-full').style('padding: 8px 12px'):
            pos_labels = create_position_display()
            ui.separator().classes('my-1')
            status_label, progress_bar = create_status_display()
        
        # Tabbed interface for different control sections
        with ui.tabs().classes('w-full') as tabs:
            control_tab = ui.tab('Control', icon='gamepad')
            job_tab = ui.tab('Job', icon='work')
            toolpath_tab = ui.tab('Toolpath', icon='route')
        
        with ui.tab_panels(tabs, value=control_tab).classes('w-full flex-1'):
            # Control tab - Manual jogging and homing
            with ui.tab_panel(control_tab):
                with ui.card().classes('w-full h-full').style('padding: 12px 16px'):
                    with ui.row().classes('w-full items-start gap-4 sm:gap-6 md:gap-8'):
                        create_jog_controls()
                        ui.separator().props('vertical').classes('hidden sm:flex')
                        create_homing_controls()
            
            # Job tab - File loading and job execution
            with ui.tab_panel(job_tab):
                with ui.card().classes('w-full h-full').style('padding: 12px 16px'):
                    with ui.column().classes('gap-4 max-w-2xl'):
                        create_file_controls()
                        ui.separator()
                        create_job_controls()
            
            # Toolpath tab - Future toolpath visualization
            with ui.tab_panel(toolpath_tab):
                with ui.card().classes('w-full h-full').style('padding: 12px 16px'):
                    with ui.column().classes('items-center justify-center h-full'):
                        ui.icon('route', size='xl').classes('text-grey-5')
                        ui.label('Toolpath Visualization').classes('text-h6 sm:text-h5 text-grey-7')
                        ui.label('Coming soon - will display toolpath plot here').classes('text-caption sm:text-body2 text-grey-5')
        
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
