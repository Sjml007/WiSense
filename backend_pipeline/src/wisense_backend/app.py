"""Application skeleton for the WiSense-Twin backend pipeline.

Wires together configuration loading, structured logging setup, and CSIRecordSource
lifecycle management, providing an end-to-end operational skeleton without
downstream business logic.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from wisense_backend.config.app_config import AppConfig
from wisense_backend.contracts.csi_source import CSIRecordSource
from wisense_backend.ingestion.serial_source import SerialCSISource, SerialSourceConfig
from wisense_backend.ingestion.synthetic_source import ScenarioConfig, SyntheticCSISource
from wisense_backend.logging_setup import configure_logging


class Application:
    """Core backend application coordinator.

    Responsible for bootstrapping runtime configuration, initializing structured logging,
    instantiating the configured CSI frame source, and managing the main ingestion loop.
    """

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        config_path: Optional[str | Path] = None,
    ) -> None:
        """Initialize the Application instance.

        Args:
            config: Optional pre-loaded AppConfig. If None, config_path will be used.
            config_path: Optional path to system_config.json. If None and config is None,
                default search locations will be resolved.

        Raises:
            FileNotFoundError: If the configuration file or scenario file cannot be found.
            NotImplementedError: If csi_source_mode is set to "hardware".
            ValueError: If configuration values fail validation.
        """
        if config is not None:
            self.config = config
        else:
            resolved_config_path = self._resolve_config_path(config_path)
            self.config = AppConfig.from_json_file(resolved_config_path)

        # Initialize structured logging
        configure_logging(self.config)
        self.logger = logging.getLogger("wisense_backend.app")

        self._stop_requested = threading.Event()
        self.source: CSIRecordSource = self._create_source()

    def _resolve_config_path(self, path: Optional[str | Path]) -> Path:
        """Resolve config file path against common project directories."""
        if path is not None:
            p = Path(path)
            if p.is_file():
                return p.resolve()
            # Check relative to backend_pipeline directory
            backend_dir = Path(__file__).resolve().parent.parent.parent
            if (backend_dir / p).is_file():
                return (backend_dir / p).resolve()
            return p.resolve()

        # Default search paths
        candidates = [
            Path("config/system_config.json"),
            Path("backend_pipeline/config/system_config.json"),
            Path(__file__).resolve().parent.parent.parent / "config" / "system_config.json",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()

        raise FileNotFoundError(
            "Default configuration file 'config/system_config.json' not found."
        )

    def _resolve_scenario_path(self, scenario_path_str: str) -> Path:
        """Resolve synthetic scenario file path against common project directories."""
        p = Path(scenario_path_str)
        if p.is_file():
            return p.resolve()

        # Check relative to backend_pipeline root directory
        backend_dir = Path(__file__).resolve().parent.parent.parent
        if (backend_dir / p).is_file():
            return (backend_dir / p).resolve()

        # Check relative to repo root directory
        repo_root = backend_dir.parent
        if (repo_root / p).is_file():
            return (repo_root / p).resolve()

        return p.resolve()

    def _create_source(self) -> CSIRecordSource:
        """Instantiate the appropriate CSIRecordSource based on configuration."""
        if self.config.csi_source_mode == "synthetic":
            assert self.config.synthetic_scenario_path is not None
            scenario_path = self._resolve_scenario_path(self.config.synthetic_scenario_path)
            scenario_cfg = ScenarioConfig.from_json_file(scenario_path)
            return SyntheticCSISource(scenario_cfg)

        if self.config.csi_source_mode == "hardware":
            if not self.config.serial_port:
                raise ValueError(
                    "serial_port must be configured when csi_source_mode is 'hardware'"
                )
            baud_rate = (
                self.config.serial_baud_rate
                if self.config.serial_baud_rate is not None
                else 921600
            )
            serial_cfg = SerialSourceConfig(
                port=self.config.serial_port,
                baud_rate=baud_rate,
            )
            return SerialCSISource(serial_cfg)

        raise ValueError(f"Unsupported csi_source_mode: {self.config.csi_source_mode}")

    def stop(self) -> None:
        """Signal the application to halt the ingestion loop and close resources."""
        self._stop_requested.set()
        self.logger.info("Application stop signal received")
        if self.source is not None and self.source.is_open:
            self.source.close()

    def run(self) -> int:
        """Run the ingestion loop until stream completion or a stop signal is received.

        Opens the CSI source, reads frames in a loop, counts records, and ensures
        clean shutdown.

        Returns:
            Total number of CSI records read.
        """
        self._stop_requested.clear()
        self.logger.info(
            "Application starting up (csi_source_mode: %s)",
            self.config.csi_source_mode,
            extra={"csi_source_mode": self.config.csi_source_mode},
        )

        record_count = 0
        try:
            with self.source:
                self.logger.info("CSI record source opened successfully")
                for _ in self.source:
                    if self._stop_requested.is_set():
                        self.logger.info("Stop requested, halting frame ingestion loop")
                        break
                    record_count += 1
        except Exception as exc:
            self.logger.error("Exception in application ingestion loop: %s", exc, exc_info=True)
            raise
        finally:
            self.logger.info(
                "Application shutdown complete. Total records processed: %d",
                record_count,
                extra={"records_processed": record_count},
            )

        return record_count
