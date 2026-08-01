from __future__ import annotations

from click import unstyle
from riverhog_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def test_riverhog_help_names_current_archive_boundaries() -> None:
    result = runner.invoke(app, ["--help"], terminal_width=160, color=False)

    assert result.exit_code == 0
    assert "collection" in result.stdout
    assert "archive" in result.stdout
    assert "find" in result.stdout


def test_collection_upload_help_exposes_archive_store_selection() -> None:
    result = runner.invoke(
        app,
        ["collection", "upload", "--help"],
        terminal_width=160,
        color=False,
    )

    assert result.exit_code == 0
    assert "--archive-store" in unstyle(result.stdout)


def test_riverhog_local_help_names_materialization_operations() -> None:
    result = runner.invoke(app, ["local", "--help"], terminal_width=160, color=False)

    assert result.exit_code == 0
    for command in ("add", "sync", "repair", "audit", "evict"):
        assert command in result.stdout
