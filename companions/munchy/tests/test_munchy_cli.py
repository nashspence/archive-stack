from __future__ import annotations

import contextlib
import json

from munchy_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def test_munchy_help() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Munchy media ingest CLI." in result.stdout
    assert "Encode profile operations." in result.stdout
    assert "Munchy job operations." in result.stdout
    assert "Submit local files through a server-owned job template." in result.stdout


def test_munchy_command_help_has_summaries() -> None:
    profile = runner.invoke(app, ["profile", "--help"])
    assert profile.exit_code == 0
    assert "Validate Munchy server encode profile config." in profile.stdout
    assert "Show normalized Munchy server encode profile config." in profile.stdout
    assert "dump-json" not in profile.stdout

    job = runner.invoke(app, ["job", "--help"])
    assert job.exit_code == 0
    for summary in (
        "Dry-run the configured routed review sweep.",
        "List Munchy jobs.",
        "Show Munchy job details.",
        "Cancel a Munchy job.",
    ):
        assert summary in job.stdout
    assert "Watch a Munchy job until it is safe to delete local" in job.stdout
    assert "sources." in job.stdout

    routing = runner.invoke(app, ["routing", "--help"])
    assert routing.exit_code == 0
    assert "Explain how routing classifies local files." in routing.stdout


def test_munchy_profile_validate(tmp_path) -> None:  # type: ignore[no-untyped-def]
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        """
target: munchy-av1-nvenc
name: camera
archive:
  codec: av1_nvenc
  container: webm
  quality: 52
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["profile", "validate", str(profile_path)])

    assert result.exit_code == 0
    assert f"{profile_path}: ok (1 profile)" in result.stdout


def test_munchy_profile_validate_json(tmp_path) -> None:  # type: ignore[no-untyped-def]
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        """
target: munchy-av1-nvenc
name: camera
archive:
  codec: av1_nvenc
  container: webm
  quality: 52
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["profile", "validate", str(profile_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "path": str(profile_path),
        "profile_count": 1,
        "profiles": [
            {
                "container": "webm",
                "name": "camera",
                "quality": 52,
                "target": "munchy-av1-nvenc",
            }
        ],
        "valid": True,
    }


def test_munchy_profile_show_json(tmp_path) -> None:  # type: ignore[no-untyped-def]
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        """
schema_version: 1
target: munchy-av1-nvenc
name: camera
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["profile", "show", str(profile_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["profiles"]["camera"]["target"] == "munchy-av1-nvenc"
    assert payload["profiles"]["camera"]["archive"]["container"] == "mkv"


def test_munchy_profile_show_accepts_job_config_profiles(tmp_path) -> None:  # type: ignore[no-untyped-def]
    config_path = tmp_path / "job.yaml"
    config_path.write_text(
        """
profiles:
  camera:
    schema_version: 1
    target: munchy-av1-nvenc
    name: camera
    archive:
      codec: av1_nvenc
      container: webm
      quality: 38
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["profile", "show", str(config_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["profiles"]["camera"]["archive"]["container"] == "webm"


def test_munchy_job_list(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "http://munchy.test"

        def list_jobs(
            self,
            *,
            page: int,
            per_page: int,
            sort: str,
            order: str,
            query: str | None,
            terminal: str,
            state: str | None,
            workflow_mode: str | None,
            handoff_destination: str | None,
            cancel_requested: bool | None,
            storage_wait: bool | None,
            all_items: bool,
        ) -> dict[str, object]:
            assert page == 2
            assert per_page == 2
            assert sort == "created_at"
            assert order == "asc"
            assert query == "camera"
            assert terminal == "all"
            assert state == "running"
            assert workflow_mode == "collection_archive"
            assert handoff_destination == "riverhog"
            assert cancel_requested is False
            assert storage_wait is True
            assert all_items is True
            return {
                "page": 2,
                "pages": 3,
                "per_page": 2,
                "total": 5,
                "sort": sort,
                "order": order,
                "query": query,
                "terminal": terminal,
                "filters": {
                    "state": state,
                    "workflow_mode": workflow_mode,
                    "handoff_destination": handoff_destination,
                    "cancel_requested": cancel_requested,
                    "storage_wait": storage_wait,
                },
                "jobs": [{"job_id": "job-1", "state": "running"}],
            }

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "job",
            "list",
            "--server-url",
            "http://munchy.test",
            "--page",
            "2",
            "--per-page",
            "2",
            "--sort",
            "created_at",
            "--order",
            "asc",
            "--query",
            "camera",
            "--terminal",
            "all",
            "--state",
            "running",
            "--workflow",
            "collection-archive",
            "--destination",
            "riverhog",
            "--not-cancel-requested",
            "--storage-wait",
            "--all",
        ],
    )

    assert result.exit_code == 0
    assert "jobs page 2/3" in result.stdout
    assert "job-1" in result.stdout
    assert "job: running" in result.stdout


def test_munchy_job_list_all_ids_is_pipeable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "http://munchy.test"

        def list_jobs(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["all_items"] is True
            return {"jobs": [{"job_id": "job-1"}, {"job_id": "job-2"}]}

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(
        app,
        ["job", "list", "--server-url", "http://munchy.test", "--all", "--ids"],
    )

    assert result.exit_code == 0
    assert result.stdout == "job-1\njob-2\n"


def test_munchy_template_list_all_ids_is_pipeable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "http://munchy.test"

        def list_job_templates(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["all_items"] is True
            assert kwargs["enabled"] is False
            return {"templates": [{"name": "archive"}, {"name": "review"}]}

    monkeypatch.setattr("munchy_cli.main.MunchyAdminClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "template",
            "list",
            "--server-url",
            "http://munchy.test",
            "--disabled",
            "--all",
            "--ids",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "archive\nreview\n"


def test_munchy_application_list_all_ids_is_pipeable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "http://munchy.test"

        def list_apps(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["all_items"] is True
            assert kwargs["active"] is True
            return {"apps": [{"name": "desktop-client"}, {"name": "jeb"}]}

    monkeypatch.setattr("munchy_cli.main.MunchyAdminClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "app",
            "list",
            "--server-url",
            "http://munchy.test",
            "--active",
            "--all",
            "--ids",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "desktop-client\njeb\n"


def test_munchy_closes_the_server_client(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    closed: list[bool] = []

    class FakeClient:
        def __init__(self, _base_url: str) -> None:
            pass

        def list_jobs(self, **_kwargs: object) -> dict[str, object]:
            return {"jobs": []}

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(app, ["job", "list", "--server-url", "http://munchy.test"])

    assert result.exit_code == 0
    assert closed == [True]


def test_munchy_job_list_json(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        def list_jobs(self, **_kwargs: object) -> dict[str, object]:
            return {
                "page": 1,
                "pages": 1,
                "per_page": 25,
                "total": 1,
                "sort": "updated_at",
                "order": "desc",
                "query": None,
                "terminal": "active",
                "filters": {},
                "jobs": [{"job_id": "job-1"}],
            }

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(app, ["job", "list", "--server-url", "http://munchy.test", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "filters": {},
        "jobs": [{"job_id": "job-1"}],
        "order": "desc",
        "page": 1,
        "pages": 1,
        "per_page": 25,
        "query": None,
        "sort": "updated_at",
        "terminal": "active",
        "total": 1,
    }


def test_munchy_job_list_reports_server_errors_without_traceback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        def list_jobs(self, **_kwargs: object) -> dict[str, object]:
            raise OSError("connection refused")

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(app, ["job", "list", "--server-url", "http://munchy.test"])

    assert result.exit_code == 1
    assert "munchy: connection refused" in result.stderr
    assert "Traceback" not in result.output


def test_munchy_job_show(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "http://munchy.test"

        def get_job(self, job_id: str, *, compact: bool = False) -> dict[str, object]:
            assert job_id == "job-1"
            assert compact is True
            return {
                "job_id": "job-1",
                "collection_tags": ["camera"],
                "state": "running",
                "phase": "encoding",
            }

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(
        app,
        ["job", "show", "job-1", "--server-url", "http://munchy.test", "--compact"],
    )

    assert result.exit_code == 0
    assert "job-1" in result.stdout
    assert "camera" in result.stdout
    assert "encoding" in result.stdout


def test_munchy_job_cancel_does_not_require_confirmation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "http://munchy.test"

        def cancel_job(self, job_id: str, *, cleanup: bool = False) -> dict[str, object]:
            assert job_id == "job-1"
            assert cleanup is False
            return {"job_id": "job-1", "state": "canceled"}

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(app, ["job", "cancel", "job-1", "--server-url", "http://munchy.test"])

    assert result.exit_code == 0
    assert "canceled" in result.stdout


def test_munchy_job_cleanup_accepts_cleaned_terminal_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cleaned = {
        "job_id": "job-1",
        "state": "failed",
        "phase": "routing",
        "cleanup_completed_at": "2026-01-01T00:00:00Z",
    }

    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "http://munchy.test"

        def cancel_job(self, job_id: str, *, cleanup: bool = False) -> dict[str, object]:
            assert job_id == "job-1"
            assert cleanup is True
            return cleaned

        def wait_for_job(self, job_id: str, *, interval: float = 10.0) -> dict[str, object]:
            assert job_id == "job-1"
            return cleaned

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(
        app,
        ["job", "cancel", "job-1", "--server-url", "http://munchy.test", "--cleanup"],
    )

    assert result.exit_code == 0
    assert "cleanup complete" in result.stdout


def test_munchy_submit_uses_server_template(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    seen: dict[str, object] = {}
    awake_reasons: list[str] = []

    @contextlib.contextmanager
    def fake_keep_awake(reason: str):  # type: ignore[no-untyped-def]
        awake_reasons.append(reason)
        yield

    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "http://munchy.test"

        def preflight_submission(self, request):  # type: ignore[no-untyped-def]
            seen["request"] = request
            return {
                "accepted": True,
                "template": {"name": request.template, "revision": 3, "digest": "digest"},
                "workflow_mode": "collection_archive",
                "content_inspection": "after_upload",
            }

        def create_submission(self, request):  # type: ignore[no-untyped-def]
            return {
                "submission_id": request.submission_id,
                "upload": {"state": "uploading"},
                "job": {"job_id": request.submission_id, "state": "queued"},
            }

        def upload_files(self, request):  # type: ignore[no-untyped-def]
            seen["uploaded"] = request.submission_id
            return {"state": "uploaded"}

        def wait_for_submission(self, submission_id: str, *, interval: float = 10.0):
            assert interval == 0.5
            return {
                "submission_id": submission_id,
                "upload": {"state": "uploaded"},
                "job": {
                    "job_id": submission_id,
                    "state": "succeeded",
                    "phase": "done",
                    "handoff": {"state": "complete", "safe_to_delete": True},
                },
            }

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)
    monkeypatch.setattr("munchy_cli.main.keep_system_awake", fake_keep_awake)

    result = runner.invoke(
        app,
        [
            "submit",
            str(source),
            "--template",
            "camera-archive",
            "--input",
            "route=camera-main",
            "--server-url",
            "http://munchy.test",
            "--tag",
            "camera",
            "--run-id",
            "20260621T120000.123456Z",
            "--no-hash-cache",
            "--interval",
            "0.5",
            "--json",
        ],
    )

    assert result.exit_code == 0
    request = seen["request"]
    assert request.template == "camera-archive"
    assert request.inputs == {"route": "camera-main"}
    assert request.collection_tags == ("camera",)
    assert request.run_id == "20260621T120000.123456Z"
    assert [item.rel_path for item in request.files] == ["clip.mp4"]
    assert seen["uploaded"] == request.submission_id
    assert awake_reasons == ["munchy submit"]
    payload = json.loads(result.stdout)
    assert payload["submission_id"] == request.submission_id
    assert payload["job"]["state"] == "succeeded"


def test_munchy_submit_dry_run_preflights_without_creating_state(
    monkeypatch,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "phone"
    source_dir.mkdir()
    (source_dir / "IMG_0001.MOV").write_bytes(b"video")
    (source_dir / ".DS_Store").write_bytes(b"finder")
    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "http://munchy.test"

        def preflight_submission(self, request):  # type: ignore[no-untyped-def]
            seen["request"] = request
            return {
                "accepted": True,
                "template": {"name": request.template, "revision": 2, "digest": "digest"},
                "workflow_mode": "review",
                "content_inspection": "after_upload",
            }

        def create_submission(self, request):  # type: ignore[no-untyped-def]
            raise AssertionError(f"dry-run created submission {request.submission_id}")

    monkeypatch.setattr("munchy_cli.main.MunchyClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "submit",
            str(source_dir),
            "--template",
            "phone-review",
            "--server-url",
            "http://munchy.test",
            "--no-hash-cache",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    request = seen["request"]
    assert [item.rel_path for item in request.files] == ["IMG_0001.MOV"]
    payload = json.loads(result.stdout)
    assert payload["status"] == "would_submit"
    assert payload["template"] == "phone-review"
    assert payload["template_revision"] == 2
    assert payload["workflow_mode"] == "review"


def test_munchy_job_plan_review_sweep_reports_routes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "camera"
    source_dir.mkdir()
    (source_dir / "clip.mp4").write_bytes(b"video")
    (source_dir / "photo.jpg").write_bytes(b"photo")
    config = tmp_path / "munchy-review.yaml"
    config.write_text(
        """
job:
  workflow_mode: review
  run_id: 20260712T120000Z
  handoff:
    destination: rclone
    options:
      location: review-remote:reviews/{device_id}/{route_id}/{profile_id}/{run_id}
  review:
    device_id: camera
    sweep:
      route_ids:
        - camera-video
      quality: 24..28:4
  routing:
    routes:
      - id: camera-video
        group: video
        when:
          path:
            suffix: .mp4
      - id: camera-photo
        group: preserve
        when:
          path:
            suffix: .jpg

profiles:
  video:
    schema_version: 1
    target: munchy-av1-nvenc
    name: video
    archive:
      codec: av1_nvenc
      container: webm
      quality: 40

groups:
  video:
    profile: video
    output_mode: video
    tasks:
      - archive_video
      - qcut_video
  preserve:
    output_mode: preserve
    tasks: []
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "job",
            "plan-review-sweep",
            str(source_dir),
            "--config",
            str(config),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "munchy.review-sweep-plan"
    assert payload["ok"] is True
    assert payload["requested_route_ids"] == ["camera-video"]
    assert payload["routes_total"] == 1
    assert payload["files_total"] == 1
    assert payload["routing"]["matched_files"] == 2
    route = payload["routes"][0]
    assert route["route_id"] == "camera-video"
    assert route["tasks"] == ["qcut_video"]
    assert [variant["profile_id"] for variant in route["variants"]] == ["q24", "q28"]
    assert route["variants"][1]["location"] == (
        "review-remote:reviews/camera/camera-video/q28/20260712T120000Z"
    )


def test_munchy_routing_explain_reports_matches(tmp_path) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "phone"
    source_dir.mkdir()
    (source_dir / "IMG_0001.MOV").write_bytes(b"video")
    config = tmp_path / "munchy.yaml"
    config.write_text(
        """
job:
  destination_prefix: phone
  handoff:
    destination: command
  routing:
    routes:
      - id: phone-video
        group: video
        into: phone/video
        when:
          path:
            prefix: phone
            suffix: .mov

groups:
  video:
    output_mode: video
    tasks:
      - archive_video
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["routing", "explain", str(source_dir), "--config", str(config), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["matched_files"] == 1
    assert payload["matches"][0]["path"] == "phone/IMG_0001.MOV"
    assert payload["matches"][0]["route_id"] == "phone-video"
    assert payload["matches"][0]["group"] == "video"
    assert payload["matches"][0]["collection_rel_path"] == "phone/video/IMG_0001.MOV"


def test_munchy_routing_explain_uses_configured_sidecar_facts_only(
    monkeypatch,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "camera"
    source_dir.mkdir()
    (source_dir / "C0001.MP4").write_bytes(b"video")
    (source_dir / "C0001M01.XML").write_text("<metadata />", encoding="utf-8")
    config = tmp_path / "munchy.yaml"
    config.write_text(
        """
job:
  destination_prefix: camera
  handoff:
    destination: command
  routing:
    sidecars:
      camera_xml:
        format: xml
        path: "{parent}/{stem}M01.XML"
        primary:
          path:
            suffix: .mp4
        facts:
          source: exiftool
          tags:
            - Make
            - Model
    routes:
      - id: camera-video
        group: video
        when:
          all:
            - path:
                suffix: .mp4
            - fact: sidecars.camera_xml.facts.exif.make
              equals: example imaging

groups:
  video:
    output_mode: video
    tasks:
      - archive_video
""".strip(),
        encoding="utf-8",
    )
    exiftool_calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_exiftool(path, *, tags):  # type: ignore[no-untyped-def]
        exiftool_calls.append((path.name, tuple(tags)))
        assert path.name == "C0001M01.XML"
        return {
            "EXIF:Make": "Example Imaging",
            "EXIF:Model": "Synthetic Camera",
        }

    monkeypatch.setattr("munchy_api_client.local_routing.exiftool_for_routing", fake_exiftool)

    result = runner.invoke(
        app,
        ["routing", "explain", str(source_dir), "--config", str(config), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["matches"][0]["route_id"] == "camera-video"
    assert payload["matches"][0]["matched_facts"] == {
        "sidecars.camera_xml.facts.exif.make": "example imaging"
    }
    assert exiftool_calls == [("C0001M01.XML", ("Make", "Model"))]


def test_munchy_routing_explain_skips_expensive_tools_for_path_only_route(
    monkeypatch,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "camera"
    source_dir.mkdir()
    (source_dir / "leinfo.sav").write_bytes(b"state")
    config = tmp_path / "munchy.yaml"
    config.write_text(
        """
job:
  destination_prefix: camera
  handoff:
    destination: command
  routing:
    routes:
      - id: device-state
        group: state
        when:
          path:
            filename_glob: leinfo.sav
      - id: camera-video
        group: video
        when:
          all:
            - path:
                suffix: .mp4
            - fact: video.codec
              equals: hevc
            - fact: exif.make
              equals: example imaging

groups:
  state:
    output_mode: preserve
    tasks: []
  video:
    output_mode: video
    tasks:
      - archive_video
""".strip(),
        encoding="utf-8",
    )

    def fail_probe(path):  # type: ignore[no-untyped-def]
        raise AssertionError(f"unexpected ffprobe call for {path}")

    def fail_exiftool(path, *, tags):  # type: ignore[no-untyped-def]
        raise AssertionError(f"unexpected exiftool call for {path} with {tags}")

    monkeypatch.setattr("munchy_api_client.local_routing.ffprobe_for_routing", fail_probe)
    monkeypatch.setattr("munchy_api_client.local_routing.exiftool_for_routing", fail_exiftool)

    result = runner.invoke(
        app,
        ["routing", "explain", str(source_dir), "--config", str(config), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["matches"][0]["route_id"] == "device-state"
