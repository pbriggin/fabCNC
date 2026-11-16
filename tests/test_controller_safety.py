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
from cnc.controller import (  # noqa: E402
    CNCController,
    parse_homing_sensitivity_response,
    parse_tmc_prewarn_response,
)
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

    def test_tmc_fault_and_open_load_records_are_persisted(self):
        with patch.object(controller_module, "log_controller_event") as record:
            controller_module._persist_tmc_event_line(
                "TMC_FAULT|v=1|kind=open_load|driver=Y2|coil=A|status=0x20000000|uptime_ms=1500|count=4"
            )
            controller_module._persist_tmc_event_line(
                "TMC_OPEN_LOAD|driver=Y2|coil=B|status=0x40000000|uptime_ms=1000|count=3"
            )

        self.assertEqual(record.call_count, 2)
        self.assertEqual(record.call_args_list[0].args[0], "tmc_driver_fault")
        self.assertEqual(record.call_args_list[1].args[0], "tmc_open_load_event")
        self.assertIn("driver=Y2", record.call_args_list[1].kwargs["event"])

    def test_tmc_fault_latches_reset_and_keeps_sd_marker_until_halt_line(self):
        controller = connected_controller()
        controller.sd_job_active = True
        fault = "TMC_FAULT|v=1|kind=open_load|driver=Y2|coil=A|status=0x20000000|uptime_ms=1500|count=4"

        controller._handle_controller_fault_line(fault)

        self.assertTrue(controller.reset_required)
        self.assertTrue(controller.stop_requested)
        self.assertFalse(controller.homed)
        self.assertEqual(fault, controller.last_driver_fault)
        self.assertTrue(controller.sd_job_active)
        self.assertEqual("E-Stop Reset Required", machine_state.snapshot().status_text)

        controller._handle_controller_fault_line("Error:Printer halted. kill() called!")

        self.assertFalse(controller.sd_job_active)
        self.assertEqual(fault, controller.last_driver_fault)

    def test_halt_kind_distinguishes_estop_soft_stop_and_kill(self):
        controller = connected_controller()
        controller._handle_controller_fault_line("Error:!! KILL caused by KILL button/pin")
        controller._handle_controller_fault_line("Error:Printer halted. kill() called!")
        self.assertEqual("estop", controller.halt_kind)

        controller = connected_controller()
        controller._handle_controller_fault_line(
            "TMC_FAULT|v=1|kind=open_load|driver=Y2|coil=A|status=0x601F0003|uptime_ms=1|count=1"
        )
        controller._handle_controller_fault_line(
            "Error:Printer stopped due to errors. Fix the error and use M999 to restart."
        )
        self.assertEqual("soft", controller.halt_kind)
        self.assertTrue(controller.reset_required)

        controller = connected_controller()
        controller._handle_controller_fault_line("Error:Printer halted. kill() called!")
        self.assertEqual("hard", controller.halt_kind)

    def test_tmc_fault_is_latched_from_a_synchronous_read(self):
        controller = connected_controller(responses=(
            "TMC_FAULT|v=1|kind=short|driver=X|coil=B|status=0x10000000|uptime_ms=9|count=1",
        ))

        controller._read_response(timeout=0.05)

        self.assertTrue(controller.reset_required)
        self.assertIn("driver=X", controller.last_driver_fault)

    def test_raw_open_load_event_is_recorded_without_stopping(self):
        controller = connected_controller()
        event = "TMC_OPEN_LOAD|driver=X|coil=A|status=0x20000000|uptime_ms=5|count=1|moving=1|enabled=1|standstill=0|stealth=0|eligible=1"

        controller._handle_controller_fault_line(event)

        self.assertEqual(event, controller.last_open_load_event)
        self.assertFalse(controller.reset_required)
        self.assertFalse(controller.stop_requested)

    def test_estop_reset_clears_driver_fault_and_open_load_state(self):
        controller = connected_controller()
        controller.reset_required = True
        controller.last_driver_fault = "TMC_FAULT|v=1|kind=open_load|driver=Y|coil=A"
        controller.last_open_load_event = "TMC_OPEN_LOAD|driver=Y|coil=A|eligible=1"
        controller._exchange_locked = lambda command, timeout: "ok"

        self.assertTrue(controller.reset_controller_after_estop())

        self.assertFalse(controller.reset_required)
        self.assertEqual("", controller.last_driver_fault)
        self.assertEqual("", controller.last_open_load_event)

    def test_estop_reset_fails_while_marlin_is_still_halted(self):
        controller = connected_controller()
        controller.reset_required = True
        controller._exchange_locked = lambda command, timeout: "No response"

        self.assertFalse(controller.reset_controller_after_estop())
        self.assertTrue(controller.reset_required)

    def test_sd_monitor_fails_closed_after_two_silent_status_polls(self):
        controller = connected_controller()
        controller.sd_job_active = True
        calls = []
        controller.send_command_with_response = lambda *a, **k: calls.append(a[0]) or "No response"

        with patch.object(controller_module.time, "sleep"):
            controller._monitor_sd_job(total_bytes=100)

        self.assertEqual(["M27", "M27"], calls)
        self.assertTrue(controller.serial_uncertain)
        self.assertTrue(controller.stop_requested)
        self.assertTrue(controller.sd_job_active)
        self.assertIn("M27", controller.last_controller_error)

    def test_sd_monitor_treats_busy_keepalive_as_a_live_link(self):
        controller = connected_controller()
        controller.sd_job_active = True
        replies = deque(["No response", "echo:busy: processing", "No response",
                         "echo:busy: processing", "No response", "No response"])
        controller.send_command_with_response = lambda *a, **k: replies.popleft()

        with patch.object(controller_module.time, "sleep"):
            controller._monitor_sd_job(total_bytes=100)

        self.assertEqual(0, len(replies))
        self.assertTrue(controller.serial_uncertain)

    def test_tmc_fault_summary_names_driver_coil_and_status(self):
        self.assertEqual(
            "Detected: open load on Y2, coil A (DRV_STATUS 0x20000000).",
            controller_module.describe_tmc_fault(
                "TMC_FAULT|v=1|kind=open_load|driver=Y2|coil=A|status=0x20000000|uptime_ms=1|count=4"
            ),
        )
        self.assertEqual(
            "Detected: overtemperature on X (DRV_STATUS 0x2000000).",
            controller_module.describe_tmc_fault(
                "TMC_FAULT|v=1|kind=overtemperature|driver=X|coil=-|status=0x2000000|uptime_ms=1|count=1"
            ),
        )
        self.assertEqual("", controller_module.describe_tmc_fault("Error:Printer halted. kill() called!"))

    def test_open_load_warning_is_shown_only_for_samples_that_count_toward_a_stop(self):
        base = "TMC_OPEN_LOAD|driver=X|coil=B|status=0x40000000|uptime_ms=1|count=2|moving=1|enabled=1|standstill=0|stealth=0|eligible="
        self.assertIn("open-load on X, coil B", controller_module.describe_open_load_event(base + "1"))
        self.assertEqual("", controller_module.describe_open_load_event(base + "0"))
        self.assertEqual("", controller_module.describe_open_load_event(""))

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

    def test_m911_response_parser_reads_latched_driver_prewarn_flags(self):
        response = (
            "X temperature prewarn triggered: false\n"
            "Y temperature prewarn triggered: true\n"
            "Y2 temperature prewarn triggered: false\n"
            "ok"
        )

        self.assertEqual(
            {"X": False, "Y": True, "Y2": False},
            parse_tmc_prewarn_response(response),
        )

    def test_job_end_m911_check_flags_the_driver_and_logs_title_data(self):
        controller = connected_controller([
            "X temperature prewarn triggered: false",
            "Y temperature prewarn triggered: true",
            "ok",
        ])

        result = controller._read_tmc_prewarn_at_job_end()

        self.assertEqual("prewarn", result["status"])
        self.assertEqual(["Y"], result["drivers"])
        self.assertEqual(
            "Job complete — TMC PREWARN LATCHED: Y",
            controller._job_completion_title(result),
        )
        self.assertEqual(["M911"], controller.serial_port.writes)

    def test_sd_job_completion_refreshes_position_from_marlin(self):
        controller = connected_controller([
            "X:0.00 Y:0.00 Z:0.00 A:0.00 Count X:0 Y:0 Z:0",
            "ok",
            "X temperature prewarn triggered: false",
            "Y temperature prewarn triggered: false",
            "ok",
        ])
        machine_state.update_position(x=860, y=830, z=0, a=0)

        controller._finish_sd_job()

        self.assertEqual(["M114", "M911"], controller.serial_port.writes)
        self.assertEqual((0.0, 0.0, 0.0, 0.0), machine_state.get_position())
        self.assertEqual("Complete", machine_state.snapshot().status_text)

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
        self.assertNotIn("M203 Z25", commands)
        self.assertNotIn("M201 Z100", commands)

    def test_normal_home_all_does_not_cycle_y_motors(self):
        controller = connected_controller()
        controller._quiesce_before_stream = lambda timeout=180.0: True
        commands = []
        controller._exchange_locked = lambda command, timeout: commands.append(command) or "ok"

        homed = controller.home_all()

        self.assertTrue(homed)
        self.assertNotIn("M18 Y", commands)
        self.assertEqual("M17", commands[0])
        self.assertFalse(any(command.startswith(("M203 Z", "M201 Z")) for command in commands))

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
        self.assertFalse(any(command.startswith(("M203 Z", "M201 Z")) for command in commands))

    def test_home_does_not_set_pi_z_limits(self):
        controller = connected_controller()
        controller._quiesce_before_stream = lambda timeout=180.0: True
        commands = []

        def respond(command, timeout):
            commands.append(command)
            return "ok"

        controller._exchange_locked = respond

        homed = controller.home_all()

        self.assertTrue(homed)
        self.assertTrue(controller.homed)
        self.assertFalse(any(command.startswith(("M203 Z", "M201 Z")) for command in commands))

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
