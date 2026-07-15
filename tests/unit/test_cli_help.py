from __future__ import annotations

from typer.testing import CliRunner

from riverhog_cli.main import app

runner = CliRunner()


def test_riverhog_help_names_current_operator_boundaries() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "collection" in result.stdout
    assert "hot" in result.stdout
    assert "find" in result.stdout


def test_hot_fetch_help_describes_archive_materialization() -> None:
    result = runner.invoke(app, ["hot", "fetch", "start", "--help"])

    assert result.exit_code == 0
    assert "only the archive objects its files require" in result.stdout


def test_collection_upload_help_exposes_retained_hot_materialization() -> None:
    result = runner.invoke(app, ["collection", "upload", "--help"])

    assert result.exit_code == 0
    assert "--archive-only" in result.stdout
