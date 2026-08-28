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


def _verify(connection: StateConnection) -> None:
    tables = {
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        if str(row[0]) != STATE_VERSION_TABLE
    }
    if tables != {"source_cursors"}:
        raise RuntimeError(f"Mango Fish state tables do not match the current schema: {tables}")
    columns = tuple(
        str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(source_cursors)")
    )
    if columns != ("source", "cursor"):
        raise RuntimeError(
            f"Mango Fish source_cursors has columns {columns}, expected ('source', 'cursor')"
        )


def state_schema(database: Path) -> StateSchema:
    path = Path(database)

    def engine_factory() -> StateEngine:
        path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite_engine(path)

    return StateSchema(
        name="mango-fish",
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
