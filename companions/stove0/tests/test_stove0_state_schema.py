from __future__ import annotations

import json
from pathlib import Path

from stove0_api.app import main
from stove0_core import SqlAlchemyStateStore, stove0_state_schema


def test_stove0_state_upgrade_establishes_exact_current_v1_schema(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'stove0.sqlite3'}"
    schema = stove0_state_schema(database_url)

    upgraded = schema.upgrade()
    verified = schema.validate()
    store = SqlAlchemyStateStore(database_url, initialize=False)
    try:
        assert upgraded.condition == "current"
        assert verified == upgraded
        assert store.list_work()["total"] == 0
        assert store.list_evaluations()["total"] == 0
    finally:
        store.engine.dispose()


def test_state_cli_needs_only_database_authority(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    database_url = f"sqlite+pysqlite:///{tmp_path / 'stove0.sqlite3'}"
    monkeypatch.setenv("STOVE0_DATABASE_URL", database_url)

    assert main(["state", "upgrade", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "condition": "current",
        "current_revision": "v1_0003",
        "head_revision": "v1_0003",
        "name": "stove0 control",
    }
