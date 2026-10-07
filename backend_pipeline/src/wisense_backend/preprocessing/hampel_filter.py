"""Hampel filter implementation for CSI outlier suppression.

The Hampel filter detects and suppresses impulse noise and transient outliers
across individual OFDM subcarriers by computing the median and Median Absolute
Deviation (MAD) over a rolling causal window.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional

import numpy as np

from wisense_backend.contracts.csi_source import CSIRecord
from wisense_backend.contracts.preprocessor import Preprocessor


@dataclass(frozen=True)
class HampelConfig:
    """Configuration parameters for the Hampel filter.

    Attributes:
        window_size: Number of consecutive samples in the rolling window (must be >= 3).
        threshold_multiplier: Sensitivity multiplier in units of MAD standard deviations (must be > 0.0).
            Higher values only catch extreme spikes; lower values catch subtler deviations.
        min_mad: Absolute noise floor threshold to prevent false-positive spike flags
            when the signal is completely flat (MAD == 0).
        filter_phases: If True, also filters phase angles if present in the record.
            Kept optional/disabled by default per project constraints.
    """

    window_size: int = 7
    threshold_multiplier: float = 3.0
    min_mad: float = 1e-6
    filter_phases: bool = False

    def __post_init__(self) -> None:
        if self.window_size < 3:
            raise ValueError(f"window_size must be >= 3, got {self.window_size}")
        if self.threshold_multiplier <= 0.0:
            raise ValueError(
                f"threshold_multiplier must be > 0.0, got {self.threshold_multiplier}"
            )
        if self.min_mad < 0.0:
            raise ValueError(f"min_mad must be non-negative, got {self.min_mad}")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HampelConfig:
        return cls(
            window_size=int(data.get("window_size", 7)),
            threshold_multiplier=float(data.get("threshold_multiplier", 3.0)),
            min_mad=float(data.get("min_mad", 1e-6)),
            filter_phases=bool(data.get("filter_phases", False)),
        )


class HampelFilter(Preprocessor):
    """Causal rolling-window Hampel filter for multi-subcarrier CSI records.

    For each subcarrier, tracks the last `window_size` samples. If the current sample
    deviates from the rolling median by more than `threshold_multiplier * 1.4826 * MAD`,
    it is considered an outlier and replaced with the rolling median.
    """

    # Normal distribution scale factor relating MAD to standard deviation
    MAD_SCALE_FACTOR: float = 1.4826

    def __init__(self, config: Optional[HampelConfig] = None) -> None:
        self.config = config if config is not None else HampelConfig()
        # Rolling buffers storing arrays of shape (N_subcarriers,)
        self._amplitude_history: deque[np.ndarray] = deque(maxlen=self.config.window_size)
        self._phase_history: deque[np.ndarray] = deque(maxlen=self.config.window_size)

    def reset(self) -> None:
        """Clear rolling sample buffers."""
        self._amplitude_history.clear()
        self._phase_history.clear()

    def _filter_array(self, current: np.ndarray, history: deque[np.ndarray]) -> np.ndarray:
        """Apply vectorized Hampel filtering across all subcarriers."""
        history.append(current)

        # At stream startup (< 3 samples), pass through unmodified
        if len(history) < 3:
            return current.copy()

        # Stack rolling window into 2D array: (window_len, N_subcarriers)
        window = np.array(history)

        # Compute median along time axis (axis 0)
        median = np.median(window, axis=0)

        # Compute Median Absolute Deviation (MAD)
        mad = np.median(np.abs(window - median), axis=0)
        scale = self.MAD_SCALE_FACTOR * mad

        # Compute nominal scale and floor with min_mad to handle flat signals with spikes
        scale_floor = np.maximum(scale, self.config.min_mad)
        threshold = self.config.threshold_multiplier * scale_floor

        # Absolute deviation of the current frame from the rolling median
        deviation = np.abs(current - median)

        # Identify outliers where deviation exceeds threshold
        is_outlier = deviation > threshold

        # Replace outliers with median; retain clean values
        cleaned = np.where(is_outlier, median, current)
        return cleaned

    def process(self, record: CSIRecord) -> CSIRecord:
        """Filter a single CSIRecord and return the cleaned record."""
        amp_array = np.asarray(record.amplitudes, dtype=np.float64)
        cleaned_amps = self._filter_array(amp_array, self._amplitude_history)

        cleaned_phases = record.phases
        if self.config.filter_phases and record.phases is not None:
            phase_array = np.asarray(record.phases, dtype=np.float64)
            cleaned_phase_array = self._filter_array(phase_array, self._phase_history)
            cleaned_phases = tuple(float(p) for p in cleaned_phase_array)

        return CSIRecord(
            timestamp=record.timestamp,
            subcarrier_count=record.subcarrier_count,
            amplitudes=tuple(float(a) for a in cleaned_amps),
            phases=cleaned_phases,
            rssi=record.rssi,
            channel=record.channel,
            sequence_number=record.sequence_number,
        )

    def process_stream(self, records: Iterable[CSIRecord]) -> Iterator[CSIRecord]:
        """Stream transformation helper yielding filtered records."""
        for record in records:
            yield self.process(record)
