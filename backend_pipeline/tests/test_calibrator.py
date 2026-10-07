"""Unit and integration tests for ZoneCalibrator and calibration contracts."""

import math
from pathlib import Path

import pytest

from wisense_backend.calibration.calibrator import ZoneCalibrator
from wisense_backend.calibration.labeled_sample import LabeledFeatureSample
from wisense_backend.contracts.calibration import CalibrationProfile, ZoneSignature
from wisense_backend.contracts.feature_extractor import FeatureVector
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


def make_test_feature_vector(
    ts: float,
    variances: tuple[float, ...],
    aggregate_variance: float | None = None,
) -> FeatureVector:
    """Helper to create a FeatureVector for testing."""
    agg = aggregate_variance if aggregate_variance is not None else sum(variances)
    return FeatureVector(
        window_start_timestamp=ts,
        window_end_timestamp=ts + 1.0,
        sample_count=20,
        subcarrier_count=len(variances),
        variance_per_subcarrier=variances,
        aggregate_variance=agg,
    )


class TestCalibrationContracts:
    """Validate ZoneSignature and CalibrationProfile invariants and serialization."""

    def test_zone_signature_validation(self) -> None:
        """Verify invariant validations on ZoneSignature."""
        # Valid signature
        sig = ZoneSignature(
            zone_id=1,
            subcarrier_count=3,
            mean_variance_per_subcarrier=(0.1, 0.2, 0.3),
            mean_aggregate_variance=0.6,
            sample_count=10,
        )
        assert sig.zone_id == 1
        assert sig.subcarrier_count == 3
        assert sig.sample_count == 10

        # Invalid zone_id (< 1)
        with pytest.raises(ValueError, match="zone_id must be >= 1"):
            ZoneSignature(
                zone_id=0,
                subcarrier_count=3,
                mean_variance_per_subcarrier=(0.1, 0.2, 0.3),
                mean_aggregate_variance=0.6,
                sample_count=10,
            )

        # Invalid subcarrier_count (< 1)
        with pytest.raises(ValueError, match="subcarrier_count must be >= 1"):
            ZoneSignature(
                zone_id=1,
                subcarrier_count=0,
                mean_variance_per_subcarrier=(),
                mean_aggregate_variance=0.6,
                sample_count=10,
            )

        # Length mismatch
        with pytest.raises(ValueError, match="does not match subcarrier_count"):
            ZoneSignature(
                zone_id=1,
                subcarrier_count=3,
                mean_variance_per_subcarrier=(0.1, 0.2),
                mean_aggregate_variance=0.3,
                sample_count=10,
            )

        # Negative or non-finite variance
        with pytest.raises(ValueError, match="must be a finite non-negative number"):
            ZoneSignature(
                zone_id=1,
                subcarrier_count=2,
                mean_variance_per_subcarrier=(-0.1, 0.2),
                mean_aggregate_variance=0.1,
                sample_count=10,
            )
        with pytest.raises(ValueError, match="must be a finite non-negative number"):
            ZoneSignature(
                zone_id=1,
                subcarrier_count=2,
                mean_variance_per_subcarrier=(0.1, float("inf")),
                mean_aggregate_variance=0.1,
                sample_count=10,
            )

        # Sample count < 1
        with pytest.raises(ValueError, match="sample_count must be >= 1"):
            ZoneSignature(
                zone_id=1,
                subcarrier_count=2,
                mean_variance_per_subcarrier=(0.1, 0.2),
                mean_aggregate_variance=0.3,
                sample_count=0,
            )

    def test_calibration_profile_validation(self) -> None:
        """Verify invariant validations on CalibrationProfile."""
        sig1 = ZoneSignature(
            zone_id=1,
            subcarrier_count=2,
            mean_variance_per_subcarrier=(0.1, 0.2),
            mean_aggregate_variance=0.3,
            sample_count=5,
        )
        sig2 = ZoneSignature(
            zone_id=2,
            subcarrier_count=2,
            mean_variance_per_subcarrier=(0.3, 0.4),
            mean_aggregate_variance=0.7,
            sample_count=5,
        )

        # Valid profile
        profile = CalibrationProfile(
            zone_count=2,
            subcarrier_count=2,
            calibration_version=1,
            created_at="2026-09-15T00:00:00+00:00",
            source="synthetic",
            zone_signatures={1: sig1, 2: sig2},
        )
        assert profile.zone_count == 2
        assert profile.get_signature(1) == sig1
        assert profile.get_signature(2) == sig2

        # Count mismatch
        with pytest.raises(ValueError, match="zone_signatures count .* does not match zone_count"):
            CalibrationProfile(
                zone_count=2,
                subcarrier_count=2,
                calibration_version=1,
                created_at="2026-09-15T00:00:00+00:00",
                source="synthetic",
                zone_signatures={1: sig1},
            )

        # Missing zone_id key (e.g. keys are 1 and 3, but zone_count is 2)
        sig3 = ZoneSignature(
            zone_id=3,
            subcarrier_count=2,
            mean_variance_per_subcarrier=(0.3, 0.4),
            mean_aggregate_variance=0.7,
            sample_count=5,
        )
        with pytest.raises(ValueError, match="Missing ZoneSignature for zone_id 2"):
            CalibrationProfile(
                zone_count=2,
                subcarrier_count=2,
                calibration_version=1,
                created_at="2026-09-15T00:00:00+00:00",
                source="synthetic",
                zone_signatures={1: sig1, 3: sig3},
            )

        # Invalid source
        with pytest.raises(ValueError, match="source must be one of"):
            CalibrationProfile(
                zone_count=2,
                subcarrier_count=2,
                calibration_version=1,
                created_at="2026-09-15T00:00:00+00:00",
                source="unsupported_source",
                zone_signatures={1: sig1, 2: sig2},
            )

        # Subcarrier count mismatch
        sig_mismatch = ZoneSignature(
            zone_id=2,
            subcarrier_count=3,
            mean_variance_per_subcarrier=(0.1, 0.2, 0.3),
            mean_aggregate_variance=0.6,
            sample_count=5,
        )
        with pytest.raises(ValueError, match="does not match profile subcarrier_count"):
            CalibrationProfile(
                zone_count=2,
                subcarrier_count=2,
                calibration_version=1,
                created_at="2026-09-15T00:00:00+00:00",
                source="synthetic",
                zone_signatures={1: sig1, 2: sig_mismatch},
            )

    def test_zone_signatures_immutable_mapping_proxy(self) -> None:
        """Verify CalibrationProfile.zone_signatures is a read-only MappingProxyType and cannot be mutated."""
        sig1 = ZoneSignature(1, 2, (0.1, 0.2), 0.3, 5)
        sig2 = ZoneSignature(2, 2, (0.3, 0.4), 0.7, 5)
        profile = CalibrationProfile(
            zone_count=2,
            subcarrier_count=2,
            calibration_version=1,
            created_at="2026-09-15T00:00:00+00:00",
            source="synthetic",
            zone_signatures={1: sig1, 2: sig2},
        )

        # Attempting to assign a new key must raise TypeError
        with pytest.raises(TypeError):
            profile.zone_signatures[5] = sig1  # type: ignore

        # Attempting to reassign an existing key must raise TypeError
        with pytest.raises(TypeError):
            profile.zone_signatures[1] = sig2  # type: ignore

        # Attempting to delete a key must raise TypeError
        with pytest.raises(TypeError):
            del profile.zone_signatures[1]  # type: ignore

    def test_save_load_round_trip(self, tmp_path: Path) -> None:
        """Verify save-to-JSON and load-from-JSON preserves all attributes, types, and precision."""
        sig1 = ZoneSignature(
            zone_id=1,
            subcarrier_count=3,
            mean_variance_per_subcarrier=(0.105, 0.205, 0.305),
            mean_aggregate_variance=0.615,
            sample_count=12,
        )
        sig2 = ZoneSignature(
            zone_id=2,
            subcarrier_count=3,
            mean_variance_per_subcarrier=(0.405, 0.505, 0.605),
            mean_aggregate_variance=1.515,
            sample_count=15,
        )
        original_profile = CalibrationProfile(
            zone_count=2,
            subcarrier_count=3,
            calibration_version=3,
            created_at="2026-09-15T12:34:56Z",
            source="hardware",
            zone_signatures={1: sig1, 2: sig2},
        )

        file_path = tmp_path / "test_profile.json"
        original_profile.to_json_file(file_path)
        assert file_path.is_file()

        loaded_profile = CalibrationProfile.from_json_file(file_path)
        assert loaded_profile.zone_count == original_profile.zone_count
        assert loaded_profile.subcarrier_count == original_profile.subcarrier_count
        assert loaded_profile.calibration_version == original_profile.calibration_version
        assert loaded_profile.created_at == original_profile.created_at
        assert loaded_profile.source == original_profile.source
        assert loaded_profile == original_profile

    def test_labeled_feature_sample_validation(self) -> None:
        """Verify LabeledFeatureSample correctly pairs FeatureVector with optional zone_id."""
        fv = make_test_feature_vector(1.0, (0.1, 0.2))

        # Quiet baseline (zone_id is None)
        baseline = LabeledFeatureSample(feature_vector=fv, zone_id=None)
        assert baseline.zone_id is None

        # Active zone motion
        active = LabeledFeatureSample(feature_vector=fv, zone_id=2)
        assert active.zone_id == 2

        # Invalid zone_id (< 1)
        with pytest.raises(ValueError, match="zone_id must be >= 1"):
            LabeledFeatureSample(feature_vector=fv, zone_id=0)

        # Invalid feature_vector type
        with pytest.raises(TypeError, match="must be an instance of FeatureVector"):
            LabeledFeatureSample(feature_vector="not_a_vector", zone_id=1)  # type: ignore


class TestZoneCalibrator:
    """Validate ZoneCalibrator batch execution, calculations, and N-zone genericity."""

    def test_calibrator_configuration(self) -> None:
        """Verify calibrator constructor constraints."""
        with pytest.raises(ValueError, match="zone_count must be >= 1"):
            ZoneCalibrator(zone_count=0, subcarrier_count=4)
        with pytest.raises(ValueError, match="subcarrier_count must be >= 1"):
            ZoneCalibrator(zone_count=4, subcarrier_count=0)
        with pytest.raises(ValueError, match="calibration_version must be >= 1"):
            ZoneCalibrator(zone_count=4, subcarrier_count=4, calibration_version=0)
        with pytest.raises(ValueError, match="source must be one of"):
            ZoneCalibrator(zone_count=4, subcarrier_count=4, source="invalid")

    def test_missing_zone_samples_raises(self) -> None:
        """Calibrating when a configured zone has zero samples must raise ValueError."""
        calibrator = ZoneCalibrator(zone_count=3, subcarrier_count=2)
        fv = make_test_feature_vector(1.0, (0.1, 0.2))
        samples = [
            LabeledFeatureSample(fv, zone_id=1),
            LabeledFeatureSample(fv, zone_id=2),
            # Missing Zone 3!
        ]
        with pytest.raises(ValueError, match="missing labeled samples for zone"):
            calibrator.calibrate(samples)

    def test_subcarrier_mismatch_raises(self) -> None:
        """Sample with mismatched subcarriers must raise ValueError."""
        calibrator = ZoneCalibrator(zone_count=2, subcarrier_count=3)
        fv = make_test_feature_vector(1.0, (0.1, 0.2))  # only 2 subcarriers!
        samples = [LabeledFeatureSample(fv, zone_id=1)]
        with pytest.raises(ValueError, match="has subcarrier_count 2, expected 3"):
            calibrator.calibrate(samples)

    def test_sample_exceeding_zone_count_raises(self) -> None:
        """Sample with zone_id > zone_count must raise ValueError."""
        calibrator = ZoneCalibrator(zone_count=2, subcarrier_count=2)
        fv = make_test_feature_vector(1.0, (0.1, 0.2))
        samples = [LabeledFeatureSample(fv, zone_id=3)]
        with pytest.raises(ValueError, match="exceeds configured zone_count 2"):
            calibrator.calibrate(samples)

    def test_generic_n_zone_calibration_2_and_5_zones(self) -> None:
        """Verify calibration works generically for N=2 and N=5 zones (zero hardcoding of 4)."""
        for n_zones in (2, 5):
            calibrator = ZoneCalibrator(
                zone_count=n_zones,
                subcarrier_count=4,
                calibration_version=1,
            )
            samples: list[LabeledFeatureSample] = []
            for z in range(1, n_zones + 1):
                for i in range(3):
                    # Distinct pattern per zone: peak at subcarrier index z % 4
                    variances = tuple(
                        0.5 if (k == (z - 1) % 4) else 0.1 for k in range(4)
                    )
                    fv = make_test_feature_vector(
                        ts=float(z * 10 + i),
                        variances=variances,
                    )
                    samples.append(LabeledFeatureSample(feature_vector=fv, zone_id=z))

            profile = calibrator.calibrate(samples)
            assert profile.zone_count == n_zones
            assert len(profile.zone_signatures) == n_zones
            for z in range(1, n_zones + 1):
                sig = profile.get_signature(z)
                assert sig.zone_id == z
                assert sig.sample_count == 3
                # Verify element-wise mean
                expected_sc = (z - 1) % 4
                assert sig.mean_variance_per_subcarrier[expected_sc] == pytest.approx(0.5)


class TestFullPipelineCalibrationIntegration:
    """Full pipeline integration test: Synthetic scenario -> Preprocessor -> Features -> Calibration."""

    def test_end_to_end_multizone_calibration(self) -> None:
        """Run a multi-zone synthetic scenario (motion in Zones 1, 2, 3, 4) through the pipeline,

        externally label FeatureVectors via scenario event timing, and verify ZoneCalibrator
        produces distinct, physically sensible ZoneSignatures.
        """
        # 1. Ingestion: 4 zones, 64 subcarriers, 24 seconds at 20 Hz (480 frames)
        # Schedule sequential motion in Zone 1, Zone 2, Zone 3, Zone 4
        scenario = ScenarioConfig(
            scenario_name="multizone_calibration_session",
            description="Sequential motion across 4 zones for calibration",
            sample_rate_hz=20.0,
            subcarrier_count=64,
            zone_count=4,
            duration_seconds=24.0,
            realtime=False,
            random_seed=12345,
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

        # 2. Preprocessing
        prep_cfg_path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "preprocessing_config.json"
        )
        preprocessor = PreprocessingPipeline.from_json_file(prep_cfg_path)
        cleaned_records = list(preprocessor.process_stream(raw_records))

        # 3. Feature Extraction (1.0s window = 20 samples, step 1)
        feat_cfg = VarianceExtractorConfig(window_size_samples=20, window_step_samples=1)
        extractor = VarianceFeatureExtractor(feat_cfg)
        feature_vectors = list(extractor.process_stream(cleaned_records))
        assert len(feature_vectors) > 0

        # 4. External Ground-Truth Labeling (Offline Calibration Step)
        # Check active event at window midpoint relative to stream start epoch
        start_epoch = raw_records[0].timestamp
        labeled_samples: list[LabeledFeatureSample] = []

        for fv in feature_vectors:
            midpoint = (fv.window_start_timestamp + fv.window_end_timestamp) / 2.0
            rel_time = midpoint - start_epoch
            active_events = [
                e for e in scenario.events if e.start_time_s <= rel_time <= e.end_time_s
            ]

            if len(active_events) == 1:
                labeled_samples.append(
                    LabeledFeatureSample(feature_vector=fv, zone_id=active_events[0].zone_id)
                )
            elif len(active_events) == 0:
                labeled_samples.append(
                    LabeledFeatureSample(feature_vector=fv, zone_id=None)
                )

        # Verify we captured samples for all 4 zones
        counts_by_zone = {z: sum(1 for s in labeled_samples if s.zone_id == z) for z in range(1, 5)}
        for z, count in counts_by_zone.items():
            assert count >= 30, f"Zone {z} has too few samples ({count})"

        # 5. Zone Calibration
        calibrator = ZoneCalibrator(
            zone_count=4,
            subcarrier_count=64,
            calibration_version=1,
            source="synthetic",
        )
        profile = calibrator.calibrate(labeled_samples)

        assert profile.zone_count == 4
        assert profile.subcarrier_count == 64

        # 6. Verify Zone Signatures reflect subcarrier spatial sensitivity:
        # In SyntheticCSISource:
        # Zone 1 center = ((1 - 0.5)/4) * 64 = 8
        # Zone 4 center = ((4 - 0.5)/4) * 64 = 56
        sig1 = profile.get_signature(1)
        sig4 = profile.get_signature(4)

        peak_sc_zone1 = max(range(64), key=lambda k: sig1.mean_variance_per_subcarrier[k])
        peak_sc_zone4 = max(range(64), key=lambda k: sig4.mean_variance_per_subcarrier[k])

        # Zone 1 peak should be near subcarrier 8 (+/- 3 subcarriers due to noise and ripple)
        assert abs(peak_sc_zone1 - 8) <= 4, f"Zone 1 peak subcarrier {peak_sc_zone1} not near 8"
        # Zone 4 peak should be near subcarrier 56 (+/- 3 subcarriers)
        assert abs(peak_sc_zone4 - 56) <= 4, f"Zone 4 peak subcarrier {peak_sc_zone4} not near 56"

        # Zone 1 should have substantially higher variance at sc=8 than Zone 4
        assert sig1.mean_variance_per_subcarrier[8] > sig4.mean_variance_per_subcarrier[8] * 2.0
        # Zone 4 should have substantially higher variance at sc=56 than Zone 1
        assert sig4.mean_variance_per_subcarrier[56] > sig1.mean_variance_per_subcarrier[56] * 2.0
