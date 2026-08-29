from __future__ import annotations

from pathlib import Path

from sqlalchemy import Column, MetaData, Table, Text
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
MANGO_FISH_STATE_METADATA = MetaData()
Table(
    "source_cursors",
    MANGO_FISH_STATE_METADATA,
    Column("source", Text, primary_key=True),
    Column("cursor", Text, nullable=False),
)


def _verify(connection: StateConnection) -> None:
    assert_schema_matches_metadata(
        connection,
        MANGO_FISH_STATE_METADATA,
        version_table=STATE_VERSION_TABLE,
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
