from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

try:
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
except Exception as exc:  # pragma: no cover - optional dependency path
    create_engine = None
    event = None
    text = None  # type: ignore[assignment]
    DeclarativeBase = object  # type: ignore[assignment]
    Session = object  # type: ignore[assignment]
    sessionmaker = None
    _SQLALCHEMY_IMPORT_ERROR: Exception | None = exc
else:
    _SQLALCHEMY_IMPORT_ERROR = None

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
]


class Base(DeclarativeBase):
    pass


def _require_sqlalchemy() -> None:
    if _SQLALCHEMY_IMPORT_ERROR is not None:
        raise RuntimeError(
            "SQLAlchemy support requires `pip install .[db]`"
        ) from _SQLALCHEMY_IMPORT_ERROR


def create_sqlite_engine(sqlite_path: str) -> Engine:
    _require_sqlalchemy()
    assert create_engine is not None
    assert event is not None
    engine = create_engine(
        f"sqlite:///{sqlite_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()

    return engine


def _table_exists(conn, table: str) -> bool:
    rows = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table},
    ).fetchall()
    return bool(rows)


def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def migrate_schema(engine: Engine) -> None:
    """Apply any pending column migrations to the catalog database.

    Each migration version is recorded in schema_migrations and runs at most once.
    """
    assert text is not None
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY)"
            )
        )
        applied = {
            row[0]
            for row in conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
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


def initialize_db(sqlite_path: str) -> None:
    """Create all catalog tables and apply any pending schema migrations.

    Call this once on service startup before any other database access.
    It is safe to call multiple times; all operations are idempotent.
    """
    from arc_core.catalog_models import (  # noqa: PLC0415 - avoid circular import at module level
        ActivePinRecord,
        CandidateCoveredPathRecord,
        CollectionFileRecord,
        CollectionRecord,
        FetchEntryRecord,
        FileCopyRecord,
        FinalizedImageCoveredPathRecord,
        FinalizedImageRecord,
        ImageCopyRecord,
        PlannedCandidateRecord,
    )

    _ = (
        ActivePinRecord,
        CandidateCoveredPathRecord,
        CollectionFileRecord,
        CollectionRecord,
        FetchEntryRecord,
        FileCopyRecord,
        FinalizedImageCoveredPathRecord,
        FinalizedImageRecord,
        ImageCopyRecord,
        PlannedCandidateRecord,
    )
    engine = create_sqlite_engine(sqlite_path)
    Base.metadata.create_all(engine)
    migrate_schema(engine)


def make_session_factory(sqlite_path: str):
    _require_sqlalchemy()
    assert sessionmaker is not None
    engine = create_sqlite_engine(sqlite_path)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@contextmanager
def session_scope(session_factory) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
