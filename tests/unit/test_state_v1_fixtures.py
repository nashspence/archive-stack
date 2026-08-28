from __future__ import annotations

import hashlib
import sqlite3
import tomllib
from contextlib import closing
from pathlib import Path

from gogurt_listener_runtime import ListenerStore
from mango_fish.relay import CursorState
from mango_fish.schema import state_schema as mango_fish_state_schema
from riverhog_cli.local_state import state_schema as local_state_schema
from riverhog_provenance import load_or_create_installation_id

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


def test_gogurt_listener_v1_fixture_preserves_uncertain_dispatch_custody(
    tmp_path: Path,
) -> None:
    database = tmp_path / "listener.sqlite3"
    _restore_sqlite(FIXTURES / "gogurt-listener.sqlite.sql", database)

    store = ListenerStore(database)
    store.create()

    assert store.summary() == {
        "counts": {"uncertain": 1},
        "attention": [
            {
                "dispatch_id": "b" * 64,
                "mount_point": "/fixture/mounted-volume",
                "route": "camera",
                "state": "uncertain",
                "attempts": 1,
                "exit_code": None,
                "error": "listener exited while the action process had custody",
            }
        ],
    }


def test_provenance_installation_v1_fixture_retains_exact_identity(tmp_path: Path) -> None:
    fixture = FIXTURES / "provenance-installation-id"
    destination = tmp_path / "provenance-installation-id"
    destination.write_bytes(fixture.read_bytes())

    assert load_or_create_installation_id(destination) == (
        "urn:uuid:00000000-0000-4000-8000-000000000001"
    )


def test_release_inventory_accounts_for_every_v1_state_fixture() -> None:
    release = tomllib.loads((REPO_ROOT / "release.toml").read_text(encoding="utf-8"))
    inventory = release["state"]
    assert inventory["schema"] == "riverhog-durable-state-inventory/v1"
    owners = inventory["owners"]
    assert all(owner["classification"] == "compatibility-preserved" for owner in owners)
    fixture_paths = {fixture for owner in owners for fixture in owner["fixtures"]}
    assert fixture_paths == {
        path.relative_to(REPO_ROOT).as_posix() for path in FIXTURES.rglob("*") if path.is_file()
    }
    assert all(
        len(hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest()) == 64
        for path in fixture_paths
    )
