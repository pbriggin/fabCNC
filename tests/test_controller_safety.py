import sys
import threading
import time
import unittest
from collections import deque
from pathlib import Path
import serial.tools.list_ports


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cnc_ui"))

_real_comports = serial.tools.list_ports.comports
serial.tools.list_ports.comports = lambda: []
from cnc.controller import CNCController  # noqa: E402
from cnc.state import machine_state  # noqa: E402
serial.tools.list_ports.comports = _real_comports


class FakeSerial:
    def __init__(self, responses=(), on_write=None, auto_ok=False):
        self.is_open = True
        self._responses = deque(line.encode() + b"\n" for line in responses)
        self.writes = []
        self.on_write = on_write or {}
        self.auto_ok = auto_ok
        self.port = "fake"

    @property
    def in_waiting(self):
        return len(self._responses)

    def readline(self):
        return self._responses.popleft() if self._responses else b""

    def write(self, payload):
        command = payload.decode().rstrip("\n")
        self.writes.append(command)
        for line in self.on_write.get(command, ()):
            self._responses.append(line.encode() + b"\n")
        if self.auto_ok:
            self._responses.append(b"ok\n")
        return len(payload)

    def flush(self):
        pass


def connected_controller(responses=(), on_write=None, auto_ok=False):
    controller = CNCController(auto_connect=False)
    controller.serial_port = FakeSerial(responses, on_write=on_write, auto_ok=auto_ok)
    controller.connected = True
    controller.serial_uncertain = False
    return controller


class ControllerSafetyTests(unittest.TestCase):
    def setUp(self):
        machine_state.set_status("Idle", busy=False)
        machine_state.set_job_loaded(False)

    def test_manual_command_is_rejected_while_machine_busy(self):
        controller = connected_controller(["ok"])
        machine_state.set_status("Running", busy=True)

        response = controller.send_manual_command("G92 X0 Y0")

        self.assertIn("not safely idle", response)
        self.assertEqual([], controller.serial_port.writes)

    def test_quiesce_requires_identifiable_boundary_not_stale_ok(self):
        controller = connected_controller(["ok"])

        result = controller._quiesce_before_stream(timeout=0.02)

        self.assertFalse(result)
        self.assertTrue(controller.serial_uncertain)
        self.assertIn("M400", controller.serial_port.writes)
        self.assertIn("M115", controller.serial_port.writes)

    def test_quiesce_accepts_m115_banner_followed_by_ok(self):
        controller = connected_controller(
            ["ok"],  # stale acknowledgement; drained before the boundary
            on_write={"M115": ["FIRMWARE_NAME:Marlin 2.1.2", "ok"]},
        )

        result = controller._quiesce_before_stream(timeout=0.1)

        self.assertTrue(result)
        self.assertFalse(controller.serial_uncertain)

    def test_concurrent_jogs_cannot_interleave_modal_sequences(self):
        controller = connected_controller(auto_ok=True)
        original = controller._jog_locked

        def slow_jog(axis, distance, feed):
            original(axis, distance, feed)
            time.sleep(0.01)

        controller._jog_locked = slow_jog
        threads = [
            threading.Thread(target=controller.jog, args=("X", 10, 1000)),
            threading.Thread(target=controller.jog, args=("Y", 20, 1000)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        writes = controller.serial_port.writes
        relative_indices = [i for i, command in enumerate(writes) if command == "G91"]
        self.assertEqual(2, len(relative_indices))
        first_relative, second_relative = relative_indices
        self.assertIn("G90", writes[first_relative:second_relative])
        self.assertIn("G1 X10 F1000", writes)
        self.assertIn("G1 Y20 F1000", writes)

    def test_utility_sequence_adds_physical_completion_barrier(self):
        controller = connected_controller()
        captured = []

        def capture(lines, is_job=False):
            captured.extend(lines)
            machine_state.set_status("Idle", busy=False)

        controller._execute_job = capture
        controller.run_utility_sequence(["G90", "G0 X10"])
        controller.job_thread.join(timeout=1)

        self.assertEqual("M400", captured[-1])

    def test_error_burst_trailing_ok_does_not_credit_stream_progress(self):
        controller = connected_controller()
        controller.streaming_mode = True
        controller.next_line_number = 4

        self.assertFalse(controller._handle_streaming_response("Error:Line Number is not Last Line Number+1, Last Line: 1"))
        self.assertFalse(controller._handle_streaming_response("Resend: 2"))
        self.assertFalse(controller._handle_streaming_response("ok"))
        self.assertEqual(1, controller.committed_line)

    def test_stream_timeout_fails_closed_without_fabricating_acks(self):
        controller = connected_controller()
        controller._quiesce_before_stream = lambda: True
        controller.stream_ack_timeout = 0.01
        controller.stream_final_ack_timeout = 0.01

        controller._execute_job(["G90", "G1 X1", "G1 X2", "G1 X3"], is_job=False)

        self.assertTrue(controller.serial_uncertain)
        self.assertEqual(0, controller.ok_count)
        self.assertNotEqual("Complete", machine_state.status_text)


if __name__ == "__main__":
    unittest.main()
