"""Health monitoring contracts and data structures for WiSense-Twin.

Defines `SystemHealth` wire snapshot dataclass and the `HealthMonitor` protocol.

AUDIT COMPLIANCE & DESIGN DISCIPLINE:
-------------------------------------
1. Observability Only:
   This module observes and reports subsystem states; it does NOT implement
   reconnection, recovery, or error-correction mechanics.
2. Honest, Categorical Vocabulary:
   Subsystem and rollup health states use discrete, well-defined strings
   (e.g., "active", "stale", "disconnected", "healthy", "degraded", "critical").
   Fabricated numeric "health scores" (e.g., "85% health") are strictly forbidden.
3. Decoupled Architecture:
   The monitor accepts generic callable probes, decoupling it from concrete
   hardware, serial drivers, or database connections.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Optional, Protocol, runtime_checkable


class CsiSourceStatus(StrEnum):
    """Operational status of the CSI packet ingestion stream."""

    ACTIVE = "active"
    STALE = "stale"
    DISCONNECTED = "disconnected"


class DatabaseStatus(StrEnum):
    """Operational status of the telemetry database persistence backend."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


class OverallHealthStatus(StrEnum):
    """Composite system health status rolled up from individual subsystem statuses."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"


VALID_CSI_STATUSES: frozenset[str] = frozenset(status.value for status in CsiSourceStatus)
VALID_DATABASE_STATUSES: frozenset[str] = frozenset(status.value for status in DatabaseStatus)
VALID_OVERALL_STATUSES: frozenset[str] = frozenset(status.value for status in OverallHealthStatus)


@dataclass(frozen=True, slots=True)
class SystemHealth:
    """Immutable snapshot of the WiSense-Twin backend operational health.

    Attributes:
        timestamp: UTC Unix epoch seconds (time.time()) when the snapshot was computed.
        csi_source_status: Categorical status of CSI ingestion ("active", "stale", "disconnected").
        csi_source_last_record_age_seconds: Elapsed seconds since the last received CSIRecord,
            or None if no record has ever been received.
        websocket_client_count: Number of currently connected WebSocket twin/dashboard clients (>= 0).
        database_status: Categorical status of database connectivity ("connected", "disconnected", "unknown").
        overall_status: Deterministic rollup status ("healthy", "degraded", "critical").
    """

    timestamp: float
    csi_source_status: str
    csi_source_last_record_age_seconds: Optional[float]
    websocket_client_count: int
    database_status: str
    overall_status: str

    def __post_init__(self) -> None:
        """Validate invariant constraints on SystemHealth attributes."""
        if self.timestamp <= 0.0:
            raise ValueError(f"timestamp must be positive, got {self.timestamp}")

        if self.csi_source_status not in VALID_CSI_STATUSES:
            raise ValueError(
                f"Invalid csi_source_status '{self.csi_source_status}'. "
                f"Must be one of {sorted(VALID_CSI_STATUSES)}"
            )

        if self.csi_source_last_record_age_seconds is not None:
            if (
                not math.isfinite(self.csi_source_last_record_age_seconds)
                or self.csi_source_last_record_age_seconds < 0.0
            ):
                raise ValueError(
                    f"csi_source_last_record_age_seconds must be a finite non-negative number or None, "
                    f"got {self.csi_source_last_record_age_seconds}"
                )

        if not isinstance(self.websocket_client_count, int) or isinstance(self.websocket_client_count, bool):
            raise TypeError(
                f"websocket_client_count must be an int, got {type(self.websocket_client_count).__name__}"
            )
        if self.websocket_client_count < 0:
            raise ValueError(
                f"websocket_client_count must be non-negative, got {self.websocket_client_count}"
            )

        if self.database_status not in VALID_DATABASE_STATUSES:
            raise ValueError(
                f"Invalid database_status '{self.database_status}'. "
                f"Must be one of {sorted(VALID_DATABASE_STATUSES)}"
            )

        if self.overall_status not in VALID_OVERALL_STATUSES:
            raise ValueError(
                f"Invalid overall_status '{self.overall_status}'. "
                f"Must be one of {sorted(VALID_OVERALL_STATUSES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Convert health snapshot to a JSON-serializable dictionary."""
        return {
            "timestamp": self.timestamp,
            "csi_source_status": self.csi_source_status,
            "csi_source_last_record_age_seconds": (
                round(self.csi_source_last_record_age_seconds, 3)
                if self.csi_source_last_record_age_seconds is not None
                else None
            ),
            "websocket_client_count": self.websocket_client_count,
            "database_status": self.database_status,
            "overall_status": self.overall_status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SystemHealth:
        """Construct SystemHealth from a dictionary."""
        age = (
            float(data["csi_source_last_record_age_seconds"])
            if data.get("csi_source_last_record_age_seconds") is not None
            else None
        )
        return cls(
            timestamp=float(data["timestamp"]),
            csi_source_status=str(data["csi_source_status"]),
            csi_source_last_record_age_seconds=age,
            websocket_client_count=int(data["websocket_client_count"]),
            database_status=str(data["database_status"]),
            overall_status=str(data["overall_status"]),
        )

    def to_json(self) -> str:
        """Serialize health snapshot to a JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, json_str: str) -> SystemHealth:
        """Deserialize health snapshot from a JSON string."""
        data = json.loads(json_str)
        return cls.from_dict(data)


@runtime_checkable
class HealthMonitor(Protocol):
    """Abstract protocol for health monitors producing SystemHealth snapshots."""

    def register_csi_source_probe(self, probe: Callable[[], Optional[float]]) -> None:
        """Register a probe returning the UTC timestamp of the most recent CSIRecord (or None)."""
        ...

    def register_database_probe(self, probe: Callable[[], str]) -> None:
        """Register a probe returning database status ("connected", "disconnected", "unknown")."""
        ...

    def register_websocket_probe(self, probe: Callable[[], int]) -> None:
        """Register a probe returning the number of connected WebSocket clients."""
        ...

    def get_current_health(self, current_time: Optional[float] = None) -> SystemHealth:
        """Produce an on-demand snapshot of system health.

        Args:
            current_time: Optional UTC timestamp to evaluate against (defaults to time.time()).

        Returns:
            SystemHealth snapshot capturing current subsystem and rollup statuses.
        """
        ...
