from __future__ import annotations

import json

from typer.testing import CliRunner

from munchy_cli.main import app

runner = CliRunner()


def test_munchy_help() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Munchy media ingest CLI." in result.stdout
    assert "Encode profile operations." in result.stdout
    assert "Runner job operations." in result.stdout


def test_munchy_command_help_has_summaries() -> None:
    profile = runner.invoke(app, ["profile", "--help"])
    assert profile.exit_code == 0
    assert "Validate an encode profile file." in profile.stdout
    assert "Show a normalized encode profile." in profile.stdout
    assert "dump-json" not in profile.stdout

    job = runner.invoke(app, ["job", "--help"])
    assert job.exit_code == 0
    for summary in (
        "Upload local media and start a runner job.",
        "List runner jobs.",
        "Show runner job details.",
        "Watch a runner job until it is safe to delete local sources.",
        "Cancel a runner job.",
    ):
        assert summary in job.stdout


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
    assert f"{profile_path}: ok" in result.stdout


def test_munchy_profile_validate_json(tmp_path) -> None:  # type: ignore[no-untyped-def]
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

    result = runner.invoke(app, ["profile", "validate", str(profile_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "container": "webm",
        "path": str(profile_path),
        "quality": 52,
        "target": "munchy-av1-nvenc",
        "valid": True,
    }


def test_munchy_profile_show_json(tmp_path) -> None:  # type: ignore[no-untyped-def]
    profile_path = tmp_path / "profile.toml"
    profile_path.write_text('target = "munchy-av1-nvenc"\n', encoding="utf-8")

    result = runner.invoke(app, ["profile", "show", str(profile_path), "--json"])

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
    assert "job-1" in result.stdout
    assert "job: running" in result.stdout


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


def test_munchy_job_list_reports_runner_errors_without_traceback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        def list_jobs(self, *, include_terminal: bool, limit: int) -> list[dict[str, object]]:
            raise OSError("connection refused")

    monkeypatch.setattr("munchy_cli.main.MunchyRunnerClient", FakeClient)

    result = runner.invoke(app, ["job", "list", "--runner-url", "http://runner"])

    assert result.exit_code == 1
    assert "munchy: connection refused" in result.stderr
    assert "Traceback" not in result.output


def test_munchy_job_show(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "http://runner"

        def get_job(self, job_id: str, *, compact: bool = False) -> dict[str, object]:
            assert job_id == "job-1"
            assert compact is True
            return {
                "job_id": "job-1",
                "collection_slug": "camera",
                "state": "running",
                "phase": "encoding",
            }

    monkeypatch.setattr("munchy_cli.main.MunchyRunnerClient", FakeClient)

    result = runner.invoke(
        app,
        ["job", "show", "job-1", "--runner-url", "http://runner", "--compact"],
    )

    assert result.exit_code == 0
    assert "job-1" in result.stdout
    assert "camera" in result.stdout
    assert "encoding" in result.stdout


def test_munchy_job_cancel_does_not_require_confirmation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "http://runner"

        def cancel_job(self, job_id: str, *, cleanup: bool = False) -> dict[str, object]:
            assert job_id == "job-1"
            assert cleanup is False
            return {"job_id": "job-1", "state": "cancelled"}

    monkeypatch.setattr("munchy_cli.main.MunchyRunnerClient", FakeClient)

    result = runner.invoke(app, ["job", "cancel", "job-1", "--runner-url", "http://runner"])

    assert result.exit_code == 0
    assert "cancelled" in result.stdout


def test_munchy_job_start_builds_direct_group_upload(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "http://runner"

        def check_ready(
            self,
            workflow_mode: str | None = None,
            *,
            requested_containers: list[str] | None = None,
        ) -> None:
            seen["workflow_mode"] = workflow_mode
            seen["requested_containers"] = requested_containers

        def create_or_get_input_upload(self, request) -> dict[str, object]:  # type: ignore[no-untyped-def]
            seen["request"] = request
            return {"upload_id": request.upload_id}

        def create_job(self, request) -> dict[str, object]:  # type: ignore[no-untyped-def]
            seen["job_payload"] = request.job_payload
            return {"job_id": request.job_id, "state": "queued"}

        def upload_files(self, request) -> dict[str, object]:  # type: ignore[no-untyped-def]
            seen["uploaded"] = request.upload_id
            return {"upload_id": request.upload_id, "state": "uploaded"}

        def wait_for_job(self, job_id: str, *, interval: float = 10.0) -> dict[str, object]:
            assert interval == 0.5
            return {
                "job_id": job_id,
                "collection_slug": "camera",
                "state": "succeeded",
                "phase": "done",
            }

    monkeypatch.setattr("munchy_cli.main.MunchyRunnerClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "job",
            "start",
            str(source),
            "--runner-url",
            "http://runner",
            "--collection",
            "camera",
            "--timestamp",
            "20260621T120000Z",
            "--group",
            "video",
            "--no-hash-cache",
            "--interval",
            "0.5",
            "--json",
        ],
    )

    assert result.exit_code == 0
    request = seen["request"]
    assert request.job_id == "camera-20260621T120000Z"
    assert request.upload_id == "camera-20260621T120000Z"
    assert request.files[0].rel_path == "video/clip.mp4"
    assert request.storage_hint["structured_routing"] is False
    assert request.storage_hint["groups"]["video"]["archive_mode"] == "av1_nvenc"
    assert seen["workflow_mode"] == "archive"
    assert seen["requested_containers"] == []
    assert seen["uploaded"] == "camera-20260621T120000Z"
    assert json.loads(result.stdout)["state"] == "succeeded"


def test_munchy_job_start_uses_configured_profile_routing(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "camera"
    source_dir.mkdir()
    (source_dir / "clip.mp4").write_bytes(b"video")
    config = tmp_path / "munchy.toml"
    config.write_text(
        """
[job]
workflow_mode = "archive"

[job.riverhog]
enabled = true

[profiles.camera]
schema_version = 1
target = "munchy-av1-nvenc"
name = "camera"

[profiles.camera.archive]
codec = "av1_nvenc"
container = "webm"
quality = 38

[groups.video]
profile = "camera"
archive_mode = "av1_nvenc"
gpu_tasks = ["archive_video"]

[groups.passthrough]
archive_mode = "originals"
gpu_tasks = []

[[job.profile_routing.routes]]
id = "camera-video"
group = "video"
suffixes = [".mp4"]
""".strip(),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        def check_ready(
            self,
            workflow_mode: str | None = None,
            *,
            requested_containers: list[str] | None = None,
        ) -> None:
            seen["requested_containers"] = requested_containers

        def create_or_get_input_upload(self, request) -> dict[str, object]:  # type: ignore[no-untyped-def]
            seen["request"] = request
            return {"upload_id": request.upload_id}

        def create_job(self, request) -> dict[str, object]:  # type: ignore[no-untyped-def]
            seen["job_payload"] = request.job_payload
            return {"job_id": request.job_id, "state": "queued"}

        def upload_files(self, request) -> dict[str, object]:  # type: ignore[no-untyped-def]
            return {"upload_id": request.upload_id, "state": "uploaded"}

    monkeypatch.setattr("munchy_cli.main.MunchyRunnerClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "job",
            "start",
            str(source_dir),
            "--runner-url",
            "http://runner",
            "--config",
            str(config),
            "--collection",
            "camera",
            "--timestamp",
            "20260621T120000Z",
            "--no-hash-cache",
            "--no-wait",
        ],
    )

    assert result.exit_code == 0
    request = seen["request"]
    assert request.files[0].rel_path == "clip.mp4"
    assert request.storage_hint["structured_routing"] is True
    assert request.storage_hint["groups"]["video"]["gpu_tasks"] == ["archive_video"]
    assert request.storage_hint["groups"]["passthrough"]["gpu_tasks"] == []
    assert request.job_payload["riverhog"]["enabled"] is True
    assert request.job_payload["profile_routing"]["routes"][0]["group"] == "video"
    assert (
        request.job_payload["groups"]["video"]["encode_profile"]["archive"]["container"] == "webm"
    )
    assert seen["requested_containers"] == ["webm"]
