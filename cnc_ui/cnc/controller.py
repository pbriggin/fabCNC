# cnc/controller.py
"""
CNC controller interface - provides methods for controlling the CNC machine.
This implementation communicates with Marlin firmware via serial.
"""

import time
import threading
import serial
import serial.tools.list_ports
from typing import Optional
from .state import machine_state
import logging

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
        self.buffer_size = 4  # Keep this many commands ahead
        
        # Try to connect on initialization
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
                        
                        # Set acceleration and jerk for smoother motion
                        self._send_command("M204 P1000 T1000")  # Set acceleration (mm/s²)
                        self._send_command("M205 X10 Y10 Z10")  # Set jerk (mm/s)
                        
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
            return True
        except Exception as e:
            logger.error(f"Error sending command '{command}': {e}")
            return False
    
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
            except Exception as e:
                logger.error(f"Error in read loop: {e}")
                time.sleep(0.1)
    
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
        
        self._send_command("G91")  # Relative mode
        self._send_command(f"G1 X{x_distance} Y{y_distance} F{feed_rate}")  # Use G1 for controlled acceleration
        self._send_command("G90")  # Back to absolute mode
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
        
        # Marlin homing command
        self._send_command(f"G28 {axis.upper()}")
        
        # Wait for homing to complete
        time.sleep(0.5)
        self._send_command("M114")  # Request position update
        
        machine_state.set_status("Idle", busy=False)
    
    def home_all(self) -> None:
        """Home all axes sequentially."""
        if not machine_state.is_idle() or not self.connected:
            return
        
        machine_state.set_status("Homing all axes...", busy=True)
        
        # Marlin home all command
        self._send_command("G28")  # Home X, Y, Z
        
        # Wait for homing
        time.sleep(2.0)
        
        # Zero the A axis (E in Marlin)
        self._send_command("G92 E0")
        self._send_command("M114")  # Request position update
        
        machine_state.set_status("Idle", busy=False)
    
    # ==================== SD Card Operations ====================
    
    def sd_init(self) -> str:
        """Initialize SD card. Returns response."""
        return self.send_command_with_response("M21", timeout=5.0)
    
    def sd_list_files(self) -> str:
        """List files on SD card. Returns file listing."""
        return self.send_command_with_response("M20", timeout=10.0)
    
    def sd_upload_file(self, filename: str, gcode_content: str) -> bool:
        """
        Upload a gcode file to the SD card.
        
        Args:
            filename: Name for the file on SD card (8.3 format recommended)
            gcode_content: The gcode content to upload
            
        Returns:
            True if upload successful
        """
        if not self.connected:
            logger.error("Cannot upload to SD: not connected")
            return False
        
        try:
            # Initialize SD card
            self._send_command("M21")
            time.sleep(0.5)
            
            # Start file write
            self._send_command(f"M28 {filename}")
            time.sleep(0.2)
            
            # Send gcode lines
            lines = gcode_content.strip().split('\n')
            for line in lines:
                line = line.strip()
                if line and not line.startswith(';'):
                    self._send_command(line)
                    time.sleep(0.01)  # Small delay to not overflow buffer
            
            # End file write
            self._send_command("M29")
            time.sleep(0.5)
            
            logger.info(f"Uploaded {filename} to SD card ({len(lines)} lines)")
            return True
            
        except Exception as e:
            logger.error(f"SD upload failed: {e}")
            return False
    
    def sd_start_file(self, filename: str) -> bool:
        """
        Start running a gcode file from SD card.
        
        Args:
            filename: Name of file on SD card
            
        Returns:
            True if started successfully
        """
        if not self.connected:
            return False
        
        try:
            # Initialize SD
            self._send_command("M21")
            time.sleep(0.3)
            
            # Select file
            self._send_command(f"M23 {filename}")
            time.sleep(0.3)
            
            # Start print
            self._send_command("M24")
            
            machine_state.set_status("Running (SD)", busy=True)
            logger.info(f"Started SD print: {filename}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start SD file: {e}")
            return False
    
    def sd_pause(self) -> None:
        """Pause SD card print."""
        self._send_command("M25")
        machine_state.set_status("Paused (SD)", busy=True, paused=True)
    
    def sd_resume(self) -> None:
        """Resume SD card print."""
        self._send_command("M24")
        machine_state.set_status("Running (SD)", busy=True, paused=False)
    
    def sd_stop(self) -> None:
        """Stop SD card print and delete temp file."""
        self._send_command("M524")  # Abort SD print (Marlin 2.x)
        machine_state.set_status("Stopped", busy=False)
    
    def sd_get_progress(self) -> Optional[float]:
        """Get SD print progress (0.0 to 1.0)."""
        response = self.send_command_with_response("M27", timeout=2.0)
        # Parse response like "SD printing byte 1234/5678"
        if "byte" in response.lower():
            try:
                parts = response.split("byte")[1].strip().split("/")
                current = int(parts[0])
                total = int(parts[1])
                if total > 0:
                    return current / total
            except:
                pass
        return None
    
    # ==================== Job Execution ====================
    
    def start_job(self, gcode_lines: list[str], use_sd: bool = False) -> None:
        """
        Start executing a G-code job.
        
        Args:
            gcode_lines: List of G-code commands to execute
            use_sd: If True, upload to SD card and run from there (more reliable)
        """
        if not machine_state.is_idle() or not machine_state.job_loaded or not self.connected:
            return
        
        self.stop_requested = False
        self.pause_requested = False
        
        if use_sd:
            # Upload to SD and run from there
            gcode_content = '\n'.join(gcode_lines)
            if self.sd_upload_file("job.gco", gcode_content):
                self.sd_start_file("job.gco")
            else:
                logger.error("Failed to upload to SD card, falling back to streaming")
                self.job_thread = threading.Thread(target=self._execute_job, args=(gcode_lines,), daemon=True)
                self.job_thread.start()
        else:
            # Stream via serial (original method)
            self.job_thread = threading.Thread(target=self._execute_job, args=(gcode_lines,), daemon=True)
            self.job_thread.start()
    
    def pause_job(self) -> None:
        """Pause the currently running job."""
        if machine_state.is_running():
            self.pause_requested = True
            machine_state.set_status("Paused", busy=True, paused=True)
    
    def resume_job(self) -> None:
        """Resume a paused job."""
        if machine_state.paused:
            self.pause_requested = False
            machine_state.set_status("Running", busy=True, paused=False)
    
    def stop_job(self) -> None:
        """Stop the currently running job immediately."""
        self.stop_requested = True
        self.pause_requested = False
        
        # Send emergency stop
        if self.connected:
            self._send_command("M410")  # Marlin quick stop
        
        # Wait for job thread to finish
        if self.job_thread and self.job_thread.is_alive():
            self.job_thread.join(timeout=2.0)
        
        machine_state.reset_job()
        machine_state.set_status("Stopped", busy=False)
    
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
        
        total_commands = len(commands)
        logger.info(f"Starting job with {total_commands} commands (buffer size: {self.buffer_size})")
        
        sent_count = 0  # Commands sent
        
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
                
                # Send the command
                self._send_command(cmd)
                sent_count += 1
                
                # Calculate how many commands are "in flight" (sent but not acknowledged)
                with self.ok_lock:
                    in_flight = sent_count - self.ok_count
                
                # If buffer is full, wait for an 'ok' before sending more
                # Use longer wait for special commands
                if cmd.upper().startswith('G28'):  # Homing
                    max_wait = 60.0
                    # Wait for ALL pending commands to complete before and after homing
                    while not self.stop_requested:
                        with self.ok_lock:
                            in_flight = sent_count - self.ok_count
                        if in_flight <= 0:
                            break
                        self.ok_event.clear()
                        self.ok_event.wait(timeout=1.0)
                    time.sleep(0.1)  # Extra delay after homing
                    
                elif cmd.upper().startswith('G4'):  # Dwell
                    # Wait for dwell to complete
                    max_wait = 30.0
                    start = time.time()
                    while not self.stop_requested and (time.time() - start) < max_wait:
                        with self.ok_lock:
                            in_flight = sent_count - self.ok_count
                        if in_flight <= 0:
                            break
                        self.ok_event.clear()
                        self.ok_event.wait(timeout=1.0)
                        
                elif in_flight >= self.buffer_size:
                    # Buffer full - wait for at least one ok
                    wait_start = time.time()
                    while not self.stop_requested:
                        self.ok_event.clear()
                        # Wait for ok with short timeout
                        if self.ok_event.wait(timeout=0.5):
                            with self.ok_lock:
                                in_flight = sent_count - self.ok_count
                            if in_flight < self.buffer_size:
                                break
                        
                        # Timeout check - if no ok in 10 seconds, something is wrong
                        if time.time() - wait_start > 10.0:
                            logger.warning(f"Timeout waiting for ok (in_flight={in_flight})")
                            # Reset and continue - may cause issues but better than hanging
                            with self.ok_lock:
                                self.ok_count = sent_count - 1
                            break
                
                # Update progress based on acknowledged commands
                with self.ok_lock:
                    acked = self.ok_count
                progress = min(1.0, acked / total_commands) if total_commands > 0 else 1.0
                machine_state.update_job_progress(progress)
                
                # Log progress every 10%
                if (i + 1) % max(1, total_commands // 10) == 0:
                    with self.ok_lock:
                        in_flight = sent_count - self.ok_count
                    logger.info(f"Job progress: {progress*100:.0f}% (sent={sent_count}, acked={acked}, in_flight={in_flight})")
            
            # Wait for all remaining commands to be acknowledged
            if not self.stop_requested:
                logger.info("Waiting for remaining commands to complete...")
                wait_start = time.time()
                while True:
                    with self.ok_lock:
                        in_flight = sent_count - self.ok_count
                    if in_flight <= 0:
                        break
                    if time.time() - wait_start > 30.0:
                        logger.warning(f"Timeout waiting for final commands ({in_flight} remaining)")
                        break
                    self.ok_event.clear()
                    self.ok_event.wait(timeout=1.0)
            
            # Job complete
            if not self.stop_requested:
                logger.info("Job complete!")
                machine_state.set_status("Complete", busy=False)
                machine_state.update_job_progress(1.0)
            else:
                machine_state.reset_job()
                machine_state.set_status("Stopped", busy=False)
                
        except Exception as e:
            logger.error(f"Job execution error: {e}", exc_info=True)
            machine_state.set_status("Error", busy=False)
        finally:
            self.streaming_mode = False


# Global controller instance
cnc_controller = CNCController()
