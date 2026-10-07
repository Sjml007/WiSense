"""Serial-based CSI record source implementation for physical ESP32 transceivers.

Implements the canonical `CSIRecordSource` protocol using real UART binary streaming
from the `rx_csi_collector` firmware (Section 14).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any, Callable, Iterator, Optional

from wisense_backend.contracts.csi_source import CSIRecord, CSIRecordSource
from wisense_backend.ingestion.frame_parser import CSIFrame, CSIFrameParser

logger = logging.getLogger("wisense_backend.ingestion.serial_source")


@dataclass(frozen=True)
class SerialSourceConfig:
    """Configuration for SerialCSISource hardware reader.

    Attributes:
        port: Communication port name (e.g. 'COM4' or '/dev/ttyUSB0').
            Required, no default per the project's 'no hardcoded COM ports' rule.
        baud_rate: Serial UART baud rate (default: 921600 matching rx_csi_collector).
        timeout: Read/resync timeout in seconds (default: 1.0).
    """

    port: str
    baud_rate: int = 921600
    timeout: float = 1.0

    def __post_init__(self) -> None:
        """Validate invariant configuration constraints."""
        if not self.port or not isinstance(self.port, str) or not self.port.strip():
            raise ValueError("Serial port must be a non-empty string")
        if not isinstance(self.baud_rate, int) or self.baud_rate <= 0:
            raise ValueError(
                f"Serial baud rate must be a positive integer, got {self.baud_rate}"
            )
        if not isinstance(self.timeout, (int, float)) or self.timeout <= 0:
            raise ValueError(f"Serial timeout must be positive, got {self.timeout}")


class SerialCSISource(CSIRecordSource):
    """Hardware CSI frame producer consuming binary frames over serial UART.

    Conforms strictly to the `CSIRecordSource` protocol. Captures host receipt
    epoch timestamps, validates subcarrier session consistency, computes
    subcarrier amplitudes via hypot(I, Q), and provides robust resynchronization
    over corrupted or torn frames.
    """

    def __init__(
        self,
        config: Optional[SerialSourceConfig] = None,
        *,
        port: Optional[str] = None,
        baud_rate: int = 921600,
        timeout: float = 1.0,
        serial_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        """Initialize the SerialCSISource instance.

        Args:
            config: Optional SerialSourceConfig instance.
            port: Serial port name if config is not provided directly.
            baud_rate: Baud rate if config is not provided directly (default: 921600).
            timeout: Read timeout in seconds (default: 1.0).
            serial_factory: Optional callable returning a serial port object (useful for testing/mocking).

        Raises:
            ValueError: If neither config nor port is specified, or parameters fail validation.
        """
        if config is not None:
            self._config = config
        elif port is not None:
            self._config = SerialSourceConfig(port=port, baud_rate=baud_rate, timeout=timeout)
        else:
            raise ValueError("Either config or port must be provided for SerialCSISource")

        self._serial_factory = serial_factory
        self._serial: Any = None
        self._is_open: bool = False
        self._parser = CSIFrameParser()
        self._session_subcarrier_count: Optional[int] = None
        self._last_frame: Optional[CSIFrame] = None
        self._read_chunk_size: int = 4096

    @property
    def config(self) -> SerialSourceConfig:
        """Return the immutable source configuration."""
        return self._config

    @property
    def is_open(self) -> bool:
        """Return True if the serial stream is currently open."""
        return self._is_open

    @property
    def corrupt_frame_count(self) -> int:
        """Total count of corrupted/torn frames encountered and skipped."""
        return self._parser.corrupt_frame_count

    @property
    def valid_frame_count(self) -> int:
        """Total count of successfully parsed and verified frames."""
        return self._parser.valid_frame_count

    @property
    def last_frame(self) -> Optional[CSIFrame]:
        """Return the most recently parsed raw CSIFrame, or None."""
        return self._last_frame

    @property
    def session_subcarrier_count(self) -> Optional[int]:
        """Return the active subcarrier count established for this session."""
        return self._session_subcarrier_count

    def open(self) -> None:
        """Open the serial port and initialize the parser state.

        Idempotent if already opened.
        """
        if self._is_open:
            return

        logger.info(
            "Opening serial CSI source on %s @ %d baud (timeout=%.2fs)",
            self._config.port,
            self._config.baud_rate,
            self._config.timeout,
        )

        if self._serial_factory is not None:
            self._serial = self._serial_factory(
                self._config.port,
                self._config.baud_rate,
                min(0.1, self._config.timeout),
            )
        else:
            try:
                import serial  # type: ignore
            except ImportError as exc:
                raise ImportError(
                    "pyserial is required for SerialCSISource. Run: pip install pyserial"
                ) from exc

            self._serial = serial.Serial(
                port=self._config.port,
                baudrate=self._config.baud_rate,
                timeout=min(0.1, self._config.timeout),
            )

        self._parser.clear()
        self._session_subcarrier_count = None
        self._last_frame = None
        self._is_open = True
        logger.info("Serial CSI source opened successfully on %s", self._config.port)

    def close(self) -> None:
        """Close the serial port and release all system resources.

        Idempotent if already closed.
        """
        if not self._is_open:
            return

        logger.info(
            "Closing serial CSI source on %s (valid_frames=%d, corrupt_frames=%d)",
            self._config.port,
            self._parser.valid_frame_count,
            self._parser.corrupt_frame_count,
        )
        self._is_open = False

        if self._serial is not None:
            try:
                self._serial.close()
            except Exception as exc:
                logger.debug("Exception closing serial port: %s", exc)
            self._serial = None

        self._session_subcarrier_count = None

    def _process_frame(self, frame: CSIFrame) -> CSIRecord:
        """Validate session subcarrier count and convert frame to CSIRecord.

        Raises:
            ValueError: If subcarrier_count changes unexpectedly mid-stream.
        """
        if self._session_subcarrier_count is None:
            self._session_subcarrier_count = frame.subcarrier_count
        elif frame.subcarrier_count != self._session_subcarrier_count:
            raise ValueError(
                f"Inconsistent subcarrier count mid-stream: "
                f"expected {self._session_subcarrier_count}, got {frame.subcarrier_count}"
            )

        self._last_frame = frame
        return frame.to_csi_record()

    def read_record(self, timeout: Optional[float] = None) -> Optional[CSIRecord]:
        """Read the next available CSI frame from the serial stream.

        Args:
            timeout: Maximum seconds to block waiting for the next valid frame.
                If None, intentionally falls back to `self.config.timeout` (default 1.0s)
                rather than blocking indefinitely. This is an intentional, scoped deviation
                from the CSIRecordSource protocol: on real hardware, a dead, disconnected,
                or stalled transmitter does not signal EOF, so an unbounded wait would
                hang the calling thread forever. If 0.0, checks buffer non-blockingly.

        Returns:
            The next `CSIRecord`, or `None` if the timeout elapsed or EOF/close.

        Raises:
            RuntimeError: If called while the source is closed.
            ValueError: If subcarrier_count changes mid-stream.
        """
        if not self._is_open:
            raise RuntimeError("SerialCSISource is not open. Call open() first.")

        # 1. Check if a valid frame is already pending in the parser buffer
        frame = self._parser.parse_next()
        if frame is not None:
            return self._process_frame(frame)

        # 2. Determine effective deadline
        effective_timeout = timeout if timeout is not None else self._config.timeout
        start_time = time.monotonic()

        while self._is_open:
            # Check elapsed timeout
            if effective_timeout is not None:
                elapsed = time.monotonic() - start_time
                if elapsed >= effective_timeout:
                    return None

            # Read chunk from serial
            try:
                chunk = self._serial.read(self._read_chunk_size)
            except Exception as exc:
                logger.warning("Error reading from serial port %s: %s", self._config.port, exc)
                self.close()
                return None

            if chunk:
                self._parser.feed(chunk)
                frame = self._parser.parse_next()
                if frame is not None:
                    return self._process_frame(frame)
            else:
                # Serial read returned 0 bytes (serial timeout or EOF)
                if hasattr(self._serial, "is_open") and not self._serial.is_open:
                    return None
                if getattr(self._serial, "eof", False):
                    return None
                if effective_timeout is not None and (time.monotonic() - start_time >= effective_timeout):
                    return None

        return None

    def records(self) -> Iterator[CSIRecord]:
        """Yield CSI records sequentially until stream completion or closure."""
        if not self._is_open:
            self.open()

        while self._is_open:
            record = self.read_record()
            if record is None:
                # If stream was closed or encountered EOF, terminate iteration
                if not self._is_open or getattr(self._serial, "eof", False):
                    break
                # On timeout without EOF/close, continue waiting unless stop was requested
                continue
            yield record

    def __iter__(self) -> Iterator[CSIRecord]:
        return self.records()

    def __enter__(self) -> SerialCSISource:
        self.open()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()
