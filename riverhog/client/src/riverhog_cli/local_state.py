from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from state_schema import (
    StateConnection,
    StateEngine,
    StateSchema,
    StateStatus,
    assert_schema_matches_metadata,
    sqlite_engine,
)

STATE_VERSION_TABLE = "state_schema_revision"
STATE_MIGRATIONS = Path(__file__).with_name("state_migrations")

LOCAL_STATE_METADATA = MetaData()
Table(
    "settings",
    LOCAL_STATE_METADATA,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)
Table(
    "desired_collections",
    LOCAL_STATE_METADATA,
    Column("collection_id", Integer, primary_key=True),
    Column("record_etag", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("tags_json", Text, nullable=False),
    Column("remote_deleted", Integer, nullable=False, server_default=text("0")),
    CheckConstraint("collection_id > 0", name="ck_desired_collections_id"),
    CheckConstraint(
        "length(record_etag) = 64 AND record_etag = lower(record_etag) "
        "AND record_etag NOT GLOB '*[^0-9a-f]*'",
        name="ck_desired_collections_etag",
    ),
    CheckConstraint("json_valid(tags_json)", name="ck_desired_collections_tags_json"),
    CheckConstraint("remote_deleted IN (0, 1)", name="ck_desired_collections_remote_deleted"),
)
Table(
    "desired_files",
    LOCAL_STATE_METADATA,
    Column(
        "collection_id",
        Integer,
        ForeignKey("desired_collections.collection_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("path", Text, primary_key=True),
    Column("bytes", Integer, nullable=False),
    Column("sha256", Text, nullable=False),
    CheckConstraint("bytes >= 0", name="ck_desired_files_bytes"),
    CheckConstraint(
        "length(sha256) = 64 AND sha256 = lower(sha256) AND sha256 NOT GLOB '*[^0-9a-f]*'",
        name="ck_desired_files_sha256",
    ),
)
Table(
    "retrieval_jobs",
    LOCAL_STATE_METADATA,
    Column("id", Text, primary_key=True),
    Column("state", Text, nullable=False),
    Column("updated_at", Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    CheckConstraint(
        "state IN ('requested', 'ready', 'completed', 'expired', 'failed', 'canceled')",
        name="ck_retrieval_jobs_state",
    ),
)
Table(
    "retrieval_job_files",
    LOCAL_STATE_METADATA,
    Column(
        "retrieval_job_id",
        Text,
        ForeignKey("retrieval_jobs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("ordinal", Integer, primary_key=True),
    Column("collection_id", Integer, nullable=False),
    Column("path", Text, nullable=False),
    Column("bytes", Integer, nullable=False),
    Column("sha256", Text, nullable=False),
    CheckConstraint("ordinal >= 0", name="ck_retrieval_job_files_ordinal"),
    CheckConstraint("collection_id > 0", name="ck_retrieval_job_files_collection"),
    CheckConstraint("bytes >= 0", name="ck_retrieval_job_files_bytes"),
    CheckConstraint(
        "length(sha256) = 64 AND sha256 = lower(sha256) AND sha256 NOT GLOB '*[^0-9a-f]*'",
        name="ck_retrieval_job_files_sha256",
    ),
    UniqueConstraint(
        "retrieval_job_id",
        "collection_id",
        "path",
        name="uq_retrieval_job_files_artifact",
    ),
)


def _verify(connection: StateConnection) -> None:
    assert_schema_matches_metadata(
        connection,
        LOCAL_STATE_METADATA,
        version_table=STATE_VERSION_TABLE,
    )


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
