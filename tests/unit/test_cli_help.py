from __future__ import annotations

from typer.testing import CliRunner

from arc_cli.main import app as arc_app
from arc_disc.main import app as disc_app

runner = CliRunner()


def test_arc_help() -> None:
    result = runner.invoke(arc_app, ["--help"])
    assert result.exit_code == 0
    assert "arc archival control CLI" in result.stdout


def test_arc_disc_help() -> None:
    result = runner.invoke(disc_app, ["--help"])
    assert result.exit_code == 0
    assert "fetch" in result.stdout
