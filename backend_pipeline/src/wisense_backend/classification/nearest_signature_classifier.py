"""NearestSignatureZoneClassifier implementation for WiSense-Twin.

Classifies human motion into physical room zones by comparing real-time FeatureVectors
against calibrated reference ZoneSignatures, strictly gated by motion detection.

ARCHITECTURE & ALGORITHM:
-------------------------
1. Motion Gating: When motion.is_motion is False, classification short-circuits to
   zone_id=None and separation_margin=None. Classifying background noise is prohibited.
2. Distance Matching: Evaluates the distance between FeatureVector.variance_per_subcarrier
   and each calibrated ZoneSignature using the metric from Section 7 (separability.py).
3. Diagnostic Separation Margin: Computes separation_margin = second_best_distance - best_distance.
   If separation_margin < min_separation_margin, reports zone_id=None due to spatial ambiguity.
4. Startup Health Check: Audits profile separability at classifier construction time, logging
   a warning if any zone pairs have mutual spectral interference.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from wisense_backend.calibration.separability import (
    METRIC_FUNCTIONS,
    compute_separability,
)
from wisense_backend.contracts.calibration import CalibrationProfile
from wisense_backend.contracts.feature_extractor import FeatureVector
from wisense_backend.contracts.motion_detector import MotionState
from wisense_backend.contracts.zone_classifier import (
    ZoneClassification,
    ZoneClassifier,
)


@dataclass(frozen=True, slots=True)
class ZoneClassifierConfig:
    """Configuration parameters for nearest-signature zone classification.

    Attributes:
        metric: Distance metric function key (must be in METRIC_FUNCTIONS, e.g. 'cosine_distance').
        min_separation_margin: Optional minimum distance gap required between the best and
            second-best matching zone signatures. If the gap is below this threshold, the match
            is deemed ambiguous and zone_id is reported as None even if is_motion is True.
        separability_warning_threshold: Threshold used at construction time to audit profile
            separability and warn of mutual interference between zones.
    """

    metric: str = "cosine_distance"
    min_separation_margin: Optional[float] = None
    separability_warning_threshold: float = 0.20

    def __post_init__(self) -> None:
        """Validate invariant constraints on configuration."""
        if self.metric not in METRIC_FUNCTIONS:
            raise ValueError(
                f"Unknown metric '{self.metric}'. Supported metrics: {list(METRIC_FUNCTIONS.keys())}"
            )
        if self.min_separation_margin is not None:
            if self.min_separation_margin < 0.0:
                raise ValueError(
                    f"min_separation_margin must be non-negative, got {self.min_separation_margin}"
                )
        if not (0.0 <= self.separability_warning_threshold <= 1.0):
            raise ValueError(
                f"separability_warning_threshold must be in [0.0, 1.0], got {self.separability_warning_threshold}"
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ZoneClassifierConfig:
        """Construct ZoneClassifierConfig from a dictionary."""
        min_margin = (
            float(data["min_separation_margin"])
            if data.get("min_separation_margin") is not None
            else None
        )
        return cls(
            metric=str(data.get("metric", "cosine_distance")),
            min_separation_margin=min_margin,
            separability_warning_threshold=float(
                data.get("separability_warning_threshold", 0.20)
            ),
        )

    @classmethod
    def from_json_file(cls, file_path: str | Path) -> ZoneClassifierConfig:
        """Load ZoneClassifierConfig from a JSON file."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Zone classification configuration not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)


class NearestSignatureZoneClassifier:
    """Classifies spatial zones using nearest reference signature matching with motion gating."""

    def __init__(
        self,
        profile: CalibrationProfile,
        config: Optional[ZoneClassifierConfig] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Initialize classifier with a calibration profile and configuration.

        Args:
            profile: Loaded `CalibrationProfile` containing reference ZoneSignatures.
            config: Optional classifier configuration. If omitted, default parameters are used.
            logger: Optional logger for diagnostic messages.
        """
        if not isinstance(profile, CalibrationProfile):
            raise TypeError(
                f"profile must be an instance of CalibrationProfile, got {type(profile).__name__}"
            )

        self._profile = profile
        self._config = config or ZoneClassifierConfig()
        self._logger = logger or logging.getLogger(__name__)
        self._metric_fn = METRIC_FUNCTIONS[self._config.metric]

        # Audit profile separability at construction time (Audit Item #10 payoff)
        self._audit_profile_separability()

    @property
    def profile(self) -> CalibrationProfile:
        """The active calibration profile."""
        return self._profile

    @property
    def config(self) -> ZoneClassifierConfig:
        """The active classifier configuration."""
        return self._config

    def _audit_profile_separability(self) -> None:
        """Audit the loaded profile's pairwise separability and log warnings for overlapping zones."""
        report = compute_separability(
            self._profile,
            threshold=self._config.separability_warning_threshold,
            metric=self._config.metric,
        )
        if not report.is_fully_separable:
            self._logger.warning(
                "Loaded CalibrationProfile v%d contains %d poorly-separated zone pair(s) "
                "(threshold=%.2f, metric='%s'): %s. Zone classification between these pairs "
                "may experience mutual interference or spatial ambiguity.",
                self._profile.calibration_version,
                len(report.poorly_separated_pairs),
                self._config.separability_warning_threshold,
                self._config.metric,
                report.poorly_separated_pairs,
            )

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

        Raises:
            ValueError: If feature subcarrier count does not match profile subcarrier count.
        """
        if feature.subcarrier_count != self._profile.subcarrier_count:
            raise ValueError(
                f"FeatureVector subcarrier_count ({feature.subcarrier_count}) does not match "
                f"profile subcarrier_count ({self._profile.subcarrier_count})"
            )

        # 1. Motion Gating: Short-circuit if no motion is occurring
        if not motion.is_motion:
            return ZoneClassification(
                timestamp=feature.window_end_timestamp,
                zone_id=None,
                is_motion=False,
                calibration_version=self._profile.calibration_version,
                separation_margin=None,
            )

        # 2. Distance Computation across all zone reference signatures
        distances: dict[int, float] = {}
        for z, sig in self._profile.zone_signatures.items():
            dist = self._metric_fn(
                feature.variance_per_subcarrier,
                sig.mean_variance_per_subcarrier,
            )
            distances[z] = dist

        # 3. Rank zones by ascending distance (closest match first)
        ranked = sorted(distances.items(), key=lambda item: item[1])
        best_zone, best_dist = ranked[0]

        # 4. Compute separation margin if at least 2 zones exist
        separation_margin: Optional[float] = None
        if self._profile.zone_count >= 2:
            _second_zone, second_dist = ranked[1]
            separation_margin = max(0.0, second_dist - best_dist)

        # 5. Check ambiguity threshold: if separation_margin < min_separation_margin, report zone_id=None
        zone_id: Optional[int] = best_zone
        if (
            self._config.min_separation_margin is not None
            and separation_margin is not None
            and separation_margin < self._config.min_separation_margin
        ):
            zone_id = None  # Ambiguous match between close candidate zones

        return ZoneClassification(
            timestamp=feature.window_end_timestamp,
            zone_id=zone_id,
            is_motion=True,
            calibration_version=self._profile.calibration_version,
            separation_margin=separation_margin,
        )

    def classify_stream(
        self,
        features: Iterable[FeatureVector],
        motion_states: Iterable[MotionState],
    ) -> Iterator[ZoneClassification]:
        """Classify paired streams of FeatureVectors and MotionStates.

        Args:
            features: Stream of `FeatureVector` instances.
            motion_states: Stream of corresponding `MotionState` instances.

        Yields:
            `ZoneClassification` instances.
        """
        for feature, motion in zip(features, motion_states):
            yield self.classify(feature, motion)
