"""Application configuration dataclass and loader for WiSense-Twin.

This module defines `AppConfig`, an immutable configuration container mirroring the pattern
established by `ScenarioConfig` (frozen dataclass, from_dict/from_json_file loaders, and
strict validation in __post_init__).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_VALID_LOG_FORMATS = {"text", "json"}
_VALID_CSI_SOURCE_MODES = {"synthetic", "hardware"}


@dataclass(frozen=True)
class AppConfig:
    """System-wide runtime configuration for the WiSense-Twin backend pipeline.

    Attributes:
        websocket_host: Host/IP address for the WebSocket broadcast server (reserved for Section 10).
        websocket_port: Port for the WebSocket server (reserved for Section 10).
        database_path: File path for the SQLite metrics and events database (reserved for Section 9).
        log_level: Logging severity threshold ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL").
        log_format: Structured log output format ("text" or "json").
        csi_source_mode: Frame ingestion driver mode ("synthetic" or "hardware").
        synthetic_scenario_path: Relative or absolute path to synthetic scenario JSON
            (required when csi_source_mode is "synthetic").
        serial_port: Communication port name for real ESP32 hardware UART (reserved for Section 15).
            Must not default to any hardcoded COM port.
        serial_baud_rate: Baud rate for the serial UART stream (reserved for Section 15).
    """

    websocket_host: str = "127.0.0.1"
    websocket_port: int = 8765
    database_path: str = "data/wisense.db"
    log_level: str = "INFO"
    log_format: str = "text"
    csi_source_mode: str = "synthetic"
    synthetic_scenario_path: Optional[str] = "config/synthetic_scenarios/example.json"
    serial_port: Optional[str] = None
    serial_baud_rate: Optional[int] = None
    config_dir: str = "config"
    calibration_profile_path: Optional[str] = None
    motion_config_path: Optional[str] = None
    expected_sample_rate_hz: float = 20.0
    health_log_interval_seconds: float = 10.0
    stat_log_interval_seconds: float = 5.0
    db_flush_interval_seconds: float = 1.0
    db_flush_batch_size: int = 50

    def __post_init__(self) -> None:
        """Validate invariant configuration constraints."""
        # Normalize and validate log_level
        if not isinstance(self.log_level, str):
            raise ValueError(f"log_level must be a string, got {type(self.log_level).__name__}")
        normalized_log_level = self.log_level.upper()
        if normalized_log_level not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"Invalid log_level '{self.log_level}'. Must be one of: {sorted(_VALID_LOG_LEVELS)}"
            )
        object.__setattr__(self, "log_level", normalized_log_level)

        # Normalize and validate log_format
        if not isinstance(self.log_format, str):
            raise ValueError(f"log_format must be a string, got {type(self.log_format).__name__}")
        normalized_log_format = self.log_format.lower()
        if normalized_log_format not in _VALID_LOG_FORMATS:
            raise ValueError(
                f"Invalid log_format '{self.log_format}'. Must be one of: {sorted(_VALID_LOG_FORMATS)}"
            )
        object.__setattr__(self, "log_format", normalized_log_format)

        # Normalize and validate csi_source_mode
        if not isinstance(self.csi_source_mode, str):
            raise ValueError(
                f"csi_source_mode must be a string, got {type(self.csi_source_mode).__name__}"
            )
        normalized_source_mode = self.csi_source_mode.lower()
        if normalized_source_mode not in _VALID_CSI_SOURCE_MODES:
            raise ValueError(
                f"Invalid csi_source_mode '{self.csi_source_mode}'. Must be one of: {sorted(_VALID_CSI_SOURCE_MODES)}"
            )
        object.__setattr__(self, "csi_source_mode", normalized_source_mode)

        # Validate websocket_host & port
        if not self.websocket_host or not isinstance(self.websocket_host, str):
            raise ValueError("websocket_host must be a non-empty string")
        if not (1 <= self.websocket_port <= 65535):
            raise ValueError(
                f"websocket_port must be between 1 and 65535, got {self.websocket_port}"
            )

        # Validate database_path
        if not self.database_path or not isinstance(self.database_path, str):
            raise ValueError("database_path must be a non-empty string")

        # Validate synthetic mode dependencies
        if normalized_source_mode == "synthetic":
            if not self.synthetic_scenario_path or not isinstance(self.synthetic_scenario_path, str):
                raise ValueError(
                    "synthetic_scenario_path must be a non-empty string when csi_source_mode is 'synthetic'"
                )

        # Validate hardware mode dependencies
        if normalized_source_mode == "hardware":
            if not self.serial_port or not isinstance(self.serial_port, str) or not self.serial_port.strip():
                raise ValueError(
                    "serial_port must be a non-empty string when csi_source_mode is 'hardware'"
                )

        # Validate serial parameters
        if self.serial_baud_rate is not None:
            if not isinstance(self.serial_baud_rate, int) or self.serial_baud_rate <= 0:
                raise ValueError(
                    f"serial_baud_rate must be a positive integer, got {self.serial_baud_rate}"
                )
        if self.serial_port is not None:
            if not isinstance(self.serial_port, str) or not self.serial_port.strip():
                raise ValueError("serial_port must be a non-empty string if specified")

        # Validate config_dir
        if not self.config_dir or not isinstance(self.config_dir, str) or not self.config_dir.strip():
            raise ValueError("config_dir must be a non-empty string")

        # Validate calibration_profile_path if specified
        if self.calibration_profile_path is not None:
            if not isinstance(self.calibration_profile_path, str) or not self.calibration_profile_path.strip():
                raise ValueError("calibration_profile_path must be a non-empty string if specified")

        # Validate motion_config_path if specified
        if self.motion_config_path is not None:
            if not isinstance(self.motion_config_path, str) or not self.motion_config_path.strip():
                raise ValueError("motion_config_path must be a non-empty string if specified")

        # Validate expected_sample_rate_hz
        if (
            isinstance(self.expected_sample_rate_hz, bool)
            or not isinstance(self.expected_sample_rate_hz, (int, float))
            or self.expected_sample_rate_hz <= 0.0
        ):
            raise ValueError(
                f"expected_sample_rate_hz must be a positive number, got {self.expected_sample_rate_hz}"
            )

        # Validate interval timers and batch sizes
        if (
            isinstance(self.health_log_interval_seconds, bool)
            or not isinstance(self.health_log_interval_seconds, (int, float))
            or self.health_log_interval_seconds <= 0.0
        ):
            raise ValueError(
                f"health_log_interval_seconds must be a positive number, got {self.health_log_interval_seconds}"
            )
        if (
            isinstance(self.stat_log_interval_seconds, bool)
            or not isinstance(self.stat_log_interval_seconds, (int, float))
            or self.stat_log_interval_seconds <= 0.0
        ):
            raise ValueError(
                f"stat_log_interval_seconds must be a positive number, got {self.stat_log_interval_seconds}"
            )
        if (
            isinstance(self.db_flush_interval_seconds, bool)
            or not isinstance(self.db_flush_interval_seconds, (int, float))
            or self.db_flush_interval_seconds <= 0.0
        ):
            raise ValueError(
                f"db_flush_interval_seconds must be a positive number, got {self.db_flush_interval_seconds}"
            )
        if (
            isinstance(self.db_flush_batch_size, bool)
            or not isinstance(self.db_flush_batch_size, int)
            or self.db_flush_batch_size <= 0
        ):
            raise ValueError(
                f"db_flush_batch_size must be a positive integer, got {self.db_flush_batch_size}"
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        """Parse an AppConfig instance from a dictionary.

        Args:
            data: Dictionary containing configuration key-values.

        Returns:
            Validated AppConfig instance.
        """
        raw_port = data.get("websocket_port", 8765)
        try:
            websocket_port = int(raw_port)
        except (ValueError, TypeError):
            raise ValueError(f"websocket_port must be an integer, got {raw_port!r}")

        raw_baud = data.get("serial_baud_rate")
        serial_baud_rate: Optional[int] = None
        if raw_baud is not None:
            try:
                serial_baud_rate = int(raw_baud)
            except (ValueError, TypeError):
                raise ValueError(f"serial_baud_rate must be an integer, got {raw_baud!r}")

        raw_sr = data.get("expected_sample_rate_hz", 20.0)
        try:
            expected_sample_rate_hz = float(raw_sr)
        except (ValueError, TypeError):
            raise ValueError(f"expected_sample_rate_hz must be a number, got {raw_sr!r}")

        raw_health_int = data.get("health_log_interval_seconds", 10.0)
        try:
            health_log_interval_seconds = float(raw_health_int)
        except (ValueError, TypeError):
            raise ValueError(f"health_log_interval_seconds must be a number, got {raw_health_int!r}")

        raw_stat_int = data.get("stat_log_interval_seconds", 5.0)
        try:
            stat_log_interval_seconds = float(raw_stat_int)
        except (ValueError, TypeError):
            raise ValueError(f"stat_log_interval_seconds must be a number, got {raw_stat_int!r}")

        raw_flush_int = data.get("db_flush_interval_seconds", 1.0)
        try:
            db_flush_interval_seconds = float(raw_flush_int)
        except (ValueError, TypeError):
            raise ValueError(f"db_flush_interval_seconds must be a number, got {raw_flush_int!r}")

        raw_batch_size = data.get("db_flush_batch_size", 50)
        try:
            db_flush_batch_size = int(raw_batch_size)
        except (ValueError, TypeError):
            raise ValueError(f"db_flush_batch_size must be an integer, got {raw_batch_size!r}")

        return cls(
            websocket_host=str(data.get("websocket_host", "127.0.0.1")),
            websocket_port=websocket_port,
            database_path=str(data.get("database_path", "data/wisense.db")),
            log_level=str(data.get("log_level", "INFO")),
            log_format=str(data.get("log_format", "text")),
            csi_source_mode=str(data.get("csi_source_mode", "synthetic")),
            synthetic_scenario_path=(
                str(data["synthetic_scenario_path"])
                if data.get("synthetic_scenario_path") is not None
                else None
            ),
            serial_port=(
                str(data["serial_port"]) if data.get("serial_port") is not None else None
            ),
            serial_baud_rate=serial_baud_rate,
            config_dir=str(data.get("config_dir", "config")),
            calibration_profile_path=(
                str(data["calibration_profile_path"])
                if data.get("calibration_profile_path") is not None
                else None
            ),
            motion_config_path=(
                str(data["motion_config_path"])
                if data.get("motion_config_path") is not None
                else None
            ),
            expected_sample_rate_hz=expected_sample_rate_hz,
            health_log_interval_seconds=health_log_interval_seconds,
            stat_log_interval_seconds=stat_log_interval_seconds,
            db_flush_interval_seconds=db_flush_interval_seconds,
            db_flush_batch_size=db_flush_batch_size,
        )

    @classmethod
    def from_json_file(cls, file_path: str | Path) -> AppConfig:
        """Load and validate application configuration from a JSON file.

        Args:
            file_path: Path to the JSON configuration file.

        Returns:
            Validated AppConfig instance.

        Raises:
            FileNotFoundError: If the file does not exist.
            json.JSONDecodeError: If the file is not valid JSON.
            ValueError: If configuration values fail validation.
        """
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Configuration file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)
