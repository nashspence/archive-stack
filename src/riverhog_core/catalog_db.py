from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


# Each entry is a list of (table, column, sql_type) tuples for columns to add if missing.
# New tables are handled by create_all; only column additions need explicit migration.
_COLUMN_MIGRATIONS: list[list[tuple[str, str, str]]] = [
    # version 1
    [
        ("file_copies", "disc_path", "TEXT"),
        ("file_copies", "enc_json", "TEXT"),
        ("file_copies", "part_index", "INTEGER"),
        ("file_copies", "part_count", "INTEGER"),
        ("file_copies", "part_bytes", "INTEGER"),
        ("file_copies", "part_sha256", "TEXT"),
    ],
    # version 2
    [
        ("fetch_entries", "tus_url", "TEXT"),
    ],
    # version 3
    [
        ("collections", "ingest_source", "TEXT"),
    ],
    # version 4
    [
        ("finalized_images", "required_copy_count", "INTEGER"),
        ("image_copies", "state", "TEXT"),
    ],
    # version 5
    [
        ("image_copies", "label_text", "TEXT"),
        ("image_copies", "verification_state", "TEXT"),
        ("image_copies", "location", "TEXT"),
    ],
    # version 6
    [
        ("finalized_image_coverage_parts", "object_path", "TEXT"),
        ("finalized_image_coverage_parts", "sidecar_path", "TEXT"),
    ],
    # version 7
    [
        ("glacier_recovery_sessions", "restore_next_poll_at", "TEXT"),
    ],
    # version 8
    [
        ("collection_uploads", "state", "TEXT"),
        ("collection_uploads", "archive_attempt_count", "INTEGER"),
        ("collection_uploads", "archive_next_attempt_at", "TEXT"),
        ("collection_uploads", "archive_last_attempt_at", "TEXT"),
        ("collection_uploads", "archive_failure", "TEXT"),
        ("glacier_recovery_sessions", "type", "TEXT"),
    ],
    # version 9
    [
        ("glacier_recovery_sessions", "archive_verification_state", "TEXT"),
        ("glacier_recovery_sessions", "extraction_state", "TEXT"),
        ("glacier_recovery_sessions", "materialization_state", "TEXT"),
    ],
]


def database_url_from_sqlite_path(sqlite_path: str | Path) -> str:
    path = Path(sqlite_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"


def normalize_database_url(database_url_or_path: str | Path) -> str:
    raw = str(database_url_or_path).strip()
    if "://" in raw:
        if _database_url_backend(raw) == "sqlite":
            _ensure_sqlite_parent(raw)
        return raw
    return database_url_from_sqlite_path(raw)


def create_catalog_engine(database_url_or_path: str | Path) -> Engine:
    database_url = normalize_database_url(database_url_or_path)
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


def create_sqlite_engine(sqlite_path: str) -> Engine:
    return create_catalog_engine(database_url_from_sqlite_path(sqlite_path))


def _database_url_backend(database_url: str) -> str:
    return make_url(database_url).get_backend_name()


def _ensure_sqlite_parent(database_url: str) -> None:
    url = make_url(database_url)
    database = url.database
    if database is None or database in {"", ":memory:"}:
        return
    Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _table_exists(conn: Connection, table: str) -> bool:
    return inspect(conn).has_table(table)


def _column_exists(conn: Connection, table: str, column: str) -> bool:
    return any(item["name"] == column for item in inspect(conn).get_columns(table))


def migrate_schema(engine: Engine) -> None:
    """Apply any pending column migrations to the catalog database.

    Each migration version is recorded in schema_migrations and runs at most once.
    """
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
        )
        applied = {
            row[0] for row in conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
        }
        for version, columns in enumerate(_COLUMN_MIGRATIONS, start=1):
            if version in applied:
                continue
            for table, column, col_type in columns:
                if not _table_exists(conn, table):
                    continue
                if _column_exists(conn, table, column):
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
            conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"), {"v": version}
            )


def initialize_db(database_url_or_path: str | Path) -> None:
    """Create all catalog tables and apply any pending schema migrations.

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
    database_url = normalize_database_url(database_url_or_path)
    engine = create_catalog_engine(database_url)
    Base.metadata.create_all(engine)
    migrate_schema(engine)
    _backfill_finalized_image_manifest_topology(database_url)


def _backfill_finalized_image_manifest_topology(database_url: str) -> None:
    from riverhog_core.catalog_models import (  # noqa: PLC0415
        FinalizedImageCollectionArtifactRecord,
        FinalizedImageCoveragePartRecord,
        FinalizedImageRecord,
    )
    from riverhog_core.finalized_image_coverage import (  # noqa: PLC0415
        read_finalized_image_collection_artifacts,
        read_finalized_image_coverage_parts,
    )

    session_factory = make_session_factory(database_url)
    with session_scope(session_factory) as session:
        images = session.query(FinalizedImageRecord).all()
        for image in images:
            try:
                collection_artifacts = read_finalized_image_collection_artifacts(image.image_root)
                coverage_parts = read_finalized_image_coverage_parts(image.image_root)
            except Exception:
                continue
            existing_collection_artifacts = {
                record.collection_id: record
                for record in session.query(FinalizedImageCollectionArtifactRecord).filter_by(
                    image_id=image.image_id
                )
            }
            for artifact in collection_artifacts:
                artifact_row = existing_collection_artifacts.get(artifact.collection_id)
                if artifact_row is None:
                    session.add(
                        FinalizedImageCollectionArtifactRecord(
                            image_id=image.image_id,
                            collection_id=artifact.collection_id,
                            manifest_path=artifact.manifest_path,
                            proof_path=artifact.proof_path,
                        )
                    )
                    continue
                artifact_row.manifest_path = artifact.manifest_path
                artifact_row.proof_path = artifact.proof_path

            existing_parts = {
                (record.collection_id, record.path, record.part_index): record
                for record in session.query(FinalizedImageCoveragePartRecord).filter_by(
                    image_id=image.image_id
                )
            }
            for part in coverage_parts:
                part_row = existing_parts.get((part.collection_id, part.path, part.part_index))
                if part_row is None:
                    session.add(
                        FinalizedImageCoveragePartRecord(
                            image_id=image.image_id,
                            collection_id=part.collection_id,
                            path=part.path,
                            part_index=part.part_index,
                            part_count=part.part_count,
                            object_path=part.object_path,
                            sidecar_path=part.sidecar_path,
                        )
                    )
                    continue
                part_row.part_count = part.part_count
                part_row.object_path = part.object_path
                part_row.sidecar_path = part.sidecar_path


def make_session_factory(database_url_or_path: str | Path) -> sessionmaker[Session]:
    engine = create_catalog_engine(database_url_or_path)
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
