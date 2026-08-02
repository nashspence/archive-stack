from __future__ import annotations

from pathlib import Path

from state_schema import (
    StateConnection,
    StateEngine,
    StateSchema,
    StateStatus,
    sqlite_engine,
)

import munchy_core.runtime.config as runtime_config

STATE_VERSION_TABLE = "state_schema_revision"
STATE_MIGRATIONS = Path(__file__).resolve().parents[1] / "state_migrations"

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE application_keys (
        id TEXT PRIMARY KEY,
        app TEXT NOT NULL,
        token_sha256 TEXT NOT NULL UNIQUE,
        permissions_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        revoked_at TEXT,
        last_used_at TEXT
    )
    """,
    "CREATE INDEX application_keys_app_id ON application_keys(app, id)",
    """
    CREATE TABLE states (
        kind TEXT NOT NULL,
        id TEXT NOT NULL,
        payload TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (kind, id)
    )
    """,
    "CREATE INDEX states_kind_updated_at ON states(kind, updated_at)",
    """
    CREATE INDEX riverhog_jobs_adapter_collection
    ON states(
        kind,
        json_extract(payload, '$.handoff_adapter_state.collection_id'),
        updated_at
    )
    """,
    """
    CREATE INDEX riverhog_jobs_receipt_collection
    ON states(kind, json_extract(payload, '$.handoff_receipt.external_id'), updated_at)
    """,
    """
    CREATE INDEX riverhog_jobs_progress_collection
    ON states(kind, json_extract(payload, '$.handoff_progress.external_id'), updated_at)
    """,
    """
    CREATE TABLE job_templates (
        template_id TEXT PRIMARY KEY,
        definition TEXT NOT NULL,
        resolved_job TEXT NOT NULL,
        digest TEXT NOT NULL,
        revision INTEGER NOT NULL,
        enabled INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX job_templates_enabled_id ON job_templates(enabled, template_id)",
    "CREATE INDEX job_templates_updated_id ON job_templates(updated_at, template_id)",
    """
    CREATE TABLE job_summaries (
        job_id TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        phase TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        input_upload_id TEXT NOT NULL,
        template_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        workflow_mode TEXT NOT NULL,
        handoff_destination TEXT NOT NULL,
        output_mode TEXT NOT NULL,
        profile TEXT NOT NULL,
        terminal INTEGER NOT NULL,
        cancel_requested INTEGER NOT NULL,
        storage_wait INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX job_summaries_terminal_updated
    ON job_summaries(terminal, updated_at, job_id)
    """,
    """
    CREATE INDEX job_summaries_state_updated
    ON job_summaries(state, updated_at, job_id)
    """,
    """
    CREATE INDEX job_summaries_workflow_updated
    ON job_summaries(workflow_mode, updated_at, job_id)
    """,
    """
    CREATE INDEX job_summaries_handoff_destination_updated
    ON job_summaries(handoff_destination, updated_at, job_id)
    """,
    """
    CREATE INDEX job_summaries_run_updated
    ON job_summaries(run_id, updated_at, job_id)
    """,
    "CREATE VIRTUAL TABLE job_summaries_fts USING fts5(job_id UNINDEXED, search_text)",
    """
    CREATE TABLE job_diagnostics (
        job_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        reason TEXT NOT NULL,
        path TEXT NOT NULL,
        bytes INTEGER NOT NULL CHECK(bytes >= 0),
        sha256 TEXT NOT NULL CHECK(length(sha256) = 64)
    )
    """,
    """
    CREATE INDEX job_diagnostics_created
    ON job_diagnostics(created_at, job_id)
    """,
    """
    CREATE TABLE lifecycle_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        owner TEXT NOT NULL,
        event_json TEXT NOT NULL,
        context_json TEXT,
        context_expires_at TEXT
    )
    """,
    """
    CREATE INDEX lifecycle_events_owner_sequence
    ON lifecycle_events(owner, sequence)
    """,
    """
    CREATE INDEX lifecycle_events_context_expiry
    ON lifecycle_events(context_expires_at)
    WHERE context_json IS NOT NULL
    """,
    """
    CREATE INDEX lifecycle_events_owner_subject_context
    ON lifecycle_events(owner, json_extract(event_json, '$.subject'))
    WHERE context_json IS NOT NULL AND context_expires_at IS NULL
    """,
    """
    CREATE TABLE lifecycle_event_cursors (
        source TEXT PRIMARY KEY,
        cursor TEXT NOT NULL
    )
    """,
)

EXPECTED_COLUMNS = {
    "application_keys": (
        "id",
        "app",
        "token_sha256",
        "permissions_json",
        "created_at",
        "expires_at",
        "revoked_at",
        "last_used_at",
    ),
    "states": ("kind", "id", "payload", "updated_at"),
    "job_templates": (
        "template_id",
        "definition",
        "resolved_job",
        "digest",
        "revision",
        "enabled",
        "created_at",
        "updated_at",
    ),
    "job_summaries": (
        "job_id",
        "state",
        "phase",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "input_upload_id",
        "template_id",
        "run_id",
        "workflow_mode",
        "handoff_destination",
        "output_mode",
        "profile",
        "terminal",
        "cancel_requested",
        "storage_wait",
    ),
    "job_summaries_fts": ("job_id", "search_text"),
    "job_diagnostics": ("job_id", "created_at", "reason", "path", "bytes", "sha256"),
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
    "application_keys_app_id",
    "states_kind_updated_at",
    "job_templates_enabled_id",
    "job_templates_updated_id",
    "job_summaries_terminal_updated",
    "job_summaries_state_updated",
    "job_summaries_workflow_updated",
    "job_summaries_handoff_destination_updated",
    "job_summaries_run_updated",
    "job_diagnostics_created",
    "lifecycle_events_owner_sequence",
    "lifecycle_events_context_expiry",
    "lifecycle_events_owner_subject_context",
    "riverhog_jobs_adapter_collection",
    "riverhog_jobs_progress_collection",
    "riverhog_jobs_receipt_collection",
}


def _bootstrap(connection: StateConnection) -> None:
    for statement in SCHEMA_STATEMENTS:
        connection.exec_driver_sql(statement)


def _verify(connection: StateConnection) -> None:
    rows = connection.exec_driver_sql(
        "SELECT type, name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
    )
    objects = {(str(row[0]), str(row[1])) for row in rows}
    actual_tables = {
        name
        for object_type, name in objects
        if object_type == "table"
        and not name.startswith("job_summaries_fts_")
        and name != STATE_VERSION_TABLE
    }
    expected_tables = set(EXPECTED_COLUMNS)
    if actual_tables != expected_tables:
        raise RuntimeError(
            "Munchy state tables do not match the current schema: "
            f"actual={sorted(actual_tables)} expected={sorted(expected_tables)}"
        )
    actual_indexes = {name for object_type, name in objects if object_type == "index"}
    if actual_indexes != EXPECTED_INDEXES:
        raise RuntimeError(
            "Munchy state indexes do not match the current schema: "
            f"actual={sorted(actual_indexes)} expected={sorted(EXPECTED_INDEXES)}"
        )
    for table, expected in EXPECTED_COLUMNS.items():
        actual = tuple(
            str(row[1]) for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")')
        )
        if actual != expected:
            raise RuntimeError(
                f"Munchy state table {table} has columns {actual}, expected {expected}"
            )


def state_schema(path: Path | None = None) -> StateSchema:
    database = Path(path or runtime_config.STATE_DB_PATH)

    def engine_factory() -> StateEngine:
        database.parent.mkdir(parents=True, exist_ok=True)
        return sqlite_engine(database)

    return StateSchema(
        name="munchy",
        engine_factory=engine_factory,
        script_location=STATE_MIGRATIONS,
        bootstrap=_bootstrap,
        verify=_verify,
        is_empty=lambda: not database.exists() or database.stat().st_size == 0,
        version_table=STATE_VERSION_TABLE,
    )


def upgrade_state(path: Path | None = None) -> StateStatus:
    return state_schema(path).upgrade()


def validate_state(path: Path | None = None) -> StateStatus:
    return state_schema(path).validate()
