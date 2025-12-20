# cnc/controller.py
"""
CNC controller interface - Marlin firmware communication via serial.
"""

import time
import threading
import serial
import logging
from typing import Optional
from .state import machine_state

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class CNCController:
    """
    Interface for Marlin-based CNC machine control via serial.
    Provides jogging, homing, and job execution capabilities.
    """
    
    def __init__(self, port: str = '/dev/ttyACM0', baudrate: int = 115200):
        """
        Initialize the Marlin controller.
        
        Args:
            port: Serial port path (e.g., '/dev/ttyACM0')
            baudrate: Baud rate for serial communication (default: 115200 for Marlin)
        """
        self.port = port
        self.baudrate = baudrate
        self.serial: Optional[serial.Serial] = None
        self.stop_requested = False
        self.pause_requested = False
        self.job_thread: Optional[threading.Thread] = None
        self.position_thread: Optional[threading.Thread] = None
        self.connected = False
        
        # Try to connect on initialization
        self.connect()
        
        # Start position polling thread
        if self.connected:
            self.position_thread = threading.Thread(target=self._poll_position, daemon=True)
            self.position_thread.start()
    
    def connect(self) -> bool:
        """
        Connect to the Marlin controller via serial.
        
        Returns:
            True if connected successfully, False otherwise
        """
        try:
            self.serial = serial.Serial(self.port, self.baudrate, timeout=1)
            time.sleep(2)  # Wait for Marlin to initialize
            
            # Clear any startup messages
            while self.serial.in_waiting:
                self.serial.readline()
            
            self.connected = True
            logger.info(f"Connected to Marlin on {self.port} at {self.baudrate} baud")
            machine_state.set_status("Idle", busy=False)
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Marlin: {e}")
            self.connected = False
            machine_state.set_status("Disconnected", busy=False)
            return False
    
    def disconnect(self) -> None:
        """Disconnect from the Marlin controller."""
        if self.serial and self.serial.is_open:
            self.serial.close()
        self.connected = False
        logger.info("Disconnected from Marlin")
    
    def send_command(self, command: str, wait_for_ok: bool = True) -> Optional[str]:
        """
        Send a G-code command to Marlin and optionally wait for response.
        
        Args:
            command: G-code command to send
            wait_for_ok: Whether to wait for "ok" response
            
        Returns:
            Response from Marlin, or None if no response expected
        """
        if not self.connected or not self.serial:
            logger.warning(f"Cannot send command '{command}': Not connected")
            return None
        
        try:
            # Send command
            self.serial.write(f"{command}\n".encode())
            logger.debug(f"Sent: {command}")
            
            if wait_for_ok:
                # Wait for "ok" response
                timeout = time.time() + 5  # 5 second timeout
                response_lines = []
                
                while time.time() < timeout:
                    if self.serial.in_waiting:
                        line = self.serial.readline().decode().strip()
                        logger.debug(f"Received: {line}")
                        response_lines.append(line)
                        
                        if 'ok' in line.lower():
                            return '\n'.join(response_lines)
                        elif 'error' in line.lower():
                            logger.error(f"Marlin error: {line}")
                            return '\n'.join(response_lines)
                    time.sleep(0.01)
                
                logger.warning(f"Timeout waiting for response to '{command}'")
                return None
            
            return None
            
        except Exception as e:
            logger.error(f"Error sending command '{command}': {e}")
            return None
    
    def jog(self, axis: str, distance: float, feed_rate: float) -> None:
        """
        Jog a single axis by the specified distance.
        
        Args:
            axis: Axis to jog ('X', 'Y', 'Z', or 'A')
            distance: Distance to jog in mm (or degrees for A)
            feed_rate: Feed rate in mm/min
        """
        if not machine_state.is_idle() or not self.connected:
            return
        
        # Use G91 (relative positioning) for jogging
        self.send_command("G91")  # Relative mode
        self.send_command(f"G0 {axis.upper()}{distance} F{feed_rate}")
        self.send_command("G90")  # Back to absolute mode
    
    def home_axis(self, axis: str) -> None:
        """
        Home a single axis using Marlin's G28 command.
        
        Args:
            axis: Axis to home ('X', 'Y', 'Z', or 'A')
        """
        if not machine_state.is_idle() or not self.connected:
            return
        
        machine_state.set_status(f"Homing {axis}...", busy=True)
        
        # Marlin: G28 X homes X axis, G28 Y homes Y, etc.
        self.send_command(f"G28 {axis.upper()}")
        
        machine_state.set_status("Idle", busy=False)
    
    def home_all(self) -> None:
        """Home all axes using Marlin's G28 command."""
        if not machine_state.is_idle() or not self.connected:
            return
        
        machine_state.set_status("Homing all axes...", busy=True)
        
        # Marlin: G28 without parameters homes all axes
        self.send_command("G28")
        
        machine_state.set_status("Idle", busy=False)
    
    def _poll_position(self) -> None:
        """
        Poll position from Marlin periodically (runs in background thread).
        Uses M114 to get current position.
        """
        while self.connected:
            try:
                if self.serial and self.serial.is_open:
                    # Request position report
                    self.serial.write(b"M114\n")
                    
                    # Read response
                    timeout = time.time() + 0.5
                    while time.time() < timeout:
                        if self.serial.in_waiting:
                            line = self.serial.readline().decode().strip()
                            
                            # Parse M114 response: "X:0.00 Y:0.00 Z:0.00 E:0.00"
                            if line.startswith('X:'):
                                parts = line.split()
                                try:
                                    x = float(parts[0].split(':')[1])
                                    y = float(parts[1].split(':')[1])
                                    z = float(parts[2].split(':')[1])
                                    # A axis might be reported as E (extruder)
                                    a = 0.0
                                    if len(parts) > 3:
                                        a = float(parts[3].split(':')[1])
                                    
                                    machine_state.update_position(x=x, y=y, z=z, a=a)
                                except (IndexError, ValueError) as e:
                                    logger.debug(f"Could not parse position: {line}")
                                break
                
                time.sleep(0.1)  # Poll every 100ms
                
            except Exception as e:
                logger.error(f"Error polling position: {e}")
                time.sleep(1)  # Back off on error
    
    def start_job(self, gcode_lines: list[str]) -> None:
        """
        Start executing a G-code job in a background thread.
        
        Args:
            gcode_lines: List of G-code commands to execute
        """
        if not machine_state.is_idle() or not machine_state.job_loaded or not self.connected:
            return
        
        self.stop_requested = False
        self.pause_requested = False
        
        # Launch job execution in background thread
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
        
        # Send emergency stop to Marlin
        if self.connected:
            self.send_command("M112", wait_for_ok=False)  # Emergency stop
            time.sleep(0.1)
            self.send_command("M999", wait_for_ok=False)  # Reset from emergency stop
        
        # Wait for job thread to finish
        if self.job_thread and self.job_thread.is_alive():
            self.job_thread.join(timeout=2.0)
        
        machine_state.reset_job()
        machine_state.set_status("Stopped", busy=False)
    
    def _execute_job(self, gcode_lines: list[str]) -> None:
        """
        Internal method to execute G-code job by sending to Marlin.
        Runs in a background thread.
        
        Args:
            gcode_lines: List of G-code commands to execute
        """
        machine_state.set_status("Running", busy=True, paused=False)
        
        total_lines = len(gcode_lines)
        
        for i, line in enumerate(gcode_lines):
            # Check for stop request
            if self.stop_requested:
                machine_state.reset_job()
                return
            
            # Check for pause request
            while self.pause_requested and not self.stop_requested:
                time.sleep(0.1)
            
            # Skip empty lines and comments
            line = line.strip()
            if not line or line.startswith(';') or line.startswith('('):
                continue
            
            # Send G-code line to Marlin
            self.send_command(line)
            
            # Update progress
            progress = (i + 1) / total_lines
            machine_state.update_job_progress(progress)
        
        # Job complete
        machine_state.set_status("Complete", busy=False)
        machine_state.update_job_progress(1.0)


# Global controller instance
cnc_controller = CNCController()
