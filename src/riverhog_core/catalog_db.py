from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, text
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
        "CREATE INDEX IF NOT EXISTS ix_fetch_selectors_target "
        "ON fetch_selectors (target, fetch_id)",
        "CREATE INDEX IF NOT EXISTS ix_archive_restores_state_created "
        "ON archive_restores (state, created_at, restore_id)",
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def initialize_db(database_url: str) -> None:
    """Create the current catalog schema."""
    from riverhog_core.catalog_models import (  # noqa: PLC0415
        ArchiveRestoreCollectionRecord,
        ArchiveRestoreRecord,
        ArchiveUsageSnapshotRecord,
        CollectionArchiveRecord,
        CollectionFileRecord,
        CollectionRecord,
        CollectionUploadFileRecord,
        CollectionUploadRecord,
        FetchRecord,
        FetchSelectorRecord,
    )

    _ = (
        ArchiveRestoreCollectionRecord,
        ArchiveRestoreRecord,
        ArchiveUsageSnapshotRecord,
        CollectionArchiveRecord,
        CollectionFileRecord,
        CollectionRecord,
        CollectionUploadFileRecord,
        CollectionUploadRecord,
        FetchRecord,
        FetchSelectorRecord,
    )
    engine = create_catalog_engine(database_url)
    Base.metadata.create_all(engine)
    _ensure_schema_indexes(engine)


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
