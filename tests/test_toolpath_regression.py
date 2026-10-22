import re
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cnc_ui"))

from toolpath_planning.toolpath_generator import ToolpathGenerator  # noqa: E402
from toolpath_planning.shape_metadata import store_shape_snapshot  # noqa: E402


class ToolpathRegressionTests(unittest.TestCase):
    def test_shape_move_without_segment_fields_preserves_notch_segmentation(self):
        shapes = {'part': [(0, 0), (10, 0), (10, 10)]}
        breaks = {'part': [0, 1, 2]}
        types = {'part': ['line', 'arc', 'line']}

        store_shape_snapshot(
            'part', [(5, 5), (15, 5), (15, 15)], {'shapeName': 'part'},
            shapes, breaks, types, preserve_missing_metadata=True,
        )

        self.assertEqual(breaks['part'], [0, 1, 2])
        self.assertEqual(types['part'], ['line', 'arc', 'line'])
        self.assertEqual(shapes['part'], [(5, 5), (15, 5), (15, 15)])

    def test_shape_snapshot_updates_segment_data_or_defaults_new_shape(self):
        shapes, breaks, types = {}, {}, {}
        points = [(0, 0), (10, 0), (10, 10)]
        metadata = {'segmentBreaks': [0, 2], 'segmentTypes': ['line', 'arc']}

        store_shape_snapshot('part', points, metadata, shapes, breaks, types)
        self.assertEqual(breaks['part'], [0, 2])
        self.assertEqual(types['part'], ['line', 'arc'])

        store_shape_snapshot('legacy', points, {}, shapes, breaks, types)
        self.assertEqual(breaks['legacy'], [0])
        self.assertEqual(types['legacy'], [])

    def make_generator(self, output_dir: str) -> ToolpathGenerator:
        generator = ToolpathGenerator()
        generator.output_dir = output_dir
        return generator

    def test_generated_job_establishes_units_mode_and_completion_barrier(self):
        with tempfile.TemporaryDirectory() as output_dir:
            gcode = self.make_generator(output_dir).generate_toolpath(
                {"square": [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]}
            )

        executable = [line.split(";", 1)[0].strip() for line in gcode.splitlines()]
        executable = [line for line in executable if line]
        self.assertLess(executable.index("G21"), executable.index("G90"))
        self.assertIn("G28 X Y Z", executable)
        self.assertIn("G28 A", executable)
        self.assertLess(executable.index("M18 Y"), executable.index("G28 X Y Z"))
        self.assertLess(executable.index("M17 Y"), executable.index("G28 X Y Z"))
        self.assertIn("M400", executable)
        self.assertLess(executable.index("M400"), executable.index("M18"))

    def test_a_axis_commands_never_jump_more_than_180_degrees(self):
        with tempfile.TemporaryDirectory() as output_dir:
            gcode = self.make_generator(output_dir).generate_toolpath(
                {"loop": [(0, 0), (20, 0), (20, 20), (0, 20), (0, 0)]}
            )

        angles = []
        for line in gcode.splitlines():
            command = line.split(";", 1)[0].strip()
            if not command.startswith(("G0 ", "G1 ")):
                continue
            match = re.search(r"\bA(-?\d+(?:\.\d+)?)", command)
            if match:
                angles.append(float(match.group(1)))
        self.assertGreater(len(angles), 3)
        for previous, current in zip(angles, angles[1:]):
            self.assertLessEqual(abs(current - previous), 180.0)

    def test_notches_are_emitted_before_shape_outlines(self):
        notch = {"apex": [5, 2], "e1": [3, 0], "e2": [7, 0]}
        with tempfile.TemporaryDirectory() as output_dir:
            gcode = self.make_generator(output_dir).generate_toolpath(
                {"part": [(0, 0), (10, 0), (10, 10)]},
                notches={"part": [notch]},
            )

        self.assertLess(gcode.index("; Notch on part"), gcode.index("; Shape: part"))


if __name__ == "__main__":
    unittest.main()
