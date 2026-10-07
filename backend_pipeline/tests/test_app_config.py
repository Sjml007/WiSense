"""Unit tests for AppConfig configuration dataclass and validation."""

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from wisense_backend.config.app_config import AppConfig


class TestAppConfig:
    """Validate AppConfig parsing, validation, and immutability."""

    def test_default_config(self) -> None:
        """Verify default configuration attributes."""
        cfg = AppConfig()
        assert cfg.websocket_host == "127.0.0.1"
        assert cfg.websocket_port == 8765
        assert cfg.database_path == "data/wisense.db"
        assert cfg.log_level == "INFO"
        assert cfg.log_format == "text"
        assert cfg.csi_source_mode == "synthetic"
        assert cfg.synthetic_scenario_path == "config/synthetic_scenarios/example.json"
        assert cfg.serial_port is None
        assert cfg.serial_baud_rate is None

    def test_from_dict_valid(self) -> None:
        """Verify parsing a valid dictionary with custom fields."""
        data = {
            "websocket_host": "0.0.0.0",
            "websocket_port": 9000,
            "database_path": "custom/db.sqlite",
            "log_level": "debug",
            "log_format": "JSON",
            "csi_source_mode": "SYNTHETIC",
            "synthetic_scenario_path": "path/to/scenario.json",
            "serial_port": "COM3",
            "serial_baud_rate": 115200,
        }
        cfg = AppConfig.from_dict(data)
        assert cfg.websocket_host == "0.0.0.0"
        assert cfg.websocket_port == 9000
        assert cfg.database_path == "custom/db.sqlite"
        assert cfg.log_level == "DEBUG"  # normalized uppercase
        assert cfg.log_format == "json"  # normalized lowercase
        assert cfg.csi_source_mode == "synthetic"  # normalized lowercase
        assert cfg.synthetic_scenario_path == "path/to/scenario.json"
        assert cfg.serial_port == "COM3"
        assert cfg.serial_baud_rate == 115200

    def test_from_json_file_sample_config(self) -> None:
        """Verify loading the project's canonical config/system_config.json."""
        config_path = (
            Path(__file__).resolve().parent.parent / "config" / "system_config.json"
        )
        assert config_path.is_file(), f"Sample config not found at {config_path}"

        cfg = AppConfig.from_json_file(config_path)
        assert cfg.csi_source_mode == "synthetic"
        assert cfg.synthetic_scenario_path == "config/synthetic_scenarios/example.json"
        assert cfg.serial_port is None
        assert cfg.serial_baud_rate is None

    def test_immutability(self) -> None:
        """Verify that AppConfig is frozen and cannot be mutated."""
        cfg = AppConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.websocket_port = 9999  # type: ignore

    def test_invalid_csi_source_mode(self) -> None:
        """Verify that invalid source modes are rejected."""
        with pytest.raises(ValueError, match="Invalid csi_source_mode"):
            AppConfig(csi_source_mode="bluetooth")

    def test_invalid_log_level(self) -> None:
        """Verify that invalid log levels are rejected."""
        with pytest.raises(ValueError, match="Invalid log_level"):
            AppConfig(log_level="VERBOSE")

    def test_invalid_log_format(self) -> None:
        """Verify that invalid log formats are rejected."""
        with pytest.raises(ValueError, match="Invalid log_format"):
            AppConfig(log_format="yaml")

    def test_invalid_websocket_port_out_of_range(self) -> None:
        """Verify that port bounds (1-65535) are strictly enforced."""
        with pytest.raises(ValueError, match="websocket_port must be between 1 and 65535"):
            AppConfig(websocket_port=0)
        with pytest.raises(ValueError, match="websocket_port must be between 1 and 65535"):
            AppConfig(websocket_port=70000)

    def test_invalid_websocket_port_non_integer(self) -> None:
        """Verify non-integer ports in from_dict raise ValueError."""
        with pytest.raises(ValueError, match="websocket_port must be an integer"):
            AppConfig.from_dict({"websocket_port": "not_a_number"})

    def test_empty_websocket_host(self) -> None:
        """Verify empty websocket_host is rejected."""
        with pytest.raises(ValueError, match="websocket_host must be a non-empty string"):
            AppConfig(websocket_host="")

    def test_empty_database_path(self) -> None:
        """Verify empty database_path is rejected."""
        with pytest.raises(ValueError, match="database_path must be a non-empty string"):
            AppConfig(database_path="")

    def test_missing_synthetic_scenario_path_in_synthetic_mode(self) -> None:
        """Verify synthetic mode requires synthetic_scenario_path."""
        with pytest.raises(
            ValueError, match="synthetic_scenario_path must be a non-empty string"
        ):
            AppConfig(csi_source_mode="synthetic", synthetic_scenario_path=None)

        with pytest.raises(
            ValueError, match="synthetic_scenario_path must be a non-empty string"
        ):
            AppConfig(csi_source_mode="synthetic", synthetic_scenario_path="")

    def test_invalid_serial_baud_rate(self) -> None:
        """Verify invalid serial baud rate values are rejected."""
        with pytest.raises(ValueError, match="serial_baud_rate must be a positive integer"):
            AppConfig(serial_baud_rate=-9600)
        with pytest.raises(ValueError, match="serial_baud_rate must be an integer"):
            AppConfig.from_dict({"serial_baud_rate": "fast"})

    def test_empty_serial_port(self) -> None:
        """Verify whitespace or empty serial_port string is rejected."""
        with pytest.raises(ValueError, match="serial_port must be a non-empty string"):
            AppConfig(serial_port="   ")

    def test_file_not_found(self, tmp_path: Path) -> None:
        """Verify FileNotFoundError on non-existent config file."""
        non_existent = tmp_path / "missing_config.json"
        with pytest.raises(FileNotFoundError, match="Configuration file not found"):
            AppConfig.from_json_file(non_existent)

    def test_invalid_json_file(self, tmp_path: Path) -> None:
        """Verify JSONDecodeError on malformed JSON file."""
        bad_json = tmp_path / "bad.json"
        bad_json.write_text("{ unquoted_key: 123 ", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            AppConfig.from_json_file(bad_json)
