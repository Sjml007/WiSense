"""Unit tests for SerialCSISource hardware ingestion driver."""

import time
from typing import Optional
import pytest

from wisense_backend.contracts.csi_source import CSIRecord, CSIRecordSource
from wisense_backend.ingestion.frame_parser import build_frame
from wisense_backend.ingestion.serial_source import SerialCSISource, SerialSourceConfig


class FakeSerial:
    """Minimal mock object implementing the subset of pyserial's interface used by SerialCSISource."""

    def __init__(
        self,
        port: str = "COM4",
        baudrate: int = 921600,
        timeout: Optional[float] = 0.1,
        initial_data: bytes = b"",
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.buffer = bytearray(initial_data)
        self.is_open: bool = True
        self.eof: bool = False

    @property
    def in_waiting(self) -> int:
        return len(self.buffer)

    def write_incoming(self, data: bytes) -> None:
        """Inject data into the simulated serial receive buffer."""
        self.buffer.extend(data)
        self.eof = False

    def mark_eof(self) -> None:
        """Signal that no further bytes will be received."""
        self.eof = True

    def read(self, size: int = 1) -> bytes:
        """Simulate pyserial.read(size)."""
        if not self.is_open:
            raise RuntimeError("Port is closed")
        if not self.buffer:
            return b""
        chunk = bytes(self.buffer[:size])
        del self.buffer[:size]
        return chunk

    def close(self) -> None:
        self.is_open = False


class TestSerialCSISource:
    """Validate SerialCSISource protocol conformance, parsing, resync, and error handling."""

    def test_protocol_conformance(self) -> None:
        """Verify SerialCSISource satisfies the CSIRecordSource protocol."""
        config = SerialSourceConfig(port="COM4", baud_rate=921600)
        source = SerialCSISource(config=config)
        assert isinstance(source, CSIRecordSource)

    def test_config_validation(self) -> None:
        """Verify SerialSourceConfig rejects invalid parameters."""
        with pytest.raises(ValueError, match="Serial port must be a non-empty string"):
            SerialSourceConfig(port="")
        with pytest.raises(ValueError, match="Serial port must be a non-empty string"):
            SerialSourceConfig(port="   ")
        with pytest.raises(ValueError, match="Serial baud rate must be a positive integer"):
            SerialSourceConfig(port="COM4", baud_rate=-1)
        with pytest.raises(ValueError, match="Serial timeout must be positive"):
            SerialSourceConfig(port="COM4", timeout=0.0)

    def test_convenience_constructor_without_config_object(self) -> None:
        """Verify constructing SerialCSISource with direct keyword arguments."""
        source = SerialCSISource(port="COM5", baud_rate=115200, timeout=2.0)
        assert source.config.port == "COM5"
        assert source.config.baud_rate == 115200
        assert source.config.timeout == 2.0

        with pytest.raises(ValueError, match="Either config or port must be provided"):
            SerialCSISource()

    def test_lifecycle_open_close_idempotence(self) -> None:
        """Verify open() and close() are idempotent and track is_open correctly."""
        fake_ser = FakeSerial()
        source = SerialCSISource(
            port="COM4",
            serial_factory=lambda port, baud, timeout: fake_ser,
        )

        assert not source.is_open
        source.open()
        assert source.is_open
        source.open()  # idempotent
        assert source.is_open

        source.close()
        assert not source.is_open
        source.close()  # idempotent
        assert not source.is_open

    def test_context_manager(self) -> None:
        """Verify context manager interface opens and closes source."""
        fake_ser = FakeSerial()
        source = SerialCSISource(
            port="COM4",
            serial_factory=lambda port, baud, timeout: fake_ser,
        )

        with source as src:
            assert src.is_open
        assert not source.is_open

    def test_read_well_formed_frames(self) -> None:
        """Verify SerialCSISource parses multiple valid frames with correct fields."""
        # 4 subcarriers with known I/Q pairs:
        # (3, 4) -> 5.0, (-5, 12) -> 13.0, (0, 7) -> 7.0, (8, -15) -> 17.0
        payload = bytes([3, 4, -5 & 0xFF, 12, 0, 7, 8, -15 & 0xFF])
        f0 = build_frame(payload=payload, seq_num=0, rssi=-45, channel=11)
        f1 = build_frame(payload=payload, seq_num=1, rssi=-46, channel=11)
        f2 = build_frame(payload=payload, seq_num=2, rssi=-47, channel=11)

        fake_ser = FakeSerial(initial_data=f0 + f1 + f2)
        source = SerialCSISource(
            port="COM4",
            serial_factory=lambda port, baud, timeout: fake_ser,
        )

        with source:
            r0 = source.read_record()
            assert r0 is not None
            assert r0.sequence_number == 0
            assert r0.subcarrier_count == 4
            assert r0.rssi == -45.0
            assert r0.channel == 11
            assert r0.phases is None
            assert pytest.approx(r0.amplitudes[0]) == 5.0
            assert pytest.approx(r0.amplitudes[1]) == 13.0
            assert pytest.approx(r0.amplitudes[2]) == 7.0
            assert pytest.approx(r0.amplitudes[3]) == 17.0

            r1 = source.read_record()
            assert r1 is not None
            assert r1.sequence_number == 1

            r2 = source.read_record()
            assert r2 is not None
            assert r2.sequence_number == 2

            assert source.valid_frame_count == 3
            assert source.corrupt_frame_count == 0

    def test_host_timestamp_convention(self) -> None:
        """Verify record.timestamp strictly reflects host epoch time (time.time())."""
        payload = bytes([2, 2, 2, 2])
        # Device timestamps are in microseconds / milliseconds from boot
        frame_bytes = build_frame(
            payload=payload,
            seq_num=99,
            rx_timestamp_us=42000000,  # 42 seconds from boot
            sender_ts_ms=42000,
        )

        fake_ser = FakeSerial(initial_data=frame_bytes)
        source = SerialCSISource(
            port="COM4",
            serial_factory=lambda port, baud, timeout: fake_ser,
        )

        before = time.time()
        with source:
            record = source.read_record()
        after = time.time()

        assert record is not None
        assert before <= record.timestamp <= after
        # Must definitely NOT be the device boot timestamp
        assert record.timestamp != 42000000
        assert record.timestamp != 42000

    def test_corrupted_frame_resync_and_loss_counting(self) -> None:
        """Verify corrupted frame is skipped, counted in corrupt_frame_count, and resynced."""
        payload = bytes([4, 3, 12, 5])
        f0 = build_frame(payload=payload, seq_num=10)
        # Corrupted frame in middle: bad CRC
        f1_bad = build_frame(payload=payload, seq_num=11, corrupt_crc=True)
        f2 = build_frame(payload=payload, seq_num=12)

        fake_ser = FakeSerial(initial_data=f0 + f1_bad + f2)
        source = SerialCSISource(
            port="COM4",
            serial_factory=lambda port, baud, timeout: fake_ser,
        )

        with source:
            r0 = source.read_record()
            assert r0 is not None
            assert r0.sequence_number == 10

            # Next read skips f1_bad, increments corrupt_frame_count, and returns f2
            r2 = source.read_record()
            assert r2 is not None
            assert r2.sequence_number == 12

            assert source.valid_frame_count == 2
            assert source.corrupt_frame_count == 1

    def test_mid_stream_subcarrier_count_mismatch_raises_error(self) -> None:
        """Verify unexpected subcarrier count change mid-stream raises ValueError."""
        payload_4sc = bytes([1, 1, 2, 2, 3, 3, 4, 4])  # 4 subcarriers (8 bytes)
        payload_2sc = bytes([1, 1, 2, 2])              # 2 subcarriers (4 bytes)

        f0 = build_frame(payload=payload_4sc, seq_num=1)
        f1 = build_frame(payload=payload_2sc, seq_num=2)

        fake_ser = FakeSerial(initial_data=f0 + f1)
        source = SerialCSISource(
            port="COM4",
            serial_factory=lambda port, baud, timeout: fake_ser,
        )

        with source:
            r0 = source.read_record()
            assert r0 is not None
            assert r0.subcarrier_count == 4

            with pytest.raises(
                ValueError,
                match="Inconsistent subcarrier count mid-stream: expected 4, got 2",
            ):
                source.read_record()

    def test_records_iterator_and_stream_completion(self) -> None:
        """Verify records() yields all available records and terminates cleanly on EOF."""
        payload = bytes([5, 5, 5, 5])
        frames = b"".join(build_frame(payload=payload, seq_num=i) for i in range(5))

        fake_ser = FakeSerial(initial_data=frames)
        fake_ser.mark_eof()

        source = SerialCSISource(
            port="COM4",
            serial_factory=lambda port, baud, timeout: fake_ser,
        )

        with source:
            collected = list(source.records())

        assert len(collected) == 5
        assert [r.sequence_number for r in collected] == [0, 1, 2, 3, 4]

    def test_read_record_when_closed_raises_runtime_error(self) -> None:
        """Verify reading from a closed source raises RuntimeError."""
        source = SerialCSISource(port="COM4")
        with pytest.raises(RuntimeError, match="SerialCSISource is not open"):
            source.read_record()

    def test_read_record_timeout(self) -> None:
        """Verify read_record returns None when timeout expires on empty stream."""
        fake_ser = FakeSerial(initial_data=b"")
        source = SerialCSISource(
            port="COM4",
            timeout=0.05,
            serial_factory=lambda port, baud, timeout: fake_ser,
        )
        with source:
            # Explicit timeout
            rec = source.read_record(timeout=0.05)
            assert rec is None

            # timeout=None uses config.timeout (0.05s) rather than hanging indefinitely
            rec_default = source.read_record(timeout=None)
            assert rec_default is None

    def test_application_hardware_mode_run(self) -> None:
        """Verify Application running with csi_source_mode='hardware' ingests frames cleanly."""
        from wisense_backend.app import Application
        from wisense_backend.config.app_config import AppConfig

        payload = bytes([3, 4, 0, 5])  # 2 subcarriers
        frames = b"".join(build_frame(payload=payload, seq_num=i) for i in range(10))

        fake_ser = FakeSerial(initial_data=frames)
        fake_ser.mark_eof()

        cfg = AppConfig(
            csi_source_mode="hardware",
            serial_port="COM4",
            serial_baud_rate=921600,
            motion_config_path="config/motion_detection_config.json",
            log_level="INFO",
        )
        app = Application(config=cfg)
        # Inject fake serial backend into source
        app.source._serial_factory = lambda port, baud, timeout: fake_ser

        records_processed = app.run()
        assert records_processed == 10
        assert not app.source.is_open
