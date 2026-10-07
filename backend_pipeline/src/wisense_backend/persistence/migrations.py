"""Database schema migration mechanism for WiSense-Twin.

Provides a lightweight, deterministic migration runner tracking schema versions
in the `schema_migrations` table, satisfying Project Coding Standard #28 ("make database
migrations possible").

DESIGN PRINCIPLES:
------------------
1. Idempotence: Running `apply_migrations` against an already up-to-date database is a safe no-op.
2. Ordered Execution: Migration steps execute in strict ascending version sequence.
3. Transactional Integrity: Each migration step commits atomically alongside its version record.
4. Extensibility: Future sections can register new migrations by appending to `MIGRATIONS`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence


@dataclass(frozen=True)
class Migration:
    """Represents a discrete database migration step."""

    version: int
    description: str
    up: Callable[[sqlite3.Connection], None]


def _migration_0001_initial_schema(conn: sqlite3.Connection) -> None:
    """Migration 1: Create initial event_logs table and performance indexes."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS event_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            event_timestamp REAL NOT NULL,
            window_start_timestamp REAL NOT NULL,
            window_end_timestamp REAL NOT NULL,
            sample_count INTEGER NOT NULL,
            is_motion INTEGER NOT NULL,
            presence INTEGER NOT NULL,
            zone_id INTEGER,
            separation_margin REAL,
            calibration_version INTEGER,
            aggregate_variance REAL NOT NULL,
            window_latency_ms REAL NOT NULL,
            pipeline_latency_ms REAL,
            ground_truth_zone INTEGER,
            ground_truth_motion INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );

        CREATE INDEX IF NOT EXISTS idx_event_logs_run_id ON event_logs(run_id);
        CREATE INDEX IF NOT EXISTS idx_event_logs_timestamp ON event_logs(event_timestamp);
        """
    )


# Registry of canonical system migrations
MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        description="Initial telemetry event_logs schema with run_id and timestamp indexes",
        up=_migration_0001_initial_schema,
    ),
]


def apply_migrations(
    conn: sqlite3.Connection,
    migrations: Sequence[Migration] | None = None,
) -> int:
    """Apply any pending schema migrations to the target SQLite database.

    Args:
        conn: Open SQLite connection.
        migrations: Optional sequence of migrations to apply. If None, default `MIGRATIONS` is used.

    Returns:
        The current schema version of the database after applying pending migrations.
    """
    active_migrations = sorted(
        migrations if migrations is not None else MIGRATIONS,
        key=lambda m: m.version,
    )

    # 1. Ensure schema_migrations tracking table exists
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL,
            description TEXT NOT NULL
        );
        """
    )
    conn.commit()

    # 2. Identify already applied versions
    cursor = conn.cursor()
    cursor.execute("SELECT version FROM schema_migrations ORDER BY version ASC;")
    applied_versions = {row[0] for row in cursor.fetchall()}

    current_version = max(applied_versions, default=0)

    # 3. Apply pending migrations in sequence
    for migration in active_migrations:
        if migration.version > current_version and migration.version not in applied_versions:
            # Execute migration DDL
            migration.up(conn)

            # Record version in schema_migrations
            now_iso = datetime.now(timezone.utc).isoformat()
            cursor.execute(
                "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?);",
                (migration.version, now_iso, migration.description),
            )
            conn.commit()
            current_version = migration.version

    return current_version
