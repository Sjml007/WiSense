"""CSI data source interface and record definition.

This module establishes the canonical data structure for Channel State Information (CSI)
frames and the protocol interface (`CSIRecordSource`) implemented by both synthetic
generators and real hardware ingestion drivers (e.g., serial readers).

Downstream pipeline components (preprocessing, feature extraction, zone classification)
consume `CSIRecord` instances exclusively through `CSIRecordSource`, ensuring seamless
interchangeability between synthetic test beds and physical hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class CSIRecord:
    """Immutable representation of a single captured CSI frame.

    Attributes:
        timestamp: UTC Unix epoch timestamp in seconds (float, e.g. time.time()) when the
            frame was acquired. Must strictly follow UTC Unix epoch seconds; relative or ad-hoc
            clocks are not permitted.
        subcarrier_count: Total number of OFDM subcarriers captured in this frame.
        amplitudes: Tuple of amplitude magnitudes across all subcarriers. Primary CSI feature.
        phases: Optional tuple of phase angles in radians across all subcarriers.
            Kept optional/disableable as phase unwrapping is experimental.
        rssi: Optional Received Signal Strength Indicator (in dBm) associated with the packet.
        channel: Optional WiFi RF channel index (e.g., 1, 6, 11, 36).
        sequence_number: Optional monotonic packet counter from transmitter/receiver firmware.
    """

    timestamp: float
    subcarrier_count: int
    amplitudes: tuple[float, ...]
    phases: Optional[tuple[float, ...]] = None
    rssi: Optional[float] = None
    channel: Optional[int] = None
    sequence_number: Optional[int] = None

    def __post_init__(self) -> None:
        """Validate invariant constraints on record attributes."""
        if self.timestamp <= 0.0:
            raise ValueError(
                f"timestamp must be a positive UTC Unix epoch timestamp in seconds, got {self.timestamp}"
            )
        if self.subcarrier_count <= 0:
            raise ValueError(f"subcarrier_count must be positive, got {self.subcarrier_count}")
        if len(self.amplitudes) != self.subcarrier_count:
            raise ValueError(
                f"amplitudes length ({len(self.amplitudes)}) does not match "
                f"subcarrier_count ({self.subcarrier_count})"
            )
        if self.phases is not None and len(self.phases) != self.subcarrier_count:
            raise ValueError(
                f"phases length ({len(self.phases)}) does not match "
                f"subcarrier_count ({self.subcarrier_count})"
            )


@runtime_checkable
class CSIRecordSource(Protocol):
    """Abstract protocol for any CSI frame producer.

    Both synthetic replay engines and real hardware UART/serial drivers must
    conform to this protocol. Consumers rely solely on these methods without
    coupling to concrete implementation details.
    """

    def open(self) -> None:
        """Initialize and open the CSI stream.

        Allocates communication ports, starts background timing mechanisms,
        or opens scenario files. Idempotent if already opened.
        """
        ...

    def close(self) -> None:
        """Gracefully terminate the CSI stream and release all resources.

        Closes open serial handles, cancels timers, and flushes buffers.
        Idempotent if already closed.
        """
        ...

    @property
    def is_open(self) -> bool:
        """Return True if the source is currently open and ready for reading."""
        ...

    def read_record(self, timeout: Optional[float] = None) -> Optional[CSIRecord]:
        """Read the next available CSI frame.

        Args:
            timeout: Maximum seconds to block waiting for the next frame.
                If None, blocks until a record is available or stream ends.
                If 0.0, returns immediately with None if no record is ready.

        Returns:
            The next `CSIRecord`, or `None` if the timeout elapsed or the
            underlying stream reached EOF/closed.
        """
        ...

    def records(self) -> Iterator[CSIRecord]:
        """Provide an iterator yielding CSI records sequentially until stream completion.

        Yields:
            `CSIRecord` instances as they become available.
        """
        ...

    def __iter__(self) -> Iterator[CSIRecord]:
        """Allow the source to be iterated over directly using standard `for` loops."""
        ...

    def __enter__(self) -> CSIRecordSource:
        """Context management entry: opens the source."""
        ...

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Context management exit: safely closes the source."""
        ...
