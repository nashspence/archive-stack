from __future__ import annotations

from pathlib import Path

from state_schema import (
    StateConnection,
    StateEngine,
    StateSchema,
    StateStatus,
    sqlite_engine,
)

STATE_VERSION_TABLE = "state_schema_revision"
STATE_MIGRATIONS = Path(__file__).with_name("state_migrations")

SCHEMA_STATEMENTS = (
    "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """
    CREATE TABLE desired_collections (
        collection_id INTEGER PRIMARY KEY,
        record_etag TEXT NOT NULL,
        created_at TEXT NOT NULL,
        tags_json TEXT NOT NULL,
        remote_deleted INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE desired_files (
        collection_id INTEGER NOT NULL,
        path TEXT NOT NULL,
        bytes INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        PRIMARY KEY (collection_id, path),
        FOREIGN KEY (collection_id) REFERENCES desired_collections(collection_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE retrieval_jobs (
        id TEXT PRIMARY KEY,
        state TEXT NOT NULL CHECK (
            state IN ('requested', 'ready', 'completed', 'expired', 'failed', 'canceled')
        ),
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE retrieval_job_files (
        retrieval_job_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        collection_id INTEGER NOT NULL CHECK (collection_id > 0),
        path TEXT NOT NULL,
        bytes INTEGER NOT NULL CHECK (bytes >= 0),
        sha256 TEXT NOT NULL CHECK (
            length(sha256) = 64 AND sha256 = lower(sha256) AND sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        PRIMARY KEY (retrieval_job_id, ordinal),
        UNIQUE (retrieval_job_id, collection_id, path),
        FOREIGN KEY (retrieval_job_id) REFERENCES retrieval_jobs(id)
            ON DELETE CASCADE
    )
    """,
)

EXPECTED_COLUMNS = {
    "settings": ("key", "value"),
    "desired_collections": (
        "collection_id",
        "record_etag",
        "created_at",
        "tags_json",
        "remote_deleted",
    ),
    "desired_files": ("collection_id", "path", "bytes", "sha256"),
    "retrieval_jobs": ("id", "state", "updated_at"),
    "retrieval_job_files": (
        "retrieval_job_id",
        "ordinal",
        "collection_id",
        "path",
        "bytes",
        "sha256",
    ),
}


def _verify(connection: StateConnection) -> None:
    actual_tables = {
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        if str(row[0]) != STATE_VERSION_TABLE
    }
    if actual_tables != set(EXPECTED_COLUMNS):
        raise RuntimeError(
            "Riverhog local-state tables do not match the current schema: "
            f"actual={sorted(actual_tables)} expected={sorted(EXPECTED_COLUMNS)}"
        )
    for table, expected in EXPECTED_COLUMNS.items():
        actual = tuple(
            str(row[1]) for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")')
        )
        if actual != expected:
            raise RuntimeError(
                f"Riverhog local-state table {table} has columns {actual}, expected {expected}"
            )
    expected_foreign_keys = {
        "desired_files": (
            "desired_collections",
            "collection_id",
            "collection_id",
            "NO ACTION",
            "CASCADE",
        ),
        "retrieval_job_files": (
            "retrieval_jobs",
            "retrieval_job_id",
            "id",
            "NO ACTION",
            "CASCADE",
        ),
    }
    for table, expected in expected_foreign_keys.items():
        foreign_keys = list(connection.exec_driver_sql(f'PRAGMA foreign_key_list("{table}")'))
        if (
            len(foreign_keys) != 1
            or tuple(str(value) for value in foreign_keys[0][2:7]) != expected
        ):
            raise RuntimeError(f"Riverhog local-state {table} ownership constraint is invalid")


def state_schema(database: Path) -> StateSchema:
    path = Path(database)

    def engine_factory() -> StateEngine:
        path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite_engine(path)

    return StateSchema(
        name="riverhog local",
        engine_factory=engine_factory,
        script_location=STATE_MIGRATIONS,
        verify=_verify,
        is_empty=lambda: not path.exists() or path.stat().st_size == 0,
        version_table=STATE_VERSION_TABLE,
    )


def upgrade_state(database: Path) -> StateStatus:
    return state_schema(database).upgrade()


def validate_state(database: Path) -> StateStatus:
    return state_schema(database).validate()
