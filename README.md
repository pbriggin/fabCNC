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
- Local access: http://127.0.0.1:8080
- Network access (by hostname): http://fabcnc.local:8080
- Network access (by IP): http://\<raspberry-pi-ip\>:8080

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

Once WiFi is up, the `fabcnc` service starts the NiceGUI app. It's accessible at `http://fabcnc.local:8080` from any Mac/iOS device on the network (via mDNS/Bonjour), or by IP at `http://<pi-ip>:8080`.

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

## Logging & Remote Diagnostics

fabCNC writes structured logs to `cnc_ui/logs/` and can ship them to a remote
endpoint on a schedule — designed for headless Raspberry Pi installs where SSH
isn't available.

### Files written

| File | Format | Contents |
|------|--------|----------|
| `app.log` | Human-readable | Everything the app prints (rotating, 10 MB × 10) |
| `events.jsonl` | One JSON object per line | File imports, canvas saves/loads/clears, shape transforms / copies / nests / moves / deletes, V-notches, jogs, home, system events |
| `controller.jsonl` | JSON lines | Every serial command sent (`tx`) and received (`rx`), plus job start / pause / resume / stop / complete / error |
| `toolpath.jsonl` | JSON lines | Toolpath-generation summaries (shape count, segments, corners, cut settings, generated gcode line count, notches) |

All four files rotate (default 10 MB × 10 backups).

### Configure via `logging_config.json`

Edit [`logging_config.json`](logging_config.json) at the repo root — commit and
`git pull` on the Pi to push changes. Restart the fabcnc service to apply
(or use the **Restart Service** button in the System tab).

```json
{
  "enabled": true,
  "log_dir": "cnc_ui/logs",
  "console_level": "INFO",
  "file_level": "DEBUG",
  "max_file_size_mb": 10,
  "backup_count": 10,
  "upload": {
    "enabled": true,
    "url": "https://your-webhook.example.com/fabcnc-logs",
    "method": "POST",
    "interval_minutes": 60,
    "device_id": "shop-pi-1",
    "auth_header": "Bearer YOUR_TOKEN",
    "include_gcode": true,
    "include_uploads": false,
    "max_bundle_mb": 50
  }
}
```

### Remote upload destinations

Any HTTPS endpoint that accepts an upload works:

- **POST multipart** (default): standard form upload with two fields, `manifest`
  (JSON metadata) and `file` (the zip). Works with FastAPI / Express / Flask
  receivers, n8n / Zapier / Make "Catch Hook" nodes, or services like
  [webhook.site](https://webhook.site) for ad-hoc debugging.
- **PUT raw** (`"method": "PUT"`): the entire request body is the zip — point at
  a pre-signed S3 / GCS / R2 URL.

The Pi only pushes *new bytes since the last successful upload*, tracked in
`cnc_ui/logs/.uploader_state.json`, so traffic stays small.

### How to fetch logs

Three ways:

1. **Automatic push** — set `upload.enabled=true` and `upload.url=…`. The
   uploader thread runs every `interval_minutes`.
2. **One-click upload from the GUI** — open the System tab and click
   **Upload Logs Now**. Same bundle, sent immediately.
3. **Download from any browser** — `http://<pi>:8080/debug-bundle` returns a
   zip of logs + canvas saves + recent gcode. Useful before remote-upload is
   wired up.

### Diagnostic endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/debug-bundle` | GET | Stream a zip with logs + uploads + canvases + recent gcode |
| `/logs/upload-now` | POST | Trigger an immediate remote upload (`?full=true` for a full re-send) |
| `/logs/status` | GET | JSON: current config (auth header redacted), log files, last upload state |

## License

MIT License
