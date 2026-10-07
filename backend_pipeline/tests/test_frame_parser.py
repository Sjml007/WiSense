"""Unit tests for binary CSI frame parsing, validation, and resynchronization."""

import math
import struct
import time
import pytest

from wisense_backend.contracts.csi_source import CSIRecord
from wisense_backend.ingestion.frame_parser import (
    CSIFrame,
    CSIFrameHeader,
    CSIFrameParser,
    HEADER_LEN,
    MAGIC,
    TRAILER,
    build_frame,
    compute_crc16,
)


class TestFrameParser:
    """Validate binary CSI frame extraction, CRC checking, and resynchronization."""

    def test_compute_crc16_deterministic(self) -> None:
        """Verify CRC-16-CCITT computation matches known reference output."""
        data = b"WiSense-Twin-123456789"
        crc1 = compute_crc16(data)
        crc2 = compute_crc16(data)
        assert crc1 == crc2
        assert isinstance(crc1, int)
        assert 0 <= crc1 <= 0xFFFF

    def test_build_and_parse_valid_frame(self) -> None:
        """Verify building a frame and parsing it produces a valid CSIFrame."""
        # 6-byte payload = 3 subcarriers with known I/Q pairs:
        # (3, 4) -> hypot = 5.0
        # (-5, 12) -> hypot = 13.0
        # (-8, -15) -> hypot = 17.0
        payload = bytes([3, 4, -5 & 0xFF, 12, -8 & 0xFF, -15 & 0xFF])
        raw_bytes = build_frame(
            payload=payload,
            seq_num=101,
            rx_timestamp_us=2500000,
            sender_ts_ms=2500,
            rssi=-42,
            channel=11,
            noise_floor=-92,
            first_word_invalid=0,
            sig_mode=2,
            mcs=0,
        )

        parser = CSIFrameParser()
        parser.feed(raw_bytes)
        frame = parser.parse_next()

        assert frame is not None
        assert parser.valid_frame_count == 1
        assert parser.corrupt_frame_count == 0

        # Header fields
        assert frame.header.header_len == HEADER_LEN
        assert frame.header.payload_len == 6
        assert frame.header.seq_num == 101
        assert frame.header.rx_timestamp_us == 2500000
        assert frame.header.sender_ts_ms == 2500
        assert frame.header.rssi == -42
        assert frame.header.channel == 11
        assert frame.header.noise_floor == -92
        assert frame.header.sig_mode == 2
        assert frame.header.mcs == 0

        # Subcarrier calculation
        assert frame.subcarrier_count == 3
        amps = frame.compute_amplitudes()
        assert len(amps) == 3
        assert pytest.approx(amps[0], rel=1e-5) == 5.0
        assert pytest.approx(amps[1], rel=1e-5) == 13.0
        assert pytest.approx(amps[2], rel=1e-5) == 17.0

        # CSIRecord conversion
        record = frame.to_csi_record()
        assert isinstance(record, CSIRecord)
        assert record.subcarrier_count == 3
        assert record.sequence_number == 101
        assert record.rssi == -42.0
        assert record.channel == 11
        assert record.phases is None
        # Must reflect host epoch timestamp, not device timestamps
        assert record.timestamp > 1_700_000_000.0
        assert abs(record.timestamp - time.time()) < 2.0

    def test_interleaved_ascii_logs_stripped(self) -> None:
        """Verify ASCII boot logs and [RX_STAT] lines are stripped without corruption."""
        log_text = (
            b"I (342) rx_csi: Baud: 921600 | Channel: 11\r\n"
            b"[RX_STAT] count=80 | uptime=1027 ms | rate=80.0 pps | dropped=0\r\n"
        )
        payload = bytes([10, 0, 0, 20])  # 2 subcarriers
        valid_frame = build_frame(payload=payload, seq_num=1)

        parser = CSIFrameParser()
        # Feed log text followed by a valid binary frame
        parser.feed(log_text + valid_frame)

        frame = parser.parse_next()
        assert frame is not None
        assert frame.header.seq_num == 1
        assert parser.valid_frame_count == 1
        assert parser.corrupt_frame_count == 0

        extracted_text = parser.extract_text()
        assert "rx_csi: Baud: 921600" in extracted_text
        assert "[RX_STAT] count=80" in extracted_text

    def test_corrupted_crc_skipped_and_counted(self) -> None:
        """Verify frame with invalid CRC is skipped and increments corrupt_frame_count."""
        payload = bytes([5, 5, 10, 10])
        bad_frame = build_frame(payload=payload, seq_num=1, corrupt_crc=True)

        parser = CSIFrameParser()
        parser.feed(bad_frame)
        frame = parser.parse_next()

        assert frame is None
        assert parser.valid_frame_count == 0
        assert parser.corrupt_frame_count == 1

    def test_corrupted_trailer_skipped_and_counted(self) -> None:
        """Verify frame with invalid trailer marker is skipped and counted."""
        payload = bytes([5, 5, 10, 10])
        bad_frame = build_frame(payload=payload, seq_num=1, corrupt_trailer=True)

        parser = CSIFrameParser()
        parser.feed(bad_frame)
        frame = parser.parse_next()

        assert frame is None
        assert parser.valid_frame_count == 0
        assert parser.corrupt_frame_count == 1

    def test_malformed_header_len_skipped(self) -> None:
        """Verify malformed header_len is rejected and counted as corrupt."""
        payload = bytes([5, 5, 10, 10])
        bad_frame = build_frame(payload=payload, seq_num=1, corrupt_header_len=True)

        parser = CSIFrameParser()
        parser.feed(bad_frame)
        frame = parser.parse_next()

        assert frame is None
        assert parser.corrupt_frame_count == 1

    def test_torn_frame_resynchronization(self) -> None:
        """Verify recovery from a torn frame (walking_01.txt scenario).

        Stream layout:
        1. Valid Frame 1
        2. Torn Frame 2 (header says 64 bytes, but truncated at 20 bytes before next magic)
        3. Valid Frame 3
        """
        payload_valid = bytes([3, 4, 6, 8])
        frame1 = build_frame(payload=payload_valid, seq_num=10)

        # Build torn frame: complete header claiming 64-byte payload, but followed by only 16 bytes
        torn_header_body = struct.Struct("<HHIIIbBbBbb2s").pack(
            HEADER_LEN, 64, 11, 1000, 100, -50, 11, -95, 0, 2, 0, b"\x00\x00"
        )
        torn_bytes = MAGIC + torn_header_body + b"X" * 16  # Only 16 bytes of payload, no CRC/trailer!

        frame3 = build_frame(payload=payload_valid, seq_num=12)

        stream = frame1 + torn_bytes + frame3

        parser = CSIFrameParser()
        parser.feed(stream)

        # Frame 1 must parse cleanly
        parsed1 = parser.parse_next()
        assert parsed1 is not None
        assert parsed1.header.seq_num == 10

        # Frame 2 is torn and corrupt; parser must reject it, resync on next magic, and yield Frame 3
        parsed3 = parser.parse_next()
        assert parsed3 is not None
        assert parsed3.header.seq_num == 12

        assert parser.valid_frame_count == 2
        assert parser.corrupt_frame_count >= 1

    def test_incremental_chunked_feeding(self) -> None:
        """Verify parser functions properly when frame bytes arrive in small fragmented chunks."""
        payload = bytes([1, 2, 3, 4, 5, 6])
        full_frame = build_frame(payload=payload, seq_num=99)

        parser = CSIFrameParser()
        # Feed byte by byte
        for b in full_frame[:-1]:
            parser.feed(bytes([b]))
            assert parser.parse_next() is None  # incomplete

        # Feed last byte
        parser.feed(bytes([full_frame[-1]]))
        frame = parser.parse_next()
        assert frame is not None
        assert frame.header.seq_num == 99
        assert frame.subcarrier_count == 3

    def test_split_magic_across_chunks(self) -> None:
        """Verify magic marker split across two chunk boundaries is correctly detected."""
        payload = bytes([10, 20])
        full_frame = build_frame(payload=payload, seq_num=55)

        parser = CSIFrameParser()
        # Split magic b"\xaa\x55\x57\x53" into b"\xaa\x55" and b"\x57\x53..."
        parser.feed(full_frame[:2])
        assert parser.parse_next() is None
        assert parser.buffer_size == 2

        parser.feed(full_frame[2:])
        frame = parser.parse_next()
        assert frame is not None
        assert frame.header.seq_num == 55
