"""System health monitor implementation for WiSense-Twin.

Observes subsystem readiness, evaluates CSI packet staleness, and rolls up
subsystem health into an on-demand `SystemHealth` snapshot.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from wisense_backend.contracts.health_status import (
    CsiSourceStatus,
    DatabaseStatus,
    HealthMonitor,
    OverallHealthStatus,
    SystemHealth,
    VALID_DATABASE_STATUSES,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HealthMonitorConfig:
    """Configuration thresholds for health monitoring and staleness evaluation.

    Attributes:
        stale_after_seconds: Maximum elapsed seconds without a CSIRecord before
            the stream is marked as STALE. Default 2.0s.
        disconnected_after_seconds: Maximum elapsed seconds without a CSIRecord before
            the stream is marked as DISCONNECTED. Default 5.0s.
    """

    stale_after_seconds: float = 2.0
    disconnected_after_seconds: float = 5.0

    def __post_init__(self) -> None:
        """Validate configuration invariants."""
        if not math.isfinite(self.stale_after_seconds) or self.stale_after_seconds <= 0.0:
            raise ValueError(
                f"stale_after_seconds must be a finite positive number, got {self.stale_after_seconds}"
            )
        if (
            not math.isfinite(self.disconnected_after_seconds)
            or self.disconnected_after_seconds <= 0.0
        ):
            raise ValueError(
                f"disconnected_after_seconds must be a finite positive number, "
                f"got {self.disconnected_after_seconds}"
            )
        if self.stale_after_seconds >= self.disconnected_after_seconds:
            raise ValueError(
                f"stale_after_seconds ({self.stale_after_seconds}) must be strictly less than "
                f"disconnected_after_seconds ({self.disconnected_after_seconds})"
            )

    def to_dict(self) -> dict[str, Any]:
        """Convert configuration to dictionary."""
        return {
            "stale_after_seconds": self.stale_after_seconds,
            "disconnected_after_seconds": self.disconnected_after_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HealthMonitorConfig:
        """Construct HealthMonitorConfig from dictionary."""
        stale = float(data.get("stale_after_seconds", 2.0))
        disconnected = float(data.get("disconnected_after_seconds", 5.0))
        return cls(stale_after_seconds=stale, disconnected_after_seconds=disconnected)

    @classmethod
    def from_json_file(cls, path: str | Path) -> HealthMonitorConfig:
        """Load configuration from a JSON file."""
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"Health monitor config file not found: {file_path}")
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)


class SystemHealthMonitor:
    """Production health monitor producing on-demand SystemHealth snapshots.

    Implements `HealthMonitor` protocol using a lightweight pull model without
    background polling threads.

    Guarantees:
    - Observability only: Reports status but never attempts automated socket or serial reconnects.
    - Exception isolation: Errors in probe callables are trapped and mapped to
      disconnected/unknown states without raising exceptions to callers.
    - Deterministic rollup: Computes `overall_status` via explicit hierarchical rules.
    - Config-driven boundaries: CSI staleness thresholds are completely dynamic.
    """

    def __init__(self, config: Optional[HealthMonitorConfig] = None) -> None:
        """Initialize SystemHealthMonitor.

        Args:
            config: Optional HealthMonitorConfig. If None, default thresholds are used.
        """
        self._config = config or HealthMonitorConfig()
        self._csi_probe: Optional[Callable[[], Optional[float]]] = None
        self._database_probe: Optional[Callable[[], str]] = None
        self._websocket_probe: Optional[Callable[[], int]] = None
        self._custom_probes: dict[str, Callable[[], Any]] = {}

    @property
    def config(self) -> HealthMonitorConfig:
        """Return the configuration instance."""
        return self._config

    def register_csi_source_probe(self, probe: Callable[[], Optional[float]]) -> None:
        """Register a callable returning the UTC timestamp of the most recent CSIRecord."""
        self._csi_probe = probe

    def register_database_probe(self, probe: Callable[[], str]) -> None:
        """Register a callable returning database status string ("connected", "disconnected", "unknown")."""
        self._database_probe = probe

    def register_websocket_probe(self, probe: Callable[[], int]) -> None:
        """Register a callable returning the count of currently connected WebSocket clients."""
        self._websocket_probe = probe

    def register_custom_probe(self, name: str, probe: Callable[[], Any]) -> None:
        """Register a generic named probe for future subsystem extensibility."""
        self._custom_probes[name] = probe

    def get_current_health(self, current_time: Optional[float] = None) -> SystemHealth:
        """Produce an on-demand snapshot of system health.

        Args:
            current_time: Optional UTC timestamp to evaluate against. If None, time.time() is used.

        Returns:
            SystemHealth snapshot capturing current subsystem and rollup statuses.
        """
        now = time.time() if current_time is None else float(current_time)

        # 1. Evaluate CSI Source Status & Staleness
        csi_status, record_age = self._evaluate_csi_source(now)

        # 2. Evaluate Database Persistence Status
        db_status = self._evaluate_database()

        # 3. Evaluate WebSocket Subsystem Status
        client_count = self._evaluate_websocket()

        # 4. Rollup Overall Status
        overall_status = self._evaluate_overall_status(csi_status, db_status)

        return SystemHealth(
            timestamp=now,
            csi_source_status=csi_status,
            csi_source_last_record_age_seconds=record_age,
            websocket_client_count=client_count,
            database_status=db_status,
            overall_status=overall_status,
        )

    def _evaluate_csi_source(self, now: float) -> tuple[str, Optional[float]]:
        """Query CSI probe and classify stream staleness against configured thresholds."""
        if self._csi_probe is None:
            return CsiSourceStatus.DISCONNECTED.value, None

        try:
            last_record_ts = self._csi_probe()
        except Exception as e:
            logger.warning("CSI source health probe raised an exception: %s", e)
            return CsiSourceStatus.DISCONNECTED.value, None

        if last_record_ts is None:
            # Fresh state: source opened or idle, but no record has ever arrived
            return CsiSourceStatus.DISCONNECTED.value, None

        age = max(0.0, now - last_record_ts)

        if age < self._config.stale_after_seconds:
            return CsiSourceStatus.ACTIVE.value, age
        elif age < self._config.disconnected_after_seconds:
            return CsiSourceStatus.STALE.value, age
        else:
            return CsiSourceStatus.DISCONNECTED.value, age

    def _evaluate_database(self) -> str:
        """Query database persistence probe."""
        if self._database_probe is None:
            return DatabaseStatus.UNKNOWN.value

        try:
            raw_status = str(self._database_probe()).strip().lower()
        except Exception as e:
            logger.warning("Database health probe raised an exception: %s", e)
            return DatabaseStatus.DISCONNECTED.value

        if raw_status in VALID_DATABASE_STATUSES:
            return raw_status
        return DatabaseStatus.UNKNOWN.value

    def _evaluate_websocket(self) -> int:
        """Query WebSocket publisher probe for active client count."""
        if self._websocket_probe is None:
            return 0

        try:
            count = int(self._websocket_probe())
            return max(0, count)
        except Exception as e:
            logger.warning("WebSocket health probe raised an exception: %s", e)
            return 0

    @staticmethod
    def _evaluate_overall_status(csi_status: str, db_status: str) -> str:
        """Derive overall system health from subsystem states using deterministic rollup.

        Hierarchy:
        1. CRITICAL: Any core subsystem (CSI source or Database) is DISCONNECTED.
        2. DEGRADED: Any core subsystem is STALE or UNKNOWN.
        3. HEALTHY: All core subsystems are in their fully operational state.
        """
        if (
            csi_status == CsiSourceStatus.DISCONNECTED.value
            or db_status == DatabaseStatus.DISCONNECTED.value
        ):
            return OverallHealthStatus.CRITICAL.value

        if (
            csi_status == CsiSourceStatus.STALE.value
            or db_status == DatabaseStatus.UNKNOWN.value
        ):
            return OverallHealthStatus.DEGRADED.value

        return OverallHealthStatus.HEALTHY.value
