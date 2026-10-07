"""Binary CSI frame parsing, validation, and serialization.

Implements the WiSense-Twin binary wire protocol defined in Section 14
(docs/hardware/receiver-firmware.md):
- 4-byte sync marker (0xAA, 0x55, 0x57, 0x53)
- 24-byte header body (28 bytes total header)
- N-byte raw CSI payload (up to 384 bytes, signed 8-bit I/Q pairs)
- 4-byte trailer: 2-byte CRC-16-CCITT (LE) + 2-byte end marker (0x55, 0xAA)

Provides streaming parsing with resynchronization and corruption counting.
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass
import math
import struct
import time
from typing import Optional

from wisense_backend.contracts.csi_source import CSIRecord

MAGIC: bytes = bytes([0xAA, 0x55, 0x57, 0x53])
TRAILER: bytes = bytes([0x55, 0xAA])
HEADER_LEN: int = 28
HEADER_BODY_LEN: int = 24
TRAILER_LEN: int = 4
MAX_PAYLOAD_LEN: int = 384

# Header body structure after the 4-byte magic (24 bytes, little-endian):
#   header_len: uint16, payload_len: uint16, seq_num: uint32, rx_timestamp_us: uint32,
#   sender_ts_ms: uint32, rssi: int8, channel: uint8, noise_floor: int8,
#   first_word_invalid: uint8, sig_mode: int8, mcs: int8, reserved: 2 bytes
HEADER_STRUCT: struct.Struct = struct.Struct("<HHIIIbBbBbb2s")


def compute_crc16(data: bytes) -> int:
    """Compute CRC-16-CCITT (poly 0x1021, init 0xFFFF) across byte sequence.

    Matches ESP-IDF esp_crc16_be and Python binascii.crc_hqx.
    """
    return binascii.crc_hqx(data, 0xFFFF)


@dataclass(frozen=True, slots=True)
class CSIFrameHeader:
    """Parsed header of a binary CSI frame."""

    header_len: int
    payload_len: int
    seq_num: int
    rx_timestamp_us: int
    sender_ts_ms: int
    rssi: int
    channel: int
    noise_floor: int
    first_word_invalid: int
    sig_mode: int
    mcs: int
    reserved: bytes = b"\x00\x00"


@dataclass(frozen=True, slots=True)
class CSIFrame:
    """Complete parsed and verified binary CSI frame."""

    header: CSIFrameHeader
    payload: bytes
    crc: int
    host_timestamp: float

    @property
    def subcarrier_count(self) -> int:
        """Derived subcarrier count (payload_len // 2)."""
        return self.header.payload_len // 2

    def compute_amplitudes(self) -> tuple[float, ...]:
        """Compute subcarrier amplitudes: sqrt(I^2 + Q^2) from signed 8-bit pairs.

        Per Section 15 specifications, amplitudes are derived per subcarrier
        pair from raw interleaved signed 8-bit integers.
        """
        return tuple(
            math.hypot(i, q)
            for i, q in struct.iter_unpack("bb", self.payload)
        )

    def to_csi_record(self) -> CSIRecord:
        """Convert to canonical CSIRecord according to project contract.

        Uses host_timestamp (UTC Unix epoch seconds via time.time()), NOT
        device boot timestamps (rx_timestamp_us or sender_ts_ms).
        """
        return CSIRecord(
            timestamp=self.host_timestamp,
            subcarrier_count=self.subcarrier_count,
            amplitudes=self.compute_amplitudes(),
            phases=None,
            rssi=float(self.header.rssi),
            channel=self.header.channel,
            sequence_number=self.header.seq_num,
        )


def build_frame(
    payload: bytes,
    seq_num: int = 0,
    rx_timestamp_us: int = 0,
    sender_ts_ms: int = 0,
    rssi: int = -50,
    channel: int = 11,
    noise_floor: int = -96,
    first_word_invalid: int = 0,
    sig_mode: int = 2,
    mcs: int = 0,
    *,
    corrupt_crc: bool = False,
    corrupt_trailer: bool = False,
    corrupt_magic: bool = False,
    corrupt_header_len: bool = False,
) -> bytes:
    """Build a complete wire-format binary CSI frame.

    Utility for test fixtures, simulation, and test harnesses.
    """
    magic = b"\x00\x00\x00\x00" if corrupt_magic else MAGIC
    hdr_len = 99 if corrupt_header_len else HEADER_LEN
    header_body = HEADER_STRUCT.pack(
        hdr_len,
        len(payload),
        seq_num,
        rx_timestamp_us,
        sender_ts_ms,
        rssi,
        channel,
        noise_floor,
        first_word_invalid,
        sig_mode,
        mcs,
        b"\x00\x00",
    )
    header_and_payload = magic + header_body + payload
    crc = compute_crc16(header_and_payload)
    if corrupt_crc:
        crc = crc ^ 0xFFFF
    trailer_marker = b"\x00\x00" if corrupt_trailer else TRAILER
    trailer = struct.pack("<H", crc) + trailer_marker
    return header_and_payload + trailer


class CSIFrameParser:
    """Streaming binary frame parser with automatic resynchronization.

    Maintains an internal byte buffer, searches for the 4-byte sync marker,
    validates headers and CRC-16, skips non-frame ASCII text (like boot logs
    and [RX_STAT] lines), and tracks corruption metrics.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._corrupt_frame_count: int = 0
        self._valid_frame_count: int = 0
        self._text_output = bytearray()

    @property
    def corrupt_frame_count(self) -> int:
        """Total count of malformed, torn, or CRC-failing frames encountered."""
        return self._corrupt_frame_count

    @property
    def valid_frame_count(self) -> int:
        """Total count of successfully verified and parsed frames."""
        return self._valid_frame_count

    @property
    def buffer_size(self) -> int:
        """Current number of unconsumed bytes in the internal parse buffer."""
        return len(self._buffer)

    def feed(self, chunk: bytes) -> None:
        """Append incoming bytes to the parser buffer."""
        if chunk:
            self._buffer.extend(chunk)

    def extract_text(self) -> str:
        """Extract and clear any non-frame ASCII text collected so far."""
        if not self._text_output:
            return ""
        text = self._text_output.decode("ascii", errors="replace")
        self._text_output.clear()
        return text

    def clear(self) -> None:
        """Clear buffer and reset internal state."""
        self._buffer.clear()
        self._text_output.clear()
        self._corrupt_frame_count = 0
        self._valid_frame_count = 0

    def parse_next(self) -> Optional[CSIFrame]:
        """Attempt to parse and return the next complete CSIFrame from the buffer.

        If a frame is corrupted, bad CRC, or torn, it increments corrupt_frame_count,
        discards the bad sync marker, and scans forward for the next magic marker.

        Returns:
            CSIFrame if a valid frame was found, or None if more data is needed.
        """
        while True:
            idx = self._buffer.find(MAGIC)
            if idx == -1:
                # No magic sync marker found.
                # Retain last 3 bytes in case magic is split across chunks.
                if len(self._buffer) > 3:
                    self._text_output.extend(self._buffer[:-3])
                    del self._buffer[:-3]
                return None

            if idx > 0:
                # Discard non-frame bytes before magic (e.g. ASCII logs)
                self._text_output.extend(self._buffer[:idx])
                del self._buffer[:idx]

            # Buffer now starts with MAGIC
            if len(self._buffer) < HEADER_LEN:
                # Not enough bytes for full header
                return None

            header_body = self._buffer[4:HEADER_LEN]
            (
                header_len,
                payload_len,
                seq_num,
                rx_ts_us,
                sender_ts_ms,
                rssi,
                channel,
                noise_floor,
                first_word_invalid,
                sig_mode,
                mcs,
                reserved,
            ) = HEADER_STRUCT.unpack(header_body)

            # Sanity check header
            if (
                header_len != HEADER_LEN
                or payload_len <= 0
                or payload_len > MAX_PAYLOAD_LEN
                or payload_len % 2 != 0
            ):
                # Malformed header (false magic match or header corruption)
                del self._buffer[:4]
                self._corrupt_frame_count += 1
                continue

            total_len = HEADER_LEN + payload_len + TRAILER_LEN

            # Check if a subsequent magic sync marker appears before total_len (torn frame detection)
            next_magic = self._buffer.find(MAGIC, 4)
            if 0 < next_magic < total_len and len(self._buffer) >= next_magic + HEADER_LEN:
                next_hdr_len, next_p_len = struct.unpack_from("<HH", self._buffer, next_magic + 4)
                if (
                    next_hdr_len == HEADER_LEN
                    and 0 < next_p_len <= MAX_PAYLOAD_LEN
                    and next_p_len % 2 == 0
                ):
                    # Next frame header is verified; current frame is definitely torn
                    self._corrupt_frame_count += 1
                    del self._buffer[:next_magic]
                    continue

            if len(self._buffer) < total_len:
                # Need more bytes for payload and trailer
                return None

            frame_bytes = bytes(self._buffer[:total_len])
            header_and_payload = frame_bytes[: HEADER_LEN + payload_len]
            trailer_bytes = frame_bytes[HEADER_LEN + payload_len : total_len]
            crc_received = struct.unpack("<H", trailer_bytes[0:2])[0]
            end_marker = trailer_bytes[2:4]

            crc_computed = compute_crc16(header_and_payload)

            if end_marker == TRAILER and crc_computed == crc_received:
                # Valid frame!
                del self._buffer[:total_len]
                self._valid_frame_count += 1
                host_ts = time.time()
                payload = frame_bytes[HEADER_LEN : HEADER_LEN + payload_len]
                header = CSIFrameHeader(
                    header_len=header_len,
                    payload_len=payload_len,
                    seq_num=seq_num,
                    rx_timestamp_us=rx_ts_us,
                    sender_ts_ms=sender_ts_ms,
                    rssi=rssi,
                    channel=channel,
                    noise_floor=noise_floor,
                    first_word_invalid=first_word_invalid,
                    sig_mode=sig_mode,
                    mcs=mcs,
                    reserved=reserved,
                )
                return CSIFrame(
                    header=header,
                    payload=payload,
                    crc=crc_received,
                    host_timestamp=host_ts,
                )

            # CRC or trailer mismatch: corrupted or torn frame
            self._corrupt_frame_count += 1
            del self._buffer[:4]
            continue
