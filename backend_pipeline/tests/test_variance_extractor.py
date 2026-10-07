"""Unit and integration tests for VarianceFeatureExtractor."""

import json
import math
from pathlib import Path

import numpy as np
import pytest

from wisense_backend.contracts.csi_source import CSIRecord
from wisense_backend.contracts.feature_extractor import FeatureExtractor, FeatureVector
from wisense_backend.features.variance_extractor import (
    VarianceExtractorConfig,
    VarianceFeatureExtractor,
)
from wisense_backend.ingestion.synthetic_source import ScenarioConfig, SyntheticCSISource
from wisense_backend.preprocessing.pipeline import PreprocessingPipeline


def make_record(ts: float, amps: tuple[float, ...], seq: int = 1) -> CSIRecord:
    return CSIRecord(
        timestamp=ts,
        subcarrier_count=len(amps),
        amplitudes=amps,
        rssi=-45.0,
        channel=6,
        sequence_number=seq,
    )


class TestFeatureVectorContract:
    """Validate FeatureVector invariants and strict ground-truth isolation."""

    def test_feature_vector_creation_valid(self) -> None:
        fv = FeatureVector(
            window_start_timestamp=1700000000.0,
            window_end_timestamp=1700000001.0,
            sample_count=20,
            subcarrier_count=3,
            variance_per_subcarrier=(0.1, 0.4, 0.2),
            aggregate_variance=0.2333,
        )
        assert fv.sample_count == 20
        assert fv.subcarrier_count == 3
        assert len(fv.variance_per_subcarrier) == 3

    def test_ground_truth_isolation(self) -> None:
        """Verify FeatureVector has no zone, occupancy, or label attributes."""
        fv = FeatureVector(
            window_start_timestamp=1.0,
            window_end_timestamp=2.0,
            sample_count=10,
            subcarrier_count=2,
            variance_per_subcarrier=(0.1, 0.2),
            aggregate_variance=0.15,
        )
        forbidden_attrs = ["zone", "zone_id", "active_zone", "motion_type", "is_motion", "occupied", "presence"]
        for attr in forbidden_attrs:
            assert not hasattr(fv, attr), f"FeatureVector violates isolation with attribute '{attr}'"

    def test_validation_errors(self) -> None:
        """Verify invalid values raise ValueError."""
        # Negative start timestamp
        with pytest.raises(ValueError, match="window_start_timestamp must be positive"):
            FeatureVector(
                window_start_timestamp=-1.0,
                window_end_timestamp=1.0,
                sample_count=10,
                subcarrier_count=1,
                variance_per_subcarrier=(0.1,),
                aggregate_variance=0.1,
            )

        # End timestamp before start
        with pytest.raises(ValueError, match="window_end_timestamp"):
            FeatureVector(
                window_start_timestamp=2.0,
                window_end_timestamp=1.0,
                sample_count=10,
                subcarrier_count=1,
                variance_per_subcarrier=(0.1,),
                aggregate_variance=0.1,
            )

        # Negative variance
        with pytest.raises(ValueError, match="variance_per_subcarrier"):
            FeatureVector(
                window_start_timestamp=1.0,
                window_end_timestamp=2.0,
                sample_count=10,
                subcarrier_count=1,
                variance_per_subcarrier=(-0.5,),
                aggregate_variance=0.1,
            )


class TestVarianceFeatureExtractor:
    """Validate VarianceFeatureExtractor implementation and signal properties."""

    def test_implements_feature_extractor_protocol(self) -> None:
        extractor = VarianceFeatureExtractor()
        assert isinstance(extractor, FeatureExtractor)

    def test_stationary_noise_yields_low_variance(self) -> None:
        """Verify stationary Gaussian noise yields variance tracking sigma^2."""
        noise_std = 0.2
        expected_var = noise_std**2  # 0.04
        cfg = VarianceExtractorConfig(window_size_samples=20, window_step_samples=1)
        extractor = VarianceFeatureExtractor(cfg)

        np.random.seed(42)
        records = [
            make_record(
                ts=1700000000.0 + i * 0.05,
                amps=(float(30.0 + np.random.normal(0, noise_std)),),
                seq=i,
            )
            for i in range(40)
        ]

        feature_vectors = list(extractor.process_stream(records))
        # 40 records with W=20, step=1 yields 21 feature vectors
        assert len(feature_vectors) == 21

        for fv in feature_vectors:
            assert fv.sample_count == 20
            # Computed variance should be close to theoretical 0.04
            assert math.isclose(fv.aggregate_variance, expected_var, abs_tol=0.03)

    def test_injected_motion_rises_and_falls(self) -> None:
        """Verify variance rises during injected motion and returns to baseline."""
        cfg = VarianceExtractorConfig(window_size_samples=10, window_step_samples=1)
        extractor = VarianceFeatureExtractor(cfg)

        records: list[CSIRecord] = []
        # Interval 0..19: quiet baseline (noise std = 0.1)
        for i in range(20):
            records.append(make_record(1.0 + i * 0.05, (30.0 + float(np.random.normal(0, 0.1)),), seq=i))

        # Interval 20..39: motion injection (large sine swing amplitude = 5.0)
        for i in range(20, 40):
            osc = 5.0 * math.sin(i * 0.5)
            records.append(make_record(1.0 + i * 0.05, (30.0 + osc,), seq=i))

        # Interval 40..59: quiet baseline (noise std = 0.1)
        for i in range(40, 60):
            records.append(make_record(1.0 + i * 0.05, (30.0 + float(np.random.normal(0, 0.1)),), seq=i))

        features = list(extractor.process_stream(records))

        # Quiet pre-motion feature (around frame 15, index 5 in features)
        quiet_pre_var = features[5].aggregate_variance
        assert quiet_pre_var < 0.2

        # Fully active motion window (around frame 30, index 20 in features)
        motion_var = features[20].aggregate_variance
        assert motion_var > 5.0  # Big variance spike

        # Quiet post-motion feature (around frame 55, index 45 in features)
        quiet_post_var = features[45].aggregate_variance
        assert quiet_post_var < 0.2

        # Verify motion variance is orders of magnitude above quiet
        assert motion_var > 20 * quiet_pre_var
        assert motion_var > 20 * quiet_post_var


class TestEndToEndFeaturePipelineIntegration:
    """Full integration test: SyntheticCSISource -> PreprocessingPipeline -> VarianceFeatureExtractor."""

    def test_full_pipeline_synthetic_scenario_integration(self) -> None:
        """Integration test executing Section 1 -> Section 4 -> Section 5.

        Verifies:
        1. Correct feature count accounting for window-fill latency floor (W=20).
        2. Aggregate variance elevation specifically during scenario motion interval.
        3. Spatial discriminability: elevated variance in Zone 2 subcarriers vs Zone 4.
        """
        # 1. Ingestion: Load Section 1 example scenario (15.0s at 20 Hz = 300 frames; Zone 2 motion from 5s to 10s)
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

        # 2. Preprocessing: Pass through Section 4 PreprocessingPipeline
        prep_config_path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "preprocessing_config.json"
        )
        preprocessor = PreprocessingPipeline.from_json_file(prep_config_path)
        cleaned_records = list(preprocessor.process_stream(raw_records))
        assert len(cleaned_records) == 300

        # 3. Feature Extraction: Window size 20 (1.0s at 20 Hz), step 1
        extractor_cfg = VarianceExtractorConfig(
            window_size_samples=20,
            window_step_samples=1,
            emit_partial=False,
        )
        extractor = VarianceFeatureExtractor(extractor_cfg)
        feature_vectors = list(extractor.process_stream(cleaned_records))

        # Check total emitted vectors: 300 frames - 20 window_size + 1 = 281 vectors
        assert len(feature_vectors) == 281

        start_epoch = raw_records[0].timestamp

        # Partition feature vectors by scenario timeline
        quiet_pre_features: list[FeatureVector] = []
        motion_features: list[FeatureVector] = []
        quiet_post_features: list[FeatureVector] = []

        for fv in feature_vectors:
            mid_t = (fv.window_start_timestamp + fv.window_end_timestamp) / 2.0 - start_epoch
            if mid_t < 3.5:
                quiet_pre_features.append(fv)
            elif 6.0 <= mid_t <= 9.0:
                motion_features.append(fv)
            elif mid_t > 11.5:
                quiet_post_features.append(fv)

        assert len(quiet_pre_features) > 0
        assert len(motion_features) > 0
        assert len(quiet_post_features) > 0

        # Average aggregate variance across intervals
        mean_quiet_pre = float(np.mean([fv.aggregate_variance for fv in quiet_pre_features]))
        mean_motion = float(np.mean([fv.aggregate_variance for fv in motion_features]))
        mean_quiet_post = float(np.mean([fv.aggregate_variance for fv in quiet_post_features]))

        # Condition 1: Aggregate variance is significantly elevated during motion vs quiet
        assert mean_motion > 2.0 * mean_quiet_pre, (
            f"Motion variance ({mean_motion:.4f}) must be > 2x quiet_pre ({mean_quiet_pre:.4f})"
        )
        assert mean_motion > 2.0 * mean_quiet_post, (
            f"Motion variance ({mean_motion:.4f}) must be > 2x quiet_post ({mean_quiet_post:.4f})"
        )

        # Condition 2: Spatial discriminability (Zone 2 center vs Zone 4 center)
        # Zone 2 center: ((2 - 0.5) / 4) * 64 = 24
        # Zone 4 center: ((4 - 0.5) / 4) * 64 = 56
        z2_center = int(((2 - 0.5) / 4) * 64)
        z4_center = int(((4 - 0.5) / 4) * 64)

        # Compute mean per-subcarrier variance across the active motion interval
        avg_subcarrier_var = np.mean([fv.variance_per_subcarrier for fv in motion_features], axis=0)

        # Local band variance around Zone 2 vs Zone 4 center (+/- 3 subcarriers)
        z2_band_var = float(np.mean(avg_subcarrier_var[z2_center - 3 : z2_center + 4]))
        z4_band_var = float(np.mean(avg_subcarrier_var[z4_center - 3 : z4_center + 4]))

        assert z2_band_var > 1.8 * z4_band_var, (
            f"Active Zone 2 band variance ({z2_band_var:.4f}) should exceed "
            f"inactive Zone 4 band variance ({z4_band_var:.4f}) by > 1.8x"
        )
