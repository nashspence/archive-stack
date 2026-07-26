from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def _normalize_database_url(database_url: str) -> str:
    raw = database_url.strip()
    if "://" not in raw:
        raise ValueError("database URL must be a SQLAlchemy URL")
    return raw


def create_catalog_engine(database_url: str) -> Engine:
    database_url = _normalize_database_url(database_url)
    backend = make_url(database_url).get_backend_name()
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


def _ensure_schema_indexes(engine: Engine) -> None:
    statements = (
        "CREATE INDEX IF NOT EXISTS idx_collection_upload_files_collection_order "
        "ON collection_upload_files (collection_id, file_order)",
        "CREATE INDEX IF NOT EXISTS ix_retrieval_jobs_due "
        "ON retrieval_jobs (state, next_poll_at, id)",
        "CREATE INDEX IF NOT EXISTS ix_retrieval_cache_leases_expiry "
        "ON retrieval_cache_leases (expires_at, owner)",
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _assert_schema_matches_models(engine: Engine) -> None:
    inspector = inspect(engine)
    expected = {
        table.name: {column.name for column in table.columns}
        for table in Base.metadata.sorted_tables
    }
    actual_tables = set(inspector.get_table_names())
    differences: list[str] = []
    for table_name in sorted(actual_tables - set(expected)):
        differences.append(f"unexpected table {table_name}")
    for table_name in sorted(set(expected) - actual_tables):
        differences.append(f"missing table {table_name}")
    for table_name in sorted(actual_tables & set(expected)):
        actual_columns = {str(column["name"]) for column in inspector.get_columns(table_name)}
        for column_name in sorted(actual_columns - expected[table_name]):
            differences.append(f"unexpected column {table_name}.{column_name}")
        for column_name in sorted(expected[table_name] - actual_columns):
            differences.append(f"missing column {table_name}.{column_name}")
    if differences:
        raise RuntimeError(
            "catalog schema does not match current models: " + "; ".join(differences)
        )


def initialize_db(database_url: str) -> None:
    """Create the current catalog schema."""
    from riverhog_core.catalog_models import (  # noqa: PLC0415
        AppKeyAccessGrantRecord,
        AppKeyRecord,
        ArchiveCopyJobRecord,
        ArchiveCopyRetirementRecord,
        ArchiveDownloadReservationRecord,
        ArchiveDownloadUsageRecord,
        ArchiveUsageSnapshotRecord,
        CatalogEventRecord,
        CollectionArchiveAttestationRecord,
        CollectionArchiveCopyRecord,
        CollectionArchiveFileObjectRecord,
        CollectionArchiveObjectRecord,
        CollectionArchiveObjectUploadRecord,
        CollectionDeletionRecord,
        CollectionFileRecord,
        CollectionMetadataPublicationRecord,
        CollectionProofMaturationRecord,
        CollectionRecord,
        CollectionTagRecord,
        CollectionUploadFileRecord,
        CollectionUploadRecord,
        IngressCleanupRecord,
        KeyDownloadReservationRecord,
        KeyDownloadUsageRecord,
        LifecycleEventRecord,
        RetrievalCacheLeaseRecord,
        RetrievalCacheObjectRecord,
        RetrievalJobFileRecord,
        RetrievalJobObjectRecord,
        RetrievalJobRecord,
        TagRecord,
    )

    _ = (
        AppKeyRecord,
        AppKeyAccessGrantRecord,
        ArchiveCopyJobRecord,
        ArchiveCopyRetirementRecord,
        ArchiveDownloadReservationRecord,
        ArchiveDownloadUsageRecord,
        ArchiveUsageSnapshotRecord,
        CatalogEventRecord,
        CollectionArchiveCopyRecord,
        CollectionArchiveAttestationRecord,
        CollectionArchiveFileObjectRecord,
        CollectionArchiveObjectRecord,
        CollectionArchiveObjectUploadRecord,
        CollectionDeletionRecord,
        CollectionFileRecord,
        CollectionRecord,
        CollectionMetadataPublicationRecord,
        CollectionProofMaturationRecord,
        CollectionTagRecord,
        CollectionUploadFileRecord,
        CollectionUploadRecord,
        IngressCleanupRecord,
        LifecycleEventRecord,
        KeyDownloadReservationRecord,
        KeyDownloadUsageRecord,
        RetrievalJobRecord,
        RetrievalJobFileRecord,
        RetrievalJobObjectRecord,
        RetrievalCacheObjectRecord,
        RetrievalCacheLeaseRecord,
        TagRecord,
    )
    engine = create_catalog_engine(database_url)
    Base.metadata.create_all(engine)
    _ensure_schema_indexes(engine)
    _assert_schema_matches_models(engine)


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
