from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from jeb_api.composition import config_from_env, create_services
from jeb_core.persistence.schema import state_schema as jeb_state_schema
from lifecycle_events import SQLiteEventCursorStore, SQLiteLifecycleEventLog
from mango_fish.relay import CursorState
from mango_fish.schema import state_schema as mango_fish_state_schema
from munchy_core.persistence.application_keys import SQLiteApplicationKeyStore
from munchy_core.persistence.schema import state_schema as munchy_state_schema
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


def test_munchy_v1_fixture_reaches_head_with_authorization_and_cursors(
    tmp_path: Path,
) -> None:
    database = tmp_path / "munchy.sqlite3"
    _restore_sqlite(FIXTURES / "munchy.sqlite.sql", database)

    status = munchy_state_schema(database).upgrade()
    key_store = SQLiteApplicationKeyStore(lambda: _connect(database))
    principal = key_store.authenticate("fixture-v1-munchy-token")
    event_log = SQLiteLifecycleEventLog(lambda: _connect(database))
    cursors = SQLiteEventCursorStore(lambda: _connect(database))

    assert status.condition == "current"
    assert principal is not None
    assert principal.app == "fixture-client"
    assert principal.allows("submissions:manage")
    assert cursors.cursor("riverhog") == "41"
    assert [event.id for event in event_log.page(after=None, limit=100).events] == [
        "munchy-v1-event"
    ]


def test_jeb_v1_fixture_reaches_head_with_sources_summaries_and_cursors(
    tmp_path: Path,
) -> None:
    config = config_from_env(
        {
            "JEB_LANDING_DIR": str(tmp_path / "landing"),
            "JEB_STATE_DIR": str(tmp_path / "state"),
            "JEB_MUNCHY_URL": "https://munchy.invalid",
        }
    )
    _restore_sqlite(FIXTURES / "jeb.sqlite.sql", config.service.state_db)

    status = jeb_state_schema(config).upgrade()
    services = create_services(config)
    services.runtime.initialize()
    source = services.source_registry.get("fixture-camera")
    with closing(services.store.connect()) as connection:
        batch = connection.execute(
            "SELECT file_count, total_bytes FROM batches WHERE id = 'fixture-batch'"
        ).fetchone()

    assert status.condition == "current"
    assert source.target == "munchy"
    assert source.target_config == {"template_id": "fixture-archive"}
    assert batch is not None
    assert tuple(batch) == (1, 12)
    assert services.event_cursors.cursor("munchy") == "17"
    assert [event.id for event in services.event_log.page(after=None, limit=100).events] == [
        "jeb-v1-event"
    ]


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
    assert cursor_state.cursor("jeb") == "23"
