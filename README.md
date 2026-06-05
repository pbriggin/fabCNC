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
