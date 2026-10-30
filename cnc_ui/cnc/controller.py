# cnc/controller.py
"""
CNC controller interface - provides methods for controlling the CNC machine.
This implementation communicates with Marlin firmware via serial.
"""

import json
import hashlib
import math
import os
import re
import time
import threading
import uuid
from pathlib import Path
import serial
from typing import Callable, Optional
from .firmware_update import (
    BFTProtocolError,
    CONTROL_CLOSE,
    CONTROL_PROTOCOL,
    CONTROL_SYNC,
    FILE_TRANSFER_PROTOCOL,
    PROTOCOL_VERSION,
    TRANSFER_ABORT,
    TRANSFER_CLOSE,
    TRANSFER_OPEN,
    TRANSFER_QUERY,
    TRANSFER_WRITE,
    decode_packet,
    encode_packet,
    parse_ack,
    parse_sync_response,
)
from .state import machine_state
import logging

# Persisted between runs — written on disconnect, deleted on successful resume
_RESUME_STATE_FILE = Path(__file__).parent.parent / 'resume_state.json'
_SD_JOB_STATE_FILE = Path(__file__).parent.parent / 'sd_job_state.json'

# Marlin line-number protocol parsing. Used while a job is streaming so we can
# recover from corrupted bytes on the serial link via Marlin's built-in Resend
# mechanism.
_OK_N_RE = re.compile(r"^ok\s+N(\d+)", re.IGNORECASE)
_RESEND_RE = re.compile(r"Resend\s*:\s*(\d+)", re.IGNORECASE)
_LAST_LINE_RE = re.compile(r"Last\s*Line\s*:\s*(\d+)", re.IGNORECASE)
_SD_PROGRESS_RE = re.compile(r"SD printing byte\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)
_SD_FILENAME = "FABJOB.GCO"  # Short 8.3-compatible name supported by Marlin SD builds.
_SD_UPLOAD_POLL_INTERVAL = 0.001  # 1 ms; don't wait the normal 10 ms between SD-write acks.
SERIAL_PORT = os.environ.get("FABCNC_SERIAL_PORT", "/dev/ttyAMA0")
SERIAL_BAUDRATE = int(os.environ.get("FABCNC_SERIAL_BAUDRATE", "115200"))
_FIRMWARE_FILENAME = "firmware.bin"
_MAX_FIRMWARE_SIZE = 1024 * 1024
_BFT_PACKET_RETRIES = 3


def _prepare_sd_commands(gcode_lines: list[str]) -> list[str]:
    """Return executable G-code lines in the same form as the serial streamer."""
    commands = []
    for raw_line in gcode_lines:
        command = raw_line.split(";", 1)[0].strip()
        if not command or command.startswith("#"):
            continue
        if command.split(maxsplit=1)[0].upper() == "M29":
            raise ValueError("G-code contains M29, which would terminate the SD upload")
        try:
            encoded = command.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("G-code contains non-ASCII command text") from exc
        # SD upload still uses Marlin's line-number/checksum protocol once a
        # streamed job has enabled it. Include that wire overhead in the limit.
        next_line = len(commands) + 1
        wire = f"N{next_line} {command}*255".encode("ascii")
        if len(wire) > 95:  # MAX_CMD_SIZE is 96 bytes including the terminator.
            raise ValueError(f"Numbered G-code line exceeds Marlin's 95-byte limit: {command[:40]}")
        commands.append(command)
    return commands


def _sd_response_ok(response: str) -> bool:
    """Return whether a Marlin response contains an acknowledgement and no error."""
    lines = [line.strip().lower() for line in response.splitlines()]
    return any(line.startswith("ok") for line in lines) and not any(
        line.startswith("error") or "unknown command" in line for line in lines
    )


def _parse_sd_progress(response: str) -> tuple[int, int] | None:
    """Parse Marlin's M27 `SD printing byte <position>/<size>` response."""
    match = _SD_PROGRESS_RE.search(response)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def parse_homing_sensitivity_response(response: str) -> dict[str, int]:
    """Parse Marlin M914 output such as ``X homing sensitivity: 2``."""
    values: dict[str, int] = {}
    pattern = re.compile(
        r"^\s*(X|Y)\s+(?:homing\s+)?sensitivity\s*:\s*(-?\d+)\s*$",
        re.IGNORECASE,
    )
    for line in response.splitlines():
        match = pattern.match(line)
        if match:
            values[match.group(1).upper()] = int(match.group(2))
    return values


def parse_tmc_prewarn_response(response: str) -> dict[str, bool]:
    """Parse Marlin M911 output into per-driver overtemperature prewarn flags."""
    values: dict[str, bool] = {}
    pattern = re.compile(
        r"^\s*([A-Z][A-Z0-9]*)\s+temperature\s+prewarn\s+triggered:\s*(true|false)\s*$",
        re.IGNORECASE,
    )
    for line in response.splitlines():
        match = pattern.match(line)
        if match:
            values[match.group(1).upper()] = match.group(2).lower() == "true"
    return values


def _marlin_checksum(payload: str) -> int:
    """XOR-of-bytes checksum used by Marlin for line-numbered commands."""
    cs = 0
    for b in payload.encode('utf-8'):
        cs ^= b
    return cs & 0xff

try:
    # Structured logging helpers — present whenever main.py has been imported.
    from logging_setup import (
        log_serial_tx,
        log_serial_rx,
        log_controller_event,
    )
    import log_uploader as _log_uploader
except Exception:  # pragma: no cover — keep the controller importable standalone
    def log_serial_tx(*a, **kw): pass
    def log_serial_rx(*a, **kw): pass
    def log_controller_event(*a, **kw): pass
    _log_uploader = None

logger = logging.getLogger(__name__)


class CNCController:
    """
    Interface for Marlin-based CNC machine control.
    Provides jogging, homing, and job execution capabilities.
    """
    
    def __init__(
        self,
        baudrate: int = SERIAL_BAUDRATE,
        *,
        port: str = SERIAL_PORT,
        auto_connect: bool = True,
    ):
        self.serial_device = port
        self.baudrate = baudrate
        self.serial_port: Optional[serial.Serial] = None
        self.stop_requested = False
        self.pause_requested = False
        self.sd_uploading = False
        self.sd_job_active = False
        self.sd_job_disconnected = False
        self.job_thread: Optional[threading.Thread] = None
        self.read_thread: Optional[threading.Thread] = None
        self.connected = False
        self.disconnect_started_at: Optional[float] = None
        self.reconnect_thread_active = False
        self.reset_required = False
        self.last_controller_error = ""
        self.last_serial_device = self.serial_device
        self.emergency_parser_supported: Optional[bool] = None
        self.read_loop_paused = False
        self.reader_paused = threading.Event()
        self.read_lock = threading.Lock()
        # Protect bytes on the wire independently from response ownership.
        # Safety commands may bypass operation_lock, but must never interleave
        # their bytes with another serial write.
        self.serial_write_lock = threading.Lock()
        # Serial operations are transactions, not individual writes.  This
        # re-entrant lock prevents jog/home/console/monitor operations from
        # interleaving modal commands or stealing one another's responses.
        self.operation_lock = threading.RLock()
        # Coordinates firmware ownership with the priority Stop reservation.
        # Stop must remain able to bypass operation_lock during a job.
        self.firmware_update_gate = threading.Lock()
        self.serial_uncertain = False
        self.firmware_update_active = False
        self.firmware_update_generation = 0
        self.firmware_reboot_pending = False
        self._firmware_status_lock = threading.Lock()
        self._firmware_update_status = {
            "active": False,
            "stage": "Idle",
            "progress": 0.0,
            "message": "No firmware update has been started.",
            "filename": None,
            "size": 0,
            "sha256": None,
        }
        self.rehome_required = False
        # A software Stop may leave the dual-Y motors in a state that makes the
        # next sensorless home trigger asymmetrically. Release and re-energize
        # Y once before the next Y-containing recovery home. This is deliberately
        # not applied to ordinary cold homing.
        self.reset_y_motors_before_home = False
        
        # Flow control for streaming
        self.ok_count = 0  # Number of 'ok' responses received
        self.ok_lock = threading.Lock()
        self.ok_event = threading.Event()  # Signal when ok received
        self.streaming_mode = False  # True when streaming a job
        # Buffer settings for streaming (Marlin typically has 4-16 command buffer)
        self.buffer_size = 8  # Keep this many commands ahead to prevent pauses
        self.homing_timeout = 180.0
        self.stream_ack_timeout = 30.0
        self.stream_final_ack_timeout = 10.0
        self.firmware_reconnect_timeout = 120.0

        # Line-number protocol state (active only while streaming_mode is True).
        # Each streamed line is wrapped as "N<n> <cmd>*<xor-cs>". Marlin will
        # respond with `Resend: <n>` if the bytes arrived corrupted, allowing
        # us to retransmit instead of silently skipping a move.
        self.line_buffer_lock = threading.Lock()
        self.next_line_number = 1            # N to assign to next new command
        self.line_buffer: dict[int, str] = {}  # N -> raw command (no N, no cs)
        self._max_line_buffer = 64           # keep ~recent history for resends
        self.committed_line = 0              # highest N Marlin has confirmed done
        self.resend_requested: Optional[int] = None  # lowest N Marlin wants resent
        # Marlin replies to a bad-checksum line with up to three back-to-back
        # lines (Error / Resend / ok), then exactly one trailing `ok`. This
        # latch absorbs that single trailing `ok` so we don't credit it as
        # forward progress. It's a one-shot per burst — additional Error/
        # Resend lines within the same burst leave it set.
        self.error_burst_pending = False
        self.resend_total = 0                # cumulative resends in current job
        
        # Try to connect on initialization
        self.homed = False              # True only after a successful home_all()
        self._current_job_gcode: list = []  # Copy of last started job's gcode
        self._current_job_piece_count = 0
        self._current_source_filename: Optional[str] = None
        self.current_job_id = ""
        self._stopped_log_upload_scheduled = False
        if _SD_JOB_STATE_FILE.exists():
            # A prior process may have exited while the controller continued
            # an SD print. Preserve this marker before opening the serial port.
            self.sd_job_active = True
            self.sd_job_disconnected = True
            try:
                saved_sd_state = json.loads(_SD_JOB_STATE_FILE.read_text())
                self._current_job_piece_count = max(0, int(saved_sd_state.get("piece_count", 0)))
                source_filename = saved_sd_state.get("source_filename")
                if source_filename:
                    machine_state.set_job_loaded(True, source_filename)
            except Exception as exc:
                logger.warning(f"Could not read saved SD job state: {exc}")
        if auto_connect:
            connected = self._connect_configured_port()
            if self.connected and self.sd_job_disconnected:
                self._start_sd_job_reconciliation()
            elif not connected and self.sd_job_disconnected:
                # A restarted Pi can inherit an active SD-job marker while
                # Marlin is still cutting. Enter the same automatic recovery
                # path used for a live serial disconnect.
                log_controller_event(
                    "serial_auto_reconnect_started",
                    reason="restored_sd_job_state",
                )
                self._start_reconnect_loop()
    
    def _connect_configured_port(
        self,
    ) -> bool:
        """Open only the configured controller UART and verify Marlin with M115."""
        try:
            self.last_serial_device = self.serial_device
            logger.info(
                "Opening Marlin serial port %s at %s baud",
                self.serial_device,
                self.baudrate,
            )
            # Set DTR low before opening the port. UART recovery must not pulse
            # the USB CDC host-ready line or reset the controller.
            self.serial_port = serial.Serial(
                port=None,
                baudrate=self.baudrate,
                timeout=2.0,
                write_timeout=2.0,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            self.serial_port.dtr = False
            self.serial_port.port = self.serial_device
            self.serial_port.open()
            if not self._probe_marlin_on_open_port(self.serial_device):
                self.serial_port.close()
                self.serial_port = None
                return False

            logger.info("Connected to Marlin on %s", self.serial_device)
            self.connected = True
            self.disconnect_started_at = None
            self.serial_uncertain = False
            self.reset_required = False
            self.last_controller_error = ""

            # Start background thread to read responses
            self.read_thread = threading.Thread(target=self._read_loop, daemon=True)
            self.read_thread.start()

            if not self.sd_job_disconnected and not self.firmware_update_active:
                # Enable position reporting and restore the normal
                # acceleration only when no SD print may be active.
                self._send_command("M114")
                self._send_command("M204 P2000 T3000")  # Print accel 2000, Travel accel 3000

            return True
        except (serial.SerialException, OSError) as e:
            logger.warning("Failed to connect to Marlin on %s: %s", self.serial_device, e)
            if self.serial_port and self.serial_port.is_open:
                self.serial_port.close()
            self.serial_port = None
            return False
        except Exception as e:
            logger.error("Error during connection to Marlin on %s: %s", self.serial_device, e)
            if self.serial_port and self.serial_port.is_open:
                self.serial_port.close()
            self.serial_port = None
            return False

    def _probe_marlin_on_open_port(
        self,
        port_device: str,
    ) -> bool:
        """Try to elicit a Marlin banner/version reply on an already-open serial port."""
        if not self.serial_port or not self.serial_port.is_open:
            return False

        # Flush stale bytes before probing. UART reconnect does not use DTR.
        try:
            self.serial_port.reset_input_buffer()
            self.serial_port.reset_output_buffer()
        except Exception:
            pass

        # Probe twice: immediate and delayed, to tolerate slow boot after E-stop.
        for idx, boot_wait in enumerate((1.0, 3.0), start=1):
            try:
                time.sleep(boot_wait)
                self._send_command("M115")
                response = self._read_response(timeout=4.0)
                if response and "FIRMWARE_NAME" in response:
                    capability = re.search(
                        r"Cap:EMERGENCY_PARSER:(\d+)", response, re.IGNORECASE
                    )
                    self.emergency_parser_supported = (
                        bool(int(capability.group(1))) if capability else None
                    )
                    log_controller_event(
                        "firmware_stop_capability",
                        emergency_parser=self.emergency_parser_supported,
                    )
                    if self.emergency_parser_supported is not True:
                        logger.warning(
                            "Firmware does not confirm EMERGENCY_PARSER support; "
                            "GUI Stop is not an emergency stop"
                        )
                    return True
                logger.warning(
                    f"Marlin probe attempt {idx} on {port_device} did not return firmware banner"
                )
            except Exception as e:
                logger.warning(f"Marlin probe attempt {idx} on {port_device} failed: {e}")
                try:
                    self.serial_port.close()
                except Exception:
                    pass
                return False

        return False

    def _handle_disconnect(self) -> None:
        """Handle an unexpected serial disconnection or controller power loss."""
        if not self.connected:
            return  # Already handled — avoid duplicate logging
        firmware_reboot = self.firmware_reboot_pending
        logger.error("Serial connection lost — controller disconnected or powered off")
        self.connected = False
        if self.disconnect_started_at is None:
            self.disconnect_started_at = time.monotonic()
        self.homed = False
        self.rehome_required = True
        if firmware_reboot:
            self.serial_uncertain = False
            self.stop_requested = False
            self.pause_requested = False
            self._set_firmware_update_status(
                "Reconnecting", 96.0, "Controller rebooted; waiting for Marlin to return."
            )
            log_controller_event("firmware_update_expected_disconnect")
            self._start_reconnect_loop()
            return

        self.serial_uncertain = True
        self.stop_requested = True  # Abort any running job
        self.ok_event.set()   # Unblock any flow-control waits in _execute_job
        machine_state.set_status("Disconnected", busy=False)
        log_controller_event("serial_disconnect")
        if self.sd_job_active:
            # An SD print may continue without the Pi's serial link. Do not offer
            # the serial-stream resume flow, which uses line acknowledgements.
            # Reopen the configured UART, then ask Marlin whether playback is
            # still active before deciding what the UI should report.
            self.sd_job_disconnected = True
            log_controller_event("sd_job_host_disconnected", filename=_SD_FILENAME)
            log_controller_event("serial_auto_reconnect_started", reason="sd_job_disconnect")
            self._start_reconnect_loop()
        elif not self.sd_uploading and not self.reset_required:
            self._save_resume_state()
        if _log_uploader:
            _log_uploader.log_system_snapshot(trigger="serial_disconnect")
            threading.Thread(
                target=_log_uploader.upload_now,
                args=(False, "disconnect"),
                daemon=True,
                name="log-upload-on-disconnect",
            ).start()
        # Non-SD disconnects stay operator-driven because command delivery may
        # have been interrupted mid-stream. SD playback has its own automatic
        # reconnect path above and is reconciled with M27 before job state is restored.

    def _start_reconnect_loop(self) -> None:
        """Retry the configured UART directly and reconcile any active SD job."""
        if self.reconnect_thread_active:
            return

        def _worker():
            self.reconnect_thread_active = True
            try:
                # Close the stale port first
                if self.serial_port:
                    try:
                        self.last_serial_device = self.last_serial_device or str(self.serial_port.port or "")
                    except Exception:
                        pass
                    try:
                        self.serial_port.close()
                    except Exception:
                        pass
                    self.serial_port = None

                attempt = 0
                failed_attempt_streak = 0
                while not self.connected:
                    attempt += 1
                    machine_state.set_status("Retrying connection...", busy=False)
                    logger.info(f"Reconnection attempt {attempt}...")
                    if self._connect_configured_port():
                        logger.info(f"Reconnected on attempt {attempt}")
                        self.stop_requested = False
                        log_controller_event("serial_reconnect", attempt=attempt)
                        if self.sd_job_disconnected:
                            self._reconcile_sd_job_after_reconnect()
                            if not self.connected:
                                # The serial link dropped again while M27 was
                                # reconciling. Keep this same retry loop alive.
                                failed_attempt_streak += 1
                                logger.warning(
                                    "Controller disconnected while reconciling SD job status"
                                )
                            else:
                                return
                        elif self.firmware_update_active:
                            self._set_firmware_update_status(
                                "Reconnecting", 98.0,
                                "Controller reconnected; verifying the firmware response."
                            )
                            return
                        else:
                            machine_state.set_status("Idle", busy=False)
                            return

                    else:
                        failed_attempt_streak += 1

                    if self.sd_job_disconnected:
                        # The UART device may stay present while the controller
                        # is powered off. Keep retrying so a possible SD print
                        # can be reconciled with M27 when Marlin returns.
                        failed_attempt_streak = 0
                        time.sleep(3.0)
                        continue

                    if self.firmware_update_active:
                        # M997 may take longer than an ordinary serial reset.
                        # The update transaction owns recovery until its own
                        # explicit verification deadline expires.
                        time.sleep(3.0)
                        continue

                    if failed_attempt_streak >= 5:
                        machine_state.set_status("Disconnected", busy=False)
                        log_controller_event(
                            "serial_reconnect_aborted",
                            attempts=attempt,
                            device=self.serial_device,
                        )
                        return

                    time.sleep(3.0)
            finally:
                self.reconnect_thread_active = False

        # Reserve ownership before starting the worker so rapid UI clicks cannot
        # create competing reconnect loops.
        self.reconnect_thread_active = True
        threading.Thread(target=_worker, daemon=True, name="serial-reconnect").start()

    def _start_sd_job_reconciliation(self) -> None:
        """Query Marlin in the background when startup restores an SD-job marker."""
        if self.reconnect_thread_active:
            return
        self.reconnect_thread_active = True

        def _worker():
            try:
                self._reconcile_sd_job_after_reconnect()
            finally:
                disconnected_during_check = not self.connected and self.sd_job_disconnected
                self.reconnect_thread_active = False
                if disconnected_during_check:
                    self._start_reconnect_loop()

        threading.Thread(
            target=_worker,
            daemon=True,
            name="sd-job-status-reconciliation",
        ).start()

    def attempt_reconnect(self) -> None:
        """Retry Marlin on the configured serial port."""
        if self.firmware_update_active:
            logger.warning("Manual reconnect rejected during firmware update")
            return
        if self.connected:
            return
        machine_state.set_status("Retrying connection...", busy=False)
        log_controller_event(
            "serial_reconnect_requested",
            device=self.serial_device,
        )
        self._start_reconnect_loop()

    def reset_controller_after_estop(self) -> bool:
        """Clear a Marlin halt state after the physical E-stop has been released."""
        generation = self.firmware_update_generation
        if self._firmware_operation_blocked(generation):
            return False
        with self.operation_lock:
            if self._firmware_operation_blocked(generation) or not self.connected:
                return False
            response = self._exchange_locked("M999", 5.0)
            if not _sd_response_ok(response):
                self.last_controller_error = response.strip() or "No response from M999"
                log_controller_event(
                    "controller_reset_failed",
                    response=self.last_controller_error[:500],
                )
                return False

            self.reset_required = False
            self.last_controller_error = ""
            self.stop_requested = False
            self.pause_requested = False
            self.homed = False
            self.rehome_required = True
            machine_state.reset_job()
            machine_state.set_status("Idle", busy=False)
            self._send_command("M114")
            log_controller_event("controller_reset", response=response[:500])
            return True

    def recover_uncertain_state(self) -> bool:
        """Re-establish serial ordering after the operator confirms motion stopped."""
        generation = self.firmware_update_generation
        if self._firmware_operation_blocked(generation):
            return False
        with self.operation_lock:
            if self._firmware_operation_blocked(generation):
                return False
            if not self.connected or self.reset_required or self.sd_uploading:
                return False
            if not self._quiesce_before_stream():
                return False
            if (
                self.sd_job_active
                or self.sd_job_disconnected
                or _SD_JOB_STATE_FILE.exists()
            ):
                sd_status = self._exchange_locked("M27", 8.0)
                if "not sd printing" not in sd_status.lower():
                    logger.error(
                        "Recovery refused because SD playback is not confirmed stopped: %s",
                        sd_status,
                    )
                    log_controller_event(
                        "uncertain_state_recovery_sd_not_idle",
                        response=sd_status[:500],
                    )
                    machine_state.set_status(
                        "SD stop unconfirmed — use physical E-stop", busy=True
                    )
                    return False
                self._clear_sd_job_state()
            self.serial_uncertain = False
            self.stop_requested = False
            self.pause_requested = False
            self.homed = False
            self.rehome_required = True
            machine_state.reset_job()
            machine_state.set_status("Idle — re-home required", busy=False)
            log_controller_event(
                "uncertain_state_recovered_rehome_required",
                sd_idle_confirmed=True,
            )
            return True

    # ==================== Resume After Disconnect ====================

    def has_resume_state(self) -> bool:
        """Check whether a resume-state file was saved after a disconnect."""
        return _RESUME_STATE_FILE.exists()

    def clear_resume_state(self) -> None:
        """Delete the resume-state file without resuming (user chose to discard)."""
        try:
            _RESUME_STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    def _get_safe_height(self, commands: list) -> float | None:
        """Find the tool travel (safe) height from the first G0 Z in the preamble."""
        for cmd in commands[:30]:
            upper = cmd.strip().upper()
            if upper.startswith('G0 Z') or upper.startswith('G0Z'):
                try:
                    z_str = upper.split('Z', 1)[1].split('F')[0].strip()
                    return float(z_str)
                except (ValueError, IndexError):
                    pass
        return None

    def _find_safe_resume_index(self, commands: list, last_acked: int) -> int:
        """
        Scan backward from last_acked to find the last G0 Z<safe_height> command.
        That is a 'tool up, between shapes' position — safe to rapid to and resume from.
        """
        safe_z = self._get_safe_height(commands)
        search_to = min(last_acked, len(commands) - 1)
        for i in range(search_to, -1, -1):
            upper = commands[i].strip().upper()
            if upper.startswith('G0 Z') or upper.startswith('G0Z'):
                if safe_z is not None:
                    try:
                        z_str = upper.split('Z', 1)[1].split('F')[0].strip()
                        z_val = float(z_str)
                        if abs(z_val - safe_z) < 0.5:
                            return i
                    except (ValueError, IndexError):
                        pass
                else:
                    return i  # No known safe height — take the first G0 Z found
        logger.warning("No safe G0 Z resume point found; defaulting to command 0")
        return 0

    def _extract_preamble(self, commands: list) -> list:
        """
        Return setup commands from the job header (before the first G0/G1 motion),
        excluding G28 homing lines (machine is already homed at resume time).
        """
        preamble = []
        for cmd in commands:
            upper = cmd.strip().upper()
            if upper.startswith('G0') or upper.startswith('G1'):
                break
            if not upper.startswith('G28'):
                preamble.append(cmd)
        return preamble

    def _find_last_xy_before(self, commands: list, idx: int) -> tuple[float | None, float | None]:
        """
        Scan backward from idx for the last command that specifies both X and Y.
        Returns (x, y) — the machine's position at the moment we want to resume.
        Needed because gcode at safe_idx assumes the previous cut's end position,
        but after a home + resume the machine is at (0, 0).
        """
        x_val: float | None = None
        y_val: float | None = None
        for i in range(min(idx, len(commands) - 1), -1, -1):
            upper = commands[i].strip().upper()
            if not (upper.startswith('G0') or upper.startswith('G1')):
                continue
            # Parse tokens like "X12.345", "Y-67.89"
            for tok in upper.split():
                if x_val is None and tok.startswith('X'):
                    try:
                        x_val = float(tok[1:])
                    except ValueError:
                        pass
                elif y_val is None and tok.startswith('Y'):
                    try:
                        y_val = float(tok[1:])
                    except ValueError:
                        pass
            if x_val is not None and y_val is not None:
                return x_val, y_val
        return x_val, y_val

    def _save_resume_state(self) -> None:
        """Persist enough information to resume the job after a reconnect + re-home."""
        if not self._current_job_gcode:
            logger.warning("_save_resume_state: skipped — no job gcode in memory (no job was running)")
            return  # No job was running
        # committed_line is the highest N Marlin confirmed (1-indexed; N1 == commands[0]),
        # so it is also the count of filtered commands successfully completed.
        with self.line_buffer_lock:
            last_acked = self.committed_line
        if last_acked == 0:
            logger.warning("_save_resume_state: skipped — committed_line is 0 (job had not started sending)")
            return  # Job hadn't meaningfully started
        safe_idx = self._find_safe_resume_index(self._current_job_gcode, last_acked)
        pct = last_acked / len(self._current_job_gcode) * 100
        state = {
            'filename': machine_state.snapshot().loaded_filename,
            'safe_resume_index': safe_idx,
            'last_acked_index': last_acked,
            'total_commands': len(self._current_job_gcode),
            'gcode': self._current_job_gcode,
        }
        try:
            _RESUME_STATE_FILE.write_text(json.dumps(state))
            logger.info(
                f"Resume state saved: safe_resume_index={safe_idx}, "
                f"last_acked={last_acked}/{len(self._current_job_gcode)} "
                f"({pct:.1f}% complete)"
            )
            log_controller_event(
                "resume_state_saved",
                filename=machine_state.snapshot().loaded_filename,
                safe_resume_index=safe_idx,
                last_acked_index=last_acked,
                total_commands=len(self._current_job_gcode),
                pct_complete=round(pct, 1),
            )
        except Exception as e:
            logger.error(f"Failed to save resume state: {e}")

    def resume_from_disconnect(self) -> bool:
        """
        Resume a job from the saved disconnect state.
        Machine must be homed (home_all) before calling this.
        """
        if not self.homed:
            logger.warning("resume_from_disconnect: machine not homed")
            return False
        if not _RESUME_STATE_FILE.exists():
            logger.warning("resume_from_disconnect: no saved state file")
            return False
        try:
            state = json.loads(_RESUME_STATE_FILE.read_text())
        except Exception as e:
            logger.error(f"Failed to load resume state: {e}")
            return False

        gcode = state.get('gcode', [])
        safe_idx = state.get('safe_resume_index', 0)
        if not gcode or safe_idx >= len(gcode):
            logger.error("Resume state is invalid or index out of range")
            return False

        preamble = self._extract_preamble(gcode)
        safe_z = self._get_safe_height(gcode)
        resume_x, resume_y = self._find_last_xy_before(gcode, safe_idx)
        # After homing, the machine is at (0, 0). The gcode at safe_idx assumes
        # the previous cut's end position. We must:
        #   1) lift to safe Z (above any material)
        #   2) rapid to the last known X/Y (still at safe Z)
        # before letting the resumed gcode plunge to cut depth.
        safety_lift = []
        if safe_z is not None:
            safety_lift.append(f"; --- resume safety lift to Z={safe_z} ---")
            safety_lift.append(f"G0 Z{safe_z} F1200")
        if resume_x is not None and resume_y is not None:
            safety_lift.append(
                f"; --- resume travel to last X/Y ({resume_x}, {resume_y}) at safe Z ---"
            )
            safety_lift.append(f"G0 X{resume_x} Y{resume_y} F10000")
        # Our injected lift replaces the G0 Z<safe> that lives at safe_idx,
        # so skip it to avoid a duplicate (harmless but noisy in logs).
        resume_start = safe_idx
        if safe_z is not None and resume_start < len(gcode):
            head = gcode[resume_start].strip().upper()
            if head.startswith('G0 Z') or head.startswith('G0Z'):
                try:
                    z_str = head.split('Z', 1)[1].split('F')[0].strip()
                    if abs(float(z_str) - safe_z) < 0.5:
                        resume_start += 1
                except (ValueError, IndexError):
                    pass
        resume_commands = preamble + safety_lift + gcode[resume_start:]
        logger.info(
            f"Resuming from index {safe_idx}/{len(gcode)} — "
            f"skipping {safe_idx} commands, {len(resume_commands)} remaining "
            f"(safe_z={safe_z}, resume_xy=({resume_x}, {resume_y}))"
        )

        # --- Debug: save the exact gcode we're about to stream, plus context ---
        try:
            from datetime import datetime as _dt
            debug_dir = Path(__file__).resolve().parent.parent / 'uploads' / 'gcode_output'
            debug_dir.mkdir(parents=True, exist_ok=True)
            ts = _dt.now().strftime('%Y%m%d_%H%M%S')
            ctx_start = max(0, safe_idx - 10)
            ctx_lines = [
                "; ============ RESUME DEBUG DUMP ============",
                f"; filename:           {state.get('filename')}",
                f"; total_commands:     {len(gcode)}",
                f"; last_acked_index:   {state.get('last_acked_index')}",
                f"; safe_resume_index:  {safe_idx}",
                f"; safe_z:             {safe_z}",
                f"; resume_xy:          ({resume_x}, {resume_y})",
                f"; preamble_lines:     {len(preamble)}",
                f"; safety_lift_lines:  {len(safety_lift)}",
                f"; resume_total_lines: {len(resume_commands)}",
                "",
                f"; ---- Context: original gcode[{ctx_start}:{safe_idx + 5}] ----",
            ]
            for i in range(ctx_start, min(safe_idx + 5, len(gcode))):
                marker = "  >>"  if i == safe_idx else "    "
                ctx_lines.append(f"; {i:6d} {marker} {gcode[i]}")
            ctx_lines += [
                "",
                "; ============ ACTUAL STREAM TO MARLIN ============",
                "",
            ]
            debug_path = debug_dir / f"resume_{ts}.gcode"
            debug_path.write_text("\n".join(ctx_lines + resume_commands) + "\n")
            logger.info(f"Resume debug dump saved: {debug_path}")
        except Exception as e:
            logger.warning(f"Failed to write resume debug dump: {e}")
        log_controller_event(
            "job_resume_disconnect",
            filename=state.get('filename'),
            safe_resume_index=safe_idx,
            last_acked_index=state.get('last_acked_index'),
            total=len(gcode),
        )

        # Clear saved state so it isn't offered again after a clean finish
        try:
            _RESUME_STATE_FILE.unlink()
        except Exception:
            pass

        self.stop_requested = False
        self.pause_requested = False
        self._current_job_gcode = resume_commands
        self.job_thread = threading.Thread(
            target=self._execute_job,
            args=(resume_commands,),
            daemon=True,
            name="job-resume",
        )
        self.job_thread.start()
        return True

    def _send_command(self, command: str) -> bool:
        """Send a G-code command to Marlin."""
        if not self.serial_port or not self.serial_port.is_open:
            logger.error("Serial port not connected")
            return False
        try:
            cmd = command.strip() + "\n"
            wire_bytes = cmd.encode('utf-8')
            with self.serial_write_lock:
                self.serial_port.write(wire_bytes)
                self.serial_port.flush()  # Ensure data is sent immediately
            log_serial_tx(
                command,
                streaming=self.streaming_mode,
                wire=cmd.rstrip("\n"),
                byte_count=len(wire_bytes),
                job_id=self.current_job_id or None,
            )
            return True
        except (serial.SerialException, OSError) as e:
            logger.error(f"Error sending command '{command}': {e}")
            log_serial_tx(command, error=str(e), streaming=self.streaming_mode)
            self._handle_disconnect()
            return False
        except Exception as e:
            logger.error(f"Error sending command '{command}': {e}")
            log_serial_tx(command, error=str(e), streaming=self.streaming_mode)
            return False

    def _send_priority_stop(self, *, sd_job: bool) -> bool:
        """Put stop commands on the wire without waiting for operation_lock.

        Stop is the one operation allowed to bypass the normal response-ordering
        lock. serial_write_lock still guarantees complete, non-interleaved lines.
        Responses are reconciled later through the normal locked path.
        """
        if not self.serial_port or not self.serial_port.is_open:
            return False
        commands = ("M524", "M410") if sd_job else ("M410",)
        try:
            with self.serial_write_lock:
                for command in commands:
                    wire = (command + "\n").encode("ascii")
                    self.serial_port.write(wire)
                    log_serial_tx(
                        command, streaming=False, priority_stop=True,
                        wire=command, byte_count=len(wire),
                        job_id=self.current_job_id or None,
                    )
                self.serial_port.flush()
            return True
        except (serial.SerialException, OSError) as exc:
            logger.error("Priority stop write failed: %s", exc)
            log_controller_event("priority_stop_write_failed", error=str(exc))
            self._handle_disconnect()
            return False
        except Exception as exc:
            logger.error("Priority stop write failed: %s", exc)
            log_controller_event("priority_stop_write_failed", error=str(exc))
            return False

    def _pause_reader(self, timeout: float = 3.0) -> bool:
        """Hand synchronous response ownership to the current thread."""
        self.read_loop_paused = True
        if not self.read_thread or not self.read_thread.is_alive():
            return True
        if self.reader_paused.wait(timeout=timeout):
            return True
        logger.error("Background serial reader did not pause")
        self.read_loop_paused = False
        return False

    def _resume_reader(self) -> None:
        self.read_loop_paused = False
        self.reader_paused.clear()
    
    def firmware_update_snapshot(self) -> dict[str, object]:
        """Return a thread-safe view of the current or most recent update."""
        with self._firmware_status_lock:
            return dict(self._firmware_update_status)

    def _set_firmware_update_status(
        self, stage: str, progress: float, message: str,
        *, filename: Optional[str] = None, size: Optional[int] = None,
        sha256: Optional[str] = None,
    ) -> None:
        with self._firmware_status_lock:
            status = self._firmware_update_status
            status.update(
                active=self.firmware_update_active,
                stage=stage,
                progress=max(0.0, min(100.0, float(progress))),
                message=message,
            )
            if filename is not None:
                status["filename"] = filename
            if size is not None:
                status["size"] = size
            if sha256 is not None:
                status["sha256"] = sha256
        if self.firmware_update_active:
            machine_state.set_status(f"Firmware update: {stage}", busy=True, paused=False)

    def _firmware_operation_blocked(self, generation: Optional[int] = None) -> bool:
        return self.firmware_update_active or (
            generation is not None and generation != self.firmware_update_generation
        )

    def _write_binary_payload(self, payload: bytes) -> bool:
        """Write one BFT frame without passing binary bytes through G-code paths."""
        if not self.serial_port or not self.serial_port.is_open:
            return False
        try:
            with self.serial_write_lock:
                written = self.serial_port.write(payload)
                self.serial_port.flush()
            if written != len(payload):
                self._mark_serial_uncertain(
                    "Partial BFT packet write", f"Wrote {written} of {len(payload)} bytes"
                )
                return False
            return True
        except (serial.SerialException, OSError) as exc:
            logger.error("BFT serial write failed: %s", exc)
            self._handle_disconnect()
            return False
        except Exception as exc:
            logger.exception("BFT serial write failed")
            self._mark_serial_uncertain("BFT serial write failed", str(exc))
            return False

    def _read_bft_sync_response(self, timeout: float = 8.0) -> str:
        deadline = time.monotonic() + timeout
        lines: list[str] = []
        while time.monotonic() < deadline:
            if not self.serial_port or not self.serial_port.is_open:
                break
            try:
                if self.serial_port.in_waiting:
                    line = self.serial_port.readline().decode("ascii", errors="replace").strip()
                    if not line:
                        continue
                    lines.append(line)
                    log_serial_rx(line, mode="bft_sync")
                    if line.lower().startswith(("ss", "fe")):
                        break
                else:
                    time.sleep(0.005)
            except (serial.SerialException, OSError) as exc:
                self._handle_disconnect()
                lines.append(f"serial read failed: {exc}")
                break
        return "\n".join(lines)

    def _read_bft_packet_response(
        self, sync: int, *, timeout: float = 8.0, require_pft: bool = False,
    ) -> tuple[tuple[str, int] | None, list[str]]:
        deadline = time.monotonic() + timeout
        lines: list[str] = []
        ack: tuple[str, int] | None = None
        last_input_at = time.monotonic()
        while time.monotonic() < deadline:
            if not self.serial_port or not self.serial_port.is_open:
                break
            try:
                if self.serial_port.in_waiting:
                    line = self.serial_port.readline().decode("ascii", errors="replace").strip()
                    if not line:
                        continue
                    lines.append(line)
                    last_input_at = time.monotonic()
                    log_serial_rx(line, mode="bft_packet")
                    parsed = parse_ack(line)
                    if parsed:
                        kind, number = parsed
                        if kind in {"rs", "fe"}:
                            return parsed, lines
                        if number == sync:
                            ack = parsed
                    if ack and require_pft and any(item.startswith("PFT:") for item in lines):
                        return ack, lines
                    if ack and not require_pft and time.monotonic() - last_input_at >= 0.05:
                        return ack, lines
                else:
                    if ack and not require_pft and time.monotonic() - last_input_at >= 0.05:
                        return ack, lines
                    time.sleep(0.005)
            except (serial.SerialException, OSError) as exc:
                self._handle_disconnect()
                lines.append(f"serial read failed: {exc}")
                break
        return ack, lines

    def _send_bft_packet(
        self, sync: int, protocol: int, packet_type: int, payload: bytes = b"", *,
        require_pft: bool = False, timeout: float = 8.0,
    ) -> list[str]:
        packet = encode_packet(sync, protocol, packet_type, payload)
        for attempt in range(_BFT_PACKET_RETRIES + 1):
            if not self._write_binary_payload(packet):
                raise BFTProtocolError("Could not write the complete BFT packet")
            log_serial_tx(
                "BFT packet", mode="binary", sync=sync, protocol=protocol,
                packet_type=packet_type, packet_bytes=len(packet),
                packet_sha256=hashlib.sha256(packet).hexdigest(),
                retransmission=attempt > 0,
            )
            ack, lines = self._read_bft_packet_response(
                sync, timeout=timeout, require_pft=require_pft
            )
            if ack and ack[0] == "rs":
                if ack[1] != sync:
                    self._mark_serial_uncertain("Unexpected BFT resend sequence", "\n".join(lines))
                    raise BFTProtocolError("Marlin requested a different BFT sequence")
                if attempt >= _BFT_PACKET_RETRIES:
                    raise BFTProtocolError("BFT packet exceeded the retransmission limit")
                continue
            if ack and ack[0] == "fe":
                self._mark_serial_uncertain("Marlin rejected a BFT packet", "\n".join(lines))
                raise BFTProtocolError("Marlin reset the BFT connection after a packet error")
            if ack != ("ok", sync):
                self._mark_serial_uncertain(
                    "BFT packet acknowledgement was missing or ambiguous", "\n".join(lines)
                )
                raise BFTProtocolError("BFT packet acknowledgement was not confirmed")
            if require_pft and not any(item.startswith("PFT:") for item in lines):
                self._mark_serial_uncertain("BFT operation response was missing", "\n".join(lines))
                raise BFTProtocolError("Marlin did not confirm the BFT operation")
            return lines
        raise BFTProtocolError("BFT packet retry loop ended unexpectedly")

    def install_firmware(
        self,
        path: str | os.PathLike[str],
        progress_callback: Optional[Callable[[str, float, str], None]] = None,
    ) -> tuple[bool, str]:
        """Upload firmware.bin over BFT, request one reboot, then verify Marlin."""
        image_path = Path(path)
        try:
            if image_path.suffix.lower() != ".bin" or not image_path.is_file():
                return False, "Select a readable .bin firmware file."
            image = image_path.read_bytes()
        except OSError as exc:
            return False, f"Could not read firmware file: {exc}"
        if not image or len(image) > _MAX_FIRMWARE_SIZE:
            return False, "Firmware must be between 1 byte and 1 MiB."
        firmware_name_match = re.search(rb"FIRMWARE_NAME:([^\x00\r\n]+)", image)
        if not firmware_name_match:
            return False, "Firmware image does not contain a Marlin firmware identity."
        expected_firmware_name = firmware_name_match.group(1).decode("ascii", errors="replace")
        expected_firmware_name = expected_firmware_name.split(" SOURCE_CODE_URL:", 1)[0].strip()
        if not expected_firmware_name:
            return False, "Firmware image does not contain a usable Marlin firmware identity."

        image_sha256 = hashlib.sha256(image).hexdigest()
        image_name = image_path.name
        initial_state = machine_state.snapshot()
        generation = self.firmware_update_generation

        with self.operation_lock:
            if self._firmware_operation_blocked(generation):
                return False, "Another firmware update owns the controller."
            job_thread_active = bool(self.job_thread and self.job_thread.is_alive())
            if (
                not self.connected
                or not self.serial_port
                or not self.serial_port.is_open
                or not machine_state.is_idle()
                or job_thread_active
                or self.sd_uploading
                or self.sd_job_active
                or self.sd_job_disconnected
                or self.streaming_mode
                or self.pause_requested
                or self.stop_requested
                or self.serial_uncertain
                or self.reset_required
                or self.reconnect_thread_active
            ):
                return False, "Firmware update requires a connected, known, idle controller with no active job or recovery."

            with self.firmware_update_gate:
                if not machine_state.is_idle() or self.stop_requested:
                    return False, "Firmware update requires a connected, known, idle controller with no active job or recovery."
                self.firmware_update_generation += 1
                self.firmware_update_active = True
                self.firmware_reboot_pending = False
                self._set_firmware_update_status(
                    "Preparing", 1.0, "Checking firmware capabilities and SD storage.",
                    filename=image_name, size=len(image), sha256=image_sha256,
                )
            log_controller_event(
                "firmware_update_start", filename=image_name,
                target=_FIRMWARE_FILENAME, size=len(image), sha256=image_sha256,
            )

            binary_mode_started = False
            transfer_open_attempted = False
            reboot_requested = False
            success = False
            current_sync: Optional[int] = None
            failure_message = ""

            def report(stage: str, progress: float, message: str) -> None:
                self._set_firmware_update_status(stage, progress, message)
                if progress_callback:
                    try:
                        progress_callback(stage, progress, message)
                    except Exception:
                        logger.exception("Firmware update progress callback failed")

            try:
                report("Preparing", 2.0, "Querying Marlin firmware capabilities.")
                response = self._exchange_locked("M115", 10.0)
                required_caps = (
                    "Cap:BINARY_FILE_TRANSFER:1",
                    "Cap:SDCARD:1",
                    "Cap:SD_WRITE:1",
                )
                missing_caps = [cap for cap in required_caps if cap.lower() not in response.lower()]
                if not _sd_response_ok(response) or missing_caps:
                    detail = ", ".join(missing_caps) if missing_caps else response or "No response"
                    raise BFTProtocolError(f"Required Marlin firmware upload capabilities are missing: {detail}")

                response = self._exchange_locked("M21", 10.0)
                if not _sd_response_ok(response):
                    raise BFTProtocolError(f"Could not mount the onboard SD card: {response or 'No response'}")

                response = self._exchange_locked("M28 B1", 10.0)
                if not _sd_response_ok(response) or "switching to binary protocol" not in response.lower():
                    if not any(
                        line.strip().lower().startswith("error")
                        or "unknown command" in line.lower()
                        for line in response.splitlines()
                    ):
                        self._mark_serial_uncertain(
                            "M28 B1 response was missing or ambiguous", response
                        )
                    raise BFTProtocolError(
                        f"Marlin did not enter Binary File Transfer mode: {response or 'No response'}"
                    )
                binary_mode_started = True
                report("Uploading", 8.0, f"Opening {_FIRMWARE_FILENAME} on the controller SD card.")

                with self.read_lock:
                    if not self._pause_reader(timeout=5.0):
                        self._mark_serial_uncertain("Could not acquire serial ownership for firmware transfer")
                        raise BFTProtocolError("Could not pause the normal serial reader")
                    try:
                        sync_packet = encode_packet(0, CONTROL_PROTOCOL, CONTROL_SYNC)
                        if not self._write_binary_payload(sync_packet):
                            raise BFTProtocolError("Could not send the BFT synchronization packet")
                        log_serial_tx(
                            "BFT sync", mode="binary", packet_bytes=len(sync_packet),
                            packet_sha256=hashlib.sha256(sync_packet).hexdigest(),
                        )
                        sync_response = self._read_bft_sync_response()
                        sync_line = next(
                            (line for line in sync_response.splitlines() if line.lower().startswith("ss")),
                            "",
                        )
                        if not sync_line:
                            self._mark_serial_uncertain("BFT synchronization response was missing", sync_response)
                            raise BFTProtocolError("Marlin did not complete BFT synchronization")
                        sync_info = parse_sync_response(sync_line)
                        if sync_info.version != PROTOCOL_VERSION:
                            raise BFTProtocolError(f"Unsupported BFT protocol version: {sync_info.version}")
                        if sync_info.buffer_size < 1:
                            raise BFTProtocolError("Marlin reported an invalid BFT buffer size")
                        current_sync = sync_info.sync

                        query_lines = self._send_bft_packet(
                            current_sync, FILE_TRANSFER_PROTOCOL, TRANSFER_QUERY, require_pft=True
                        )
                        current_sync = (current_sync + 1) & 0xFF
                        query_response = next(
                            (line for line in query_lines if line.startswith("PFT:version:")), ""
                        )
                        if not query_response or ":compression:" not in query_response:
                            raise BFTProtocolError("Marlin returned an invalid BFT query response")

                        open_payload = b"\x00\x00" + _FIRMWARE_FILENAME.encode("ascii") + b"\x00"
                        transfer_open_attempted = True
                        open_lines = self._send_bft_packet(
                            current_sync, FILE_TRANSFER_PROTOCOL, TRANSFER_OPEN,
                            open_payload, require_pft=True,
                        )
                        current_sync = (current_sync + 1) & 0xFF
                        if "PFT:success" not in open_lines:
                            raise BFTProtocolError(
                                f"Marlin could not open {_FIRMWARE_FILENAME}: {'; '.join(open_lines)}"
                            )

                        chunk_size = min(sync_info.buffer_size, 96)
                        if chunk_size < 1:
                            raise BFTProtocolError("Marlin reported an unusable BFT payload size")
                        for offset in range(0, len(image), chunk_size):
                            chunk = image[offset : offset + chunk_size]
                            write_lines = self._send_bft_packet(
                                current_sync, FILE_TRANSFER_PROTOCOL, TRANSFER_WRITE, chunk
                            )
                            current_sync = (current_sync + 1) & 0xFF
                            if any(line == "PFT:ioerror" for line in write_lines):
                                raise BFTProtocolError("Marlin reported an SD write error")
                            progress = 8.0 + (offset + len(chunk)) / len(image) * 78.0
                            report(
                                "Uploading", progress,
                                f"Transferred {min(offset + len(chunk), len(image)):,} of {len(image):,} bytes.",
                            )

                        report("Verifying upload", 88.0, "Closing and checking the firmware file.")
                        close_lines = self._send_bft_packet(
                            current_sync, FILE_TRANSFER_PROTOCOL, TRANSFER_CLOSE, require_pft=True
                        )
                        current_sync = (current_sync + 1) & 0xFF
                        if "PFT:success" not in close_lines:
                            raise BFTProtocolError(
                                f"Marlin did not confirm closing {_FIRMWARE_FILENAME}: {'; '.join(close_lines)}"
                            )
                        close_session_lines = self._send_bft_packet(
                            current_sync, CONTROL_PROTOCOL, CONTROL_CLOSE
                        )
                        if not any(parse_ack(line) == ("ok", current_sync) for line in close_session_lines):
                            self._mark_serial_uncertain(
                                "BFT session close acknowledgement was missing",
                                "\n".join(close_session_lines),
                            )
                            raise BFTProtocolError("Marlin did not confirm exiting BFT mode")
                        current_sync = (current_sync + 1) & 0xFF
                        binary_mode_started = False
                    except Exception:
                        if (
                            current_sync is not None
                            and transfer_open_attempted
                            and self.serial_port and self.serial_port.is_open
                            and not self.serial_uncertain
                        ):
                            try:
                                abort_lines = self._send_bft_packet(
                                    current_sync, FILE_TRANSFER_PROTOCOL, TRANSFER_ABORT,
                                    require_pft=True, timeout=3.0,
                                )
                                current_sync = (current_sync + 1) & 0xFF
                                if "PFT:success" in abort_lines:
                                    self._send_bft_packet(
                                        current_sync, CONTROL_PROTOCOL, CONTROL_CLOSE, timeout=3.0
                                    )
                                    binary_mode_started = False
                            except Exception:
                                logger.exception("Could not cleanly abort the BFT transfer")
                        elif (
                            current_sync is not None and binary_mode_started
                            and self.serial_port and self.serial_port.is_open
                            and not self.serial_uncertain
                        ):
                            try:
                                self._send_bft_packet(
                                    current_sync, CONTROL_PROTOCOL, CONTROL_CLOSE, timeout=3.0
                                )
                                binary_mode_started = False
                            except Exception:
                                logger.exception("Could not close the BFT session")
                        raise
                    finally:
                        self._resume_reader()

                report("Verifying upload", 92.0, f"Checking the onboard SD listing for {_FIRMWARE_FILENAME}.")
                listing = self._exchange_locked("M20 F", 15.0)
                if (
                    not _sd_response_ok(listing)
                    or re.search(r"(?<![A-Za-z0-9_.-])firmware\.bin(?![A-Za-z0-9_.-])", listing, re.I) is None
                ):
                    raise BFTProtocolError(
                        f"Marlin did not list the completed {_FIRMWARE_FILENAME}: {listing or 'No response'}"
                    )

                report("Rebooting controller", 94.0, "Upload verified; requesting the firmware reboot once.")
                self.firmware_reboot_pending = True
                if not self._send_command("M997"):
                    self.firmware_reboot_pending = False
                    raise BFTProtocolError("M997 could not be written; firmware state is unknown")
                reboot_requested = True
                # M997 restarts STM32 without a useful serial acknowledgement.
                # Route the expected reset through the existing reconnect path.
                if self.connected:
                    self._handle_disconnect()
                report("Reconnecting", 96.0, "Waiting for Marlin to restart and answer M115.")

                deadline = time.monotonic() + self.firmware_reconnect_timeout
                while time.monotonic() < deadline and not self.connected:
                    time.sleep(0.1)
                if not self.connected or not self.serial_port or not self.serial_port.is_open:
                    failure_message = "Controller did not reconnect after M997; firmware state is unknown."
                    self._mark_serial_uncertain("Firmware update reconnect timed out", failure_message)
                    raise BFTProtocolError(failure_message)

                report("Reconnecting", 99.0, "Controller is back; verifying Marlin capabilities.")
                response = self._exchange_locked("M115", 12.0)
                if (
                    not _sd_response_ok(response)
                    or "FIRMWARE_NAME" not in response
                    or f"FIRMWARE_NAME:{expected_firmware_name}" not in response
                    or any(cap.lower() not in response.lower() for cap in required_caps)
                ):
                    self._mark_serial_uncertain(
                        "Firmware reboot did not report the uploaded Marlin identity and capabilities",
                        response,
                    )
                    raise BFTProtocolError(
                        "Controller reconnected, but the uploaded Marlin identity and required capabilities were not verified."
                    )

                self.firmware_reboot_pending = False
                self.serial_uncertain = False
                self.reset_required = False
                self.homed = False
                self.rehome_required = True
                self._set_firmware_update_status(
                    "Success", 100.0,
                    "Marlin restarted and reported the required capabilities; home before motion.",
                )
                machine_state.set_status("Firmware updated — re-home required", busy=False)
                log_controller_event(
                    "firmware_update_success", filename=image_name,
                    target=_FIRMWARE_FILENAME, size=len(image), sha256=image_sha256,
                    firmware_response=response[:500],
                )
                success = True
                return True, "Firmware update completed and Marlin was verified. Re-home before motion."

            except Exception as exc:
                failure_message = failure_message or str(exc)
                logger.exception("Firmware update failed")
                if reboot_requested:
                    self.firmware_reboot_pending = False
                    self.serial_uncertain = True
                    self.homed = False
                    self.rehome_required = True
                    machine_state.set_status("Firmware update failed — verify controller", busy=True)
                elif not self.serial_uncertain and self.connected:
                    machine_state.set_status(
                        initial_state.status_text, busy=initial_state.busy,
                        paused=initial_state.paused,
                    )
                self._set_firmware_update_status(
                    "Failed", self.firmware_update_snapshot()["progress"], failure_message
                )
                log_controller_event(
                    "firmware_update_failed", filename=image_name,
                    target=_FIRMWARE_FILENAME, size=len(image), sha256=image_sha256,
                    after_m997=reboot_requested, serial_uncertain=self.serial_uncertain,
                    error=failure_message[:500],
                )
                return False, failure_message
            finally:
                self.firmware_reboot_pending = False
                self.firmware_update_active = False
                with self._firmware_status_lock:
                    self._firmware_update_status["active"] = False
                if success:
                    machine_state.set_status("Firmware updated — re-home required", busy=False)

    def send_command(self, command: str) -> bool:
        """Send one operator command only while the controller is idle."""
        generation = self.firmware_update_generation
        if self._firmware_operation_blocked(generation):
            return False
        with self.operation_lock:
            if self._firmware_operation_blocked(generation):
                return False
            if (not self.connected or not machine_state.is_idle() or self.serial_uncertain
                    or self.rehome_required):
                logger.warning("Rejected direct command while controller is not safely idle")
                return False
            return self._send_command(command)

    # ==================== Line-numbered streaming (Marlin Resend protocol) ====================

    @staticmethod
    def _wrap_with_line_number(line_number: int, cmd: str) -> str:
        """Build a checksummed line: 'N<n> <cmd>*<cs>' per Marlin's protocol."""
        payload = f"N{line_number} {cmd}"
        return f"{payload}*{_marlin_checksum(payload)}"

    def _write_serial_payload(self, payload: str) -> bool:
        """Write a fully-formed line (including any N/checksum) to the serial port."""
        if not self.serial_port or not self.serial_port.is_open:
            logger.error("Serial port not connected")
            return False
        try:
            with self.serial_write_lock:
                self.serial_port.write((payload + "\n").encode('utf-8'))
                self.serial_port.flush()
            return True
        except (serial.SerialException, OSError) as e:
            logger.error(f"Error writing serial payload '{payload}': {e}")
            self._handle_disconnect()
            return False
        except Exception as e:
            logger.error(f"Error writing serial payload '{payload}': {e}")
            return False

    def _emit_new_streamed(self, cmd: str) -> Optional[int]:
        """Assign the next line number to `cmd`, buffer it for possible resend,
        and write the wrapped form to the serial port."""
        with self.line_buffer_lock:
            n = self.next_line_number
            self.next_line_number += 1
            self.line_buffer[n] = cmd
            if len(self.line_buffer) > self._max_line_buffer:
                # Only drop entries Marlin has already confirmed — anything at
                # or above committed_line might still be requested for resend.
                droppable = sorted(k for k in self.line_buffer if k <= self.committed_line)
                while len(self.line_buffer) > self._max_line_buffer and droppable:
                    del self.line_buffer[droppable.pop(0)]
        payload = self._wrap_with_line_number(n, cmd)
        ok = self._write_serial_payload(payload)
        log_serial_tx(cmd, streaming=True, line_number=n, wire=payload)
        if not ok:
            log_serial_tx(cmd, streaming=True, line_number=n, wire=payload, error="write_failed")
            return None
        return n

    def _resend_line(self, n: int) -> bool:
        """Re-emit a previously-buffered line under its original line number."""
        with self.line_buffer_lock:
            cmd = self.line_buffer.get(n)
        if cmd is None:
            logger.error(
                f"Resend requested for N{n} but command is no longer in buffer "
                f"(committed_line={self.committed_line}, next_line={self.next_line_number}). "
                f"Pausing job."
            )
            log_controller_event("resend_buffer_miss", line_number=n,
                                 committed_line=self.committed_line)
            self.pause_requested = True
            return False
        payload = self._wrap_with_line_number(n, cmd)
        ok = self._write_serial_payload(payload)
        self.resend_total += 1
        logger.warning(f"Resending N{n} (total resends this job: {self.resend_total})")
        log_serial_tx(cmd, streaming=True, line_number=n, wire=payload, resend=True)
        return ok

    def _drain_resends(self) -> int:
        """Re-emit every line from the lowest requested N up to the latest
        assigned line number. Loops until no new resend request appears
        mid-drain. Returns the number of lines re-emitted."""
        emitted = 0
        guard = 0
        while True:
            with self.line_buffer_lock:
                start = self.resend_requested
                self.resend_requested = None
                end = self.next_line_number  # exclusive
            if start is None:
                return emitted
            if start >= end:
                logger.warning(
                    f"Resend N{start} requested but only N<{end} have been sent; ignoring."
                )
                return emitted
            for n in range(start, end):
                if self.stop_requested:
                    return emitted
                if not self._resend_line(n):
                    return emitted
                emitted += 1
                # Mid-resend, throttle so we don't outrun Marlin's serial buffer.
                while not self.stop_requested:
                    with self.ok_lock:
                        in_flight = (self.next_line_number - 1 + self.resend_total) - self.ok_count
                    if in_flight < 6:
                        break
                    self.ok_event.clear()
                    self.ok_event.wait(timeout=0.05)
            guard += 1
            if guard > 8:
                logger.error("Resend drain ran 8 times without converging — pausing job")
                log_controller_event("resend_drain_livelock")
                self.pause_requested = True
                return emitted
    
    def _read_response(self, timeout: float = 1.0, poll_interval: float = 0.01) -> str:
        """Read response from Marlin until 'ok' is received."""
        if not self.serial_port or not self.serial_port.is_open:
            return ""
        
        response_lines = []
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                if self.serial_port.in_waiting:
                    line = self.serial_port.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        log_serial_rx(line, mode="sync")
                    response_lines.append(line)
                    
                    if line.startswith("ok"):
                        break
                else:
                    time.sleep(poll_interval)
            except (serial.SerialException, OSError) as e:
                logger.error(f"Error reading response: {e}")
                self._handle_disconnect()
                break
            except Exception as e:
                logger.error(f"Error reading response: {e}")
                break
        
        return "\n".join(response_lines)
    
    def _read_loop(self):
        """Background thread to continuously read from serial port."""
        while self.connected and self.serial_port and self.serial_port.is_open:
            try:
                # Pause if manual command is being sent
                if self.read_loop_paused:
                    self.reader_paused.set()
                    time.sleep(0.01)
                    continue
                self.reader_paused.clear()
                
                if self.serial_port.in_waiting:
                    line = self.serial_port.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        log_serial_rx(line, mode="async")

                        # --- Line-number protocol bookkeeping (streaming only) ---
                        credit_stream_ok = True
                        if self.streaming_mode:
                            credit_stream_ok = self._handle_streaming_response(line)

                        # Track 'ok' responses for streaming flow control
                        if line.lower().startswith("ok") and credit_stream_ok:
                            with self.ok_lock:
                                self.ok_count += 1
                            self.ok_event.set()  # Signal that ok was received

                        # Parse position updates (M114 response)
                        if line.startswith("X:"):
                            logger.info(f"PARSING POSITION: {line}")
                            self._parse_position(line)

                        # Log errors
                        if 'error' in line.lower() or 'err:' in line.lower():
                            logger.warning(f"Controller error: {line}")
                        lower = line.lower()
                        if (
                            'printer halted' in lower
                            or 'stopped due to errors' in lower
                            or 'kill() called' in lower
                            or 'kill caused by' in lower
                        ):
                            had_sd_job = (
                                self.sd_job_active
                                or self.sd_job_disconnected
                                or _SD_JOB_STATE_FILE.exists()
                            )
                            if not self.reset_required:
                                log_controller_event("controller_reset_required", response=line[:500])
                            self.reset_required = True
                            self.last_controller_error = line
                            self.homed = False
                            self.stop_requested = True
                            self.pause_requested = False
                            self.ok_event.set()
                            machine_state.reset_job()
                            machine_state.set_status("E-Stop Reset Required", busy=False)
                            if had_sd_job:
                                self._clear_sd_job_state()
                                log_controller_event(
                                    "sd_job_cleared_after_controller_kill",
                                    filename=_SD_FILENAME,
                                    response=line[:500],
                                )
                        
                time.sleep(0.005)  # 5ms polling rate
            except OSError as e:
                if e.errno == 5:  # EIO — serial device disconnected (e.g. SKR board power loss)
                    logger.error(f"Serial device disconnected (EIO) — likely controller power loss: {e}")
                    self._handle_disconnect()
                    break
                logger.error(f"Error in read loop: {e}")
                time.sleep(0.1)
            except serial.SerialException as e:
                logger.error(f"Serial exception in read loop — connection lost: {e}")
                self._handle_disconnect()
                break
    
    def _handle_streaming_response(self, line: str) -> bool:
        """Update line-number / resend bookkeeping from a single Marlin response.

        Called from the read loop while a job is streaming. Maintains
        `committed_line` (highest N Marlin has confirmed), `resend_requested`
        (lowest N Marlin still needs), and `error_burst_pending` (set while
        an error/resend burst is awaiting its single trailing `ok`)."""
        try:
            # Marlin emits one `ok` per processed line *and* one `ok` after
            # each error/resend burst. We absorb that single trailing `ok`
            # via the `error_burst_pending` latch so it doesn't advance
            # forward progress.
            resend_m = _RESEND_RE.search(line)
            last_line_m = _LAST_LINE_RE.search(line)
            lower = line.lower()
            is_error = lower.startswith("error") or "err:" in lower

            if resend_m:
                n = int(resend_m.group(1))
                with self.line_buffer_lock:
                    if self.resend_requested is None or n < self.resend_requested:
                        self.resend_requested = n
                    if n - 1 > self.committed_line:
                        self.committed_line = n - 1
                    self.error_burst_pending = True
                logger.warning(f"Marlin requested resend from N{n}: {line}")
                log_controller_event("resend_requested", line_number=n,
                                     committed_line=self.committed_line,
                                     next_line=self.next_line_number)
                return False

            if is_error and last_line_m:
                # Error reports its last good line. The matching Resend:
                # (if any) arrives on the next line and shares the same
                # trailing `ok` — so the latch is idempotent here.
                n = int(last_line_m.group(1))
                with self.line_buffer_lock:
                    if n > self.committed_line:
                        self.committed_line = n
                    self.error_burst_pending = True
                logger.warning(f"Marlin error during stream: {line}")
                return False

            if line.startswith("ok") or line.startswith("OK"):
                m = _OK_N_RE.match(line)
                with self.line_buffer_lock:
                    if m:
                        n = int(m.group(1))
                        if n > self.committed_line:
                            self.committed_line = n
                        # An explicit `ok N<n>` also ends any pending burst.
                        self.error_burst_pending = False
                    elif self.error_burst_pending:
                        # This `ok` is the trailing ack of the error/resend burst.
                        self.error_burst_pending = False
                        return False
                    else:
                        # Bare `ok` — by FIFO it acks the next uncommitted line.
                        if self.committed_line < self.next_line_number - 1:
                            self.committed_line += 1
                return True
            return False
        except Exception as e:
            # Bookkeeping must never break the read loop.
            logger.error(f"Error in _handle_streaming_response for {line!r}: {e}",
                         exc_info=True)
            return False

    def _parse_position(self, line: str):
        """Parse Marlin position response: X:0.00 Y:0.00 Z:0.00 A:0.00"""
        try:
            # Stop parsing at "Count" - we only want position values, not stepper counts
            if 'Count' in line:
                line = line.split('Count')[0]
            
            parts = line.split()
            pos = {}
            for part in parts:
                if ':' in part:
                    axis, value = part.split(':')
                    if axis in ['X', 'Y', 'Z', 'A', 'E']:
                        pos[axis] = float(value)
                        logger.info(f"  Parsed {axis}: {value}")
            
            # Update machine state - A axis reports in degrees directly
            a_value = pos.get('A', pos.get('E'))
            
            logger.info(f"POSITION UPDATE: X={pos.get('X')}, Y={pos.get('Y')}, Z={pos.get('Z')}, A={a_value}°")
            
            machine_state.update_position(
                x=pos.get('X'),
                y=pos.get('Y'),
                z=pos.get('Z'),
                a=a_value
            )
        except Exception as e:
            logger.error(f"Error parsing position '{line}': {e}", exc_info=True)
    
    def jog(self, axis: str, distance: float, feed_rate: float) -> None:
        """
        Jog a single axis by the specified distance.
        
        Args:
            axis: Axis to jog ('X', 'Y', 'Z', or 'A')
            distance: Distance to jog in mm (or degrees for A)
            feed_rate: Feed rate in mm/min (or deg/min for A)
        """
        generation = self.firmware_update_generation
        if self._firmware_operation_blocked(generation):
            return
        axis = str(axis).upper()
        if axis not in {"X", "Y", "Z", "A"}:
            logger.warning("Rejected jog for invalid axis %r", axis)
            return
        numeric = (int, float)
        if (
            isinstance(distance, bool)
            or isinstance(feed_rate, bool)
            or not isinstance(distance, numeric)
            or not isinstance(feed_rate, numeric)
            or not math.isfinite(distance)
            or not math.isfinite(feed_rate)
            or feed_rate <= 0
        ):
            logger.warning("Rejected jog with invalid distance/feed: %r/%r", distance, feed_rate)
            return
        with self.operation_lock:
            if self._firmware_operation_blocked(generation):
                return
            if (not machine_state.is_idle() or not self.connected or self.serial_uncertain
                    or self.rehome_required):
                logger.warning(f"Cannot jog: idle={machine_state.is_idle()}, connected={self.connected}")
                return
            machine_state.set_status("Jogging...", busy=True)
            try:
                self._jog_locked(axis, distance, feed_rate)
            finally:
                if self.connected and not self.serial_uncertain:
                    machine_state.set_status("Idle", busy=False)

    def _jog_locked(self, axis: str, distance: float, feed_rate: float) -> None:
        """Execute one indivisible jog transaction; operation_lock is held."""
        if axis.upper() == 'A':
            distance = -distance
            logger.info(f"JOG A AXIS: {distance} deg (inverted), feed={feed_rate} deg/min")
            move = f"G1 A{distance} F{feed_rate}"
        else:
            logger.info(f"JOG {axis.upper()} AXIS: distance={distance} mm, feed={feed_rate} mm/min")
            move = f"G1 {axis}{distance} F{feed_rate}"
        self._run_acknowledged_sequence_locked([
            ("M204 P500 T500", 10.0), ("G91", 10.0), (move, 10.0),
            ("G90", 10.0), ("M400", 180.0),
            ("M204 P2000 T3000", 10.0), ("M114", 10.0),
        ], "jog")
    
    def jog_xy(self, x_distance: float, y_distance: float, feed_rate: float) -> None:
        """
        Jog X and Y axes simultaneously (diagonal move).
        
        Args:
            x_distance: Distance to jog X axis in mm
            y_distance: Distance to jog Y axis in mm
            feed_rate: Feed rate in mm/min
        """
        generation = self.firmware_update_generation
        if self._firmware_operation_blocked(generation):
            return
        with self.operation_lock:
            if self._firmware_operation_blocked(generation):
                return
            if (not machine_state.is_idle() or not self.connected or self.serial_uncertain
                    or self.rehome_required):
                logger.warning(f"Cannot jog: idle={machine_state.is_idle()}, connected={self.connected}")
                return
            machine_state.set_status("Jogging...", busy=True)
            try:
                self._jog_xy_locked(x_distance, y_distance, feed_rate)
            finally:
                if self.connected and not self.serial_uncertain:
                    machine_state.set_status("Idle", busy=False)

    def _jog_xy_locked(self, x_distance: float, y_distance: float, feed_rate: float) -> None:
        logger.info(f"JOG XY DIAGONAL: X={x_distance} mm, Y={y_distance} mm, feed={feed_rate} mm/min")
        self._run_acknowledged_sequence_locked([
            ("M204 P500 T500", 10.0), ("G91", 10.0),
            (f"G1 X{x_distance} Y{y_distance} F{feed_rate}", 10.0),
            ("G90", 10.0), ("M400", 180.0),
            ("M204 P2000 T3000", 10.0), ("M114", 10.0),
        ], "jog")

    def _run_acknowledged_sequence_locked(
        self, commands: list[tuple[str, float]], operation: str
    ) -> bool:
        for command, timeout in commands:
            response = self._exchange_locked(command, timeout)
            if not _sd_response_ok(response):
                self._mark_serial_uncertain(
                    f"{operation} failed waiting for {command}", response
                )
                return False
        return True
    
    def home_axis(self, axis: str) -> bool:
        """
        Home a single axis.
        
        Args:
            axis: Axis to home ('X', 'Y', 'Z', or 'A')
        """
        generation = self.firmware_update_generation
        if self._firmware_operation_blocked(generation):
            return False
        axis = str(axis).upper()
        if axis not in {"X", "Y", "Z", "A"}:
            logger.warning("Rejected home for invalid axis %r", axis)
            log_controller_event("home_rejected", axis=axis, reason="invalid_axis")
            return False
        with self.operation_lock:
            if self._firmware_operation_blocked(generation):
                return False
            if not machine_state.is_idle() or not self.connected or self.serial_uncertain:
                reason = self._homing_rejection_reason()
                logger.warning("Rejected home %s: %s", axis, reason)
                log_controller_event("home_rejected", axis=axis, reason=reason)
                return False
            self.homed = False
            machine_state.set_status(f"Homing {axis}...", busy=True)
            if not self._quiesce_before_stream():
                return False
            reset_y = axis == "Y" and self.reset_y_motors_before_home
            commands = []
            if reset_y:
                commands.extend((("M18 Y", 10.0), ("G4 P1000", 10.0),
                                 ("M17 Y", 10.0), ("G4 P500", 10.0)))
                log_controller_event("recovery_home_y_motor_cycle", axis=axis)
            commands.extend((("M17", 10.0), ("M203 Z5", 10.0), ("M201 Z15", 10.0),
                             ("M204 P1000 T1000", 10.0), (f"G28 {axis}", 180.0),
                             ("M400", 180.0), ("M114", 10.0),
                             ("M204 P2000 T3000", 10.0), ("M18", 10.0)))
            for command, timeout in commands:
                response = self._exchange_locked(command, timeout)
                if not _sd_response_ok(response):
                    self._mark_serial_uncertain(f"Homing failed waiting for {command}", response)
                    return False
            if reset_y:
                self.reset_y_motors_before_home = False
            machine_state.set_status("Idle", busy=False)
            return True
    
    def _homing_rejection_reason(self) -> str:
        if not self.connected:
            return "controller disconnected"
        if self.serial_uncertain:
            return "controller state uncertain; confirm motion stopped and run recovery"
        if not machine_state.is_idle():
            return f"machine is {machine_state.snapshot().status_text}"
        return "controller is not ready"

    def home_all(self) -> bool:
        """Home all axes sequentially."""
        generation = self.firmware_update_generation
        if self._firmware_operation_blocked(generation):
            return False
        with self.operation_lock:
            if self._firmware_operation_blocked(generation):
                return False
            if not machine_state.is_idle() or not self.connected or self.serial_uncertain:
                reason = self._homing_rejection_reason()
                logger.warning("Rejected home all: %s", reason)
                log_controller_event("home_rejected", axis="ALL", reason=reason)
                return False
            self.homed = False
            machine_state.set_status("Homing all axes...", busy=True)
            if not self._quiesce_before_stream():
                return False
            reset_y = self.reset_y_motors_before_home
            commands = []
            if reset_y:
                commands.extend((("M18 Y", 10.0), ("G4 P1000", 10.0),
                                 ("M17 Y", 10.0), ("G4 P500", 10.0)))
                log_controller_event("recovery_home_y_motor_cycle", axis="ALL")
            commands.extend((("M17", 10.0), ("M203 Z5", 10.0), ("M201 Z15", 10.0),
                             ("M204 P1000 T1000", 10.0), ("G28", 180.0),
                             ("M400", 180.0), ("G92 E0", 10.0), ("M114", 10.0),
                             ("M204 P2000 T3000", 10.0), ("M18", 10.0)))
            for command, timeout in commands:
                response = self._exchange_locked(command, timeout)
                if not _sd_response_ok(response):
                    self._mark_serial_uncertain(f"Homing failed waiting for {command}", response)
                    return False
            if reset_y:
                self.reset_y_motors_before_home = False
            machine_state.set_status("Idle", busy=False)
            self.homed = True
            self.rehome_required = False
            return True

    # ==================== Job Execution ====================
    
    def run_utility_sequence(self, gcode_lines: list[str]) -> Optional[threading.Thread]:
        """
        Run a utility G-code sequence (e.g. move to center) without requiring a loaded job.
        
        Args:
            gcode_lines: List of G-code commands to execute
        """
        generation = self.firmware_update_generation
        if self._firmware_operation_blocked(generation):
            return None
        with self.operation_lock:
            if self._firmware_operation_blocked(generation):
                return None
            if (not machine_state.is_idle() or not self.connected or self.serial_uncertain
                    or self.rehome_required):
                return None
            commands = list(gcode_lines)
            if not any(line.split(';', 1)[0].strip().upper() == "M400" for line in commands[-2:]):
                commands.append("M400")
            self.stop_requested = False
            self.pause_requested = False
            # Reserve the machine synchronously so a second UI callback cannot
            # start another operation before the worker begins.
            machine_state.set_status("Running", busy=True, paused=False)
            self.job_thread = threading.Thread(
                target=self._execute_job, args=(commands,), kwargs={"is_job": False}, daemon=True
            )
            self.job_thread.start()
            return self.job_thread

    def start_job(
        self,
        gcode_lines: list[str],
        piece_count: int = 0,
        source_filename: Optional[str] = None,
    ) -> None:
        """Atomically reserve the machine and start an SD-card job."""
        generation = self.firmware_update_generation
        if self._firmware_operation_blocked(generation):
            log_controller_event("job_start_rejected", reason="firmware_update_active")
            return
        with self.operation_lock:
            if self._firmware_operation_blocked(generation):
                log_controller_event("job_start_rejected", reason="firmware_update_active")
                return
            self._start_job_locked(gcode_lines, piece_count, source_filename)

    @staticmethod
    def _job_starts_with_full_home(gcode_lines: list[str]) -> bool:
        """Return whether XYZ and A are homed before the job's first motion."""
        homed_axes: set[str] = set()
        for raw_line in gcode_lines:
            command = raw_line.split(";", 1)[0].strip().upper()
            if not command:
                continue
            opcode = command.split(maxsplit=1)[0]
            if opcode in {"G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03", "G5", "G05"}:
                break
            if opcode == "G28":
                parameters = command[len(opcode):].strip()
                if not parameters:
                    homed_axes.update({"X", "Y", "Z", "A"})
                else:
                    homed_axes.update(
                        axis for axis in ("X", "Y", "Z", "A")
                        if re.search(rf"(?:^|\s){axis}(?:\s|$|[-+0-9.])", parameters)
                    )
        return {"X", "Y", "Z", "A"}.issubset(homed_axes)

    def _start_job_locked(
        self,
        gcode_lines: list[str],
        piece_count: int = 0,
        source_filename: Optional[str] = None,
    ) -> None:
        """
        Upload a G-code job to the controller SD card and run it from there.
        
        Args:
            gcode_lines: List of G-code commands to execute
            piece_count: Number of pieces this job is expected to cut.
        """
        state = machine_state.snapshot()
        if self.firmware_update_active:
            log_controller_event("job_start_rejected", reason="firmware_update_active")
            return
        homes_before_motion = self._job_starts_with_full_home(gcode_lines)
        if (not machine_state.is_idle() or not gcode_lines or not self.connected
                or self.serial_uncertain
                or ((not self.homed or self.rehome_required) and not homes_before_motion)):
            log_controller_event(
                "job_start_rejected",
                idle=machine_state.is_idle(),
                has_gcode=bool(gcode_lines),
                connected=self.connected,
                homed=self.homed,
                rehome_required=self.rehome_required,
                homes_before_motion=homes_before_motion,
            )
            return
        
        self.stop_requested = False
        self.pause_requested = False
        self._current_job_gcode = list(gcode_lines)
        self._current_job_piece_count = max(0, int(piece_count or 0))
        self._current_source_filename = source_filename or state.loaded_filename
        if self._current_source_filename:
            # Freeze the identity of the canvas that actually won the machine
            # reservation, rather than leaving the header tied to mutable UI state.
            machine_state.set_job_loaded(True, self._current_source_filename)
        self.current_job_id = uuid.uuid4().hex
        self._stopped_log_upload_scheduled = False
        self.sd_job_active = False
        self.sd_job_disconnected = False
        _SD_JOB_STATE_FILE.unlink(missing_ok=True)
        self.clear_resume_state()
        self.sd_uploading = True
        log_controller_event(
            "job_start",
            command_count=len(gcode_lines),
            filename=self._current_source_filename,
            piece_count=self._current_job_piece_count,
            job_id=self.current_job_id,
        )
        machine_state.set_status("Uploading to SD card...", busy=True, paused=False)
        self.job_thread = threading.Thread(
            target=self._execute_sd_job,
            args=(gcode_lines,),
            daemon=True,
            name="sd-job-upload-and-monitor",
        )
        self.job_thread.start()
    
    def pause_job(self) -> None:
        """Pause a running SD print, or pause host streaming for utility moves."""
        generation = self.firmware_update_generation
        if self._firmware_operation_blocked(generation):
            return
        with self.operation_lock:
            if self._firmware_operation_blocked(generation):
                return
            self._pause_job_locked()

    def _pause_job_locked(self) -> None:
        if machine_state.is_running():
            if self.sd_uploading:
                logger.warning("Cannot pause while the G-code file is being uploaded")
                return
            if self.sd_job_active:
                response = self.send_command_with_response("M25", timeout=8.0)
                if not _sd_response_ok(response):
                    logger.error(f"Failed to pause SD print: {response}")
                    self._mark_serial_uncertain(
                        "SD pause acknowledgement missing", response, stop_motion=False
                    )
                    return
            self.pause_requested = True
            machine_state.set_status("Paused", busy=True, paused=True)
            log_controller_event("job_pause")
    
    def resume_job(self) -> None:
        """Resume a paused SD print or host-streamed utility sequence."""
        generation = self.firmware_update_generation
        if self._firmware_operation_blocked(generation):
            return
        with self.operation_lock:
            if self._firmware_operation_blocked(generation):
                return
            self._resume_job_locked()

    def _resume_job_locked(self) -> None:
        if machine_state.snapshot().paused:
            if self.sd_job_active:
                response = self.send_command_with_response("M24", timeout=8.0)
                if not _sd_response_ok(response):
                    logger.error(f"Failed to resume SD print: {response}")
                    self._mark_serial_uncertain(
                        "SD resume acknowledgement missing", response, stop_motion=False
                    )
                    return
            self.pause_requested = False
            machine_state.set_status("Running", busy=True, paused=False)
            log_controller_event("job_resume")
    
    def stop_job(self) -> bool:
        """Request an immediate stop, then prove the controller is quiescent."""
        generation = self.firmware_update_generation
        with self.firmware_update_gate:
            if self._firmware_operation_blocked(generation):
                return False
            self.pause_requested = False
            self.stop_requested = True
            self.homed = False
            self.rehome_required = True
            self.reset_y_motors_before_home = True
            machine_state.set_status("Stopping", busy=True, paused=False)
            log_controller_event("job_stop_requested", priority=True)

        stopped_during_job = self.sd_uploading or self.sd_job_active or self.streaming_mode

        def upload_stopped_log() -> None:
            if (not stopped_during_job or not _log_uploader
                    or self._stopped_log_upload_scheduled):
                return
            self._stopped_log_upload_scheduled = True
            threading.Thread(
                target=_log_uploader.upload_now,
                args=(False, "job_stopped"),
                daemon=True,
                name="log-upload-on-job-stopped",
            ).start()

        if self.connected and self.sd_uploading:
            # M28 treats all incoming commands as file contents. The upload
            # thread observes stop_requested and closes the file with M29.
            pass
        elif self.connected and self.sd_job_active:
            if not self._send_priority_stop(sd_job=True):
                self._mark_serial_uncertain("Could not transmit priority SD stop")
                upload_stopped_log()
                return False
            with self.operation_lock:
                if not self._quiesce_after_priority_stop(timeout=30.0):
                    upload_stopped_log()
                    return False
                status = self._exchange_locked("M27", 8.0)
                if "not sd printing" not in status.lower():
                    self._mark_serial_uncertain("Could not confirm SD playback stopped", status)
                    upload_stopped_log()
                    return False
                self._clear_sd_job_state()
        elif self.connected:
            if not self._send_priority_stop(sd_job=False):
                self._mark_serial_uncertain("Could not transmit priority quick stop")
                upload_stopped_log()
                return False
            with self.operation_lock:
                if not self._quiesce_after_priority_stop(timeout=30.0):
                    upload_stopped_log()
                    return False
        else:
            self.serial_uncertain = True
            machine_state.set_status("Stop unconfirmed — use physical E-stop", busy=True)
            upload_stopped_log()
            return False

        if self.job_thread and self.job_thread.is_alive():
            self.job_thread.join(timeout=2.0)
            if self.job_thread.is_alive() and self.sd_uploading:
                machine_state.set_status("Cancelling SD upload...", busy=True)
                upload_stopped_log()
                return True

        machine_state.reset_job()
        machine_state.set_status("Stopped — re-home required", busy=False)
        log_controller_event("job_stopped", rehome_required=True)
        upload_stopped_log()
        return True
    
    def send_command_with_response(self, command: str, timeout: float = 5.0) -> str:
        """Send a G-code command and return the response."""
        generation = self.firmware_update_generation
        if self._firmware_operation_blocked(generation):
            return "ERROR: Firmware update owns the controller"
        with self.operation_lock:
            if self._firmware_operation_blocked(generation):
                return "ERROR: Firmware update owns the controller"
            if not self.connected:
                return "ERROR: Not connected to controller"
            return self._exchange_locked(command, timeout)

    def send_manual_command(self, command: str, timeout: float = 10.0) -> str:
        """Run an operator console command only in a known idle state."""
        generation = self.firmware_update_generation
        if self._firmware_operation_blocked(generation):
            return "ERROR: Firmware update owns the controller"
        with self.operation_lock:
            if self._firmware_operation_blocked(generation):
                return "ERROR: Firmware update owns the controller"
            if (not self.connected or not machine_state.is_idle() or self.sd_uploading
                    or self.sd_job_active or self.serial_uncertain or self.rehome_required):
                return "ERROR: Controller is not safely idle"
            return self._exchange_locked(command, timeout)

    def _exchange_locked(self, command: str, timeout: float) -> str:
        """Perform one request/response exchange while operation_lock is held."""
        with self.read_lock:
            if not self._pause_reader():
                return "ERROR: Could not acquire serial response ownership"
            try:
                if not self._send_command(command):
                    return "ERROR: Serial write failed"
                response = self._read_response(timeout=timeout)
                return response if response else "No response"
            finally:
                self._resume_reader()

    def _mark_serial_uncertain(
        self, reason: str, response: str = "", *, stop_motion: bool = True
    ) -> None:
        """Fail closed when delivery or controller state cannot be established."""
        self.serial_uncertain = True
        self.homed = False
        self.rehome_required = True
        if stop_motion:
            self.stop_requested = True
        machine_state.set_status("Machine state uncertain — verify controller", busy=True)
        logger.error("%s: %s", reason, response or "no response")
        log_controller_event("machine_state_uncertain", reason=reason, response=response[:500])

    def read_homing_sensitivity(self) -> str:
        """Read current sensorless homing sensitivity values from firmware."""
        return self.send_manual_command("M914", timeout=6.0)

    def set_homing_sensitivity(
        self,
        *,
        x: Optional[int] = None,
        y: Optional[int] = None,
        persist_to_eeprom: bool = False,
    ) -> tuple[bool, str]:
        generation = self.firmware_update_generation
        if self._firmware_operation_blocked(generation):
            return False, "ERROR: Firmware update owns the controller"
        with self.operation_lock:
            if self._firmware_operation_blocked(generation):
                return False, "ERROR: Firmware update owns the controller"
            return self._set_homing_sensitivity_locked(
                x=x, y=y, persist_to_eeprom=persist_to_eeprom
            )

    def _set_homing_sensitivity_locked(
        self,
        *,
        x: Optional[int] = None,
        y: Optional[int] = None,
        persist_to_eeprom: bool = False,
    ) -> tuple[bool, str]:
        """Set sensorless homing sensitivity via Marlin M914.

        Returns:
            (ok, response): ok indicates whether firmware accepted the command(s).
            response includes firmware text for operator/debug visibility.
        """
        if not self.connected:
            return False, "ERROR: Not connected to controller"
        if not machine_state.is_idle() or self.sd_job_active or self.sd_uploading or self.serial_uncertain:
            return False, "ERROR: Controller is not safely idle"
        if x is None and y is None:
            return False, "ERROR: Provide at least one axis value"

        parts = ["M914"]
        if x is not None:
            parts.append(f"X{int(x)}")
        if y is not None:
            parts.append(f"Y{int(y)}")
        cmd = " ".join(parts)

        response = self.send_command_with_response(cmd, timeout=8.0)
        if not _sd_response_ok(response):
            return False, response or "No response"

        if persist_to_eeprom:
            save_resp = self.send_command_with_response("M500", timeout=10.0)
            combined = f"{response}\n{save_resp}".strip()
            if not _sd_response_ok(save_resp):
                return False, combined
            return True, combined

        return True, response
    
    def _try_serial_boundary(self, timeout: float) -> tuple[bool, str]:
        """Try one identifiable M400/M115 serial boundary without changing safety state."""
        if not self.serial_port or not self.serial_port.is_open:
            return False, "Serial port is not open"

        with self.read_lock:
            if not self._pause_reader():
                return False, "Could not acquire serial response ownership"
            try:
                # Drain bytes already delivered to the host before creating the
                # boundary. Outstanding controller work may still reply later,
                # so an acknowledgement alone is not a sufficient marker.
                while self.serial_port.in_waiting:
                    line = self.serial_port.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        log_serial_rx(line, mode="sync_drain")
                if not self._send_command("M400"):
                    return False, "Could not send M400"
                # M115 is queued after M400 and has an identifiable banner. A
                # banner followed by its own ok proves all earlier commands,
                # including M400, were processed; a stale bare ok cannot do so.
                if not self._send_command("M115"):
                    return False, "Could not send M115"
                lines: list[str] = []
                banner_seen = False
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if self.serial_port.in_waiting:
                        line = self.serial_port.readline().decode('utf-8', errors='ignore').strip()
                        if line:
                            lines.append(line)
                            log_serial_rx(line, mode="sync_boundary")
                            banner_seen = banner_seen or "FIRMWARE_NAME" in line
                            if banner_seen and line.lower().startswith("ok"):
                                return True, "\n".join(lines)
                    else:
                        time.sleep(0.01)
                return False, "\n".join(lines)
            finally:
                self._resume_reader()

    def _quiesce_after_priority_stop(self, timeout: float = 30.0) -> bool:
        """Retry synchronization while Marlin finishes its asynchronous SD abort.

        With EMERGENCY_PARSER, M524 can clear the normal command queue just after
        the host sends its first boundary probe. Retrying is safe after M410 and
        avoids requiring operator recovery merely because that probe was erased.
        """
        deadline = time.monotonic() + timeout
        responses: list[str] = []
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            remaining = deadline - time.monotonic()
            complete, response = self._try_serial_boundary(min(2.0, remaining))
            if complete:
                log_controller_event("priority_stop_serial_boundary", attempts=attempts)
                return True
            if response:
                responses.append(response)
            if time.monotonic() < deadline:
                time.sleep(0.1)

        response = "\n--- retry ---\n".join(responses[-3:])
        self._mark_serial_uncertain(
            "Could not synchronize after priority stop", response
        )
        return False

    def _quiesce_before_stream(self, timeout: float = 180.0) -> bool:
        """Wait for any in-flight commands to finish before a streaming job begins.

        A manually-issued command (e.g. Home All) may still be executing — and
        its `ok` responses still pending — when a job is started. Because the
        background read loop credits every `ok` toward streaming flow control,
        those stale acks would otherwise be counted against the first streamed
        lines. That makes blocking commands like G28 appear to finish instantly,
        letting cutting moves race ahead of homing. Here we drain the link, with
        the read loop paused, and require an identifiable M115 response queued
        behind M400 before treating the serial stream as quiescent.
        """
        complete, response = self._try_serial_boundary(timeout)
        if not complete:
            self._mark_serial_uncertain("M400 quiesce was not acknowledged", response)
            return False
        return True

    def _upload_gcode_to_sd(self, gcode_lines: list[str]) -> tuple[bool, int, str]:
        """Upload, verify, select, and start a file using Marlin's SD commands."""
        try:
            commands = _prepare_sd_commands(gcode_lines)
        except ValueError as exc:
            return False, 0, str(exc)
        if not commands:
            return False, 0, "The generated G-code contains no executable commands"

        total_bytes = sum(
            len((self._wrap_with_line_number(index, command) + "\n").encode("ascii"))
            for index, command in enumerate(commands, start=1)
        )
        self.sd_uploading = True
        self.streaming_mode = False

        def exchange(command: str, timeout: float = 8.0) -> str:
            if not self._send_command(command):
                return ""
            return self._read_response(
                timeout=timeout,
                poll_interval=_SD_UPLOAD_POLL_INTERVAL,
            )

        # Own the serial receive stream for the complete upload transaction.
        # M28 makes every incoming line file content until M29, so no unrelated
        # command may be interleaved during this critical section.
        with self.read_lock:
            if not self._pause_reader():
                return False, 0, "Could not acquire serial response ownership"
            write_open = False
            try:
                if self.stop_requested:
                    return False, 0, "SD upload cancelled by operator"

                # A prior serial-streamed job leaves Marlin expecting the next
                # N value. Reset the protocol before each SD transfer so its
                # first numbered payload line is always N1.
                reset_payload = self._wrap_with_line_number(0, "M110 N0")
                if not self._write_serial_payload(reset_payload):
                    return False, 0, "Could not reset Marlin's serial line counter"
                log_serial_tx("M110 N0", sd_upload=True, line_number=0, wire=reset_payload)
                response = self._read_response(
                    timeout=8.0,
                    poll_interval=_SD_UPLOAD_POLL_INTERVAL,
                )
                if not _sd_response_ok(response):
                    return False, 0, f"Could not reset Marlin's serial line counter: {response or 'No response'}"

                response = exchange("M21")
                if not _sd_response_ok(response):
                    return False, 0, f"SD card initialization failed: {response or 'No response'}"

                response = exchange(f"M28 {_SD_FILENAME}")
                if not _sd_response_ok(response):
                    return False, 0, f"Could not open SD file for writing: {response or 'No response'}"
                write_open = True

                for index, command in enumerate(commands, start=1):
                    if self.stop_requested:
                        return False, 0, "SD upload cancelled by operator"
                    payload = self._wrap_with_line_number(index, command)
                    attempts = 0
                    while True:
                        if not self._write_serial_payload(payload):
                            return False, 0, f"Serial write failed at G-code line {index}"
                        log_serial_tx(
                            command,
                            sd_upload=True,
                            line_number=index,
                            wire=payload,
                            resend=attempts > 0,
                        )
                        response = self._read_response(
                            timeout=8.0,
                            poll_interval=_SD_UPLOAD_POLL_INTERVAL,
                        )
                        resend = _RESEND_RE.search(response)
                        if resend:
                            requested = int(resend.group(1))
                            if requested != index or attempts >= 2:
                                return False, 0, (
                                    f"SD write could not recover line N{index}; "
                                    f"Marlin requested N{requested}: {response}"
                                )
                            attempts += 1
                            continue
                        if not _sd_response_ok(response):
                            return False, 0, f"SD write failed at G-code line {index}: {response or 'No acknowledgement'}"
                        break

                response = exchange("M29", timeout=10.0)
                write_open = False
                if not _sd_response_ok(response):
                    return False, 0, f"Could not close SD file: {response or 'No response'}"

                listing = exchange("M20", timeout=15.0)
                if not _sd_response_ok(listing) or _SD_FILENAME.casefold() not in listing.casefold():
                    return False, 0, f"Uploaded file was not confirmed in the SD listing: {listing or 'No response'}"

                if self.stop_requested:
                    return False, 0, "SD job cancelled before start"

                response = exchange(f"M23 {_SD_FILENAME}")
                if not _sd_response_ok(response):
                    return False, 0, f"Could not select SD file: {response or 'No response'}"
                if self.stop_requested:
                    return False, 0, "SD job cancelled before start"

                sd_state = {
                    "filename": _SD_FILENAME,
                    "source_filename": self._current_source_filename,
                    "piece_count": self._current_job_piece_count,
                    "total_bytes": total_bytes,
                    "started_at": time.time(),
                    "job_id": self.current_job_id,
                }
                try:
                    _SD_JOB_STATE_FILE.write_text(json.dumps(sd_state))
                except OSError as exc:
                    return False, 0, f"Could not persist SD job recovery state: {exc}"

                # From this point a lost serial acknowledgement cannot prove
                # that playback did not start, so disconnect handling must
                # treat the SD job as potentially active.
                self.sd_job_active = True
                response = exchange("M24")
                if not _sd_response_ok(response):
                    # Delivery is ambiguous after the write: Marlin may have
                    # started autonomous SD playback even when its `ok` was
                    # lost. Preserve the recovery marker and block all further
                    # motion until the operator verifies the machine.
                    self._mark_serial_uncertain(
                        "SD start acknowledgement missing",
                        response,
                        stop_motion=False,
                    )
                    return False, 0, f"Could not start SD print: {response or 'No response'}"
                return True, total_bytes, ""
            except Exception as exc:
                logger.exception("SD upload transaction failed")
                return False, 0, str(exc)
            finally:
                if write_open and self.serial_port and self.serial_port.is_open:
                    # Always leave Marlin out of SD-write mode after a failed or
                    # cancelled transfer. Never start the partial file.
                    exchange("M29", timeout=5.0)
                self._resume_reader()
                self.sd_uploading = False

    def _execute_sd_job(self, gcode_lines: list[str]) -> None:
        """Upload a job to the controller SD card, then monitor Marlin's M27 status."""
        if not self.connected:
            logger.error("Cannot upload SD job: not connected to controller")
            machine_state.set_status("Error", busy=False)
            return

        machine_state.set_status("Uploading to SD card...", busy=True, paused=False)
        started_at = time.time()
        log_controller_event(
            "sd_upload_start",
            filename=_SD_FILENAME,
            command_count=len(gcode_lines),
            job_id=self.current_job_id,
        )
        # Drain old acknowledgements before the SD-write protocol takes over.
        try:
            with self.operation_lock:
                if not self._quiesce_before_stream():
                    uploaded, total_bytes, error = False, 0, "Controller did not acknowledge M400"
                else:
                    uploaded, total_bytes, error = self._upload_gcode_to_sd(gcode_lines)
        except Exception as exc:
            logger.exception("Failed while preparing the SD upload")
            uploaded, total_bytes, error = False, 0, str(exc)
        finally:
            self.sd_uploading = False
        if not uploaded:
            if self.sd_job_active and _SD_JOB_STATE_FILE.exists():
                machine_state.set_status(
                    "SD start uncertain — use physical E-stop and verify machine",
                    busy=True,
                )
                log_controller_event(
                    "sd_start_uncertain",
                    filename=_SD_FILENAME,
                    error=error,
                )
                return
            if not self.connected:
                machine_state.set_status("Disconnected", busy=False)
                if self.sd_job_disconnected:
                    return
            elif self.stop_requested:
                machine_state.reset_job()
                machine_state.set_status("Stopped", busy=False)
                log_controller_event("sd_upload_cancelled", filename=_SD_FILENAME)
            else:
                logger.error(f"SD upload failed: {error}")
                machine_state.set_status("SD upload failed", busy=False)
                log_controller_event("sd_upload_failed", filename=_SD_FILENAME, error=error)
            self._current_job_piece_count = 0
            return

        if not self.connected and self.sd_job_disconnected:
            # Preserve the reconnect marker: SD playback may still be running.
            return

        if self.stop_requested:
            if self.connected and self.sd_job_active:
                response = self.send_command_with_response("M524", timeout=8.0)
                if not _sd_response_ok(response):
                    self.stop_requested = False
                    machine_state.set_status("SD stop failed — use physical E-stop", busy=True)
                    log_controller_event("sd_stop_failed", response=response[:300])
                    self._monitor_sd_job(total_bytes=total_bytes)
                    return
                self._send_command("M410")
            machine_state.reset_job()
            machine_state.set_status("Stopped", busy=False)
            self._clear_sd_job_state()
            return

        self.sd_job_active = True
        self.sd_job_disconnected = False
        machine_state.set_status("Running", busy=True, paused=False)
        log_controller_event(
            "sd_job_started",
            filename=_SD_FILENAME,
            total_bytes=total_bytes,
            job_id=self.current_job_id,
            upload_elapsed_s=round(time.time() - started_at, 2),
        )
        self._monitor_sd_job(total_bytes=total_bytes)

    def _monitor_sd_job(
        self,
        *,
        total_bytes: int = 0,
        initially_active: bool = False,
        reconnected: bool = False,
    ) -> None:
        """Poll M27 while Marlin runs from SD; retain state if serial disconnects."""
        active_seen = initially_active
        idle_reports = 0
        missed_reports = 0
        last_position_poll = 0.0

        while self.connected and not self.stop_requested:
            response = self.send_command_with_response("M27", timeout=4.0)
            if not self.connected:
                return

            progress = _parse_sd_progress(response)
            if progress:
                position, reported_total = progress
                active_seen = True
                idle_reports = 0
                total_bytes = reported_total or total_bytes
                if total_bytes > 0:
                    machine_state.update_job_progress(position / total_bytes)
                missed_reports = 0
            elif "not sd printing" in response.lower():
                idle_reports += 1
                if active_seen and idle_reports >= 2:
                    self._finish_sd_job()
                    return
                if not active_seen and idle_reports >= 4:
                    if reconnected:
                        machine_state.set_status(
                            "SD job ended while disconnected — re-home required",
                            busy=False,
                        )
                        log_controller_event("sd_job_ended_while_disconnected", filename=_SD_FILENAME)
                        self._clear_sd_job_state()
                    else:
                        # A successfully acknowledged M24 followed by repeated
                        # idle reports is a short completed file, not a stream.
                        self._finish_sd_job()
                    return
            elif not response or response == "No response":
                missed_reports += 1
                if missed_reports >= 3:
                    machine_state.set_status("Running (SD status unavailable)", busy=True)
            else:
                missed_reports += 1

            # M114 may block behind motion and delay Stop. M27 provides all
            # progress information needed while Marlin owns SD playback.
            time.sleep(0.5)

        if not self.connected and self.sd_job_disconnected:
            # Serial loss must not clear the marker needed to query M27 on reconnect.
            return

        if self.stop_requested:
            # stop_job owns reconciliation and preserves the recovery marker
            # until serial ordering and SD-idle state are both proven.
            return

    def _finish_sd_job(self) -> None:
        """Mark a fully completed SD print and run existing completion hooks."""
        position = self._read_position_at_job_end()
        prewarn = self._read_tmc_prewarn_at_job_end()
        machine_state.update_job_progress(1.0)
        machine_state.set_status("Complete", busy=False)
        title = self._job_completion_title(prewarn)
        log_controller_event(
            "job_complete",
            title=title,
            source="sd_card",
            filename=_SD_FILENAME,
            job_id=self.current_job_id or None,
            tmc_prewarn=prewarn["status"],
            tmc_prewarn_drivers=prewarn["drivers"],
            tmc_prewarn_response=prewarn["response"][:500],
            position_refresh=position["status"],
            position_response=position["response"][:500],
        )
        self._log_tmc_prewarn_result(prewarn)
        if _log_uploader:
            if self._current_job_piece_count > 0:
                threading.Thread(
                    target=_log_uploader.send_piece_count,
                    args=(self._current_job_piece_count,),
                    daemon=True,
                    name="piece-count-on-complete",
                ).start()
            threading.Thread(
                target=_log_uploader.upload_now,
                args=(False, "job_complete"),
                daemon=True,
                name="log-upload-on-complete",
            ).start()
        self._clear_sd_job_state()

    def _read_position_at_job_end(self) -> dict[str, str]:
        """Refresh the UI's logical position after Marlin confirms a job is idle."""
        unavailable = {"status": "unavailable", "response": "M114 check could not be run"}
        with self.operation_lock:
            if not self.connected or self.serial_uncertain or self.stop_requested:
                return unavailable
            try:
                response = self._exchange_locked("M114", 8.0)
            except Exception as exc:
                logger.exception("M114 position query failed after job completion")
                return {
                    "status": "unavailable",
                    "response": f"M114 query raised {type(exc).__name__}: {exc}",
                }

        position_line = next(
            (line for line in response.splitlines() if line.startswith("X:")),
            None,
        )
        if not position_line or not _sd_response_ok(response):
            logger.warning("Could not refresh Marlin position after job completion: %s", response)
            return {"status": "unavailable", "response": response or "No response"}

        axes = {part.split(":", 1)[0] for part in position_line.split() if ":" in part}
        if not {"X", "Y", "Z"}.issubset(axes) or not ({"A", "E"} & axes):
            logger.warning("Incomplete M114 position after job completion: %s", position_line)
            return {"status": "unavailable", "response": response}

        self._parse_position(position_line)
        return {"status": "updated", "response": response}

    def _read_tmc_prewarn_at_job_end(self) -> dict[str, object]:
        """Read Marlin's latched M911 flags after a job has finished."""
        unavailable = {
            "status": "unavailable",
            "drivers": [],
            "response": "M911 check could not be run",
        }
        with self.operation_lock:
            if not self.connected or self.serial_uncertain or self.stop_requested:
                return unavailable
            try:
                response = self._exchange_locked("M911", 8.0)
            except Exception as exc:
                logger.exception("M911 prewarn query failed after job completion")
                return {**unavailable, "response": f"M911 query raised {type(exc).__name__}: {exc}"}

        flags = parse_tmc_prewarn_response(response)
        if not flags or not _sd_response_ok(response):
            return {**unavailable, "response": response or "No response"}
        drivers = sorted(axis for axis, triggered in flags.items() if triggered)
        return {
            "status": "prewarn" if drivers else "clear",
            "drivers": drivers,
            "response": response,
        }

    @staticmethod
    def _job_completion_title(prewarn: dict[str, object]) -> str:
        if prewarn["status"] == "prewarn":
            return f"Job complete — TMC PREWARN LATCHED: {', '.join(prewarn['drivers'])}"
        if prewarn["status"] == "unavailable":
            return "Job complete — TMC PREWARN check unavailable"
        return "Job complete — TMC PREWARN clear"

    def _log_tmc_prewarn_result(self, prewarn: dict[str, object]) -> None:
        if prewarn["status"] == "prewarn":
            logger.warning(
                "%s; M911 response: %s",
                self._job_completion_title(prewarn),
                prewarn["response"],
            )
        elif prewarn["status"] == "unavailable":
            logger.warning(
                "%s; M911 response: %s",
                self._job_completion_title(prewarn),
                prewarn["response"],
            )
        else:
            logger.info("%s", self._job_completion_title(prewarn))

    def _clear_sd_job_state(self) -> None:
        self.sd_job_active = False
        self.sd_job_disconnected = False
        self._current_job_piece_count = 0
        try:
            _SD_JOB_STATE_FILE.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"Could not remove saved SD job state: {exc}")

    def _reconcile_sd_job_after_reconnect(self) -> None:
        """Keep asking Marlin for SD state until it reports active or idle."""
        machine_state.set_status("Checking Marlin SD job status...", busy=True)
        attempt = 0
        while self.connected and self.sd_job_disconnected:
            attempt += 1
            response = self.send_command_with_response("M27", timeout=5.0)
            if not self.connected:
                return
            progress = _parse_sd_progress(response)
            if progress:
                position, total_bytes = progress
                self.sd_job_active = True
                self.sd_job_disconnected = False
                self.stop_requested = False
                self.pause_requested = False
                machine_state.update_job_progress(position / total_bytes if total_bytes else 0.0)
                machine_state.set_status("Running", busy=True, paused=False)
                log_controller_event(
                    "sd_job_reconnected",
                    filename=_SD_FILENAME,
                    position=position,
                    total_bytes=total_bytes,
                    status_query_attempts=attempt,
                )
                self.job_thread = threading.Thread(
                    target=self._monitor_sd_job,
                    kwargs={"total_bytes": total_bytes, "initially_active": True, "reconnected": True},
                    daemon=True,
                    name="sd-job-monitor-reconnected",
                )
                self.job_thread.start()
                return
            if "not sd printing" in response.lower():
                self.stop_requested = False
                self.pause_requested = False
                self._clear_sd_job_state()
                self.homed = False
                self.rehome_required = True
                machine_state.set_status(
                    "SD job ended while disconnected — re-home required",
                    busy=False,
                )
                log_controller_event(
                    "sd_job_ended_while_disconnected",
                    filename=_SD_FILENAME,
                    response=response[:500],
                    status_query_attempts=attempt,
                )
                return

            # A timeout, malformed reply, or unrelated response does not
            # establish whether Marlin is still cutting. Keep the recovery
            # marker and require operator verification instead of looping
            # forever or inferring that playback stopped.
            self._mark_serial_uncertain(
                "Could not confirm Marlin SD job status after reconnect",
                response,
                stop_motion=False,
            )
            log_controller_event(
                "sd_job_reconnect_status_ambiguous",
                filename=_SD_FILENAME,
                attempt=attempt,
                response=response[:500],
            )
            return

    def _execute_job(self, gcode_lines: list[str], is_job: bool = True) -> None:
        """
        Internal method to execute G-code job.
        Runs in a background thread.
        
        Uses buffered streaming with flow control - keeps the motion planner
        buffer full by sending commands ahead, waiting for 'ok' only when
        the buffer is full.
        
        Args:
            gcode_lines: List of G-code commands to execute
            is_job: True for a real toolpath job (logs job_complete and
                triggers a log upload). False for utility/jog sequences (e.g.
                move-to-center, change-cutting-wheel), which run through the
                same streaming path but should not be logged or uploaded.
        """
        if not self.connected:
            logger.error("Cannot execute job: not connected to controller")
            return
        
        machine_state.set_status("Running", busy=True, paused=False)

        # Make sure any previously-issued command (e.g. a manual Home All) has
        # fully completed and its `ok`s have been consumed before we start
        # counting acks for streamed lines. Otherwise those stale acks inflate
        # ok_count, so blocking commands like G28 appear to finish instantly and
        # cutting moves can race ahead of homing.
        with self.operation_lock:
            if not self._quiesce_before_stream():
                logger.error("Could not establish an idle serial boundary; aborting stream")
                if not self.serial_uncertain:
                    self._mark_serial_uncertain("Could not quiesce before stream")
                return

        self.streaming_mode = True
        
        # Reset ok counter
        with self.ok_lock:
            self.ok_count = 0

        # Reset line-number protocol state. After M110 N0, Marlin expects N1 next.
        with self.line_buffer_lock:
            self.next_line_number = 1
            self.line_buffer.clear()
            self.committed_line = 0
            self.resend_requested = None
            self.error_burst_pending = False
            self.resend_total = 0
        # M110 itself must be wrapped: 'N0 M110 N0*<cs>'. This both sets
        # Marlin's expected line counter to 0 and is its own line 0.
        reset_payload = self._wrap_with_line_number(0, "M110 N0")
        if not self._write_serial_payload(reset_payload):
            logger.error("Failed to send M110 N0 reset; aborting job")
            self.streaming_mode = False
            machine_state.set_status("Error", busy=False)
            return
        log_serial_tx("M110 N0", streaming=True, line_number=0, wire=reset_payload)
        # Give Marlin a moment to process the reset and emit its `ok`.
        time.sleep(0.05)

        # Filter out empty lines and comments first
        commands = []
        for line in gcode_lines:
            line = line.strip()
            # Remove inline comments
            if ';' in line:
                line = line.split(';')[0].strip()
            if line and not line.startswith('#'):
                commands.append(line)

        # Update _current_job_gcode to the filtered list so committed_line
        # (which tracks acked filtered commands; N1 == commands[0]) is a
        # valid index into it.
        self._current_job_gcode = commands
        
        total_commands = len(commands)
        # Flow control: keep some commands in flight but don't overflow Marlin's buffer
        max_in_flight = 8  # Conservative - Marlin has ~16 slot buffer
        throttle_threshold = 4  # Start waiting when this many in flight
        logger.info(f"Starting job with {total_commands} commands (line-numbered streaming)")
        
        sent_count = 1  # We've already sent M110 N0 — counts toward the ok we expect
        last_progress_log = 0
        
        # Debug timing
        job_start_time = time.time()
        total_wait_time = 0
        wait_count = 0
        max_wait_time = 0
        protocol_failed = False
        
        try:
            for i, cmd in enumerate(commands):
                # Check for stop request
                if self.stop_requested:
                    logger.info("Job stopped by user")
                    break
                
                # Check for pause request
                while self.pause_requested and not self.stop_requested:
                    time.sleep(0.1)
                
                if self.stop_requested:
                    break

                # Resends take priority — drain anything Marlin asked us to
                # retransmit before we put more new lines into its buffer.
                drained = self._drain_resends()
                if drained:
                    sent_count += drained

                # Send the command immediately (line-numbered, checksummed).
                if self._emit_new_streamed(cmd) is None:
                    logger.error("Streaming write failed; aborting job")
                    break
                sent_count += 1
                
                # Special handling only for blocking commands
                if cmd.upper().startswith('G28'):  # Homing
                    # Wait for ALL pending commands to complete
                    homing_wait_started = time.monotonic()
                    while not self.stop_requested:
                        # Drain any resends triggered by lines already in flight.
                        drained = self._drain_resends()
                        if drained:
                            sent_count += drained
                        with self.ok_lock:
                            in_flight = sent_count - self.ok_count
                        if in_flight <= 0:
                            break
                        if time.monotonic() - homing_wait_started > self.homing_timeout:
                            protocol_failed = True
                            self.stop_requested = True
                            self._mark_serial_uncertain(
                                "Timed out waiting for homing acknowledgement"
                            )
                            break
                        self.ok_event.clear()
                        self.ok_event.wait(timeout=1.0)
                    time.sleep(0.1)
                    
                elif cmd.upper().startswith('G4'):  # Dwell
                    start = time.time()
                    while not self.stop_requested and (time.time() - start) < 30.0:
                        drained = self._drain_resends()
                        if drained:
                            sent_count += drained
                        with self.ok_lock:
                            in_flight = sent_count - self.ok_count
                        if in_flight <= 0:
                            break
                        self.ok_event.clear()
                        self.ok_event.wait(timeout=1.0)
                else:
                    # Flow control - wait if too many commands in flight
                    with self.ok_lock:
                        in_flight = sent_count - self.ok_count
                    
                    if in_flight >= throttle_threshold:
                        wait_start = time.time()
                        while not self.stop_requested and in_flight >= throttle_threshold:
                            self.ok_event.clear()
                            self.ok_event.wait(timeout=0.05)
                            drained = self._drain_resends()
                            if drained:
                                sent_count += drained
                            with self.ok_lock:
                                in_flight = sent_count - self.ok_count
                            
                            if time.time() - wait_start > self.stream_ack_timeout:
                                logger.error(f"Timeout waiting for ok (in_flight={in_flight}); aborting")
                                protocol_failed = True
                                self.stop_requested = True
                                self._mark_serial_uncertain(
                                    "Timed out waiting for streamed command acknowledgement"
                                )
                                break
                        
                        this_wait = time.time() - wait_start
                        total_wait_time += this_wait
                        wait_count += 1
                        if this_wait > max_wait_time:
                            max_wait_time = this_wait
                
                # Update progress — committed_line is the last N Marlin
                # confirmed (1-indexed; N1 == commands[0]).
                committed = self.committed_line
                progress = min(1.0, committed / total_commands) if total_commands > 0 else 1.0
                machine_state.update_job_progress(progress)
                
                # Log progress every 10%
                progress_pct = int(progress * 10)
                if progress_pct > last_progress_log:
                    last_progress_log = progress_pct
                    with self.ok_lock:
                        in_flight = sent_count - self.ok_count
                    elapsed = time.time() - job_start_time
                    logger.info(
                        f"Job progress: {progress*100:.0f}% "
                        f"(committed=N{committed}/{total_commands}, "
                        f"sent={sent_count}, in_flight={in_flight}, "
                        f"resends={self.resend_total}, elapsed={elapsed:.1f}s)"
                    )
            
            # Wait for all remaining commands to be acknowledged
            if not self.stop_requested and not protocol_failed:
                logger.info("Waiting for remaining commands to complete...")
                wait_start = time.time()
                while True:
                    drained = self._drain_resends()
                    if drained:
                        sent_count += drained
                    with self.ok_lock:
                        in_flight = sent_count - self.ok_count
                    if in_flight <= 0:
                        break
                    # Shorter timeout - 10 seconds should be enough for motion to complete
                    if time.time() - wait_start > self.stream_final_ack_timeout:
                        logger.error(f"Timeout waiting for final commands ({in_flight} remaining); state is uncertain")
                        protocol_failed = True
                        self.stop_requested = True
                        self._mark_serial_uncertain(
                            "Timed out waiting for final streamed acknowledgements"
                        )
                        break
                    self.ok_event.clear()
                    self.ok_event.wait(timeout=0.2)
            
            # Job complete
            if not self.stop_requested and not protocol_failed:
                logger.info(f"Job complete! ({self.resend_total} resends)")
                prewarn = None
                position = None
                if is_job:
                    # All streamed acknowledgements have arrived. Release the
                    # line-numbered response path before the synchronous M911
                    # query takes serial response ownership.
                    self.streaming_mode = False
                    position = self._read_position_at_job_end()
                    prewarn = self._read_tmc_prewarn_at_job_end()
                machine_state.set_status("Complete" if is_job else "Idle", busy=False)
                machine_state.update_job_progress(1.0)
                if is_job:
                    title = self._job_completion_title(prewarn)
                    log_controller_event(
                        "job_complete",
                        title=title,
                        job_id=self.current_job_id or None,
                        sent=sent_count,
                        acked=self.ok_count,
                        committed=self.committed_line,
                        resends=self.resend_total,
                        elapsed_s=round(time.time() - job_start_time, 2),
                        total_wait_s=round(total_wait_time, 2),
                        max_wait_s=round(max_wait_time, 2),
                        tmc_prewarn=prewarn["status"],
                        tmc_prewarn_drivers=prewarn["drivers"],
                        tmc_prewarn_response=prewarn["response"][:500],
                        position_refresh=position["status"],
                        position_response=position["response"][:500],
                    )
                    self._log_tmc_prewarn_result(prewarn)
                    if _log_uploader:
                        if self._current_job_piece_count > 0:
                            threading.Thread(
                                target=_log_uploader.send_piece_count,
                                args=(self._current_job_piece_count,),
                                daemon=True,
                                name="piece-count-on-complete",
                            ).start()
                        threading.Thread(
                            target=_log_uploader.upload_now,
                            args=(False, "job_complete"),
                            daemon=True,
                            name="log-upload-on-complete",
                        ).start()
            elif not protocol_failed:
                machine_state.reset_job()
                if self.connected:
                    machine_state.set_status("Stopped", busy=False)
                else:
                    # Disconnect handler already set status to "Disconnected" — restore it
                    machine_state.set_status("Disconnected", busy=False)
                if is_job:
                    log_controller_event(
                        "job_aborted",
                        sent=sent_count,
                        acked=self.ok_count,
                        committed=self.committed_line,
                        resends=self.resend_total,
                        elapsed_s=round(time.time() - job_start_time, 2),
                    )
                    if _log_uploader:
                        threading.Thread(
                            target=_log_uploader.upload_now,
                            args=(False, "job_abort"),
                            daemon=True,
                            name="log-upload-on-abort",
                        ).start()
                
        except Exception as e:
            logger.error(f"Job execution error: {e}", exc_info=True)
            machine_state.set_status("Error", busy=False)
            log_controller_event("job_error", error=str(e))
        finally:
            self.streaming_mode = False
            self._current_job_piece_count = 0


# Global controller instance
cnc_controller = CNCController()
