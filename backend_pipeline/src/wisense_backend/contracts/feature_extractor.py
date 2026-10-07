"""Feature extraction contracts and data structures for WiSense-Twin.

This module defines `FeatureVector` (the immutable representation of spatial RF features
extracted from a temporal window of CSI frames) and `FeatureExtractor` (the shared
protocol implemented by all algorithmic feature extractors).

Design Principles:
- Ground-Truth Isolation: FeatureVector contains NO zone IDs, room labels, or motion
  classifications. Feature extraction is strictly zone-agnostic; mapping features to
  spatial zones is exclusively the responsibility of zone classification (Section 8).
- Interchangeable Extractor Interface: All feature extractors sit behind the
  `FeatureExtractor` protocol, enabling future spectral/Doppler extractors to drop in
  without modifying upstream sources or downstream classifiers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional, Protocol, Sequence, runtime_checkable

from wisense_backend.contracts.csi_source import CSIRecord


@dataclass(frozen=True, slots=True)
class FeatureVector:
    """Immutable representation of extracted spatial features over a CSI frame window.

    Attributes:
        window_start_timestamp: UTC Unix epoch seconds (time.time()) of the earliest
            frame in this analysis window.
        window_end_timestamp: UTC Unix epoch seconds (time.time()) of the most recent
            frame in this analysis window.
        sample_count: Actual number of CSI records contributing to this window calculation.
        subcarrier_count: Total number of OFDM subcarriers analyzed.
        variance_per_subcarrier: Tuple of variance values, one per subcarrier.
        aggregate_variance: Scalar energy metric summarizing total perturbation across all subcarriers.
    """

    window_start_timestamp: float
    window_end_timestamp: float
    sample_count: int
    subcarrier_count: int
    variance_per_subcarrier: tuple[float, ...]
    aggregate_variance: float

    def __post_init__(self) -> None:
        """Validate invariant constraints on feature vector attributes."""
        if self.window_start_timestamp <= 0.0:
            raise ValueError(
                f"window_start_timestamp must be positive, got {self.window_start_timestamp}"
            )
        if self.window_end_timestamp < self.window_start_timestamp:
            raise ValueError(
                f"window_end_timestamp ({self.window_end_timestamp}) must be >= "
                f"window_start_timestamp ({self.window_start_timestamp})"
            )
        if self.sample_count < 1:
            raise ValueError(f"sample_count must be >= 1, got {self.sample_count}")
        if self.subcarrier_count < 1:
            raise ValueError(f"subcarrier_count must be >= 1, got {self.subcarrier_count}")
        if len(self.variance_per_subcarrier) != self.subcarrier_count:
            raise ValueError(
                f"variance_per_subcarrier length ({len(self.variance_per_subcarrier)}) "
                f"does not match subcarrier_count ({self.subcarrier_count})"
            )
        for idx, var_val in enumerate(self.variance_per_subcarrier):
            if not math.isfinite(var_val) or var_val < 0.0:
                raise ValueError(
                    f"variance_per_subcarrier[{idx}] must be a finite non-negative number, got {var_val}"
                )
        if not math.isfinite(self.aggregate_variance) or self.aggregate_variance < 0.0:
            raise ValueError(
                f"aggregate_variance must be a finite non-negative number, got {self.aggregate_variance}"
            )


@runtime_checkable
class FeatureExtractor(Protocol):
    """Abstract protocol for feature extractors consuming CSI frame streams.

    Extractors process sequential `CSIRecord` frames (or discrete windows of them)
    and produce `FeatureVector` instances.
    """

    def process(self, record: CSIRecord) -> Optional[FeatureVector]:
        """Ingest a single CSIRecord.

        Args:
            record: Cleaned `CSIRecord` from the preprocessing pipeline.

        Returns:
            A `FeatureVector` if an analysis window completed and step trigger fired,
            or `None` if buffering / waiting for the next step.
        """
        ...

    def extract(self, window: Sequence[CSIRecord]) -> FeatureVector:
        """Compute a FeatureVector directly from a pre-buffered window of records.

        Args:
            window: Sequence of `CSIRecord` instances.

        Returns:
            Computed `FeatureVector`.
        """
        ...

    def reset(self) -> None:
        """Reset internal rolling buffers and step counters."""
        ...

    def process_stream(self, records: Iterable[CSIRecord]) -> Iterator[FeatureVector]:
        """Stream transformation helper yielding FeatureVectors as windows complete.

        Args:
            records: Stream of `CSIRecord` instances.

        Yields:
            `FeatureVector` instances as each window step triggers.
        """
        ...
