from __future__ import annotations

from typer.testing import CliRunner

from djdan.main import app as disc_app
from riverhog_cli.main import app as riverhog_app

runner = CliRunner()


def test_riverhog_help() -> None:
    result = runner.invoke(riverhog_app, ["--help"])
    assert result.exit_code == 0
    assert "riverhog collection and hot-storage CLI" in result.stdout
    assert "collection" in result.stdout
    assert "hot" in result.stdout
    assert "dashboard" not in result.stdout


def test_djdan_help() -> None:
    result = runner.invoke(disc_app, ["--help"])
    assert result.exit_code == 0
    assert "fetch" in result.stdout
    assert "image" in result.stdout
    assert "disc" in result.stdout
    assert "recover" not in result.stdout
