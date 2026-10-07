"""Configurable sliding window buffer for CSI record streams.

This module implements `SlidingWindowBuffer`, providing temporal aggregation of
sequential `CSIRecord` frames across OFDM subcarriers.

LATENCY BUDGET & AUDIT ITEM #14:
--------------------------------
This buffering step represents the primary physical source of system detection latency
in the WiSense-Twin pipeline (Audit Item #14). At sampling frequency f_s (e.g. 20 Hz)
and window size W (e.g. 20 samples), the pipeline MUST accumulate frames for:

    Latency_floor = W / f_s = 20 / 20.0 = 1.0 second

before the initial FeatureVector can be emitted. Downstream motion detectors and
zone classifiers CANNOT respond faster than this window accumulation period.
Once filled, subsequent FeatureVectors are emitted every S / f_s seconds
(where S is window_step_samples; e.g. S=1 => new feature vector every 50ms).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

from wisense_backend.contracts.csi_source import CSIRecord


@dataclass(frozen=True)
class WindowConfig:
    """Configuration for temporal sliding window buffering.

    Attributes:
        window_size_samples: Total number of frames in a complete analysis window (must be >= 2).
        window_step_samples: Number of new incoming frames required before emitting the
            next window (must be >= 1). A step of 1 produces a continuous sliding window.
        emit_partial: If True, emits partial windows before window_size_samples are accumulated.
            Defaults to False (recommended) to prevent erratic variance estimation on small N.
    """

    window_size_samples: int = 20
    window_step_samples: int = 1
    emit_partial: bool = False

    def __post_init__(self) -> None:
        if self.window_size_samples < 2:
            raise ValueError(
                f"window_size_samples must be >= 2 for variance calculation, got {self.window_size_samples}"
            )
        if self.window_step_samples < 1:
            raise ValueError(
                f"window_step_samples must be >= 1, got {self.window_step_samples}"
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WindowConfig:
        return cls(
            window_size_samples=int(data.get("window_size_samples", 20)),
            window_step_samples=int(data.get("window_step_samples", 1)),
            emit_partial=bool(data.get("emit_partial", False)),
        )


class SlidingWindowBuffer:
    """Sliding temporal buffer aggregating sequential CSIRecords into analysis windows.

    Partial Window Policy:
    ----------------------
    By default (`emit_partial=False`), this buffer emits `None` until at least
    `window_size_samples` frames have been buffered. Computing variance over
    very few frames (e.g. N=2 or N=3) yields mathematically degenerate, high-variance
    noise estimates that would trigger spurious false alarms in downstream motion
    classifiers.
    """

    def __init__(self, config: Optional[WindowConfig] = None) -> None:
        self.config = config if config is not None else WindowConfig()
        self._buffer: deque[CSIRecord] = deque(maxlen=self.config.window_size_samples)
        self._samples_since_last_emit: int = 0
        self._has_emitted_full_window: bool = False

    @property
    def is_full(self) -> bool:
        """Return True if the buffer has reached configured window_size_samples."""
        return len(self._buffer) >= self.config.window_size_samples

    @property
    def sample_count(self) -> int:
        """Number of records currently held in the window buffer."""
        return len(self._buffer)

    def get_current_window(self) -> tuple[CSIRecord, ...]:
        """Return snapshot of records currently held in the buffer."""
        return tuple(self._buffer)

    def reset(self) -> None:
        """Clear all buffered records and reset step counters."""
        self._buffer.clear()
        self._samples_since_last_emit = 0
        self._has_emitted_full_window = False

    def add_record(self, record: CSIRecord) -> Optional[tuple[CSIRecord, ...]]:
        """Ingest a new record into the buffer and return a window if ready.

        Args:
            record: Cleaned `CSIRecord` from preprocessing.

        Returns:
            A tuple of `CSIRecord` instances if window readiness and step conditions
            are satisfied, or `None` if buffering.
        """
        # Validate subcarrier consistency
        if self._buffer and record.subcarrier_count != self._buffer[0].subcarrier_count:
            raise ValueError(
                f"Subcarrier count mismatch: incoming record has {record.subcarrier_count}, "
                f"buffered window expects {self._buffer[0].subcarrier_count}"
            )

        self._buffer.append(record)
        self._samples_since_last_emit += 1

        # Policy 1: Emit partial windows before full buffer
        if self.config.emit_partial and not self.is_full:
            if self._samples_since_last_emit >= self.config.window_step_samples:
                self._samples_since_last_emit = 0
                return tuple(self._buffer)
            return None

        # Policy 2: Emit nothing until buffer is completely full
        if not self.is_full:
            return None

        # Buffer is full (len == window_size_samples)
        if not self._has_emitted_full_window:
            # First time reaching full capacity: emit immediately
            self._has_emitted_full_window = True
            self._samples_since_last_emit = 0
            return tuple(self._buffer)

        # Subsequent steps: emit once window_step_samples have been received
        if self._samples_since_last_emit >= self.config.window_step_samples:
            self._samples_since_last_emit = 0
            return tuple(self._buffer)

        return None
