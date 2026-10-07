"""Unit tests for schema migrations and migration runner."""

import sqlite3
from pathlib import Path

import pytest

from wisense_backend.persistence.migrations import (
    MIGRATIONS,
    Migration,
    apply_migrations,
)


class TestSchemaMigrations:
    """Validate migration runner, schema version tracking, and idempotency."""

    def test_apply_migrations_fresh_database(self) -> None:
        """Verify migrations create schema_migrations and event_logs on a fresh database."""
        conn = sqlite3.connect(":memory:")
        current_ver = apply_migrations(conn)

        assert current_ver == 1

        # Check schema_migrations table contents
        cur = conn.cursor()
        cur.execute("SELECT version, description FROM schema_migrations;")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 1
        assert "Initial telemetry event_logs schema" in rows[0][1]

        # Check event_logs table exists and inspect columns
        cur.execute("PRAGMA table_info(event_logs);")
        columns = {row[1] for row in cur.fetchall()}
        expected_columns = {
            "id",
            "run_id",
            "event_timestamp",
            "window_start_timestamp",
            "window_end_timestamp",
            "sample_count",
            "is_motion",
            "presence",
            "zone_id",
            "separation_margin",
            "calibration_version",
            "aggregate_variance",
            "window_latency_ms",
            "pipeline_latency_ms",
            "ground_truth_zone",
            "ground_truth_motion",
            "created_at",
        }
        assert expected_columns.issubset(columns)
        conn.close()

    def test_migration_idempotency(self, tmp_path: Path) -> None:
        """Verify calling apply_migrations repeatedly is a safe no-op."""
        db_file = tmp_path / "test_migration_idempotent.db"
        conn = sqlite3.connect(str(db_file))

        # First application
        v1 = apply_migrations(conn)
        assert v1 == 1

        # Second application
        v2 = apply_migrations(conn)
        assert v2 == 1

        # Third application
        v3 = apply_migrations(conn)
        assert v3 == 1

        # Verify only one version 1 record exists in tracking table
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 1;")
        count = cur.fetchone()[0]
        assert count == 1
        conn.close()

    def test_forward_migration_extensibility(self) -> None:
        """Verify adding a new migration step (e.g. version 2) upgrades cleanly."""
        conn = sqlite3.connect(":memory:")

        # Apply initial version 1
        assert apply_migrations(conn) == 1

        # Define an incremental migration step
        def _migration_0002_add_metadata_table(c: sqlite3.Connection) -> None:
            c.execute(
                """
                CREATE TABLE session_metadata (
                    run_id TEXT PRIMARY KEY,
                    session_notes TEXT
                );
                """
            )

        custom_migrations = [
            MIGRATIONS[0],
            Migration(
                version=2,
                description="Add session metadata table",
                up=_migration_0002_add_metadata_table,
            ),
        ]

        # Apply multi-step migrations
        v2 = apply_migrations(conn, migrations=custom_migrations)
        assert v2 == 2

        # Verify tracking table contains both versions
        cur = conn.cursor()
        cur.execute("SELECT version, description FROM schema_migrations ORDER BY version ASC;")
        rows = cur.fetchall()
        assert len(rows) == 2
        assert rows[0][0] == 1
        assert rows[1][0] == 2
        assert rows[1][1] == "Add session metadata table"

        # Verify the new table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_metadata';")
        assert cur.fetchone() is not None

        # Re-running must be idempotent
        assert apply_migrations(conn, migrations=custom_migrations) == 2
        conn.close()
