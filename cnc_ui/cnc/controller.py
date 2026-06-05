# cnc/controller.py
"""
CNC controller interface - provides methods for controlling the CNC machine.
This implementation communicates with Marlin firmware via serial.
"""

import json
import time
import threading
from pathlib import Path
import serial
import serial.tools.list_ports
from typing import Optional
from .state import machine_state
import logging

# Persisted between runs — written on disconnect, deleted on successful resume
_RESUME_STATE_FILE = Path(__file__).parent.parent / 'resume_state.json'

try:
    # Structured logging helpers — present whenever main.py has been imported.
    from logging_setup import (
        log_serial_tx,
        log_serial_rx,
        log_controller_event,
    )
    import log_uploader as _log_uploader
except Exception:  # pragma: no cover — keep the controller importable standalone
    def log_serial_tx(*a, **kw): pass
    def log_serial_rx(*a, **kw): pass
    def log_controller_event(*a, **kw): pass
    _log_uploader = None

logger = logging.getLogger(__name__)


class CNCController:
    """
    Interface for Marlin-based CNC machine control.
    Provides jogging, homing, and job execution capabilities.
    """
    
    def __init__(self, baudrate: int = 115200):
        self.baudrate = baudrate
        self.serial_port: Optional[serial.Serial] = None
        self.stop_requested = False
        self.pause_requested = False
        self.job_thread: Optional[threading.Thread] = None
        self.read_thread: Optional[threading.Thread] = None
        self.connected = False
        self.read_loop_paused = False
        self.read_lock = threading.Lock()
        
        # Flow control for streaming
        self.ok_count = 0  # Number of 'ok' responses received
        self.ok_lock = threading.Lock()
        self.ok_event = threading.Event()  # Signal when ok received
        self.streaming_mode = False  # True when streaming a job
        # Buffer settings for streaming (Marlin typically has 4-16 command buffer)
        self.buffer_size = 8  # Keep this many commands ahead to prevent pauses
        
        # Try to connect on initialization
        self.homed = False              # True only after a successful home_all()
        self._current_job_gcode: list = []  # Copy of last started job's gcode
        self._auto_connect()
    
    def _auto_connect(self) -> bool:
        """Auto-detect and connect to the first available serial port."""
        try:
            # List all available serial ports
            ports = serial.tools.list_ports.comports()
            
            for port in ports:
                # Try connecting to this port
                try:
                    logger.info(f"Trying to connect to {port.device} ({port.description})")
                    self.serial_port = serial.Serial(
                        port=port.device,
                        baudrate=self.baudrate,
                        timeout=2.0,
                        write_timeout=2.0
                    )
                    
                    # Wait for Marlin to initialize
                    time.sleep(2.0)
                    
                    # Clear any startup messages
                    while self.serial_port.in_waiting:
                        self.serial_port.readline()
                    
                    # Test connection with M115 (get firmware info)
                    self._send_command("M115")
                    response = self._read_response(timeout=3.0)
                    
                    if response and "FIRMWARE_NAME" in response:
                        logger.info(f"Connected to Marlin on {port.device}")
                        self.connected = True
                        
                        # Start background thread to read responses
                        self.read_thread = threading.Thread(target=self._read_loop, daemon=True)
                        self.read_thread.start()
                        
                        # Enable position reporting
                        self._send_command("M114")  # Get current position
                        
                        # Set faster XY acceleration (firmware supports up to 3000)
                        self._send_command("M204 P2000 T3000")  # Print accel 2000, Travel accel 3000
                        
                        return True
                    else:
                        self.serial_port.close()
                        
                except (serial.SerialException, OSError) as e:
                    logger.warning(f"Failed to connect to {port.device}: {e}")
                    if self.serial_port and self.serial_port.is_open:
                        self.serial_port.close()
                    continue
            
            logger.error("No Marlin controller found on any serial port")
            return False
            
        except Exception as e:
            logger.error(f"Error during auto-connect: {e}")
            return False
    
    def _handle_disconnect(self) -> None:
        """Handle an unexpected serial disconnection (e.g. controller power loss or USB drop)."""
        if not self.connected:
            return  # Already handled — avoid duplicate logging
        logger.error("Serial connection lost — controller disconnected (power loss or USB fault)")
        self.connected = False
        self.homed = False
        self.stop_requested = True  # Abort any running job
        self.ok_event.set()   # Unblock any flow-control waits in _execute_job
        machine_state.set_status("Disconnected", busy=False)
        log_controller_event("serial_disconnect")
        self._save_resume_state()
        if _log_uploader:
            _log_uploader.log_system_snapshot(trigger="serial_disconnect")
            threading.Thread(
                target=_log_uploader.upload_now,
                args=(False, "disconnect"),
                daemon=True,
                name="log-upload-on-disconnect",
            ).start()
        self._start_reconnect_loop()

    def _start_reconnect_loop(self) -> None:
        """Background thread: waits for the serial device to re-enumerate then reconnects."""
        def _worker():
            # Close the stale port first
            if self.serial_port:
                try:
                    self.serial_port.close()
                except Exception:
                    pass
                self.serial_port = None

            attempt = 0
            while not self.connected:
                attempt += 1
                machine_state.set_status("Reconnecting...", busy=False)
                logger.info(f"Reconnection attempt {attempt}...")
                if self._auto_connect():
                    logger.info(f"Reconnected on attempt {attempt}")
                    self.stop_requested = False
                    machine_state.set_status("Idle", busy=False)
                    log_controller_event("serial_reconnect", attempt=attempt)
                    return
                time.sleep(3.0)

        threading.Thread(target=_worker, daemon=True, name="serial-reconnect").start()

    # ==================== Resume After Disconnect ====================

    def has_resume_state(self) -> bool:
        """Check whether a resume-state file was saved after a disconnect."""
        return _RESUME_STATE_FILE.exists()

    def clear_resume_state(self) -> None:
        """Delete the resume-state file without resuming (user chose to discard)."""
        try:
            _RESUME_STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    def _get_safe_height(self, commands: list) -> float | None:
        """Find the tool travel (safe) height from the first G0 Z in the preamble."""
        for cmd in commands[:30]:
            upper = cmd.strip().upper()
            if upper.startswith('G0 Z') or upper.startswith('G0Z'):
                try:
                    z_str = upper.split('Z', 1)[1].split('F')[0].strip()
                    return float(z_str)
                except (ValueError, IndexError):
                    pass
        return None

    def _find_safe_resume_index(self, commands: list, last_acked: int) -> int:
        """
        Scan backward from last_acked to find the last G0 Z<safe_height> command.
        That is a 'tool up, between shapes' position — safe to rapid to and resume from.
        """
        safe_z = self._get_safe_height(commands)
        search_to = min(last_acked, len(commands) - 1)
        for i in range(search_to, -1, -1):
            upper = commands[i].strip().upper()
            if upper.startswith('G0 Z') or upper.startswith('G0Z'):
                if safe_z is not None:
                    try:
                        z_str = upper.split('Z', 1)[1].split('F')[0].strip()
                        z_val = float(z_str)
                        if abs(z_val - safe_z) < 0.5:
                            return i
                    except (ValueError, IndexError):
                        pass
                else:
                    return i  # No known safe height — take the first G0 Z found
        logger.warning("No safe G0 Z resume point found; defaulting to command 0")
        return 0

    def _extract_preamble(self, commands: list) -> list:
        """
        Return setup commands from the job header (before the first G0/G1 motion),
        excluding G28 homing lines (machine is already homed at resume time).
        """
        preamble = []
        for cmd in commands:
            upper = cmd.strip().upper()
            if upper.startswith('G0') or upper.startswith('G1'):
                break
            if not upper.startswith('G28'):
                preamble.append(cmd)
        return preamble

    def _save_resume_state(self) -> None:
        """Persist enough information to resume the job after a reconnect + re-home."""
        if not self._current_job_gcode:
            logger.warning("_save_resume_state: skipped — no job gcode in memory (no job was running)")
            return  # No job was running
        with self.ok_lock:
            last_acked = self.ok_count
        if last_acked == 0:
            logger.warning("_save_resume_state: skipped — ok_count is 0 (job had not started sending)")
            return  # Job hadn't meaningfully started
        safe_idx = self._find_safe_resume_index(self._current_job_gcode, last_acked)
        pct = last_acked / len(self._current_job_gcode) * 100
        state = {
            'filename': machine_state.loaded_filename,
            'safe_resume_index': safe_idx,
            'last_acked_index': last_acked,
            'total_commands': len(self._current_job_gcode),
            'gcode': self._current_job_gcode,
        }
        try:
            _RESUME_STATE_FILE.write_text(json.dumps(state))
            logger.info(
                f"Resume state saved: safe_resume_index={safe_idx}, "
                f"last_acked={last_acked}/{len(self._current_job_gcode)} "
                f"({pct:.1f}% complete)"
            )
            log_controller_event(
                "resume_state_saved",
                filename=machine_state.loaded_filename,
                safe_resume_index=safe_idx,
                last_acked_index=last_acked,
                total_commands=len(self._current_job_gcode),
                pct_complete=round(pct, 1),
            )
        except Exception as e:
            logger.error(f"Failed to save resume state: {e}")

    def resume_from_disconnect(self) -> bool:
        """
        Resume a job from the saved disconnect state.
        Machine must be homed (home_all) before calling this.
        """
        if not self.homed:
            logger.warning("resume_from_disconnect: machine not homed")
            return False
        if not _RESUME_STATE_FILE.exists():
            logger.warning("resume_from_disconnect: no saved state file")
            return False
        try:
            state = json.loads(_RESUME_STATE_FILE.read_text())
        except Exception as e:
            logger.error(f"Failed to load resume state: {e}")
            return False

        gcode = state.get('gcode', [])
        safe_idx = state.get('safe_resume_index', 0)
        if not gcode or safe_idx >= len(gcode):
            logger.error("Resume state is invalid or index out of range")
            return False

        preamble = self._extract_preamble(gcode)
        resume_commands = preamble + gcode[safe_idx:]
        logger.info(
            f"Resuming from index {safe_idx}/{len(gcode)} — "
            f"skipping {safe_idx} commands, {len(resume_commands)} remaining"
        )
        log_controller_event(
            "job_resume_disconnect",
            filename=state.get('filename'),
            safe_resume_index=safe_idx,
            last_acked_index=state.get('last_acked_index'),
            total=len(gcode),
        )

        # Clear saved state so it isn't offered again after a clean finish
        try:
            _RESUME_STATE_FILE.unlink()
        except Exception:
            pass

        self.stop_requested = False
        self.pause_requested = False
        self._current_job_gcode = resume_commands
        self.job_thread = threading.Thread(
            target=self._execute_job,
            args=(resume_commands,),
            daemon=True,
            name="job-resume",
        )
        self.job_thread.start()
        return True

    def _send_command(self, command: str) -> bool:
        """Send a G-code command to Marlin."""
        if not self.serial_port or not self.serial_port.is_open:
            logger.error("Serial port not connected")
            return False
        
        try:
            cmd = command.strip() + "\n"
            self.serial_port.write(cmd.encode('utf-8'))
            self.serial_port.flush()  # Ensure data is sent immediately
            if not self.streaming_mode:
                logger.info(f">>> SENT: {command}")
            else:
                logger.debug(f">>> SENT: {command}")
            log_serial_tx(command, streaming=self.streaming_mode)
            return True
        except Exception as e:
            logger.error(f"Error sending command '{command}': {e}")
            log_serial_tx(command, error=str(e), streaming=self.streaming_mode)
            return False
    
    def send_command(self, command: str) -> bool:
        """Public method to send a G-code command to Marlin."""
        return self._send_command(command)
    
    def _read_response(self, timeout: float = 1.0) -> str:
        """Read response from Marlin until 'ok' is received."""
        if not self.serial_port or not self.serial_port.is_open:
            return ""
        
        response_lines = []
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                if self.serial_port.in_waiting:
                    line = self.serial_port.readline().decode('utf-8', errors='ignore').strip()
                    logger.info(f"<<< RECV: {line}")
                    if line:
                        log_serial_rx(line, mode="sync")
                    response_lines.append(line)
                    
                    if line.startswith("ok"):
                        break
                else:
                    time.sleep(0.01)
            except Exception as e:
                logger.error(f"Error reading response: {e}")
                break
        
        return "\n".join(response_lines)
    
    def _read_loop(self):
        """Background thread to continuously read from serial port."""
        while self.connected and self.serial_port and self.serial_port.is_open:
            try:
                # Pause if manual command is being sent
                if self.read_loop_paused:
                    time.sleep(0.01)
                    continue
                
                if self.serial_port.in_waiting:
                    line = self.serial_port.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        logger.debug(f"<<< READ: {line}")
                        log_serial_rx(line, mode="async")
                        
                        # Track 'ok' responses for streaming flow control
                        if line.startswith("ok"):
                            with self.ok_lock:
                                self.ok_count += 1
                            self.ok_event.set()  # Signal that ok was received
                        
                        # Parse position updates (M114 response)
                        if line.startswith("X:"):
                            logger.info(f"PARSING POSITION: {line}")
                            self._parse_position(line)
                        
                        # Log errors
                        if 'error' in line.lower() or 'err:' in line.lower():
                            logger.warning(f"Controller error: {line}")
                        
                time.sleep(0.005)  # 5ms polling rate
            except OSError as e:
                if e.errno == 5:  # EIO — USB/serial device disconnected (e.g. SKR board power loss)
                    logger.error(f"Serial device disconnected (EIO) — likely controller power loss: {e}")
                    self._handle_disconnect()
                    break
                logger.error(f"Error in read loop: {e}")
                time.sleep(0.1)
            except serial.SerialException as e:
                logger.error(f"Serial exception in read loop — connection lost: {e}")
                self._handle_disconnect()
                break
    
    def _parse_position(self, line: str):
        """Parse Marlin position response: X:0.00 Y:0.00 Z:0.00 A:0.00"""
        try:
            # Stop parsing at "Count" - we only want position values, not stepper counts
            if 'Count' in line:
                line = line.split('Count')[0]
            
            parts = line.split()
            pos = {}
            for part in parts:
                if ':' in part:
                    axis, value = part.split(':')
                    if axis in ['X', 'Y', 'Z', 'A', 'E']:
                        pos[axis] = float(value)
                        logger.info(f"  Parsed {axis}: {value}")
            
            # Update machine state - A axis reports in degrees directly
            a_value = pos.get('A', pos.get('E'))
            
            logger.info(f"POSITION UPDATE: X={pos.get('X')}, Y={pos.get('Y')}, Z={pos.get('Z')}, A={a_value}°")
            
            machine_state.update_position(
                x=pos.get('X'),
                y=pos.get('Y'),
                z=pos.get('Z'),
                a=a_value
            )
        except Exception as e:
            logger.error(f"Error parsing position '{line}': {e}", exc_info=True)
    
    def jog(self, axis: str, distance: float, feed_rate: float) -> None:
        """
        Jog a single axis by the specified distance.
        
        Args:
            axis: Axis to jog ('X', 'Y', 'Z', or 'A')
            distance: Distance to jog in mm (or degrees for A)
            feed_rate: Feed rate in mm/min (or deg/min for A)
        """
        if not machine_state.is_idle() or not self.connected:
            logger.warning(f"Cannot jog: idle={machine_state.is_idle()}, connected={self.connected}")
            return
        
        # Set slower acceleration for jogging (gentler motion)
        self._send_command("M204 P500 T500")  # Jog accel 500 mm/s²
        
        # For A axis, use degrees directly (Marlin handles rotary axes)
        if axis.upper() == 'A':
            # Invert A axis direction to match physical motor wiring
            distance = -distance
            logger.info(f"JOG A AXIS: {distance} deg (inverted), feed={feed_rate} deg/min")
            
            self._send_command("G91")  # Relative mode
            self._send_command(f"G1 A{distance} F{feed_rate}")  # Use G1 for controlled acceleration
            self._send_command("G90")  # Back to absolute mode
        else:
            # Linear axes (X, Y, Z)
            logger.info(f"JOG {axis.upper()} AXIS: distance={distance} mm, feed={feed_rate} mm/min")
            
            self._send_command("G91")  # Relative mode
            self._send_command(f"G1 {axis}{distance} F{feed_rate}")  # Use G1 for controlled acceleration
            self._send_command("G90")  # Back to absolute mode
        
        self._send_command("M400")  # Wait for move to finish
        self._send_command("M204 P2000 T3000")  # Restore fast acceleration
        self._send_command("M114")  # Request position update
    
    def jog_xy(self, x_distance: float, y_distance: float, feed_rate: float) -> None:
        """
        Jog X and Y axes simultaneously (diagonal move).
        
        Args:
            x_distance: Distance to jog X axis in mm
            y_distance: Distance to jog Y axis in mm
            feed_rate: Feed rate in mm/min
        """
        if not machine_state.is_idle() or not self.connected:
            logger.warning(f"Cannot jog: idle={machine_state.is_idle()}, connected={self.connected}")
            return
        
        logger.info(f"JOG XY DIAGONAL: X={x_distance} mm, Y={y_distance} mm, feed={feed_rate} mm/min")
        
        # Set slower acceleration for jogging (gentler motion)
        self._send_command("M204 P500 T500")  # Jog accel 500 mm/s²
        
        self._send_command("G91")  # Relative mode
        self._send_command(f"G1 X{x_distance} Y{y_distance} F{feed_rate}")  # Use G1 for controlled acceleration
        self._send_command("G90")  # Back to absolute mode
        self._send_command("M400")  # Wait for move to finish
        self._send_command("M204 P2000 T3000")  # Restore fast acceleration
        self._send_command("M114")  # Request position update
    
    def home_axis(self, axis: str) -> None:
        """
        Home a single axis.
        
        Args:
            axis: Axis to home ('X', 'Y', 'Z', or 'A')
        """
        if not machine_state.is_idle() or not self.connected:
            return
        
        machine_state.set_status(f"Homing {axis}...", busy=True)
        
        # Enable all steppers
        self._send_command("M17")
        
        # Reset Z speed/accel to safe firmware defaults for homing
        self._send_command("M203 Z5")  # Max Z speed 5 mm/s
        self._send_command("M201 Z15")  # Max Z accel 15 mm/s²
        self._send_command("M204 P1000 T1000")  # Safe acceleration
        
        # Marlin homing command
        self._send_command(f"G28 {axis.upper()}")
        
        # Wait for homing to complete
        time.sleep(0.5)
        self._send_command("M114")  # Request position update
        
        # Restore faster XY acceleration (but keep Z safe)
        self._send_command("M204 P2000 T3000")
        
        # Disable steppers after homing
        self._send_command("M18")
        
        machine_state.set_status("Idle", busy=False)
    
    def home_all(self) -> None:
        """Home all axes sequentially."""
        if not machine_state.is_idle() or not self.connected:
            return
        
        machine_state.set_status("Homing all axes...", busy=True)
        
        # Enable all steppers
        self._send_command("M17")
        
        # Reset Z speed/accel to safe firmware defaults for homing
        self._send_command("M203 Z5")  # Max Z speed 5 mm/s
        self._send_command("M201 Z15")  # Max Z accel 15 mm/s²
        self._send_command("M204 P1000 T1000")  # Safe acceleration
        
        # Marlin home all command
        self._send_command("G28")  # Home X, Y, Z
        
        # Wait for homing
        time.sleep(2.0)
        
        # Zero the A axis (E in Marlin)
        self._send_command("G92 E0")
        self._send_command("M114")  # Request position update
        
        # Restore faster XY acceleration (but keep Z safe)
        self._send_command("M204 P2000 T3000")
        
        # Disable steppers after homing
        self._send_command("M18")

        machine_state.set_status("Idle", busy=False)
        self.homed = True

    # ==================== Job Execution ====================
    
    def run_utility_sequence(self, gcode_lines: list[str]) -> None:
        """
        Run a utility G-code sequence (e.g. move to center) without requiring a loaded job.
        
        Args:
            gcode_lines: List of G-code commands to execute
        """
        if not machine_state.is_idle() or not self.connected:
            return

        self.stop_requested = False
        self.pause_requested = False
        self.job_thread = threading.Thread(target=self._execute_job, args=(gcode_lines,), daemon=True)
        self.job_thread.start()

    def start_job(self, gcode_lines: list[str]) -> None:
        """
        Start executing a G-code job via serial streaming.
        
        Args:
            gcode_lines: List of G-code commands to execute
        """
        if not machine_state.is_idle() or not machine_state.job_loaded or not self.connected:
            log_controller_event(
                "job_start_rejected",
                idle=machine_state.is_idle(),
                job_loaded=machine_state.job_loaded,
                connected=self.connected,
            )
            return
        
        self.stop_requested = False
        self.pause_requested = False
        self._current_job_gcode = list(gcode_lines)
        log_controller_event(
            "job_start",
            command_count=len(gcode_lines),
            filename=machine_state.loaded_filename,
        )
        if _log_uploader:
            _log_uploader.notify_job_run()

        # Stream via serial
        self.job_thread = threading.Thread(target=self._execute_job, args=(gcode_lines,), daemon=True)
        self.job_thread.start()
    
    def pause_job(self) -> None:
        """Pause the currently running job."""
        if machine_state.is_running():
            self.pause_requested = True
            machine_state.set_status("Paused", busy=True, paused=True)
            log_controller_event("job_pause")
    
    def resume_job(self) -> None:
        """Resume a paused job."""
        if machine_state.paused:
            self.pause_requested = False
            machine_state.set_status("Running", busy=True, paused=False)
            log_controller_event("job_resume")
    
    def stop_job(self) -> None:
        """Stop the currently running job immediately."""
        self.stop_requested = True
        self.pause_requested = False
        log_controller_event("job_stop_requested")
        
        # Send emergency stop
        if self.connected:
            self._send_command("M410")  # Marlin quick stop
        
        # Wait for job thread to finish
        if self.job_thread and self.job_thread.is_alive():
            self.job_thread.join(timeout=2.0)
        
        machine_state.reset_job()
        machine_state.set_status("Stopped", busy=False)
        log_controller_event("job_stopped")
    
    def send_command_with_response(self, command: str, timeout: float = 5.0) -> str:
        """Send a G-code command and return the response."""
        if not self.connected:
            return "ERROR: Not connected to controller"
        
        with self.read_lock:
            # Pause the background read loop
            self.read_loop_paused = True
            time.sleep(0.05)  # Give read loop time to pause
            
            try:
                self._send_command(command)
                response = self._read_response(timeout=timeout)
                return response if response else "No response"
            finally:
                # Resume the background read loop
                self.read_loop_paused = False
    
    def _execute_job(self, gcode_lines: list[str]) -> None:
        """
        Internal method to execute G-code job.
        Runs in a background thread.
        
        Uses buffered streaming with flow control - keeps the motion planner
        buffer full by sending commands ahead, waiting for 'ok' only when
        the buffer is full.
        
        Args:
            gcode_lines: List of G-code commands to execute
        """
        if not self.connected:
            logger.error("Cannot execute job: not connected to controller")
            return
        
        machine_state.set_status("Running", busy=True, paused=False)
        self.streaming_mode = True
        
        # Reset ok counter
        with self.ok_lock:
            self.ok_count = 0
        
        # Filter out empty lines and comments first
        commands = []
        for line in gcode_lines:
            line = line.strip()
            # Remove inline comments
            if ';' in line:
                line = line.split(';')[0].strip()
            if line and not line.startswith('#'):
                commands.append(line)

        # Update _current_job_gcode to the filtered list so that ok_count
        # (which tracks acked filtered commands) is a valid index into it.
        self._current_job_gcode = commands
        
        total_commands = len(commands)
        # Flow control: keep some commands in flight but don't overflow Marlin's buffer
        max_in_flight = 8  # Conservative - Marlin has ~16 slot buffer
        throttle_threshold = 4  # Start waiting when this many in flight
        logger.info(f"Starting job with {total_commands} commands")
        
        sent_count = 0  # Commands sent
        last_progress_log = 0
        
        # Debug timing
        job_start_time = time.time()
        total_wait_time = 0
        wait_count = 0
        max_wait_time = 0
        
        try:
            for i, cmd in enumerate(commands):
                # Check for stop request
                if self.stop_requested:
                    logger.info("Job stopped by user")
                    break
                
                # Check for pause request
                while self.pause_requested and not self.stop_requested:
                    time.sleep(0.1)
                
                if self.stop_requested:
                    break
                
                # Send the command immediately
                self._send_command(cmd)
                sent_count += 1
                
                # Special handling only for blocking commands
                if cmd.upper().startswith('G28'):  # Homing
                    # Wait for ALL pending commands to complete
                    while not self.stop_requested:
                        with self.ok_lock:
                            in_flight = sent_count - self.ok_count
                        if in_flight <= 0:
                            break
                        self.ok_event.clear()
                        self.ok_event.wait(timeout=1.0)
                    time.sleep(0.1)
                    
                elif cmd.upper().startswith('G4'):  # Dwell
                    start = time.time()
                    while not self.stop_requested and (time.time() - start) < 30.0:
                        with self.ok_lock:
                            in_flight = sent_count - self.ok_count
                        if in_flight <= 0:
                            break
                        self.ok_event.clear()
                        self.ok_event.wait(timeout=1.0)
                else:
                    # Flow control - wait if too many commands in flight
                    with self.ok_lock:
                        in_flight = sent_count - self.ok_count
                    
                    if in_flight >= throttle_threshold:
                        wait_start = time.time()
                        while not self.stop_requested and in_flight >= throttle_threshold:
                            self.ok_event.clear()
                            self.ok_event.wait(timeout=0.05)
                            with self.ok_lock:
                                in_flight = sent_count - self.ok_count
                            
                            if time.time() - wait_start > 30.0:
                                logger.warning(f"Timeout waiting for ok (in_flight={in_flight})")
                                with self.ok_lock:
                                    self.ok_count = sent_count - throttle_threshold + 1
                                break
                        
                        this_wait = time.time() - wait_start
                        total_wait_time += this_wait
                        wait_count += 1
                        if this_wait > max_wait_time:
                            max_wait_time = this_wait
                
                # Update progress
                with self.ok_lock:
                    acked = self.ok_count
                progress = min(1.0, acked / total_commands) if total_commands > 0 else 1.0
                machine_state.update_job_progress(progress)
                
                # Log progress every 10%
                progress_pct = int(progress * 10)
                if progress_pct > last_progress_log:
                    last_progress_log = progress_pct
                    with self.ok_lock:
                        in_flight = sent_count - self.ok_count
                    elapsed = time.time() - job_start_time
                    avg_wait = (total_wait_time / wait_count * 1000) if wait_count > 0 else 0
                    logger.info(f"Job progress: {progress*100:.0f}% (sent={sent_count}, acked={acked}, in_flight={in_flight}, elapsed={elapsed:.1f}s)")
            
            # Wait for all remaining commands to be acknowledged
            if not self.stop_requested:
                logger.info("Waiting for remaining commands to complete...")
                wait_start = time.time()
                while True:
                    with self.ok_lock:
                        in_flight = sent_count - self.ok_count
                    if in_flight <= 0:
                        break
                    # Shorter timeout - 10 seconds should be enough for motion to complete
                    if time.time() - wait_start > 10.0:
                        logger.warning(f"Timeout waiting for final commands ({in_flight} remaining) - continuing anyway")
                        break
                    self.ok_event.clear()
                    self.ok_event.wait(timeout=0.2)
            
            # Job complete
            if not self.stop_requested:
                logger.info("Job complete!")
                machine_state.set_status("Complete", busy=False)
                machine_state.update_job_progress(1.0)
                log_controller_event(
                    "job_complete",
                    sent=sent_count,
                    acked=self.ok_count,
                    elapsed_s=round(time.time() - job_start_time, 2),
                    total_wait_s=round(total_wait_time, 2),
                    max_wait_s=round(max_wait_time, 2),
                )
                if _log_uploader:
                    threading.Thread(
                        target=_log_uploader.upload_now,
                        args=(False, "job_complete"),
                        daemon=True,
                        name="log-upload-on-complete",
                    ).start()
            else:
                machine_state.reset_job()
                if self.connected:
                    machine_state.set_status("Stopped", busy=False)
                else:
                    # Disconnect handler already set status to "Disconnected" — restore it
                    machine_state.set_status("Disconnected", busy=False)
                log_controller_event(
                    "job_aborted",
                    sent=sent_count,
                    acked=self.ok_count,
                    elapsed_s=round(time.time() - job_start_time, 2),
                )
                if _log_uploader:
                    threading.Thread(
                        target=_log_uploader.upload_now,
                        args=(False, "job_abort"),
                        daemon=True,
                        name="log-upload-on-abort",
                    ).start()
                
        except Exception as e:
            logger.error(f"Job execution error: {e}", exc_info=True)
            machine_state.set_status("Error", busy=False)
            log_controller_event("job_error", error=str(e))
        finally:
            self.streaming_mode = False


# Global controller instance
cnc_controller = CNCController()
