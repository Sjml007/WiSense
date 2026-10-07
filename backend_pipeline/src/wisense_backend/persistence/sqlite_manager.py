"""SQLite database persistence manager implementation for WiSense-Twin.

Manages SQLite connection lifecycle, schema migrations, and high-performance parameterized
event insertion and retrieval.

DESIGN & SECURITY CONSTRAINTS:
------------------------------
1. 100% Parameterized SQL: Zero string formatting or concatenation in SQL execution,
   guaranteeing total immunity to SQL injection.
2. WAL & Concurrency: Configures Write-Ahead Logging (WAL) and synchronous=NORMAL for
   robust concurrent read/write throughput without blocking writers.
3. Automatic Migration Execution: Migrations run automatically on `connect()`, guaranteeing
   the database schema is fully aligned before any transaction executes.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from wisense_backend.contracts.database import DatabaseManager, EventLogRecord
from wisense_backend.persistence.migrations import apply_migrations


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """Configuration parameters for SQLite database connection.

    Attributes:
        database_path: File path for SQLite database (or ':memory:' for tests).
            Mirrors the `database_path` field in AppConfig.
        timeout_seconds: SQLite lock acquisition timeout in seconds.
        wal_mode: Whether to enable Write-Ahead Logging (WAL) mode for concurrency.
    """

    database_path: str = "data/wisense.db"
    timeout_seconds: float = 10.0
    wal_mode: bool = True

    def __post_init__(self) -> None:
        """Validate invariant constraints on configuration."""
        if not isinstance(self.database_path, str) or not self.database_path.strip():
            raise ValueError(
                f"database_path must be a non-empty string, got {self.database_path!r}"
            )
        if self.timeout_seconds <= 0.0:
            raise ValueError(
                f"timeout_seconds must be positive, got {self.timeout_seconds}"
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DatabaseConfig:
        """Construct DatabaseConfig from a dictionary."""
        return cls(
            database_path=str(data.get("database_path", "data/wisense.db")),
            timeout_seconds=float(data.get("timeout_seconds", 10.0)),
            wal_mode=bool(data.get("wal_mode", True)),
        )

    @classmethod
    def from_json_file(cls, file_path: str | Path) -> DatabaseConfig:
        """Load DatabaseConfig from a JSON file."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Database configuration not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)


_INSERT_EVENT_SQL = """
INSERT INTO event_logs (
    run_id, event_timestamp, window_start_timestamp, window_end_timestamp,
    sample_count, is_motion, presence, zone_id, separation_margin,
    calibration_version, aggregate_variance, window_latency_ms,
    pipeline_latency_ms, ground_truth_zone, ground_truth_motion
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""


def _record_to_params(r: EventLogRecord) -> tuple[Any, ...]:
    """Convert an EventLogRecord to a tuple of parameters matching _INSERT_EVENT_SQL."""
    return (
        r.run_id,
        r.event_timestamp,
        r.window_start_timestamp,
        r.window_end_timestamp,
        r.sample_count,
        1 if r.is_motion else 0,
        1 if r.presence else 0,
        r.zone_id,
        r.separation_margin,
        r.calibration_version,
        r.aggregate_variance,
        r.window_latency_ms,
        r.pipeline_latency_ms,
        r.ground_truth_zone,
        1 if r.ground_truth_motion is True else (0 if r.ground_truth_motion is False else None),
    )


class SQLiteDatabaseManager:
    """SQLite implementation of the DatabaseManager protocol."""

    def __init__(
        self,
        config: Optional[DatabaseConfig | str | Path] = None,
    ) -> None:
        """Initialize manager with configuration or file path.

        Args:
            config: Optional DatabaseConfig instance, or a path string / Path object.
        """
        if config is None:
            self._config = DatabaseConfig()
        elif isinstance(config, (str, Path)):
            self._config = DatabaseConfig(database_path=str(config))
        elif isinstance(config, DatabaseConfig):
            self._config = config
        else:
            raise TypeError(
                f"config must be DatabaseConfig, str, Path, or None; got {type(config).__name__}"
            )

        self._conn: Optional[sqlite3.Connection] = None

    @property
    def config(self) -> DatabaseConfig:
        """The active database configuration."""
        return self._config

    @property
    def is_connected(self) -> bool:
        """Whether an active database connection is open."""
        return self._conn is not None

    def connect(self) -> None:
        """Open database connection, configure pragmas, and apply migrations."""
        if self._conn is not None:
            return  # Already connected (idempotent)

        db_path = self._config.database_path
        if db_path != ":memory:":
            path_obj = Path(db_path)
            path_obj.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            db_path,
            timeout=self._config.timeout_seconds,
        )
        self._conn.row_factory = sqlite3.Row

        # Configure connection PRAGMAs
        self._conn.execute("PRAGMA foreign_keys = ON;")
        if self._config.wal_mode and db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA synchronous = NORMAL;")

        # Apply schema migrations automatically on connect
        apply_migrations(self._conn)

    def close(self) -> None:
        """Close connection and release database file locks."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def insert_event(self, record: EventLogRecord) -> None:
        """Persist a single telemetry event record using parameterized SQL.

        Args:
            record: The `EventLogRecord` to persist.

        Raises:
            RuntimeError: If called before `connect()`.
        """
        if self._conn is None:
            raise RuntimeError("Database connection is not open. Call connect() first.")

        params = _record_to_params(record)
        with self._conn:
            self._conn.execute(_INSERT_EVENT_SQL, params)

    def insert_events_batch(self, records: Sequence[EventLogRecord]) -> None:
        """Persist a sequence of telemetry event records in a single batch transaction.

        Args:
            records: Sequence of `EventLogRecord` instances to persist.

        Raises:
            RuntimeError: If called before `connect()`.
        """
        if self._conn is None:
            raise RuntimeError("Database connection is not open. Call connect() first.")
        if not records:
            return

        params_list = [_record_to_params(r) for r in records]
        with self._conn:
            self._conn.executemany(_INSERT_EVENT_SQL, params_list)

    def query_by_run_id(self, run_id: str) -> list[EventLogRecord]:
        """Retrieve all event records for a specific run_id ordered by timestamp.

        Args:
            run_id: Session/run identifier to query.

        Returns:
            List of `EventLogRecord` instances.

        Raises:
            RuntimeError: If called before `connect()`.
        """
        if self._conn is None:
            raise RuntimeError("Database connection is not open. Call connect() first.")

        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT
                run_id, event_timestamp, window_start_timestamp, window_end_timestamp,
                sample_count, is_motion, presence, zone_id, separation_margin,
                calibration_version, aggregate_variance, window_latency_ms,
                pipeline_latency_ms, ground_truth_zone, ground_truth_motion
            FROM event_logs
            WHERE run_id = ?
            ORDER BY event_timestamp ASC, id ASC;
            """,
            (run_id,),
        )
        rows = cursor.fetchall()

        results: list[EventLogRecord] = []
        for row in rows:
            gt_motion_val = row["ground_truth_motion"]
            gt_motion = (
                bool(gt_motion_val) if gt_motion_val is not None else None
            )

            record = EventLogRecord(
                run_id=row["run_id"],
                event_timestamp=float(row["event_timestamp"]),
                window_start_timestamp=float(row["window_start_timestamp"]),
                window_end_timestamp=float(row["window_end_timestamp"]),
                sample_count=int(row["sample_count"]),
                is_motion=bool(row["is_motion"]),
                presence=bool(row["presence"]),
                zone_id=row["zone_id"],
                separation_margin=row["separation_margin"],
                calibration_version=row["calibration_version"],
                aggregate_variance=float(row["aggregate_variance"]),
                window_latency_ms=float(row["window_latency_ms"]),
                pipeline_latency_ms=row["pipeline_latency_ms"],
                ground_truth_zone=row["ground_truth_zone"],
                ground_truth_motion=gt_motion,
            )
            results.append(record)

        return results

    def __enter__(self) -> SQLiteDatabaseManager:
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()
