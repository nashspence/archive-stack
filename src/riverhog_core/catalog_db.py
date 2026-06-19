from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


SCHEMA_BASELINE_VERSION = 1
SCHEMA_LATEST_VERSION = 2
_SUPPORTED_SCHEMA_VERSIONS = {SCHEMA_BASELINE_VERSION, SCHEMA_LATEST_VERSION}
_SCHEMA_V2_DROPPED_COLUMNS = {
    "glacier_usage_snapshots": (
        "estimated_billable_bytes",
        "estimated_monthly_cost_usd",
        "pricing_label",
        "glacier_storage_rate_usd_per_gib_month",
        "standard_storage_rate_usd_per_gib_month",
        "archived_metadata_bytes_per_object",
        "standard_metadata_bytes_per_object",
        "minimum_storage_duration_days",
    ),
    "glacier_recovery_sessions": ("estimate_json",),
}


def _normalize_database_url(database_url: str) -> str:
    raw = database_url.strip()
    if "://" not in raw:
        raise ValueError("database URL must be a SQLAlchemy URL")
    return raw


def create_catalog_engine(database_url: str) -> Engine:
    database_url = _normalize_database_url(database_url)
    backend = _database_url_backend(database_url)
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if backend == "sqlite" else {},
        future=True,
        pool_pre_ping=True,
    )

    if backend == "sqlite":

        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.close()

    return engine


def _database_url_backend(database_url: str) -> str:
    return make_url(database_url).get_backend_name()


def _check_database_is_baseline_compatible(engine: Engine) -> None:
    table_names = set(inspect(engine).get_table_names())
    if table_names and "schema_migrations" not in table_names:
        raise RuntimeError(
            "unsupported unversioned catalog schema detected; reset the Riverhog "
            "database before starting this greenfield build"
        )


def _apply_schema_migrations(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
        )
        applied_versions = {
            row[0] for row in conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
        }
        if applied_versions - _SUPPORTED_SCHEMA_VERSIONS:
            raise RuntimeError(
                "unsupported pre-baseline catalog schema detected; reset the Riverhog "
                "database before starting this greenfield build"
            )
        if SCHEMA_BASELINE_VERSION not in applied_versions:
            conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                {"v": SCHEMA_BASELINE_VERSION},
            )
            applied_versions.add(SCHEMA_BASELINE_VERSION)
        if SCHEMA_LATEST_VERSION not in applied_versions:
            _migrate_schema_v2(conn)
            conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                {"v": SCHEMA_LATEST_VERSION},
            )


def _migrate_schema_v2(conn: Connection) -> None:
    inspector = inspect(conn)
    table_names = set(inspector.get_table_names())
    for table_name, column_names in _SCHEMA_V2_DROPPED_COLUMNS.items():
        if table_name not in table_names:
            continue
        existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name in column_names:
            if column_name in existing_columns:
                conn.execute(
                    text(
                        "ALTER TABLE "
                        f"{_quote_identifier(conn, table_name)} "
                        "DROP COLUMN "
                        f"{_quote_identifier(conn, column_name)}"
                    )
                )


def _quote_identifier(conn: Connection, identifier: str) -> str:
    return conn.dialect.identifier_preparer.quote(identifier)


def _ensure_schema_indexes(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_collection_upload_files_collection_order "
                "ON collection_upload_files (collection_id, file_order)"
            )
        )


def initialize_db(database_url: str) -> None:
    """Create the current baseline catalog schema.

    Call this once on service startup before any other database access.
    It is safe to call multiple times; all operations are idempotent.
    """
    from riverhog_core.catalog_models import (  # noqa: PLC0415 - avoid circular import at module level
        ActivePinRecord,
        CandidateCoveredPathRecord,
        CollectionArchiveRecord,
        CollectionFileRecord,
        CollectionRecord,
        CollectionUploadFileRecord,
        CollectionUploadRecord,
        FetchEntryRecord,
        FileCopyRecord,
        FinalizedImageCollectionArtifactRecord,
        FinalizedImageCoveragePartRecord,
        FinalizedImageCoveredPathRecord,
        FinalizedImageRecord,
        GlacierRecoverySessionCollectionRecord,
        GlacierRecoverySessionImageRecord,
        GlacierRecoverySessionRecord,
        GlacierUsageSnapshotRecord,
        ImageCopyEventRecord,
        ImageCopyRecord,
        PlannedCandidateRecord,
    )

    _ = (
        ActivePinRecord,
        CandidateCoveredPathRecord,
        CollectionArchiveRecord,
        CollectionFileRecord,
        CollectionRecord,
        FetchEntryRecord,
        FileCopyRecord,
        FinalizedImageCollectionArtifactRecord,
        FinalizedImageCoveragePartRecord,
        FinalizedImageCoveredPathRecord,
        FinalizedImageRecord,
        GlacierRecoverySessionImageRecord,
        GlacierRecoverySessionCollectionRecord,
        GlacierRecoverySessionRecord,
        GlacierUsageSnapshotRecord,
        ImageCopyEventRecord,
        ImageCopyRecord,
        CollectionUploadFileRecord,
        CollectionUploadRecord,
        PlannedCandidateRecord,
    )
    engine = create_catalog_engine(database_url)
    _check_database_is_baseline_compatible(engine)
    Base.metadata.create_all(engine)
    _ensure_schema_indexes(engine)
    _apply_schema_migrations(engine)


def make_session_factory(database_url: str) -> sessionmaker[Session]:
    engine = create_catalog_engine(database_url)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
