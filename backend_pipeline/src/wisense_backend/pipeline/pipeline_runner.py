"""Pipeline runner and orchestrator for WiSense-Twin.

Orchestrates sequential processing of CSI records through preprocessing,
windowed variance feature extraction, threshold-based motion detection,
and optional nearest-signature zone classification. Pure and testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from wisense_backend.contracts.csi_source import CSIRecord
from wisense_backend.contracts.feature_extractor import FeatureExtractor, FeatureVector
from wisense_backend.contracts.motion_detector import MotionDetector, MotionState
from wisense_backend.contracts.preprocessor import Preprocessor
from wisense_backend.contracts.state_message import StateMessage
from wisense_backend.contracts.zone_classifier import ZoneClassification, ZoneClassifier


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Result of processing a single CSIRecord through the pipeline.

    Attributes:
        feature: The computed FeatureVector from windowed variance extraction.
        motion: The MotionState evaluated by hysteresis motion detection.
        classification: Optional ZoneClassification (None in no-zone mode).
        state_message: The constructed StateMessage ready for wire publication.
    """

    feature: FeatureVector
    motion: MotionState
    classification: Optional[ZoneClassification]
    state_message: StateMessage


class PipelineRunner:
    """Sequential pipeline runner executing signal processing stages per CSI record."""

    def __init__(
        self,
        preprocessor: Preprocessor,
        feature_extractor: FeatureExtractor,
        motion_detector: MotionDetector,
        classifier: Optional[ZoneClassifier] = None,
    ) -> None:
        """Initialize PipelineRunner with processing stages.

        Args:
            preprocessor: Outlier filtering and smoothing stage.
            feature_extractor: Windowed variance feature extraction stage.
            motion_detector: Hysteresis-based motion detection stage.
            classifier: Optional zone classification stage. If omitted, runner operates
                in no-zone mode.
        """
        self._preprocessor = preprocessor
        self._feature_extractor = feature_extractor
        self._motion_detector = motion_detector
        self._classifier = classifier

    @property
    def preprocessor(self) -> Preprocessor:
        """Return the preprocessor stage."""
        return self._preprocessor

    @property
    def feature_extractor(self) -> FeatureExtractor:
        """Return the feature extractor stage."""
        return self._feature_extractor

    @property
    def motion_detector(self) -> MotionDetector:
        """Return the motion detector stage."""
        return self._motion_detector

    @property
    def classifier(self) -> Optional[ZoneClassifier]:
        """Return the classifier stage, or None if in no-zone mode."""
        return self._classifier

    def process(self, record: CSIRecord) -> Optional[PipelineResult]:
        """Process a single CSIRecord sequentially through all stages.

        Args:
            record: Incoming raw CSI frame record.

        Returns:
            PipelineResult if the feature extractor emitted a FeatureVector,
            or None while the sliding window is still buffering.
        """
        clean_record = self._preprocessor.process(record)
        feature = self._feature_extractor.process(clean_record)
        if feature is None:
            return None

        motion = self._motion_detector.process(feature)

        if self._classifier is not None:
            classification = self._classifier.classify(feature, motion)
            zone_id = classification.zone_id
            separation_margin = classification.separation_margin
            cal_ver = classification.calibration_version
        else:
            classification = None
            zone_id = None
            separation_margin = None
            cal_ver = None

        state_msg = StateMessage(
            schema_version=1,
            timestamp=feature.window_end_timestamp,
            zone_id=zone_id,
            is_motion=motion.is_motion,
            presence=motion.presence,
            separation_margin=separation_margin,
            calibration_version=cal_ver,
        )

        return PipelineResult(
            feature=feature,
            motion=motion,
            classification=classification,
            state_message=state_msg,
        )

    def reset(self) -> None:
        """Reset internal state of every constituent pipeline stage."""
        self._preprocessor.reset()
        self._feature_extractor.reset()
        self._motion_detector.reset()
        if self._classifier is not None and hasattr(self._classifier, "reset"):
            self._classifier.reset()
