from __future__ import annotations

from pathlib import Path

import pytest
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


def test_state_cli_enforces_the_postgresql_deployment_boundary(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    database_url = f"sqlite+pysqlite:///{tmp_path / 'stove0.sqlite3'}"
    monkeypatch.setenv("STOVE0_DATABASE_URL", database_url)

    with pytest.raises(ValueError, match="STOVE0_DATABASE_URL must use postgresql"):
        main(["state", "upgrade", "--json"])
