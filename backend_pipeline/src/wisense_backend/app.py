"""Application coordinator for the WiSense-Twin backend pipeline.

Wires together configuration loading, structured logging, CSI ingestion drivers,
signal processing pipeline execution, WebSocket publishing (both canonical StateMessage v1
and legacy zone protocol), SQLite persistence, system health monitoring, and graceful shutdown.
"""

from __future__ import annotations

import json
import logging
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from wisense_backend.calibration.calibrator import ZoneCalibrator
from wisense_backend.classification.nearest_signature_classifier import (
    NearestSignatureZoneClassifier,
    ZoneClassifierConfig,
)
from wisense_backend.communication.legacy_zone_adapter import (
    LegacyZoneAdapter,
    LegacyZoneBridge,
)
from wisense_backend.communication.websocket_server import (
    WebSocketConfig,
    WebSocketStatePublisher,
)
from wisense_backend.config.app_config import AppConfig
from wisense_backend.contracts.calibration import CalibrationProfile
from wisense_backend.contracts.csi_source import CSIRecord, CSIRecordSource
from wisense_backend.contracts.database import EventLogRecord
from wisense_backend.contracts.health_status import SystemHealth
from wisense_backend.detection.threshold_motion_detector import (
    ThresholdMotionConfig,
    ThresholdMotionDetector,
)
from wisense_backend.features.variance_extractor import (
    VarianceExtractorConfig,
    VarianceFeatureExtractor,
)
from wisense_backend.ingestion.serial_source import SerialCSISource, SerialSourceConfig
from wisense_backend.ingestion.synthetic_source import ScenarioConfig, SyntheticCSISource
from wisense_backend.logging_setup import configure_logging
from wisense_backend.monitoring.health_monitor import HealthMonitorConfig, SystemHealthMonitor
from wisense_backend.persistence.sqlite_manager import DatabaseConfig, SQLiteDatabaseManager
from wisense_backend.pipeline.pipeline_runner import PipelineResult, PipelineRunner
from wisense_backend.preprocessing.hampel_filter import HampelConfig, HampelFilter
from wisense_backend.preprocessing.moving_average import (
    MovingAverageConfig,
    MovingAverageFilter,
)
from wisense_backend.preprocessing.pipeline import PreprocessingPipeline


class Application:
    """Core backend application coordinator.

    Responsible for bootstrapping runtime configuration, initializing structured logging,
    instantiating the configured CSI frame source, assembling the signal processing pipeline,
    managing WebSocket broadcasting, persisting telemetry events to SQLite, monitoring system
    health, and managing graceful shutdown.
    """

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        config_path: Optional[str | Path] = None,
        source: Optional[CSIRecordSource] = None,
        runner: Optional[PipelineRunner] = None,
        publisher: Optional[WebSocketStatePublisher] = None,
        db_manager: Optional[SQLiteDatabaseManager] = None,
        serial_factory: Optional[Callable[..., Any]] = None,
        ws_config: Optional[WebSocketConfig] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """Initialize the Application instance.

        Args:
            config: Optional pre-loaded AppConfig. If None, config_path will be used.
            config_path: Optional path to system_config.json. If None and config is None,
                default search locations will be resolved.
            source: Optional pre-instantiated CSIRecordSource.
            runner: Optional pre-assembled PipelineRunner.
            publisher: Optional pre-instantiated WebSocketStatePublisher.
            db_manager: Optional pre-instantiated SQLiteDatabaseManager.
            serial_factory: Optional factory callable for creating serial handles (FakeSerial).
            ws_config: Optional explicit WebSocketConfig.
            clock: Optional clock callable returning epoch seconds (default time.time).

        Raises:
            FileNotFoundError: If configuration or calibration files cannot be resolved.
            ValueError: If configuration values fail invariant validation.
        """
        if config is not None:
            self.config = config
        else:
            resolved_config_path = self._resolve_config_path(config_path)
            self.config = AppConfig.from_json_file(resolved_config_path)

        # Initialize structured logging
        configure_logging(self.config)
        self.logger = logging.getLogger("wisense_backend.app")

        self._clock: Callable[[], float] = clock if clock is not None else time.time

        # Generate unique run_id session identifier (<mode>-<UTC yyyymmddThhmmssZ>-<8 hex>)
        utc_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        hex_suffix = secrets.token_hex(4)
        self.run_id: str = f"{self.config.csi_source_mode}-{utc_ts}-{hex_suffix}"
        self.logger.info("Application initialized with run_id: %s", self.run_id, extra={"run_id": self.run_id})

        self._stop_requested = threading.Event()
        self._shutdown_lock = threading.RLock()
        self._is_shutdown = False

        # Diagnostic counters
        self._record_count: int = 0
        self._published_message_count: int = 0
        self._publisher_errors: int = 0
        self._source_errors: int = 0
        self._dropped_db_rows: int = 0

        self._last_record_timestamp: Optional[float] = None
        self._last_record_host_time: Optional[float] = None
        self._last_err_log_time: float = 0.0

        # Timing tracking for periodic logging and flushes
        self._last_health_log_time: float = 0.0
        self._last_stat_log_time: float = 0.0
        self._last_db_flush_time: float = 0.0
        self._interval_variances: list[float] = []

        # Database batch buffer
        self._db_buffer: list[EventLogRecord] = []

        resolved_config_dir = self._resolve_dir_path(self.config.config_dir)

        # Instantiate CSI source
        self._serial_factory = serial_factory
        self.source: CSIRecordSource = source or self._create_source()

        # Calibration Profile (optional, None means no-zone mode)
        self.profile: Optional[CalibrationProfile] = self._load_profile(resolved_config_dir)

        # PipelineRunner
        if runner is not None:
            self.runner: Optional[PipelineRunner] = runner
        else:
            # If in hardware mode and motion_config_path is not set, defer runner construction
            # until run(), where fail-fast validation will raise with clear message.
            if self.config.csi_source_mode == "hardware" and not self.config.motion_config_path:
                self.runner = None
            else:
                self.runner = self._build_runner(resolved_config_dir)

        # WebSocket publisher & LegacyZoneBridge
        self.ws_config: WebSocketConfig = self._build_ws_config(resolved_config_dir, ws_config)
        self._wire_format: str = self.ws_config.message_format
        self.publisher: Optional[WebSocketStatePublisher] = publisher or WebSocketStatePublisher(self.ws_config)

        if self._wire_format == "legacy_zone":
            if self.profile is not None:
                adapter = LegacyZoneAdapter(zone_count=self.profile.zone_count, clock=self._clock)
                self.bridge: Optional[LegacyZoneBridge] = LegacyZoneBridge(
                    adapter=adapter,
                    publisher=self.publisher,
                    resync_interval_seconds=self.ws_config.legacy_resync_interval_seconds,
                    clock=self._clock,
                )
            else:
                self.bridge = None
        else:
            self.bridge = None

        # Database Manager
        self.db_manager: Optional[SQLiteDatabaseManager] = db_manager or SQLiteDatabaseManager(
            DatabaseConfig(database_path=self.config.database_path)
        )

        # System Health Monitor
        health_cfg_path = resolved_config_dir / "health_monitor_config.json"
        health_cfg = (
            HealthMonitorConfig.from_json_file(health_cfg_path)
            if health_cfg_path.is_file()
            else HealthMonitorConfig()
        )
        self.health_monitor: SystemHealthMonitor = SystemHealthMonitor(health_cfg)
        self.health_monitor.register_csi_source_probe(lambda: self._last_record_timestamp)
        self.health_monitor.register_database_probe(
            lambda: "connected" if (self.db_manager and self.db_manager.is_connected) else "disconnected"
        )
        self.health_monitor.register_websocket_probe(
            lambda: self.publisher.client_count if self.publisher else 0
        )

    def _resolve_config_path(self, path: Optional[str | Path]) -> Path:
        """Resolve config file path against common project directories."""
        if path is not None:
            p = Path(path)
            if p.is_file():
                return p.resolve()
            backend_dir = Path(__file__).resolve().parent.parent.parent
            if (backend_dir / p).is_file():
                return (backend_dir / p).resolve()
            repo_root = backend_dir.parent
            if (repo_root / p).is_file():
                return (repo_root / p).resolve()
            return p.resolve()

        candidates = [
            Path("config/system_config.json"),
            Path("backend_pipeline/config/system_config.json"),
            Path(__file__).resolve().parent.parent.parent / "config" / "system_config.json",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()

        raise FileNotFoundError("Default configuration file 'config/system_config.json' not found.")

    def _resolve_dir_path(self, dir_path: str | Path) -> Path:
        """Resolve a directory path against workspace locations."""
        p = Path(dir_path)
        if p.is_dir():
            return p.resolve()
        backend_dir = Path(__file__).resolve().parent.parent.parent
        if (backend_dir / p).is_dir():
            return (backend_dir / p).resolve()
        repo_root = backend_dir.parent
        if (repo_root / p).is_dir():
            return (repo_root / p).resolve()
        return p.resolve()

    def _resolve_file_path(self, file_path: str | Path) -> Path:
        """Resolve a file path against workspace locations."""
        p = Path(file_path)
        if p.is_file():
            return p.resolve()
        backend_dir = Path(__file__).resolve().parent.parent.parent
        if (backend_dir / p).is_file():
            return (backend_dir / p).resolve()
        repo_root = backend_dir.parent
        if (repo_root / p).is_file():
            return (repo_root / p).resolve()
        return p.resolve()

    def _load_profile(self, config_dir: Path) -> Optional[CalibrationProfile]:
        """Load calibration profile if configured."""
        if not self.config.calibration_profile_path:
            return None

        resolved_path = self._resolve_file_path(self.config.calibration_profile_path)
        if not resolved_path.is_file():
            raise FileNotFoundError(f"Calibration profile not found: {self.config.calibration_profile_path}")

        profile = CalibrationProfile.from_json_file(resolved_path)
        self.logger.info(
            "Loaded calibration profile v%d (%d zones, %d subcarriers) from %s",
            profile.calibration_version,
            profile.zone_count,
            profile.subcarrier_count,
            resolved_path,
        )
        return profile

    def _build_runner(self, config_dir: Path) -> PipelineRunner:
        """Build the PipelineRunner stages from config files."""
        # 1. Preprocessing stage
        prep_path = config_dir / "preprocessing_config.json"
        if prep_path.is_file():
            preprocessor = PreprocessingPipeline.from_json_file(prep_path)
        else:
            preprocessor = PreprocessingPipeline([
                HampelFilter(HampelConfig(window_size=7, threshold_multiplier=3.0)),
                MovingAverageFilter(MovingAverageConfig(window_size=5)),
            ])

        # 2. Feature extraction stage
        feat_path = config_dir / "feature_extraction_config.json"
        if feat_path.is_file():
            with open(feat_path, "r", encoding="utf-8") as f:
                feat_dict = json.load(f)
            w_size = int(feat_dict.get("window_size_samples", 20))
            w_step = int(feat_dict.get("window_step_samples", 1))
            feat_extractor = VarianceFeatureExtractor(
                VarianceExtractorConfig(window_size_samples=w_size, window_step_samples=w_step)
            )
        else:
            feat_extractor = VarianceFeatureExtractor(
                VarianceExtractorConfig(window_size_samples=20, window_step_samples=1)
            )

        # 3. Motion detection stage
        if self.config.motion_config_path:
            resolved_motion_path = self._resolve_file_path(self.config.motion_config_path)
            motion_cfg = ThresholdMotionConfig.from_json_file(resolved_motion_path)
        else:
            motion_path = config_dir / "motion_detection_config.json"
            if motion_path.is_file():
                motion_cfg = ThresholdMotionConfig.from_json_file(motion_path)
            else:
                motion_cfg = ThresholdMotionConfig(
                    enter_threshold=0.10,
                    exit_threshold=0.05,
                    presence_timeout_seconds=3.0,
                )
        motion_detector = ThresholdMotionDetector(motion_cfg)

        # 4. Zone classification stage (optional; None gives no-zone mode)
        if self.profile is not None:
            clf_path = config_dir / "zone_classification_config.json"
            clf_cfg = (
                ZoneClassifierConfig.from_json_file(clf_path)
                if clf_path.is_file()
                else ZoneClassifierConfig()
            )
            classifier: Optional[NearestSignatureZoneClassifier] = NearestSignatureZoneClassifier(
                profile=self.profile,
                config=clf_cfg,
            )
        else:
            classifier = None

        return PipelineRunner(
            preprocessor=preprocessor,
            feature_extractor=feat_extractor,
            motion_detector=motion_detector,
            classifier=classifier,
        )

    def _build_ws_config(
        self,
        config_dir: Path,
        override_ws_config: Optional[WebSocketConfig],
    ) -> WebSocketConfig:
        """Build WebSocketConfig, reusing websocket_config.json and respecting caller overrides."""
        if override_ws_config is not None:
            return override_ws_config

        ws_file = config_dir / "websocket_config.json"
        base_cfg: Optional[WebSocketConfig] = None
        if ws_file.is_file():
            try:
                base_cfg = WebSocketConfig.from_json_file(ws_file)
            except Exception as e:
                self.logger.warning("Could not parse websocket_config.json (%s), using defaults", e)

        if base_cfg is None:
            base_cfg = WebSocketConfig(
                host=self.config.websocket_host,
                port=self.config.websocket_port,
            )

        # If AppConfig specifies non-default port or host, let them take precedence
        target_port = (
            self.config.websocket_port
            if self.config.websocket_port != 8765
            else base_cfg.port
        )
        target_host = (
            self.config.websocket_host
            if self.config.websocket_host != "127.0.0.1"
            else base_cfg.host
        )

        return WebSocketConfig(
            host=target_host,
            port=target_port,
            message_format=base_cfg.message_format,
            legacy_resync_interval_seconds=base_cfg.legacy_resync_interval_seconds,
        )

    def _create_source(self) -> CSIRecordSource:
        """Instantiate the appropriate CSIRecordSource based on configuration."""
        if self.config.csi_source_mode == "synthetic":
            assert self.config.synthetic_scenario_path is not None
            scenario_path = self._resolve_file_path(self.config.synthetic_scenario_path)
            scenario_cfg = ScenarioConfig.from_json_file(scenario_path)
            return SyntheticCSISource(scenario_cfg)

        if self.config.csi_source_mode == "hardware":
            if not self.config.serial_port:
                raise ValueError("serial_port must be configured when csi_source_mode is 'hardware'")
            baud_rate = self.config.serial_baud_rate if self.config.serial_baud_rate is not None else 921600
            serial_cfg = SerialSourceConfig(
                port=self.config.serial_port,
                baud_rate=baud_rate,
            )
            return SerialCSISource(serial_cfg, serial_factory=self._serial_factory)

        raise ValueError(f"Unsupported csi_source_mode: {self.config.csi_source_mode}")

    def get_health(self) -> SystemHealth:
        """Return the current SystemHealth telemetry snapshot."""
        return self.health_monitor.get_current_health(self._clock())

    def _flush_db(self) -> None:
        """Flush buffered EventLogRecords to SQLite persistence."""
        if not self._db_buffer or self.db_manager is None:
            self._last_db_flush_time = self._clock()
            return

        batch_count = len(self._db_buffer)
        try:
            self.db_manager.insert_events_batch(self._db_buffer)
        except Exception as exc:
            self._dropped_db_rows += batch_count
            self.logger.error("Failed to insert %d event log records into database: %s", batch_count, exc)
        finally:
            self._db_buffer.clear()
            self._last_db_flush_time = self._clock()

    def _log_periodic_stats(self) -> None:
        """Log periodic statistics line over the elapsed interval."""
        if self._interval_variances:
            min_v = min(self._interval_variances)
            mean_v = sum(self._interval_variances) / len(self._interval_variances)
            max_v = max(self._interval_variances)
        else:
            min_v = mean_v = max_v = 0.0

        self.logger.info(
            "Pipeline stats: variance [min=%.6f, mean=%.6f, max=%.6f], records=%d, "
            "messages=%d, source_errors=%d, dropped_db_rows=%d",
            min_v,
            mean_v,
            max_v,
            self._record_count,
            self._published_message_count,
            self._source_errors,
            self._dropped_db_rows,
            extra={
                "variance_min": min_v,
                "variance_mean": mean_v,
                "variance_max": max_v,
                "records": self._record_count,
                "messages": self._published_message_count,
                "source_errors": self._source_errors,
                "dropped_db_rows": self._dropped_db_rows,
            },
        )
        self._interval_variances.clear()
        self._last_stat_log_time = self._clock()

    def stop(self) -> None:
        """Signal the application to halt the ingestion loop and close resources.

        Shutdown order:
        Stop signal -> stop reading -> bridge.close() (legacy) -> flush database ->
        close the source -> bounded flush -> publisher.stop().
        Idempotent and safe if called from another thread.
        """
        self._stop_requested.set()
        with self._shutdown_lock:
            if self._is_shutdown:
                return
            self._is_shutdown = True
            self.logger.info("Application shutdown initiated (run_id: %s)", self.run_id)

            # 1. bridge.close() (legacy)
            if self.bridge is not None:
                try:
                    self.bridge.close()
                except Exception as exc:
                    self.logger.warning("Error closing legacy bridge: %s", exc)

            # 2. flush database & close db
            if self.db_manager is not None:
                try:
                    self._flush_db()
                except Exception as exc:
                    self.logger.warning("Error flushing database during shutdown: %s", exc)
                try:
                    self.db_manager.close()
                except Exception as exc:
                    self.logger.warning("Error closing database manager: %s", exc)

            # 3. close the source
            if self.source is not None and getattr(self.source, "is_open", False):
                try:
                    self.source.close()
                except Exception as exc:
                    self.logger.warning("Error closing CSI source: %s", exc)

            # 4. bounded flush (allow in-flight TCP frames to leave network buffer)
            time.sleep(0.05)

            # 5. publisher.stop()
            if self.publisher is not None:
                try:
                    self.publisher.stop()
                except Exception as exc:
                    self.logger.warning("Error stopping WebSocket publisher: %s", exc)

            self.logger.info(
                "Application shutdown complete (run_id: %s, records: %d, messages: %d)",
                self.run_id,
                self._record_count,
                self._published_message_count,
            )

    def run(self) -> int:
        """Run the end-to-end pipeline ingestion loop until stream completion or stop signal.

        Returns:
            Total number of CSI records read and processed.
        """
        # Fail-fast startup validations
        if self.config.csi_source_mode == "hardware" and not self.config.motion_config_path:
            raise ValueError(
                "In hardware mode, motion_config_path must be explicitly set. "
                "Never silently use synthetic-scale default thresholds."
            )

        if self._wire_format == "legacy_zone" and self.profile is None:
            raise ValueError(
                "Legacy zone output format ('legacy_zone') is not allowed in no-zone mode. "
                "A calibration profile with zone_count is required."
            )

        if self.runner is None:
            resolved_dir = self._resolve_dir_path(self.config.config_dir)
            self.runner = self._build_runner(resolved_dir)

        self._stop_requested.clear()
        self._is_shutdown = False
        self._record_count = 0
        self._published_message_count = 0
        self._publisher_errors = 0
        self._source_errors = 0
        self._dropped_db_rows = 0

        self.logger.info(
            "Application starting up (mode: %s, run_id: %s, wire_format: %s)",
            self.config.csi_source_mode,
            self.run_id,
            self._wire_format,
            extra={
                "csi_source_mode": self.config.csi_source_mode,
                "run_id": self.run_id,
                "wire_format": self._wire_format,
            },
        )

        # 1. Start WebSocket publisher if not already running
        if self.publisher is not None and not self.publisher.is_running:
            self.publisher.start()

        # 2. Connect Database
        if self.db_manager is not None and not self.db_manager.is_connected:
            self.db_manager.connect()

        # 3. Determine sample rate & realtime mode
        if self.config.csi_source_mode == "synthetic":
            if isinstance(self.source, SyntheticCSISource):
                sample_rate = float(self.source.config.sample_rate_hz)
                is_non_realtime = not bool(getattr(self.source, "_realtime", self.source.config.realtime))
            else:
                sample_rate = float(getattr(getattr(self.source, "config", None), "sample_rate_hz", self.config.expected_sample_rate_hz))
                is_non_realtime = not bool(getattr(self.source, "_realtime", False))
        else:
            sample_rate = float(self.config.expected_sample_rate_hz)
            is_non_realtime = False

        window_size = getattr(
            getattr(self.runner.feature_extractor, "config", None),
            "window_size_samples",
            20,
        )
        window_latency_ms = (window_size / sample_rate) * 1000.0

        disconnected_threshold = self.health_monitor.config.disconnected_after_seconds
        self._last_record_host_time = None
        is_stalled = False

        self._last_health_log_time = self._clock()
        self._last_stat_log_time = self._clock()
        self._last_db_flush_time = self._clock()

        try:
            with self.source:
                self.logger.info("CSI record source opened successfully")
                while not self._stop_requested.is_set():
                    now = self._clock()

                    # Check stall on gap
                    if self._last_record_host_time is not None and not is_stalled:
                        if (now - self._last_record_host_time) >= disconnected_threshold:
                            self.runner.reset()
                            if self.bridge is not None:
                                self.bridge.emit_all_false()
                            is_stalled = True
                            self.logger.warning(
                                "CSI stream stall detected (no records for %.2fs). Pipeline reset.",
                                now - self._last_record_host_time,
                            )

                    # Periodic health log
                    if (now - self._last_health_log_time) >= self.config.health_log_interval_seconds:
                        health = self.get_health()
                        self.logger.info(
                            "Health snapshot: overall=%s, csi=%s, db=%s, clients=%d",
                            health.overall_status,
                            health.csi_source_status,
                            health.database_status,
                            health.websocket_client_count,
                            extra={"health": health.to_dict()},
                        )
                        self._last_health_log_time = now

                    # Periodic stats log
                    if (now - self._last_stat_log_time) >= self.config.stat_log_interval_seconds:
                        self._log_periodic_stats()

                    # Periodic DB flush
                    if (now - self._last_db_flush_time) >= self.config.db_flush_interval_seconds:
                        self._flush_db()

                    # Read next record from source
                    if not getattr(self.source, "is_open", False):
                        break

                    try:
                        read_timeout = 0.05 if not is_non_realtime else 0.0
                        record = self.source.read_record(timeout=read_timeout)
                    except Exception as exc:
                        self._source_errors += 1
                        now_err = self._clock()
                        if (now_err - self._last_err_log_time) >= 1.0:
                            self.logger.warning("Source error encountered: %s", exc)
                            self._last_err_log_time = now_err
                        continue

                    if record is None:
                        if not getattr(self.source, "is_open", False):
                            break
                        if isinstance(self.source, SerialCSISource):
                            if hasattr(self.source, "_serial") and getattr(self.source._serial, "eof", False):
                                break
                            continue
                        break

                    # Record received
                    now = self._clock()
                    if self._last_record_host_time is not None and not is_stalled:
                        if (now - self._last_record_host_time) >= disconnected_threshold:
                            self.runner.reset()
                            if self.bridge is not None:
                                self.bridge.emit_all_false()
                            is_stalled = True

                    is_stalled = False
                    self._last_record_host_time = now
                    self._last_record_timestamp = record.timestamp
                    self._record_count += 1

                    # First record validation against calibration profile
                    if self._record_count == 1 and self.profile is not None:
                        if self.profile.subcarrier_count != record.subcarrier_count:
                            raise ValueError(
                                f"Subcarrier count mismatch: calibration profile has {self.profile.subcarrier_count} subcarriers, "
                                f"but incoming CSI record has {record.subcarrier_count} subcarriers. "
                                f"A profile must be calibrated on this hardware."
                            )

                    # Pipeline processing
                    result = self.runner.process(record)
                    if result is None:
                        continue

                    self._interval_variances.append(result.feature.aggregate_variance)

                    # Publish state BEFORE database work (decoupling latency from disk)
                    try:
                        if self.bridge is not None:
                            self.bridge.publish_state(result.state_message)
                        elif self.publisher is not None:
                            self.publisher.publish(result.state_message)
                        self._published_message_count += 1
                    except Exception as exc:
                        self._publisher_errors += 1
                        self.logger.warning("Error publishing state: %s", exc)

                    # Telemetry persistence
                    pipe_lat = None if is_non_realtime else (self._clock() - record.timestamp) * 1000.0
                    event_rec = EventLogRecord(
                        run_id=self.run_id,
                        event_timestamp=result.state_message.timestamp,
                        window_start_timestamp=result.feature.window_start_timestamp,
                        window_end_timestamp=result.feature.window_end_timestamp,
                        sample_count=result.feature.sample_count,
                        is_motion=result.motion.is_motion,
                        presence=result.motion.presence,
                        zone_id=result.state_message.zone_id,
                        separation_margin=result.state_message.separation_margin,
                        calibration_version=result.state_message.calibration_version,
                        aggregate_variance=result.feature.aggregate_variance,
                        window_latency_ms=window_latency_ms,
                        pipeline_latency_ms=pipe_lat,
                    )
                    self._db_buffer.append(event_rec)
                    if len(self._db_buffer) >= self.config.db_flush_batch_size:
                        self._flush_db()

        except Exception as exc:
            self.logger.error("Exception in application execution: %s", exc, exc_info=True)
            raise
        finally:
            self._log_periodic_stats()
            self.stop()

        return self._record_count
