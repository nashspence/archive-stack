from __future__ import annotations

import contextlib
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
    assert "Validate Munchy runner encode profile config." in profile.stdout
    assert "Show normalized Munchy runner encode profile config." in profile.stdout
    assert "dump-json" not in profile.stdout

    job = runner.invoke(app, ["job", "--help"])
    assert job.exit_code == 0
    for summary in (
        "Dry-run the configured routed review sweep.",
        "Upload local media and start a runner job.",
        "List runner jobs.",
        "Show runner job details.",
        "Cancel a runner job.",
    ):
        assert summary in job.stdout
    assert "Watch a runner job until it is safe to delete local" in job.stdout
    assert "sources." in job.stdout

    routing = runner.invoke(app, ["routing", "--help"])
    assert routing.exit_code == 0
    assert "Explain how profile routing classifies local files." in routing.stdout


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
            assert base_url == "http://runner"

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
            collection_archive_destination: str | None,
            cancel_requested: bool | None,
            storage_wait: bool | None,
        ) -> dict[str, object]:
            assert page == 2
            assert per_page == 2
            assert sort == "created_at"
            assert order == "asc"
            assert query == "camera"
            assert terminal == "all"
            assert state == "running"
            assert workflow_mode == "collection_archive"
            assert collection_archive_destination == "riverhog"
            assert cancel_requested is False
            assert storage_wait is True
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
                    "collection_archive_destination": collection_archive_destination,
                    "cancel_requested": cancel_requested,
                    "storage_wait": storage_wait,
                },
                "jobs": [{"job_id": "job-1", "state": "running"}],
            }

    monkeypatch.setattr("munchy_cli.main.MunchyRunnerClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "job",
            "list",
            "--runner-url",
            "http://runner",
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
            "--cancel-requested",
            "false",
            "--storage-wait",
            "true",
        ],
    )

    assert result.exit_code == 0
    assert "jobs page 2/3" in result.stdout
    assert "job-1" in result.stdout
    assert "job: running" in result.stdout


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

    monkeypatch.setattr("munchy_cli.main.MunchyRunnerClient", FakeClient)

    result = runner.invoke(app, ["job", "list", "--runner-url", "http://runner", "--json"])

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


def test_munchy_job_list_reports_runner_errors_without_traceback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        def list_jobs(self, **_kwargs: object) -> dict[str, object]:
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
    awake_reasons: list[str] = []

    @contextlib.contextmanager
    def fake_keep_awake(reason: str):  # type: ignore[no-untyped-def]
        awake_reasons.append(reason)
        yield

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
    monkeypatch.setattr("munchy_cli.main.keep_system_awake", fake_keep_awake)

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
    assert seen["workflow_mode"] == "collection_archive"
    assert seen["requested_containers"] == []
    assert seen["uploaded"] == "camera-20260621T120000Z"
    assert awake_reasons == ["munchy job start"]
    assert json.loads(result.stdout)["state"] == "succeeded"


def test_munchy_job_start_skips_platform_cruft_before_upload(
    monkeypatch,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "phone"
    source_dir.mkdir()
    (source_dir / "IMG_0001.MOV").write_bytes(b"video")
    (source_dir / ".DS_Store").write_bytes(b"finder")
    nested = source_dir / "nested"
    nested.mkdir()
    (nested / "._IMG_0001.MOV").write_bytes(b"appledouble")
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
            pass

        def create_or_get_input_upload(self, request) -> dict[str, object]:  # type: ignore[no-untyped-def]
            seen["request"] = request
            return {"upload_id": request.upload_id}

        def create_job(self, request) -> dict[str, object]:  # type: ignore[no-untyped-def]
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
            "--collection",
            "phone",
            "--timestamp",
            "20260621T120000Z",
            "--group",
            "video",
            "--no-hash-cache",
            "--no-wait",
        ],
    )

    assert result.exit_code == 0
    request = seen["request"]
    assert [item.rel_path for item in request.files] == ["video/IMG_0001.MOV"]


def test_munchy_job_start_uses_configured_profile_routing(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "camera"
    source_dir.mkdir()
    (source_dir / "clip.mp4").write_bytes(b"video")
    config = tmp_path / "munchy.yaml"
    config.write_text(
        """
job:
  workflow_mode: collection_archive
  collection_archive:
    destination: riverhog
  routing:
    routes:
      - id: camera-video
        group: video
        when:
          path:
            suffix: .mp4

profiles:
  camera:
    schema_version: 1
    target: munchy-av1-nvenc
    name: camera
    archive:
      codec: av1_nvenc
      container: webm
      quality: 38

groups:
  video:
    profile: camera
    archive_mode: av1_nvenc
    eager_pipeline_batches: 1
    tasks:
      - archive_video
    metadata_projection:
      creators:
        - Example Operator
      tags:
        - device/camera
      device:
        make: Example
        model: Camera
  preserve:
    archive_mode: preserve
    tasks: []
    metadata_projection: false
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
    assert request.storage_hint["groups"]["video"]["tasks"] == ["archive_video"]
    assert request.storage_hint["groups"]["video"]["eager_pipeline_batches"] == 1
    assert request.storage_hint["groups"]["preserve"]["tasks"] == []
    assert request.job_payload["collection_archive"]["destination"] == "riverhog"
    assert request.job_payload["profile_routing"]["routes"][0]["group"] == "video"
    assert (
        request.job_payload["groups"]["video"]["encode_profile"]["archive"]["container"] == "webm"
    )
    assert request.job_payload["groups"]["video"]["eager_pipeline_batches"] == 1
    assert request.job_payload["groups"]["video"]["metadata_projection"] == {
        "creators": ["Example Operator"],
        "device": {"make": "Example", "model": "Camera"},
        "tags": ["device/camera"],
    }
    assert request.job_payload["groups"]["preserve"]["metadata_projection"] is False
    assert seen["requested_containers"] == ["webm"]


def test_munchy_job_start_uses_review_sweep_config(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "camera"
    source_dir.mkdir()
    (source_dir / "clip.mp4").write_bytes(b"video")
    config = tmp_path / "munchy-review.yaml"
    config.write_text(
        """
job:
  workflow_mode: review
  review:
    device_id: camera
    target:
      enabled: true
      destination: clover:reviews/{route_id}/{profile_id}
    sweep:
      quality: 24..28:4
      max_height:
        - 720
        - 1080
  routing:
    routes:
      - id: camera-video
        group: video
        when:
          path:
            suffix: .mp4

profiles:
  camera:
    schema_version: 1
    target: munchy-av1-nvenc
    name: camera
    archive:
      codec: av1_nvenc
      container: webm
      quality: 38

groups:
  video:
    profile: camera
    archive_mode: av1_nvenc
    tasks:
      - archive_video
  preserve:
    archive_mode: preserve
    tasks: []
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
            seen["workflow_mode"] = workflow_mode
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
            "--timestamp",
            "20260621T120000Z",
            "--no-hash-cache",
            "--no-wait",
        ],
    )

    assert result.exit_code == 0
    request = seen["request"]
    assert request.storage_hint["workflow_mode"] == "review"
    assert request.storage_hint["groups"]["video"]["tasks"] == ["qcut_video"]
    assert request.storage_hint["groups"]["preserve"]["tasks"] == []
    assert request.job_payload["groups"]["video"]["tasks"] == ["qcut_video"]
    assert request.job_payload["review"]["sweep"] == {
        "quality": "24..28:4",
        "max_height": [720, 1080],
    }
    assert "collection_slug" not in request.job_payload
    assert seen["workflow_mode"] == "review"
    assert seen["requested_containers"] == ["webm"]


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
  collection_timestamp: 20260712T120000Z
  review:
    device_id: camera
    target:
      enabled: true
      method: rclone
      destination: clover:reviews/{device_id}/{route_id}/{profile_id}/{run_id}
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
    archive_mode: av1_nvenc
    tasks:
      - archive_video
      - qcut_video
  preserve:
    archive_mode: preserve
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
    assert route["variants"][1]["destination"] == (
        "clover:reviews/camera/camera-video/q28/20260712T120000Z"
    )


def test_munchy_job_start_defaults_audio_mode_to_archive_audio(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "voice"
    source_dir.mkdir()
    (source_dir / "REC_20260628_203040.WAV").write_bytes(b"audio")
    config = tmp_path / "munchy-audio.yaml"
    config.write_text(
        """
job:
  workflow_mode: collection_archive
  archive_mode: audio

profiles:
  voice:
    schema_version: 1
    name: voice
    archive:
      codec: opus
      audio:
        bitrate: 64k
        sample_rate: 24000
        channels: 1

groups:
  voice:
    profile: voice
    archive_mode: audio
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
            seen["workflow_mode"] = workflow_mode
            seen["requested_containers"] = requested_containers

        def create_or_get_input_upload(self, request) -> dict[str, object]:  # type: ignore[no-untyped-def]
            seen["request"] = request
            return {"upload_id": request.upload_id}

        def create_job(self, request) -> dict[str, object]:  # type: ignore[no-untyped-def]
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
            "voice",
            "--timestamp",
            "20260621T120000Z",
            "--no-hash-cache",
            "--no-wait",
        ],
    )

    assert result.exit_code == 0
    request = seen["request"]
    assert request.files[0].rel_path == "voice/REC_20260628_203040.WAV"
    assert request.storage_hint["archive_mode"] == "audio"
    assert request.storage_hint["tasks"] == ["archive_audio"]
    assert request.storage_hint["groups"]["voice"]["tasks"] == ["archive_audio"]
    assert request.job_payload["groups"]["voice"]["encode_profile"]["target"] == "munchy-audio"
    assert seen["workflow_mode"] == "collection_archive"
    assert seen["requested_containers"] == ["opus"]


def test_munchy_routing_explain_reports_matches(tmp_path) -> None:  # type: ignore[no-untyped-def]
    source_dir = tmp_path / "phone"
    source_dir.mkdir()
    (source_dir / "IMG_0001.MOV").write_bytes(b"video")
    config = tmp_path / "munchy.yaml"
    config.write_text(
        """
job:
  upload_prefix: phone
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
    archive_mode: av1_nvenc
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
  upload_prefix: camera
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
    archive_mode: av1_nvenc
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

    monkeypatch.setattr("munchy.local_routing.exiftool_for_routing", fake_exiftool)

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
  upload_prefix: camera
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
    archive_mode: preserve
    tasks: []
  video:
    archive_mode: av1_nvenc
    tasks:
      - archive_video
""".strip(),
        encoding="utf-8",
    )

    def fail_probe(path):  # type: ignore[no-untyped-def]
        raise AssertionError(f"unexpected ffprobe call for {path}")

    def fail_exiftool(path, *, tags):  # type: ignore[no-untyped-def]
        raise AssertionError(f"unexpected exiftool call for {path} with {tags}")

    monkeypatch.setattr("munchy.local_routing.ffprobe_for_routing", fail_probe)
    monkeypatch.setattr("munchy.local_routing.exiftool_for_routing", fail_exiftool)

    result = runner.invoke(
        app,
        ["routing", "explain", str(source_dir), "--config", str(config), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["matches"][0]["route_id"] == "device-state"
