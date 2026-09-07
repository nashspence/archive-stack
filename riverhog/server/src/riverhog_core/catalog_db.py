from __future__ import annotations

import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from state_schema import (
    StateSchema,
    StateStatus,
    assert_schema_matches_metadata,
    attach_sha256_string_constraints,
    require_postgresql_extension,
)

# The catalog database composition boundary owns registration of every current
# v1 table with the shared declarative metadata. Model modules remain acyclic.
from riverhog_core import catalog_models as _catalog_models  # noqa: E402,F401
from riverhog_core import catalog_workflow_models as _catalog_workflow_models  # noqa: E402,F401
from riverhog_core.catalog_base import Base

attach_sha256_string_constraints(Base.metadata)

SessionFactory = sessionmaker[Session]

STATE_VERSION_TABLE = "state_schema_revision"
STATE_MIGRATIONS = Path(__file__).with_name("state_migrations")
_ENGINE_FINALIZER_ATTRIBUTE = "_riverhog_engine_finalizer"


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


def _assert_schema_matches_models(bind: Any) -> None:
    assert_schema_matches_metadata(
        bind,
        Base.metadata,
        version_table=STATE_VERSION_TABLE,
    )


def _require_catalog_database_capabilities(bind: Any) -> None:
    require_postgresql_extension(
        bind,
        name="pg_trgm",
        schema="public",
        operator_classes=("gin_trgm_ops",),
    )


def _load_catalog_models() -> None:
    from riverhog_core.catalog_models import (  # noqa: PLC0415
        AppKeyAccessGrantRecord,
        AppKeyRecord,
        ArchiveCopyJobRecord,
        ArchiveCopyObjectUploadRecord,
        ArchiveCopyRetirementRecord,
        ArchiveDownloadReservationRecord,
        ArchiveDownloadUsageRecord,
        CatalogEventAccessGroupRecord,
        CatalogEventRecord,
        CatalogSyncStateRecord,
        CollectionAccessGroupMembershipRecord,
        CollectionAccessGroupRecord,
        CollectionArchiveCopyRecord,
        CollectionArchiveFileObjectRecord,
        CollectionArchiveObjectRecord,
        CollectionArchiveObjectUploadRecord,
        CollectionDeletionRecord,
        CollectionFileProvenanceRecord,
        CollectionFileRecord,
        CollectionProvenanceEntityRecord,
        CollectionProvenanceExternalStateReferenceRecord,
        CollectionProvenanceJournalRecord,
        CollectionRecord,
        CollectionUploadFileRecord,
        CollectionUploadProvenanceArchiveVolumeRecord,
        CollectionUploadProvenanceJournalRecord,
        CollectionUploadProvenanceReachabilityRecord,
        CollectionUploadProvenanceSourceRecord,
        CollectionUploadProvenanceValidationFactRecord,
        CollectionUploadRecord,
        KeyDownloadReservationRecord,
        KeyDownloadUsageRecord,
        LifecycleEventRecord,
        RetrievalCacheAccountingReconciliationRecord,
        RetrievalCacheLeaseRecord,
        RetrievalCacheObjectRecord,
        RetrievalCachePopulationRecord,
        RetrievalCacheStoreAccountingRecord,
        RetrievalJobObjectProgressRecord,
        RetrievalJobRecord,
        RetrievalPlanFileRecord,
        RetrievalPlanObjectRecord,
        RetrievalPlanPlacementRecord,
        RetrievalPlanRecord,
    )

    _ = (
        AppKeyRecord,
        AppKeyAccessGrantRecord,
        ArchiveCopyJobRecord,
        ArchiveCopyObjectUploadRecord,
        ArchiveCopyRetirementRecord,
        ArchiveDownloadReservationRecord,
        ArchiveDownloadUsageRecord,
        CatalogEventRecord,
        CatalogSyncStateRecord,
        CatalogEventAccessGroupRecord,
        CollectionAccessGroupMembershipRecord,
        CollectionAccessGroupRecord,
        CollectionArchiveCopyRecord,
        CollectionArchiveFileObjectRecord,
        CollectionArchiveObjectRecord,
        CollectionArchiveObjectUploadRecord,
        CollectionDeletionRecord,
        CollectionFileRecord,
        CollectionFileProvenanceRecord,
        CollectionRecord,
        CollectionProvenanceEntityRecord,
        CollectionProvenanceJournalRecord,
        CollectionProvenanceExternalStateReferenceRecord,
        CollectionUploadFileRecord,
        CollectionUploadProvenanceJournalRecord,
        CollectionUploadProvenanceSourceRecord,
        CollectionUploadProvenanceArchiveVolumeRecord,
        CollectionUploadProvenanceReachabilityRecord,
        CollectionUploadProvenanceValidationFactRecord,
        CollectionUploadRecord,
        LifecycleEventRecord,
        KeyDownloadReservationRecord,
        KeyDownloadUsageRecord,
        RetrievalJobRecord,
        RetrievalJobObjectProgressRecord,
        RetrievalPlanRecord,
        RetrievalPlanFileRecord,
        RetrievalPlanObjectRecord,
        RetrievalPlanPlacementRecord,
        RetrievalCacheObjectRecord,
        RetrievalCacheLeaseRecord,
        RetrievalCacheAccountingReconciliationRecord,
        RetrievalCachePopulationRecord,
        RetrievalCacheStoreAccountingRecord,
    )


def catalog_state_schema(database_url: str) -> StateSchema:
    _load_catalog_models()
    return StateSchema(
        name="riverhog catalog",
        engine_factory=lambda: create_catalog_engine(database_url),
        script_location=STATE_MIGRATIONS,
        prerequisite=_require_catalog_database_capabilities,
        verify=_assert_schema_matches_models,
        version_table=STATE_VERSION_TABLE,
    )


def initialize_db(database_url: str) -> StateStatus:
    """Explicitly create or forward-migrate the catalog to the current revision."""

    return catalog_state_schema(database_url).upgrade()


def validate_db(database_url: str) -> StateStatus:
    """Validate current catalog state without applying schema changes."""

    return catalog_state_schema(database_url).validate()


def make_session_factory(database_url: str) -> SessionFactory:
    engine = create_catalog_engine(database_url)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    finalizer = weakref.finalize(session_factory, engine.dispose)
    setattr(session_factory, _ENGINE_FINALIZER_ATTRIBUTE, finalizer)
    return session_factory


def dispose_session_factory(session_factory: SessionFactory) -> None:
    """Release pooled connections owned by a catalog session factory."""

    finalizer = getattr(session_factory, _ENGINE_FINALIZER_ATTRIBUTE, None)
    if isinstance(finalizer, weakref.finalize):
        finalizer()
        return
    bind = session_factory.kw.get("bind")
    if isinstance(bind, Engine):
        bind.dispose()


@contextmanager
def session_scope(session_factory: SessionFactory) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
