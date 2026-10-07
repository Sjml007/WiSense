"""Unit and integration tests for SQLiteDatabaseManager, EventLogRecord, and full pipeline persistence."""

from pathlib import Path

import pytest

from wisense_backend.calibration.calibrator import ZoneCalibrator
from wisense_backend.calibration.labeled_sample import LabeledFeatureSample
from wisense_backend.classification.nearest_signature_classifier import (
    NearestSignatureZoneClassifier,
    ZoneClassifierConfig,
)
from wisense_backend.contracts.database import DatabaseManager, EventLogRecord
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
from wisense_backend.persistence.sqlite_manager import (
    DatabaseConfig,
    SQLiteDatabaseManager,
)
from wisense_backend.preprocessing.pipeline import PreprocessingPipeline


class TestEventLogRecordContracts:
    """Validate EventLogRecord invariants, naming discipline, and Protocol compliance."""

    def test_record_valid_creation(self) -> None:
        """Verify valid record attributes."""
        record = EventLogRecord(
            run_id="run_test_001",
            event_timestamp=1700000002.0,
            window_start_timestamp=1700000001.0,
            window_end_timestamp=1700000002.0,
            sample_count=20,
            is_motion=True,
            presence=True,
            zone_id=2,
            separation_margin=0.25,
            calibration_version=1,
            aggregate_variance=0.65,
            window_latency_ms=1000.0,
            pipeline_latency_ms=15.2,
            ground_truth_zone=2,
            ground_truth_motion=True,
        )
        assert record.run_id == "run_test_001"
        assert record.zone_id == 2
        assert record.is_motion is True
        assert record.window_latency_ms == 1000.0
        assert record.pipeline_latency_ms == 15.2

    def test_motion_gating_invariant_enforcement(self) -> None:
        """When is_motion is False, zone_id and separation_margin MUST be None."""
        with pytest.raises(ValueError, match="zone_id must be None when is_motion is False"):
            EventLogRecord(
                run_id="run_001",
                event_timestamp=2.0,
                window_start_timestamp=1.0,
                window_end_timestamp=2.0,
                sample_count=20,
                is_motion=False,
                presence=False,
                zone_id=1,  # Invalid: cannot have zone when no motion
                separation_margin=None,
                calibration_version=1,
                aggregate_variance=0.01,
                window_latency_ms=1000.0,
            )

        with pytest.raises(ValueError, match="separation_margin must be None when is_motion is False"):
            EventLogRecord(
                run_id="run_001",
                event_timestamp=2.0,
                window_start_timestamp=1.0,
                window_end_timestamp=2.0,
                sample_count=20,
                is_motion=False,
                presence=False,
                zone_id=None,
                separation_margin=0.10,  # Invalid: cannot have margin when no motion
                calibration_version=1,
                aggregate_variance=0.01,
                window_latency_ms=1000.0,
            )

    def test_contract_validation_errors(self) -> None:
        """Verify boundary checks on run_id, timestamps, sample_count, and latencies."""
        # Empty run_id
        with pytest.raises(ValueError, match="run_id must be a non-empty string"):
            EventLogRecord(
                run_id="",
                event_timestamp=2.0,
                window_start_timestamp=1.0,
                window_end_timestamp=2.0,
                sample_count=20,
                is_motion=False,
                presence=False,
                zone_id=None,
                separation_margin=None,
                calibration_version=1,
                aggregate_variance=0.01,
                window_latency_ms=1000.0,
            )

        # Inverted window timestamps
        with pytest.raises(ValueError, match="window_end_timestamp .* must be >="):
            EventLogRecord(
                run_id="run_001",
                event_timestamp=2.0,
                window_start_timestamp=3.0,
                window_end_timestamp=2.0,
                sample_count=20,
                is_motion=False,
                presence=False,
                zone_id=None,
                separation_margin=None,
                calibration_version=1,
                aggregate_variance=0.01,
                window_latency_ms=1000.0,
            )

        # Negative window latency
        with pytest.raises(ValueError, match="window_latency_ms must be a finite non-negative number"):
            EventLogRecord(
                run_id="run_001",
                event_timestamp=2.0,
                window_start_timestamp=1.0,
                window_end_timestamp=2.0,
                sample_count=20,
                is_motion=False,
                presence=False,
                zone_id=None,
                separation_margin=None,
                calibration_version=1,
                aggregate_variance=0.01,
                window_latency_ms=-1.0,
            )

    def test_strict_naming_discipline_no_confidence(self) -> None:
        """Per Audit Items #13 and #16, assert no confidence attributes exist."""
        record = EventLogRecord(
            run_id="run_001",
            event_timestamp=2.0,
            window_start_timestamp=1.0,
            window_end_timestamp=2.0,
            sample_count=20,
            is_motion=True,
            presence=True,
            zone_id=1,
            separation_margin=0.20,
            calibration_version=1,
            aggregate_variance=0.5,
            window_latency_ms=1000.0,
        )
        assert not hasattr(record, "confidence")
        for attr in dir(record):
            assert "confidence" not in attr.lower(), f"Forbidden word 'confidence' found in attribute '{attr}'"

    def test_dict_serialization_round_trip(self) -> None:
        """Verify to_dict and from_dict survive cleanly."""
        record = EventLogRecord(
            run_id="run_roundtrip_001",
            event_timestamp=1700000010.5,
            window_start_timestamp=1700000009.5,
            window_end_timestamp=1700000010.5,
            sample_count=20,
            is_motion=True,
            presence=True,
            zone_id=3,
            separation_margin=0.154321,
            calibration_version=2,
            aggregate_variance=0.887766,
            window_latency_ms=1000.0,
            pipeline_latency_ms=12.4,
            ground_truth_zone=3,
            ground_truth_motion=True,
        )
        d = record.to_dict()
        loaded = EventLogRecord.from_dict(d)
        assert loaded == record


class TestSQLiteDatabaseManager:
    """Validate SQLite database operations, transactions, latency separation, and queries."""

    def test_protocol_conformance(self) -> None:
        """Verify SQLiteDatabaseManager implements DatabaseManager protocol."""
        manager = SQLiteDatabaseManager(":memory:")
        assert isinstance(manager, DatabaseManager)

    def test_database_config_loader(self) -> None:
        """Verify DatabaseConfig loading from JSON."""
        config_path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "database_config.json"
        )
        assert config_path.is_file()
        cfg = DatabaseConfig.from_json_file(config_path)
        assert cfg.database_path == "data/wisense.db"
        assert cfg.timeout_seconds == 10.0
        assert cfg.wal_mode is True

    def test_single_insert_and_query_nullables_roundtrip(self) -> None:
        """Insert a record with None values for all optional fields and verify query roundtrip."""
        with SQLiteDatabaseManager(":memory:") as db:
            record = EventLogRecord(
                run_id="session_alpha",
                event_timestamp=100.0,
                window_start_timestamp=99.0,
                window_end_timestamp=100.0,
                sample_count=20,
                is_motion=False,
                presence=False,
                zone_id=None,
                separation_margin=None,
                calibration_version=None,
                aggregate_variance=0.005,
                window_latency_ms=1000.0,
                pipeline_latency_ms=None,
                ground_truth_zone=None,
                ground_truth_motion=None,
            )
            db.insert_event(record)

            results = db.query_by_run_id("session_alpha")
            assert len(results) == 1
            retrieved = results[0]
            assert retrieved == record
            assert retrieved.zone_id is None
            assert retrieved.separation_margin is None
            assert retrieved.calibration_version is None
            assert retrieved.pipeline_latency_ms is None
            assert retrieved.ground_truth_zone is None
            assert retrieved.ground_truth_motion is None

    def test_batch_insert_and_ordered_retrieval(self) -> None:
        """Verify batch insertion and chronological ordering by event_timestamp."""
        with SQLiteDatabaseManager(":memory:") as db:
            records = [
                EventLogRecord(
                    run_id="batch_run",
                    event_timestamp=10.0 + i,
                    window_start_timestamp=9.0 + i,
                    window_end_timestamp=10.0 + i,
                    sample_count=20,
                    is_motion=True,
                    presence=True,
                    zone_id=1,
                    separation_margin=0.2,
                    calibration_version=1,
                    aggregate_variance=0.5,
                    window_latency_ms=1000.0,
                )
                for i in range(5)
            ]
            db.insert_events_batch(records)

            results = db.query_by_run_id("batch_run")
            assert len(results) == 5
            for i, r in enumerate(results):
                assert r.event_timestamp == 10.0 + i

    def test_latency_separation_audit_item_14(self) -> None:
        """Verify window_latency_ms and pipeline_latency_ms remain strictly separate.

        - Non-realtime batch synthetic runs MUST have pipeline_latency_ms=None.
        - Realtime / hardware runs store measured wall-clock pipeline_latency_ms.
        """
        with SQLiteDatabaseManager(":memory:") as db:
            # Record 1: Synthetic batch run (pipeline_latency_ms=None)
            batch_record = EventLogRecord(
                run_id="latency_audit_run",
                event_timestamp=101.0,
                window_start_timestamp=100.0,
                window_end_timestamp=101.0,
                sample_count=20,
                is_motion=False,
                presence=False,
                zone_id=None,
                separation_margin=None,
                calibration_version=1,
                aggregate_variance=0.01,
                window_latency_ms=1000.0,
                pipeline_latency_ms=None,
            )

            # Record 2: Realtime hardware run (measured pipeline_latency_ms=8.4ms)
            realtime_record = EventLogRecord(
                run_id="latency_audit_run",
                event_timestamp=102.0,
                window_start_timestamp=101.0,
                window_end_timestamp=102.0,
                sample_count=20,
                is_motion=True,
                presence=True,
                zone_id=2,
                separation_margin=0.3,
                calibration_version=1,
                aggregate_variance=0.8,
                window_latency_ms=1000.0,
                pipeline_latency_ms=8.4,
            )

            db.insert_events_batch([batch_record, realtime_record])

            results = db.query_by_run_id("latency_audit_run")
            assert len(results) == 2

            # Assert distinctness
            assert results[0].window_latency_ms == 1000.0
            assert results[0].pipeline_latency_ms is None

            assert results[1].window_latency_ms == 1000.0
            assert results[1].pipeline_latency_ms == 8.4

    def test_ground_truth_fields_isolation_and_persistence(self) -> None:
        """Verify ground_truth fields default to None during normal execution and persist when provided."""
        with SQLiteDatabaseManager(":memory:") as db:
            # Normal runtime record
            normal_rec = EventLogRecord(
                run_id="gt_test_run",
                event_timestamp=101.0,
                window_start_timestamp=100.0,
                window_end_timestamp=101.0,
                sample_count=20,
                is_motion=True,
                presence=True,
                zone_id=1,
                separation_margin=0.2,
                calibration_version=1,
                aggregate_variance=0.5,
                window_latency_ms=1000.0,
            )
            assert normal_rec.ground_truth_zone is None
            assert normal_rec.ground_truth_motion is None

            # Test-harness record with explicit ground truth
            labeled_rec = EventLogRecord(
                run_id="gt_test_run",
                event_timestamp=102.0,
                window_start_timestamp=101.0,
                window_end_timestamp=102.0,
                sample_count=20,
                is_motion=True,
                presence=True,
                zone_id=1,
                separation_margin=0.2,
                calibration_version=1,
                aggregate_variance=0.5,
                window_latency_ms=1000.0,
                ground_truth_zone=1,
                ground_truth_motion=True,
            )

            db.insert_events_batch([normal_rec, labeled_rec])

            results = db.query_by_run_id("gt_test_run")
            assert len(results) == 2
            assert results[0].ground_truth_zone is None
            assert results[0].ground_truth_motion is None
            assert results[1].ground_truth_zone == 1
            assert results[1].ground_truth_motion is True

    def test_unopened_connection_raises(self) -> None:
        """Executing queries before connect() must raise RuntimeError."""
        manager = SQLiteDatabaseManager(":memory:")
        record = EventLogRecord(
            run_id="r1", event_timestamp=101.0, window_start_timestamp=100.0, window_end_timestamp=101.0,
            sample_count=20, is_motion=False, presence=False, zone_id=None, separation_margin=None,
            calibration_version=1, aggregate_variance=0.1, window_latency_ms=1000.0,
        )
        with pytest.raises(RuntimeError, match="Database connection is not open"):
            manager.insert_event(record)
        with pytest.raises(RuntimeError, match="Database connection is not open"):
            manager.query_by_run_id("r1")


class TestEndToEnd8StagePipelinePersistenceIntegration:
    """Full 8-stage pipeline integration into persistence:

    Sections 1 (Synthetic Source) -> 4 (Preprocessing) -> 5 (Features)
    -> 6 (Motion Detector) -> 7 (Calibration) -> 8 (Classification) -> 9 (SQLite Persistence).
    """

    def test_full_pipeline_persistence_roundtrip(self, tmp_path: Path) -> None:
        # Step 1: Ingestion
        scenario = ScenarioConfig(
            scenario_name="e2e_persistence_pipeline",
            description="Sequential motion across 4 zones",
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

        # Step 3: Feature Extraction (window size 20 = 1.0s at 20 Hz, step 1)
        # Config-derived window latency: 20 / 20.0 * 1000 = 1000.0 ms
        window_size_samples = 20
        sample_rate_hz = 20.0
        expected_window_latency_ms = (window_size_samples / sample_rate_hz) * 1000.0

        feat_extractor = VarianceFeatureExtractor(
            VarianceExtractorConfig(window_size_samples=window_size_samples, window_step_samples=1)
        )
        feature_vectors = list(feat_extractor.process_stream(cleaned_records))
        assert len(feature_vectors) == 461

        # Step 4: Motion Detection
        motion_detector = ThresholdMotionDetector(
            ThresholdMotionConfig(enter_threshold=0.10, exit_threshold=0.05, presence_timeout_seconds=2.0)
        )
        motion_states = list(motion_detector.process_stream(feature_vectors))

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

        # Step 6: Zone Classification
        classifier = NearestSignatureZoneClassifier(
            profile,
            config=ZoneClassifierConfig(metric="cosine_distance", min_separation_margin=0.02),
        )
        classifications = list(classifier.classify_stream(feature_vectors, motion_states))
        assert len(classifications) == 461

        # Step 7: Convert to EventLogRecords under a shared session run_id
        session_run_id = "run_session_e2e_full_001"
        event_records: list[EventLogRecord] = []
        for fv, ms, zc in zip(feature_vectors, motion_states, classifications):
            rec = EventLogRecord(
                run_id=session_run_id,
                event_timestamp=zc.timestamp,
                window_start_timestamp=fv.window_start_timestamp,
                window_end_timestamp=fv.window_end_timestamp,
                sample_count=fv.sample_count,
                is_motion=ms.is_motion,
                presence=ms.presence,
                zone_id=zc.zone_id,
                separation_margin=zc.separation_margin,
                calibration_version=zc.calibration_version,
                aggregate_variance=fv.aggregate_variance,
                window_latency_ms=expected_window_latency_ms,
                pipeline_latency_ms=None,  # Non-realtime synthetic run per Audit Item #14
            )
            event_records.append(rec)

        # Step 8: Persistence via SQLiteDatabaseManager
        db_path = tmp_path / "wisense_e2e.db"
        with SQLiteDatabaseManager(db_path) as db:
            db.insert_events_batch(event_records)

            # Query back by run_id
            persisted_records = db.query_by_run_id(session_run_id)
            assert len(persisted_records) == 461

            # Spot-check known intervals:

            # 1. Quiet pre-motion interval (rel_t < 1.5s)
            pre_records = [r for r in persisted_records if (r.event_timestamp - start_epoch) < 1.5]
            assert len(pre_records) > 0
            for r in pre_records:
                assert r.is_motion is False
                assert r.zone_id is None
                assert r.separation_margin is None
                assert r.window_latency_ms == 1000.0
                assert r.pipeline_latency_ms is None

            # 2. Zone 1 active interval (rel_t in [3.0s, 4.5s])
            z1_records = [r for r in persisted_records if 3.0 <= (r.event_timestamp - start_epoch) <= 4.5]
            assert len(z1_records) > 0
            for r in z1_records:
                assert r.is_motion is True
                assert r.zone_id == 1
                assert r.separation_margin is not None and r.separation_margin > 0.0
                assert r.window_latency_ms == 1000.0
                assert r.pipeline_latency_ms is None

            # 3. Zone 3 active interval (rel_t in [13.0s, 14.5s])
            z3_records = [r for r in persisted_records if 13.0 <= (r.event_timestamp - start_epoch) <= 14.5]
            assert len(z3_records) > 0
            for r in z3_records:
                assert r.is_motion is True
                assert r.zone_id == 3
                assert r.separation_margin is not None and r.separation_margin > 0.0
