import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cnc_ui"))

from cnc import controller as controller_module  # noqa: E402
from cnc.controller import CNCController  # noqa: E402
from cnc.firmware_update import (  # noqa: E402
    CONTROL_CLOSE,
    CONTROL_PROTOCOL,
    CONTROL_SYNC,
    FILE_TRANSFER_PROTOCOL,
    TRANSFER_ABORT,
    TRANSFER_CLOSE,
    TRANSFER_OPEN,
    TRANSFER_QUERY,
    TRANSFER_WRITE,
    decode_packet,
    encode_packet,
)
from cnc.state import machine_state  # noqa: E402


CAPABILITIES = (
    "FIRMWARE_NAME:Test Marlin 1.0 Cap:BINARY_FILE_TRANSFER:1 "
    "Cap:SDCARD:1 Cap:SD_WRITE:1"
)


class FakeFirmwareSerial:
    """Line and BFT-aware fake used to exercise the controller transaction."""

    def __init__(self, *, capabilities=True, retry_write=False, timeout_write=False,
                 ioerror_write=False, reboot_firmware_name="Test Marlin 1.0"):
        self.is_open = True
        self.port = "fake"
        self.lines = deque()
        self.writes = []
        self.binary_packets = []
        self.capabilities = capabilities
        self.retry_write = retry_write
        self.timeout_write = timeout_write
        self.ioerror_write = ioerror_write
        self.reboot_firmware_name = reboot_firmware_name
        self.rebooted = False
        self.retried = False
        self.bft_reader_threads = []
        self.reader_thread_id = None
        self.bft_mode = False

    @property
    def in_waiting(self):
        return sum(len(line) for line, _ in self.lines)

    def readline(self):
        if not self.lines:
            return b""
        line, is_bft = self.lines.popleft()
        if is_bft and self.reader_thread_id == threading.get_ident():
            self.bft_reader_threads.append(threading.get_ident())
        return line

    def _queue(self, *lines, bft=False):
        self.lines.extend((line.encode("ascii") + b"\n", bft) for line in lines)

    def write(self, payload):
        self.writes.append(payload)
        if payload.startswith(b"\xad\xb5"):
            packet = decode_packet(payload)
            self.binary_packets.append(packet)
            if packet.protocol == CONTROL_PROTOCOL and packet.packet_type == CONTROL_SYNC:
                self.bft_mode = True
                self._queue("ss0,96,0.1.0", bft=True)
            else:
                self._queue(f"ok{packet.sync}", bft=True)
                if packet.protocol == FILE_TRANSFER_PROTOCOL:
                    if packet.packet_type == TRANSFER_QUERY:
                        self._queue("PFT:version:0.1.0:compression:none", bft=True)
                    elif packet.packet_type == TRANSFER_OPEN:
                        self._queue("PFT:success", bft=True)
                    elif packet.packet_type == TRANSFER_WRITE:
                        if self.retry_write and not self.retried:
                            self.retried = True
                            self.lines.clear()
                            self._queue(f"rs{packet.sync}", bft=True)
                        elif self.timeout_write:
                            self.lines.clear()
                        elif self.ioerror_write:
                            self._queue("PFT:ioerror", bft=True)
                    elif packet.packet_type == TRANSFER_CLOSE:
                        self._queue("PFT:success", bft=True)
                    elif packet.packet_type == TRANSFER_ABORT:
                        self._queue("PFT:success", bft=True)
                elif packet.protocol == CONTROL_PROTOCOL and packet.packet_type == CONTROL_CLOSE:
                    self.bft_mode = False
            return len(payload)

        command = payload.decode("ascii", errors="replace").strip()
        self.writes.append(command)
        if command == "M115":
            if self.capabilities:
                reported_name = (
                    self.reboot_firmware_name if self.rebooted else "Existing Marlin 1.0"
                )
                self._queue(
                    CAPABILITIES.replace("Test Marlin 1.0", reported_name), "ok"
                )
            else:
                self._queue("FIRMWARE_NAME:Marlin 2.1.x Cap:SDCARD:1 Cap:SD_WRITE:1", "ok")
        elif command == "M21":
            self._queue("SD card ok", "ok")
        elif command == "M28 B1":
            self._queue("Writing to file: firmware.bin", "Switching to Binary Protocol", "ok")
        elif command == "M20 F":
            self._queue("Begin file list", "firmware.bin", "End file list", "ok")
        elif command == "M997":
            self.rebooted = True
        return len(payload)

    def flush(self):
        pass

    def close(self):
        self.is_open = False


class FirmwareUpdateTests(unittest.TestCase):
    def setUp(self):
        self.uploader_patch = patch.object(controller_module, "_log_uploader", None)
        self.uploader_patch.start()
        self.addCleanup(self.uploader_patch.stop)
        machine_state.set_status("Idle", busy=False)
        machine_state.set_job_loaded(False)
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
        self.temp_file.write(
            b"FIRMWARE_NAME:Test Marlin 1.0 SOURCE_CODE_URL:local\x00"
            + bytes(range(256)) * 2
        )
        self.temp_file.close()
        self.addCleanup(lambda: Path(self.temp_file.name).unlink(missing_ok=True))

    def make_controller(self, **serial_options):
        controller = CNCController(auto_connect=False)
        fake = FakeFirmwareSerial(**serial_options)
        controller.serial_port = fake
        controller.connected = True
        controller.serial_uncertain = False
        controller.firmware_reconnect_timeout = 0.05

        def reconnect():
            controller.connected = True
            controller.serial_port = fake

        controller._start_reconnect_loop = reconnect
        return controller, fake

    def test_rejects_update_while_job_thread_is_running(self):
        controller, fake = self.make_controller()
        controller.job_thread = type("ActiveJob", (), {"is_alive": lambda self: True})()
        machine_state.set_status("Running", busy=True)

        success, message = controller.install_firmware(self.temp_file.name)

        self.assertFalse(success)
        self.assertIn("idle controller", message)
        self.assertEqual([], fake.writes)

    def test_rejects_update_during_sd_upload(self):
        controller, fake = self.make_controller()
        controller.sd_uploading = True

        success, _ = controller.install_firmware(self.temp_file.name)

        self.assertFalse(success)
        self.assertEqual([], fake.writes)

    def test_missing_bft_capability_fails_before_binary_mode(self):
        controller, fake = self.make_controller(capabilities=False)

        success, message = controller.install_firmware(self.temp_file.name)

        self.assertFalse(success)
        self.assertIn("BINARY_FILE_TRANSFER", message)
        self.assertNotIn("M997", fake.writes)
        self.assertFalse(fake.binary_packets)

    def test_successful_multi_packet_upload_retries_and_requests_one_reboot(self):
        controller, fake = self.make_controller(retry_write=True)
        fake.reader_thread_id = threading.get_ident() + 1

        success, message = controller.install_firmware(self.temp_file.name)

        self.assertTrue(success, message)
        packets = [p for p in fake.binary_packets if p.protocol == FILE_TRANSFER_PROTOCOL]
        self.assertGreater(sum(p.packet_type == TRANSFER_WRITE for p in packets), 1)
        self.assertEqual(
            7,
            sum(p.packet_type == TRANSFER_WRITE for p in packets),
            "six chunks plus one retransmission should be sent",
        )
        self.assertEqual(1, fake.writes.count("M997"))
        self.assertEqual("Success", controller.firmware_update_snapshot()["stage"])
        self.assertIn("Re-home", message)

    def test_transfer_timeout_does_not_send_m997(self):
        controller, fake = self.make_controller(timeout_write=True)
        real_read = controller._read_bft_packet_response

        def fast_timeout(sync, *, timeout=8.0, require_pft=False):
            return real_read(sync, timeout=0.02, require_pft=require_pft)

        controller._read_bft_packet_response = fast_timeout

        success, _ = controller.install_firmware(self.temp_file.name)

        self.assertFalse(success)
        self.assertNotIn("M997", fake.writes)
        self.assertTrue(controller.serial_uncertain)

    def test_sd_write_error_does_not_send_m997(self):
        controller, fake = self.make_controller(ioerror_write=True)

        success, message = controller.install_firmware(self.temp_file.name)

        self.assertFalse(success)
        self.assertIn("SD write error", message)
        self.assertNotIn("M997", fake.writes)

    def test_reconnect_failure_is_explicit_unknown_state(self):
        controller, fake = self.make_controller()
        controller._start_reconnect_loop = lambda: None
        controller.firmware_reconnect_timeout = 0

        success, message = controller.install_firmware(self.temp_file.name)

        self.assertFalse(success)
        self.assertIn("state is unknown", message)
        self.assertEqual(1, fake.writes.count("M997"))
        self.assertEqual("Failed", controller.firmware_update_snapshot()["stage"])

    def test_reconnected_old_firmware_is_not_reported_as_success(self):
        controller, fake = self.make_controller(reboot_firmware_name="Existing Marlin 1.0")

        success, message = controller.install_firmware(self.temp_file.name)

        self.assertFalse(success)
        self.assertIn("uploaded Marlin identity", message)
        self.assertEqual(1, fake.writes.count("M997"))
        self.assertTrue(controller.serial_uncertain)

    def test_expected_disconnect_during_firmware_reboot_is_not_uncertain(self):
        controller, fake = self.make_controller()
        controller.firmware_update_active = True
        controller.firmware_reboot_pending = True
        controller._start_reconnect_loop = lambda: None

        controller._handle_disconnect()

        self.assertFalse(controller.connected)
        self.assertFalse(controller.serial_uncertain)
        self.assertEqual("Reconnecting", controller.firmware_update_snapshot()["stage"])
        self.assertEqual("fake", fake.port)

    def test_background_reader_does_not_consume_bft_responses(self):
        controller, fake = self.make_controller()
        stop_reader = threading.Event()
        original_read_loop = controller._read_loop

        def tracked_reader():
            fake.reader_thread_id = threading.get_ident()
            original_read_loop()

        controller.read_thread = threading.Thread(target=tracked_reader, daemon=True)
        controller.read_thread.start()

        success, message = controller.install_firmware(self.temp_file.name)
        controller.connected = False
        controller.read_thread.join(timeout=1)

        self.assertTrue(success, message)
        self.assertEqual([], fake.bft_reader_threads)

    def test_bft_packet_checksums_round_trip_and_detect_corruption(self):
        packet = encode_packet(9, FILE_TRANSFER_PROTOCOL, TRANSFER_WRITE, b"firmware")
        self.assertEqual(b"firmware", decode_packet(packet).payload)
        damaged = packet[:-1] + bytes([packet[-1] ^ 0x01])

        with self.assertRaises(ValueError):
            decode_packet(damaged)


if __name__ == "__main__":
    unittest.main()
