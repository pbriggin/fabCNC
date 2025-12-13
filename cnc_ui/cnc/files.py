# cnc/files.py
"""
File handling logic for DXF files and G-code generation.
Manages file uploads and storage.
"""

import os
import shutil
from pathlib import Path
from typing import Optional


class FileManager:
    """
    Manages DXF file uploads and storage.
    Provides methods for saving uploaded files and loading G-code.
    """
    
    def __init__(self, upload_dir: str = "uploads"):
        """
        Initialize file manager.
        
        Args:
            upload_dir: Directory to store uploaded files (relative to cnc_ui/)
        """
        # Get the cnc_ui directory (parent of the cnc package)
        self.base_dir = Path(__file__).parent.parent
        self.upload_dir = self.base_dir / upload_dir
        
        # Ensure upload directory exists
        self.upload_dir.mkdir(parents=True, exist_ok=True)
    
    def save_uploaded_file(self, source_path: str, filename: str) -> str:
        """
        Save an uploaded file to the uploads directory.
        
        Args:
            source_path: Path to the temporary uploaded file
            filename: Original filename
            
        Returns:
            Path to the saved file
        """
        # Sanitize filename
        safe_filename = self._sanitize_filename(filename)
        destination = self.upload_dir / safe_filename
        
        # Copy file to uploads directory
        shutil.copy2(source_path, destination)
        
        return str(destination)
    
    def get_gcode_stub(self, filepath: str) -> list[str]:
        """
        Generate stub G-code for a DXF file.
        In production, this would parse the DXF and generate actual toolpaths.
        
        Args:
            filepath: Path to the DXF file
            
        Returns:
            List of G-code command strings
        """
        # Stub implementation - return sample G-code
        return [
            "G21 ; Set units to millimeters",
            "G90 ; Absolute positioning",
            "G0 Z5.0 ; Lift to safe height",
            "G0 X0 Y0 ; Move to origin",
            "G1 Z-1.0 F100 ; Lower to cutting depth",
            "G1 X50 Y0 F300 ; Cut line",
            "G1 X50 Y50 ; Cut line",
            "G1 X0 Y50 ; Cut line",
            "G1 X0 Y0 ; Cut line",
            "G0 Z5.0 ; Lift to safe height",
            "G0 X0 Y0 ; Return to origin",
            "M2 ; Program end",
        ]
    
    def list_uploaded_files(self) -> list[str]:
        """
        Get list of all uploaded DXF files.
        
        Returns:
            List of filenames in the upload directory
        """
        if not self.upload_dir.exists():
            return []
        
        files = [f.name for f in self.upload_dir.iterdir() if f.is_file() and f.suffix.lower() == '.dxf']
        return sorted(files)
    
    def delete_file(self, filename: str) -> bool:
        """
        Delete a file from the uploads directory.
        
        Args:
            filename: Name of the file to delete
            
        Returns:
            True if successful, False otherwise
        """
        try:
            filepath = self.upload_dir / filename
            if filepath.exists() and filepath.is_file():
                filepath.unlink()
                return True
        except Exception:
            pass
        return False
    
    def _sanitize_filename(self, filename: str) -> str:
        """
        Sanitize a filename to prevent directory traversal and other issues.
        
        Args:
            filename: Original filename
            
        Returns:
            Sanitized filename
        """
        # Get just the filename without path components
        safe_name = os.path.basename(filename)
        
        # Remove any remaining problematic characters
        safe_name = "".join(c for c in safe_name if c.isalnum() or c in "._- ")
        
        # Ensure it has a reasonable length
        if len(safe_name) > 255:
            name, ext = os.path.splitext(safe_name)
            safe_name = name[:250] + ext
        
        return safe_name or "unnamed.dxf"


# Global file manager instance
file_manager = FileManager()
