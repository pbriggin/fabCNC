import sys
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cnc_ui"))

from cnc import controller as controller_module  # noqa: E402
from cnc.controller import CNCController, parse_homing_sensitivity_response  # noqa: E402
from cnc.state import machine_state  # noqa: E402


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

    def open(self):
        self.is_open = True


def connected_controller(responses=(), on_write=None, auto_ok=False):
    controller = CNCController(auto_connect=False)
    controller.serial_port = FakeSerial(responses, on_write=on_write, auto_ok=auto_ok)
    controller.connected = True
    controller.serial_uncertain = False
    return controller


class ControllerSafetyTests(unittest.TestCase):
    def setUp(self):
        uploader_patch = patch.object(controller_module, "_log_uploader", None)
        uploader_patch.start()
        self.addCleanup(uploader_patch.stop)
        machine_state.set_status("Idle", busy=False)
        machine_state.set_job_loaded(False)

    def test_controller_opens_configured_uart_at_configured_baud_without_dtr_pulse(self):
        controller = CNCController(auto_connect=False, port="/dev/ttyAMA0", baudrate=115200)
        fake_serial = FakeSerial()
        fake_serial.is_open = False
        fake_serial.dtr = True
        open_dtr_states = []
        original_open = fake_serial.open

        def record_open():
            open_dtr_states.append(fake_serial.dtr)
            original_open()

        fake_serial.open = record_open
        with (
            patch.object(controller_module.serial, "Serial", return_value=fake_serial) as serial_open,
            patch.object(controller, "_probe_marlin_on_open_port", return_value=True),
            patch.object(controller, "_read_loop", return_value=None),
            patch.object(controller, "_send_command"),
        ):
            self.assertTrue(controller._connect_configured_port())

        serial_open.assert_called_once_with(
            port=None,
            baudrate=115200,
            timeout=2.0,
            write_timeout=2.0,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        self.assertEqual([False], open_dtr_states)
        self.assertEqual("/dev/ttyAMA0", fake_serial.port)
        self.assertTrue(controller.connected)

    def test_manual_command_is_rejected_while_machine_busy(self):
        controller = connected_controller(["ok"])
        machine_state.set_status("Running", busy=True)

        response = controller.send_manual_command("G92 X0 Y0")

        self.assertIn("not safely idle", response)
        self.assertEqual([], controller.serial_port.writes)

    def test_m914_response_parser_accepts_marlin_homing_sensitivity_format(self):
        response = (
            "X homing sensitivity: 2\n"
            "Y homing sensitivity: 1\n"
            "Y2 homing sensitivity: 1\n"
            "ok"
        )

        self.assertEqual(
            {"X": 2, "Y": 1},
            parse_homing_sensitivity_response(response),
        )

    def test_homing_sensitivity_apply_sends_m914_then_m500(self):
        controller = connected_controller(
            on_write={"M914 X2 Y1": ["ok"], "M500": ["Settings Stored", "ok"]}
        )

        ok, response = controller.set_homing_sensitivity(
            x=2, y=1, persist_to_eeprom=True
        )

        self.assertTrue(ok, response)
        self.assertEqual(["M914 X2 Y1", "M500"], controller.serial_port.writes)

    def test_invalid_jog_axis_cannot_reach_serial(self):
        controller = connected_controller(auto_ok=True)

        controller.jog("X G28", 10, 1000)

        self.assertEqual([], controller.serial_port.writes)

    def test_machine_state_snapshot_is_immutable_and_consistent(self):
        machine_state.set_status("Paused", busy=True, paused=True)
        machine_state.update_position(x=12.5, y=7.0)

        snapshot = machine_state.snapshot()

        self.assertEqual((12.5, 7.0), (snapshot.x, snapshot.y))
        self.assertTrue(snapshot.busy)
        self.assertTrue(snapshot.paused)
        with self.assertRaises(Exception):
            snapshot.busy = False

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
        worker = controller.run_utility_sequence(["G90", "G0 X10"])
        worker.join(timeout=1)

        self.assertEqual("M400", captured[-1])

    def test_successful_utility_sequence_returns_to_idle_not_job_complete(self):
        controller = connected_controller()
        controller._quiesce_before_stream = lambda: True

        def accept_reset(_payload):
            with controller.ok_lock:
                controller.ok_count += 1
            return True

        def accept_command(_command):
            with controller.ok_lock:
                controller.ok_count += 1
            controller.committed_line += 1
            return controller.committed_line

        controller._write_serial_payload = accept_reset
        controller._emit_new_streamed = accept_command

        worker = controller.run_utility_sequence(["G90", "G0 X10"])
        self.assertIsNotNone(worker)
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual("Idle", machine_state.snapshot().status_text)

    def test_rejected_utility_sequence_does_not_start_worker(self):
        controller = connected_controller(auto_ok=True)
        machine_state.set_status("Running", busy=True)

        worker = controller.run_utility_sequence(["G90", "G0 X10"])

        self.assertIsNone(worker)

    def test_unhomed_controller_accepts_job_that_fully_homes_before_motion(self):
        controller = connected_controller()
        controller.homed = False
        controller.rehome_required = True
        captured = []
        controller._execute_sd_job = lambda lines: captured.extend(lines)

        controller.start_job([
            "G21", "G90", "M17", "G28 X Y Z", "G28 A", "G0 Z-15"
        ])
        controller.job_thread.join(timeout=1)

        self.assertIn("G28 X Y Z", captured)
        self.assertIn("G0 Z-15", captured)

    def test_unhomed_controller_rejects_job_that_moves_before_full_home(self):
        controller = connected_controller()
        controller.homed = False
        controller.rehome_required = True

        controller.start_job(["G90", "G28 Z", "G0 X10", "G28 X Y A"])

        self.assertIsNone(controller.job_thread)
        self.assertFalse(controller.sd_uploading)

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

    def test_estop_reset_requires_explicit_acknowledgement(self):
        controller = connected_controller()
        controller.reset_required = True

        result = controller.reset_controller_after_estop()

        self.assertFalse(result)
        self.assertTrue(controller.reset_required)
        self.assertEqual("No response", controller.last_controller_error)

    def test_missing_sd_start_ack_preserves_recovery_marker(self):
        controller = connected_controller()
        controller._current_job_piece_count = 1
        machine_state.set_job_loaded(True, "test.dxf")
        responses = iter([
            "ok",                 # M110
            "ok",                 # M21
            "ok",                 # M28
            "ok",                 # numbered G-code line
            "ok",                 # M29
            "FABJOB.GCO\nok",    # M20
            "ok",                 # M23
            "",                   # M24 acknowledgement lost
        ])
        controller._read_response = lambda **kwargs: next(responses)

        try:
            uploaded, _, _ = controller._upload_gcode_to_sd(["G90"])

            self.assertFalse(uploaded)
            self.assertTrue(controller.sd_job_active)
            self.assertTrue(controller.serial_uncertain)
            self.assertTrue(controller_module._SD_JOB_STATE_FILE.exists())
        finally:
            controller_module._SD_JOB_STATE_FILE.unlink(missing_ok=True)

    def test_reconnect_timeout_keeps_possible_sd_job_marker(self):
        controller = connected_controller()
        controller.sd_job_active = True
        controller.sd_job_disconnected = True
        controller.send_command_with_response = lambda *args, **kwargs: "No response"
        controller_module._SD_JOB_STATE_FILE.write_text("{}")

        try:
            controller._reconcile_sd_job_after_reconnect()

            self.assertTrue(controller.sd_job_active)
            self.assertTrue(controller.sd_job_disconnected)
            self.assertTrue(controller.serial_uncertain)
            self.assertTrue(controller_module._SD_JOB_STATE_FILE.exists())
            self.assertTrue(machine_state.busy)
        finally:
            controller_module._SD_JOB_STATE_FILE.unlink(missing_ok=True)

    def test_uncertain_recovery_clears_sd_marker_only_after_idle_confirmation(self):
        controller = connected_controller()
        controller.serial_uncertain = True
        controller.sd_job_active = True
        controller._quiesce_before_stream = lambda timeout=180.0: True
        controller._exchange_locked = lambda command, timeout: "Not SD printing\nok"
        controller_module._SD_JOB_STATE_FILE.write_text("{}")

        try:
            recovered = controller.recover_uncertain_state()

            self.assertTrue(recovered)
            self.assertFalse(controller.sd_job_active)
            self.assertFalse(controller.serial_uncertain)
            self.assertTrue(controller.rehome_required)
            self.assertFalse(controller_module._SD_JOB_STATE_FILE.exists())
        finally:
            controller_module._SD_JOB_STATE_FILE.unlink(missing_ok=True)

    def test_uncertain_recovery_preserves_active_sd_job(self):
        controller = connected_controller()
        controller.serial_uncertain = True
        controller.sd_job_active = True
        controller._quiesce_before_stream = lambda timeout=180.0: True
        controller._exchange_locked = lambda command, timeout: (
            "SD printing byte 120/1000\nok"
        )
        controller_module._SD_JOB_STATE_FILE.write_text("{}")

        try:
            recovered = controller.recover_uncertain_state()

            self.assertFalse(recovered)
            self.assertTrue(controller.sd_job_active)
            self.assertTrue(controller.serial_uncertain)
            self.assertTrue(controller_module._SD_JOB_STATE_FILE.exists())
        finally:
            controller_module._SD_JOB_STATE_FILE.unlink(missing_ok=True)

    def test_pause_ack_loss_keeps_job_active_and_blocks_motion(self):
        controller = connected_controller()
        controller.sd_job_active = True
        controller.send_command_with_response = lambda *args, **kwargs: "No response"
        machine_state.set_status("Running", busy=True)

        controller.pause_job()

        self.assertTrue(controller.sd_job_active)
        self.assertTrue(controller.serial_uncertain)
        self.assertFalse(machine_state.snapshot().paused)
        self.assertTrue(machine_state.snapshot().busy)

    def test_resume_ack_loss_does_not_claim_running(self):
        controller = connected_controller()
        controller.sd_job_active = True
        controller.send_command_with_response = lambda *args, **kwargs: "No response"
        machine_state.set_status("Paused", busy=True, paused=True)

        controller.resume_job()

        self.assertTrue(controller.serial_uncertain)
        self.assertNotEqual("Running", machine_state.snapshot().status_text)
        self.assertTrue(machine_state.snapshot().busy)
        self.assertTrue(controller.sd_job_active)

    def test_stop_ack_loss_preserves_possible_sd_job(self):
        controller = connected_controller()
        controller.sd_job_active = True
        controller._quiesce_after_priority_stop = lambda timeout=30.0: False
        machine_state.set_status("Running", busy=True)

        stopped = controller.stop_job()

        self.assertFalse(stopped)
        self.assertTrue(controller.sd_job_active)
        self.assertEqual(["M524", "M410"], controller.serial_port.writes)
        self.assertFalse(controller.homed)
        self.assertTrue(controller.rehome_required)
        self.assertTrue(machine_state.snapshot().busy)

    def test_stop_writes_priority_commands_before_operation_lock_is_available(self):
        controller = connected_controller()
        controller.sd_job_active = True
        controller.homed = True
        controller._quiesce_after_priority_stop = lambda timeout=30.0: False
        machine_state.set_status("Running", busy=True)

        controller.operation_lock.acquire()
        try:
            worker = threading.Thread(target=controller.stop_job)
            worker.start()
            deadline = time.monotonic() + 0.5
            while len(controller.serial_port.writes) < 2 and time.monotonic() < deadline:
                time.sleep(0.005)

            self.assertEqual(["M524", "M410"], controller.serial_port.writes)
            self.assertTrue(controller.stop_requested)
            self.assertFalse(controller.homed)
            self.assertTrue(controller.rehome_required)
            self.assertEqual("Stopping", machine_state.snapshot().status_text)
            self.assertTrue(worker.is_alive())
        finally:
            controller.operation_lock.release()
        worker.join(timeout=1)

    def test_confirmed_sd_stop_requires_rehome(self):
        controller = connected_controller()
        controller.sd_job_active = True
        controller.homed = True
        controller._quiesce_after_priority_stop = lambda timeout=30.0: True
        controller._exchange_locked = lambda command, timeout: "Not SD printing\nok"
        controller._clear_sd_job_state = lambda: setattr(controller, "sd_job_active", False)
        machine_state.set_status("Running", busy=True)

        stopped = controller.stop_job()

        self.assertTrue(stopped)
        self.assertEqual(["M524", "M410"], controller.serial_port.writes)
        self.assertFalse(controller.homed)
        self.assertTrue(controller.rehome_required)
        self.assertEqual("Stopped — re-home required", machine_state.snapshot().status_text)

    def test_stop_during_job_uploads_stopped_log_once(self):
        controller = connected_controller()
        controller.sd_job_active = True
        controller._quiesce_after_priority_stop = lambda timeout=30.0: True
        controller._exchange_locked = lambda command, timeout: "Not SD printing\nok"
        controller._clear_sd_job_state = lambda: setattr(controller, "sd_job_active", False)
        upload_calls = []
        uploader = SimpleNamespace(
            upload_now=lambda full, trigger: upload_calls.append((full, trigger))
        )

        class ImmediateThread:
            def __init__(self, target, args=(), **kwargs):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)

        with patch.object(controller_module, "_log_uploader", uploader), \
                patch.object(controller_module.threading, "Thread", ImmediateThread):
            self.assertTrue(controller.stop_job())
            self.assertTrue(controller.stop_job())

        self.assertEqual([(False, "job_stopped")], upload_calls)

    def test_priority_stop_boundary_retries_after_abort_clears_first_probe(self):
        controller = connected_controller()
        attempts = iter(((False, ""), (True, "FIRMWARE_NAME:Marlin\nok")))
        controller._try_serial_boundary = lambda timeout: next(attempts)

        synchronized = controller._quiesce_after_priority_stop(timeout=1.0)

        self.assertTrue(synchronized)
        self.assertFalse(controller.serial_uncertain)

    def test_priority_stop_boundary_marks_uncertain_only_after_retries_fail(self):
        controller = connected_controller()
        controller._try_serial_boundary = lambda timeout: (False, "no response")

        synchronized = controller._quiesce_after_priority_stop(timeout=0.02)

        self.assertFalse(synchronized)
        self.assertTrue(controller.serial_uncertain)
        self.assertEqual(
            "Machine state uncertain — verify controller",
            machine_state.snapshot().status_text,
        )

    def test_uncertain_state_home_is_rejected_without_g28(self):
        controller = connected_controller()
        controller.serial_uncertain = True
        machine_state.set_status("Idle", busy=False)

        homed = controller.home_all()

        self.assertFalse(homed)
        self.assertNotIn("G28", controller.serial_port.writes)

    def test_recovery_home_all_cycles_y_motors_before_g28(self):
        controller = connected_controller()
        controller.rehome_required = True
        controller.reset_y_motors_before_home = True
        controller._quiesce_before_stream = lambda timeout=180.0: True
        commands = []
        controller._exchange_locked = lambda command, timeout: commands.append(command) or "ok"

        homed = controller.home_all()

        self.assertTrue(homed)
        self.assertEqual(
            ["M18 Y", "G4 P1000", "M17 Y", "G4 P500", "M17"],
            commands[:5],
        )
        self.assertLess(commands.index("M17 Y"), commands.index("G28"))
        self.assertFalse(controller.reset_y_motors_before_home)

    def test_normal_home_all_does_not_cycle_y_motors(self):
        controller = connected_controller()
        controller._quiesce_before_stream = lambda timeout=180.0: True
        commands = []
        controller._exchange_locked = lambda command, timeout: commands.append(command) or "ok"

        homed = controller.home_all()

        self.assertTrue(homed)
        self.assertNotIn("M18 Y", commands)
        self.assertEqual("M17", commands[0])

    def test_home_x_preserves_pending_y_motor_cycle(self):
        controller = connected_controller()
        controller.reset_y_motors_before_home = True
        controller._quiesce_before_stream = lambda timeout=180.0: True
        commands = []
        controller._exchange_locked = lambda command, timeout: commands.append(command) or "ok"

        homed = controller.home_axis("X")

        self.assertTrue(homed)
        self.assertNotIn("M18 Y", commands)
        self.assertTrue(controller.reset_y_motors_before_home)

    def test_sd_monitor_does_not_issue_position_poll(self):
        controller = connected_controller()
        controller.sd_job_active = True
        calls = []

        def respond(command, timeout=5.0):
            calls.append(command)
            controller.stop_requested = True
            return "SD printing byte 10/100\nok"

        controller.send_command_with_response = respond
        controller._monitor_sd_job(total_bytes=100)

        self.assertEqual(["M27"], calls)
        self.assertNotIn("M114", calls)


if __name__ == "__main__":
    unittest.main()
