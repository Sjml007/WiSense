"""Zone classification contracts and data structures for WiSense-Twin.

This module defines:
- `ZoneClassification`: The immutable representation of a spatial room zone classification
  derived from an analysis window, gated strictly by instantaneous motion detection.
- `ZoneClassifier`: The abstract protocol implemented by all zone classification algorithms.

DESIGN PRINCIPLES & AUDIT COMPLIANCE:
--------------------------------------
1. Strict Motion Gating: Zone classification only operates when human motion is actively
   occurring (is_motion=True). Classifying from quiet baseline noise would fabricate
   spatial presence that the RF signal does not support. When is_motion=False, the classifier
   must short-circuit to an explicit unknown state (zone_id=None, separation_margin=None).
2. Diagnostic Separation Margin (No Fabricated Confidence):
   `separation_margin` represents the distance gap between the best-matching and second-best
   matching zone reference signatures. It is an interpretable, defined physical distance gap,
   NOT a statistically validated probability or fabricated "confidence" score. The term
   "confidence" is strictly forbidden.
3. Two Independent Paths to Unknown (zone_id=None):
   - Path A (Motion-Gated): is_motion=False (background quiet, no active motion).
   - Path B (Ambiguous Margin): is_motion=True, but separation_margin is below the configured
     minimum threshold, indicating spatial ambiguity between candidate zones.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional, Protocol, runtime_checkable

from wisense_backend.contracts.feature_extractor import FeatureVector
from wisense_backend.contracts.motion_detector import MotionState


@dataclass(frozen=True, slots=True)
class ZoneClassification:
    """Immutable representation of a spatial zone classification decision.

    Attributes:
        timestamp: UTC Unix epoch seconds (time.time()) corresponding to the end of
            the analysis window from which this decision was derived.
        zone_id: 1-indexed identifier of the classified room zone (1 <= zone_id <= zone_count),
            or `None` if the active zone is unknown (due to lack of motion or ambiguous separation).
        is_motion: Motion detection flag carried through from the gating MotionState.
        calibration_version: Integer version of the CalibrationProfile used for this classification.
        separation_margin: The distance gap between the winning zone signature and the runner-up.
            Represents how distinct the best match was compared to its nearest competitor.
            `None` when is_motion=False or if the environment has only 1 zone.
    """

    timestamp: float
    zone_id: Optional[int]
    is_motion: bool
    calibration_version: int
    separation_margin: Optional[float]

    def __post_init__(self) -> None:
        """Validate invariant constraints on ZoneClassification attributes."""
        if self.timestamp <= 0.0:
            raise ValueError(f"timestamp must be positive, got {self.timestamp}")
        if not isinstance(self.is_motion, bool):
            raise ValueError(
                f"is_motion must be a bool, got {type(self.is_motion).__name__}"
            )
        if self.calibration_version < 1:
            raise ValueError(
                f"calibration_version must be >= 1, got {self.calibration_version}"
            )

        # Invariant 1: Motion gating — if no motion, zone and margin must be None
        if not self.is_motion:
            if self.zone_id is not None:
                raise ValueError(
                    f"zone_id must be None when is_motion is False, got {self.zone_id}"
                )
            if self.separation_margin is not None:
                raise ValueError(
                    f"separation_margin must be None when is_motion is False, got {self.separation_margin}"
                )

        # Invariant 2: Zone ID bounds
        if self.zone_id is not None:
            if not isinstance(self.zone_id, int):
                raise TypeError(
                    f"zone_id must be an int or None, got {type(self.zone_id).__name__}"
                )
            if self.zone_id < 1:
                raise ValueError(
                    f"zone_id must be >= 1 (1-indexed), got {self.zone_id}"
                )

        # Invariant 3: Separation margin bounds
        if self.separation_margin is not None:
            if not math.isfinite(self.separation_margin) or self.separation_margin < 0.0:
                raise ValueError(
                    f"separation_margin must be a finite non-negative number, got {self.separation_margin}"
                )

    def to_dict(self) -> dict[str, Any]:
        """Convert classification result to a JSON-serializable dictionary."""
        return {
            "timestamp": self.timestamp,
            "zone_id": self.zone_id,
            "is_motion": self.is_motion,
            "calibration_version": self.calibration_version,
            "separation_margin": (
                round(self.separation_margin, 6)
                if self.separation_margin is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ZoneClassification:
        """Construct a ZoneClassification from a dictionary."""
        zone_id = int(data["zone_id"]) if data.get("zone_id") is not None else None
        sep_margin = (
            float(data["separation_margin"])
            if data.get("separation_margin") is not None
            else None
        )
        return cls(
            timestamp=float(data["timestamp"]),
            zone_id=zone_id,
            is_motion=bool(data["is_motion"]),
            calibration_version=int(data["calibration_version"]),
            separation_margin=sep_margin,
        )


@runtime_checkable
class ZoneClassifier(Protocol):
    """Abstract protocol for spatial room zone classifiers."""

    def classify(
        self,
        feature: FeatureVector,
        motion: MotionState,
    ) -> ZoneClassification:
        """Classify spatial room zone for a feature vector gated by motion state.

        Args:
            feature: The computed FeatureVector from windowed variance extraction.
            motion: The MotionState corresponding to this analysis window.

        Returns:
            A `ZoneClassification` decision.
        """
        ...

    def classify_stream(
        self,
        features: Iterable[FeatureVector],
        motion_states: Iterable[MotionState],
    ) -> Iterator[ZoneClassification]:
        """Stream transformation helper yielding ZoneClassifications for paired streams.

        Args:
            features: Stream of `FeatureVector` instances.
            motion_states: Stream of corresponding `MotionState` instances.

        Yields:
            `ZoneClassification` instances as each window is classified.
        """
        ...
