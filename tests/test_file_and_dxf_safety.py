import sys
import tempfile
import unittest
from pathlib import Path

import ezdxf


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cnc_ui"))

from cnc.files import FileManager  # noqa: E402
from dxf_processing.dxf_processor import DXFProcessor  # noqa: E402


class FileAndDxfSafetyTests(unittest.TestCase):
    def test_delete_file_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = FileManager(upload_dir=temp_dir)
            outside = manager.upload_dir.parent / "outside.dxf"
            outside.write_text("must remain")

            self.assertFalse(manager.delete_file("../outside.dxf"))
            self.assertEqual("must remain", outside.read_text())
            outside.unlink()

    def test_lwpolyline_bulge_is_preserved_as_curve(self):
        document = ezdxf.new()
        entity = document.modelspace().add_lwpolyline(
            [(0, 0, 1), (10, 0, 0)],
            format="xyb",
        )

        points = DXFProcessor()._process_lwpolyline(entity)

        self.assertGreater(len(points), 2)
        self.assertTrue(any(abs(y) > 1.0 for _, y in points[1:-1]))

    def test_zero_radius_circle_is_ignored(self):
        document = ezdxf.new()
        entity = document.modelspace().add_circle((0, 0), radius=0)

        self.assertEqual([], DXFProcessor()._process_circle(entity))


if __name__ == "__main__":
    unittest.main()
