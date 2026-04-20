from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

try:
    from sqlalchemy import create_engine, event
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
except Exception as exc:  # pragma: no cover - optional dependency path
    create_engine = None
    event = None
    DeclarativeBase = object  # type: ignore[assignment]
    Session = object  # type: ignore[assignment]
    sessionmaker = None
    _SQLALCHEMY_IMPORT_ERROR: Exception | None = exc
else:
    _SQLALCHEMY_IMPORT_ERROR = None


class Base(DeclarativeBase):
    pass



def _require_sqlalchemy() -> None:
    if _SQLALCHEMY_IMPORT_ERROR is not None:
        raise RuntimeError("SQLAlchemy support requires `pip install .[db]`") from _SQLALCHEMY_IMPORT_ERROR



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
