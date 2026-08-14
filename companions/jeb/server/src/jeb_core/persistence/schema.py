from __future__ import annotations

import sqlite3
from pathlib import Path

from lifecycle_events import create_lifecycle_event_schema
from state_schema import (
    StateConnection,
    StateEngine,
    StateSchema,
    StateStatus,
    sqlite_engine,
)

from jeb_core.domain.models import JebConfig
from jeb_core.persistence.source_registry import SOURCE_COLUMNS, SourceRegistry
from jeb_core.persistence.sqlite_state import SQLiteJebStore

STATE_VERSION_TABLE = "state_schema_revision"
STATE_MIGRATIONS = Path(__file__).resolve().parents[1] / "state_migrations"

EXPECTED_COLUMNS = {
    "batches": (
        "id",
        "source_id",
        "target_name",
        "run_id",
        "cleanup",
        "manifest_digest",
        "file_count",
        "total_bytes",
        "created_at",
        "updated_at",
    ),
    "batch_attempts": (
        "id",
        "batch_id",
        "attempt_number",
        "state",
        "target_submission_id",
        "created_at",
        "updated_at",
        "last_error",
        "emitted_error_fingerprint",
        "emitted_error_at",
    ),
    "files": (
        "batch_id",
        "source_path",
        "custody_path",
        "target_path",
        "bytes",
        "mtime_ns",
        "source_device",
        "source_inode",
        "custody_device",
        "custody_inode",
        "sha256",
        "claimed_at",
    ),
    "attempt_files": ("attempt_id", "target_path", "ready_at"),
    "target_preflight_failures": (
        "source_id",
        "state",
        "target_name",
        "input_paths_json",
        "failure_json",
        "fingerprint",
        "message",
        "file_count",
        "total_bytes",
        "first_seen_at",
        "last_seen_at",
        "updated_at",
        "resolved_at",
        "emitted_error_fingerprint",
        "emitted_error_at",
    ),
    "source_removals": (
        "challenge",
        "source_id",
        "plan_json",
        "status",
        "started_at",
        "completed_at",
        "phase",
        "last_error",
    ),
    "ingress_publications": (
        "upload_id",
        "source_id",
        "relative_path",
        "declared_bytes",
        "payload_sha256",
        "provenance_sha256",
        "status",
        "error_code",
        "error_message",
        "destination_path",
        "provenance_identity",
        "created_at",
        "updated_at",
        "completed_at",
    ),
    "service_operations": (
        "id",
        "operation",
        "state",
        "started_at",
        "completed_at",
        "source",
        "attempt_id",
        "failure",
    ),
    "sources": SOURCE_COLUMNS,
    "lifecycle_events": (
        "sequence",
        "event_id",
        "owner",
        "event_json",
        "context_json",
        "context_expires_at",
    ),
    "lifecycle_event_cursors": ("source", "cursor"),
}

EXPECTED_INDEXES = {
    "idx_jeb_batches_source_period",
    "idx_jeb_batches_source",
    "idx_jeb_batches_target",
    "idx_jeb_batches_file_count",
    "idx_jeb_batches_total_bytes",
    "idx_jeb_batch_attempts_state",
    "idx_jeb_batch_attempts_updated",
    "idx_jeb_batch_attempts_created",
    "idx_jeb_batch_attempts_state_updated",
    "idx_jeb_batch_attempts_target_submission",
    "idx_jeb_batch_attempts_batch_state",
    "idx_jeb_files_batch",
    "idx_jeb_attempt_files_attempt",
    "idx_jeb_target_preflight_failures_state",
    "idx_jeb_source_removals_source",
    "idx_jeb_ingress_publications_source_status",
    "idx_jeb_ingress_publications_status_updated",
    "idx_jeb_service_operations_started",
    "idx_jeb_service_operations_state_started",
    "ux_jeb_service_operations_running",
    "lifecycle_events_owner_sequence",
    "lifecycle_events_context_expiry",
    "lifecycle_events_owner_subject_context",
}

EXPECTED_TRIGGERS = {
    "trg_jeb_files_summary_insert",
    "trg_jeb_files_summary_delete",
    "trg_jeb_files_summary_update_same_batch",
    "trg_jeb_files_summary_update_moved_batch",
}


def _sqlite_connection(connection: StateConnection) -> sqlite3.Connection:
    raw = connection.connection.driver_connection
    if not isinstance(raw, sqlite3.Connection):
        raise RuntimeError("Jeb state requires SQLite")
    raw.row_factory = sqlite3.Row
    return raw


def _bootstrap(config: JebConfig, connection: StateConnection) -> None:
    raw = _sqlite_connection(connection)
    SQLiteJebStore(config).create_current_schema(raw)
    SourceRegistry.create_current_schema(raw)
    create_lifecycle_event_schema(raw)


def _verify(connection: StateConnection) -> None:
    rows = connection.exec_driver_sql(
        "SELECT type, name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
    )
    objects = {(str(row[0]), str(row[1])) for row in rows}
    actual_tables = {
        name
        for object_type, name in objects
        if object_type == "table" and name != STATE_VERSION_TABLE
    }
    expected_tables = set(EXPECTED_COLUMNS)
    if actual_tables != expected_tables:
        raise RuntimeError(
            "Jeb state tables do not match the current schema: "
            f"actual={sorted(actual_tables)} expected={sorted(expected_tables)}"
        )
    actual_indexes = {name for object_type, name in objects if object_type == "index"}
    if actual_indexes != EXPECTED_INDEXES:
        raise RuntimeError(
            "Jeb state indexes do not match the current schema: "
            f"actual={sorted(actual_indexes)} expected={sorted(EXPECTED_INDEXES)}"
        )
    actual_triggers = {name for object_type, name in objects if object_type == "trigger"}
    if actual_triggers != EXPECTED_TRIGGERS:
        raise RuntimeError(
            "Jeb state triggers do not match the current schema: "
            f"actual={sorted(actual_triggers)} expected={sorted(EXPECTED_TRIGGERS)}"
        )
    for table, expected in EXPECTED_COLUMNS.items():
        actual = tuple(
            str(row[1]) for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")')
        )
        if actual != expected:
            raise RuntimeError(f"Jeb state table {table} has columns {actual}, expected {expected}")


def state_schema(config: JebConfig) -> StateSchema:
    database = config.service.state_db

    def engine_factory() -> StateEngine:
        database.parent.mkdir(parents=True, exist_ok=True)
        return sqlite_engine(database)

    return StateSchema(
        name="jeb",
        engine_factory=engine_factory,
        script_location=STATE_MIGRATIONS,
        bootstrap=lambda connection: _bootstrap(config, connection),
        verify=_verify,
        is_empty=lambda: not database.exists() or database.stat().st_size == 0,
        version_table=STATE_VERSION_TABLE,
    )


def upgrade_state(config: JebConfig) -> StateStatus:
    return state_schema(config).upgrade()


def validate_state(config: JebConfig) -> StateStatus:
    return state_schema(config).validate()
