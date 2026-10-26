import json
import threading
import time
from pathlib import Path

import pytest

from wisense_backend.app import Application
from wisense_backend.communication.websocket_server import WebSocketConfig
from wisense_backend.config.app_config import AppConfig


class TestApplicationSkeleton:
    """Validate Application skeleton lifecycle, wiring, and error handling."""

    def test_smoke_synthetic_mode(self, tmp_path: Path) -> None:
        """Smoke test: Application runs against Section 1 example scenario, counts records, and exits."""
        scenario_path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "synthetic_scenarios"
            / "example.json"
        )
        assert scenario_path.is_file(), f"Scenario file not found at {scenario_path}"

        cfg = AppConfig(
            csi_source_mode="synthetic",
            synthetic_scenario_path=str(scenario_path),
            database_path=str(tmp_path / "app_smoke.db"),
            log_level="INFO",
            log_format="text",
        )

        app = Application(config=cfg, ws_config=WebSocketConfig(port=0))
        record_count = app.run()

        # In example.json: duration_seconds = 15.0, sample_rate_hz = 20.0 -> exactly 300 records
        assert record_count == 300
        # Check source is properly closed after run()
        assert not app.source.is_open

    def test_smoke_default_system_config(self, tmp_path: Path) -> None:
        """Verify Application runs cleanly using default config resolution with test port and tmp db."""
        cfg = AppConfig(database_path=str(tmp_path / "default_sys.db"))
        app = Application(config=cfg, ws_config=WebSocketConfig(port=0))
        assert app.config.csi_source_mode == "synthetic"
        record_count = app.run()
        assert record_count == 300
        assert not app.source.is_open

    def test_hardware_mode_without_port_raises_value_error(self) -> None:
        """Verify csi_source_mode 'hardware' without serial_port raises ValueError."""
        with pytest.raises(
            ValueError,
            match="serial_port must be a non-empty string when csi_source_mode is 'hardware'",
        ):
            AppConfig(
                csi_source_mode="hardware",
                log_level="INFO",
            )

    def test_hardware_mode_instantiates_serial_source(self, tmp_path: Path) -> None:
        """Verify csi_source_mode 'hardware' instantiates a SerialCSISource."""
        from wisense_backend.ingestion.serial_source import SerialCSISource

        cfg = AppConfig(
            csi_source_mode="hardware",
            serial_port="COM4",
            serial_baud_rate=921600,
            database_path=str(tmp_path / "hw.db"),
            log_level="INFO",
        )
        app = Application(config=cfg, ws_config=WebSocketConfig(port=0))
        assert isinstance(app.source, SerialCSISource)
        assert app.source.config.port == "COM4"
        assert app.source.config.baud_rate == 921600
        assert not app.source.is_open

    def test_stop_signal_shuts_down_cleanly(self, tmp_path: Path) -> None:
        """Verify calling stop() halts processing early and closes resources cleanly."""
        scenario_path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "synthetic_scenarios"
            / "example.json"
        )
        with open(scenario_path, "r", encoding="utf-8") as f:
            scenario_data = json.load(f)
        # Enable wall-clock pacing (realtime=True) so 300 frames take 15 seconds
        scenario_data["realtime"] = True

        realtime_scenario_file = tmp_path / "example_realtime.json"
        realtime_scenario_file.write_text(json.dumps(scenario_data), encoding="utf-8")

        cfg = AppConfig(
            csi_source_mode="synthetic",
            synthetic_scenario_path=str(realtime_scenario_file),
            database_path=str(tmp_path / "stop.db"),
        )

        app = Application(config=cfg, ws_config=WebSocketConfig(port=0))

        # Trigger stop() shortly after starting in a background thread
        def trigger_stop() -> None:
            time.sleep(0.08)
            app.stop()

        stop_thread = threading.Thread(target=trigger_stop, daemon=True)
        stop_thread.start()

        record_count = app.run()
        stop_thread.join(timeout=2.0)

        # Proves early termination: strictly less than total 300 records
        assert record_count < 300
        assert not app.source.is_open

    def test_missing_scenario_file_raises_error(self, tmp_path: Path) -> None:
        """Verify clear FileNotFoundError when configured synthetic scenario file does not exist."""
        missing_scenario = tmp_path / "does_not_exist.json"
        cfg = AppConfig(
            csi_source_mode="synthetic",
            synthetic_scenario_path=str(missing_scenario),
            database_path=str(tmp_path / "missing.db"),
        )
        with pytest.raises(FileNotFoundError, match="Scenario configuration file not found"):
            Application(config=cfg, ws_config=WebSocketConfig(port=0))
