"""Unit and integration tests for NearestSignatureZoneClassifier and ZoneClassification contracts."""

import logging
import math
from pathlib import Path

import pytest

from wisense_backend.calibration.calibrator import ZoneCalibrator
from wisense_backend.calibration.labeled_sample import LabeledFeatureSample
from wisense_backend.classification.nearest_signature_classifier import (
    NearestSignatureZoneClassifier,
    ZoneClassifierConfig,
)
from wisense_backend.contracts.calibration import CalibrationProfile, ZoneSignature
from wisense_backend.contracts.feature_extractor import FeatureVector
from wisense_backend.contracts.motion_detector import MotionState
from wisense_backend.contracts.zone_classifier import (
    ZoneClassification,
    ZoneClassifier,
)
from wisense_backend.detection.threshold_motion_detector import (
    ThresholdMotionConfig,
    ThresholdMotionDetector,
)
from wisense_backend.features.variance_extractor import (
    VarianceExtractorConfig,
    VarianceFeatureExtractor,
)
from wisense_backend.ingestion.synthetic_source import (
    ScenarioConfig,
    ScenarioEvent,
    SyntheticCSISource,
)
from wisense_backend.preprocessing.pipeline import PreprocessingPipeline


def make_test_feature(
    ts: float,
    variances: tuple[float, ...],
    aggregate_variance: float | None = None,
) -> FeatureVector:
    """Helper to construct a FeatureVector for testing."""
    agg = aggregate_variance if aggregate_variance is not None else sum(variances)
    return FeatureVector(
        window_start_timestamp=ts - 1.0,
        window_end_timestamp=ts,
        sample_count=20,
        subcarrier_count=len(variances),
        variance_per_subcarrier=variances,
        aggregate_variance=agg,
    )


class TestZoneClassifierContracts:
    """Validate ZoneClassification invariants, naming discipline, and Protocol conformance."""

    def test_zone_classification_valid(self) -> None:
        """Verify valid classification attributes."""
        zc = ZoneClassification(
            timestamp=1700000001.0,
            zone_id=2,
            is_motion=True,
            calibration_version=1,
            separation_margin=0.25,
        )
        assert zc.timestamp == 1700000001.0
        assert zc.zone_id == 2
        assert zc.is_motion is True
        assert zc.calibration_version == 1
        assert zc.separation_margin == 0.25

    def test_motion_gating_invariant_enforcement(self) -> None:
        """When is_motion is False, zone_id and separation_margin MUST be None."""
        # zone_id not None when is_motion is False -> raises ValueError
        with pytest.raises(ValueError, match="zone_id must be None when is_motion is False"):
            ZoneClassification(
                timestamp=1.0,
                zone_id=1,
                is_motion=False,
                calibration_version=1,
                separation_margin=None,
            )

        # separation_margin not None when is_motion is False -> raises ValueError
        with pytest.raises(ValueError, match="separation_margin must be None when is_motion is False"):
            ZoneClassification(
                timestamp=1.0,
                zone_id=None,
                is_motion=False,
                calibration_version=1,
                separation_margin=0.15,
            )

    def test_contract_validation_errors(self) -> None:
        """Verify boundary checks on timestamp, version, margin, and zone_id."""
        with pytest.raises(ValueError, match="timestamp must be positive"):
            ZoneClassification(timestamp=0.0, zone_id=None, is_motion=False, calibration_version=1, separation_margin=None)

        with pytest.raises(ValueError, match="calibration_version must be >= 1"):
            ZoneClassification(timestamp=1.0, zone_id=None, is_motion=False, calibration_version=0, separation_margin=None)

        with pytest.raises(ValueError, match="zone_id must be >= 1"):
            ZoneClassification(timestamp=1.0, zone_id=0, is_motion=True, calibration_version=1, separation_margin=0.1)

        with pytest.raises(ValueError, match="separation_margin must be a finite non-negative number"):
            ZoneClassification(timestamp=1.0, zone_id=1, is_motion=True, calibration_version=1, separation_margin=-0.05)

    def test_strict_naming_discipline_no_confidence(self) -> None:
        """Per Audit Item #13, separation_margin is a diagnostic distance, NEVER a confidence score."""
        zc = ZoneClassification(
            timestamp=1.0,
            zone_id=1,
            is_motion=True,
            calibration_version=1,
            separation_margin=0.15,
        )
        assert not hasattr(zc, "confidence"), "ZoneClassification must never contain a 'confidence' field"
        for attr in dir(zc):
            assert "confidence" not in attr.lower(), f"Forbidden substring 'confidence' found in attribute '{attr}'"

    def test_serialization_round_trip(self) -> None:
        """Verify to_dict and from_dict preserve all values and types."""
        zc = ZoneClassification(
            timestamp=1700000005.5,
            zone_id=3,
            is_motion=True,
            calibration_version=2,
            separation_margin=0.185241,
        )
        d = zc.to_dict()
        assert d["zone_id"] == 3
        assert d["separation_margin"] == pytest.approx(0.185241, abs=1e-6)

        loaded = ZoneClassification.from_dict(d)
        assert loaded == zc


class TestNearestSignatureClassifier:
    """Validate NearestSignatureZoneClassifier logic, motion-gating, ambiguity margin, and warnings."""

    @pytest.fixture
    def default_profile(self) -> CalibrationProfile:
        path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "calibration_profiles"
            / "default_calibration.json"
        )
        return CalibrationProfile.from_json_file(path)

    def test_protocol_conformance(self, default_profile: CalibrationProfile) -> None:
        """Verify classifier conforms to ZoneClassifier Protocol."""
        classifier = NearestSignatureZoneClassifier(default_profile)
        assert isinstance(classifier, ZoneClassifier)

    def test_motion_gating_short_circuits_to_none(self, default_profile: CalibrationProfile) -> None:
        """When is_motion is False, classifier MUST return zone_id=None regardless of feature content."""
        classifier = NearestSignatureZoneClassifier(default_profile)

        # Construct a feature vector that identical to Zone 1's signature with strong energy
        sig1 = default_profile.get_signature(1)
        fv = make_test_feature(ts=5.0, variances=sig1.mean_variance_per_subcarrier)

        # MotionState with is_motion=False
        motion_quiet = MotionState(
            timestamp=5.0,
            is_motion=False,
            presence=False,
            variance_at_detection=0.02,
        )

        result = classifier.classify(fv, motion_quiet)
        assert result.is_motion is False
        assert result.zone_id is None
        assert result.separation_margin is None
        assert result.calibration_version == default_profile.calibration_version

    def test_nearest_signature_matching_all_zones(self, default_profile: CalibrationProfile) -> None:
        """Verify classifier correctly matches features aligning with each zone's signature."""
        classifier = NearestSignatureZoneClassifier(default_profile)

        for expected_zone in range(1, default_profile.zone_count + 1):
            sig = default_profile.get_signature(expected_zone)
            # Add slight random noise to simulate a realistic observed feature
            perturbed_variances = tuple(v * 1.05 for v in sig.mean_variance_per_subcarrier)
            fv = make_test_feature(ts=10.0 + expected_zone, variances=perturbed_variances)

            motion_active = MotionState(
                timestamp=10.0 + expected_zone,
                is_motion=True,
                presence=True,
                variance_at_detection=sig.mean_aggregate_variance,
            )

            result = classifier.classify(fv, motion_active)
            assert result.is_motion is True
            assert result.zone_id == expected_zone, f"Failed to match Zone {expected_zone}"
            assert result.separation_margin is not None
            assert result.separation_margin > 0.05

    def test_ambiguous_margin_reports_unknown(self, default_profile: CalibrationProfile) -> None:
        """When separation_margin < min_separation_margin, classifier must report zone_id=None."""
        # Construct feature vector that is an exact average of Zone 1 and Zone 2 signatures
        sig1 = default_profile.get_signature(1)
        sig2 = default_profile.get_signature(2)
        blended = tuple(
            (v1 + v2) / 2.0
            for v1, v2 in zip(sig1.mean_variance_per_subcarrier, sig2.mean_variance_per_subcarrier)
        )
        fv = make_test_feature(ts=5.0, variances=blended)
        motion_active = MotionState(timestamp=5.0, is_motion=True, presence=True, variance_at_detection=0.5)

        # 1. Without min_separation_margin threshold: picks whichever is marginally closer
        classifier_loose = NearestSignatureZoneClassifier(
            default_profile,
            config=ZoneClassifierConfig(min_separation_margin=None),
        )
        res_loose = classifier_loose.classify(fv, motion_active)
        assert res_loose.is_motion is True
        assert res_loose.zone_id in (1, 2)
        assert res_loose.separation_margin is not None
        # Margin should be nearly 0 because the vector was an exact 50/50 blend
        assert res_loose.separation_margin < 0.02

        # 2. With min_separation_margin = 0.05: reports zone_id=None due to spatial ambiguity!
        classifier_strict = NearestSignatureZoneClassifier(
            default_profile,
            config=ZoneClassifierConfig(min_separation_margin=0.05),
        )
        res_strict = classifier_strict.classify(fv, motion_active)
        assert res_strict.is_motion is True
        assert res_strict.zone_id is None, "Expected zone_id=None for ambiguous separation margin"
        assert res_strict.separation_margin is not None
        assert res_strict.separation_margin < 0.05

    def test_construction_separability_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """When loaded profile contains poorly-separated zones, classifier must log a warning at construction."""
        # Construct a 2-zone profile where Zone 1 and Zone 2 have identical subcarrier variance
        subcarrier_count = 16
        flat_variance = tuple(0.2 for _ in range(subcarrier_count))
        sig1 = ZoneSignature(1, subcarrier_count, flat_variance, sum(flat_variance), 20)
        sig2 = ZoneSignature(2, subcarrier_count, flat_variance, sum(flat_variance), 20)
        profile_poor = CalibrationProfile(
            zone_count=2,
            subcarrier_count=subcarrier_count,
            calibration_version=1,
            created_at="2026-09-15T00:00:00Z",
            source="synthetic",
            zone_signatures={1: sig1, 2: sig2},
        )

        with caplog.at_level(logging.WARNING):
            classifier = NearestSignatureZoneClassifier(profile_poor)

        # Verify warning was logged with pair info
        assert "poorly-separated zone pair" in caplog.text
        assert "(1, 2)" in caplog.text
        # Classifier must still instantiate and work without crashing
        assert classifier.profile == profile_poor

    def test_config_from_json(self) -> None:
        """Verify ZoneClassifierConfig loads from canonical JSON file."""
        config_path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "zone_classification_config.json"
        )
        assert config_path.is_file()
        cfg = ZoneClassifierConfig.from_json_file(config_path)
        assert cfg.metric == "cosine_distance"
        assert cfg.min_separation_margin == 0.05
        assert cfg.separability_warning_threshold == 0.20


class TestEndToEnd8StagePipelineIntegration:
    """Full 8-stage pipeline integration test: Sections 1 -> 4 -> 5 -> 6 -> 7 -> 8."""

    def test_full_pipeline_multizone_classification(self) -> None:
        """Run a 4-zone sequential motion scenario through all pipeline stages:

        1. Ingestion (SyntheticCSISource)
        2. Preprocessing (HampelFilter + MovingAverageFilter)
        3. Feature Extraction (VarianceFeatureExtractor)
        4. Motion Detection (ThresholdMotionDetector)
        5. Calibration (ZoneCalibrator)
        6. Classification (NearestSignatureZoneClassifier)

        Asserts:
        - Quiet intervals produce is_motion=False, zone_id=None.
        - Active motion in Zone 1 (t=2..5s) classifies as Zone 1.
        - Active motion in Zone 2 (t=7..10s) classifies as Zone 2.
        - Active motion in Zone 3 (t=12..15s) classifies as Zone 3.
        - Active motion in Zone 4 (t=17..20s) classifies as Zone 4.
        """
        # Step 1: Ingestion — 4 zones, 64 subcarriers, 24 seconds (480 frames at 20 Hz)
        scenario = ScenarioConfig(
            scenario_name="e2e_8stage_pipeline",
            description="Sequential motion across 4 zones with quiet intervals",
            sample_rate_hz=20.0,
            subcarrier_count=64,
            zone_count=4,
            duration_seconds=24.0,
            realtime=False,
            random_seed=42,
            baseline_amplitude=30.0,
            noise_std=0.20,
            events=[
                ScenarioEvent(start_time_s=2.0, end_time_s=5.0, zone_id=1, motion_type="walking", intensity=1.0),
                ScenarioEvent(start_time_s=7.0, end_time_s=10.0, zone_id=2, motion_type="walking", intensity=1.0),
                ScenarioEvent(start_time_s=12.0, end_time_s=15.0, zone_id=3, motion_type="walking", intensity=1.0),
                ScenarioEvent(start_time_s=17.0, end_time_s=20.0, zone_id=4, motion_type="walking", intensity=1.0),
            ],
        )
        with SyntheticCSISource(scenario) as source:
            raw_records = list(source)
        assert len(raw_records) == 480

        # Step 2: Preprocessing
        prep_path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "preprocessing_config.json"
        )
        preprocessor = PreprocessingPipeline.from_json_file(prep_path)
        cleaned_records = list(preprocessor.process_stream(raw_records))

        # Step 3: Feature Extraction (window size 20 = 1.0s, step 1)
        feat_extractor = VarianceFeatureExtractor(
            VarianceExtractorConfig(window_size_samples=20, window_step_samples=1)
        )
        feature_vectors = list(feat_extractor.process_stream(cleaned_records))
        assert len(feature_vectors) == 461

        # Step 4: Motion Detection (enter=0.10, exit=0.05, presence_timeout=2.0s)
        motion_detector = ThresholdMotionDetector(
            ThresholdMotionConfig(enter_threshold=0.10, exit_threshold=0.05, presence_timeout_seconds=2.0)
        )
        motion_states = list(motion_detector.process_stream(feature_vectors))
        assert len(motion_states) == 461

        # Step 5: Offline Calibration using ground-truth labeled training windows
        start_epoch = raw_records[0].timestamp
        labeled_samples: list[LabeledFeatureSample] = []
        for fv in feature_vectors:
            midpoint = (fv.window_start_timestamp + fv.window_end_timestamp) / 2.0
            rel_t = midpoint - start_epoch
            active = [e for e in scenario.events if e.start_time_s <= rel_t <= e.end_time_s]
            if len(active) == 1:
                labeled_samples.append(LabeledFeatureSample(fv, zone_id=active[0].zone_id))
            elif len(active) == 0:
                labeled_samples.append(LabeledFeatureSample(fv, zone_id=None))

        calibrator = ZoneCalibrator(zone_count=4, subcarrier_count=64, calibration_version=1)
        profile = calibrator.calibrate(labeled_samples)
        assert profile.zone_count == 4

        # Step 6: Zone Classification
        classifier = NearestSignatureZoneClassifier(
            profile,
            config=ZoneClassifierConfig(metric="cosine_distance", min_separation_margin=0.02),
        )
        classifications = list(classifier.classify_stream(feature_vectors, motion_states))
        assert len(classifications) == 461

        # Assertions across timeline:

        # 1. Pre-motion quiet interval (relative time < 1.5s)
        pre_motion = [c for c in classifications if (c.timestamp - start_epoch) < 1.5]
        assert len(pre_motion) > 0
        for c in pre_motion:
            assert c.is_motion is False
            assert c.zone_id is None
            assert c.separation_margin is None

        # 2. Zone 1 active motion (relative time 3.0s .. 4.5s)
        z1_active = [c for c in classifications if 3.0 <= (c.timestamp - start_epoch) <= 4.5]
        assert len(z1_active) > 0
        for c in z1_active:
            assert c.is_motion is True
            assert c.zone_id == 1, f"Expected Zone 1 at t={c.timestamp - start_epoch}, got {c.zone_id}"
            assert c.separation_margin is not None and c.separation_margin > 0.0

        # 3. Intermediate quiet interval between Zone 1 and Zone 2 (relative time 6.15s .. 6.85s)
        # Motion ended at 5.0s; window size is 1.0s, so variance drops below exit_threshold by 6.05s
        gap_1_2 = [c for c in classifications if 6.15 <= (c.timestamp - start_epoch) <= 6.85]
        assert len(gap_1_2) > 0
        for c in gap_1_2:
            assert c.is_motion is False
            assert c.zone_id is None
            assert c.separation_margin is None

        # 4. Zone 2 active motion (relative time 8.0s .. 9.5s)
        z2_active = [c for c in classifications if 8.0 <= (c.timestamp - start_epoch) <= 9.5]
        assert len(z2_active) > 0
        for c in z2_active:
            assert c.is_motion is True
            assert c.zone_id == 2, f"Expected Zone 2 at t={c.timestamp - start_epoch}, got {c.zone_id}"
            assert c.separation_margin is not None and c.separation_margin > 0.0

        # 5. Zone 3 active motion (relative time 13.0s .. 14.5s)
        z3_active = [c for c in classifications if 13.0 <= (c.timestamp - start_epoch) <= 14.5]
        assert len(z3_active) > 0
        for c in z3_active:
            assert c.is_motion is True
            assert c.zone_id == 3, f"Expected Zone 3 at t={c.timestamp - start_epoch}, got {c.zone_id}"
            assert c.separation_margin is not None and c.separation_margin > 0.0

        # 6. Zone 4 active motion (relative time 18.0s .. 19.5s)
        z4_active = [c for c in classifications if 18.0 <= (c.timestamp - start_epoch) <= 19.5]
        assert len(z4_active) > 0
        for c in z4_active:
            assert c.is_motion is True
            assert c.zone_id == 4, f"Expected Zone 4 at t={c.timestamp - start_epoch}, got {c.zone_id}"
            assert c.separation_margin is not None and c.separation_margin > 0.0

        # 7. Post-motion quiet interval (relative time > 22.0s)
        post_motion = [c for c in classifications if (c.timestamp - start_epoch) >= 22.0]
        assert len(post_motion) > 0
        for c in post_motion:
            assert c.is_motion is False
            assert c.zone_id is None

    def test_classify_stream_strict_zip_length_mismatch(self) -> None:
        """Verify classify_stream with strict=True raises ValueError when stream lengths mismatch."""
        path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "calibration_profiles"
            / "default_calibration.json"
        )
        profile = CalibrationProfile.from_json_file(path)
        classifier = NearestSignatureZoneClassifier(profile)
        features = [
            make_test_feature(2.0, (0.1,) * 64, aggregate_variance=0.1),
            make_test_feature(2.05, (0.1,) * 64, aggregate_variance=0.1),
        ]
        # Only 1 motion state provided for 2 features
        motion_states = [
            MotionState(timestamp=2.0, is_motion=False, presence=False, variance_at_detection=0.1),
        ]

        with pytest.raises(ValueError):
            list(classifier.classify_stream(features, motion_states))

    def test_classify_stream_timestamp_mismatch_raises_assertion_error(self) -> None:
        """Verify classify_stream raises AssertionError when FeatureVector and MotionState timestamps differ."""
        path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "calibration_profiles"
            / "default_calibration.json"
        )
        profile = CalibrationProfile.from_json_file(path)
        classifier = NearestSignatureZoneClassifier(profile)
        features = [
            make_test_feature(2.0, (0.1,) * 64, aggregate_variance=0.1),
        ]
        # Motion state timestamp is 2.5, mismatched with feature timestamp 2.0
        motion_states = [
            MotionState(timestamp=2.5, is_motion=False, presence=False, variance_at_detection=0.1),
        ]

        with pytest.raises(AssertionError, match="FeatureVector timestamp .* does not match MotionState timestamp"):
            list(classifier.classify_stream(features, motion_states))
