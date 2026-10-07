"""Labeled feature sample definition for offline zone calibration.

CRITICAL DESIGN CONSTRAINT — GROUND-TRUTH ISOLATION:
---------------------------------------------------
`LabeledFeatureSample` lives strictly within the offline calibration module.
It is NEVER imported by or leaked into online pipeline stages (ingestion, preprocessing,
feature extraction, motion detection). The online `FeatureVector` contract remains
completely blind to ground truth, zone IDs, or room layout semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from wisense_backend.contracts.feature_extractor import FeatureVector


@dataclass(frozen=True, slots=True)
class LabeledFeatureSample:
    """A FeatureVector tagged with an offline ground-truth zone label.

    Attributes:
        feature_vector: The underlying zone-agnostic FeatureVector produced by feature extraction.
        zone_id: 1-indexed room zone identifier where human motion occurred during this window,
            or `None` if this window corresponds to an idle/quiet background baseline.
    """

    feature_vector: FeatureVector
    zone_id: Optional[int] = None

    def __post_init__(self) -> None:
        """Validate invariant constraints on LabeledFeatureSample."""
        if not isinstance(self.feature_vector, FeatureVector):
            raise TypeError(
                f"feature_vector must be an instance of FeatureVector, got {type(self.feature_vector).__name__}"
            )
        if self.zone_id is not None:
            if not isinstance(self.zone_id, int):
                raise TypeError(
                    f"zone_id must be an int or None, got {type(self.zone_id).__name__}"
                )
            if self.zone_id < 1:
                raise ValueError(
                    f"zone_id must be >= 1 (1-indexed), got {self.zone_id}"
                )
