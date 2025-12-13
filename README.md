# fabCNC - Raspberry Pi CNC Web Controller

A clean, Material Design web-based CNC controller built with Python and NiceGUI for Raspberry Pi 5.

## Features

- **Manual Jogging**: Control X, Y, Z linear axes and A rotary axis with adjustable step sizes
- **Homing**: Individual axis homing and home-all functionality
- **File Management**: Upload and load DXF files for job execution
- **Job Control**: Start, pause, resume, and stop job execution
- **Real-time Status**: Live position display and job progress tracking
- **Network Access**: Accessible from any device on your local network

## Requirements

- Python 3.10 or higher
- Raspberry Pi 5 (or any Linux/macOS/Windows for development)
- Network connection

## Installation

1. Clone or download this repository:
```bash
cd /Users/peterbriggs/Code/fabCNC
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

## Running the Application

Start the web server:
```bash
cd cnc_ui
python main.py
```

The application will be available at:
- Local access: http://localhost:8080
- Network access: http://<raspberry-pi-ip>:8080

## Project Structure

```
cnc_ui/
├── main.py              # NiceGUI application entry point
├── cnc/
│   ├── controller.py    # CNC command interface (stubbed for simulation)
│   ├── state.py         # Shared machine and job state
│   └── files.py         # File handling and storage
├── uploads/             # Uploaded DXF files storage
└── system/              # System configuration (for auto-start)
```

## Usage

### Manual Jogging

1. Adjust jog parameters (step size and feed rate) as needed
2. Use the X+/X-, Y+/Y-, Z+/Z-, A+/A- buttons to move axes
3. All jog controls are disabled during job execution

### Homing

1. Use individual axis home buttons (Home X, Home Y, Home Z, Home A)
2. Or use "Home All" to home all axes sequentially
3. Homing sets the axis position to zero

### Job Execution

1. Click "Load DXF File" and select a DXF file
2. File is uploaded and stub G-code is generated
3. Click "Start" to begin job execution
4. Use "Pause"/"Resume" to control job flow
5. Click "Stop" to immediately halt the job

### Position Display

Real-time display of all axis positions:
- X, Y, Z in millimeters (mm)
- A in degrees (°)

### Status Display

Shows current machine status:
- Idle
- Loaded: [filename]
- Running
- Paused
- Complete
- Stopped

Progress bar displays job completion percentage.

## Auto-Start on Raspberry Pi (Kiosk Mode)

To configure the application to auto-start in Chromium kiosk mode on boot:

1. Create a systemd service for the Python application
2. Configure Chromium to launch in kiosk mode at http://localhost:8080
3. See `system/` directory for configuration templates

## Development Notes

### Current Implementation

This is a **simulation/stub implementation** with the following characteristics:

- CNC commands are simulated (no actual hardware control)
- DXF parsing is stubbed (returns sample G-code)
- G-code execution is simulated with a simple progress loop
- File uploads are saved to the `uploads/` directory

### Production Integration

To integrate with actual CNC hardware:

1. **Replace `cnc/controller.py`**: Implement actual GRBL or other CNC protocol communication
2. **Add DXF parsing**: Integrate a DXF parsing library and CAM toolpath generation
3. **Real G-code execution**: Send commands to CNC controller and track actual position
4. **Add limit switches**: Implement limit switch monitoring and E-stop handling
5. **Position feedback**: Read actual position from CNC controller

## Architecture

### Thread-Safe State Management

The application uses a thread-safe `MachineState` object that:
- Stores current machine position (X, Y, Z, A)
- Tracks machine status (busy, paused, job_loaded)
- Provides job progress updates
- Uses threading locks for safe concurrent access

### Non-Blocking UI

- All CNC operations run in background threads
- UI updates at 10 Hz using NiceGUI timers
- No blocking operations in UI callbacks

### Material Design

The interface follows Material Design principles with:
- Clean card-based layout
- Consistent spacing and typography
- Responsive grid layout
- Color-coded action buttons (positive/warning/negative)

## Units

- **Linear axes (X, Y, Z)**: Millimeters (mm)
- **Rotary axis (A)**: Degrees (°)
- **Feed rate**: mm/min

## Safety

- No software E-stop implemented (physical E-stop required)
- Job controls disabled during manual operations
- Manual controls disabled during job execution

## License

MIT License - Feel free to modify and use for your CNC projects.

## Support

For issues or questions, please refer to the project repository.
