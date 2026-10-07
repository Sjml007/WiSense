"""Unit and integration tests for ThresholdMotionDetector."""

import math
from pathlib import Path

import pytest

from wisense_backend.contracts.feature_extractor import FeatureVector
from wisense_backend.contracts.motion_detector import MotionDetector, MotionState
from wisense_backend.detection.threshold_motion_detector import (
    ThresholdMotionConfig,
    ThresholdMotionDetector,
)
from wisense_backend.features.variance_extractor import (
    VarianceExtractorConfig,
    VarianceFeatureExtractor,
)
from wisense_backend.ingestion.synthetic_source import ScenarioConfig, SyntheticCSISource
from wisense_backend.preprocessing.pipeline import PreprocessingPipeline


def make_feature(ts: float, aggregate_variance: float) -> FeatureVector:
    """Helper to construct a FeatureVector for testing."""
    return FeatureVector(
        window_start_timestamp=1700000000.0 + ts - 0.5,
        window_end_timestamp=1700000000.0 + ts,
        sample_count=20,
        subcarrier_count=1,
        variance_per_subcarrier=(aggregate_variance,),
        aggregate_variance=aggregate_variance,
    )


class TestMotionContracts:
    """Validate MotionState dataclass invariants and strict naming discipline."""

    def test_motion_state_valid(self) -> None:
        state = MotionState(
            timestamp=1700000001.0,
            is_motion=True,
            presence=True,
            variance_at_detection=0.85,
        )
        assert state.timestamp == 1700000001.0
        assert state.is_motion is True
        assert state.presence is True
        assert state.variance_at_detection == 0.85

    def test_naming_discipline_no_occupancy(self) -> None:
        """Verify strict naming discipline: no 'occupancy' or 'is_occupied' attributes exist."""
        state = MotionState(
            timestamp=1.0,
            is_motion=False,
            presence=False,
            variance_at_detection=0.05,
        )
        forbidden_attrs = ["occupancy", "is_occupied", "occupied", "zone", "zone_id"]
        for attr in forbidden_attrs:
            assert not hasattr(state, attr), f"MotionState violates naming discipline with '{attr}'"

    def test_motion_state_validation_errors(self) -> None:
        """Verify invalid values raise ValueError."""
        with pytest.raises(ValueError, match="timestamp must be positive"):
            MotionState(timestamp=-1.0, is_motion=False, presence=False, variance_at_detection=0.1)

        with pytest.raises(ValueError, match="variance_at_detection must be a finite non-negative number"):
            MotionState(timestamp=1.0, is_motion=False, presence=False, variance_at_detection=-0.1)


class TestThresholdMotionDetector:
    """Validate hysteresis deadband, presence timeout, and state transitions."""

    def test_implements_protocol(self) -> None:
        detector = ThresholdMotionDetector()
        assert isinstance(detector, MotionDetector)

    def test_config_validation(self) -> None:
        """Verify threshold ordering and positive timeout constraints."""
        # exit_threshold <= 0
        with pytest.raises(ValueError, match="exit_threshold must be positive"):
            ThresholdMotionConfig(enter_threshold=0.5, exit_threshold=0.0)

        # enter_threshold <= exit_threshold
        with pytest.raises(ValueError, match="enter_threshold .* must be strictly greater than exit_threshold"):
            ThresholdMotionConfig(enter_threshold=0.3, exit_threshold=0.3)
        with pytest.raises(ValueError, match="enter_threshold .* must be strictly greater than exit_threshold"):
            ThresholdMotionConfig(enter_threshold=0.2, exit_threshold=0.4)

        # presence_timeout_seconds <= 0
        with pytest.raises(ValueError, match="presence_timeout_seconds must be positive"):
            ThresholdMotionConfig(enter_threshold=0.5, exit_threshold=0.2, presence_timeout_seconds=0.0)

    def test_from_dict_defaults_match_dataclass_defaults(self) -> None:
        """Regression test: verify from_dict({}) produces identical thresholds as no-arg constructor."""
        from_dict_cfg = ThresholdMotionConfig.from_dict({})
        default_cfg = ThresholdMotionConfig()
        assert from_dict_cfg == default_cfg
        assert from_dict_cfg.enter_threshold == default_cfg.enter_threshold == 0.10
        assert from_dict_cfg.exit_threshold == default_cfg.exit_threshold == 0.05
        assert from_dict_cfg.presence_timeout_seconds == default_cfg.presence_timeout_seconds == 3.0

    def test_low_variance_stream_remains_quiet(self) -> None:
        """Verify low variance never triggers motion or presence."""
        cfg = ThresholdMotionConfig(enter_threshold=0.40, exit_threshold=0.20)
        detector = ThresholdMotionDetector(cfg)

        for i in range(20):
            state = detector.process(make_feature(ts=1.0 + i * 0.1, aggregate_variance=0.08))
            assert state.is_motion is False
            assert state.presence is False

    def test_enter_threshold_triggers_motion(self) -> None:
        """Verify variance exceeding enter_threshold triggers is_motion and presence."""
        cfg = ThresholdMotionConfig(enter_threshold=0.40, exit_threshold=0.20)
        detector = ThresholdMotionDetector(cfg)

        # Sub-threshold
        s1 = detector.process(make_feature(ts=1.0, aggregate_variance=0.35))
        assert s1.is_motion is False
        assert s1.presence is False

        # Cross above enter_threshold
        s2 = detector.process(make_feature(ts=2.0, aggregate_variance=0.45))
        assert s2.is_motion is True
        assert s2.presence is True

    def test_hysteresis_prevents_flicker_in_deadband(self) -> None:
        """Verify hysteresis deadband: variance oscillating between thresholds does NOT flicker."""
        # Enter at 0.50, Exit at 0.25 -> Deadband is (0.25, 0.50)
        cfg = ThresholdMotionConfig(enter_threshold=0.50, exit_threshold=0.25)
        detector = ThresholdMotionDetector(cfg)

        # 1. Starting from False: variance in deadband (0.35) remains False
        s_initial = detector.process(make_feature(ts=1.0, aggregate_variance=0.35))
        assert s_initial.is_motion is False

        # 2. Spike above enter threshold (0.60) -> transitions to True
        s_enter = detector.process(make_feature(ts=2.0, aggregate_variance=0.60))
        assert s_enter.is_motion is True

        # 3. Oscillate within deadband (0.28, 0.45, 0.30, 0.40) -> MUST REMAIN TRUE (NO FLICKER!)
        deadband_variances = [0.28, 0.45, 0.30, 0.42, 0.35, 0.48]
        for idx, var in enumerate(deadband_variances):
            state = detector.process(make_feature(ts=3.0 + idx, aggregate_variance=var))
            assert state.is_motion is True, f"Flickered to False at variance {var} in deadband!"

        # 4. Drop below exit threshold (0.20) -> transitions to False
        s_exit = detector.process(make_feature(ts=10.0, aggregate_variance=0.20))
        assert s_exit.is_motion is False

        # 5. Rise back into deadband (0.35) -> MUST REMAIN FALSE (NO PREMATURE TRIGGER!)
        s_deadband_again = detector.process(make_feature(ts=11.0, aggregate_variance=0.35))
        assert s_deadband_again.is_motion is False

    def test_presence_timeout_exact_cutoff(self) -> None:
        """Verify presence stays True for configured timeout and flips to False exactly when elapsed."""
        timeout_seconds = 2.0
        cfg = ThresholdMotionConfig(
            enter_threshold=0.40,
            exit_threshold=0.20,
            presence_timeout_seconds=timeout_seconds,
        )
        detector = ThresholdMotionDetector(cfg)

        # Trigger motion at t = 10.0
        m1 = detector.process(make_feature(ts=10.0, aggregate_variance=0.60))
        assert m1.is_motion is True
        assert m1.presence is True

        # Motion ceases at t = 10.5 (variance drops to 0.10)
        m2 = detector.process(make_feature(ts=10.5, aggregate_variance=0.10))
        assert m2.is_motion is False
        assert m2.presence is True  # Sustained by timeout! (time_since_motion = 0.5s)

        # t = 11.5: 1.5s since motion -> presence MUST still be True
        m3 = detector.process(make_feature(ts=11.5, aggregate_variance=0.10))
        assert m3.is_motion is False
        assert m3.presence is True

        # t = 12.0: exactly 2.0s since motion -> presence MUST still be True (<= timeout)
        m4 = detector.process(make_feature(ts=12.0, aggregate_variance=0.10))
        assert m4.is_motion is False
        assert m4.presence is True

        # t = 12.01: 2.01s since motion -> presence MUST FLIP TO FALSE!
        m5 = detector.process(make_feature(ts=12.01, aggregate_variance=0.10))
        assert m5.is_motion is False
        assert m5.presence is False

        # Subsequent quiet frames remain False
        m6 = detector.process(make_feature(ts=13.0, aggregate_variance=0.10))
        assert m6.is_motion is False
        assert m6.presence is False


class TestEndToEndMotionPipelineIntegration:
    """Full 4-stage integration test: Ingestion -> Preprocessing -> Feature Extraction -> Motion Detection."""

    def test_full_pipeline_scenario_motion_activation_and_tail(self) -> None:
        """Run example.json scenario through all 4 stages (Sections 1->4->5->6).

        Asserts:
        - Quiet pre-motion: is_motion=False, presence=False.
        - Active motion interval: is_motion=True, presence=True.
        - Post-motion: instantaneous is_motion=False, presence holds for timeout tail, then flips False.
        """
        # Stage 1: Ingestion (example.json has 300 frames; motion in Zone 2 between t=5.0s and t=10.0s)
        scenario_path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "synthetic_scenarios"
            / "example.json"
        )
        scenario_cfg = ScenarioConfig.from_json_file(scenario_path)

        with SyntheticCSISource(scenario_cfg) as source:
            raw_records = list(source)
        assert len(raw_records) == 300

        # Stage 2: Preprocessing (Hampel + Moving Average)
        prep_cfg_path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "preprocessing_config.json"
        )
        preprocessor = PreprocessingPipeline.from_json_file(prep_cfg_path)
        cleaned_records = list(preprocessor.process_stream(raw_records))

        # Stage 3: Feature Extraction (Window size 20 = 1.0s, step 1)
        feat_cfg = VarianceExtractorConfig(window_size_samples=20, window_step_samples=1)
        extractor = VarianceFeatureExtractor(feat_cfg)
        feature_vectors = list(extractor.process_stream(cleaned_records))
        assert len(feature_vectors) == 281

        # Stage 4: Motion Detection (enter=0.10, exit=0.05, timeout=2.0s)
        motion_cfg = ThresholdMotionConfig(
            enter_threshold=0.10,
            exit_threshold=0.05,
            presence_timeout_seconds=2.0,
        )
        detector = ThresholdMotionDetector(motion_cfg)
        motion_states = list(detector.process_stream(feature_vectors))
        assert len(motion_states) == 281

        start_epoch = raw_records[0].timestamp

        # 1. Pre-motion quiet interval (relative t < 4.0s)
        pre_motion_states = [
            s for s in motion_states if (s.timestamp - start_epoch) < 4.0
        ]
        for s in pre_motion_states:
            assert s.is_motion is False, f"Unexpected motion in quiet interval at t={s.timestamp - start_epoch}"
            assert s.presence is False, f"Unexpected presence in quiet interval at t={s.timestamp - start_epoch}"

        # 2. Active motion interval (relative t between 6.0s and 9.5s)
        active_motion_states = [
            s for s in motion_states if 6.0 <= (s.timestamp - start_epoch) <= 9.5
        ]
        for s in active_motion_states:
            assert s.is_motion is True, f"Expected motion at t={s.timestamp - start_epoch}, got False"
            assert s.presence is True, f"Expected presence at t={s.timestamp - start_epoch}, got False"

        # 3. Post-motion: Motion event ends at t = 10.0s.
        # Window size is 1.0s, so by t = 11.2s the analysis window has completely cleared the motion interval.
        # Instantaneous is_motion MUST be False, but presence is SUSTAINED by the 2.0s timeout.
        post_motion_window = [
            s for s in motion_states if 11.2 <= (s.timestamp - start_epoch) <= 12.5
        ]
        assert len(post_motion_window) > 0
        for s in post_motion_window:
            assert s.is_motion is False, f"is_motion should be False after motion ended, at t={s.timestamp - start_epoch}"
            assert s.presence is True, f"presence should still be True during timeout tail at t={s.timestamp - start_epoch}"

        # 4. Beyond presence timeout: t > 10.0s + 2.0s + 1.0s window lag = t > 13.0s
        # At t >= 13.5s, presence MUST have expired and flipped to False!
        late_states = [
            s for s in motion_states if (s.timestamp - start_epoch) >= 13.5
        ]
        assert len(late_states) > 0
        for s in late_states:
            assert s.is_motion is False
            assert s.presence is False, f"presence should have expired at t={s.timestamp - start_epoch}"
