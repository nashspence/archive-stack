from __future__ import annotations

import json
from pathlib import Path

from riverhog_cli.main import app
from typer.testing import CliRunner

RUNNER = CliRunner()


def test_local_provenance_observer_introspection_has_human_json_parity() -> None:
    result = RUNNER.invoke(app, ["local", "provenance-observer", "list", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["format"] == "riverhog-provenance-observer-provider-list/v1"
    names = {item["name"] for item in payload["providers"]}
    assert {"riverhog-linux", "riverhog-macos", "riverhog-windows"} <= names
    human = RUNNER.invoke(app, ["local", "provenance-observer", "list"])
    assert human.exit_code == 0
    assert "provenance observers:" in human.stdout
    assert "riverhog-linux" in human.stdout


def test_local_provenance_observer_show_reports_exact_contract_identity() -> None:
    result = RUNNER.invoke(
        app,
        ["local", "provenance-observer", "show", "riverhog-linux", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["observer_id"] == "riverhog-provenance-linux-observer/v1"
    assert payload["contract_id"] == "riverhog-provenance-linux-observation/v1"
    assert len(payload["contract_sha256"]) == 64
    assert payload["schema_ids"]
    human = RUNNER.invoke(
        app,
        ["local", "provenance-observer", "show", "riverhog-linux"],
    )
    assert human.exit_code == 0
    assert payload["contract_sha256"] in human.stdout


def test_upload_requires_explicit_capture_composition_before_opening_session(
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    (root / "payload.bin").write_bytes(b"payload")

    result = RUNNER.invoke(app, ["collection", "upload", "start", str(root)])

    assert result.exit_code != 0
    assert "provenance capture requires" in result.output


def test_upload_dry_run_reports_the_exact_selected_provider_in_both_outputs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    (root / "payload.bin").write_bytes(b"payload")
    arguments = [
        "collection",
        "upload",
        "start",
        str(root),
        "--provenance-observer",
        "riverhog-linux",
        "--dry-run",
    ]

    result = RUNNER.invoke(app, [*arguments, "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provenance_observer"]["name"] == "riverhog-linux"
    human = RUNNER.invoke(app, arguments)
    assert human.exit_code == 0
    assert "provenance observer: riverhog-linux" in human.stdout
    assert payload["provenance_observer"]["contract_sha256"] in human.stdout


def test_upload_provider_environment_selection_is_connected(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    (root / "payload.bin").write_bytes(b"payload")
    monkeypatch.setenv("RIVERHOG_PROVENANCE_OBSERVER", "riverhog-linux")

    result = RUNNER.invoke(
        app,
        ["collection", "upload", "start", str(root), "--dry-run", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["provenance_observer"]["name"] == "riverhog-linux"
