from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import CheckConstraint, UniqueConstraint, create_engine, event, inspect
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from state_schema import StateSchema, StateStatus

from riverhog_core.catalog_base import Base

STATE_VERSION_TABLE = "state_schema_revision"
STATE_MIGRATIONS = Path(__file__).with_name("state_migrations")


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


def _type_name(value: Any, bind: Connection | Engine) -> str:
    return " ".join(str(value.compile(dialect=bind.dialect)).upper().split())


def _foreign_key_signature(constraint: Any) -> tuple[tuple[str, ...], str, tuple[str, ...], str]:
    elements = tuple(constraint.elements)
    return (
        tuple(str(element.parent.name) for element in elements),
        str(elements[0].column.table.name),
        tuple(str(element.column.name) for element in elements),
        str(constraint.ondelete or "").upper(),
    )


def _inspected_foreign_key_signature(
    constraint: Mapping[str, Any],
) -> tuple[tuple[str, ...], str, tuple[str, ...], str]:
    return (
        tuple(str(name) for name in constraint["constrained_columns"]),
        str(constraint["referred_table"]),
        tuple(str(name) for name in constraint["referred_columns"]),
        str(constraint.get("options", {}).get("ondelete") or "").upper(),
    )


def _assert_schema_matches_models(bind: Connection | Engine) -> None:
    inspector = inspect(bind)
    expected = {table.name: table for table in Base.metadata.sorted_tables}
    actual_tables = set(inspector.get_table_names()) - {STATE_VERSION_TABLE}
    differences: list[str] = []
    for table_name in sorted(actual_tables - set(expected)):
        differences.append(f"unexpected table {table_name}")
    for table_name in sorted(set(expected) - actual_tables):
        differences.append(f"missing table {table_name}")
    for table_name in sorted(actual_tables & set(expected)):
        table = expected[table_name]
        expected_columns = {column.name: column for column in table.columns}
        actual_columns = {
            str(column["name"]): column for column in inspector.get_columns(table_name)
        }
        for column_name in sorted(set(actual_columns) - set(expected_columns)):
            differences.append(f"unexpected column {table_name}.{column_name}")
        for column_name in sorted(set(expected_columns) - set(actual_columns)):
            differences.append(f"missing column {table_name}.{column_name}")
        for column_name in sorted(set(expected_columns) & set(actual_columns)):
            expected_column = expected_columns[column_name]
            actual_column = actual_columns[column_name]
            expected_type = _type_name(expected_column.type, bind)
            actual_type = _type_name(actual_column["type"], bind)
            if actual_type != expected_type:
                differences.append(
                    f"column {table_name}.{column_name} has type {actual_type}, "
                    f"expected {expected_type}"
                )
            if bool(actual_column["nullable"]) != bool(expected_column.nullable):
                differences.append(
                    f"column {table_name}.{column_name} has nullable="
                    f"{bool(actual_column['nullable'])}, expected {bool(expected_column.nullable)}"
                )
            if bind.dialect.name != "sqlite":
                expected_identity = expected_column.identity is not None
                actual_identity = actual_column.get("identity") is not None
                if actual_identity != expected_identity:
                    differences.append(
                        f"column {table_name}.{column_name} has identity={actual_identity}, "
                        f"expected {expected_identity}"
                    )

        expected_primary_key = tuple(str(column.name) for column in table.primary_key.columns)
        actual_primary_key = tuple(
            str(name)
            for name in (inspector.get_pk_constraint(table_name)["constrained_columns"] or ())
        )
        if actual_primary_key != expected_primary_key:
            differences.append(
                f"table {table_name} has primary key {actual_primary_key}, "
                f"expected {expected_primary_key}"
            )

        expected_foreign_keys = {
            _foreign_key_signature(constraint) for constraint in table.foreign_key_constraints
        }
        actual_foreign_keys = {
            _inspected_foreign_key_signature(constraint)
            for constraint in inspector.get_foreign_keys(table_name)
        }
        for signature in sorted(actual_foreign_keys - expected_foreign_keys):
            differences.append(f"unexpected foreign key on {table_name}: {signature}")
        for signature in sorted(expected_foreign_keys - actual_foreign_keys):
            differences.append(f"missing foreign key on {table_name}: {signature}")

        expected_unique_constraints = {
            tuple(str(column.name) for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        actual_unique_constraints = {
            tuple(str(name) for name in constraint["column_names"])
            for constraint in inspector.get_unique_constraints(table_name)
        }
        for columns in sorted(actual_unique_constraints - expected_unique_constraints):
            differences.append(f"unexpected unique constraint on {table_name}: {columns}")
        for columns in sorted(expected_unique_constraints - actual_unique_constraints):
            differences.append(f"missing unique constraint on {table_name}: {columns}")

        expected_indexes = {
            str(index.name): (
                tuple(str(column.name) for column in index.columns),
                bool(index.unique),
            )
            for index in table.indexes
        }
        actual_indexes = {
            str(index["name"]): (
                tuple(str(name) for name in index["column_names"]),
                bool(index["unique"]),
            )
            for index in inspector.get_indexes(table_name)
            if not index.get("duplicates_constraint")
        }
        for index_name in sorted(set(actual_indexes) - set(expected_indexes)):
            differences.append(f"unexpected index {table_name}.{index_name}")
        for index_name in sorted(set(expected_indexes) - set(actual_indexes)):
            differences.append(f"missing index {table_name}.{index_name}")
        for index_name in sorted(set(expected_indexes) & set(actual_indexes)):
            if actual_indexes[index_name] != expected_indexes[index_name]:
                differences.append(
                    f"index {table_name}.{index_name} is {actual_indexes[index_name]}, "
                    f"expected {expected_indexes[index_name]}"
                )

        expected_checks = {
            str(constraint.name)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name is not None
        }
        actual_checks = {
            str(constraint["name"])
            for constraint in inspector.get_check_constraints(table_name)
            if constraint["name"] is not None
        }
        for check_name in sorted(actual_checks - expected_checks):
            differences.append(f"unexpected check constraint {table_name}.{check_name}")
        for check_name in sorted(expected_checks - actual_checks):
            differences.append(f"missing check constraint {table_name}.{check_name}")
    if differences:
        raise RuntimeError(
            "catalog schema does not match current models: " + "; ".join(differences)
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
        CatalogEventRecord,
        CatalogEventTagRecord,
        CollectionArchiveAttestationRecord,
        CollectionArchiveCopyRecord,
        CollectionArchiveFileObjectRecord,
        CollectionArchiveObjectRecord,
        CollectionArchiveObjectUploadRecord,
        CollectionDeletionRecord,
        CollectionFileProvenanceRecord,
        CollectionFileRecord,
        CollectionMetadataPublicationRecord,
        CollectionProofMaturationRecord,
        CollectionProvenanceEntityRecord,
        CollectionProvenanceJournalRecord,
        CollectionRecord,
        CollectionTagRecord,
        CollectionUploadFileRecord,
        CollectionUploadProvenanceJournalRecord,
        CollectionUploadRecord,
        CollectionUploadTagRecord,
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
        ArchiveCopyObjectUploadRecord,
        ArchiveCopyRetirementRecord,
        ArchiveDownloadReservationRecord,
        ArchiveDownloadUsageRecord,
        CatalogEventRecord,
        CatalogEventTagRecord,
        CollectionArchiveCopyRecord,
        CollectionArchiveAttestationRecord,
        CollectionArchiveFileObjectRecord,
        CollectionArchiveObjectRecord,
        CollectionArchiveObjectUploadRecord,
        CollectionDeletionRecord,
        CollectionFileRecord,
        CollectionFileProvenanceRecord,
        CollectionRecord,
        CollectionProvenanceEntityRecord,
        CollectionProvenanceJournalRecord,
        CollectionMetadataPublicationRecord,
        CollectionProofMaturationRecord,
        CollectionTagRecord,
        CollectionUploadFileRecord,
        CollectionUploadProvenanceJournalRecord,
        CollectionUploadRecord,
        CollectionUploadTagRecord,
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


def catalog_state_schema(database_url: str) -> StateSchema:
    _load_catalog_models()
    return StateSchema(
        name="riverhog catalog",
        engine_factory=lambda: create_catalog_engine(database_url),
        script_location=STATE_MIGRATIONS,
        bootstrap=lambda connection: Base.metadata.create_all(connection),
        verify=_assert_schema_matches_models,
        version_table=STATE_VERSION_TABLE,
    )


def initialize_db(database_url: str) -> StateStatus:
    """Explicitly create or forward-migrate the catalog to the current revision."""

    return catalog_state_schema(database_url).upgrade()


def validate_db(database_url: str) -> StateStatus:
    """Validate current catalog state without applying schema changes."""

    return catalog_state_schema(database_url).validate()


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
