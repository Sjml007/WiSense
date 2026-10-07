"""Unit and integration tests for SystemHealth and SystemHealthMonitor."""

import json
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Optional

import pytest

from wisense_backend.communication.websocket_server import (
    WebSocketConfig,
    WebSocketStatePublisher,
)
from wisense_backend.contracts.health_status import (
    CsiSourceStatus,
    DatabaseStatus,
    HealthMonitor,
    OverallHealthStatus,
    SystemHealth,
)
from wisense_backend.ingestion.synthetic_source import ScenarioConfig, SyntheticCSISource
from wisense_backend.monitoring.health_monitor import (
    HealthMonitorConfig,
    SystemHealthMonitor,
)
from wisense_backend.persistence.sqlite_manager import SQLiteDatabaseManager


class TestSystemHealthContract:
    """Validate SystemHealth dataclass invariants, immutability, and serialization."""

    def test_valid_instantiation(self) -> None:
        """Verify normal creation of a healthy SystemHealth snapshot."""
        h = SystemHealth(
            timestamp=1700000000.0,
            csi_source_status=CsiSourceStatus.ACTIVE.value,
            csi_source_last_record_age_seconds=0.125,
            websocket_client_count=2,
            database_status=DatabaseStatus.CONNECTED.value,
            overall_status=OverallHealthStatus.HEALTHY.value,
        )
        assert h.timestamp == 1700000000.0
        assert h.csi_source_status == "active"
        assert h.csi_source_last_record_age_seconds == 0.125
        assert h.websocket_client_count == 2
        assert h.database_status == "connected"
        assert h.overall_status == "healthy"

    def test_immutability(self) -> None:
        """Verify SystemHealth is frozen and cannot be mutated."""
        h = SystemHealth(
            timestamp=1700000000.0,
            csi_source_status="active",
            csi_source_last_record_age_seconds=0.1,
            websocket_client_count=0,
            database_status="connected",
            overall_status="healthy",
        )
        with pytest.raises(FrozenInstanceError):
            h.websocket_client_count = 1  # type: ignore[misc]

    def test_validation_errors(self) -> None:
        """Verify invariant rejections for invalid attributes."""
        # Non-positive timestamp
        with pytest.raises(ValueError, match="timestamp must be positive"):
            SystemHealth(
                timestamp=0.0,
                csi_source_status="active",
                csi_source_last_record_age_seconds=None,
                websocket_client_count=0,
                database_status="connected",
                overall_status="healthy",
            )

        # Invalid CSI status outside vocabulary
        with pytest.raises(ValueError, match="Invalid csi_source_status"):
            SystemHealth(
                timestamp=1.0,
                csi_source_status="running",  # not in active, stale, disconnected
                csi_source_last_record_age_seconds=None,
                websocket_client_count=0,
                database_status="connected",
                overall_status="healthy",
            )

        # Negative record age
        with pytest.raises(ValueError, match="csi_source_last_record_age_seconds"):
            SystemHealth(
                timestamp=1.0,
                csi_source_status="active",
                csi_source_last_record_age_seconds=-0.5,
                websocket_client_count=0,
                database_status="connected",
                overall_status="healthy",
            )

        # Negative websocket client count
        with pytest.raises(ValueError, match="websocket_client_count must be non-negative"):
            SystemHealth(
                timestamp=1.0,
                csi_source_status="active",
                csi_source_last_record_age_seconds=None,
                websocket_client_count=-1,
                database_status="connected",
                overall_status="healthy",
            )

        # Boolean websocket client count
        with pytest.raises(TypeError, match="websocket_client_count must be an int"):
            SystemHealth(
                timestamp=1.0,
                csi_source_status="active",
                csi_source_last_record_age_seconds=None,
                websocket_client_count=True,  # type: ignore[arg-type]
                database_status="connected",
                overall_status="healthy",
            )

        # Invalid database status
        with pytest.raises(ValueError, match="Invalid database_status"):
            SystemHealth(
                timestamp=1.0,
                csi_source_status="active",
                csi_source_last_record_age_seconds=None,
                websocket_client_count=0,
                database_status="online",
                overall_status="healthy",
            )

        # Invalid overall status
        with pytest.raises(ValueError, match="Invalid overall_status"):
            SystemHealth(
                timestamp=1.0,
                csi_source_status="active",
                csi_source_last_record_age_seconds=None,
                websocket_client_count=0,
                database_status="connected",
                overall_status="ok",
            )

    def test_strict_naming_discipline_no_numeric_scores(self) -> None:
        """Verify no numeric health score or confidence fields exist in SystemHealth."""
        h = SystemHealth(
            timestamp=1700000000.0,
            csi_source_status="active",
            csi_source_last_record_age_seconds=0.1,
            websocket_client_count=1,
            database_status="connected",
            overall_status="healthy",
        )
        d = h.to_dict()
        json_str = h.to_json()

        for key in d.keys():
            assert "score" not in key.lower(), f"Forbidden word found in key: {key}"
            assert "confidence" not in key.lower(), f"Forbidden word found in key: {key}"

        assert "score" not in json_str.lower()
        assert "confidence" not in json_str.lower()

    def test_serialization_round_trip(self) -> None:
        """Verify dictionary and JSON round-tripping."""
        h = SystemHealth(
            timestamp=1700000005.1234,
            csi_source_status="stale",
            csi_source_last_record_age_seconds=3.4567,
            websocket_client_count=4,
            database_status="connected",
            overall_status="degraded",
        )
        d = h.to_dict()
        assert d["csi_source_last_record_age_seconds"] == 3.457  # rounded to 3 places
        restored = SystemHealth.from_dict(d)
        assert restored.timestamp == h.timestamp
        assert restored.csi_source_status == "stale"
        assert restored.csi_source_last_record_age_seconds == 3.457
        assert restored.websocket_client_count == 4
        assert restored.database_status == "connected"
        assert restored.overall_status == "degraded"

        json_str = h.to_json()
        from_json_h = SystemHealth.from_json(json_str)
        assert from_json_h == restored


class TestHealthMonitorConfig:
    """Validate configuration defaults, JSON file loading, and constraints."""

    def test_default_config(self) -> None:
        """Verify default configuration parameters."""
        cfg = HealthMonitorConfig()
        assert cfg.stale_after_seconds == 2.0
        assert cfg.disconnected_after_seconds == 5.0

    def test_from_dict_and_to_dict(self) -> None:
        """Verify dictionary roundtrip."""
        d = {"stale_after_seconds": 3.0, "disconnected_after_seconds": 8.0}
        cfg = HealthMonitorConfig.from_dict(d)
        assert cfg.stale_after_seconds == 3.0
        assert cfg.disconnected_after_seconds == 8.0
        assert cfg.to_dict() == d

    def test_from_json_file(self) -> None:
        """Verify loading from canonical config/health_monitor_config.json."""
        config_path = (
            Path(__file__).resolve().parent.parent / "config" / "health_monitor_config.json"
        )
        assert config_path.is_file()
        cfg = HealthMonitorConfig.from_json_file(config_path)
        assert cfg.stale_after_seconds == 2.0
        assert cfg.disconnected_after_seconds == 5.0

    def test_invalid_config_values(self) -> None:
        """Verify validation errors for bad threshold configurations."""
        with pytest.raises(ValueError, match="stale_after_seconds must be a finite positive number"):
            HealthMonitorConfig(stale_after_seconds=0.0)

        with pytest.raises(ValueError, match="disconnected_after_seconds must be a finite positive number"):
            HealthMonitorConfig(disconnected_after_seconds=-1.0)

        with pytest.raises(ValueError, match="must be strictly less than"):
            HealthMonitorConfig(stale_after_seconds=5.0, disconnected_after_seconds=5.0)

        with pytest.raises(ValueError, match="must be strictly less than"):
            HealthMonitorConfig(stale_after_seconds=6.0, disconnected_after_seconds=5.0)


class TestCSIStalenessClassification:
    """Validate CSI source staleness detection across all three boundary regions."""

    def test_fresh_or_never_received_state(self) -> None:
        """Verify fresh state (no records ever received) evaluates to disconnected and None age."""
        monitor = SystemHealthMonitor(HealthMonitorConfig(stale_after_seconds=2.0, disconnected_after_seconds=5.0))
        # Unregistered probe
        h1 = monitor.get_current_health(current_time=100.0)
        assert h1.csi_source_status == "disconnected"
        assert h1.csi_source_last_record_age_seconds is None
        assert h1.overall_status == "critical"

        # Registered probe returning None
        monitor.register_csi_source_probe(lambda: None)
        h2 = monitor.get_current_health(current_time=100.0)
        assert h2.csi_source_status == "disconnected"
        assert h2.csi_source_last_record_age_seconds is None
        assert h2.overall_status == "critical"

    def test_recent_record_is_active(self) -> None:
        """Verify record within stale_after_seconds evaluates to active."""
        monitor = SystemHealthMonitor(HealthMonitorConfig(stale_after_seconds=2.0, disconnected_after_seconds=5.0))
        monitor.register_csi_source_probe(lambda: 99.5)  # age = 0.5s < 2.0s
        monitor.register_database_probe(lambda: "connected")

        h = monitor.get_current_health(current_time=100.0)
        assert h.csi_source_status == "active"
        assert h.csi_source_last_record_age_seconds == pytest.approx(0.5)
        assert h.overall_status == "healthy"

    def test_stale_boundary_exact_and_intermediate(self) -> None:
        """Verify exact boundary at stale_after_seconds and within (stale, disconnected)."""
        monitor = SystemHealthMonitor(HealthMonitorConfig(stale_after_seconds=2.0, disconnected_after_seconds=5.0))
        monitor.register_database_probe(lambda: "connected")

        # Exact boundary: age == 2.0s -> stale
        monitor.register_csi_source_probe(lambda: 98.0)
        h1 = monitor.get_current_health(current_time=100.0)
        assert h1.csi_source_status == "stale"
        assert h1.csi_source_last_record_age_seconds == pytest.approx(2.0)
        assert h1.overall_status == "degraded"

        # Intermediate age: 3.5s -> stale
        monitor.register_csi_source_probe(lambda: 96.5)
        h2 = monitor.get_current_health(current_time=100.0)
        assert h2.csi_source_status == "stale"
        assert h2.csi_source_last_record_age_seconds == pytest.approx(3.5)
        assert h2.overall_status == "degraded"

    def test_disconnected_boundary_exact_and_long_elapsed(self) -> None:
        """Verify exact boundary at disconnected_after_seconds and long elapsed time."""
        monitor = SystemHealthMonitor(HealthMonitorConfig(stale_after_seconds=2.0, disconnected_after_seconds=5.0))
        monitor.register_database_probe(lambda: "connected")

        # Exact boundary: age == 5.0s -> disconnected
        monitor.register_csi_source_probe(lambda: 95.0)
        h1 = monitor.get_current_health(current_time=100.0)
        assert h1.csi_source_status == "disconnected"
        assert h1.csi_source_last_record_age_seconds == pytest.approx(5.0)
        assert h1.overall_status == "critical"

        # Long elapsed: age == 600.0s -> disconnected
        monitor.register_csi_source_probe(lambda: 100.0 - 600.0)
        h2 = monitor.get_current_health(current_time=100.0)
        assert h2.csi_source_status == "disconnected"
        assert h2.csi_source_last_record_age_seconds == pytest.approx(600.0)
        assert h2.overall_status == "critical"


class TestOverallStatusRollup:
    """Validate deterministic overall_status derivation rules across subsystems."""

    def test_all_subsystems_healthy(self) -> None:
        """Verify overall_status == healthy when all subsystems are operational."""
        monitor = SystemHealthMonitor()
        monitor.register_csi_source_probe(lambda: 99.0)  # age = 1.0s -> active
        monitor.register_database_probe(lambda: "connected")
        monitor.register_websocket_probe(lambda: 3)

        h = monitor.get_current_health(current_time=100.0)
        assert h.overall_status == "healthy"

    def test_degraded_when_csi_stale(self) -> None:
        """Verify overall_status == degraded when CSI is stale even if DB is connected."""
        monitor = SystemHealthMonitor()
        monitor.register_csi_source_probe(lambda: 97.5)  # age = 2.5s -> stale
        monitor.register_database_probe(lambda: "connected")

        h = monitor.get_current_health(current_time=100.0)
        assert h.csi_source_status == "stale"
        assert h.database_status == "connected"
        assert h.overall_status == "degraded"

    def test_degraded_when_database_unknown(self) -> None:
        """Verify overall_status == degraded when DB is unknown even if CSI is active."""
        monitor = SystemHealthMonitor()
        monitor.register_csi_source_probe(lambda: 99.0)  # active
        monitor.register_database_probe(lambda: "unknown")

        h = monitor.get_current_health(current_time=100.0)
        assert h.csi_source_status == "active"
        assert h.database_status == "unknown"
        assert h.overall_status == "degraded"

    def test_critical_when_csi_disconnected(self) -> None:
        """Verify overall_status == critical when CSI is disconnected."""
        monitor = SystemHealthMonitor()
        monitor.register_csi_source_probe(lambda: 90.0)  # age = 10s -> disconnected
        monitor.register_database_probe(lambda: "connected")

        h = monitor.get_current_health(current_time=100.0)
        assert h.csi_source_status == "disconnected"
        assert h.overall_status == "critical"

    def test_critical_when_database_disconnected(self) -> None:
        """Verify overall_status == critical when DB is disconnected even if CSI is active."""
        monitor = SystemHealthMonitor()
        monitor.register_csi_source_probe(lambda: 99.5)  # active
        monitor.register_database_probe(lambda: "disconnected")

        h = monitor.get_current_health(current_time=100.0)
        assert h.csi_source_status == "active"
        assert h.database_status == "disconnected"
        assert h.overall_status == "critical"

    def test_critical_dominates_stale(self) -> None:
        """Verify critical takes precedence over degraded when one is disconnected and one is stale."""
        monitor = SystemHealthMonitor()
        monitor.register_csi_source_probe(lambda: 97.0)  # stale
        monitor.register_database_probe(lambda: "disconnected")  # critical

        h = monitor.get_current_health(current_time=100.0)
        assert h.csi_source_status == "stale"
        assert h.database_status == "disconnected"
        assert h.overall_status == "critical"


class TestFaultTolerantProbes:
    """Validate exception handling during probe execution."""

    def test_csi_probe_raising_exception_yields_disconnected(self) -> None:
        """Probe raising error does not crash monitor; maps to disconnected."""
        monitor = SystemHealthMonitor()

        def broken_probe() -> Optional[float]:
            raise RuntimeError("Hardware UART bus communication fault")

        monitor.register_csi_source_probe(broken_probe)
        monitor.register_database_probe(lambda: "connected")

        h = monitor.get_current_health(current_time=100.0)
        assert h.csi_source_status == "disconnected"
        assert h.csi_source_last_record_age_seconds is None
        assert h.overall_status == "critical"

    def test_database_probe_raising_exception_yields_disconnected(self) -> None:
        """DB probe raising error maps to disconnected."""
        monitor = SystemHealthMonitor()
        monitor.register_csi_source_probe(lambda: 99.5)

        def broken_db() -> str:
            raise ConnectionError("SQLite lock acquisition timeout")

        monitor.register_database_probe(broken_db)
        h = monitor.get_current_health(current_time=100.0)
        assert h.database_status == "disconnected"
        assert h.overall_status == "critical"

    def test_websocket_probe_raising_exception_yields_zero_clients(self) -> None:
        """WS probe raising error defaults to 0 clients without error."""
        monitor = SystemHealthMonitor()
        monitor.register_csi_source_probe(lambda: 99.5)
        monitor.register_database_probe(lambda: "connected")

        def broken_ws() -> int:
            raise OSError("Socket descriptor closed")

        monitor.register_websocket_probe(broken_ws)
        h = monitor.get_current_health(current_time=100.0)
        assert h.websocket_client_count == 0
        assert h.overall_status == "healthy"


class TestConfigDrivenThresholdAlteration:
    """Verify changing thresholds in config alters staleness classification boundaries."""

    def test_custom_thresholds_alter_classification(self) -> None:
        """Setting thresholds to 10.0s and 30.0s alters boundary transitions."""
        cfg = HealthMonitorConfig(stale_after_seconds=10.0, disconnected_after_seconds=30.0)
        monitor = SystemHealthMonitor(cfg)
        monitor.register_database_probe(lambda: "connected")

        # Age = 5.0s: would be disconnected under default (5.0s), but is active under custom (10.0s)
        monitor.register_csi_source_probe(lambda: 95.0)
        h1 = monitor.get_current_health(current_time=100.0)
        assert h1.csi_source_status == "active"
        assert h1.overall_status == "healthy"

        # Age = 15.0s: stale under custom (10.0s..30.0s)
        monitor.register_csi_source_probe(lambda: 85.0)
        h2 = monitor.get_current_health(current_time=100.0)
        assert h2.csi_source_status == "stale"
        assert h2.overall_status == "degraded"

        # Age = 35.0s: disconnected under custom (> 30.0s)
        monitor.register_csi_source_probe(lambda: 65.0)
        h3 = monitor.get_current_health(current_time=100.0)
        assert h3.csi_source_status == "disconnected"
        assert h3.overall_status == "critical"


class TestSubsystemIntegration:
    """Integration harness wiring live components into SystemHealthMonitor."""

    def test_live_subsystem_probes_reporting(self, tmp_path: Path) -> None:
        """Wire real SyntheticCSISource, SQLiteDatabaseManager, and WebSocketStatePublisher probes."""
        monitor = SystemHealthMonitor(HealthMonitorConfig(stale_after_seconds=1.0, disconnected_after_seconds=3.0))

        # 1. Real SQLiteDatabaseManager
        db_path = tmp_path / "test_health.db"
        db = SQLiteDatabaseManager(db_path)
        db.connect()

        # Database probe: returns "connected" if file exists and connection is live
        monitor.register_database_probe(lambda: "connected" if db_path.exists() else "disconnected")

        # 2. Real WebSocketStatePublisher
        ws_pub = WebSocketStatePublisher(WebSocketConfig(port=0))
        ws_pub.start()
        monitor.register_websocket_probe(lambda: ws_pub.client_count)

        # 3. Real SyntheticCSISource
        last_rec_ts: Optional[float] = None

        def record_tracker() -> Optional[float]:
            return last_rec_ts

        monitor.register_csi_source_probe(record_tracker)

        try:
            # Check 1: Before any record arrives
            h_init = monitor.get_current_health(current_time=1000.0)
            assert h_init.csi_source_status == "disconnected"
            assert h_init.database_status == "connected"
            assert h_init.websocket_client_count == 0
            assert h_init.overall_status == "critical"

            # Ingest 1 synthetic record
            scenario = ScenarioConfig(
                scenario_name="health_test",
                duration_seconds=1.0,
                sample_rate_hz=10.0,
                subcarrier_count=52,
                realtime=False,
                start_epoch_time=1000.0,
            )
            with SyntheticCSISource(scenario) as source:
                rec = source.read_record()
                assert rec is not None
                last_rec_ts = rec.timestamp  # 1000.0

            # Check 2: Recent record (1000.5s -> age = 0.5s < 1.0s stale)
            h_active = monitor.get_current_health(current_time=1000.5)
            assert h_active.csi_source_status == "active"
            assert h_active.csi_source_last_record_age_seconds == pytest.approx(0.5)
            assert h_active.overall_status == "healthy"

            # Check 3: Stale record (1002.0s -> age = 2.0s, in [1.0, 3.0))
            h_stale = monitor.get_current_health(current_time=1002.0)
            assert h_stale.csi_source_status == "stale"
            assert h_stale.overall_status == "degraded"

            # Check 4: Disconnected record (1004.0s -> age = 4.0s > 3.0s)
            h_disc = monitor.get_current_health(current_time=1004.0)
            assert h_disc.csi_source_status == "disconnected"
            assert h_disc.overall_status == "critical"

        finally:
            ws_pub.stop()
            db.close()
