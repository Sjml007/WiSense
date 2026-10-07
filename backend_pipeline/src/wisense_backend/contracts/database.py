"""Database persistence contracts and data structures for WiSense-Twin.

This module defines:
- `EventLogRecord`: The comprehensive, immutable telemetry event record persisted to the
  database for every processed analysis window, fixing all gaps identified in the architecture
  audit (Audit Item #16).
- `DatabaseManager`: The abstract protocol implemented by all persistence backends.

AUDIT COMPLIANCE & DESIGN PRINCIPLES:
-------------------------------------
1. Latency Separation (Audit Item #14):
   `window_latency_ms` and `pipeline_latency_ms` are kept as two distinct fields and must NEVER
   be combined into a single "latency" number:
   - `window_latency_ms`: Physical buffering floor derived from configuration (window_size / sample_rate).
   - `pipeline_latency_ms`: Measured wall-clock computational time. It is meaningful only during
     realtime or hardware execution, and must be `None` for non-realtime batch synthetic runs.
2. Separation Margin vs. Fabricated Confidence (Audit Item #13):
   `separation_margin` represents the physical distance gap between the winning zone and runner-up.
   It is NEVER called or treated as "confidence".
3. Ground-Truth Isolation in Telemetry:
   `ground_truth_zone` and `ground_truth_motion` are nullable fields defaulting to `None`. They are
   never populated by the online pipeline and exist solely to support future offline labeled test
   harnesses (Section 23 validation).
4. Motion Gating Invariant:
   If `is_motion` is False, `zone_id` and `separation_margin` must be `None`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True, slots=True)
class EventLogRecord:
    """Immutable telemetry event record representing a classified analysis window.

    Attributes:
        run_id: Caller-supplied session/run identifier grouping events from one execution session.
        event_timestamp: UTC Unix epoch seconds (time.time()) corresponding to decision publication.
        window_start_timestamp: UTC Unix epoch seconds of earliest frame in the analysis window.
        window_end_timestamp: UTC Unix epoch seconds of most recent frame in the analysis window.
        sample_count: Number of CSI records contributing to this analysis window.
        is_motion: Instantaneous motion detection flag with hysteresis deadband smoothing.
        presence: Motion-derived presence flag sustained by configurable timeout.
        zone_id: 1-indexed room zone identifier, or None if unknown/no-motion/ambiguous.
        separation_margin: Distance gap between winning zone and runner-up (None if no motion/1 zone).
        calibration_version: Integer version of the CalibrationProfile used, or None if uncalibrated.
        aggregate_variance: Scalar energy metric summarizing RF perturbation across subcarriers.
        window_latency_ms: Config-derived physical window fill delay (window_size / sample_rate * 1000).
        pipeline_latency_ms: Measured wall-clock execution time (ms), or None for non-realtime runs.
        ground_truth_zone: Optional ground-truth zone for validation (None during normal runtime).
        ground_truth_motion: Optional ground-truth motion for validation (None during normal runtime).
    """

    run_id: str
    event_timestamp: float
    window_start_timestamp: float
    window_end_timestamp: float
    sample_count: int
    is_motion: bool
    presence: bool
    zone_id: Optional[int]
    separation_margin: Optional[float]
    calibration_version: Optional[int]
    aggregate_variance: float
    window_latency_ms: float
    pipeline_latency_ms: Optional[float] = None
    ground_truth_zone: Optional[int] = None
    ground_truth_motion: Optional[bool] = None

    def __post_init__(self) -> None:
        """Validate invariant constraints on EventLogRecord attributes."""
        # 1. run_id validation
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError(f"run_id must be a non-empty string, got {self.run_id!r}")

        # 2. Timestamps validation
        if self.event_timestamp <= 0.0:
            raise ValueError(f"event_timestamp must be positive, got {self.event_timestamp}")
        if self.window_start_timestamp <= 0.0:
            raise ValueError(
                f"window_start_timestamp must be positive, got {self.window_start_timestamp}"
            )
        if self.window_end_timestamp < self.window_start_timestamp:
            raise ValueError(
                f"window_end_timestamp ({self.window_end_timestamp}) must be >= "
                f"window_start_timestamp ({self.window_start_timestamp})"
            )

        # 3. Sample count
        if self.sample_count < 1:
            raise ValueError(f"sample_count must be >= 1, got {self.sample_count}")

        # 4. Booleans
        if not isinstance(self.is_motion, bool):
            raise ValueError(f"is_motion must be a bool, got {type(self.is_motion).__name__}")
        if not isinstance(self.presence, bool):
            raise ValueError(f"presence must be a bool, got {type(self.presence).__name__}")

        # 5. Motion gating consistency
        if not self.is_motion:
            if self.zone_id is not None:
                raise ValueError(
                    f"zone_id must be None when is_motion is False, got {self.zone_id}"
                )
            if self.separation_margin is not None:
                raise ValueError(
                    f"separation_margin must be None when is_motion is False, got {self.separation_margin}"
                )

        # 6. Zone ID
        if self.zone_id is not None:
            if not isinstance(self.zone_id, int):
                raise TypeError(f"zone_id must be an int or None, got {type(self.zone_id).__name__}")
            if self.zone_id < 1:
                raise ValueError(f"zone_id must be >= 1, got {self.zone_id}")

        # 7. Separation margin
        if self.separation_margin is not None:
            if not math.isfinite(self.separation_margin) or self.separation_margin < 0.0:
                raise ValueError(
                    f"separation_margin must be a finite non-negative number, got {self.separation_margin}"
                )

        # 8. Calibration version
        if self.calibration_version is not None:
            if self.calibration_version < 1:
                raise ValueError(
                    f"calibration_version must be >= 1, got {self.calibration_version}"
                )

        # 9. Aggregate variance
        if not math.isfinite(self.aggregate_variance) or self.aggregate_variance < 0.0:
            raise ValueError(
                f"aggregate_variance must be a finite non-negative number, got {self.aggregate_variance}"
            )

        # 10. Latencies
        if not math.isfinite(self.window_latency_ms) or self.window_latency_ms < 0.0:
            raise ValueError(
                f"window_latency_ms must be a finite non-negative number, got {self.window_latency_ms}"
            )
        if self.pipeline_latency_ms is not None:
            if not math.isfinite(self.pipeline_latency_ms) or self.pipeline_latency_ms < 0.0:
                raise ValueError(
                    f"pipeline_latency_ms must be a finite non-negative number, got {self.pipeline_latency_ms}"
                )

        # 11. Ground-truth fields (optional validation)
        if self.ground_truth_zone is not None:
            if self.ground_truth_zone < 1:
                raise ValueError(f"ground_truth_zone must be >= 1, got {self.ground_truth_zone}")
        if self.ground_truth_motion is not None:
            if not isinstance(self.ground_truth_motion, bool):
                raise ValueError(
                    f"ground_truth_motion must be a bool or None, got {type(self.ground_truth_motion).__name__}"
                )

    def to_dict(self) -> dict[str, Any]:
        """Convert record to a JSON/SQLite compatible dictionary."""
        return {
            "run_id": self.run_id,
            "event_timestamp": self.event_timestamp,
            "window_start_timestamp": self.window_start_timestamp,
            "window_end_timestamp": self.window_end_timestamp,
            "sample_count": self.sample_count,
            "is_motion": self.is_motion,
            "presence": self.presence,
            "zone_id": self.zone_id,
            "separation_margin": (
                round(self.separation_margin, 6)
                if self.separation_margin is not None
                else None
            ),
            "calibration_version": self.calibration_version,
            "aggregate_variance": round(self.aggregate_variance, 6),
            "window_latency_ms": round(self.window_latency_ms, 3),
            "pipeline_latency_ms": (
                round(self.pipeline_latency_ms, 3)
                if self.pipeline_latency_ms is not None
                else None
            ),
            "ground_truth_zone": self.ground_truth_zone,
            "ground_truth_motion": self.ground_truth_motion,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EventLogRecord:
        """Construct EventLogRecord from a dictionary."""
        zone_id = int(data["zone_id"]) if data.get("zone_id") is not None else None
        sep_margin = (
            float(data["separation_margin"])
            if data.get("separation_margin") is not None
            else None
        )
        cal_ver = (
            int(data["calibration_version"])
            if data.get("calibration_version") is not None
            else None
        )
        pipe_lat = (
            float(data["pipeline_latency_ms"])
            if data.get("pipeline_latency_ms") is not None
            else None
        )
        gt_zone = (
            int(data["ground_truth_zone"])
            if data.get("ground_truth_zone") is not None
            else None
        )
        gt_motion = (
            bool(data["ground_truth_motion"])
            if data.get("ground_truth_motion") is not None
            else None
        )

        return cls(
            run_id=str(data["run_id"]),
            event_timestamp=float(data["event_timestamp"]),
            window_start_timestamp=float(data["window_start_timestamp"]),
            window_end_timestamp=float(data["window_end_timestamp"]),
            sample_count=int(data["sample_count"]),
            is_motion=bool(data["is_motion"]),
            presence=bool(data["presence"]),
            zone_id=zone_id,
            separation_margin=sep_margin,
            calibration_version=cal_ver,
            aggregate_variance=float(data["aggregate_variance"]),
            window_latency_ms=float(data["window_latency_ms"]),
            pipeline_latency_ms=pipe_lat,
            ground_truth_zone=gt_zone,
            ground_truth_motion=gt_motion,
        )


@runtime_checkable
class DatabaseManager(Protocol):
    """Abstract protocol for interchangeable database persistence managers."""

    def connect(self) -> None:
        """Establish database connection and ensure migrations are applied."""
        ...

    def close(self) -> None:
        """Close database connection and release resources."""
        ...

    def insert_event(self, record: EventLogRecord) -> None:
        """Persist a single telemetry event record."""
        ...

    def insert_events_batch(self, records: Sequence[EventLogRecord]) -> None:
        """Persist a sequence of telemetry event records in a single batch transaction."""
        ...

    def query_by_run_id(self, run_id: str) -> list[EventLogRecord]:
        """Retrieve all event records for a specific run_id ordered by timestamp."""
        ...

    def __enter__(self) -> DatabaseManager:
        """Context manager entry."""
        ...

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        ...
