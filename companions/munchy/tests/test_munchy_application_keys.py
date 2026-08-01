from __future__ import annotations

import sqlite3
from pathlib import Path

from munchy_core.persistence.application_keys import SQLiteApplicationKeyStore


def _connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def test_munchy_application_keys_authenticate_and_aggregate_in_sql(tmp_path: Path) -> None:
    database = tmp_path / "munchy.sqlite3"
    store = SQLiteApplicationKeyStore(lambda: _connection(database))
    store.initialize()

    desktop = store.create(
        app="desktop-client",
        permissions=["submissions:manage", "events:read"],
    )
    relay = store.create(app="desktop-client", permissions=["events:read"])
    jeb = store.create(app="jeb", permissions=["submissions:manage", "events:read"])

    principal = store.authenticate(str(desktop["token"]))
    assert principal is not None
    assert principal.app == "desktop-client"
    assert principal.allows("submissions:manage")

    apps = store.list_apps(
        page=1,
        per_page=25,
        q=None,
        sort="name",
        order="asc",
        all_items=True,
    )
    assert apps["total"] == 2
    summaries = apps["apps"]
    assert isinstance(summaries, list)
    assert [summary["name"] for summary in summaries] == ["desktop-client", "jeb"]
    assert summaries[0]["keys"] == 2
    assert summaries[0]["active_keys"] == 2
    assert summaries[0]["last_used_at"] is not None
    assert summaries[1] == {
        "name": "jeb",
        "keys": 1,
        "active_keys": 1,
        "last_used_at": None,
    }

    revoked = store.revoke(app="desktop-client", key_id=str(relay["id"]))
    assert revoked["status"] == "revoked"
    assert store.authenticate(str(relay["token"])) is None
    assert store.authenticate(str(jeb["token"])).app == "jeb"  # type: ignore[union-attr]
