"""Pure helpers for Marlin's Binary File Transfer (BFT) protocol."""

from __future__ import annotations

from dataclasses import dataclass
import re
import struct


START_TOKEN = 0xB5AD
CONTROL_PROTOCOL = 0
FILE_TRANSFER_PROTOCOL = 1
CONTROL_SYNC = 1
CONTROL_CLOSE = 2
TRANSFER_QUERY = 0
TRANSFER_OPEN = 1
TRANSFER_CLOSE = 2
TRANSFER_WRITE = 3
TRANSFER_ABORT = 4
PROTOCOL_VERSION = (0, 1, 0)


class BFTProtocolError(ValueError):
    """Raised when BFT packet data or responses are malformed."""


@dataclass(frozen=True)
class BFTPacket:
    sync: int
    protocol: int
    packet_type: int
    payload: bytes


@dataclass(frozen=True)
class BFTSyncInfo:
    sync: int
    buffer_size: int
    version: tuple[int, int, int]


def fletcher16(data: bytes) -> int:
    """Return Marlin BFT's Fletcher-16 checksum (modulo 255)."""
    checksum = 0
    for value in data:
        low = ((checksum & 0xFF) + value) % 255
        checksum = ((((checksum >> 8) + low) % 255) << 8) | low
    return checksum


def encode_packet(sync: int, protocol: int, packet_type: int, payload: bytes = b"") -> bytes:
    """Encode one little-endian BFT packet, including both checksums."""
    if not 0 <= sync <= 0xFF:
        raise BFTProtocolError("BFT sync number must fit in one byte")
    if not 0 <= protocol <= 0x0F or not 0 <= packet_type <= 0x0F:
        raise BFTProtocolError("BFT protocol and packet type must fit in four bits")
    if len(payload) > 0xFFFF:
        raise BFTProtocolError("BFT payload is too large")

    header_prefix = struct.pack(
        "<HBBH", START_TOKEN, sync, (protocol << 4) | packet_type, len(payload)
    )
    header_checksum = fletcher16(header_prefix[2:])
    header = header_prefix + struct.pack("<H", header_checksum)
    packet_checksum = fletcher16(header[2:] + payload)
    return header + payload + struct.pack("<H", packet_checksum)


def decode_packet(packet: bytes) -> BFTPacket:
    """Decode and verify a complete BFT packet (also useful for transport tests)."""
    if len(packet) < 10:
        raise BFTProtocolError("BFT packet is shorter than its header and footer")
    token, sync, meta, payload_size, header_checksum = struct.unpack_from("<HBBHH", packet)
    if token != START_TOKEN:
        raise BFTProtocolError("BFT start token is invalid")
    if len(packet) != 10 + payload_size:
        raise BFTProtocolError("BFT packet length does not match its header")
    if fletcher16(packet[2:6]) != header_checksum:
        raise BFTProtocolError("BFT header checksum is invalid")
    payload = packet[8 : 8 + payload_size]
    packet_checksum = struct.unpack_from("<H", packet, 8 + payload_size)[0]
    if fletcher16(packet[2 : 8 + payload_size]) != packet_checksum:
        raise BFTProtocolError("BFT packet checksum is invalid")
    return BFTPacket(sync, meta >> 4, meta & 0x0F, payload)


def parse_sync_response(line: str) -> BFTSyncInfo:
    match = re.fullmatch(r"ss\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\.(\d+)\.(\d+)", line.strip(), re.I)
    if not match:
        raise BFTProtocolError(f"Malformed BFT sync response: {line!r}")
    sync, buffer_size, major, minor, patch = map(int, match.groups())
    if not 0 <= sync <= 0xFF or buffer_size <= 0:
        raise BFTProtocolError("BFT sync response contains invalid limits")
    return BFTSyncInfo(sync, buffer_size, (major, minor, patch))


def parse_ack(line: str) -> tuple[str, int] | None:
    """Parse a BFT ack, resend request, or fatal-error response."""
    match = re.fullmatch(r"\s*(ok|rs|fe)\s*(\d+)\s*", line, re.I)
    if not match:
        return None
    return match.group(1).lower(), int(match.group(2))
