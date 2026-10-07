"""Motion detection contracts and data structures for WiSense-Twin.

This module defines `MotionState` (representing instantaneous motion and motion-derived
presence) and the `MotionDetector` protocol implemented by motion detection engines.

NAMING DISCIPLINE & SYSTEM PRINCIPLE:
------------------------------------
Per the architecture feasibility audit, this WiFi CSI sensing system detects physical
RF perturbations caused by human MOTION, not static human presence ("occupancy").
A stationary or motionless human produces minimal CSI variance that cannot be reliably
distinguished from static baseline multipath.

Consequently:
- `is_motion` represents instantaneous, hysteresis-smoothed RF disturbance detection.
- `presence` represents motion-derived presence with a configurable timeout
  ("motion occurred within the last T seconds", NOT "someone is definitely physically here").
- Under NO circumstances should any attribute or component be named `occupancy` or `is_occupied`.
- Motion detection is strictly zone-agnostic: mapping motion to room zones is exclusively
  handled by zone classification (Section 8).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Iterator, Protocol, runtime_checkable

from wisense_backend.contracts.feature_extractor import FeatureVector


@dataclass(frozen=True, slots=True)
class MotionState:
    """Immutable representation of motion and presence state at a discrete point in time.

    Attributes:
        timestamp: UTC Unix epoch seconds (time.time()) corresponding to the end of
            the analysis window from which this decision was derived.
        is_motion: Instantaneous motion detection flag with hysteresis deadband smoothing.
        presence: Motion-derived presence flag sustained by a configurable post-motion timeout.
        variance_at_detection: The aggregate subcarrier variance that drove this decision.
            Serves as an empirical diagnostic and calibration metric (NOT a confidence score).
    """

    timestamp: float
    is_motion: bool
    presence: bool
    variance_at_detection: float

    def __post_init__(self) -> None:
        """Validate invariant constraints on motion state attributes."""
        if self.timestamp <= 0.0:
            raise ValueError(f"timestamp must be positive, got {self.timestamp}")
        if not isinstance(self.is_motion, bool):
            raise ValueError(f"is_motion must be a bool, got {type(self.is_motion).__name__}")
        if not isinstance(self.presence, bool):
            raise ValueError(f"presence must be a bool, got {type(self.presence).__name__}")
        if not math.isfinite(self.variance_at_detection) or self.variance_at_detection < 0.0:
            raise ValueError(
                f"variance_at_detection must be a finite non-negative number, got {self.variance_at_detection}"
            )


@runtime_checkable
class MotionDetector(Protocol):
    """Abstract protocol for algorithms transforming FeatureVectors into MotionStates.

    Operates purely on aggregate energy metrics without coupling to spatial zones.
    """

    def process(self, feature: FeatureVector) -> MotionState:
        """Ingest a FeatureVector and evaluate the current motion and presence state.

        Args:
            feature: Ingested `FeatureVector` from the feature extraction stage.

        Returns:
            The resulting `MotionState`.
        """
        ...

    def reset(self) -> None:
        """Reset internal hysteresis state and presence timeout timers."""
        ...

    def process_stream(self, features: Iterable[FeatureVector]) -> Iterator[MotionState]:
        """Stream transformation helper yielding MotionStates for incoming features.

        Args:
            features: Iterable of `FeatureVector` instances.

        Yields:
            Computed `MotionState` instances.
        """
        ...
