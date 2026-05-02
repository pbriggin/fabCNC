# fabCNC - Fabric CNC Web Controller

A web-based controller for a 4-axis fabric CNC cutting machine, built with Python and NiceGUI. Designed to run on a Raspberry Pi 5 with Marlin firmware over serial.

## Features

- **Manual Jogging**: Control X, Y, Z linear axes and A rotary (blade angle) axis with configurable step sizes and feed rate
- **Homing**: Individual axis and home-all functionality via Marlin `G28`
- **DXF Processing**: Upload DXF files; shapes are extracted and converted to point lists using `ezdxf`
- **Toolpath Generation**: Converts DXF shapes to G-code with Z-height management, corner handling (raise/rotate/lower), and adaptive curve feed rate slowdown
- **Shape Nesting**: Optimally pack multiple DXF shapes onto a sheet using the Packaide library (configurable spacing and rotation increments)
- **Toolpath Preview**: Interactive 2D canvas visualization of the generated toolpath with tool orientation (A-axis) overlay
- **Job Control**: Start, pause, resume, and stop G-code job execution with real-time progress tracking
- **Real-time Status**: Live position display (X, Y, Z in mm; A in degrees), job progress bar, and estimated time remaining
- **Network Access**: Accessible from any device on your local network; local IP shown in the UI
- **Auto-Update**: Git-based update checking built into the UI

## Requirements

- Python 3.10 or higher
- Raspberry Pi 5 (or any Linux/macOS for development)
- Marlin-based CNC controller connected via USB serial (auto-detected at 115200 baud)
- Network connection

## Installation

1. Clone this repository:
```bash
git clone <repo-url>
cd fabCNC
```

2. Run the setup script:
```bash
./setup.sh
```

This creates a `.venv` virtual environment, installs all dependencies, and verifies the install. Run it once after cloning.

> **Note:** `packaide` (shape nesting) is a C++ library built from source. The setup script handles this automatically, but requires `cmake`, `boost`, and `cgal` — installed via Homebrew on macOS or `apt` on Linux. If the build fails, the app still works; only the nesting feature will be unavailable.

## Running the Application

```bash
source .venv/bin/activate
cd cnc_ui && python main.py
```

The application will be available at:
- Local access: http://localhost:8080
- Network access: http://\<raspberry-pi-ip\>:8080

## Project Structure

```
cnc_ui/
├── main.py                        # NiceGUI application entry point; UI layout and API endpoints
├── cnc/
│   ├── controller.py              # Marlin serial controller (auto-connects, streams G-code)
│   ├── state.py                   # Thread-safe MachineState (position, status, job progress)
│   └── files.py                   # DXF file upload and storage management
├── dxf_processing/
│   └── dxf_processor.py           # DXF → point-list conversion using ezdxf
├── toolpath_planning/
│   ├── toolpath_generator.py      # Point lists → G-code with Z/A axis management
│   └── gcode_visualizer.py        # G-code → matplotlib 2D toolpath visualization
├── static/
│   └── toolpath_canvas.js         # Client-side canvas rendering for toolpath preview
└── uploads/                       # Uploaded DXF files
```

## Usage

### Manual Jogging

1. Set step size (XY, Z, A) and feed rate in the jog controls panel
2. Use the axis buttons (X+/X−, Y+/Y−, Z+/Z−, A+/A−) to move
3. Jogging is disabled during job execution

### Loading and Running a Job

1. Upload a DXF file via the file picker
2. The DXF is processed into shapes and a toolpath is generated automatically
3. Review the toolpath in the 2D canvas preview
4. Optionally use the nesting panel to arrange multiple shapes on the sheet
5. Click **Start** to stream G-code to the machine
6. Use **Pause** / **Resume** to hold or continue; **Stop** to abort immediately

### Shape Nesting

- Open the nesting panel and select the shapes to nest
- Configure sheet dimensions (default 1720 × 1660 mm), spacing (mm), and rotation increments
- Click **Nest** to run Packaide; the nested layout is shown in the preview
- Proceed to generate the toolpath from the nested arrangement

### Toolpath Parameters

The toolpath generator uses the following defaults (configurable in `main.py`):

| Parameter | Default | Description |
|---|---|---|
| Cutting height (Z) | −30 mm | Z when blade is down |
| Safe height (Z) | −15 mm | Z when blade is raised |
| Corner angle threshold | 30° | Angle above which Z raises at corners |
| Feed rate | 15,000 mm/min | Cutting speed |
| Plunge rate | 12,000 mm/min | Z plunge speed |
| Rapid rate | 18,000 mm/min | Travel moves |
| Min curve feed rate | 1,000 mm/min | Speed floor for tight curves |
| Curve slowdown radius | 75 mm | Start slowing below this arc radius |

## Architecture

### Marlin Serial Communication

`controller.py` auto-detects the connected serial port and verifies Marlin with `M115`. G-code jobs are streamed with flow control: the controller tracks the firmware command buffer and waits for `ok` responses before sending the next line. Position is polled continuously from `M114` responses.

### Thread-Safe State

`MachineState` uses a `threading.Lock` to guard all reads and writes. Background threads handle serial I/O and job streaming; the NiceGUI UI reads state at ~10 Hz via timers without blocking the event loop.

### DXF → G-code Pipeline

1. `DXFProcessor` reads a DXF file and converts all entities (splines, polylines, arcs, etc.) into lists of `(x, y)` points sampled at a configurable angular resolution
2. `ToolpathGenerator` iterates the point lists, computes segment angles, and emits G-code with:
   - `G0` rapid moves between shapes
   - `G1` cutting moves with adaptive feed rate based on local curve radius
   - Z raise/lower sequences at corners exceeding the angle threshold
   - Continuous A-axis rotation to keep the cutting blade tangent to the path

## Auto-Start on Raspberry Pi

On Linux, `setup.sh` automatically installs and enables two systemd services — no extra steps needed.

### WiFi Provisioning

If the Pi has no saved WiFi credentials on boot, it creates a hotspot called **`fabCNC Setup`**. Connect to it from any phone or laptop and a captive portal lets you select a network and enter the password. Once connected, the hotspot closes and the app starts.

This uses [wifi-connect](https://github.com/balena-os/wifi-connect) (by balena.io), which requires NetworkManager — `setup.sh` handles that automatically.

### Web Server

Once WiFi is up, the `fabcnc` service starts the NiceGUI app. It's accessible at `http://<pi-ip>:8080` from any device on the network.

Useful commands:
```bash
sudo systemctl start fabcnc          # start app now
sudo systemctl status fabcnc         # check app status
sudo systemctl status wifi-provision # check WiFi provisioning status
journalctl -u fabcnc -f              # view app logs
journalctl -u wifi-provision -f      # view provisioning logs
```

## Units

- **Linear axes (X, Y, Z)**: Millimeters (mm)
- **Rotary axis (A)**: Degrees (°)
- **Feed rate**: mm/min

## Safety

- Physical E-stop required; no software E-stop is implemented
- Job controls are disabled during manual operations and vice versa

## License

MIT License
