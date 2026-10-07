"""Variance-based spatial feature extractor for WiSense-Twin.

Calculates subcarrier-wise temporal variance and total aggregate energy over a
sliding window of cleaned CSI frames.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

import numpy as np

from wisense_backend.contracts.csi_source import CSIRecord
from wisense_backend.contracts.feature_extractor import FeatureExtractor, FeatureVector
from wisense_backend.features.sliding_window import SlidingWindowBuffer, WindowConfig


@dataclass(frozen=True)
class VarianceExtractorConfig:
    """Configuration for the variance feature extractor.

    Attributes:
        window_size_samples: Total number of frames per window (must be >= 2).
        window_step_samples: Frame step between emitted feature vectors (must be >= 1).
        emit_partial: Whether to emit feature vectors before the window buffer is full.
        ddof: Delta degrees of freedom for variance divisor (N - ddof). Default is 1 (unbiased sample variance).
    """

    window_size_samples: int = 20
    window_step_samples: int = 1
    emit_partial: bool = False
    ddof: int = 1

    def __post_init__(self) -> None:
        if self.window_size_samples < 2:
            raise ValueError(
                f"window_size_samples must be >= 2, got {self.window_size_samples}"
            )
        if self.window_step_samples < 1:
            raise ValueError(
                f"window_step_samples must be >= 1, got {self.window_step_samples}"
            )
        if self.ddof < 0:
            raise ValueError(f"ddof must be non-negative, got {self.ddof}")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VarianceExtractorConfig:
        return cls(
            window_size_samples=int(data.get("window_size_samples", 20)),
            window_step_samples=int(data.get("window_step_samples", 1)),
            emit_partial=bool(data.get("emit_partial", False)),
            ddof=int(data.get("ddof", 1)),
        )

    @classmethod
    def from_json_file(cls, file_path: str | Path) -> VarianceExtractorConfig:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Feature extraction configuration not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)


class VarianceFeatureExtractor(FeatureExtractor):
    """Computes windowed temporal variance per subcarrier and aggregate spatial variance.

    Conforms to the `FeatureExtractor` protocol. Employs `SlidingWindowBuffer` for
    temporal aggregation, enforcing the configured partial-window policy.
    """

    def __init__(self, config: Optional[VarianceExtractorConfig] = None) -> None:
        self.config = config if config is not None else VarianceExtractorConfig()
        window_cfg = WindowConfig(
            window_size_samples=self.config.window_size_samples,
            window_step_samples=self.config.window_step_samples,
            emit_partial=self.config.emit_partial,
        )
        self._buffer = SlidingWindowBuffer(window_cfg)

    def reset(self) -> None:
        """Reset internal sliding window buffer state."""
        self._buffer.reset()

    def extract(self, window: Sequence[CSIRecord]) -> FeatureVector:
        """Compute FeatureVector directly from a pre-buffered window of CSIRecords.

        Args:
            window: Sequence of at least 1 `CSIRecord`.

        Returns:
            Calculated `FeatureVector`.
        """
        if not window:
            raise ValueError("Cannot extract features from an empty window.")

        sample_count = len(window)
        subcarrier_count = window[0].subcarrier_count

        # Stack amplitudes into 2D array: shape (sample_count, subcarrier_count)
        amp_matrix = np.array([rec.amplitudes for rec in window], dtype=np.float64)

        # Handle degree-of-freedom boundary if sample_count <= ddof
        effective_ddof = self.config.ddof if sample_count > self.config.ddof else 0

        # Compute variance across time (axis 0) for each subcarrier
        variance_array = np.var(amp_matrix, axis=0, ddof=effective_ddof)

        # Aggregate variance: mean across all subcarriers
        aggregate_var = float(np.mean(variance_array))

        return FeatureVector(
            window_start_timestamp=window[0].timestamp,
            window_end_timestamp=window[-1].timestamp,
            sample_count=sample_count,
            subcarrier_count=subcarrier_count,
            variance_per_subcarrier=tuple(float(v) for v in variance_array),
            aggregate_variance=aggregate_var,
        )

    def process(self, record: CSIRecord) -> Optional[FeatureVector]:
        """Ingest a single CSIRecord and return a FeatureVector if window step triggers."""
        ready_window = self._buffer.add_record(record)
        if ready_window is not None:
            return self.extract(ready_window)
        return None

    def process_stream(self, records: Iterable[CSIRecord]) -> Iterator[FeatureVector]:
        """Stream transformation helper yielding FeatureVectors as windows trigger."""
        for record in records:
            feature = self.process(record)
            if feature is not None:
                yield feature
