# cnc/controller.py
"""
CNC controller interface - provides methods for controlling the CNC machine.
This is a stubbed implementation that simulates CNC behavior.
"""

import time
import threading
from typing import Optional
from .state import machine_state


class CNCController:
    """
    Interface for CNC machine control.
    Provides jogging, homing, and job execution capabilities.
    This is a simulated implementation - replace with actual GRBL/CNC interface for production.
    """
    
    def __init__(self):
        self.stop_requested = False
        self.pause_requested = False
        self.job_thread: Optional[threading.Thread] = None
    
    def jog(self, axis: str, distance: float, feed_rate: float) -> None:
        """
        Jog a single axis by the specified distance.
        
        Args:
            axis: Axis to jog ('X', 'Y', 'Z', or 'A')
            distance: Distance to jog in mm (or degrees for A)
            feed_rate: Feed rate in mm/min
        """
        if not machine_state.is_idle():
            return
        
        # Simulate jog motion with a small delay
        duration = abs(distance) / feed_rate * 60  # Convert to seconds
        time.sleep(min(duration, 0.1))  # Cap at 100ms for UI responsiveness
        
        # Update position
        current = machine_state.get_position()
        new_positions = {
            'X': (current[0] + distance, None, None, None),
            'Y': (None, current[1] + distance, None, None),
            'Z': (None, None, current[2] + distance, None),
            'A': (None, None, None, current[3] + distance),
        }
        
        if axis.upper() in new_positions:
            x, y, z, a = new_positions[axis.upper()]
            machine_state.update_position(x=x, y=y, z=z, a=a)
    
    def home_axis(self, axis: str) -> None:
        """
        Home a single axis.
        
        Args:
            axis: Axis to home ('X', 'Y', 'Z', or 'A')
        """
        if not machine_state.is_idle():
            return
        
        # Simulate homing motion
        machine_state.set_status(f"Homing {axis}...", busy=True)
        time.sleep(0.5)  # Simulate homing delay
        
        # Set axis position to zero
        positions = {
            'X': (0.0, None, None, None),
            'Y': (None, 0.0, None, None),
            'Z': (None, None, 0.0, None),
            'A': (None, None, None, 0.0),
        }
        
        if axis.upper() in positions:
            x, y, z, a = positions[axis.upper()]
            machine_state.update_position(x=x, y=y, z=z, a=a)
        
        machine_state.set_status("Idle", busy=False)
    
    def home_all(self) -> None:
        """Home all axes sequentially."""
        if not machine_state.is_idle():
            return
        
        machine_state.set_status("Homing all axes...", busy=True)
        
        # Simulate homing all axes
        time.sleep(1.0)
        
        # Set all positions to zero
        machine_state.update_position(x=0.0, y=0.0, z=0.0, a=0.0)
        machine_state.set_status("Idle", busy=False)
    
    def start_job(self, gcode_lines: list[str]) -> None:
        """
        Start executing a G-code job in a background thread.
        
        Args:
            gcode_lines: List of G-code commands to execute
        """
        if not machine_state.is_idle() or not machine_state.job_loaded:
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
        
        # Wait for job thread to finish
        if self.job_thread and self.job_thread.is_alive():
            self.job_thread.join(timeout=2.0)
        
        machine_state.reset_job()
        machine_state.set_status("Stopped", busy=False)
    
    def _execute_job(self, gcode_lines: list[str]) -> None:
        """
        Internal method to execute G-code job.
        Runs in a background thread.
        
        Args:
            gcode_lines: List of G-code commands (currently simulated)
        """
        machine_state.set_status("Running", busy=True, paused=False)
        
        # Simulate job execution with 100 steps
        total_steps = 100
        
        for step in range(total_steps):
            # Check for stop request
            if self.stop_requested:
                machine_state.reset_job()
                return
            
            # Check for pause request
            while self.pause_requested and not self.stop_requested:
                time.sleep(0.1)
            
            # Simulate processing a G-code line
            progress = (step + 1) / total_steps
            machine_state.update_job_progress(progress)
            
            # Simulate execution time (adjust for realistic behavior)
            time.sleep(0.05)  # 50ms per step = 5 seconds total
        
        # Job complete
        machine_state.set_status("Complete", busy=False)
        machine_state.update_job_progress(1.0)


# Global controller instance
cnc_controller = CNCController()
