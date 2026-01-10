# cnc/state.py
"""
Shared machine and job state for the CNC controller.
Thread-safe state object that tracks machine position, status, and job progress.
"""

import threading
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MachineState:
    """
    Centralized state for the CNC machine.
    
    All position values are in mm for linear axes (X, Y, Z) and degrees for rotary axis (A).
    State is updated by the controller and read by the UI.
    """
    
    # Machine position (mm for XYZ, degrees for A)
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    a: float = 0.0
    
    # Machine status flags
    busy: bool = False
    paused: bool = False
    
    # Job status
    job_loaded: bool = False
    job_progress: float = 0.0  # 0.0 to 1.0
    status_text: str = "Idle"
    toolpath_generated: bool = False  # Whether toolpath has been generated and is being previewed
    
    # Job file info
    loaded_filename: Optional[str] = None
    
    # Thread lock for safe concurrent access
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    
    def update_position(self, x: Optional[float] = None, y: Optional[float] = None, 
                       z: Optional[float] = None, a: Optional[float] = None) -> None:
        """Update machine position (thread-safe)."""
        with self._lock:
            if x is not None:
                self.x = x
            if y is not None:
                self.y = y
            if z is not None:
                self.z = z
            if a is not None:
                self.a = a
    
    def set_status(self, status: str, busy: bool = False, paused: bool = False) -> None:
        """Update machine status (thread-safe)."""
        with self._lock:
            self.status_text = status
            self.busy = busy
            self.paused = paused
    
    def set_job_loaded(self, loaded: bool, filename: Optional[str] = None) -> None:
        """Update job loaded status (thread-safe)."""
        with self._lock:
            self.job_loaded = loaded
            self.loaded_filename = filename
            if loaded:
                self.status_text = f"Loaded: {filename}"
            else:
                self.status_text = "Idle"
                self.job_progress = 0.0
    
    def update_job_progress(self, progress: float) -> None:
        """Update job progress (thread-safe)."""
        with self._lock:
            self.job_progress = max(0.0, min(1.0, progress))
    
    def get_position(self) -> tuple[float, float, float, float]:
        """Get current position (thread-safe)."""
        with self._lock:
            return (self.x, self.y, self.z, self.a)
    
    def is_idle(self) -> bool:
        """Check if machine is idle and ready for new commands (thread-safe)."""
        with self._lock:
            return not self.busy and not self.paused
    
    def is_running(self) -> bool:
        """Check if machine is currently running a job (thread-safe)."""
        with self._lock:
            return self.busy and not self.paused
    
    def reset_job(self) -> None:
        """Reset job state (thread-safe)."""
        with self._lock:
            self.busy = False
            self.paused = False
            self.job_progress = 0.0
            if self.job_loaded:
                self.status_text = f"Loaded: {self.loaded_filename}"
            else:
                self.status_text = "Idle"
    
    def set_toolpath_generated(self, generated: bool) -> None:
        """Set whether toolpath has been generated (thread-safe)."""
        with self._lock:
            self.toolpath_generated = generated


# Global machine state instance
machine_state = MachineState()
