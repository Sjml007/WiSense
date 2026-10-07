"""Moving Average filter implementation for high-frequency CSI noise smoothing.

A causal sliding-window moving average filter that attenuates high-frequency noise
while preserving baseline amplitude trends across individual OFDM subcarriers.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional

import numpy as np

from wisense_backend.contracts.csi_source import CSIRecord
from wisense_backend.contracts.preprocessor import Preprocessor


@dataclass(frozen=True)
class MovingAverageConfig:
    """Configuration parameters for the Moving Average filter.

    Attributes:
        window_size: Number of consecutive samples to average (must be >= 1).
            A window_size of 1 acts as an identity pass-through.
        filter_phases: If True, also smooths phase angles if present in the record.
            Kept optional/disabled by default per project constraints.
    """

    window_size: int = 5
    filter_phases: bool = False

    def __post_init__(self) -> None:
        if self.window_size < 1:
            raise ValueError(f"window_size must be >= 1, got {self.window_size}")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MovingAverageConfig:
        return cls(
            window_size=int(data.get("window_size", 5)),
            filter_phases=bool(data.get("filter_phases", False)),
        )


class MovingAverageFilter(Preprocessor):
    r"""Causal rolling-window Moving Average filter for multi-subcarrier CSI records.

    Maintains a rolling buffer of up to `window_size` samples per subcarrier.
    At stream start (when fewer than `window_size` samples have been observed),
    averages over the available samples seen so far ($1 \le k \le W$), ensuring
    zero latency and no dropped frames.
    """

    def __init__(self, config: Optional[MovingAverageConfig] = None) -> None:
        self.config = config if config is not None else MovingAverageConfig()
        self._amplitude_history: deque[np.ndarray] = deque(maxlen=self.config.window_size)
        self._phase_history: deque[np.ndarray] = deque(maxlen=self.config.window_size)

    def reset(self) -> None:
        """Clear rolling sample buffers."""
        self._amplitude_history.clear()
        self._phase_history.clear()

    def _filter_array(self, current: np.ndarray, history: deque[np.ndarray]) -> np.ndarray:
        """Append to rolling buffer and compute mean across time axis."""
        history.append(current)
        # Fast path for window_size == 1
        if self.config.window_size == 1:
            return current.copy()
        # Compute mean across history buffer (shape: (len, N_subcarriers))
        return np.mean(history, axis=0)

    def process(self, record: CSIRecord) -> CSIRecord:
        """Filter a single CSIRecord and return the smoothed record."""
        amp_array = np.asarray(record.amplitudes, dtype=np.float64)
        smoothed_amps = self._filter_array(amp_array, self._amplitude_history)

        smoothed_phases = record.phases
        if self.config.filter_phases and record.phases is not None:
            phase_array = np.asarray(record.phases, dtype=np.float64)
            smoothed_phase_array = self._filter_array(phase_array, self._phase_history)
            smoothed_phases = tuple(float(p) for p in smoothed_phase_array)

        return CSIRecord(
            timestamp=record.timestamp,
            subcarrier_count=record.subcarrier_count,
            amplitudes=tuple(float(a) for a in smoothed_amps),
            phases=smoothed_phases,
            rssi=record.rssi,
            channel=record.channel,
            sequence_number=record.sequence_number,
        )

    def process_stream(self, records: Iterable[CSIRecord]) -> Iterator[CSIRecord]:
        """Stream transformation helper yielding smoothed records."""
        for record in records:
            yield self.process(record)
