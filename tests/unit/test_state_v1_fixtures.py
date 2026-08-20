from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from mango_fish.relay import CursorState
from mango_fish.schema import state_schema as mango_fish_state_schema
from riverhog_cli.local_state import state_schema as local_state_schema

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests/fixtures/state/v1_0001"


def _restore_sqlite(fixture: Path, database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(fixture.read_text(encoding="utf-8"))


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    return connection


def test_riverhog_local_v1_fixture_reaches_head_with_selection_and_retrieval_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "riverhog-local.sqlite3"
    _restore_sqlite(FIXTURES / "riverhog-local.sqlite.sql", database)

    status = local_state_schema(database).upgrade()
    with closing(_connect(database)) as connection:
        collection = connection.execute(
            "SELECT record_etag, tags_json, remote_deleted "
            "FROM desired_collections WHERE collection_id = 1"
        ).fetchone()
        file = connection.execute(
            "SELECT path, bytes, sha256 FROM desired_files WHERE collection_id = 1"
        ).fetchone()
        retrieval = connection.execute(
            "SELECT state, files_json FROM retrieval_jobs WHERE id = 'fixture-retrieval'"
        ).fetchone()

    assert status.condition == "current"
    assert collection is not None
    assert tuple(collection) == ("b" * 64, '["fixture"]', 0)
    assert file is not None
    assert tuple(file) == ("notes/fixture.txt", 12, "a" * 64)
    assert retrieval is not None
    assert tuple(retrieval) == ("ready", '["notes/fixture.txt"]')


def test_mango_fish_v1_fixture_reaches_head_with_source_cursor(tmp_path: Path) -> None:
    database = tmp_path / "mango-fish.sqlite3"
    _restore_sqlite(FIXTURES / "mango-fish.sqlite.sql", database)

    status = mango_fish_state_schema(database).upgrade()
    cursor_state = CursorState(database)

    assert status.condition == "current"
    assert cursor_state.cursor("stove0") == "23"
