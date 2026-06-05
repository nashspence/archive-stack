from __future__ import annotations

import json

from typer.testing import CliRunner

from munchy_cli.main import app

runner = CliRunner()


def test_munchy_help() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "munchy media ingest CLI" in result.stdout


def test_munchy_profile_validate(tmp_path) -> None:  # type: ignore[no-untyped-def]
    profile_path = tmp_path / "profile.toml"
    profile_path.write_text(
        """
target = "munchy-av1-nvenc"

[archive]
container = "webm"

[archive.video]
quality = 52
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["profile", "validate", str(profile_path)])

    assert result.exit_code == 0
    assert "container=webm quality=52" in result.stdout


def test_munchy_profile_dump_json(tmp_path) -> None:  # type: ignore[no-untyped-def]
    profile_path = tmp_path / "profile.toml"
    profile_path.write_text('target = "munchy-av1-nvenc"\n', encoding="utf-8")

    result = runner.invoke(app, ["profile", "dump-json", str(profile_path)])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["target"] == "munchy-av1-nvenc"
    assert payload["archive"]["container"] == "mkv"
