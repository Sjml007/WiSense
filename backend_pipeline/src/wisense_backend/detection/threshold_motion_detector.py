"""Dual-threshold hysteresis motion detector with presence timeout.

Implements `ThresholdMotionDetector`, evaluating spatial aggregate variance against
configurable dual thresholds (enter/exit) to suppress boundary flicker, and maintaining
a motion-derived presence state with a post-motion timeout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from wisense_backend.contracts.feature_extractor import FeatureVector
from wisense_backend.contracts.motion_detector import MotionDetector, MotionState


@dataclass(frozen=True)
class ThresholdMotionConfig:
    """Configuration for threshold-based motion detection.

    Attributes:
        enter_threshold: Aggregate variance threshold required to transition from
            no-motion to motion (must be > exit_threshold).
        exit_threshold: Aggregate variance threshold below which state transitions
            from motion to no-motion (must be > 0.0 and < enter_threshold).
        presence_timeout_seconds: Duration in seconds to sustain the `presence` flag
            after the last confirmed motion frame (must be > 0.0).
    """

    enter_threshold: float = 0.10
    exit_threshold: float = 0.05
    presence_timeout_seconds: float = 3.0

    def __post_init__(self) -> None:
        """Validate hysteresis threshold ordering and positive timeout."""
        if self.exit_threshold <= 0.0:
            raise ValueError(f"exit_threshold must be positive, got {self.exit_threshold}")
        if self.enter_threshold <= self.exit_threshold:
            raise ValueError(
                f"enter_threshold ({self.enter_threshold}) must be strictly greater than "
                f"exit_threshold ({self.exit_threshold}) to establish a hysteresis deadband"
            )
        if self.presence_timeout_seconds <= 0.0:
            raise ValueError(
                f"presence_timeout_seconds must be positive, got {self.presence_timeout_seconds}"
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThresholdMotionConfig:
        return cls(
            enter_threshold=float(data.get("enter_threshold", 0.10)),
            exit_threshold=float(data.get("exit_threshold", 0.05)),
            presence_timeout_seconds=float(data.get("presence_timeout_seconds", 3.0)),
        )

    @classmethod
    def from_json_file(cls, file_path: str | Path) -> ThresholdMotionConfig:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Motion detection configuration not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)


class ThresholdMotionDetector(MotionDetector):
    """Dual-threshold hysteresis motion detector with presence timeout tracking.

    Hysteresis Deadband Principle:
    ------------------------------
    Single-threshold detectors flicker when variance hovers near the threshold.
    Using two distinct thresholds (enter_threshold > exit_threshold) creates a
    stable deadband:
    - Transition to Motion requires variance > enter_threshold.
    - Transition to No-Motion requires variance < exit_threshold.
    - Intermediate values retain the previous state, preventing boundary chatter.

    Presence Timeout Principle:
    ---------------------------
    `presence` is a motion-derived heuristic ("motion was recently observed"),
    sustaining True for `presence_timeout_seconds` after the last confirmed motion.
    It does NOT assert physical stationary occupancy.
    """

    def __init__(self, config: Optional[ThresholdMotionConfig] = None) -> None:
        self.config = config if config is not None else ThresholdMotionConfig()
        self._is_motion: bool = False
        self._last_motion_timestamp: Optional[float] = None

    def reset(self) -> None:
        """Clear motion state and last motion timestamp."""
        self._is_motion = False
        self._last_motion_timestamp = None

    def process(self, feature: FeatureVector) -> MotionState:
        """Process an incoming FeatureVector into a MotionState."""
        variance = feature.aggregate_variance
        timestamp = feature.window_end_timestamp

        # 1. Evaluate instantaneous motion with hysteresis deadband
        if not self._is_motion:
            if variance > self.config.enter_threshold:
                self._is_motion = True
        else:
            if variance < self.config.exit_threshold:
                self._is_motion = False

        # 2. Evaluate motion-derived presence with timeout
        if self._is_motion:
            self._last_motion_timestamp = timestamp
            presence = True
        else:
            if self._last_motion_timestamp is not None:
                time_since_motion = timestamp - self._last_motion_timestamp
                if time_since_motion <= self.config.presence_timeout_seconds:
                    presence = True
                else:
                    presence = False
                    self._last_motion_timestamp = None
            else:
                presence = False

        return MotionState(
            timestamp=timestamp,
            is_motion=self._is_motion,
            presence=presence,
            variance_at_detection=variance,
        )

    def process_stream(self, features: Iterable[FeatureVector]) -> Iterator[MotionState]:
        """Stream transformation helper yielding MotionStates."""
        for feature in features:
            yield self.process(feature)
