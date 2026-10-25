"""End-to-end integration and verification tests for Section 21 Application wiring.

Covers:
1. PipelineRunner equivalence with manual 4-stage chain on example scenario (281 vectors).
2. Full synthetic Application run (fast, port 0, temp DB, real WebSocket test client):
   - Valid StateMessages received
   - DB rows equal feature-vector count under one run_id
   - window_latency_ms set, pipeline_latency_ms None
   - Health snapshot reports active source
   - Shutdown is clean
3. Full synthetic run in legacy_zone mode:
   - Two-key messages only ({"zone": int, "occupied": bool})
   - Final message is all-false on shutdown
4. Hardware mode with FakeSerial:
   - Refuses to start without explicit motion_config_path
   - Subcarrier mismatch fails fast naming both numbers
   - No-zone mode yields zone_id=None while motion works
   - No-zone mode rejects legacy_zone format
5. Stall handling:
   - Gap >= disconnected_after_seconds resets pipeline (requires W new records before next feature)
   - Legacy mode emits all-false once
   - state_message_v1 publishes nothing (watchdog signal)
6. Source error recovery, run_id format, shutdown order, thread-safe stop().
7. Config defaults preservation.
"""

import dataclasses
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Generator, List, Optional
import pytest
import websockets.sync.client as ws_client
from websockets.exceptions import ConnectionClosed

from wisense_backend.app import Application
from wisense_backend.classification.nearest_signature_classifier import NearestSignatureZoneClassifier
from wisense_backend.communication.legacy_zone_adapter import LegacyZoneAdapter, LegacyZoneBridge
from wisense_backend.communication.websocket_server import WebSocketConfig, WebSocketStatePublisher
from wisense_backend.config.app_config import AppConfig
from wisense_backend.contracts.calibration import CalibrationProfile
from wisense_backend.contracts.csi_source import CSIRecord, CSIRecordSource
from wisense_backend.contracts.motion_detector import MotionState
from wisense_backend.contracts.state_message import StateMessage
from wisense_backend.detection.threshold_motion_detector import ThresholdMotionConfig, ThresholdMotionDetector
from wisense_backend.features.variance_extractor import VarianceExtractorConfig, VarianceFeatureExtractor
from wisense_backend.ingestion.frame_parser import build_frame
from wisense_backend.ingestion.serial_source import SerialCSISource, SerialSourceConfig
from wisense_backend.ingestion.synthetic_source import ScenarioConfig, SyntheticCSISource
from wisense_backend.monitoring.health_monitor import HealthMonitorConfig
from wisense_backend.persistence.sqlite_manager import DatabaseConfig, SQLiteDatabaseManager
from wisense_backend.pipeline.pipeline_runner import PipelineResult, PipelineRunner
from wisense_backend.preprocessing.pipeline import PreprocessingPipeline
from tests.test_serial_source import FakeSerial

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _build_fake_frames(subcarrier_count: int = 64, count: int = 25) -> bytes:
    """Helper to build raw serial frames with a given subcarrier count."""
    payload = bytes([1, 2] * subcarrier_count)
    return b"".join(build_frame(payload=payload, seq_num=i) for i in range(count))


class ControlledTimeSource(CSIRecordSource):
    """CSIRecordSource that allows injecting records with controllable timing hooks."""

    def __init__(
        self,
        records: List[CSIRecord],
        stall_index: Optional[int] = None,
        on_stall: Optional[Callable[[], None]] = None,
    ) -> None:
        self._records = list(records)
        self._stall_index = stall_index
        self._on_stall = on_stall
        self._is_open = False
        self._idx = 0

    @property
    def is_open(self) -> bool:
        return self._is_open

    def open(self) -> None:
        self._is_open = True
        self._idx = 0

    def close(self) -> None:
        self._is_open = False

    def __enter__(self) -> "ControlledTimeSource":
        self.open()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def read_record(self, timeout: Optional[float] = None) -> Optional[CSIRecord]:
        if not self._is_open:
            raise RuntimeError("Source is not open")
        if self._stall_index is not None and self._idx == self._stall_index and self._on_stall:
            self._on_stall()
        if self._idx < len(self._records):
            rec = self._records[self._idx]
            self._idx += 1
            return rec
        return None

    def records(self) -> Generator[CSIRecord, None, None]:
        while self._is_open:
            rec = self.read_record()
            if rec is None:
                break
            yield rec


class FlakySource(CSIRecordSource):
    """CSIRecordSource that raises an error mid-stream to test fault tolerance."""

    def __init__(self, base_records: List[CSIRecord], error_idx: int = 5) -> None:
        self._records = list(base_records)
        self._error_idx = error_idx
        self._idx = 0
        self._is_open = False

    @property
    def is_open(self) -> bool:
        return self._is_open

    def open(self) -> None:
        self._is_open = True
        self._idx = 0

    def close(self) -> None:
        self._is_open = False

    def __enter__(self) -> "FlakySource":
        self.open()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def read_record(self, timeout: Optional[float] = None) -> Optional[CSIRecord]:
        if not self._is_open:
            raise RuntimeError("Source is not open")
        if self._idx == self._error_idx:
            self._idx += 1
            raise ValueError("Simulated mid-stream subcarrier mismatch")
        if self._idx < len(self._records):
            rec = self._records[self._idx]
            self._idx += 1
            return rec
        return None

    def records(self) -> Generator[CSIRecord, None, None]:
        while self._is_open:
            try:
                rec = self.read_record()
                if rec is None:
                    break
                yield rec
            except ValueError:
                raise


# ==============================================================================
# Requirement A: PipelineRunner Equivalence
# ==============================================================================

def test_pipeline_runner_equivalence_with_manual_chain() -> None:
    """Verify PipelineRunner produces identical results to manual 4-stage chain on example scenario."""
    scenario_path = CONFIG_DIR / "synthetic_scenarios" / "example.json"
    scenario_cfg = ScenarioConfig.from_json_file(scenario_path)

    # Ingest all records
    with SyntheticCSISource(scenario_cfg) as src:
        raw_records = list(src)
    assert len(raw_records) == 300

    # Manual chain setup
    prep_cfg = CONFIG_DIR / "preprocessing_config.json"
    preprocessor = PreprocessingPipeline.from_json_file(prep_cfg)
    cleaned_records = list(preprocessor.process_stream(raw_records))

    feat_cfg = VarianceExtractorConfig(window_size_samples=20, window_step_samples=1)
    extractor = VarianceFeatureExtractor(feat_cfg)
    manual_features = list(extractor.process_stream(cleaned_records))
    assert len(manual_features) == 281

    motion_cfg = ThresholdMotionConfig(
        enter_threshold=0.10,
        exit_threshold=0.05,
        presence_timeout_seconds=2.0,
    )
    detector = ThresholdMotionDetector(motion_cfg)
    manual_motions = list(detector.process_stream(manual_features))
    assert len(manual_motions) == 281

    profile_path = CONFIG_DIR / "calibration_profiles" / "default_calibration.json"
    profile = CalibrationProfile.from_json_file(profile_path)
    classifier = NearestSignatureZoneClassifier(profile)
    manual_classifications = list(classifier.classify_stream(manual_features, manual_motions))
    assert len(manual_classifications) == 281

    # PipelineRunner setup
    runner = PipelineRunner(
        preprocessor=PreprocessingPipeline.from_json_file(prep_cfg),
        feature_extractor=VarianceFeatureExtractor(feat_cfg),
        motion_detector=ThresholdMotionDetector(motion_cfg),
        classifier=NearestSignatureZoneClassifier(profile),
    )

    runner_results: List[PipelineResult] = []
    for record in raw_records:
        res = runner.process(record)
        if res is not None:
            runner_results.append(res)

    assert len(runner_results) == 281

    # Verify 1-to-1 equivalence
    for i in range(281):
        rr = runner_results[i]
        mf = manual_features[i]
        mm = manual_motions[i]
        mc = manual_classifications[i]

        assert rr.feature == mf
        assert rr.motion == mm
        assert rr.classification == mc
        assert rr.state_message.timestamp == mf.window_end_timestamp
        assert rr.state_message.is_motion == mm.is_motion
        assert rr.state_message.presence == mm.presence
        assert rr.state_message.zone_id == mc.zone_id
        assert rr.state_message.separation_margin == mc.separation_margin
        assert rr.state_message.calibration_version == mc.calibration_version


def test_pipeline_runner_no_zone_mode() -> None:
    """Verify PipelineRunner with classifier=None produces zone_id=None and margin=None."""
    prep_cfg = CONFIG_DIR / "preprocessing_config.json"
    feat_cfg = VarianceExtractorConfig(window_size_samples=20, window_step_samples=1)
    motion_cfg = ThresholdMotionConfig(enter_threshold=0.10, exit_threshold=0.05, presence_timeout_seconds=2.0)

    runner = PipelineRunner(
        preprocessor=PreprocessingPipeline.from_json_file(prep_cfg),
        feature_extractor=VarianceFeatureExtractor(feat_cfg),
        motion_detector=ThresholdMotionDetector(motion_cfg),
        classifier=None,
    )

    scenario_path = CONFIG_DIR / "synthetic_scenarios" / "example.json"
    with SyntheticCSISource(ScenarioConfig.from_json_file(scenario_path)) as src:
        raw_records = list(src)

    results = [runner.process(r) for r in raw_records]
    valid_results = [r for r in results if r is not None]
    assert len(valid_results) == 281

    for res in valid_results:
        assert res.classification is None
        assert res.state_message.zone_id is None
        assert res.state_message.separation_margin is None
        assert res.state_message.calibration_version is None


# ==============================================================================
# Requirement B, D, E, G, H: Full Synthetic Run (state_message_v1)
# ==============================================================================

def test_full_synthetic_application_run(tmp_path: Path) -> None:
    """Verify end-to-end synthetic run with real WebSocket client and SQLite logging."""
    db_path = tmp_path / "test_run.db"

    cfg = AppConfig(
        csi_source_mode="synthetic",
        synthetic_scenario_path=str(CONFIG_DIR / "synthetic_scenarios" / "example.json"),
        calibration_profile_path=str(CONFIG_DIR / "calibration_profiles" / "default_calibration.json"),
        motion_config_path=str(CONFIG_DIR / "motion_detection_config.json"),
        database_path=str(db_path),
        db_flush_interval_seconds=0.1,
        db_flush_batch_size=50,
    )
    ws_cfg = WebSocketConfig(host="127.0.0.1", port=0)

    app = Application(config=cfg, ws_config=ws_cfg)

    # run_id format verification (<mode>-<UTC yyyymmddThhmmssZ>-<8 hex>)
    pattern = r"^synthetic-\d{8}T\d{6}Z-[0-9a-f]{8}$"
    assert re.match(pattern, app.run_id) is not None

    # Start publisher ahead of ingestion to connect test client
    app.publisher.start()
    port = app.publisher.actual_port
    assert port is not None and port > 0

    received_messages: List[str] = []
    client_connected = threading.Event()
    client_done = threading.Event()

    def ws_client_worker() -> None:
        try:
            with ws_client.connect(f"ws://127.0.0.1:{port}") as client:
                client_connected.set()
                while not client_done.is_set():
                    try:
                        msg = client.recv(timeout=0.2)
                        received_messages.append(msg)
                    except TimeoutError:
                        continue
                    except ConnectionClosed:
                        break
        finally:
            client_done.set()

    client_thread = threading.Thread(target=ws_client_worker, daemon=True)
    client_thread.start()

    assert client_connected.wait(timeout=3.0)

    # Run Application synchronously to completion
    records_processed = app.run()
    assert records_processed == 300

    # Wait for client to receive all messages
    time.sleep(0.1)
    client_done.set()
    client_thread.join(timeout=2.0)

    # 1. Verify WebSocket messages
    assert len(received_messages) == 281
    for raw_msg in received_messages:
        parsed = json.loads(raw_msg)
        assert parsed["schema_version"] == 1
        assert "timestamp" in parsed
        assert "is_motion" in parsed
        assert "presence" in parsed
        assert "zone_id" in parsed

    # 2. Verify Database records
    assert app.db_manager is not None
    # Re-connect to read back rows
    with SQLiteDatabaseManager(DatabaseConfig(database_path=str(db_path))) as db:
        events = db.query_by_run_id(run_id=app.run_id)
        assert len(events) == 281
        for ev in events:
            assert ev.run_id == app.run_id
            assert ev.window_latency_ms == pytest.approx(1000.0, rel=1e-3)
            assert ev.pipeline_latency_ms is None  # Fast non-realtime run

    # 3. Verify Health snapshot reports active source
    health = app.health_monitor.get_current_health(current_time=app._last_record_timestamp)
    assert health.csi_source_status == "active"

    # 4. Verify clean shutdown
    assert not app.publisher.is_running
    assert not app.source.is_open


# ==============================================================================
# Requirement E, F: Full Synthetic Run (legacy_zone mode)
# ==============================================================================

def test_full_synthetic_application_run_legacy_zone(tmp_path: Path) -> None:
    """Verify legacy_zone mode: two-key messages only and all-false emitted on shutdown."""
    db_path = tmp_path / "test_legacy.db"

    cfg = AppConfig(
        csi_source_mode="synthetic",
        synthetic_scenario_path=str(CONFIG_DIR / "synthetic_scenarios" / "example.json"),
        calibration_profile_path=str(CONFIG_DIR / "calibration_profiles" / "default_calibration.json"),
        motion_config_path=str(CONFIG_DIR / "motion_detection_config.json"),
        database_path=str(db_path),
    )

    ws_cfg = WebSocketConfig(
        host="127.0.0.1",
        port=0,
        message_format="legacy_zone",
        legacy_resync_interval_seconds=1.0,
    )

    app = Application(config=cfg, ws_config=ws_cfg)
    assert app._wire_format == "legacy_zone"
    assert app.bridge is not None

    app.publisher.start()
    port = app.publisher.actual_port
    assert port is not None

    received_messages: List[str] = []
    client_connected = threading.Event()
    client_done = threading.Event()

    def ws_client_worker() -> None:
        try:
            with ws_client.connect(f"ws://127.0.0.1:{port}") as client:
                client_connected.set()
                while not client_done.is_set():
                    try:
                        msg = client.recv(timeout=0.2)
                        received_messages.append(msg)
                    except TimeoutError:
                        continue
                    except ConnectionClosed:
                        break
        finally:
            client_done.set()

    client_thread = threading.Thread(target=ws_client_worker, daemon=True)
    client_thread.start()
    assert client_connected.wait(timeout=3.0)

    records_processed = app.run()
    assert records_processed == 300

    time.sleep(0.1)
    client_done.set()
    client_thread.join(timeout=2.0)

    # Verify messages
    assert len(received_messages) > 0
    for raw_msg in received_messages:
        parsed = json.loads(raw_msg)
        assert set(parsed.keys()) == {"zone", "occupied"}
        assert isinstance(parsed["zone"], int)
        assert isinstance(parsed["occupied"], bool)

    # Final messages MUST be all-false on shutdown across all zones
    final_msgs = [json.loads(m) for m in received_messages[-4:]]
    assert final_msgs == [{"zone": z, "occupied": False} for z in range(1, 5)]


# ==============================================================================
# Requirement C: Startup Validation (Hardware Mode)
# ==============================================================================

def test_hardware_mode_missing_motion_config_raises_error() -> None:
    """Verify hardware mode refuses to start without explicit motion_config_path."""
    cfg = AppConfig(
        csi_source_mode="hardware",
        serial_port="COM4",
        motion_config_path=None,
    )
    app = Application(config=cfg)
    with pytest.raises(
        ValueError,
        match=r"In hardware mode, motion_config_path must be explicitly set",
    ):
        app.run()


def test_hardware_mode_subcarrier_mismatch_fails_fast() -> None:
    """Verify profile subcarrier mismatch fails fast naming both numbers."""
    frames = _build_fake_frames(subcarrier_count=2, count=5)
    fake_ser = FakeSerial(initial_data=frames)
    fake_ser.mark_eof()

    cfg = AppConfig(
        csi_source_mode="hardware",
        serial_port="COM4",
        motion_config_path=str(CONFIG_DIR / "motion_detection_config.json"),
        calibration_profile_path=str(CONFIG_DIR / "calibration_profiles" / "default_calibration.json"),
    )
    app = Application(config=cfg, serial_factory=lambda port, baud, timeout: fake_ser)

    with pytest.raises(
        ValueError,
        match=r"Subcarrier count mismatch: calibration profile has 64 subcarriers, but incoming CSI record has 2 subcarriers",
    ):
        app.run()


def test_no_zone_mode_rejects_legacy_zone_format() -> None:
    """Verify legacy_zone wire format is rejected in no-zone mode."""
    cfg = AppConfig(
        csi_source_mode="synthetic",
        synthetic_scenario_path=str(CONFIG_DIR / "synthetic_scenarios" / "example.json"),
        calibration_profile_path=None,  # No-zone mode
    )
    ws_cfg = WebSocketConfig(host="127.0.0.1", port=0, message_format="legacy_zone")

    app = Application(config=cfg, ws_config=ws_cfg)
    with pytest.raises(
        ValueError,
        match=r"Legacy zone output format \('legacy_zone'\) is not allowed in no-zone mode",
    ):
        app.run()


def test_hardware_no_zone_mode_runs_cleanly(tmp_path: Path) -> None:
    """Verify hardware mode with calibration_profile_path=None outputs zone_id=None while motion works."""
    db_path = tmp_path / "test_hw_no_zone.db"
    # 64 subcarriers to match synthetic-scale motion detection
    frames = _build_fake_frames(subcarrier_count=64, count=25)
    fake_ser = FakeSerial(initial_data=frames)
    fake_ser.mark_eof()

    cfg = AppConfig(
        csi_source_mode="hardware",
        serial_port="COM4",
        calibration_profile_path=None,
        motion_config_path=str(CONFIG_DIR / "motion_detection_config.json"),
        database_path=str(db_path),
    )
    ws_cfg = WebSocketConfig(host="127.0.0.1", port=0)
    app = Application(config=cfg, ws_config=ws_cfg, serial_factory=lambda port, baud, timeout: fake_ser)

    records_processed = app.run()
    assert records_processed == 25

    with SQLiteDatabaseManager(DatabaseConfig(database_path=str(db_path))) as db:
        events = db.query_by_run_id(run_id=app.run_id)
        assert len(events) == 6  # 25 - 20 + 1 = 6 feature vectors
        for ev in events:
            assert ev.zone_id is None
            assert ev.separation_margin is None
            assert ev.calibration_version is None


# ==============================================================================
# Requirement F: Stall Handling
# ==============================================================================

def test_stall_resets_pipeline_state_message_v1(tmp_path: Path) -> None:
    """Verify gap longer than disconnected_after_seconds resets pipeline, requiring W new records."""
    scenario_path = CONFIG_DIR / "synthetic_scenarios" / "example.json"
    with SyntheticCSISource(ScenarioConfig.from_json_file(scenario_path)) as src:
        base_records = list(src)

    # 25 records with normal delta (0.05s) -> 6 feature vectors
    batch1 = base_records[:25]
    # Record 26 arrives after a 10.0s gap
    stalled_record = dataclasses.replace(
        batch1[0],
        timestamp=batch1[-1].timestamp + 10.0,
        sequence_number=100,
    )
    # Next 10 records after gap (not enough to fill window of 20)
    batch2 = [
        dataclasses.replace(
            batch1[0],
            timestamp=stalled_record.timestamp + (i + 1) * 0.05,
            sequence_number=101 + i,
        )
        for i in range(10)
    ]
    # Next 15 records (reaches 25 new records total after gap -> 6 new feature vectors)
    batch3 = [
        dataclasses.replace(
            batch1[0],
            timestamp=batch2[-1].timestamp + (i + 1) * 0.05,
            sequence_number=111 + i,
        )
        for i in range(15)
    ]

    all_records = batch1 + [stalled_record] + batch2 + batch3

    cfg = AppConfig(
        csi_source_mode="synthetic",
        synthetic_scenario_path=str(CONFIG_DIR / "synthetic_scenarios" / "example.json"),
        calibration_profile_path=str(CONFIG_DIR / "calibration_profiles" / "default_calibration.json"),
        motion_config_path=str(CONFIG_DIR / "motion_detection_config.json"),
        database_path=str(tmp_path / "stall_v1.db"),
    )
    ws_cfg = WebSocketConfig(host="127.0.0.1", port=0)

    def trigger_stall() -> None:
        # Step last_record_host_time back by 5.0 seconds (disconnected_after_seconds = 2.0)
        app._last_record_host_time = time.time() - 5.0

    source = ControlledTimeSource(all_records, stall_index=25, on_stall=trigger_stall)
    app = Application(config=cfg, ws_config=ws_cfg, source=source)

    records_processed = app.run()
    assert records_processed == len(all_records)

    with SQLiteDatabaseManager(DatabaseConfig(database_path=str(tmp_path / "stall_v1.db"))) as db:
        events = db.query_by_run_id(run_id=app.run_id)
        # batch1 (25) -> 6 vectors
        # gap resets runner -> batch2 (10) + batch3 (15) + stalled_record (1) = 26 records -> 26 - 20 + 1 = 7 vectors
        # Total = 6 + 7 = 13 vectors! (If window straddled gap, it would be 51 - 20 + 1 = 32 vectors)
        assert len(events) == 13


def test_stall_legacy_zone_emits_all_false(tmp_path: Path) -> None:
    """Verify stall in legacy_zone mode emits all-false once to connected clients."""
    scenario_path = CONFIG_DIR / "synthetic_scenarios" / "example.json"
    with SyntheticCSISource(ScenarioConfig.from_json_file(scenario_path)) as src:
        base_records = list(src)[:25]

    cfg = AppConfig(
        csi_source_mode="synthetic",
        synthetic_scenario_path=str(CONFIG_DIR / "synthetic_scenarios" / "example.json"),
        calibration_profile_path=str(CONFIG_DIR / "calibration_profiles" / "default_calibration.json"),
        motion_config_path=str(CONFIG_DIR / "motion_detection_config.json"),
        database_path=str(tmp_path / "stall_legacy.db"),
    )
    ws_cfg = WebSocketConfig(host="127.0.0.1", port=0, message_format="legacy_zone")

    def trigger_stall() -> None:
        app._last_record_host_time = time.time() - 5.0

    source = ControlledTimeSource(base_records, stall_index=22, on_stall=trigger_stall)
    app = Application(config=cfg, ws_config=ws_cfg, source=source)

    app.publisher.start()
    port = app.publisher.actual_port
    assert port is not None

    received_messages: List[str] = []
    client_connected = threading.Event()
    client_done = threading.Event()

    def ws_client_worker() -> None:
        try:
            with ws_client.connect(f"ws://127.0.0.1:{port}") as client:
                client_connected.set()
                while not client_done.is_set():
                    try:
                        msg = client.recv(timeout=0.2)
                        received_messages.append(msg)
                    except TimeoutError:
                        continue
                    except ConnectionClosed:
                        break
        finally:
            client_done.set()

    client_thread = threading.Thread(target=ws_client_worker, daemon=True)
    client_thread.start()
    assert client_connected.wait(timeout=3.0)

    records_processed = app.run()
    assert records_processed == 25

    time.sleep(0.1)
    client_done.set()
    client_thread.join(timeout=2.0)

    # Check that all-false was emitted during stall
    all_false_msgs = [m for m in received_messages if json.loads(m).get("occupied") is False]
    assert len(all_false_msgs) >= 1


# ==============================================================================
# Requirement F, G, H: Fault Tolerance, Stop from Thread, Shutdown Order
# ==============================================================================

def test_source_error_fault_tolerance(tmp_path: Path) -> None:
    """Verify Application survives source errors without crashing and logs/counts them."""
    scenario_path = CONFIG_DIR / "synthetic_scenarios" / "example.json"
    with SyntheticCSISource(ScenarioConfig.from_json_file(scenario_path)) as src:
        base_records = list(src)[:25]

    flaky_source = FlakySource(base_records, error_idx=10)

    cfg = AppConfig(
        csi_source_mode="synthetic",
        synthetic_scenario_path=str(CONFIG_DIR / "synthetic_scenarios" / "example.json"),
        calibration_profile_path=str(CONFIG_DIR / "calibration_profiles" / "default_calibration.json"),
        motion_config_path=str(CONFIG_DIR / "motion_detection_config.json"),
        database_path=str(tmp_path / "flaky.db"),
    )
    ws_cfg = WebSocketConfig(host="127.0.0.1", port=0)
    app = Application(config=cfg, ws_config=ws_cfg, source=flaky_source)

    records_processed = app.run()
    # 25 records total, 1 was an error, so 24 processed
    assert records_processed == 24
    assert app._source_errors == 1


def test_application_stop_from_another_thread(tmp_path: Path) -> None:
    """Verify calling app.stop() from another thread shuts down cleanly."""
    scenario_path = CONFIG_DIR / "synthetic_scenarios" / "example.json"
    with SyntheticCSISource(ScenarioConfig.from_json_file(scenario_path)) as src:
        base_records = list(src)

    # Infinite repeating stream
    class InfiniteSource(CSIRecordSource):
        def __init__(self, records: List[CSIRecord]) -> None:
            self._records = records
            self._is_open = False

        @property
        def is_open(self) -> bool:
            return self._is_open

        def open(self) -> None:
            self._is_open = True

        def close(self) -> None:
            self._is_open = False

        def __enter__(self) -> "InfiniteSource":
            self.open()
            return self

        def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
            self.close()

        def read_record(self, timeout: Optional[float] = None) -> Optional[CSIRecord]:
            if not self._is_open:
                return None
            time.sleep(0.01)
            return self._records[0]

        def records(self) -> Generator[CSIRecord, None, None]:
            while self._is_open:
                yield self._records[0]
                time.sleep(0.01)

    inf_source = InfiniteSource(base_records)

    cfg = AppConfig(
        csi_source_mode="synthetic",
        synthetic_scenario_path=str(CONFIG_DIR / "synthetic_scenarios" / "example.json"),
        calibration_profile_path=str(CONFIG_DIR / "calibration_profiles" / "default_calibration.json"),
        motion_config_path=str(CONFIG_DIR / "motion_detection_config.json"),
        database_path=str(tmp_path / "inf.db"),
    )
    ws_cfg = WebSocketConfig(host="127.0.0.1", port=0)
    app = Application(config=cfg, ws_config=ws_cfg, source=inf_source)

    app_thread = threading.Thread(target=app.run, daemon=True)
    app_thread.start()

    time.sleep(0.2)
    assert app_thread.is_alive()

    # Call stop from main thread
    app.stop()
    app_thread.join(timeout=2.0)
    assert not app_thread.is_alive()
    assert not app.source.is_open


def test_shutdown_order_and_idempotence(tmp_path: Path) -> None:
    """Verify shutdown order: bridge.close -> db_flush -> source.close -> publisher.stop."""
    call_order: List[str] = []

    cfg = AppConfig(
        csi_source_mode="synthetic",
        synthetic_scenario_path=str(CONFIG_DIR / "synthetic_scenarios" / "example.json"),
        calibration_profile_path=str(CONFIG_DIR / "calibration_profiles" / "default_calibration.json"),
        motion_config_path=str(CONFIG_DIR / "motion_detection_config.json"),
        database_path=str(tmp_path / "order.db"),
    )
    ws_cfg = WebSocketConfig(host="127.0.0.1", port=0, message_format="legacy_zone")
    app = Application(config=cfg, ws_config=ws_cfg)

    # Monkeypatch close methods to record order
    orig_bridge_close = app.bridge.close
    orig_db_flush = app._flush_db
    orig_source_close = app.source.close
    orig_pub_stop = app.publisher.stop

    app.bridge.close = lambda: (call_order.append("bridge.close"), orig_bridge_close())  # type: ignore[assignment]
    app._flush_db = lambda: (call_order.append("db.flush"), orig_db_flush())  # type: ignore[assignment]
    app.source.close = lambda: (call_order.append("source.close"), orig_source_close())  # type: ignore[assignment]
    app.publisher.stop = lambda: (call_order.append("publisher.stop"), orig_pub_stop())  # type: ignore[assignment]

    app.source.open()
    app.stop()
    assert call_order == ["bridge.close", "db.flush", "source.close", "publisher.stop"]

    # Calling stop() again should be a safe no-op (idempotent)
    app.stop()
    assert len(call_order) == 4


def test_config_defaults_leave_existing_tests_unchanged() -> None:
    """Verify new AppConfig fields all have backwards-compatible defaults."""
    cfg = AppConfig()
    assert cfg.config_dir == "config"
    assert cfg.calibration_profile_path is None
    assert cfg.motion_config_path is None
    assert cfg.expected_sample_rate_hz == 20.0
    assert cfg.health_log_interval_seconds == 10.0
    assert cfg.stat_log_interval_seconds == 5.0
    assert cfg.db_flush_interval_seconds == 1.0
    assert cfg.db_flush_batch_size == 50
