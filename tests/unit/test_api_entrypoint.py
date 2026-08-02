import importlib.metadata
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from riverhog_api import app as api_app

from tests.unit.db_helpers import sqlite_url


def test_api_version_matches_the_installed_server_distribution() -> None:
    assert api_app.create_app().version == importlib.metadata.version("riverhog-server")


def test_api_entrypoint_listens_on_the_container_network(monkeypatch: Any) -> None:
    invocation: dict[str, Any] = {}

    def fake_run(application: str, **options: Any) -> None:
        invocation.update({"application": application, **options})

    monkeypatch.setattr(api_app.uvicorn, "run", fake_run)

    api_app.main([])

    assert invocation == {
        "application": "riverhog_api.app:create_app",
        "factory": True,
        "host": "0.0.0.0",
        "port": 8000,
        "reload": False,
    }


def test_api_state_commands_report_and_verify_the_current_revision(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    monkeypatch.setattr(
        api_app,
        "load_runtime_config",
        lambda: SimpleNamespace(database_url=database_url),
    )

    assert api_app.main(["state", "status", "--json"]) == 0
    empty = json.loads(capsys.readouterr().out)
    assert api_app.main(["state", "upgrade", "--json"]) == 0
    upgraded = json.loads(capsys.readouterr().out)
    assert api_app.main(["state", "verify", "--json"]) == 0
    verified = json.loads(capsys.readouterr().out)

    assert empty["condition"] == "empty"
    assert upgraded["current_revision"] == "v1_0001"
    assert verified["condition"] == "current"
