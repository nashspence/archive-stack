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


def test_munchy_job_list(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "http://runner"

        def list_jobs(self, *, include_terminal: bool, limit: int) -> list[dict[str, object]]:
            assert include_terminal is True
            assert limit == 2
            return [{"job_id": "job-1", "state": "running"}]

    monkeypatch.setattr("munchy_cli.main.MunchyRunnerClient", FakeClient)

    result = runner.invoke(
        app,
        ["job", "list", "--runner-url", "http://runner", "--all", "--limit", "2"],
    )

    assert result.exit_code == 0
    assert "job-1 | job: running" in result.stdout


def test_munchy_job_list_json(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        def list_jobs(self, *, include_terminal: bool, limit: int) -> list[dict[str, object]]:
            return [{"job_id": "job-1"}]

    monkeypatch.setattr("munchy_cli.main.MunchyRunnerClient", FakeClient)

    result = runner.invoke(app, ["job", "list", "--runner-url", "http://runner", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"jobs": [{"job_id": "job-1"}]}


def test_munchy_job_cancel_requires_confirmation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(app, ["job", "cancel", "job-1", "--runner-url", "http://runner"])

    assert result.exit_code != 0
    assert "--yes" in result.output
