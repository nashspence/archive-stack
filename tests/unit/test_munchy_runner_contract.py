from __future__ import annotations

import base64
import errno
import gzip
import hashlib
import importlib.util
import json
import logging
import sys
import threading
import uuid
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from munchy.filesystem_metadata import SOURCE_FILESYSTEM_METADATA_FILENAME
from munchy.uvicorn_logging import DropHealthAccessLogFilter


def secure_link_token(*, expires: str, path: str, secret: str) -> str:
    digest = hashlib.md5(f"{expires}{unquote(path)} {secret}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def load_runner(tmp_path: Path, monkeypatch) -> ModuleType:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MUNCHY_RUNNER_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("MUNCHY_RUNNER_TUSD_DIR", str(tmp_path / "tusd"))
    monkeypatch.setenv("MUNCHY_RUNNER_GPU_RUNTIME_DIR", str(tmp_path / "gpu-runtime"))
    module_path = (
        Path(__file__).resolve().parents[2] / "services" / "munchy-runner" / "app" / "main.py"
    )
    module_name = f"munchy_runner_main_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_health_access_log_filter_drops_only_health_paths() -> None:
    access_filter = DropHealthAccessLogFilter()
    health = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/health/ready", "1.1", 200),
        None,
    )
    api = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/v1/jobs/job-1", "1.1", 200),
        None,
    )

    assert access_filter.filter(health) is False
    assert access_filter.filter(api) is True


def test_api_auth_protects_v1_but_not_health(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_API_TOKEN", "runner-token")
    runner = load_runner(tmp_path, monkeypatch)

    client = TestClient(runner.app)

    assert client.get("/health/live").status_code == 200
    unauthorized = client.get("/v1/capabilities")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["WWW-Authenticate"] == "Bearer"
    authorized = client.get(
        "/v1/capabilities",
        headers={"Authorization": "Bearer runner-token"},
    )
    assert authorized.status_code == 200
    assert authorized.json()["operations"]["list_jobs"] is True


def test_capabilities_advertise_munchy_profile_target(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)

    capabilities = runner.capabilities()

    assert capabilities["archive_modes"] == ["av1_nvenc", "audio", "preserve"]
    assert capabilities["tasks"] == [
        "archive_video",
        "archive_audio",
        "qcut_video",
        "audio_review",
    ]
    assert capabilities["encode_profile"]["targets"] == ["munchy-av1-nvenc", "munchy-audio"]
    assert capabilities["encode_profile"]["archive_codecs"] == ["av1_nvenc", "opus"]
    assert capabilities["encode_profile"]["containers"] == ["mkv", "webm", "opus"]
    assert capabilities["profile_groups"]["input_path_shape"] == "<profile-group>/<file>"
    assert capabilities["profile_groups"]["structured_routing"] is True
    assert capabilities["storage"]["eager_archive_only_encoding"] is True
    assert "MUNCHY_RUNNER_NOTIFY_WEBHOOKS" in capabilities["notify"]["webhook_config"]
    assert capabilities["storage"]["eager_archive_pipeline_batches"] == 3
    assert capabilities["storage"]["max_running_jobs"] == 1
    assert capabilities["notify"]["default_enabled"] is False
    assert capabilities["notify"]["default_recipients"] == []
    assert capabilities["notify"]["client_preflight_failed"] is True
    assert capabilities["operations"]["notify_preflight_failed"] is True


def test_riverhog_handoff_wait_defaults_to_finalized(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("MUNCHY_RUNNER_RIVERHOG_WAIT", raising=False)
    runner = load_runner(tmp_path, monkeypatch)

    assert runner.RIVERHOG_WAIT == "finalized"
    assert runner.RiverhogConfig(enabled=True).wait == "finalized"


def test_public_tusd_upload_url_signs_nginx_normalized_path(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_TUSD_PUBLIC_SIGNING_SECRET", "fixture-secret")
    runner = load_runner(tmp_path, monkeypatch)

    url = runner.public_tusd_upload_url("http://runner.test/files/.munchy-runner%2Fuploads%2Fabc")

    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    expires = query["expires"][0]
    assert query == {
        "expires": [expires],
        "md5": [
            secure_link_token(
                expires=expires,
                path=parsed.path,
                secret="fixture-secret",
            )
        ],
    }


def test_public_tusd_upload_url_omits_signature_without_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)

    url = runner.public_tusd_upload_url("http://runner.test/files/.munchy-runner%2Fuploads%2Fabc")

    assert url == "http://runner.test/files/.munchy-runner%2Fuploads%2Fabc"


def test_structured_input_upload_accepts_source_prefixed_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)

    req = runner.CreateInputUploadRequest(
        files=[runner.InputFileSpec(path="front-door/telephoto/clip.mp4", bytes=12)],
        storage_hint=runner.InputUploadStorageHint(
            workflow_mode="collection_archive",
            structured_routing=True,
            groups={
                "video": runner.StorageGroupHint(
                    archive_mode="av1_nvenc",
                    tasks=["archive_video"],
                )
            },
        ),
    )

    assert req.files[0].path == "front-door/telephoto/clip.mp4"
    assert (
        runner.upload_file_resolved_group(
            {"path": "front-door/telephoto/clip.mp4", "structured_routing": True}
        )
        == ""
    )


def test_preserve_profile_groups_are_copy_only(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)

    group = runner.ProfileGroupConfig(
        archive_mode="preserve",
        tasks=["archive_video"],
    )
    storage_group = runner.StorageGroupHint(
        archive_mode="preserve",
        tasks=["archive_video"],
    )

    assert group.archive_mode == "preserve"
    assert group.tasks == []
    assert storage_group.archive_mode == "preserve"
    assert storage_group.tasks == []


def test_review_job_request_accepts_clip_plan_config(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)

    req = runner.CreateJobRequest(
        workflow_mode="review",
        tasks=["qcut_video"],
        review={
            "device_id": "camera",
            "route_id": "camera-main-video",
            "profile_id": "webm-q42",
            "target": {"enabled": True, "destination": "clover:reviews"},
            "clip_plan": {"target_seconds": 90},
        },
    )

    assert req.review is not None
    assert req.review.clip_plan is not None
    assert req.review.clip_plan.target_seconds == 90
    assert req.review.clip_plan.min_seconds == 6
    assert req.review.clip_plan.max_seconds == 9

    with pytest.raises(ValueError, match="min_seconds"):
        runner.CreateJobRequest(
            workflow_mode="review",
            tasks=["qcut_video"],
            review={
                "device_id": "camera",
                "route_id": "camera-main-video",
                "profile_id": "webm-q42",
                "target": {"enabled": True, "destination": "clover:reviews"},
                "clip_plan": {"min_seconds": 10, "max_seconds": 9},
            },
        )


def test_create_job_request_accepts_full_metadata_projection_config(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)

    req = runner.CreateJobRequest(
        collection_slug="phone-collection-archive",
        workflow_mode="collection_archive",
        groups={
            "phone-video": {
                "archive_mode": "av1_nvenc",
                "tasks": ["archive_video"],
                "metadata_projection": {
                    "creators": ["Nash Spence", "Katie Spence", "Nash Spence"],
                    "device": {
                        "make": " Apple ",
                        "model": " iPhone SE (2nd generation) ",
                    },
                    "gps": {
                        "latitude": 48.999527523960296,
                        "longitude": -122.74040765142755,
                    },
                    "tags": ["device/nash-iphone-se2", "device/nash-iphone-se2"],
                    "include_context_tags": False,
                },
            }
        },
    )

    projection = runner.profile_group_dump(req.groups["phone-video"])["metadata_projection"]

    assert projection["creators"] == ["Nash Spence", "Katie Spence"]
    assert projection["device"] == {
        "make": "Apple",
        "model": "iPhone SE (2nd generation)",
    }
    assert projection["gps"] == {
        "latitude": 48.999527523960296,
        "longitude": -122.74040765142755,
    }
    assert projection["tags"] == ["device/nash-iphone-se2"]
    assert projection["include_context_tags"] is False


def test_create_job_request_accepts_disabled_group_metadata_projection(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)

    req = runner.CreateJobRequest(
        collection_slug="camera-collection-archive",
        workflow_mode="collection_archive",
        groups={
            "device-state": {
                "archive_mode": "preserve",
                "metadata_projection": False,
            },
        },
    )

    assert runner.profile_group_dump(req.groups["device-state"])["metadata_projection"] is False


def test_create_job_request_accepts_profile_routing_extra_exiftool_tags(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)

    req = runner.CreateJobRequest(
        collection_slug="camera-collection-archive",
        workflow_mode="collection_archive",
        groups={
            "video": {
                "archive_mode": "av1_nvenc",
                "tasks": ["archive_video"],
            },
        },
        profile_routing={
            "extra_exiftool_tags": [
                "AndroidCaptureFPS",
                "AndroidMake",
                "AndroidCaptureFPS",
            ],
            "routes": [
                {
                    "id": "camera-video",
                    "group": "video",
                    "when": {"path": {"suffix": ".mp4"}},
                }
            ],
        },
    )

    routing = req.profile_routing.model_dump(exclude_none=True)

    assert routing["extra_exiftool_tags"] == ["AndroidCaptureFPS", "AndroidMake"]


def test_create_job_request_accepts_sidecar_fact_extractors(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)

    req = runner.CreateJobRequest(
        collection_slug="camera-collection-archive",
        workflow_mode="collection_archive",
        groups={
            "video": {
                "archive_mode": "av1_nvenc",
                "tasks": ["archive_video"],
            },
        },
        profile_routing={
            "sidecars": [
                {
                    "id": "camera_xml",
                    "format": "xml",
                    "path": "{parent}/{stem}M01.XML",
                    "primary": {"path": {"suffix": ".mp4"}},
                    "facts": {
                        "source": "exiftool",
                        "tags": ["VendorGroup", "VendorFieldName", "VendorFieldValue"],
                        "extractors": [
                            {
                                "type": "name_value",
                                "name_tag": "VendorFieldName",
                                "value_tag": "VendorFieldValue",
                                "requires": [{"tag": "VendorGroup", "contains": "Gps"}],
                                "fields": {"Latitude": "exif.gps_latitude"},
                            }
                        ],
                    },
                }
            ],
            "routes": [
                {
                    "id": "sidecar-camera-video",
                    "group": "video",
                    "when": {
                        "fact": "sidecars.camera_xml.facts.exif.gps_latitude",
                        "exists": True,
                    },
                }
            ],
        },
    )

    routing = req.profile_routing.model_dump(exclude_none=True)

    assert routing["sidecars"][0]["facts"]["extractors"] == [
        {
            "type": "name_value",
            "name_tag": "VendorFieldName",
            "value_tag": "VendorFieldValue",
            "requires": [{"tag": "VendorGroup", "contains": "Gps"}],
            "fields": {"Latitude": "exif.gps_latitude"},
        }
    ]


def test_completed_structured_file_routes_by_path_without_full_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    shared_file = runner.shared_input_upload_root("upload-1") / "front-door/clip.mp4"
    shared_file.parent.mkdir(parents=True, exist_ok=True)
    shared_file.write_bytes(b"video")
    upload = runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "structured_routing": True,
                "groups": {
                    "video": {"archive_mode": "av1_nvenc", "tasks": ["archive_video"]},
                    "preserve": {"archive_mode": "preserve", "tasks": []},
                },
            },
            "files": [
                {
                    "path": "front-door/clip.mp4",
                    "bytes": 5,
                    "upload_id": "front-door-clip",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                },
                {
                    "path": "front-door/pending.mp4",
                    "bytes": 5,
                    "upload_id": "front-door-pending",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                },
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "profile_routing": {
            "routes": [
                {
                    "id": "front-door-video",
                    "group": "video",
                    "when": {"path": {"prefix": "front-door", "suffix": ".mp4"}},
                }
            ]
        },
    }
    groups = {
        "video": {"archive_mode": "av1_nvenc", "tasks": ["archive_video"]},
        "preserve": {"archive_mode": "preserve", "tasks": []},
    }

    routed = runner.route_completed_input_files(job, upload, groups)
    video_files = runner.upload_files_for_groups(routed, {"video"})

    assert [item["path"] for item in video_files] == ["front-door/clip.mp4"]
    assert video_files[0]["resolved_group"] == "video"
    assert video_files[0]["resolved_group_rel"] == "front-door/clip.mp4"
    assert runner.upload_file_group_rel_for_state(video_files[0], "video") == Path(
        "front-door/clip.mp4"
    )


def test_profile_routing_manifest_records_actual_route_and_output(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    shared_file = runner.shared_input_upload_root("upload-1") / "phone/IMG_0001.MOV"
    shared_file.parent.mkdir(parents=True, exist_ok=True)
    shared_file.write_bytes(b"video")
    upload = runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "structured_routing": True,
                "groups": {
                    "video": {"archive_mode": "av1_nvenc", "tasks": ["archive_video"]},
                },
            },
            "files": [
                {
                    "path": "phone/IMG_0001.MOV",
                    "bytes": 5,
                    "sha256": "0" * 64,
                    "upload_id": "phone-img-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                }
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "collection_slug": "phone-collection-archive",
        "collection_timestamp": "20260628T000000Z",
        "profile_routing": {
            "routes": [
                {
                    "id": "iphone-video",
                    "group": "video",
                    "into": "iphone/video",
                    "when": {"path": {"prefix": "phone", "suffix": ".mov"}},
                }
            ]
        },
    }
    groups = {
        "video": {
            "archive_mode": "av1_nvenc",
            "tasks": ["archive_video"],
            "encode_profile": {"archive": {"container": "webm"}},
        }
    }

    routed = runner.route_completed_input_files(job, upload, groups)
    archive_dir = tmp_path / "archive"
    output = runner.archive_output_for_upload_file(
        runner.upload_files_for_groups(routed, {"video"})[0],
        group_name="video",
        group_config=groups["video"],
        archive_dir=archive_dir,
    )
    output.parent.mkdir(parents=True)
    output.write_bytes(b"webm")

    runner.write_profile_routing_manifest(job, routed, groups, archive_dir)

    manifest = json.loads((archive_dir / runner.ROUTING_MANIFEST_FILENAME).read_text())
    assert manifest["schema"] == "munchy.profile-routing-manifest"
    assert manifest["job_id"] == "job-1"
    assert manifest["files"] == [
        {
            "output": {
                "bytes": 4,
                "exists": True,
                "path": "video/iphone/video/IMG_0001.webm",
            },
            "route": {
                "action": "upload",
                "group": "video",
                "group_rel_path": "iphone/video/IMG_0001.MOV",
                "id": "iphone-video",
            },
            "source": {
                "bytes": 5,
                "path": "phone/IMG_0001.MOV",
                "sha256": "0" * 64,
            },
        }
    ]


def test_profile_routing_manifest_records_sidecar_evidence_without_fake_output(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    shared_root = runner.shared_input_upload_root("upload-1") / "phone"
    shared_root.mkdir(parents=True, exist_ok=True)
    (shared_root / "IMG_0001.MOV").write_bytes(b"video")
    (shared_root / "IMG_0001.MOV.xmp").write_text("<xmpmeta />", encoding="utf-8")
    upload = runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "structured_routing": True,
                "groups": {
                    "video": {"archive_mode": "av1_nvenc", "tasks": ["archive_video"]},
                },
            },
            "files": [
                {
                    "path": "phone/IMG_0001.MOV",
                    "bytes": 5,
                    "sha256": "0" * 64,
                    "upload_id": "phone-img-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                },
                {
                    "path": "phone/IMG_0001.MOV.xmp",
                    "bytes": 11,
                    "sha256": "1" * 64,
                    "upload_id": "phone-xmp-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                },
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "collection_slug": "phone-collection-archive",
        "collection_timestamp": "20260628T000000Z",
        "profile_routing": {
            "sidecars": [
                {
                    "id": "xmp",
                    "format": "xmp",
                    "primary": {"path": {"suffix": ".mov"}},
                }
            ],
            "routes": [
                {
                    "id": "iphone-video",
                    "group": "video",
                    "into": "iphone/video",
                    "when": {"path": {"prefix": "phone", "suffix": ".mov"}},
                }
            ],
        },
    }
    groups = {
        "video": {
            "archive_mode": "av1_nvenc",
            "tasks": ["archive_video"],
            "encode_profile": {"archive": {"container": "webm"}},
        }
    }
    routed = runner.route_completed_input_files(job, upload, groups)
    archive_dir = tmp_path / "archive"
    output = runner.archive_output_for_upload_file(
        runner.upload_files_for_groups(routed, {"video"})[0],
        group_name="video",
        group_config=groups["video"],
        archive_dir=archive_dir,
    )
    output.parent.mkdir(parents=True)
    output.write_bytes(b"webm")

    runner.write_profile_routing_manifest(job, routed, groups, archive_dir)

    manifest = json.loads((archive_dir / runner.ROUTING_MANIFEST_FILENAME).read_text())
    files_by_source = {item["source"]["path"]: item for item in manifest["files"]}
    evidence = files_by_source["phone/IMG_0001.MOV.xmp"]
    assert evidence["route"]["action"] == "evidence"
    assert evidence["route"]["sidecar"] == {
        "id": "xmp",
        "format": "xmp",
        "for": "phone/IMG_0001.MOV",
    }
    assert evidence["output"] == {
        "kind": "none",
        "reason": "sidecar_evidence",
    }
    assert evidence["custody"] == {
        "kind": "source_artifact_sidecar",
        "primary_source": "phone/IMG_0001.MOV",
        "source_artifacts_path": "video/iphone/video/IMG_0001.webm.source-artifacts.tar.zst",
        "source_artifacts_entry": "sidecars/phone/IMG_0001.MOV.xmp",
    }


def test_metadata_projection_sidecars_written_for_archive_outputs(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    upload = runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "files": [
                {
                    "path": "video/IMG_0001.MOV",
                    "bytes": 5,
                    "upload_id": "phone-img-1",
                    "metadata_projection_metadata": {
                        "capture_date": "2026-06-28T20:30:40-07:00",
                        "capture_date_source": "exif.date_time_original",
                        "gps": {"latitude": 37.3317, "longitude": -122.0301},
                        "gps_source": "exif.gps_latitude+exif.gps_longitude",
                        "device": {
                            "make": "Apple",
                            "model": "iPhone SE (2nd generation)",
                        },
                        "creators": ["Nash Spence"],
                        "tags": [],
                    },
                    "profile_route_id": "iphone-video",
                    "resolved_group_rel": "iphone/video/IMG_0001.MOV",
                    "profile_pair_kind": "live-photo",
                    "profile_pair_role": "movie",
                }
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "collection_slug": "phone-collection-archive",
    }
    runner.save_job(job)
    groups = {
        "video": {
            "archive_mode": "av1_nvenc",
            "tasks": ["archive_video"],
            "encode_profile": {"archive": {"container": "webm"}},
            "metadata_projection": {
                "enabled": True,
                "device": {"make": "Apple", "model": "iPhone SE (2nd generation)"},
                "creators": ["Nash Spence"],
                "tags": ["iphone-se2"],
            },
        }
    }
    archive_dir = tmp_path / "archive"
    output = runner.archive_output_for_upload_file(
        upload["files"][0],
        group_name="video",
        group_config=groups["video"],
        archive_dir=archive_dir,
    )
    output.parent.mkdir(parents=True)
    output.write_bytes(b"webm")

    updated = runner.write_metadata_projection_sidecars(job, upload, groups, archive_dir)

    sidecar = output.with_name("IMG_0001.webm.xmp")
    assert sidecar.exists()
    xmp = sidecar.read_text(encoding="utf-8")
    assert 'exif:DateTimeOriginal="2026-06-28T20:30:40-07:00"' in xmp
    assert 'tiff:Make="Apple"' in xmp
    assert 'tiff:Model="iPhone SE (2nd generation)"' in xmp
    assert 'geo:lat="37.3317"' in xmp
    assert "<rdf:li>Nash Spence</rdf:li>" in xmp
    assert "<rdf:li>iphone-se2</rdf:li>" in xmp
    assert "<rdf:li>munchy/collection/phone-collection-archive</rdf:li>" in xmp
    assert "<rdf:li>munchy/group/video</rdf:li>" in xmp
    assert "<rdf:li>munchy/route/iphone-video</rdf:li>" in xmp
    assert "<rdf:li>munchy/output/iphone/video</rdf:li>" in xmp
    assert "<rdf:li>munchy/pair/live-photo/movie</rdf:li>" in xmp
    assert updated["files"][0]["metadata_projection_sidecar"] == (
        "video/iphone/video/IMG_0001.webm.xmp"
    )
    assert runner.load_job("job-1")["metadata_projection_result"]["sidecars"] == 1
    assert runner.routing_manifest_output_entry(output, archive_dir=archive_dir)[
        "metadata_sidecars"
    ] == [
        {
            "target": "immich_xmp",
            "path": "video/iphone/video/IMG_0001.webm.xmp",
            "bytes": sidecar.stat().st_size,
        }
    ]


def test_metadata_projection_sidecar_can_follow_eager_handoff_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    upload = runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "files": [
                {
                    "path": "video/IMG_0001.MOV",
                    "bytes": 5,
                    "upload_id": "phone-img-1",
                    "metadata_projection_metadata": {
                        "capture_date": "2026-06-28T20:30:40-07:00",
                        "capture_date_source": "exif.date_time_original",
                        "gps": {"latitude": 37.3317, "longitude": -122.0301},
                        "gps_source": "exif.gps_latitude+exif.gps_longitude",
                        "device": {
                            "make": "Apple",
                            "model": "iPhone SE (2nd generation)",
                        },
                        "creators": ["Nash Spence"],
                        "tags": [],
                    },
                    "profile_route_id": "iphone-video",
                    "resolved_group_rel": "iphone/video/IMG_0001.MOV",
                }
            ],
        }
    )
    groups = {
        "video": {
            "archive_mode": "av1_nvenc",
            "tasks": ["archive_video"],
            "encode_profile": {"archive": {"container": "webm"}},
            "metadata_projection": {
                "enabled": True,
                "device": {"make": "Apple", "model": "iPhone SE (2nd generation)"},
                "creators": ["Nash Spence"],
            },
        }
    }
    archive_dir = tmp_path / "archive"
    output = runner.archive_output_for_upload_file(
        upload["files"][0],
        group_name="video",
        group_config=groups["video"],
        archive_dir=archive_dir,
    )
    output_rel = output.relative_to(archive_dir).as_posix()
    runner.save_job(
        {
            "job_id": "job-1",
            "input_upload_id": "upload-1",
            "collection_slug": "phone-collection-archive",
            "riverhog": {"enabled": True},
            "riverhog_session_upload": {
                "state": "open",
                "files": {
                    output_rel: {
                        "path": output_rel,
                        "bytes": 4,
                        "uploaded_bytes": 4,
                        "state": "deleted",
                    }
                },
            },
        }
    )
    stale_job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "collection_slug": "phone-collection-archive",
        "riverhog": {"enabled": True},
    }

    updated = runner.write_metadata_projection_sidecars(stale_job, upload, groups, archive_dir)

    sidecar = output.with_name("IMG_0001.webm.xmp")
    assert not output.exists()
    assert sidecar.exists()
    assert stale_job["riverhog_session_upload"]["files"][output_rel]["state"] == "deleted"
    assert updated["files"][0]["metadata_projection_sidecar"] == (
        "video/iphone/video/IMG_0001.webm.xmp"
    )
    assert runner.load_job("job-1")["metadata_projection_result"]["sidecars"] == 1


def test_metadata_projection_still_fails_for_missing_unhanded_output(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    upload = runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "files": [
                {
                    "path": "video/IMG_0001.MOV",
                    "bytes": 5,
                    "metadata_projection_metadata": {
                        "capture_date": "2026-06-28T20:30:40-07:00",
                        "capture_date_source": "exif.date_time_original",
                        "gps": {"latitude": 37.3317, "longitude": -122.0301},
                        "gps_source": "exif.gps_latitude+exif.gps_longitude",
                        "device": {"make": "Apple", "model": "iPhone"},
                        "creators": ["Nash Spence"],
                    },
                    "resolved_group_rel": "iphone/video/IMG_0001.MOV",
                }
            ],
        }
    )
    job = {"job_id": "job-1", "input_upload_id": "upload-1"}
    groups = {
        "video": {
            "archive_mode": "av1_nvenc",
            "tasks": ["archive_video"],
            "encode_profile": {"archive": {"container": "webm"}},
            "metadata_projection": {"enabled": True},
        }
    }

    with pytest.raises(RuntimeError, match="metadata projection output is missing"):
        runner.write_metadata_projection_sidecars(job, upload, groups, tmp_path / "archive")


def test_metadata_projection_merges_existing_xmp_sidecar_for_preserve_outputs(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.tusd_data_path("phone-xmp-1").parent.mkdir(parents=True, exist_ok=True)
    runner.tusd_data_path("phone-xmp-1").write_text(
        """<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">
   <dc:subject><rdf:Bag><rdf:li>existing-tag</rdf:li></rdf:Bag></dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
""",
        encoding="utf-8",
    )
    upload = runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "files": [
                {
                    "path": "phone/IMG_0001.HEIC",
                    "bytes": 5,
                    "upload_id": "phone-img-1",
                    "metadata_projection_metadata": {
                        "capture_date": "2026-06-28T20:30:40-07:00",
                        "capture_date_source": "exif.date_time_original",
                        "gps": {"latitude": 37.3317, "longitude": -122.0301},
                        "gps_source": "exif.gps_latitude+exif.gps_longitude",
                        "device": {
                            "make": "Apple",
                            "model": "iPhone SE (2nd generation)",
                        },
                        "creators": ["Nash Spence"],
                        "tags": [],
                    },
                    "profile_route_id": "iphone-photo",
                    "resolved_group": "photos",
                    "resolved_group_rel": "IMG_0001.HEIC",
                },
                {
                    "path": "phone/IMG_0001.HEIC.xmp",
                    "bytes": 20,
                    "upload_id": "phone-xmp-1",
                    "profile_route_action": "evidence",
                    "profile_sidecar_id": "xmp",
                    "profile_sidecar_format": "xmp",
                    "profile_sidecar_for": "phone/IMG_0001.HEIC",
                    "resolved_group": "photos",
                    "resolved_group_rel": "IMG_0001.HEIC.xmp",
                },
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "collection_slug": "phone-collection-archive",
    }
    runner.save_job(job)
    groups = {
        "photos": {
            "archive_mode": "preserve",
            "tasks": [],
            "metadata_projection": {
                "enabled": True,
                "device": {"make": "Apple", "model": "iPhone SE (2nd generation)"},
                "creators": ["Nash Spence"],
            },
        }
    }
    archive_dir = tmp_path / "archive"
    output = archive_dir / "photos" / "IMG_0001.HEIC"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"heic")

    updated = runner.write_metadata_projection_sidecars(job, upload, groups, archive_dir)

    sidecar = output.with_name("IMG_0001.HEIC.xmp")
    assert sidecar.exists()
    xmp = sidecar.read_text(encoding="utf-8")
    assert "<rdf:li>existing-tag</rdf:li>" in xmp
    assert 'exif:DateTimeOriginal="2026-06-28T20:30:40-07:00"' in xmp
    assert 'tiff:Make="Apple"' in xmp
    assert "<rdf:li>munchy/collection/phone-collection-archive</rdf:li>" in xmp
    assert updated["files"][0]["metadata_projection_sidecar"] == "photos/IMG_0001.HEIC.xmp"
    assert "metadata_projection_sidecar" not in updated["files"][1]


def test_source_artifact_entries_include_sidecar_evidence_for_reencodes(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    materialized_group_root = tmp_path / "input" / "video"
    primary = materialized_group_root / "iphone" / "IMG_0001.MOV"
    sidecar = materialized_group_root / "iphone" / "IMG_0001.MOV.xmp"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"mov")
    sidecar.write_text("<xmpmeta />", encoding="utf-8")
    primary_state = {
        "path": "phone/IMG_0001.MOV",
        "resolved_group": "video",
        "resolved_group_rel": "iphone/IMG_0001.MOV",
    }
    upload = {
        "files": [
            primary_state,
            {
                "path": "phone/IMG_0001.MOV.xmp",
                "profile_route_action": "evidence",
                "profile_sidecar_id": "xmp",
                "profile_sidecar_format": "xmp",
                "profile_sidecar_for": "phone/IMG_0001.MOV",
                "resolved_group": "video",
                "resolved_group_rel": "iphone/IMG_0001.MOV.xmp",
            },
        ]
    }

    entries = runner.source_artifacts_sidecar_entries(
        upload,
        [primary_state],
        group_name="video",
        materialized_group_root=materialized_group_root,
    )

    assert entries == {
        "iphone/IMG_0001.MOV": [
            {
                "id": "xmp",
                "format": "xmp",
                "path": str(sidecar),
                "arcname": "sidecars/iphone/IMG_0001.MOV.xmp",
                "source_rel_path": "phone/IMG_0001.MOV.xmp",
            }
        ]
    }


def test_metadata_projection_can_use_uploaded_filesystem_birthtime(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    source = tmp_path / "REC_20260628_203040.WAV"
    source.write_bytes(b"wav")
    monkeypatch.setattr(runner, "ffprobe_for_routing", lambda _path: {"format": {}, "streams": []})
    monkeypatch.setattr(runner, "exiftool_for_routing", lambda _path, **_kwargs: {})

    metadata = runner.projection_metadata_from_source(
        "voice/REC_20260628_203040.WAV",
        source,
        group_config={
            "metadata_projection": {
                "allow_missing_gps": True,
                "device": {"make": "eSonic", "model": "MEMOQ SR-600"},
                "creators": ["Nash Spence"],
                "capture_date_sources": [
                    {"type": "embedded"},
                    {"type": "filesystem_birthtime"},
                ],
            }
        },
        filesystem_metadata={
            "stat": {
                "birthtime": "2026-06-28T20:30:40+00:00",
                "size": 3,
            }
        },
    )

    assert metadata.capture_date == "2026-06-28T20:30:40+00:00"
    assert metadata.capture_date_source == "filesystem_birthtime:source_birthtime"


def test_metadata_projection_uses_declared_non_xmp_sidecar_facts(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.tusd_data_path("camera-video-1").parent.mkdir(parents=True, exist_ok=True)
    primary_path = runner.tusd_data_path("camera-video-1")
    sidecar_path = runner.tusd_data_path("camera-xml-1")
    primary_path.write_bytes(b"video")
    sidecar_path.write_text("<metadata />", encoding="utf-8")
    exiftool_calls: list[tuple[Path, tuple[str, ...]]] = []

    monkeypatch.setattr(
        runner,
        "ffprobe_for_routing",
        lambda _path: {"format": {}, "streams": []},
    )

    def fake_exiftool(path: Path, *, tags=()):  # type: ignore[no-untyped-def]
        exiftool_calls.append((path, tuple(tags)))
        if path == sidecar_path:
            assert tuple(tags) == (
                "VendorSidecarCreationDate",
                "VendorSidecarGroupName",
                "VendorSidecarFieldName",
                "VendorSidecarFieldValue",
            )
            return {
                "VendorSidecarCreationDate": "2026:07:08 15:46:25-07:00",
                "VendorSidecarGroupName": [
                    "GpsGroup",
                    "CameraUnitMetadataSet",
                ],
                "VendorSidecarFieldName": [
                    "VersionID",
                    "LatitudeRef",
                    "Latitude",
                    "LongitudeRef",
                    "Longitude",
                    "DateStamp",
                    "TimeStamp",
                    "Status",
                    "CaptureGammaEquation",
                ],
                "VendorSidecarFieldValue": [
                    "2.2.0.0",
                    "N",
                    "48;59;58.213",
                    "W",
                    "122;44;25.579",
                    "2026:07:08",
                    "22:46:21.000",
                    "A",
                    "rec709",
                ],
            }
        return {}

    monkeypatch.setattr(runner, "exiftool_for_routing", fake_exiftool)
    upload = {
        "upload_id": "upload-1",
        "files": [
            {
                "path": "camera/C0001.MP4",
                "bytes": 5,
                "upload_id": "camera-video-1",
                "resolved_group": "video",
                "resolved_group_rel": "camera/C0001.MP4",
            },
            {
                "path": "camera/C0001M01.XML",
                "bytes": 12,
                "upload_id": "camera-xml-1",
                "profile_route_action": "evidence",
                "profile_sidecar_id": "camera_xml",
                "profile_sidecar_format": "xml",
                "profile_sidecar_for": "camera/C0001.MP4",
                "resolved_group": "video",
                "resolved_group_rel": "camera/C0001M01.XML",
            },
        ],
    }
    job = {
        "job_id": "job-1",
        "profile_routing": {
            "sidecars": [
                {
                    "id": "camera_xml",
                    "format": "xml",
                    "paths": ["{parent}/{stem}M01.XML"],
                    "primary": {"path": {"suffix": ".mp4"}},
                    "facts": {
                        "source": "exiftool",
                        "tags": [
                            "VendorSidecarCreationDate",
                            "VendorSidecarGroupName",
                            "VendorSidecarFieldName",
                            "VendorSidecarFieldValue",
                        ],
                        "extractors": [
                            {
                                "type": "name_value",
                                "name_tag": "VendorSidecarFieldName",
                                "value_tag": "VendorSidecarFieldValue",
                                "requires": [
                                    {
                                        "tag": "VendorSidecarGroupName",
                                        "contains": "GpsGroup",
                                    }
                                ],
                                "fields": {
                                    "Latitude": "exif.gps_latitude",
                                    "LatitudeRef": "exif.gps_latitude_ref",
                                    "Longitude": "exif.gps_longitude",
                                    "LongitudeRef": "exif.gps_longitude_ref",
                                },
                            }
                        ],
                    },
                }
            ],
            "routes": [
                {
                    "id": "camera-video",
                    "group": "video",
                    "when": {"path": {"suffix": ".mp4"}},
                }
            ],
        },
    }
    group_config = {
        "metadata_projection": {
            "allow_missing_gps": True,
            "device": {"make": "Example", "model": "Camera"},
            "creators": ["Example Operator"],
            "capture_date_sources": [
                {
                    "type": "sidecar",
                    "id": "camera_xml",
                    "fact": "exiftool.tags.vendor_sidecar_creation_date",
                },
            ],
            "gps_sources": [
                {"type": "sidecar", "id": "camera_xml"},
                {"type": "embedded"},
            ],
        }
    }

    changed = runner.ensure_file_projection_metadata(
        upload,
        upload["files"][0],
        job=job,
        group_config=group_config,
    )

    assert changed is True
    metadata = upload["files"][0]["metadata_projection_metadata"]
    assert metadata["capture_date"] == "2026-07-08T15:46:25-07:00"
    assert (
        metadata["capture_date_source"]
        == "sidecar:camera_xml:exiftool.tags.vendor_sidecar_creation_date"
    )
    assert metadata["gps"]["latitude"] == pytest.approx(48.99950361)
    assert metadata["gps"]["longitude"] == pytest.approx(-122.74043861)
    assert (
        metadata["gps_source"]
        == "sidecar:camera_xml:exif.gps_latitude+exif.gps_longitude"
    )
    assert (
        sidecar_path,
        (
            "VendorSidecarCreationDate",
            "VendorSidecarGroupName",
            "VendorSidecarFieldName",
            "VendorSidecarFieldValue",
        ),
    ) in exiftool_calls


def test_metadata_projection_can_use_configured_gps(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    source = tmp_path / "RecM0A_20260628_203040_203100_0_main.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(runner, "ffprobe_for_routing", lambda _path: {"format": {}, "streams": []})
    monkeypatch.setattr(
        runner,
        "exiftool_for_routing",
        lambda _path, **_kwargs: {"DateTimeOriginal": "2026:06:28 20:30:40Z"},
    )

    metadata = runner.projection_metadata_from_source(
        "camera/RecM0A_20260628_203040_203100_0_main.mp4",
        source,
        group_config={
            "metadata_projection": {
                "gps": {
                    "latitude": 48.999527523960296,
                    "longitude": -122.74040765142755,
                },
                "device": {"make": "Reolink", "model": "E1 Pro"},
                "creators": ["Nash Spence"],
            }
        },
    )

    assert metadata.gps is not None
    assert metadata.gps.latitude == pytest.approx(48.999527523960296)
    assert metadata.gps.longitude == pytest.approx(-122.74040765142755)
    assert metadata.gps_source == "metadata_projection.gps"


def test_metadata_projection_path_regex_fallback_survives_exiftool_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    source = tmp_path / "Back Yard_00_20260630150631.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(runner, "ffprobe_for_routing", lambda _path: {"format": {}, "streams": []})

    def fail_exiftool(path: Path, **_kwargs: object) -> dict[str, object]:
        raise runner.RoutingFailed(f"exiftool failed for {path.name}")

    monkeypatch.setattr(runner, "exiftool_for_routing", fail_exiftool)

    metadata = runner.projection_metadata_from_source(
        "back-yard-camera/Back Yard_00_20260630150631.mp4",
        source,
        group_config={
            "metadata_projection": {
                "capture_date_sources": [
                    {"type": "embedded"},
                    {
                        "type": "path_regex",
                        "name": "reolink_camera_filename",
                        "pattern": r"(?:^|/)[^/]+_[0-9]+_(?P<stamp>[0-9]{14})\.mp4$",
                        "datetime_group": "stamp",
                        "format": "%Y%m%d%H%M%S",
                        "timezone": "America/Los_Angeles",
                    },
                ],
                "gps": {
                    "latitude": 48.999527523960296,
                    "longitude": -122.74040765142755,
                },
                "device": {"make": "Reolink", "model": "Duo 3V PoE"},
                "creators": ["Nash Spence"],
            }
        },
    )

    assert metadata.capture_date == "2026-06-30T15:06:31-07:00"
    assert metadata.capture_date_source == "path_regex:reolink_camera_filename"


def test_audio_container_metadata_args_write_capture_date_and_gps(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    metadata = runner.project_immich_metadata(
        {
            "ffprobe.format_tags.creation_time": "2026-06-28T20:30:40-07:00",
            "ffprobe.format_tags.location": "+37.331700-122.030100+19.500/",
        },
        device_make="Apple",
        device_model="iPhone SE (2nd generation)",
        creators=["Nash Spence"],
    )

    assert runner.audio_container_metadata_args(metadata) == [
        "-metadata",
        "DATE=2026-06-28T20:30:40-07:00",
        "-metadata",
        "creation_time=2026-06-28T20:30:40-07:00",
        "-metadata",
        "ARTIST=Nash Spence",
        "-metadata",
        "CREATOR=Nash Spence",
        "-metadata",
        "MAKE=Apple",
        "-metadata",
        "MODEL=iPhone SE (2nd generation)",
        "-metadata",
        "LOCATION=+37.3317-122.0301+19.5/",
        "-metadata",
        "GPSLatitude=37.3317",
        "-metadata",
        "GPSLongitude=-122.0301",
        "-metadata",
        "GPSAltitude=19.5",
    ]


def test_gpu_payload_carries_required_projected_container_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    group_config = {
        "archive_mode": "av1_nvenc",
        "tasks": ["archive_video"],
        "metadata_projection": {
            "device": {"make": "Apple", "model": "iPhone SE (2nd generation)"},
            "creators": ["Nash Spence"],
        },
    }
    file_state = {
        "path": "camera/IMG_0001.MOV",
        "bytes": 5,
        "upload_id": "upload-a",
        "metadata_projection_metadata": {
            "capture_date": "2026-06-28T20:30:40-07:00",
            "capture_date_source": "exif.date_time_original",
            "gps": {"latitude": 37.3317, "longitude": -122.0301},
            "gps_source": "exif.gps_latitude+exif.gps_longitude",
            "device": {"make": "Apple", "model": "iPhone SE (2nd generation)"},
            "creators": ["Nash Spence"],
            "tags": [],
        },
    }

    container_metadata, changed = runner.container_metadata_for_gpu_payload(
        {"job_id": "job-1"},
        {"upload_id": "upload-1", "files": [file_state]},
        [file_state],
        group_name="camera",
        group_config=group_config,
        tasks=["archive_video"],
    )
    payload = runner.build_eager_gpu_payload(
        {"job_id": "job-1", "collection_slug": "phone-collection-archive"},
        batch_id="batch-1",
        group_name="camera",
        group_config=group_config,
        tasks=["archive_video"],
        container_metadata=container_metadata,
    )

    assert changed is False
    assert payload["container_metadata_required"] is True
    assert payload["container_metadata"]["IMG_0001.MOV"]["device"] == {
        "make": "Apple",
        "model": "iPhone SE (2nd generation)",
    }
    assert payload["container_metadata"]["IMG_0001.MOV"]["creators"] == ["Nash Spence"]


def test_eager_gpu_projection_reads_materialized_original_name(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    tusd_source = runner.tusd_data_path("upload-a")
    tusd_source.parent.mkdir(parents=True, exist_ok=True)
    tusd_source.write_bytes(b"video")
    upload = runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "archive_mode": "av1_nvenc",
                "tasks": ["archive_video"],
                "groups": {
                    "camera": {"archive_mode": "av1_nvenc", "tasks": ["archive_video"]}
                },
            },
            "files": [
                {
                    "path": "camera/Back Yard_00_20260630123456.mp4",
                    "bytes": 5,
                    "upload_id": "upload-a",
                }
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "collection_slug": "camera-archive",
        "collection_timestamp": "20260101T000000Z",
    }
    runner.save_job(job)
    group_config = {
        "archive_mode": "av1_nvenc",
        "tasks": ["archive_video"],
        "metadata_projection": {
            "gps": {"latitude": 48.9995, "longitude": -122.7404},
            "device": {"make": "Reolink", "model": "Duo 3V PoE"},
            "creators": ["Nash Spence"],
        },
    }
    payloads: list[dict[str, object]] = []
    exiftool_paths: list[Path] = []

    monkeypatch.setattr(runner, "ffprobe_for_routing", lambda _path: {"format": {}, "streams": []})

    def fake_exiftool(path: Path, **_kwargs: object) -> dict[str, object]:
        exiftool_paths.append(path)
        if path == tusd_source:
            raise runner.RoutingFailed("extensionless tusd backing file was inspected")
        return {"DateTimeOriginal": "2026:06:30 12:34:56-0700"}

    monkeypatch.setattr(runner, "exiftool_for_routing", fake_exiftool)
    monkeypatch.setattr(runner, "start_gpu_job", lambda payload: payloads.append(payload))

    updated = runner.start_eager_gpu_batch(
        job,
        upload,
        group_name="camera",
        group_config=group_config,
        file_states=[upload["files"][0]],
        archive_dir=tmp_path / "archive",
        space_checked=True,
    )

    assert len(exiftool_paths) == 1
    assert exiftool_paths[0] != tusd_source
    assert exiftool_paths[0].name == "Back Yard_00_20260630123456.mp4"
    assert "eager-input" in exiftool_paths[0].parts
    assert payloads[0]["container_metadata"]["Back Yard_00_20260630123456.mp4"][
        "capture_date"
    ] == "2026-06-30T12:34:56-07:00"
    assert updated["files"][0]["metadata_projection_metadata"]["capture_date"] == (
        "2026-06-30T12:34:56-07:00"
    )


def test_routed_structured_file_stays_complete_after_materialized_tusd_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    upload_id = "upload-1"
    file_state = {
        "path": "phone/IMG_0001.HEIC",
        "bytes": 5,
        "upload_id": "phone-img-0001",
        "input_upload_id": upload_id,
        "structured_routing": True,
        "resolved_group": "phone-preserve",
        "resolved_group_rel": "iphone-se2/photo/rear-heic/IMG_0001.HEIC",
    }
    materialized = runner.shared_input_upload_root(upload_id) / (
        "phone-preserve/iphone-se2/photo/rear-heic/IMG_0001.HEIC"
    )
    materialized.parent.mkdir(parents=True, exist_ok=True)
    materialized.write_bytes(b"image")

    status = runner.upload_file_status(file_state)

    assert status["complete"] is True
    assert status["upload_state"] == "uploaded"
    assert status["uploaded_bytes"] == 5


def test_completed_structured_file_can_route_by_probe_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    shared_file = runner.shared_input_upload_root("upload-1") / "phone/IMG_0001.MOV"
    shared_file.parent.mkdir(parents=True, exist_ok=True)
    shared_file.write_bytes(b"video")
    upload = runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "structured_routing": True,
                "groups": {"phone-video": {"archive_mode": "av1_nvenc", "tasks": []}},
            },
            "files": [
                {
                    "path": "phone/IMG_0001.MOV",
                    "bytes": 5,
                    "upload_id": "phone-img-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                }
            ],
        }
    )

    def fake_ffprobe_for_routing(path: Path) -> dict[str, object]:
        assert path == shared_file
        return {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "tags": {"make": "Apple"}},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "width": 3840,
                    "height": 2160,
                    "avg_frame_rate": "60000/1001",
                    "tags": {"handler_name": "Core Media Video"},
                }
            ],
        }

    monkeypatch.setattr(runner, "ffprobe_for_routing", fake_ffprobe_for_routing)
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "profile_routing": {
            "routes": [
                {
                    "id": "iphone-4k",
                    "group": "phone-video",
                    "when": {
                        "all": [
                            {"path": {"prefix": "phone", "suffix": ".mov"}},
                            {"fact": "video.long_edge", "min": 3000},
                            {"fact": "ffprobe.format_tags.make", "equals": "apple"},
                        ]
                    },
                }
            ]
        },
    }
    groups = {"phone-video": {"archive_mode": "av1_nvenc", "tasks": []}}

    routed = runner.route_completed_input_files(job, upload, groups)

    assert (
        runner.upload_files_for_groups(routed, {"phone-video"})[0]["profile_route_id"]
        == "iphone-4k"
    )


def test_sidecar_evidence_is_not_probed_during_runner_routing(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    video_file = runner.shared_input_upload_root("upload-1") / "phone/IMG_0001.MOV"
    sidecar_file = runner.shared_input_upload_root("upload-1") / "phone/IMG_0001.MOV.xmp"
    video_file.parent.mkdir(parents=True, exist_ok=True)
    video_file.write_bytes(b"video")
    sidecar_file.write_text("<xmpmeta />", encoding="utf-8")
    upload = runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "structured_routing": True,
                "groups": {"phone-video": {"archive_mode": "av1_nvenc", "tasks": []}},
            },
            "files": [
                {
                    "path": "phone/IMG_0001.MOV",
                    "bytes": 5,
                    "upload_id": "phone-img-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                },
                {
                    "path": "phone/IMG_0001.MOV.xmp",
                    "bytes": 11,
                    "upload_id": "phone-xmp-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                },
            ],
        }
    )

    probed: list[Path] = []

    def fake_ffprobe_for_routing(path: Path) -> dict[str, object]:
        probed.append(path)
        assert path == video_file
        return {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "width": 3840,
                    "height": 2160,
                    "avg_frame_rate": "30/1",
                }
            ],
        }

    monkeypatch.setattr(runner, "ffprobe_for_routing", fake_ffprobe_for_routing)
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "profile_routing": {
            "sidecars": [
                {
                    "id": "xmp",
                    "format": "xmp",
                    "primary": {"path": {"suffix": ".mov"}},
                }
            ],
            "routes": [
                {
                    "id": "iphone-4k",
                    "group": "phone-video",
                    "when": {
                        "all": [
                            {"path": {"prefix": "phone", "suffix": ".mov"}},
                            {"fact": "video.long_edge", "min": 3000},
                        ]
                    },
                }
            ],
        },
    }
    groups = {"phone-video": {"archive_mode": "av1_nvenc", "tasks": []}}

    routed = runner.route_completed_input_files(job, upload, groups)

    assert probed == [video_file]
    files_by_path = {item["path"]: item for item in routed["files"]}
    assert files_by_path["phone/IMG_0001.MOV"]["profile_route_id"] == "iphone-4k"
    assert files_by_path["phone/IMG_0001.MOV.xmp"]["profile_route_action"] == "evidence"
    assert files_by_path["phone/IMG_0001.MOV.xmp"]["profile_sidecar_id"] == "xmp"
    assert files_by_path["phone/IMG_0001.MOV.xmp"]["profile_sidecar_for"] == (
        "phone/IMG_0001.MOV"
    )


def test_sidecar_facts_are_bounded_during_runner_routing(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    video_file = runner.shared_input_upload_root("upload-1") / "camera/C0001.MP4"
    sidecar_file = runner.shared_input_upload_root("upload-1") / "camera/C0001M01.XML"
    video_file.parent.mkdir(parents=True, exist_ok=True)
    video_file.write_bytes(b"video")
    sidecar_file.write_text("<metadata />", encoding="utf-8")
    upload = runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "structured_routing": True,
                "groups": {"video": {"archive_mode": "av1_nvenc", "tasks": []}},
            },
            "files": [
                {
                    "path": "camera/C0001.MP4",
                    "bytes": 5,
                    "upload_id": "camera-video-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                },
                {
                    "path": "camera/C0001M01.XML",
                    "bytes": 12,
                    "upload_id": "camera-xml-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                },
            ],
        }
    )
    exiftool_calls: list[tuple[Path, tuple[str, ...]]] = []

    def fail_ffprobe_for_routing(path: Path) -> dict[str, object]:
        raise AssertionError(f"ffprobe should not run for sidecar-facts-only routing: {path}")

    def fake_exiftool_for_routing(path: Path, *, tags=None):  # type: ignore[no-untyped-def]
        exiftool_calls.append((path, tuple(tags or ())))
        assert path == sidecar_file
        return {
            "EXIF:Make": "Example Imaging",
            "EXIF:Model": "Synthetic Camera",
        }

    monkeypatch.setattr(runner, "ffprobe_for_routing", fail_ffprobe_for_routing)
    monkeypatch.setattr(runner, "exiftool_for_routing", fake_exiftool_for_routing)
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "profile_routing": {
            "sidecars": [
                {
                    "id": "camera_xml",
                    "format": "xml",
                    "path": "{parent}/{stem}M01.XML",
                    "primary": {"path": {"suffix": ".mp4"}},
                    "facts": {"source": "exiftool", "tags": ["Make", "Model"]},
                }
            ],
            "routes": [
                {
                    "id": "sidecar-camera-video",
                    "group": "video",
                    "when": {
                        "all": [
                            {"path": {"suffix": ".mp4"}},
                            {
                                "fact": "sidecars.camera_xml.facts.exif.make",
                                "equals": "example imaging",
                            },
                        ]
                    },
                }
            ],
        },
    }

    routed = runner.route_completed_input_files(
        job,
        upload,
        {"video": {"archive_mode": "av1_nvenc", "tasks": []}},
    )

    files_by_path = {item["path"]: item for item in routed["files"]}
    assert files_by_path["camera/C0001.MP4"]["profile_route_id"] == "sidecar-camera-video"
    assert files_by_path["camera/C0001M01.XML"]["profile_route_action"] == "evidence"
    assert files_by_path["camera/C0001M01.XML"]["profile_sidecar_id"] == "camera_xml"
    assert exiftool_calls == [(sidecar_file, ("Make", "Model"))]


def test_sidecar_facts_unlock_primary_exiftool_during_runner_routing(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    video_file = runner.shared_input_upload_root("upload-1") / "camera/C0001.MP4"
    sidecar_file = runner.shared_input_upload_root("upload-1") / "camera/C0001M01.XML"
    video_file.parent.mkdir(parents=True, exist_ok=True)
    video_file.write_bytes(b"video")
    sidecar_file.write_text("<metadata />", encoding="utf-8")
    upload = runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "structured_routing": True,
                "groups": {"video": {"archive_mode": "av1_nvenc", "tasks": []}},
            },
            "files": [
                {
                    "path": "camera/C0001.MP4",
                    "bytes": 5,
                    "upload_id": "camera-video-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                },
                {
                    "path": "camera/C0001M01.XML",
                    "bytes": 12,
                    "upload_id": "camera-xml-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                },
            ],
        }
    )
    exiftool_calls: list[tuple[Path, tuple[str, ...]]] = []

    def fail_ffprobe_for_routing(path: Path) -> dict[str, object]:
        raise AssertionError(f"ffprobe should not run for exiftool-only routing: {path}")

    def fake_exiftool_for_routing(path: Path, *, tags=None):  # type: ignore[no-untyped-def]
        exiftool_calls.append((path, tuple(tags or ())))
        if path == sidecar_file:
            return {"EXIF:Make": "Example Imaging"}
        if path == video_file:
            assert "VideoAvgBitrate" in tuple(tags or ())
            return {"QuickTime:VideoAvgBitrate": "200 Mbps"}
        raise AssertionError(f"unexpected exiftool path: {path}")

    monkeypatch.setattr(runner, "ffprobe_for_routing", fail_ffprobe_for_routing)
    monkeypatch.setattr(runner, "exiftool_for_routing", fake_exiftool_for_routing)
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "profile_routing": {
            "extra_exiftool_tags": ["VideoAvgBitrate"],
            "sidecars": [
                {
                    "id": "camera_xml",
                    "format": "xml",
                    "path": "{parent}/{stem}M01.XML",
                    "primary": {"path": {"suffix": ".mp4"}},
                    "facts": {"source": "exiftool", "tags": ["Make"]},
                }
            ],
            "routes": [
                {
                    "id": "sidecar-and-bitrate-video",
                    "group": "video",
                    "when": {
                        "all": [
                            {"path": {"suffix": ".mp4"}},
                            {
                                "fact": "sidecars.camera_xml.facts.exif.make",
                                "equals": "example imaging",
                            },
                            {
                                "fact": "exiftool.tags.video_avg_bitrate",
                                "equals": "200 Mbps",
                            },
                        ]
                    },
                }
            ],
        },
    }

    routed = runner.route_completed_input_files(
        job,
        upload,
        {"video": {"archive_mode": "av1_nvenc", "tasks": []}},
    )

    files_by_path = {item["path"]: item for item in routed["files"]}
    assert files_by_path["camera/C0001.MP4"]["profile_route_id"] == (
        "sidecar-and-bitrate-video"
    )
    assert files_by_path["camera/C0001M01.XML"]["profile_route_action"] == "evidence"
    assert [path for path, _tags in exiftool_calls] == [sidecar_file, video_file]
    assert exiftool_calls[0] == (sidecar_file, ("Make",))


def test_profile_routing_skips_expensive_tools_for_path_only_runner_route(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    state_file = runner.shared_input_upload_root("upload-1") / "camera/leinfo.sav"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_bytes(b"state")
    upload = runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "structured_routing": True,
                "groups": {"state": {"archive_mode": "preserve", "tasks": []}},
            },
            "files": [
                {
                    "path": "camera/leinfo.sav",
                    "bytes": 5,
                    "upload_id": "state-1",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                },
            ],
        }
    )

    def fail_ffprobe_for_routing(path: Path) -> dict[str, object]:
        raise AssertionError(f"unexpected ffprobe call for {path}")

    def fail_exiftool_for_routing(path: Path, *, tags=None):  # type: ignore[no-untyped-def]
        raise AssertionError(f"unexpected exiftool call for {path} with {tags}")

    monkeypatch.setattr(runner, "ffprobe_for_routing", fail_ffprobe_for_routing)
    monkeypatch.setattr(runner, "exiftool_for_routing", fail_exiftool_for_routing)
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "profile_routing": {
            "routes": [
                {
                    "id": "device-state",
                    "group": "state",
                    "when": {"path": {"filename_glob": "leinfo.sav"}},
                },
                {
                    "id": "camera-video",
                    "group": "video",
                    "when": {
                        "all": [
                            {"path": {"suffix": ".mp4"}},
                            {"fact": "video.codec", "equals": "hevc"},
                            {"fact": "exif.make", "equals": "example imaging"},
                        ]
                    },
                },
            ],
        },
    }

    routed = runner.route_completed_input_files(
        job,
        upload,
        {
            "state": {"archive_mode": "preserve", "tasks": []},
            "video": {"archive_mode": "av1_nvenc", "tasks": []},
        },
    )

    assert routed["files"][0]["profile_route_id"] == "device-state"
    assert routed["files"][0]["resolved_group"] == "state"


def test_profile_routing_preflight_uses_submitted_probe_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    client = TestClient(runner.app)

    response = client.post(
        "/v1/profile-routing/preflight",
        json={
            "files": [
                {
                    "path": "phone/IMG_0001.MOV",
                    "bytes": 123,
                    "probe_summary": {
                        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                        "duration": 1.5,
                        "has_audio": True,
                        "video_stream_count": 1,
                        "audio_stream_count": 1,
                        "codec_name": "hevc",
                        "video_codec_name": "hevc",
                        "width": 1440,
                        "height": 1080,
                        "fps": 30.0,
                        "audio_codec_name": "aac",
                        "format_tags": {"com.apple.quicktime.model": "iphone se"},
                    },
                }
            ],
            "groups": {
                "live-photo": {
                    "archive_mode": "preserve",
                    "tasks": [],
                }
            },
            "profile_routing": {
                "routes": [
                    {
                        "id": "iphone-live-photo-sidecar",
                        "group": "live-photo",
                        "when": {
                            "all": [
                                {"path": {"prefix": "phone", "suffix": ".mov"}},
                                {"fact": "video.codec", "equals": "hevc"},
                                {"fact": "audio.codec_name", "equals": "aac"},
                                {"fact": "ffprobe.duration", "max": 3.0},
                                {
                                    "fact": "ffprobe.format_tags.com.apple.quicktime.model",
                                    "contains": "iphone se",
                                },
                            ]
                        },
                    }
                ]
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["matches"] == [
        {
            "path": "phone/IMG_0001.MOV",
            "bytes": 123,
            "route_id": "iphone-live-photo-sidecar",
            "route_index": 0,
            "action": "upload",
            "pair_kind": None,
            "pairing_id": None,
            "pair_role": None,
            "pair_with": None,
            "matched_facts": {
                "audio.codec_name": "aac",
                "ffprobe.duration": 1.5,
                "ffprobe.format_tags.com.apple.quicktime.model": "iphone se",
                "video.codec": "hevc",
            },
            "group": "live-photo",
            "into": None,
            "collection_rel_path": "phone/IMG_0001.MOV",
        }
    ]


def test_profile_routing_preflight_uses_submitted_sidecar_facts(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    client = TestClient(runner.app)

    response = client.post(
        "/v1/profile-routing/preflight",
        json={
            "files": [
                {"path": "camera/C0001.MP4", "bytes": 100},
                {
                    "path": "camera/C0001M01.XML",
                    "bytes": 20,
                    "sidecar_facts": {
                        "exif.make": "example imaging",
                        "exiftool": {"tags": {"model": "Synthetic Camera"}},
                    },
                },
            ],
            "groups": {"video": {"archive_mode": "av1_nvenc", "tasks": []}},
            "profile_routing": {
                "sidecars": [
                    {
                        "id": "camera_xml",
                        "format": "xml",
                        "path": "{parent}/{stem}M01.XML",
                        "primary": {"path": {"suffix": ".mp4"}},
                        "facts": {"source": "exiftool", "tags": ["Make", "Model"]},
                    }
                ],
                "routes": [
                    {
                        "id": "sidecar-camera-video",
                        "group": "video",
                        "when": {
                            "all": [
                                {"path": {"suffix": ".mp4"}},
                                {
                                    "fact": "sidecars.camera_xml.facts.exif.make",
                                    "equals": "example imaging",
                                },
                                {
                                    "fact": "sidecars.camera_xml.facts.exiftool.tags.model",
                                    "equals": "Synthetic Camera",
                                },
                            ]
                        },
                    }
                ],
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert [item["action"] for item in payload["matches"]] == ["upload", "evidence"]
    assert payload["matches"][0]["matched_facts"] == {
        "sidecars.camera_xml.facts.exif.make": "example imaging",
        "sidecars.camera_xml.facts.exiftool.tags.model": "Synthetic Camera",
    }


def test_profile_routing_preflight_reports_missing_sidecar_facts(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    client = TestClient(runner.app)

    response = client.post(
        "/v1/profile-routing/preflight",
        json={
            "files": [
                {"path": "camera/C0001.MP4", "bytes": 100},
                {"path": "camera/C0001M01.XML", "bytes": 20},
            ],
            "groups": {"video": {"archive_mode": "av1_nvenc", "tasks": []}},
            "profile_routing": {
                "sidecars": [
                    {
                        "id": "camera_xml",
                        "format": "xml",
                        "path": "{parent}/{stem}M01.XML",
                        "primary": {"path": {"suffix": ".mp4"}},
                        "facts": {"source": "exiftool", "tags": ["Make"]},
                    }
                ],
                "routes": [
                    {
                        "id": "sidecar-camera-video",
                        "group": "video",
                        "when": {
                            "fact": "sidecars.camera_xml.facts.exif.make",
                            "equals": "example imaging",
                        },
                    }
                ],
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["unmatched"][0]["path"] == "camera/C0001.MP4"
    assert payload["unmatched"][0]["reason"] == "sidecar_facts_failed"
    assert "configured sidecar facts were not submitted" in (
        payload["unmatched"][0]["sidecar_facts_error"]
    )


def test_profile_routing_preflight_reports_sidecar_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    client = TestClient(runner.app)

    response = client.post(
        "/v1/profile-routing/preflight",
        json={
            "files": [
                {"path": "phone/IMG_0001.MOV", "bytes": 100},
                {
                    "path": "phone/IMG_0001.MOV.xmp",
                    "bytes": 20,
                    "routing_facts": {
                        "exif.date_time_original": "2002:03:04 05:06:07-0700",
                        "exif.gps_latitude": "3.4567 N",
                        "exif.gps_longitude": "4.5678 E",
                    },
                },
            ],
            "groups": {
                "video": {
                    "archive_mode": "av1_nvenc",
                    "tasks": ["archive_video"],
                    "metadata_projection": {
                        "capture_date_sources": [
                            {"type": "sidecar", "id": "xmp"},
                            {"type": "embedded"},
                        ],
                        "gps_sources": [
                            {"type": "sidecar", "id": "xmp"},
                            {"type": "embedded"},
                        ],
                        "device": {
                            "make": "Apple",
                            "model": "iPhone SE (2nd generation)",
                        },
                        "creators": ["Nash Spence"],
                    },
                }
            },
            "profile_routing": {
                "sidecars": [
                    {
                        "id": "xmp",
                        "format": "xmp",
                        "primary": {"path": {"suffix": ".mov"}},
                    }
                ],
                "routes": [
                    {
                        "id": "video",
                        "group": "video",
                        "when": {"path": {"suffix": ".mov"}},
                    }
                ],
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert [item["action"] for item in payload["matches"]] == ["upload", "evidence"]
    assert payload["matches"][1]["sidecar_id"] == "xmp"
    assert payload["matches"][1]["sidecar_for"] == "phone/IMG_0001.MOV"
    readout_by_path = {item["path"]: item for item in payload["readout"]["files"]}
    assert readout_by_path["phone/IMG_0001.MOV"]["sidecars"] == [
        {
            "id": "xmp",
            "format": "xmp",
            "path": "phone/IMG_0001.MOV.xmp",
            "custody": "source_artifacts",
        }
    ]
    projection = readout_by_path["phone/IMG_0001.MOV"]["metadata_projection"]
    assert projection["ok"] is True
    assert projection["capture_date"] == {
        "present": True,
        "source": "sidecar:xmp:exif.date_time_original",
        "value": "2002-03-04T05:06:07-07:00",
    }
    assert projection["gps"]["present"] is True
    assert projection["gps"]["source"] == "sidecar:xmp:exif.gps_latitude+exif.gps_longitude"
    assert projection["gps"]["latitude"] == 3.4567
    assert projection["gps"]["longitude"] == 4.5678
    assert readout_by_path["phone/IMG_0001.MOV.xmp"]["sidecar"] == {
        "id": "xmp",
        "format": "xmp",
        "for": "phone/IMG_0001.MOV",
    }
    assert readout_by_path["phone/IMG_0001.MOV.xmp"]["custody"] == {
        "kind": "source_artifact_sidecar"
    }


def test_profile_routing_preflight_keeps_metadata_projection_diagnostic_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    client = TestClient(runner.app)

    response = client.post(
        "/v1/profile-routing/preflight",
        json={
            "files": [{"path": "camera/C0001.MP4", "bytes": 100}],
            "groups": {
                "video": {
                    "archive_mode": "av1_nvenc",
                    "tasks": ["archive_video"],
                    "metadata_projection": {
                        "gps": {"latitude": 48.9995, "longitude": -122.7404},
                        "device": {"make": "Reolink", "model": "E1 Pro"},
                        "creators": ["Nash Spence"],
                    },
                }
            },
            "profile_routing": {
                "routes": [
                    {
                        "id": "camera-video",
                        "group": "video",
                        "when": {"path": {"suffix": ".mp4"}},
                    }
                ],
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["unmatched"] == []
    readout_by_path = {item["path"]: item for item in payload["readout"]["files"]}
    projection = readout_by_path["camera/C0001.MP4"]["metadata_projection"]
    assert projection["ok"] is False
    assert "requires a valid capture date" in projection["error"]


def test_profile_routing_preflight_can_enforce_metadata_projection(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    client = TestClient(runner.app)

    response = client.post(
        "/v1/profile-routing/preflight",
        json={
            "enforce_metadata_projection": True,
            "files": [{"path": "camera/C0001.MP4", "bytes": 100}],
            "groups": {
                "video": {
                    "archive_mode": "av1_nvenc",
                    "tasks": ["archive_video"],
                    "metadata_projection": {
                        "gps": {"latitude": 48.9995, "longitude": -122.7404},
                        "device": {"make": "Reolink", "model": "E1 Pro"},
                        "creators": ["Nash Spence"],
                    },
                }
            },
            "profile_routing": {
                "routes": [
                    {
                        "id": "camera-video",
                        "group": "video",
                        "when": {"path": {"suffix": ".mp4"}},
                    }
                ],
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["matches"] == []
    assert payload["unmatched_files"] == 1
    [unmatched] = payload["unmatched"]
    assert unmatched["path"] == "camera/C0001.MP4"
    assert unmatched["reason"] == "metadata_projection_failed"
    assert "requires a valid capture date" in unmatched["metadata_projection_error"]
    readout_by_path = {item["path"]: item for item in payload["readout"]["files"]}
    assert readout_by_path["camera/C0001.MP4"]["metadata_projection"]["ok"] is False


def test_profile_routing_preflight_reports_fallthrough(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    client = TestClient(runner.app)

    response = client.post(
        "/v1/profile-routing/preflight",
        json={
            "files": [{"path": "phone/IMG_0001.HEIC", "bytes": 123}],
            "groups": {"video": {"archive_mode": "av1_nvenc", "tasks": []}},
            "profile_routing": {
                "routes": [
                    {
                        "id": "phone-video",
                        "group": "video",
                        "when": {"path": {"prefix": "phone", "suffix": ".mov"}},
                    }
                ]
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["unmatched"] == [
        {
            "path": "phone/IMG_0001.HEIC",
            "bytes": 123,
            "reason": "no_matching_route",
            "probe_error": None,
            "facts_error": None,
        }
    ]


def test_completed_structured_file_fails_when_no_route_matches(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    shared_file = runner.shared_input_upload_root("upload-1") / "unknown/clip.mp4"
    shared_file.parent.mkdir(parents=True, exist_ok=True)
    shared_file.write_bytes(b"video")
    upload = runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "state": "uploading",
            "storage_hint": {"workflow_mode": "collection_archive", "structured_routing": True},
            "files": [
                {
                    "path": "unknown/clip.mp4",
                    "bytes": 5,
                    "upload_id": "unknown-clip",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                }
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "profile_routing": {
            "routes": [
                {
                    "id": "front-door-video",
                    "group": "video",
                    "when": {"path": {"prefix": "front-door"}},
                }
            ]
        },
    }

    with pytest.raises(runner.RoutingFailed, match="no matching route"):
        runner.route_completed_input_files(
            job,
            upload,
            {"video": {"archive_mode": "av1_nvenc", "tasks": []}},
        )


def test_archive_admission_uses_eager_batch_peak_for_gpu_scratch(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "EAGER_ARCHIVE_BATCH_FILES", 2)
    monkeypatch.setattr(runner, "EAGER_ARCHIVE_PIPELINE_BATCHES", 2)
    monkeypatch.setattr(runner, "GPU_SCRATCH_MULTIPLIER", 2.5)
    monkeypatch.setattr(runner, "EAGER_ARCHIVE_SCRATCH_MULTIPLIER", 0.5)
    monkeypatch.setattr(runner, "gpu_input_copy_multiplier", lambda: 0.0)
    files = [
        runner.InputFileSpec(path=f"camera/{index}.mp4", bytes=size)
        for index, size in enumerate([100, 90, 80, 70, 60], start=1)
    ]
    hint = runner.InputUploadStorageHint(
        workflow_mode="collection_archive",
        groups={
            "camera": runner.StorageGroupHint(
                archive_mode="av1_nvenc",
                tasks=["archive_video"],
            )
        },
    )

    required = runner.gpu_scratch_admission_required_bytes(files, hint)

    assert required == int((100 + 90 + 80 + 70) * 0.5)
    assert required < runner.gpu_scratch_required_bytes(sum(item.bytes for item in files), hint)


def test_audio_archive_hint_does_not_reserve_gpu_scratch(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    files = [runner.InputFileSpec(path="voice/REC_0001.wav", bytes=100)]
    hint = runner.InputUploadStorageHint(
        workflow_mode="collection_archive",
        archive_mode="audio",
        groups={"voice": runner.StorageGroupHint(archive_mode="audio")},
    )

    assert hint.groups["voice"].tasks == ["archive_audio"]
    assert runner.storage_hint_has_gpu_work(hint) is False
    assert runner.gpu_scratch_required_bytes(100, hint) == 0
    assert runner.gpu_scratch_admission_required_bytes(files, hint) == 0


def test_eager_archive_audio_group_encodes_and_consumes_uploaded_files(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    upload_id = "upload-voice"
    source_data = runner.tusd_data_path("upload-a")
    source_data.parent.mkdir(parents=True, exist_ok=True)
    source_data.write_bytes(b"mp3")
    group_config = {
        "archive_mode": "audio",
        "tasks": ["archive_audio"],
        "metadata_projection": {"enabled": False},
    }
    upload = runner.save_input_upload_raw(
        {
            "upload_id": upload_id,
            "files": [
                {
                    "path": "voice/R-00001_2606291200_REC.MP3",
                    "bytes": 3,
                    "upload_id": "upload-a",
                    "input_upload_id": upload_id,
                    "filesystem_metadata": {"stat": {"birthtime": "2026-06-29T12:00:00Z"}},
                }
            ],
        }
    )
    job = {
        "job_id": "job-voice",
        "input_upload_id": upload_id,
        "collection_slug": "voice-collection-archive",
    }
    runner.save_job(job)
    archive_dir = tmp_path / "archive"
    calls: list[tuple[Path, Path]] = []

    def fake_run_archive_audio_group(
        *,
        input_root: Path,
        output_root: Path,
        group_config: dict[str, object],
    ) -> dict[str, object]:
        calls.append((input_root, output_root))
        assert (input_root / "R-00001_2606291200_REC.MP3").read_bytes() == b"mp3"
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "R-00001_2606291200_REC.opus").write_bytes(b"opus")
        return {"status": "succeeded", "count": 1, "items": []}

    monkeypatch.setattr(runner, "run_archive_audio_group", fake_run_archive_audio_group)

    assert runner.eager_archive_group_names({"voice": group_config}) == {"voice"}
    updated = runner.start_eager_audio_batch(
        job,
        upload,
        group_name="voice",
        group_config=group_config,
        file_states=upload["files"],
        archive_dir=archive_dir,
    )

    stored_job = runner.load_job("job-voice")
    stored_upload = runner.load_input_upload(upload_id)
    progress = runner.encode_progress_for_job(stored_job)
    assert calls
    assert (archive_dir / "voice" / "R-00001_2606291200_REC.opus").read_bytes() == b"opus"
    assert updated["files"][0]["consumed_at"]
    assert stored_upload["files"][0]["consumed_at"]
    assert not source_data.exists()
    assert progress is not None
    assert progress["mode"] == "eager_archive"
    assert progress["groups"] == ["voice"]
    assert progress["files_total"] == 1
    assert progress["files_encoded"] == 1
    assert progress["percent_files"] == 100.0


def test_archive_audio_group_encodes_opus_and_writes_source_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    input_root = tmp_path / "input" / "voice"
    output_root = tmp_path / "archive" / "voice"
    source = input_root / "REC_20260628_203040.WAV"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"wav")
    runner.write_filesystem_metadata_map(
        input_root,
        {
            source.name: {
                "stat": {
                    "birthtime": "2026-06-28T20:30:40+00:00",
                    "size": 3,
                }
            }
        },
        created_at="2026-06-29T00:00:00Z",
    )
    group_config = {
        "archive_mode": "audio",
        "tasks": ["archive_audio"],
        "encode_profile": {
            "target": "munchy-audio",
            "archive": {
                "codec": "opus",
                "container": "opus",
                "audio": {
                    "bitrate": "28k",
                    "sample_rate": 24000,
                    "channels": 1,
                    "vbr": True,
                    "application": "audio",
                    "compression_level": 10,
                    "frame_duration": 40,
                    "cutoff": 12000,
                },
            },
        },
        "metadata_projection": {
            "allow_missing_gps": True,
            "device": {"make": "eSonic", "model": "MEMOQ SR-600"},
            "creators": ["Nash Spence"],
            "capture_date_sources": [
                {"type": "embedded"},
                {"type": "filesystem_birthtime"},
            ],
        },
    }
    commands: list[list[str]] = []

    def fake_run_command(cmd: list[str], **_kwargs: object) -> dict[str, object]:
        commands.append(cmd)
        Path(cmd[-1]).write_bytes(b"opus")
        return {"command": cmd, "duration_s": 0.1}

    def fake_build_strict_source_artifacts(**kwargs: object) -> dict[str, object]:
        metadata = kwargs["source_filesystem_metadata"]
        assert isinstance(metadata, dict)
        assert metadata["stat"]["birthtime"] == "2026-06-28T20:30:40+00:00"
        return {"path": str(kwargs["archive_mkv"]) + ".source-artifacts.tar.zst"}

    monkeypatch.setattr(runner, "run_command", fake_run_command)
    monkeypatch.setattr(runner, "build_strict_source_artifacts", fake_build_strict_source_artifacts)
    monkeypatch.setattr(runner, "ffprobe_for_routing", lambda _path: {"format": {}, "streams": []})
    monkeypatch.setattr(runner, "exiftool_for_routing", lambda _path, **_kwargs: {})

    result = runner.run_archive_audio_group(
        input_root=input_root,
        output_root=output_root,
        group_config=group_config,
    )

    output = output_root / "REC_20260628_203040.opus"
    assert result["status"] == "succeeded"
    assert result["count"] == 1
    assert output.read_bytes() == b"opus"
    assert commands == [
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-ignore_unknown",
            "-i",
            str(source),
            "-map",
            "0:a?",
            "-map_metadata",
            "0",
            "-vn",
            "-c:a",
            "libopus",
            "-ar",
            "24000",
            "-ac",
            "1",
            "-b:a",
            "28k",
            "-vbr",
            "on",
            "-compression_level",
            "10",
            "-application",
            "audio",
            "-frame_duration",
            "40",
            "-cutoff",
            "12000",
            "-metadata",
            "DATE=2026-06-28T20:30:40+00:00",
            "-metadata",
            "creation_time=2026-06-28T20:30:40+00:00",
            "-metadata",
            "ARTIST=Nash Spence",
            "-metadata",
            "CREATOR=Nash Spence",
            "-metadata",
            "MAKE=eSonic",
            "-metadata",
            "MODEL=MEMOQ SR-600",
            "-f",
            "opus",
            str(output),
        ]
    ]


def test_audio_archive_projection_uses_birthtime_and_conversion_only_source_custody(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    upload_id = "upload-voice"
    input_root = runner.shared_input_upload_root(upload_id) / "voice"
    output_root = tmp_path / "archive" / "voice"
    source = input_root / "memo" / "R-00013_2606222246_REC.MP3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"mp3")
    source_metadata = {
        "stat": {
            "birthtime": "2026-06-23T05:46:32+00:00",
            "size": 3,
        }
    }
    runner.write_filesystem_metadata_map(
        input_root,
        {"memo/R-00013_2606222246_REC.MP3": source_metadata},
        created_at="2026-06-29T00:00:00Z",
    )
    group_config = {
        "archive_mode": "audio",
        "tasks": ["archive_audio"],
        "encode_profile": {
            "target": "munchy-audio",
            "archive": {
                "codec": "opus",
                "container": "opus",
                "audio": {"bitrate": "64k"},
            },
            "source": {"allow_conversion_only_container": True},
        },
        "metadata_projection": {
            "allow_missing_gps": True,
            "device": {"make": "eSonic", "model": "MEMOQ SR-600"},
            "creators": ["Nash Spence"],
            "capture_date_sources": [
                {"type": "embedded"},
                {"type": "filesystem_birthtime"},
            ],
            "tags": ["device/esonic-memoq-sr600-nash"],
        },
    }

    def fake_run_command(cmd: list[str], **_kwargs: object) -> dict[str, object]:
        Path(cmd[-1]).write_bytes(b"opus")
        return {"command": cmd, "duration_s": 0.1}

    def fake_build_strict_source_artifacts(**kwargs: object) -> dict[str, object]:
        encode_profile = kwargs["encode_profile"]
        filesystem_metadata = kwargs["source_filesystem_metadata"]
        assert isinstance(encode_profile, dict)
        assert encode_profile["source"]["allow_conversion_only_container"] is True
        assert filesystem_metadata == source_metadata
        sidecar = Path(str(kwargs["archive_mkv"]) + ".source-artifacts.tar.zst")
        sidecar.write_bytes(b"source artifacts")
        return {
            "path": str(sidecar),
            "audit": {
                "rebuild_supported": False,
                "rebuild_blockers": ["source_container"],
                "rebuild_scope": (
                    "conversion-only archive; source-container rebuild is not supported"
                ),
            },
        }

    monkeypatch.setattr(runner, "run_command", fake_run_command)
    monkeypatch.setattr(runner, "build_strict_source_artifacts", fake_build_strict_source_artifacts)
    monkeypatch.setattr(runner, "ffprobe_for_routing", lambda _path: {"format": {}, "streams": []})
    monkeypatch.setattr(runner, "exiftool_for_routing", lambda _path, **_kwargs: {})

    result = runner.run_archive_audio_group(
        input_root=input_root,
        output_root=output_root,
        group_config=group_config,
    )
    output = output_root / "memo" / "R-00013_2606222246_REC.opus"
    upload = runner.save_input_upload_raw(
        {
            "upload_id": upload_id,
            "files": [
                {
                    "path": "voice/memo/R-00013_2606222246_REC.MP3",
                    "bytes": 3,
                    "upload_id": "file-voice",
                    "input_upload_id": upload_id,
                    "filesystem_metadata": source_metadata,
                }
            ],
        }
    )
    job = {
        "job_id": "job-voice",
        "input_upload_id": upload_id,
        "collection_slug": "esonic-collection-archive",
    }
    runner.save_job(job)

    updated = runner.write_metadata_projection_sidecars(
        job,
        upload,
        {"voice": group_config},
        tmp_path / "archive",
    )

    assert result["status"] == "succeeded"
    assert output.read_bytes() == b"opus"
    sidecar = output.with_name("R-00013_2606222246_REC.opus.xmp")
    xmp = sidecar.read_text(encoding="utf-8")
    assert 'exif:DateTimeOriginal="2026-06-23T05:46:32+00:00"' in xmp
    assert 'tiff:Make="eSonic"' in xmp
    assert 'tiff:Model="MEMOQ SR-600"' in xmp
    assert "geo:lat=" not in xmp
    assert "<rdf:li>Nash Spence</rdf:li>" in xmp
    assert "<rdf:li>device/esonic-memoq-sr600-nash</rdf:li>" in xmp
    assert "<rdf:li>munchy/collection/esonic-collection-archive</rdf:li>" in xmp
    assert "<rdf:li>munchy/group/voice</rdf:li>" in xmp
    assert updated["files"][0]["metadata_projection_sidecar"] == (
        "voice/memo/R-00013_2606222246_REC.opus.xmp"
    )


def test_scheduler_reserves_running_job_slots_and_leaves_extra_jobs_queued(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_MAX_RUNNING_JOBS", "1")
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_job(
        {
            "job_id": "job-a",
            "state": "queued",
            "phase": "queued",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    runner.save_job(
        {
            "job_id": "job-b",
            "state": "queued",
            "phase": "queued",
            "created_at": "2026-01-01T00:00:01Z",
        }
    )
    scheduled_tasks: list[tuple[object, tuple[object, ...]]] = []

    class Tasks:
        def add_task(self, func, *args):  # type: ignore[no-untyped-def]
            scheduled_tasks.append((func, args))

    first = runner.schedule_pending_jobs(Tasks())
    second = runner.schedule_pending_jobs(Tasks())
    job_b = runner.job_response(runner.load_job("job-b"))

    assert first == ["job-a"]
    assert second == []
    assert scheduled_tasks == [(runner.run_job, ("job-a",))]
    assert runner.scheduler_status()["scheduled_jobs"] == ["job-a"]
    assert job_b["queue"]["position"] == 2
    assert job_b["queue"]["running_job_limit"] == 1


def test_notification_defaults_come_from_runner_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_NOTIFY_ENABLED", "1")
    monkeypatch.setenv("MUNCHY_RUNNER_NOTIFY_DEFAULT_RECIPIENTS", "operator")
    runner = load_runner(tmp_path, monkeypatch)

    req = runner.CreateJobRequest(collection_slug="collection")
    capabilities = runner.capabilities()

    assert req.notify.enabled is True
    assert req.notify.recipients == ["operator"]
    assert capabilities["notify"]["default_enabled"] is True
    assert capabilities["notify"]["default_recipients"] == ["operator"]


def test_notification_payload_identifies_munchy_with_canonical_emoji(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)

    payload = runner.notify_payload(
        {
            "job_id": "job-1",
            "collection_slug": "collection",
            "collection_timestamp": "20260605T120000Z",
            "phase": "queued",
            "state": "queued",
        },
        event="job.received",
        message="Job received.",
        severity="info",
        recipient="operator",
        extra=None,
    )

    assert payload["source"] == "munchy"
    assert payload["actor"] == "munchy"
    assert payload["event"] == "job.received"
    assert payload["delivered_at"].endswith("Z")
    assert payload["operator_urgency"] == "passive"
    assert payload["operator_action"] == "none"
    assert payload["notification"] == {
        "title": "🤤 collection",
        "body": "Munchy received this job and queued the work.",
    }


def test_client_preflight_failed_notification_uses_runner_defaults(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_NOTIFY_ENABLED", "1")
    monkeypatch.setenv("MUNCHY_RUNNER_NOTIFY_DEFAULT_RECIPIENTS", "operator")
    runner = load_runner(tmp_path, monkeypatch)
    calls: list[dict[str, object]] = []

    def fake_send_notify_deliveries(job, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"job": job, **kwargs})
        return [{"recipient": "operator", "status": 200}]

    monkeypatch.setattr(runner, "send_notify_deliveries", fake_send_notify_deliveries)

    result = runner.notify_preflight_failed(
        runner.ClientPreflightFailedNotificationRequest(
            message="Local media preflight failed for camera.",
            device_id="camera",
            workflow_mode="collection_archive",
            profile_group="camera-video",
            collection_slug="camera-collection-archive",
            collection_timestamp="20260606T120000Z",
            upload_id="upload-1",
            job_id="job-1",
            files=2,
            failed_file_count=1,
            failed_files=[
                {
                    "path": "camera-video/bad.mp4",
                    "source": "/source/bad.mp4",
                    "issues": [{"code": "mp4_atom_extends_past_eof", "message": "bad atom"}],
                }
            ],
        )
    )

    assert result["status"] == "attempted"
    assert calls[0]["event"] == "job.issue"
    assert calls[0]["severity"] == "critical"
    assert calls[0]["recipients"] == ["operator"]
    assert calls[0]["job"]["phase"] == "preflight_failed"
    assert calls[0]["extra"]["component"] == "preflight"
    assert calls[0]["extra"]["error"] == "bad atom (bad.mp4)"
    assert calls[0]["extra"]["failed_file_count"] == 1


def test_upload_waiting_reminder_is_time_sensitive_and_paced(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_NOTIFY_ENABLED", "1")
    monkeypatch.setenv("MUNCHY_RUNNER_NOTIFY_DEFAULT_RECIPIENTS", "operator")
    monkeypatch.setenv("RIVERHOG_OPERATOR_WEBHOOK_REMINDER_INTERVAL", "1")
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    calls: list[dict[str, object]] = []

    def fake_send_notify_deliveries(job, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"job": job, **kwargs})
        return [{"recipient": "operator", "status": 200}]

    monkeypatch.setattr(runner, "send_notify_deliveries", fake_send_notify_deliveries)
    job = {
        "job_id": "job-1",
        "collection_slug": "camera-collection-archive",
        "collection_timestamp": "20260606T120000Z",
        "state": "running",
        "phase": "waiting_for_eager_files:1/2",
        "notify": {
            "enabled": True,
            "recipients": ["operator"],
            "events": runner.DEFAULT_NOTIFY_EVENTS,
        },
    }
    runner.save_job(job)
    upload = {
        "upload_id": "upload-1",
        "created_at": "2026-01-01T00:00:00Z",
        "files": [
            {"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"},
            {"path": "camera/b.mp4", "bytes": 1, "upload_id": "upload-b"},
        ],
    }
    progress = {
        "files_total": 2,
        "files_uploaded": 1,
        "bytes_total": 2,
        "uploaded_bytes": 1,
    }

    first = runner.notify_upload_waiting_reminder(job, upload, progress)
    second = runner.notify_upload_waiting_reminder(job, upload, progress)

    assert first["status"] == "attempted"
    assert second["status"] == "suppressed"
    assert second["reason"] == "reminder_repeat_limit"
    assert calls[0]["event"] == "job.upload_waiting.reminder"
    assert calls[0]["severity"] == "warning"
    assert calls[0]["extra"]["reminder_count"] == 1
    assert calls[0]["extra"]["reminder_interval_seconds"] == 1
    assert calls[0]["extra"]["upload_progress"]["files_uploaded"] == 1


def test_retry_handoff_transient_failure_does_not_notify(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    monkeypatch.setattr(runner, "retry_sleep", lambda *args, **kwargs: None)
    notify_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        runner,
        "notify_job_issue",
        lambda *args, **kwargs: notify_calls.append(dict(kwargs)),
    )
    job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "riverhog_upload",
    }
    runner.save_job(job)
    attempts = 0

    def operation() -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Connection reset by peer")
        return {"ok": True}

    result = runner.retry_handoff_until_success(
        job,
        result_key="riverhog_upload_result",
        phase="riverhog_upload",
        action="riverhog upload",
        component="riverhog_upload",
        operation=operation,
    )

    assert result["ok"] is True
    assert result["attempt"] == 2
    assert isinstance(result["succeeded_at"], str)
    assert notify_calls == []


def test_eager_gpu_transient_failure_does_not_notify(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    notify_calls: list[dict[str, object]] = []
    submit_calls: list[str] = []
    monkeypatch.setattr(
        runner,
        "notify_job_issue",
        lambda *args, **kwargs: notify_calls.append(dict(kwargs)),
    )

    def fail_gpu_request(method: str, path: str, payload=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("Server disconnected without sending a response.")

    monkeypatch.setattr(runner, "gpu_target_request", fail_gpu_request)
    monkeypatch.setattr(
        runner,
        "submit_eager_gpu_job",
        lambda job, batch, force=False: submit_calls.append(str(batch["gpu_job_id"])),
    )
    job = {"job_id": "job-1"}
    upload = {"upload_id": "upload-1"}
    batch = {
        "batch_id": "batch-1",
        "gpu_job_id": "gpu-1",
        "payload": {"job_id": "gpu-1"},
    }

    result = runner.poll_eager_gpu_batch(job, upload, batch, {}, tmp_path / "archive")

    assert result is upload
    assert submit_calls == ["gpu-1"]
    assert notify_calls == []


def test_load_input_upload_does_not_refresh_state_timestamp(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
        }
    )
    before = runner.read_state("input-upload", "upload-1")["updated_at"]

    loaded = runner.load_input_upload("upload-1")

    after = runner.read_state("input-upload", "upload-1")["updated_at"]
    assert loaded["files_total"] == 1
    assert after == before


def test_resume_job_clears_failed_runtime_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    cancelled: list[str] = []
    monkeypatch.setattr(runner, "schedule_pending_jobs", lambda _background_tasks: None)
    monkeypatch.setattr(
        runner,
        "cancel_riverhog_upload_session",
        lambda job, reason: cancelled.append(f"{job['job_id']}:{reason}"),
    )
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "failed",
            "phase": "eager_archive:pipeline=1/3",
            "input_upload_id": "upload-1",
            "error": "old error",
            "finished_at": "2026-01-01T00:00:00Z",
            "groups": {"camera": {"archive_mode": "av1_nvenc", "tasks": ["archive_video"]}},
            "profile_routing_result": {"files": 1},
            "eager_archive": {"files": {"camera/a.mp4": {"state": "failed"}}},
            "riverhog_session_upload": {"collection_id": "collection-1", "files": {}},
            "debug_bundle_dir": "/debug/old",
        }
    )

    resumed = runner.resume_job("job-1", runner.BackgroundTasks())
    stored = runner.load_job("job-1")

    assert resumed["state"] == "queued"
    assert stored["phase"] == "queued"
    assert cancelled == ["job-1:job_resume_reset"]
    assert "error" not in stored
    assert "finished_at" not in stored
    assert "eager_archive" not in stored
    assert "riverhog_session_upload" not in stored
    assert "profile_routing_result" not in stored
    assert "debug_bundle_dir" not in stored
    assert stored["groups"] == {
        "camera": {"archive_mode": "av1_nvenc", "tasks": ["archive_video"]}
    }


def test_resume_job_preserves_fully_uploaded_riverhog_session(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    monkeypatch.setattr(runner, "schedule_pending_jobs", lambda _background_tasks: None)
    monkeypatch.setattr(runner, "riverhog_session_visible_for_resume", lambda _job: True)
    monkeypatch.setattr(
        runner,
        "cancel_riverhog_upload_session",
        lambda _job, _reason: (_ for _ in ()).throw(
            AssertionError("resume should preserve a fully uploaded Riverhog session")
        ),
    )
    riverhog_session = {
        "collection_id": "2026/20260615T030000Z__weekly-device-artifacts",
        "state": "open",
        "files": {
            "camera/a.webm": {
                "path": "camera/a.webm",
                "bytes": 7,
                "uploaded_bytes": 7,
                "state": "uploaded",
            },
            "camera/a.source-artifacts.tar.zst": {
                "path": "camera/a.source-artifacts.tar.zst",
                "bytes": 3,
                "uploaded_bytes": 3,
                "state": "uploaded",
            },
        },
    }
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "failed",
            "phase": "copying_preserve:preserve",
            "input_upload_id": "upload-1",
            "collection_slug": "weekly-device-artifacts",
            "collection_timestamp": "20260615T030000Z",
            "workflow_mode": "collection_archive",
            "riverhog": {"enabled": True, "wait": "finalized"},
            "error": "old error",
            "finished_at": "2026-01-01T00:00:00Z",
            "eager_archive": {"files": {"camera/a.mp4": {"state": "encoded"}}},
            "riverhog_session_upload": riverhog_session,
            "debug_bundle_dir": "/debug/old",
        }
    )

    resumed = runner.resume_job("job-1", runner.BackgroundTasks())
    stored = runner.load_job("job-1")

    assert resumed["state"] == "queued"
    assert stored["phase"] == "queued"
    assert "error" not in stored
    assert "finished_at" not in stored
    assert "debug_bundle_dir" not in stored
    assert stored["eager_archive"]["files"] == {"camera/a.mp4": {"state": "encoded"}}
    assert stored["riverhog_session_upload"]["files"] == riverhog_session["files"]
    assert stored["riverhog_resume_preserved_at"]


def test_review_rclone_upload_excludes_platform_cruft(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_TARGET_UPLOAD_ENABLED", "1")
    runner = load_runner(tmp_path, monkeypatch)
    monkeypatch.setattr(
        runner,
        "retry_handoff_until_success",
        lambda _job, **kwargs: kwargs["operation"](),
    )
    source_dir = tmp_path / "review"
    source_dir.mkdir()
    (source_dir / ".DS_Store").write_bytes(b"finder")
    (source_dir / "clip.webm").write_bytes(b"video")
    nested = source_dir / "nested"
    nested.mkdir()
    (nested / "._clip.webm").write_bytes(b"appledouble")
    commands: list[list[str]] = []

    def fake_run_target_command(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        commands.append(list(cmd))
        return {"returncode": 0}

    monkeypatch.setattr(runner, "run_target_command", fake_run_target_command)

    result = runner.upload_target(
        {
            "job_id": "job-1",
            "collection_slug": "collection-archive",
            "collection_timestamp": "20260629T000000Z",
        },
        source_dir,
        config={
            "enabled": True,
            "method": "rclone",
            "mode": "copy",
            "destination": "remote:{collection_slug}/{collection_timestamp}",
        },
        source_label="collection archive",
        result_key="collection_archive_target_upload_result",
        phase="collection_archive_target_upload",
        component="collection_archive_target_upload",
        event="collection_archive.handoff",
    )

    assert result["artifact_count"] == 1
    assert result["destination"] == "remote:collection-archive/20260629T000000Z"
    command = commands[0]
    assert command[:2] == ["rclone", "copy"]
    assert str(source_dir) in command
    assert "remote:collection-archive/20260629T000000Z" in command
    assert command.index(str(source_dir)) < command.index(
        "remote:collection-archive/20260629T000000Z"
    )
    assert command.count("--exclude") >= 4
    assert ".DS_Store" in command
    assert "**/._*" in command


def test_cancel_job_with_cleanup_removes_local_work_and_input_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"a")
    shared_root = runner.shared_input_upload_root("upload-1")
    shared_root.mkdir(parents=True)
    (shared_root / "camera").mkdir()
    (shared_root / "camera" / "a.mp4").write_bytes(b"a")
    work_dir = runner.GPU_RUNTIME_DIR / "jobs" / "job-1"
    work_dir.mkdir(parents=True)
    (work_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
        }
    )
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
        }
    )

    job = runner.cancel_job("job-1", cleanup=True)

    assert job["state"] == "cancelled"
    assert job["cleanup_requested"] is True
    assert runner.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not shared_root.exists()
    assert not work_dir.exists()
    assert "input-upload:upload-1" in job["cleanup_removed"]


def test_cancel_job_with_cleanup_preserves_input_upload_referenced_by_sibling(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"a")
    shared_root = runner.shared_input_upload_root("upload-1")
    shared_root.mkdir(parents=True)
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
        }
    )
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
        }
    )
    runner.save_job(
        {
            "job_id": "job-2",
            "state": "queued",
            "phase": "queued",
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
        }
    )

    job = runner.cancel_job("job-1", cleanup=True)

    assert job["state"] == "cancelled"
    assert runner.read_state("input-upload", "upload-1") is not None
    assert data_path.exists()
    assert shared_root.exists()
    assert "input-upload:upload-1" not in job.get("cleanup_removed", [])


def test_finalize_cancelled_job_persists_terminal_state_before_cleanup_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"a")
    shared_root = runner.shared_input_upload_root("upload-1")
    shared_root.mkdir(parents=True)
    (shared_root / "camera").mkdir()
    (shared_root / "camera" / "a.mp4").write_bytes(b"a")
    work_dir = runner.GPU_RUNTIME_DIR / "jobs" / "job-1"
    work_dir.mkdir(parents=True)
    (work_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
        }
    )
    job = runner.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "cancel_requested",
            "cancel_requested": True,
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
            "riverhog": {"enabled": True},
            "riverhog_session_upload": {
                "state": "open",
                "collection_id": "2026/20260101T000000Z__camera-archive",
                "files": {},
            },
        }
    )

    def fail_cancel(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("riverhog cancel unavailable")

    monkeypatch.setattr(runner, "cancel_riverhog_upload_session", fail_cancel)

    finalized = runner.finalize_cancelled_job(job, reason="test_cancel")
    stored = runner.load_job("job-1")

    assert finalized["state"] == "cancelled"
    assert stored["state"] == "cancelled"
    assert stored["phase"] == "cancelled"
    assert "cancel_requested" not in stored
    assert stored["riverhog_cancel_failed_at"]
    assert "riverhog cancel unavailable" in stored["riverhog_cancel_error"]
    assert runner.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not shared_root.exists()
    assert not work_dir.exists()


def test_failed_job_with_cleanup_removes_local_work_and_input_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"a")
    shared_root = runner.shared_input_upload_root("upload-1")
    shared_root.mkdir(parents=True)
    (shared_root / "camera").mkdir()
    (shared_root / "camera" / "a.mp4").write_bytes(b"a")
    work_dir = runner.GPU_RUNTIME_DIR / "jobs" / "job-1"
    work_dir.mkdir(parents=True)
    (work_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
        }
    )
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "failed",
            "phase": "gpu",
            "finished_at": "2026-01-01T00:00:00Z",
            "input_upload_id": "upload-1",
            "input_upload_progress": {"files_total": 1, "files_uploaded": 1},
            "groups": {"camera": {}},
        }
    )

    job = runner.cancel_job("job-1", cleanup=True)

    assert job["state"] == "failed"
    assert runner.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not shared_root.exists()
    assert not work_dir.exists()
    assert "input-upload:upload-1" in job["cleanup_removed"]
    assert job["cleanup_completed_at"]
    assert job["input_upload_deleted_at"]
    assert "input_upload_progress" not in job


def test_terminal_cleanup_removes_eager_batch_gpu_work_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    computed_id = runner.gpu_eager_batch_job_id("job-1", "batch-2")
    roots = [
        runner.GPU_RUNTIME_DIR / "jobs" / "job-1",
        runner.GPU_RUNTIME_DIR / "jobs" / "explicit-eager-gpu-job",
        runner.GPU_RUNTIME_DIR / "jobs" / "payload-eager-gpu-job",
        runner.GPU_RUNTIME_DIR / "jobs" / computed_id,
        runner.GPU_RUNTIME_DIR / "jobs" / "job-1__eager__orphaned-old-batch__abcdef0123",
    ]
    for root in roots:
        root.mkdir(parents=True)
        (root / "scratch.txt").write_text("scratch", encoding="utf-8")
    job = {
        "job_id": "job-1",
        "state": "failed",
        "phase": "gpu",
        "eager_archive": {
            "batches": {
                "batch-1": {
                    "batch_id": "batch-1",
                    "gpu_job_id": "explicit-eager-gpu-job",
                    "payload": {"job_id": "payload-eager-gpu-job"},
                },
                "batch-2": {"batch_id": "batch-2"},
            }
        },
    }

    removed = runner.cleanup_terminal_job(job)

    assert len(removed) == len(roots)
    for root in roots:
        assert not root.exists()
        assert str(root) in removed


def test_cleanup_once_removes_old_failed_job_work_and_input_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"a")
    shared_root = runner.shared_input_upload_root("upload-1")
    shared_root.mkdir(parents=True)
    (shared_root / "camera").mkdir()
    (shared_root / "camera" / "a.mp4").write_bytes(b"a")
    work_dir = runner.GPU_RUNTIME_DIR / "jobs" / "job-1"
    work_dir.mkdir(parents=True)
    (work_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
        }
    )
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "failed",
            "phase": "gpu",
            "finished_at": "2026-01-01T00:00:00Z",
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
        }
    )

    result = runner.cleanup_once()

    assert "job-cleanup:job-1" in result["removed"]
    assert runner.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not shared_root.exists()
    assert not work_dir.exists()


def test_cleanup_once_repairs_stale_cancel_requested_job(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"a")
    shared_root = runner.shared_input_upload_root("upload-1")
    shared_root.mkdir(parents=True)
    (shared_root / "camera").mkdir()
    (shared_root / "camera" / "a.mp4").write_bytes(b"a")
    work_dir = runner.GPU_RUNTIME_DIR / "jobs" / "job-1"
    work_dir.mkdir(parents=True)
    (work_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
        }
    )
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "cancel_requested",
            "cancel_requested": True,
            "input_upload_id": "upload-1",
            "groups": {"camera": {}},
        }
    )

    result = runner.cleanup_once()
    job = runner.load_job("job-1")

    assert result["repaired_cancelled"] == ["job-1"]
    assert job["state"] == "cancelled"
    assert job["phase"] == "cancelled"
    assert "cancel_requested" not in job
    assert job["cancel_reason"] == "stale_cancel_requested"
    assert runner.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not shared_root.exists()
    assert not work_dir.exists()


def test_cancel_cleanup_snapshots_partial_encode_totals_before_input_deletion(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    for upload_id, _path in (("upload-a", "camera/a.mp4"), ("upload-b", "camera/b.mp4")):
        data_path = runner.tusd_data_path(upload_id)
        data_path.parent.mkdir(parents=True, exist_ok=True)
        data_path.write_bytes(b"a")
    shared_root = runner.shared_input_upload_root("upload-1")
    (shared_root / "camera").mkdir(parents=True)
    (shared_root / "camera" / "a.mp4").write_bytes(b"a")
    (shared_root / "camera" / "b.mp4").write_bytes(b"a")
    output = runner.GPU_RUNTIME_DIR / "jobs" / "job-1" / "archive" / "camera" / "a.webm"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"encoded")
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "files": [
                {"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"},
                {"path": "camera/b.mp4", "bytes": 1, "upload_id": "upload-b"},
            ],
        }
    )
    job = runner.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "cancel_requested",
            "cancel_requested": True,
            "input_upload_id": "upload-1",
            "groups": {
                "camera": {
                    "archive_mode": "av1_nvenc",
                    "tasks": ["archive_video"],
                }
            },
            "eager_archive": {
                "files": {
                    "camera/a.mp4": {
                        "state": "encoded",
                        "group": "camera",
                        "input_bytes": 1,
                        "output": str(output),
                        "output_bytes": 7,
                    }
                },
                "batches": {},
                "next_batch_number": 1,
            },
        }
    )

    runner.finalize_cancelled_job(job, reason="test_cancel")
    stored = runner.load_job("job-1")

    assert stored["state"] == "cancelled"
    assert stored["encode_progress"]["files_total"] == 2
    assert stored["encode_progress"]["files_encoded"] == 1
    assert stored["upload_progress"]["files_total"] == 2
    assert runner.read_state("input-upload", "upload-1") is None


def test_compact_job_response_includes_cleanup_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    job = {
        "job_id": "job-1",
        "state": "failed",
        "phase": "failed",
        "cleanup_removed": ["input-upload:upload-1"],
        "cleanup_completed_at": "2026-01-01T00:00:00Z",
        "input_upload_deleted_at": "2026-01-01T00:00:00Z",
        "local_work_cleaned_at": "2026-01-01T00:00:00Z",
        "local_work_removed": ["/gpu/jobs/job-1"],
        "eager_archive": {"large": ["payload"]},
    }

    compact = runner.compact_job_response(job)

    assert compact["cleanup_removed"] == ["input-upload:upload-1"]
    assert compact["cleanup_completed_at"] == "2026-01-01T00:00:00Z"
    assert compact["input_upload_deleted_at"] == "2026-01-01T00:00:00Z"
    assert compact["local_work_removed"] == ["/gpu/jobs/job-1"]
    assert "eager_archive" not in compact


def test_save_job_preserves_terminal_cleanup_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "cancelled",
            "phase": "cancelled",
            "cleanup_completed_at": "2026-01-01T00:00:00Z",
            "cleanup_removed_count": 25,
            "cleanup_removed_sample": ["job-work:job-1"],
            "input_upload_deleted_at": "2026-01-01T00:00:00Z",
        }
    )

    runner.save_job(
        {
            "job_id": "job-1",
            "state": "cancelled",
            "phase": "cancelled",
            "finished_at": "2026-01-01T00:00:01Z",
        }
    )

    stored = runner.load_job("job-1")
    assert stored["cleanup_completed_at"] == "2026-01-01T00:00:00Z"
    assert stored["cleanup_removed_count"] == 25
    assert stored["cleanup_removed_sample"] == ["job-work:job-1"]
    assert stored["input_upload_deleted_at"] == "2026-01-01T00:00:00Z"


def test_cleanup_terminal_job_records_empty_cleanup_completion(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    job = {
        "job_id": "job-1",
        "state": "cancelled",
        "phase": "cancelled",
    }

    removed = runner.cleanup_terminal_job(job)
    runner.compact_terminal_job_state(job)

    assert removed == []
    assert job["cleanup_completed_at"]
    assert job["cleanup_removed_count"] == 0


def test_cleanup_terminal_job_preserves_repeat_cleanup_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    job = {
        "job_id": "job-1",
        "state": "cancelled",
        "phase": "cancelled",
        "cleanup_removed": [f"/gpu/jobs/job-1-{index}" for index in range(12)],
        "cleanup_completed_at": "2026-01-01T00:00:00Z",
    }
    runner.compact_terminal_job_state(job)

    removed = runner.cleanup_terminal_job(job)
    runner.compact_terminal_job_state(job)

    assert removed == []
    assert job["cleanup_completed_at"] != "2026-01-01T00:00:00Z"
    assert job["cleanup_removed_count"] == 12
    assert len(job["cleanup_removed_sample"]) == 8
    assert "cleanup_removed" not in job


def test_compact_terminal_job_state_keeps_summaries_and_drops_heavy_payloads(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    job = {
        "job_id": "job-1",
        "state": "succeeded",
        "phase": "done",
        "workflow_mode": "collection_archive",
        "input_upload_id": "upload-1",
        "groups": {
            "camera": {
                "archive_mode": "av1_nvenc",
                "tasks": ["archive_video"],
            }
        },
        "eager_archive": {
            "files": {
                "camera/a.mp4": {
                    "state": "encoded",
                    "group": "camera",
                    "input_bytes": 100,
                    "output_bytes": 10,
                    "started_at": "2026-01-01T00:00:00Z",
                    "encoded_at": "2026-01-01T00:00:10Z",
                }
            },
            "batches": {
                "batch-1": {
                    "state": "succeeded",
                    "started_at": "2026-01-01T00:00:00Z",
                    "finished_at": "2026-01-01T00:00:10Z",
                    "payload": {"large": "payload"},
                    "gpu_result": {"large": "result"},
                }
            },
            "gpu_results": {"batch-1": {"large": "result"}},
        },
        "gpu_payloads": {"camera": {"large": "payload"}},
        "gpu_result": {"large": "result"},
        "gpu_results": {"camera": {"large": "result"}},
        "gpu_statuses": {"gpu-job": {"state": "succeeded"}},
        "group_results": {"camera": {"large": "result"}},
        "cleanup_removed": [f"/tmp/work-{index}" for index in range(12)],
        "local_work_removed": [f"/tmp/work-{index}" for index in range(12)],
        "collection_archive_target_upload_result": {
            "method": "rclone",
            "returncode": 0,
            "stdout": "x" * 5000,
            "stderr": "",
            "destination": "remote:path",
            "succeeded_at": "2026-01-01T00:01:00Z",
        },
        "riverhog_handoff_metrics": {
            "final_sweep_uploaded_files": 0,
            "session_complete_elapsed_seconds": 0.25,
        },
        "riverhog_session_upload": {
            "files": {
                "camera/a.webm": {
                    "path": "camera/a.webm",
                    "bytes": 10,
                    "uploaded_bytes": 10,
                }
            }
        },
    }

    changed = runner.compact_terminal_job_state(job)

    assert changed is True
    assert job["encode_progress"]["files_total"] == 1
    assert job["encode_progress"]["files_encoded"] == 1
    assert job["cleanup_removed_count"] == 12
    assert len(job["cleanup_removed_sample"]) == 8
    assert "cleanup_removed" not in job
    assert job["local_work_removed_count"] == 12
    assert "local_work_removed" not in job
    assert "stdout" not in job["collection_archive_target_upload_result"]
    assert len(job["collection_archive_target_upload_result"]["stdout_tail"]) == 4000
    assert job["riverhog_handoff_metrics"]["session_complete_elapsed_seconds"] == 0.25
    assert job["terminal_state_compacted_at"]
    for key in (
        "eager_archive",
        "gpu_payloads",
        "gpu_result",
        "gpu_results",
        "gpu_statuses",
        "group_results",
        "riverhog_session_upload",
    ):
        assert key not in job


def test_failed_job_debug_bundle_preserves_pre_compaction_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    job = {
        "job_id": "job-1",
        "state": "failed",
        "phase": "gpu",
        "error": "gpu job failed: bad encode",
        "eager_archive": {"files": {"camera/a.mp4": {"state": "failed"}}},
        "gpu_payloads": {"camera": {"input_dir": "/data/input"}},
        "gpu_results": {"camera": {"state": "failed"}},
    }

    changed = runner.write_job_debug_bundle(
        job,
        reason="encoding_failed",
        error=runner.EncodingFailed("gpu job failed: bad encode"),
    )
    runner.compact_terminal_job_state(job)

    bundle = Path(job["debug_bundle_dir"])
    assert changed is True
    assert bundle.is_dir()
    assert (bundle / "metadata.json").is_file()
    assert (bundle / "error.txt").read_text(encoding="utf-8") == ("gpu job failed: bad encode\n")
    with gzip.open(bundle / "job-state-full.json.gz", "rt", encoding="utf-8") as handle:
        full_state = json.load(handle)
    assert full_state["eager_archive"]["files"]["camera/a.mp4"]["state"] == "failed"
    assert full_state["gpu_payloads"]["camera"]["input_dir"] == "/data/input"
    assert "eager_archive" not in job
    assert job["debug_bundle_reason"] == "encoding_failed"


def test_prepare_shared_input_tree_links_uploaded_files_without_sha_rehash(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    source_inode = data_path.stat().st_ino
    monkeypatch.setattr(
        runner,
        "file_sha256",
        lambda path: (_ for _ in ()).throw(AssertionError("sha256 should not run")),
    )
    upload = {
        "upload_id": "upload-1",
        "files": [
            {
                "path": "camera/a.mp4",
                "bytes": 5,
                "sha256": "0" * 64,
                "upload_id": "upload-a",
            }
        ],
    }

    root = runner.prepare_shared_input_tree(upload, {"camera"})

    linked = root / "camera" / "a.mp4"
    assert linked.read_bytes() == b"video"
    assert linked.stat().st_ino == source_inode
    assert not data_path.exists()
    marker = root / ".munchy-input-upload.json"
    metadata = json.loads(marker.read_text(encoding="utf-8"))
    assert metadata["upload_id"] == "upload-1"
    assert metadata["files"] == 1
    monkeypatch.setattr(
        runner,
        "materialize_upload_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("prepared shared input tree should be reused")
        ),
    )

    assert runner.prepare_shared_input_tree(upload, {"camera"}) == root


def test_sync_shared_input_tree_links_completed_files_incrementally(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    complete_path = runner.tusd_data_path("upload-a")
    partial_path = runner.tusd_data_path("upload-b")
    complete_path.parent.mkdir(parents=True, exist_ok=True)
    complete_path.write_bytes(b"video-a")
    partial_path.write_bytes(b"vid")
    upload = {
        "upload_id": "upload-1",
        "files": [
            {
                "path": "camera/a.mp4",
                "bytes": 7,
                "upload_id": "upload-a",
                "filesystem_metadata": {"stat": {"st_birthtime": 1.25}},
            },
            {
                "path": "camera/b.mp4",
                "bytes": 7,
                "upload_id": "upload-b",
                "filesystem_metadata": {"stat": {"st_birthtime": 2.5}},
            },
        ],
    }

    summary = runner.sync_shared_input_tree(upload, {"camera"})
    progress = runner.upload_group_progress(upload, {"camera"})
    root = runner.shared_input_upload_root("upload-1")

    assert summary["linked"] == 1
    assert (root / "camera" / "a.mp4").read_bytes() == b"video-a"
    assert not complete_path.exists()
    assert not (root / "camera" / "b.mp4").exists()
    assert partial_path.exists()
    assert progress["files_uploaded"] == 1
    assert progress["input_tree_files_ready"] == 1

    partial_path.write_bytes(b"video-b")
    summary = runner.sync_shared_input_tree(upload, {"camera"})
    progress = runner.upload_group_progress(upload, {"camera"})

    assert summary["linked"] == 2
    assert (root / "camera" / "b.mp4").read_bytes() == b"video-b"
    assert not partial_path.exists()
    assert progress["files_uploaded"] == 2
    assert progress["input_tree_files_ready"] == 2
    metadata_path = root / "camera" / SOURCE_FILESYSTEM_METADATA_FILENAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["kind"] == "munchy.input-filesystem-metadata-map"
    assert metadata["files"]["a.mp4"]["stat"]["st_birthtime"] == 1.25
    assert metadata["files"]["b.mp4"]["stat"]["st_birthtime"] == 2.5


def test_get_input_upload_does_not_sync_shared_input_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    upload = {
        "upload_id": "upload-1",
        "files": [
            {
                "path": "camera/a.mp4",
                "bytes": 5,
                "upload_id": "upload-a",
            },
        ],
    }
    runner.save_input_upload_raw(upload)

    monkeypatch.setattr(
        runner,
        "sync_shared_input_tree",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("status read must not materialize input tree")
        ),
    )

    status = runner.get_input_upload("upload-1")

    assert status["files_uploaded"] == 1
    assert not (runner.shared_input_upload_root("upload-1") / "camera" / "a.mp4").exists()


def test_eager_archive_upload_progress_does_not_report_shared_input_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 7,
                    "upload_id": "upload-a",
                    "input_upload_id": "upload-1",
                }
            ],
        }
    )
    runner.tusd_data_path("upload-a").parent.mkdir(parents=True, exist_ok=True)
    runner.tusd_data_path("upload-a").write_bytes(b"video-a")
    monkeypatch.setattr(
        runner,
        "shared_input_tree_progress",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("eager archive progress should not scan shared input tree")
        ),
    )

    progress = runner.upload_progress_for_job(
        {
            "input_upload_id": "upload-1",
            "groups": {
                "camera": {
                    "archive_mode": "av1_nvenc",
                    "tasks": ["archive_video"],
                }
            },
        }
    )

    assert progress is not None
    assert progress["files_uploaded"] == 1
    assert "input_tree_files_ready" not in progress
    assert "input_tree_bytes_ready" not in progress


def test_structured_unrouted_upload_progress_does_not_require_groups(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "storage_hint": {"workflow_mode": "collection_archive", "structured_routing": True},
            "files": [
                {
                    "path": "front-door/clip.mp4",
                    "bytes": 7,
                    "upload_id": "upload-a",
                    "input_upload_id": "upload-1",
                    "structured_routing": True,
                }
            ],
        }
    )
    runner.tusd_data_path("upload-a").parent.mkdir(parents=True, exist_ok=True)
    runner.tusd_data_path("upload-a").write_bytes(b"video-a")

    progress = runner.upload_progress_for_job(
        {
            "input_upload_id": "upload-1",
            "groups": {
                "video": {
                    "archive_mode": "av1_nvenc",
                    "tasks": ["archive_video"],
                },
                "preserve": {"archive_mode": "preserve", "tasks": []},
            },
        }
    )

    assert progress is not None
    assert progress["files_uploaded"] == 1
    assert progress["uploaded_bytes"] == 7
    assert "input_tree_files_ready" not in progress
    assert "input_tree_bytes_ready" not in progress


def test_wait_for_upload_groups_skips_configured_group_with_no_files(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "state": "uploaded",
            "files": [
                {
                    "path": "front-door/clip.mp4",
                    "bytes": 7,
                    "upload_id": "upload-a",
                    "input_upload_id": "upload-1",
                    "resolved_group": "camera",
                    "resolved_group_rel": "front-door/clip.mp4",
                }
            ],
        }
    )
    runner.tusd_data_path("upload-a").parent.mkdir(parents=True, exist_ok=True)
    runner.tusd_data_path("upload-a").write_bytes(b"video-a")
    job = {
        "job_id": "job-1",
        "state": "running",
        "input_upload_id": "upload-1",
        "profile_routing": {"routes": []},
    }
    runner.save_job(job)

    upload = runner.wait_for_upload_groups(
        job,
        "upload-1",
        {"preserve"},
        {"preserve": {"archive_mode": "preserve", "tasks": []}},
    )

    assert upload["upload_id"] == "upload-1"
    assert job["input_upload_progress"]["files_total"] == 0
    assert job["input_upload_progress"]["files_uploaded"] == 0


def test_non_eager_upload_progress_still_reports_shared_input_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_input_upload_raw(
        {
            "upload_id": "upload-1",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 7,
                    "upload_id": "upload-a",
                    "input_upload_id": "upload-1",
                }
            ],
        }
    )
    root = runner.shared_input_upload_root("upload-1")
    (root / "camera").mkdir(parents=True)
    (root / "camera" / "a.mp4").write_bytes(b"video-a")

    progress = runner.upload_progress_for_job(
        {
            "input_upload_id": "upload-1",
            "groups": {
                "camera": {
                    "archive_mode": "av1_nvenc",
                    "tasks": ["qcut_video"],
                }
            },
        }
    )

    assert progress is not None
    assert progress["input_tree_files_ready"] == 1
    assert progress["input_tree_bytes_ready"] == 7


def test_create_file_upload_does_not_sync_entire_shared_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    upload_id = "upload-1"
    rel_path = "camera/a.mp4"
    target_path = runner.target_path_for(upload_id, rel_path)
    runner.write_state(
        "input-upload",
        upload_id,
        {
            "upload_id": upload_id,
            "state": "uploading",
            "created_at": runner.now_iso(),
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "groups": {
                    "camera": {
                        "archive_mode": "av1_nvenc",
                        "tasks": ["archive_video"],
                    }
                },
            },
            "files": [
                {
                    "path": rel_path,
                    "bytes": 10,
                    "sha256": None,
                    "filesystem_metadata": {},
                    "target_path": target_path,
                    "input_upload_id": upload_id,
                    "upload_id": runner.tusd_upload_id_for_target_path(target_path),
                    "upload_url": None,
                }
            ],
            "tusd_creation_url": runner.TUSD_PUBLIC_BASE_URL,
        },
    )
    monkeypatch.setattr(
        runner,
        "sync_shared_input_tree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full sync")),
    )

    class TrackingLock:
        active = False

        def __enter__(self):  # type: ignore[no-untyped-def]
            self.active = True

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            self.active = False

    tracking_lock = TrackingLock()

    def fake_create_tusd_upload(target_path: str, _length: int) -> str:
        assert tracking_lock.active is False
        return f"{runner.TUSD_PUBLIC_BASE_URL}/{target_path}"

    monkeypatch.setattr(runner, "state_lock", tracking_lock)
    monkeypatch.setattr(runner, "create_tusd_upload", fake_create_tusd_upload)

    response = runner.create_or_resume_input_file_upload(upload_id, rel_path)

    assert response["offset"] == 0
    assert response["length"] == 10
    stored = runner.read_state("input-upload", upload_id)
    assert stored is not None
    assert stored["files"][0]["upload_url"] == response["upload_url"]


def test_concurrent_file_upload_setup_creates_one_tusd_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    upload_id = "upload-1"
    rel_path = "camera/a.mp4"
    target_path = runner.target_path_for(upload_id, rel_path)
    runner.write_state(
        "input-upload",
        upload_id,
        {
            "upload_id": upload_id,
            "state": "uploading",
            "created_at": runner.now_iso(),
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "groups": {
                    "camera": {
                        "archive_mode": "av1_nvenc",
                        "tasks": ["archive_video"],
                    }
                },
            },
            "files": [
                {
                    "path": rel_path,
                    "bytes": 10,
                    "sha256": None,
                    "filesystem_metadata": {},
                    "target_path": target_path,
                    "input_upload_id": upload_id,
                    "upload_id": runner.tusd_upload_id_for_target_path(target_path),
                    "upload_url": None,
                }
            ],
            "tusd_creation_url": runner.TUSD_PUBLIC_BASE_URL,
        },
    )

    created_targets: list[str] = []
    responses: list[dict[str, object]] = []
    errors: list[BaseException] = []
    create_started = threading.Event()
    finish_create = threading.Event()
    caller_started = [threading.Event(), threading.Event()]
    created_targets_lock = threading.Lock()
    setup_lock = runner.input_file_upload_setup_lock(upload_id, rel_path)

    def fake_create_tusd_upload(target_path: str, _length: int) -> str:
        with created_targets_lock:
            created_targets.append(target_path)
        create_started.set()
        assert finish_create.wait(timeout=5)
        return f"{runner.TUSD_PUBLIC_BASE_URL}/{target_path}"

    def fake_head_tusd_upload(_upload_url: str) -> int:
        return 0

    def call_upload(index: int) -> None:
        caller_started[index].set()
        try:
            responses.append(runner.create_or_resume_input_file_upload(upload_id, rel_path))
        except BaseException as exc:  # pragma: no cover - re-raised by the parent thread
            errors.append(exc)

    monkeypatch.setattr(runner, "create_tusd_upload", fake_create_tusd_upload)
    monkeypatch.setattr(runner, "head_tusd_upload", fake_head_tusd_upload)

    setup_lock.acquire()
    holding_setup_lock = True
    threads = [threading.Thread(target=call_upload, args=(index,)) for index in range(2)]
    try:
        for thread in threads:
            thread.start()
        assert caller_started[0].wait(timeout=5)
        assert caller_started[1].wait(timeout=5)
        assert not create_started.wait(timeout=0.25)
        setup_lock.release()
        holding_setup_lock = False
        assert create_started.wait(timeout=5)
        finish_create.set()
    finally:
        if holding_setup_lock:
            setup_lock.release()
        finish_create.set()
        for thread in threads:
            thread.join(timeout=5)

    assert errors == []
    assert len(responses) == 2
    assert created_targets == [target_path]
    stored = runner.read_state("input-upload", upload_id)
    assert stored is not None
    assert stored["files"][0]["upload_url"] == f"{runner.TUSD_PUBLIC_BASE_URL}/{target_path}"


def test_resume_file_upload_heads_tusd_outside_state_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    upload_id = "upload-1"
    rel_path = "camera/a.mp4"
    target_path = runner.target_path_for(upload_id, rel_path)
    upload_url = f"{runner.TUSD_PUBLIC_BASE_URL}/{target_path}"
    runner.write_state(
        "input-upload",
        upload_id,
        {
            "upload_id": upload_id,
            "state": "uploading",
            "created_at": runner.now_iso(),
            "files": [
                {
                    "path": rel_path,
                    "bytes": 10,
                    "sha256": None,
                    "filesystem_metadata": {},
                    "target_path": target_path,
                    "input_upload_id": upload_id,
                    "upload_id": runner.tusd_upload_id_for_target_path(target_path),
                    "upload_url": upload_url,
                }
            ],
            "storage_hint": {"source_bytes": 10},
            "tusd_creation_url": runner.TUSD_PUBLIC_BASE_URL,
        },
    )

    class TrackingLock:
        active = False

        def __enter__(self):  # type: ignore[no-untyped-def]
            self.active = True

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            self.active = False

    tracking_lock = TrackingLock()

    def fake_head_tusd_upload(_upload_url: str) -> int:
        assert tracking_lock.active is False
        return 4

    monkeypatch.setattr(runner, "state_lock", tracking_lock)
    monkeypatch.setattr(runner, "head_tusd_upload", fake_head_tusd_upload)

    response = runner.create_or_resume_input_file_upload(upload_id, rel_path)

    assert response["offset"] == 4
    assert response["length"] == 10


def test_sync_shared_input_file_materializes_only_completed_file(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    upload_id = "upload-1"
    complete_path = runner.tusd_data_path("upload-a")
    partial_path = runner.tusd_data_path("upload-b")
    complete_path.parent.mkdir(parents=True, exist_ok=True)
    complete_path.write_bytes(b"video-a")
    partial_path.write_bytes(b"vid")
    runner.write_state(
        "input-upload",
        upload_id,
        {
            "upload_id": upload_id,
            "state": "uploading",
            "created_at": runner.now_iso(),
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "groups": {
                    "camera": {
                        "archive_mode": "av1_nvenc",
                        "tasks": ["archive_video"],
                    }
                },
            },
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 7,
                    "upload_id": "upload-a",
                    "target_path": runner.target_path_for(upload_id, "camera/a.mp4"),
                    "input_upload_id": upload_id,
                    "filesystem_metadata": {"stat": {"st_birthtime": 1.25}},
                },
                {
                    "path": "camera/b.mp4",
                    "bytes": 7,
                    "upload_id": "upload-b",
                    "target_path": runner.target_path_for(upload_id, "camera/b.mp4"),
                    "input_upload_id": upload_id,
                    "filesystem_metadata": {"stat": {"st_birthtime": 2.5}},
                },
            ],
        },
    )

    assert runner.sync_shared_input_file(upload_id, "camera/a.mp4") is True

    root = runner.shared_input_upload_root(upload_id)
    assert (root / "camera" / "a.mp4").read_bytes() == b"video-a"
    assert not complete_path.exists()
    assert not (root / "camera" / "b.mp4").exists()
    assert partial_path.exists()
    assert not (root / "camera" / SOURCE_FILESYSTEM_METADATA_FILENAME).exists()


def test_consume_input_upload_file_removes_only_that_shared_input_file(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    upload = runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 7,
                    "upload_id": "upload-a",
                    "input_upload_id": "upload-1",
                },
                {
                    "path": "camera/b.mp4",
                    "bytes": 7,
                    "upload_id": "upload-b",
                    "input_upload_id": "upload-1",
                },
            ],
        }
    )
    root = runner.shared_input_upload_root("upload-1")
    (root / "camera").mkdir(parents=True)
    (root / "camera" / "a.mp4").write_bytes(b"video-a")
    (root / "camera" / "b.mp4").write_bytes(b"video-b")
    metadata_path = root / "camera" / SOURCE_FILESYSTEM_METADATA_FILENAME
    metadata_path.write_text("{}", encoding="utf-8")

    upload = runner.consume_input_upload_files("upload-1", {"camera/a.mp4"})

    assert not (root / "camera" / "a.mp4").exists()
    assert (root / "camera" / "b.mp4").read_bytes() == b"video-b"
    assert metadata_path.exists()
    by_path = {str(item["path"]): item for item in upload["files"]}
    assert by_path["camera/a.mp4"]["consumed_at"]
    assert by_path["camera/a.mp4"]["complete"] is True
    assert by_path["camera/b.mp4"]["complete"] is True


def test_cleanup_consumed_shared_input_files_removes_existing_consumed_files(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    upload = {
        "upload_id": "upload-1",
        "files": [
            {
                "path": "camera/a.mp4",
                "bytes": 7,
                "upload_id": "upload-a",
                "input_upload_id": "upload-1",
                "consumed_at": "2026-01-01T00:00:00Z",
            },
            {
                "path": "camera/b.mp4",
                "bytes": 7,
                "upload_id": "upload-b",
                "input_upload_id": "upload-1",
            },
        ],
    }
    root = runner.shared_input_upload_root("upload-1")
    (root / "camera").mkdir(parents=True)
    (root / "camera" / "a.mp4").write_bytes(b"video-a")
    (root / "camera" / "b.mp4").write_bytes(b"video-b")

    removed = runner.cleanup_consumed_shared_input_files(upload, {"camera"})
    progress = runner.shared_input_tree_progress(upload, {"camera"})

    assert removed == 1
    assert not (root / "camera" / "a.mp4").exists()
    assert (root / "camera" / "b.mp4").exists()
    assert progress["input_tree_files_ready"] == 2
    assert progress["input_tree_bytes_ready"] == 14


def test_link_or_copy_replaces_destination_atomically_on_copy_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    source = tmp_path / "source.mp4"
    dest = tmp_path / "dest" / "target.mp4"
    source.write_bytes(b"new-video")
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"old-video")
    copy_dests: list[Path] = []

    def fake_link(_source: Path, link_dest: Path) -> None:
        assert link_dest.name.startswith(".target.mp4.")
        assert link_dest.name.endswith(".part")
        raise OSError(errno.EXDEV, "cross-device")

    def fake_copy2(copy_source: Path, copy_dest: Path) -> Path:
        copy_dests.append(copy_dest)
        copy_dest.write_bytes(copy_source.read_bytes())
        return copy_dest

    monkeypatch.setattr(runner.os, "link", fake_link)
    monkeypatch.setattr(runner.shutil, "copy2", fake_copy2)

    runner.link_or_copy(source, dest)

    assert dest.read_bytes() == b"new-video"
    assert len(copy_dests) == 1
    assert copy_dests[0].name.startswith(".target.mp4.")
    assert copy_dests[0].name.endswith(".part")
    assert not copy_dests[0].exists()


def test_materialize_upload_file_reuses_existing_dest_when_tusd_data_is_gone(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    dest_root = tmp_path / "shared"
    dest = dest_root / "camera" / "a.mp4"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"video-a")
    file_state = {"path": "camera/a.mp4", "bytes": 7, "upload_id": "missing-upload"}

    runner.materialize_upload_file(file_state, dest_root)

    assert dest.read_bytes() == b"video-a"


def test_materialize_upload_file_retries_shared_source_when_tusd_disappears(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    upload_id = "upload-1"
    file_state = {
        "path": "camera/a.mp4",
        "bytes": 7,
        "upload_id": "upload-a",
        "input_upload_id": upload_id,
    }
    tusd_source = runner.tusd_data_path("upload-a")
    shared_source = runner.shared_input_file_path(file_state)
    assert shared_source is not None
    tusd_source.parent.mkdir(parents=True, exist_ok=True)
    tusd_source.write_bytes(b"video-a")
    dest_root = tmp_path / "batch"
    calls: list[Path] = []

    def fake_link_or_copy(source: Path, dest: Path) -> None:
        calls.append(source)
        if source == tusd_source:
            tusd_source.unlink()
            shared_source.parent.mkdir(parents=True, exist_ok=True)
            shared_source.write_bytes(b"video-a")
            raise RuntimeError("failed to copy missing tusd source")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source.read_bytes())

    monkeypatch.setattr(runner, "link_or_copy", fake_link_or_copy)

    runner.materialize_upload_file(file_state, dest_root)

    assert calls == [tusd_source, shared_source]
    assert (dest_root / "camera" / "a.mp4").read_bytes() == b"video-a"


def test_run_job_points_gpu_payload_at_shared_input_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "storage_hint": {
                "workflow_mode": "review",
                "archive_mode": "av1_nvenc",
                "tasks": ["qcut_video"],
                "groups": {"camera": {"archive_mode": "av1_nvenc", "tasks": ["qcut_video"]}},
            },
            "files": [{"path": "camera/a.mp4", "bytes": 5, "upload_id": "upload-a"}],
        }
    )
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "workflow_mode": "review",
            "input_upload_id": "upload-1",
            "review": {
                "device_id": "camera",
                "route_id": "camera",
                "profile_id": "webm-q42",
                "target": {"enabled": False},
            },
            "groups": {
                "camera": {
                    "archive_mode": "av1_nvenc",
                    "tasks": ["qcut_video"],
                    "profile": "profile",
                }
            },
        }
    )
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(runner, "acquire_job_gpu", lambda job: "token")
    monkeypatch.setattr(runner, "release_job_gpu", lambda job, token: None)
    monkeypatch.setattr(runner, "start_gpu_job", lambda payload: payloads.append(payload))
    monkeypatch.setattr(
        runner,
        "wait_gpu_job",
        lambda gpu_job_id, *, gpu_payload, job: {"state": "succeeded"},
    )
    monkeypatch.setattr(runner, "upload_target", lambda *args, **kwargs: {"returncode": 0})
    monkeypatch.setattr(runner, "notify_job_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "notify_job_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "schedule_pending_jobs", lambda *args, **kwargs: [])

    runner.run_job("job-1")

    assert len(payloads) == 1
    assert str(payloads[0]["input_dir"]).startswith("/data/input-uploads/")
    assert str(payloads[0]["input_dir"]).endswith("/camera")
    assert "/jobs/job-1/input" not in str(payloads[0]["input_dir"])
    job = runner.load_job("job-1")
    assert job["state"] == "succeeded"
    assert runner.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not runner.shared_input_upload_root("upload-1").exists()


def test_successful_job_keeps_shared_input_upload_for_unfinished_sibling(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "storage_hint": {
                "workflow_mode": "review",
                "archive_mode": "av1_nvenc",
                "tasks": ["qcut_video"],
                "groups": {"camera": {"archive_mode": "av1_nvenc", "tasks": ["qcut_video"]}},
            },
            "files": [{"path": "camera/a.mp4", "bytes": 5, "upload_id": "upload-a"}],
        }
    )
    common_job = {
        "state": "queued",
        "phase": "queued",
        "workflow_mode": "review",
        "input_upload_id": "upload-1",
        "groups": {
            "camera": {
                "archive_mode": "av1_nvenc",
                "tasks": ["qcut_video"],
                "profile": "profile",
            }
        },
    }
    runner.save_job(
        {
            "job_id": "job-1",
            "review": {
                "device_id": "camera",
                "route_id": "camera",
                "profile_id": "webm-q42",
                "target": {"enabled": False},
            },
            **common_job,
        }
    )
    runner.save_job(
        {
            "job_id": "job-2",
            "review": {
                "device_id": "camera",
                "route_id": "camera",
                "profile_id": "webm-q44",
                "target": {"enabled": False},
            },
            **common_job,
        }
    )
    monkeypatch.setattr(runner, "acquire_job_gpu", lambda job: "token")
    monkeypatch.setattr(runner, "release_job_gpu", lambda job, token: None)
    monkeypatch.setattr(runner, "start_gpu_job", lambda payload: None)
    monkeypatch.setattr(
        runner,
        "wait_gpu_job",
        lambda gpu_job_id, *, gpu_payload, job: {"state": "succeeded"},
    )
    monkeypatch.setattr(runner, "upload_target", lambda *args, **kwargs: {"returncode": 0})
    monkeypatch.setattr(runner, "notify_job_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "notify_job_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "schedule_pending_jobs", lambda *args, **kwargs: [])

    runner.run_job("job-1")

    assert runner.load_job("job-1")["state"] == "succeeded"
    assert runner.load_job("job-2")["state"] == "queued"
    assert runner.read_state("input-upload", "upload-1") is not None
    assert not data_path.exists()
    shared_root = runner.shared_input_upload_root("upload-1")
    assert (shared_root / "camera" / "a.mp4").read_bytes() == b"video"


def test_successful_riverhog_handoff_cleans_job_work_and_input_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "storage_hint": {
                "workflow_mode": "collection_archive",
                "archive_mode": "av1_nvenc",
                "tasks": ["archive_video", "qcut_video"],
                "groups": {
                    "camera": {
                        "archive_mode": "av1_nvenc",
                        "tasks": ["archive_video", "qcut_video"],
                    }
                },
            },
            "files": [{"path": "camera/a.mp4", "bytes": 5, "upload_id": "upload-a"}],
        }
    )
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "workflow_mode": "collection_archive",
            "input_upload_id": "upload-1",
            "collection_slug": "camera-archive",
            "collection_timestamp": "20260101T000000Z",
            "groups": {
                "camera": {
                    "archive_mode": "av1_nvenc",
                    "tasks": ["archive_video", "qcut_video"],
                    "profile": "profile",
                    "metadata_projection": {"enabled": False},
                }
            },
            "riverhog": {"enabled": True},
        }
    )
    monkeypatch.setattr(runner, "acquire_job_gpu", lambda job: "token")
    monkeypatch.setattr(runner, "release_job_gpu", lambda job, token: None)
    monkeypatch.setattr(runner, "start_gpu_job", lambda payload: None)

    def wait_gpu_job(gpu_job_id, *, gpu_payload, job):  # type: ignore[no-untyped-def]
        (runner.GPU_RUNTIME_DIR / "jobs" / "job-1" / "archive").mkdir(parents=True)
        return {"state": "succeeded"}

    monkeypatch.setattr(runner, "wait_gpu_job", wait_gpu_job)
    monkeypatch.setattr(runner, "upload_target", lambda *args, **kwargs: {"returncode": 0})
    monkeypatch.setattr(runner, "upload_to_riverhog", lambda *args, **kwargs: {"returncode": 0})
    monkeypatch.setattr(runner, "notify_job_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "notify_job_issue", lambda *args, **kwargs: None)

    runner.run_job("job-1")

    job = runner.load_job("job-1")
    assert job["state"] == "succeeded"
    assert job["riverhog_upload_result"] == {"returncode": 0}
    assert job["cleanup_completed_at"]
    assert "input-upload:upload-1" in job["cleanup_removed"]
    assert runner.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not runner.shared_input_upload_root("upload-1").exists()
    assert not (runner.GPU_RUNTIME_DIR / "jobs" / "job-1").exists()


def test_riverhog_handoff_uses_session_uploads_and_removes_local_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_RIVERHOG_UPLOAD_ENABLED", "1")
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    monkeypatch.setattr(runner, "RIVERHOG_UPLOAD_CHUNK_BYTES", 2)

    archive_dir = runner.GPU_RUNTIME_DIR / "jobs" / "job-1" / "archive"
    video = archive_dir / "camera" / "a.webm"
    sidecar = archive_dir / "camera" / "a.webm.source-artifacts.tar.zst"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    sidecar.write_bytes(b"meta")

    job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "riverhog_upload",
        "workflow_mode": "collection_archive",
        "input_upload_id": "upload-1",
        "collection_slug": "camera-archive",
        "collection_timestamp": "20260101T000000Z",
        "riverhog": {"enabled": True, "wait": "staged"},
        "notify": {"enabled": True, "recipients": ["nash", "katie"]},
    }
    runner.save_job(job)

    class FakeRiverhogApi:
        def __init__(self) -> None:
            self.registered: dict[str, dict[str, object]] = {}
            self.offsets: dict[str, int] = {}
            self.completed = False
            self._tus_client = FakeTusClient(self)

        def close(self) -> None:
            return

        def tus_client(self) -> object:
            return self._tus_client

        def create_or_resume_collection_upload_session(
            self,
            slug: str,
            *,
            ingest_source: str | None = None,
            upload_timestamp: str | None = None,
            notify: dict[str, object] | None = None,
        ) -> dict[str, object]:
            assert slug == "camera-archive"
            assert ingest_source == str(archive_dir)
            assert upload_timestamp == "20260101T000000Z"
            assert notify == {"enabled": True, "recipients": ["nash", "katie"]}
            return self.payload(state="open")

        def get_collection_upload(self, collection_id: str) -> dict[str, object]:
            assert collection_id == "2026/20260101T000000Z__camera-archive"
            return self.payload(state="open")

        def create_or_resume_registered_collection_file_upload(
            self,
            collection_id: str,
            file: dict[str, object],
        ) -> dict[str, object]:
            assert collection_id == "2026/20260101T000000Z__camera-archive"
            path = str(file["path"])
            self.registered[path] = dict(file)
            self.offsets.setdefault(path, 0)
            uploaded = self.offsets.get(path, 0)
            upload_state = "uploaded" if uploaded >= int(file["bytes"]) else "partial"
            return {
                **self.payload(state="open"),
                "path": path,
                "protocol": "tus",
                "upload_url": f"upload://{file['path']}",
                "offset": uploaded,
                "length": int(file["bytes"]),
                "checksum_algorithm": "sha256",
                "expires_at": "2026-01-01T00:00:00Z",
                "file": {
                    **dict(file),
                    "upload_state": upload_state,
                    "uploaded_bytes": uploaded,
                    "upload_state_expires_at": None
                    if upload_state == "uploaded"
                    else "2026-01-01T00:00:00Z",
                },
            }

        def complete_collection_upload_session(self, collection_id: str) -> dict[str, object]:
            assert collection_id == "2026/20260101T000000Z__camera-archive"
            self.completed = True
            return self.payload(state="archiving")

        def payload(self, *, state: str) -> dict[str, object]:
            files = [
                {
                    **payload,
                    "upload_state": "uploaded"
                    if self.offsets.get(path, 0) >= int(payload["bytes"])
                    else "partial",
                    "uploaded_bytes": self.offsets.get(path, 0),
                    "upload_state_expires_at": None,
                }
                for path, payload in sorted(self.registered.items())
            ]
            return {
                "collection_id": "2026/20260101T000000Z__camera-archive",
                "state": state,
                "files_total": len(files),
                "files_uploaded": sum(
                    int(item["uploaded_bytes"]) >= int(item["bytes"]) for item in files
                ),
                "bytes_total": sum(int(item["bytes"]) for item in files),
                "uploaded_bytes": sum(int(item["uploaded_bytes"]) for item in files),
                "missing_bytes": 0,
                "files": files,
            }

    class FakeTusClient:
        def __init__(self, api: FakeRiverhogApi) -> None:
            self.api = api

        def patch_chunk(
            self,
            upload_url: str,
            *,
            offset: int,
            checksum_algorithm: str,
            content: bytes,
        ) -> int:
            assert checksum_algorithm == "sha256"
            path = upload_url.removeprefix("upload://")
            assert offset == self.api.offsets[path]
            self.api.offsets[path] = offset + len(content)
            return self.api.offsets[path]

    fake = FakeRiverhogApi()
    monkeypatch.setattr(runner, "ApiClient", lambda: fake)
    monkeypatch.setattr(runner, "notify_job_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "notify_job_issue", lambda *args, **kwargs: None)

    result = runner.upload_to_riverhog(job, archive_dir)

    assert result["method"] == "session"
    assert result["collection_id"] == "2026/20260101T000000Z__camera-archive"
    assert fake.completed is True
    assert sorted(fake.registered) == [
        "camera/a.webm",
        "camera/a.webm.source-artifacts.tar.zst",
    ]
    assert not video.exists()
    assert not sidecar.exists()
    progress = runner.riverhog_upload_progress_for_job(job)
    assert progress["files_uploaded"] == 2
    assert progress["bytes_total"] == 9
    metrics = job["riverhog_handoff_metrics"]
    assert metrics["wait"] == "staged"
    assert metrics["final_sweep_processed_files"] == 2
    assert metrics["final_sweep_uploaded_files"] == 2
    assert metrics["final_sweep_uploaded_bytes"] == 9
    assert metrics["final_sweep_before"] == {}
    assert metrics["final_sweep_after"]["artifact_files_uploaded"] == 2
    assert metrics["session_complete_elapsed_seconds"] >= 0
    assert metrics["finished_at"]
    assert result["metrics"]["final_sweep_uploaded_files"] == 2
    assert result["metrics"]["session_complete_elapsed_seconds"] >= 0


def test_expected_riverhog_primary_files_total_counts_archive_outputs(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    upload = {
        "files": [
            {"path": "video/a.mp4", "bytes": 1},
            {"path": "video/b.mp4", "bytes": 1},
            {"path": "review/c.mp4", "bytes": 1},
        ],
    }
    groups = {
        "video": {"tasks": ["archive_video"]},
        "review": {"tasks": ["qcut_video"]},
    }

    assert runner.expected_riverhog_primary_files_total(upload, groups) == 2


def test_eager_riverhog_upload_can_be_bounded_per_tick(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()

    archive_dir = tmp_path / "archive"
    first = archive_dir / "camera" / "a.webm"
    second = archive_dir / "camera" / "b.webm"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    job = {
        "job_id": "job-1",
        "riverhog": {"enabled": True},
        "riverhog_session_upload": {
            "state": "open",
            "collection_id": "2026/20260101T000000Z__camera-archive",
            "files": {},
        },
        "eager_archive": {
            "files": {
                "camera/a.mp4": {"state": "encoded", "output": str(first)},
                "camera/b.mp4": {"state": "encoded", "output": str(second)},
            },
        },
    }
    uploaded: list[str] = []

    class FakeRiverhogApi:
        def close(self) -> None:
            return

    monkeypatch.setattr(runner, "ApiClient", lambda: FakeRiverhogApi())
    monkeypatch.setattr(
        runner,
        "ensure_riverhog_session",
        lambda job, api, archive_dir: "2026/20260101T000000Z__camera-archive",
    )

    def fake_upload_artifact(job, api, archive_dir, source_path, **_kwargs):  # type: ignore[no-untyped-def]
        uploaded.append(Path(source_path).name)
        return True

    monkeypatch.setattr(runner, "riverhog_upload_artifact", fake_upload_artifact)

    result = runner.upload_riverhog_artifacts(job, archive_dir, final=False, max_files=1)

    assert result["uploaded_files"] == 1
    assert result["processed_files"] == 1
    assert uploaded == ["a.webm"]


def test_eager_riverhog_upload_can_be_bounded_by_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()

    archive_dir = tmp_path / "archive"
    first = archive_dir / "camera" / "a.webm"
    second = archive_dir / "camera" / "b.webm"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"aa")
    second.write_bytes(b"bb")
    job = {
        "job_id": "job-1",
        "riverhog": {"enabled": True},
        "riverhog_session_upload": {
            "state": "open",
            "collection_id": "2026/20260101T000000Z__camera-archive",
            "files": {},
        },
        "eager_archive": {
            "files": {
                "camera/a.mp4": {"state": "encoded", "output": str(first)},
                "camera/b.mp4": {"state": "encoded", "output": str(second)},
            },
        },
    }
    uploaded: list[str] = []

    class FakeRiverhogApi:
        def close(self) -> None:
            return

    monkeypatch.setattr(runner, "ApiClient", lambda: FakeRiverhogApi())
    monkeypatch.setattr(
        runner,
        "ensure_riverhog_session",
        lambda job, api, archive_dir: "2026/20260101T000000Z__camera-archive",
    )

    def fake_upload_artifact(job, api, archive_dir, source_path, **_kwargs):  # type: ignore[no-untyped-def]
        uploaded.append(Path(source_path).name)
        return True

    monkeypatch.setattr(runner, "riverhog_upload_artifact", fake_upload_artifact)

    result = runner.upload_riverhog_artifacts(
        job,
        archive_dir,
        final=False,
        max_files=100,
        max_bytes=2,
    )

    assert result["uploaded_files"] == 1
    assert result["uploaded_bytes"] == 2
    assert uploaded == ["a.webm"]


def test_eager_riverhog_upload_uses_parallel_workers(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    monkeypatch.setattr(runner, "RIVERHOG_UPLOAD_WORKERS", 2)

    archive_dir = tmp_path / "archive"
    first = archive_dir / "camera" / "a.webm"
    second = archive_dir / "camera" / "b.webm"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    job = {
        "job_id": "job-1",
        "riverhog": {"enabled": True},
        "riverhog_session_upload": {
            "state": "open",
            "collection_id": "2026/20260101T000000Z__camera-archive",
            "files": {},
        },
        "eager_archive": {
            "files": {
                "camera/a.mp4": {"state": "encoded", "output": str(first)},
                "camera/b.mp4": {"state": "encoded", "output": str(second)},
            },
        },
    }

    class FakeRiverhogApi:
        def close(self) -> None:
            return

    monkeypatch.setattr(runner, "ApiClient", lambda: FakeRiverhogApi())
    monkeypatch.setattr(
        runner,
        "ensure_riverhog_session",
        lambda job, api, archive_dir: "2026/20260101T000000Z__camera-archive",
    )

    barrier = threading.Barrier(2)
    active = 0
    max_active = 0
    seen: list[str] = []
    lock = threading.Lock()

    def fake_upload_artifact(job, api, archive_dir, source_path, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            seen.append(Path(source_path).name)
        barrier.wait(timeout=2)
        with lock:
            active -= 1
        return True

    monkeypatch.setattr(runner, "riverhog_upload_artifact", fake_upload_artifact)

    result = runner.upload_riverhog_artifacts(job, archive_dir, final=False, max_files=2)

    assert result["uploaded_files"] == 2
    assert result["processed_files"] == 2
    assert sorted(seen) == ["a.webm", "b.webm"]
    assert max_active == 2


def test_eager_riverhog_upload_reuses_client_per_worker_thread(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    monkeypatch.setattr(runner, "RIVERHOG_UPLOAD_WORKERS", 2)

    archive_dir = tmp_path / "archive"
    outputs = [archive_dir / "camera" / f"{name}.webm" for name in ("a", "b", "c", "d")]
    outputs[0].parent.mkdir(parents=True)
    for output in outputs:
        output.write_bytes(b"x")
    job = {
        "job_id": "job-1",
        "riverhog": {"enabled": True},
        "riverhog_session_upload": {
            "state": "open",
            "collection_id": "2026/20260101T000000Z__camera-archive",
            "files": {},
        },
        "eager_archive": {
            "files": {
                f"camera/{output.stem}.mp4": {"state": "encoded", "output": str(output)}
                for output in outputs
            },
        },
    }

    created: list[object] = []
    closed: list[object] = []

    class FakeRiverhogApi:
        def __init__(self) -> None:
            created.append(self)

        def close(self) -> None:
            closed.append(self)

    monkeypatch.setattr(runner, "ApiClient", FakeRiverhogApi)
    monkeypatch.setattr(
        runner,
        "ensure_riverhog_session",
        lambda job, api, archive_dir: "2026/20260101T000000Z__camera-archive",
    )

    barrier = threading.Barrier(2)
    started = 0
    lock = threading.Lock()
    api_ids_by_thread: dict[int, set[int]] = {}

    def fake_upload_artifact(job, api, archive_dir, source_path, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal started
        thread_id = threading.get_ident()
        with lock:
            started += 1
            api_ids_by_thread.setdefault(thread_id, set()).add(id(api))
            should_wait = started <= 2
        if should_wait:
            barrier.wait(timeout=2)
        return True

    monkeypatch.setattr(runner, "riverhog_upload_artifact", fake_upload_artifact)

    result = runner.upload_riverhog_artifacts(job, archive_dir, final=False, max_files=4)

    assert result["uploaded_files"] == 4
    assert result["processed_files"] == 4
    assert len(created) == 2
    assert sorted(id(client) for client in closed) == sorted(id(client) for client in created)
    assert all(len(api_ids) == 1 for api_ids in api_ids_by_thread.values())


def test_save_job_preserves_newer_riverhog_upload_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "eager_archive",
            "riverhog_session_upload": {
                "state": "open",
                "updated_at": "2026-01-01T00:00:02Z",
                "files": {"camera/a.webm": {"state": "uploaded"}},
            },
        }
    )

    runner.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "eager_archive:pipeline=3/3",
            "riverhog_session_upload": {
                "state": "open",
                "updated_at": "2026-01-01T00:00:01Z",
                "files": {},
            },
        }
    )

    stored = runner.load_job("job-1")
    assert stored["phase"] == "eager_archive:pipeline=3/3"
    assert stored["riverhog_session_upload"]["files"] == {"camera/a.webm": {"state": "uploaded"}}


def test_handoff_retry_refreshes_persisted_job_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    monkeypatch.setattr(runner, "HANDOFF_RETRY_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(runner, "HANDOFF_RETRY_MAX_SECONDS", 0.01)

    stale_job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "riverhog_upload_retrying",
        "riverhog_session_upload": {
            "state": "open",
            "updated_at": "2026-01-01T00:00:01Z",
            "files": {
                "camera/a.webm": {
                    "path": "camera/a.webm",
                    "bytes": 10,
                    "uploaded_bytes": 0,
                    "state": "registered",
                }
            },
        },
    }
    runner.save_job(stale_job)
    attempts = 0

    def operation() -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if not runner.all_riverhog_session_files_uploaded(stale_job):
            runner.save_job(
                {
                    "job_id": "job-1",
                    "state": "running",
                    "phase": "riverhog_upload_retrying",
                    "riverhog_session_upload": {
                        "state": "open",
                        "updated_at": "2026-01-01T00:00:02Z",
                        "files": {
                            "camera/a.webm": {
                                "path": "camera/a.webm",
                                "bytes": 10,
                                "uploaded_bytes": 10,
                                "state": "deleted",
                            }
                        },
                    },
                }
            )
            raise RuntimeError("riverhog upload did not upload every registered file")
        return {"ok": True}

    result = runner.retry_handoff_until_success(
        stale_job,
        result_key="riverhog_upload_result",
        phase="riverhog_upload",
        action="riverhog upload",
        component="riverhog_upload",
        operation=operation,
    )

    assert result["ok"] is True
    assert attempts == 2
    assert stale_job["riverhog_session_upload"]["files"]["camera/a.webm"]["state"] == "deleted"


def test_save_job_compacts_gpu_results_before_persisting(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()

    runner.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "eager_archive:pipeline=1/3",
            "eager_archive": {
                "files": {},
                "batches": {
                    "batch-1": {
                        "state": "succeeded",
                        "gpu_result": {
                            "job_id": "gpu-job-1",
                            "state": "succeeded",
                            "profile": "camera-archive",
                            "items": {
                                "archive_video": [
                                    {
                                        "source": "/data/in/a.mp4",
                                        "output": "/data/out/a.webm",
                                        "command": ["ffmpeg", "-i", "a.mp4"],
                                        "source_artifacts": {"audit": {"ok": True}},
                                    }
                                ]
                            },
                        },
                    }
                },
                "gpu_results": {
                    "batch-1": {
                        "job_id": "gpu-job-1",
                        "state": "succeeded",
                        "items": {"archive_video": [{"command": ["ffmpeg"]}]},
                    }
                },
            },
        }
    )

    stored = runner.load_job("job-1")
    batch_result = stored["eager_archive"]["batches"]["batch-1"]["gpu_result"]
    stored_result = stored["eager_archive"]["gpu_results"]["batch-1"]
    assert batch_result == {
        "job_id": "gpu-job-1",
        "state": "succeeded",
        "profile": "camera-archive",
        "item_counts": {"archive_video": 1},
    }
    assert stored_result == {
        "job_id": "gpu-job-1",
        "state": "succeeded",
        "item_counts": {"archive_video": 1},
    }


def test_save_job_merges_newer_eager_archive_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "eager_archive:pipeline=2/3",
            "eager_archive": {
                "next_batch_number": 7,
                "files": {
                    "camera/a.mp4": {
                        "state": "encoded",
                        "encoded_at": "2026-01-01T00:00:03Z",
                        "output_bytes": 123,
                    }
                },
                "batches": {
                    "batch-1": {
                        "state": "succeeded",
                        "finished_at": "2026-01-01T00:00:03Z",
                    }
                },
                "gpu_results": {"batch-1": {"state": "succeeded"}},
            },
        }
    )

    runner.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "riverhog-upload",
            "eager_archive": {
                "next_batch_number": 4,
                "files": {
                    "camera/a.mp4": {
                        "state": "encoding",
                        "started_at": "2026-01-01T00:00:01Z",
                    },
                    "camera/b.mp4": {
                        "state": "encoding",
                        "started_at": "2026-01-01T00:00:04Z",
                    },
                },
                "batches": {
                    "batch-1": {
                        "state": "running",
                        "started_at": "2026-01-01T00:00:01Z",
                    },
                    "batch-2": {
                        "state": "running",
                        "started_at": "2026-01-01T00:00:04Z",
                    },
                },
            },
        }
    )

    stored = runner.load_job("job-1")
    eager = stored["eager_archive"]
    assert eager["next_batch_number"] == 7
    assert eager["files"]["camera/a.mp4"]["state"] == "encoded"
    assert eager["files"]["camera/a.mp4"]["output_bytes"] == 123
    assert eager["files"]["camera/b.mp4"]["state"] == "encoding"
    assert eager["batches"]["batch-1"]["state"] == "succeeded"
    assert eager["batches"]["batch-2"]["state"] == "running"
    assert eager["gpu_results"] == {"batch-1": {"state": "succeeded"}}


def test_save_job_does_not_resurrect_terminal_job_from_stale_worker_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "cancelled",
            "phase": "cancelled",
            "finished_at": "2026-01-01T00:00:02Z",
        }
    )

    saved = runner.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "cancel_requested",
            "cancel_requested": True,
            "updated_at": "2026-01-01T00:00:01Z",
        }
    )

    assert saved["state"] == "cancelled"
    stored = runner.load_job("job-1")
    assert stored["state"] == "cancelled"
    assert stored["phase"] == "cancelled"


def test_save_job_allows_explicit_resume_from_cancelled_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "cancelled",
            "phase": "cancelled",
            "finished_at": "2026-01-01T00:00:02Z",
        }
    )

    saved = runner.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "_allow_clear_cancel": True,
        }
    )

    assert saved["state"] == "queued"
    assert runner.load_job("job-1")["state"] == "queued"


def test_riverhog_upload_progress_uses_expected_archive_output_count(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    monkeypatch.setattr(
        runner,
        "encode_progress_for_job",
        lambda job: {"files_total": 69},
    )

    files = {
        f"camera/{index}.webm": {
            "path": f"camera/{index}.webm",
            "bytes": 100,
            "uploaded_bytes": 100 if index < 6 else 0,
            "state": "uploaded" if index < 6 else "registered",
            "upload_state": "uploaded" if index < 6 else "partial",
        }
        for index in range(7)
    }
    job = {
        "job_id": "job-1",
        "riverhog": {"enabled": True},
        "riverhog_expected_primary_files_total": 3636,
        "riverhog_session_upload": {
            "state": "open",
            "collection_id": "2026/20260101T000000Z__camera-archive",
            "files": files,
        },
    }

    progress = runner.riverhog_upload_progress_for_job(job)

    assert progress["registered_files_total"] == 7
    assert progress["expected_primary_files_total"] == 3636
    assert progress["files_total"] == 3636
    assert progress["files_uploaded"] == 6
    assert progress["primary_files_total"] == 3636
    assert progress["primary_files_uploaded"] == 6
    assert progress["artifact_files_known"] == 7
    assert progress["artifact_files_uploaded"] == 6
    assert progress["percent_files"] == 0.17
    assert progress["percent_primary_files"] == 0.17


def test_sync_riverhog_session_uses_finalized_collection_when_upload_is_gone(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    job = {
        "job_id": "job-1",
        "riverhog": {"enabled": True},
        "riverhog_session_upload": {
            "state": "archiving",
            "collection_id": "2026/20260101T000000Z__camera-archive",
            "files": {},
        },
    }

    class FakeApi:
        def get_collection_upload(self, collection_id: str) -> dict[str, object]:
            assert collection_id == "2026/20260101T000000Z__camera-archive"
            raise runner.NotFound("already finalized")

        def get_collection(self, collection_id: str) -> dict[str, object]:
            assert collection_id == "2026/20260101T000000Z__camera-archive"
            return {
                "id": collection_id,
                "files": 7,
                "bytes": 1234,
                "glacier": {"state": "uploaded", "stored_bytes": 567},
            }

    payload = runner.sync_riverhog_session_from_remote(job, FakeApi())  # type: ignore[arg-type]
    progress = runner.riverhog_upload_progress_for_job(job)

    assert payload is not None
    assert payload["state"] == "finalized"
    assert progress["state"] == "finalized"
    assert progress["safe_to_delete"] is True
    assert progress["hot_promoted_files"] == 7
    assert progress["riverhog_bytes_total"] == 1234
    assert progress["archive_uploaded_bytes"] == 567


def test_refresh_riverhog_session_uses_compacted_upload_result(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    job = {
        "job_id": "job-1",
        "state": "succeeded",
        "riverhog": {"enabled": True, "wait": "staged"},
        "riverhog_upload_result": {
            "collection_id": "2026/20260101T000000Z__camera-archive",
            "wait": "staged",
        },
    }
    runner.save_job(job)

    class FakeApi:
        def get_collection_upload(self, collection_id: str) -> dict[str, object]:
            assert collection_id == "2026/20260101T000000Z__camera-archive"
            return {
                "collection_id": collection_id,
                "state": "archiving",
                "files_total": 14,
                "files_uploaded": 14,
                "bytes_total": 1234,
                "uploaded_bytes": 1234,
                "hot_promoted_files": 0,
                "hot_promoted_bytes": 0,
                "archive_phase": "uploading",
                "archive_uploaded_bytes": 256,
                "archive_total_bytes": 1024,
                "archive_uploaded_parts": 1,
                "archive_total_parts": 4,
            }

        def close(self) -> None:
            pass

    monkeypatch.setattr(runner, "ApiClient", FakeApi)

    job = runner.load_job("job-1")
    runner.refresh_riverhog_session_from_remote(job)
    refreshed = runner.job_response(job)
    progress = refreshed["riverhog_upload_progress"]

    assert progress["collection_id"] == "2026/20260101T000000Z__camera-archive"
    assert progress["state"] == "archiving"
    assert progress["archive_phase"] == "uploading"
    assert progress["archive_uploaded_bytes"] == 256
    assert progress["archive_total_bytes"] == 1024
    assert progress["archive_uploaded_parts"] == 1
    assert progress["archive_total_parts"] == 4
    assert progress["safe_to_delete"] is False


def test_riverhog_upload_progress_prefers_recent_burst_rate(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    monkeypatch.setattr(runner, "encode_progress_for_job", lambda job: {"files_total": 1})

    job = {
        "job_id": "job-1",
        "riverhog": {"enabled": True},
        "riverhog_session_upload": {
            "state": "open",
            "collection_id": "2026/20260101T000000Z__camera-archive",
            "last_eager_upload_at": runner.now_iso(),
            "last_eager_upload_bytes": 2048,
            "last_eager_upload_elapsed_seconds": 2.0,
            "files": {
                "camera/a.webm": {
                    "path": "camera/a.webm",
                    "bytes": 2048,
                    "uploaded_bytes": 2048,
                    "state": "uploaded",
                },
            },
        },
    }

    progress = runner.riverhog_upload_progress_for_job(job)

    assert progress["recent_rate_bytes_per_second"] == 1024
    assert progress["rate_bytes_per_second"] == 1024


def test_eager_handoff_metrics_survive_newer_persisted_job_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    monkeypatch.setattr(runner, "RIVERHOG_UPLOAD_ENABLED", True)
    monkeypatch.setattr(runner, "now_iso", lambda: "2026-01-01T00:00:03Z")
    monkeypatch.setattr(
        runner,
        "upload_riverhog_artifacts",
        lambda *args, **kwargs: {  # noqa: ARG005
            "processed_files": 1,
            "uploaded_files": 1,
            "uploaded_bytes": 2048,
            "elapsed_seconds": 2.0,
        },
    )

    runner.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "eager_archive:pipeline=3/3",
            "riverhog": {"enabled": True},
            "riverhog_session_upload": {
                "state": "open",
                "updated_at": "2026-01-01T00:00:04Z",
                "files": {},
            },
        }
    )
    stale_worker_job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "eager_archive:pipeline=3/3",
        "riverhog": {"enabled": True},
        "riverhog_session_upload": {
            "state": "open",
            "updated_at": "2026-01-01T00:00:01Z",
            "files": {},
        },
    }

    runner.maybe_upload_riverhog_artifacts(stale_worker_job, tmp_path / "archive")
    stored = runner.load_job("job-1")
    state = stored["riverhog_session_upload"]

    assert state["last_eager_upload_at"] == "2026-01-01T00:00:03Z"
    assert state["last_eager_upload_files"] == 1
    assert state["last_eager_upload_bytes"] == 2048
    assert state["last_eager_upload_elapsed_seconds"] == 2.0
    assert state["updated_at"] == "2026-01-01T00:00:04Z"


def test_stale_eager_handoff_metrics_do_not_replace_newer_persisted_metrics(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()

    runner.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "riverhog": {"enabled": True},
            "riverhog_session_upload": {
                "state": "open",
                "updated_at": "2026-01-01T00:00:04Z",
                "last_eager_upload_at": "2026-01-01T00:00:04Z",
                "last_eager_upload_files": 4,
                "last_eager_upload_bytes": 4096,
                "last_eager_upload_elapsed_seconds": 1.0,
                "files": {},
            },
        }
    )

    runner.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "riverhog": {"enabled": True},
            "riverhog_session_upload": {
                "state": "open",
                "updated_at": "2026-01-01T00:00:05Z",
                "last_eager_upload_at": "2026-01-01T00:00:02Z",
                "last_eager_upload_files": 1,
                "last_eager_upload_bytes": 1024,
                "last_eager_upload_elapsed_seconds": 3.0,
                "files": {},
            },
        }
    )
    state = runner.load_job("job-1")["riverhog_session_upload"]

    assert state["last_eager_upload_at"] == "2026-01-01T00:00:04Z"
    assert state["last_eager_upload_files"] == 4
    assert state["last_eager_upload_bytes"] == 4096
    assert state["last_eager_upload_elapsed_seconds"] == 1.0


def test_riverhog_upload_progress_counts_known_local_sidecars(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    monkeypatch.setattr(
        runner,
        "encode_progress_for_job",
        lambda job: {"files_total": 1},
    )

    video = runner.GPU_RUNTIME_DIR / "jobs" / "job-1" / "archive" / "camera" / "a.webm"
    sidecar = runner.source_artifact_sidecar_for_archive_output(video)
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    sidecar.write_bytes(b"meta")
    job = {
        "job_id": "job-1",
        "riverhog": {"enabled": True},
        "riverhog_session_upload": {
            "state": "open",
            "collection_id": "2026/20260101T000000Z__camera-archive",
            "files": {},
        },
        "eager_archive": {
            "files": {
                "camera/a.mp4": {
                    "state": "encoded",
                    "output": str(video),
                },
            },
        },
    }

    progress = runner.riverhog_upload_progress_for_job(job)

    assert progress["registered_files_total"] == 0
    assert progress["local_artifacts_total"] == 2
    assert progress["files_total"] == 1
    assert progress["primary_files_total"] == 1
    assert progress["primary_files_encoded"] == 1
    assert progress["primary_files_uploaded"] == 0
    assert progress["artifact_files_known"] == 2
    assert progress["artifact_files_pending_local"] == 2


def test_cancel_riverhog_upload_session_cancels_open_session(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_RIVERHOG_UPLOAD_ENABLED", "1")
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()

    job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "riverhog_upload",
        "riverhog": {"enabled": True, "wait": "staged"},
        "riverhog_session_upload": {
            "state": "open",
            "collection_id": "2026/20260101T000000Z__camera-archive",
            "files": {},
        },
    }
    runner.save_job(job)

    class FakeRiverhogApi:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        def close(self) -> None:
            return

        def cancel_collection_upload_session(self, collection_id: str) -> dict[str, object]:
            self.cancelled.append(collection_id)
            return {
                "collection_id": collection_id,
                "state": "canceled",
                "files_total": 0,
                "files_uploaded": 0,
                "bytes_total": 0,
                "uploaded_bytes": 0,
                "missing_bytes": 0,
                "files": [],
            }

    fake = FakeRiverhogApi()
    monkeypatch.setattr(runner, "ApiClient", lambda: fake)

    runner.cancel_riverhog_upload_session(job, reason="test")

    assert fake.cancelled == ["2026/20260101T000000Z__camera-archive"]
    stored = runner.load_job("job-1")
    assert stored["riverhog_session_upload"]["state"] == "canceled"
    assert stored["riverhog_session_upload"]["cancelled_at"]
    assert stored["riverhog_session_upload"]["cancel_reason"] == "test"


def test_cancel_riverhog_upload_session_derives_unpersisted_session_id(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_RIVERHOG_UPLOAD_ENABLED", "1")
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()

    job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "cancel_requested",
        "collection_slug": "camera-archive",
        "collection_timestamp": "20260101T000000Z",
        "riverhog": {"enabled": True, "wait": "staged"},
    }
    runner.save_job(job)

    class FakeRiverhogApi:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        def close(self) -> None:
            return

        def cancel_collection_upload_session(self, collection_id: str) -> dict[str, object]:
            self.cancelled.append(collection_id)
            return {
                "collection_id": collection_id,
                "state": "canceled",
                "files_total": 0,
                "files_uploaded": 0,
                "bytes_total": 0,
                "uploaded_bytes": 0,
                "missing_bytes": 0,
                "files": [],
            }

    fake = FakeRiverhogApi()
    monkeypatch.setattr(runner, "ApiClient", lambda: fake)

    runner.cancel_riverhog_upload_session(job, reason="test")

    assert fake.cancelled == ["2026/20260101T000000Z__camera-archive"]
    stored = runner.load_job("job-1")
    assert stored["riverhog_session_upload"]["collection_id"] == (
        "2026/20260101T000000Z__camera-archive"
    )
    assert stored["riverhog_session_upload"]["state"] == "canceled"
    assert stored["riverhog_session_upload"]["cancelled_at"]
    assert stored["riverhog_session_upload"]["cancel_reason"] == "test"


def test_cancel_riverhog_upload_session_treats_missing_session_as_clean(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_RUNNER_RIVERHOG_UPLOAD_ENABLED", "1")
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()

    job = runner.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "cancel_requested",
            "collection_slug": "camera-archive",
            "collection_timestamp": "20260101T000000Z",
            "riverhog": {"enabled": True, "wait": "staged"},
        }
    )

    class FakeRiverhogApi:
        def close(self) -> None:
            return

        def cancel_collection_upload_session(self, collection_id: str) -> dict[str, object]:
            raise runner.NotFound("missing")

    monkeypatch.setattr(runner, "ApiClient", FakeRiverhogApi)

    runner.cancel_riverhog_upload_session(job, reason="test")

    stored = runner.load_job("job-1")
    assert stored["riverhog_session_upload"]["state"] == "canceled"
    assert stored["riverhog_session_upload"]["riverhog_state"] == "absent"
    assert stored["riverhog_session_upload"]["cancel_not_found"] is True
    assert stored["riverhog_session_upload"]["cancelled_at"]


def test_repeated_cleanup_updates_empty_cleanup_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    work_dir = runner.GPU_RUNTIME_DIR / "jobs" / "job-1"
    work_dir.mkdir(parents=True)
    (work_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    job = {
        "job_id": "job-1",
        "state": "cancelled",
        "phase": "cancelled",
        "cleanup_completed_at": "2026-01-01T00:00:00Z",
        "cleanup_removed": [],
        "cleanup_removed_count": 0,
    }

    runner.cleanup_terminal_job(job)
    runner.compact_terminal_job_state(job)

    assert job["cleanup_removed_count"] == 1
    assert job["cleanup_removed"] == [str(work_dir)]
    assert job["local_work_removed_count"] == 1


def test_encoding_failure_cleans_job_work_and_input_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "storage_hint": {
                "workflow_mode": "review",
                "archive_mode": "av1_nvenc",
                "tasks": ["qcut_video"],
                "groups": {"camera": {"archive_mode": "av1_nvenc", "tasks": ["qcut_video"]}},
            },
            "files": [{"path": "camera/a.mp4", "bytes": 5, "upload_id": "upload-a"}],
        }
    )
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "queued",
            "phase": "queued",
            "workflow_mode": "review",
            "input_upload_id": "upload-1",
            "review": {
                "device_id": "camera",
                "route_id": "camera",
                "profile_id": "webm-q42",
                "target": {"enabled": False},
            },
            "groups": {
                "camera": {
                    "archive_mode": "av1_nvenc",
                    "tasks": ["qcut_video"],
                    "profile": "profile",
                }
            },
        }
    )
    monkeypatch.setattr(runner, "acquire_job_gpu", lambda job: "token")
    monkeypatch.setattr(runner, "release_job_gpu", lambda job, token: None)
    monkeypatch.setattr(runner, "start_gpu_job", lambda payload: None)
    monkeypatch.setattr(
        runner,
        "wait_gpu_job",
        lambda gpu_job_id, *, gpu_payload, job: (_ for _ in ()).throw(
            runner.EncodingFailed("gpu job failed: bad encode")
        ),
    )
    monkeypatch.setattr(runner, "notify_job_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "notify_job_issue", lambda *args, **kwargs: None)

    runner.run_job("job-1")

    job = runner.load_job("job-1")
    assert job["state"] == "failed"
    assert job["error"] == "gpu job failed: bad encode"
    assert job["cleanup_completed_at"]
    assert "input-upload:upload-1" in job["cleanup_removed"]
    assert runner.read_state("input-upload", "upload-1") is None
    assert not data_path.exists()
    assert not runner.shared_input_upload_root("upload-1").exists()
    assert not (runner.GPU_RUNTIME_DIR / "jobs" / "job-1").exists()


def test_run_job_reuses_stored_shared_review_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    data_path = runner.tusd_data_path("upload-a")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"video")
    plan = {
        "kind": "munchy.qcut-plan",
        "version": 1,
        "clips": [{"index": 1, "source": "/data/input-uploads/upload/camera/a.mp4"}],
        "files": [{"path": "/data/input-uploads/upload/camera/a.mp4"}],
    }
    runner.store_shared_review_plan("upload-1", "camera", "qcut_video", plan)
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "created_at": "2026-01-01T00:00:00Z",
            "storage_hint": {
                "workflow_mode": "review",
                "archive_mode": "av1_nvenc",
                "tasks": ["qcut_video"],
                "groups": {"camera": {"archive_mode": "av1_nvenc", "tasks": ["qcut_video"]}},
            },
            "files": [{"path": "camera/a.mp4", "bytes": 5, "upload_id": "upload-a"}],
        }
    )
    runner.save_job(
        {
            "job_id": "job-2",
            "state": "queued",
            "phase": "queued",
            "workflow_mode": "review",
            "input_upload_id": "upload-1",
            "review": {
                "device_id": "camera",
                "route_id": "camera",
                "profile_id": "webm-q43",
                "target": {"enabled": False},
                "clip_plan": {
                    "target_seconds": 90,
                    "min_seconds": 4,
                    "max_seconds": 7,
                },
            },
            "groups": {
                "camera": {
                    "archive_mode": "av1_nvenc",
                    "tasks": ["qcut_video"],
                    "profile": "profile",
                }
            },
        }
    )
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(runner, "acquire_job_gpu", lambda job: "token")
    monkeypatch.setattr(runner, "release_job_gpu", lambda job, token: None)
    monkeypatch.setattr(runner, "start_gpu_job", lambda payload: payloads.append(payload))
    monkeypatch.setattr(
        runner,
        "wait_gpu_job",
        lambda gpu_job_id, *, gpu_payload, job: {"state": "succeeded"},
    )
    monkeypatch.setattr(runner, "upload_target", lambda *args, **kwargs: {"returncode": 0})
    monkeypatch.setattr(runner, "notify_job_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "notify_job_issue", lambda *args, **kwargs: None)

    runner.run_job("job-2")

    assert payloads[0]["review_plans"]["qcut_video"]["kind"] == "munchy.qcut-plan"  # type: ignore[index]
    assert payloads[0]["review_plans"]["qcut_video"]["shared_plan"]["upload_id"] == "upload-1"  # type: ignore[index]
    assert payloads[0]["review_clip_plan"] == {
        "target_seconds": 90,
        "min_seconds": 4,
        "max_seconds": 7,
    }


def test_preflight_issue_notification_error_keeps_truncated_filename_at_end(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    filename = "example-camera-" + ("very-long-" * 12) + "clip.mp4"

    error = runner.preflight_issue_notification_error(
        path=f"camera-video/2026/06/06/{filename}",
        issue_message=(
            "ffprobe failed because the MP4 atom table points past the end of the "
            "available local file"
        ),
    )

    assert len(error) <= 120
    assert error.startswith("ffprobe failed because")
    assert error.endswith("clip.mp4)")
    assert " (..." in error


def test_ready_eager_files_skips_claimed_encoding_files(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    (runner.TUSD_DIR / "upload-b").write_bytes(b"b")
    job = {
        "job_id": "job-1",
        "eager_archive": {
            "files": {
                "camera/a.mp4": {
                    "state": "encoding",
                    "batch_id": "batch-1",
                },
                "camera/c.mp4": {
                    "state": "failed",
                    "batch_id": "batch-2",
                },
            },
            "batches": {},
            "next_batch_number": 1,
        },
    }
    upload = {
        "upload_id": "upload-1",
        "files": [
            {"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"},
            {"path": "camera/b.mp4", "bytes": 1, "upload_id": "upload-b"},
            {"path": "camera/c.mp4", "bytes": 1, "upload_id": "upload-c"},
        ],
    }

    ready = runner.ready_eager_files(
        job,
        upload,
        {"camera": {}},
        {"camera"},
        tmp_path / "archive",
        limit=32,
    )

    assert ready is not None
    group_name, files = ready
    assert group_name == "camera"
    assert [file_state["path"] for file_state in files] == ["camera/b.mp4"]


def test_ready_eager_files_limits_audio_batches_to_worker_count(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    monkeypatch.setattr(runner, "AUDIO_ARCHIVE_MAX_PARALLEL", 2)
    for upload_id in ("upload-a", "upload-b", "upload-c"):
        (runner.TUSD_DIR / upload_id).write_bytes(b"x")
    job = {"job_id": "job-1"}
    upload = {
        "upload_id": "upload-1",
        "files": [
            {"path": "voice/a.mp3", "bytes": 1, "upload_id": "upload-a"},
            {"path": "voice/b.mp3", "bytes": 1, "upload_id": "upload-b"},
            {"path": "voice/c.mp3", "bytes": 1, "upload_id": "upload-c"},
        ],
    }
    groups = {"voice": {"archive_mode": "audio", "tasks": ["archive_audio"]}}

    ready = runner.ready_eager_files(
        job,
        upload,
        groups,
        {"voice"},
        tmp_path / "archive",
    )

    assert ready is not None
    group_name, files = ready
    assert group_name == "voice"
    assert [file_state["path"] for file_state in files] == [
        "voice/a.mp3",
        "voice/b.mp3",
    ]


def test_claim_running_eager_batch_files_marks_legacy_running_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    job = {
        "job_id": "job-1",
        "eager_archive": {
            "files": {},
            "batches": {
                "batch-1": {
                    "batch_id": "batch-1",
                    "state": "running",
                    "group": "camera",
                    "paths": ["camera/a.mp4"],
                    "started_at": "2026-06-05T00:00:00Z",
                }
            },
            "next_batch_number": 2,
        },
    }
    upload = {
        "upload_id": "upload-1",
        "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
    }

    changed = runner.claim_running_eager_batch_files(
        job,
        upload,
        {"camera": {}},
        tmp_path / "archive",
    )

    assert changed is True
    assert job["eager_archive"]["files"]["camera/a.mp4"]["state"] == "encoding"
    assert job["eager_archive"]["files"]["camera/a.mp4"]["batch_id"] == "batch-1"


def test_mark_existing_eager_outputs_does_not_complete_claimed_files(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    archive_dir = tmp_path / "archive"
    output = archive_dir / "camera" / "a.mkv"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"partial")
    job = {
        "job_id": "job-1",
        "eager_archive": {
            "files": {
                "camera/a.mp4": {
                    "state": "encoding",
                    "batch_id": "batch-1",
                }
            },
            "batches": {},
            "next_batch_number": 2,
        },
    }
    upload = {
        "upload_id": "upload-1",
        "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
    }

    updated_upload, changed = runner.mark_existing_eager_outputs(
        job,
        upload,
        {"camera": {}},
        {"camera"},
        archive_dir,
    )

    assert updated_upload is upload
    assert changed is False
    assert job["eager_archive"]["files"]["camera/a.mp4"]["state"] == "encoding"
    assert job["eager_archive"]["files"]["camera/a.mp4"]["batch_id"] == "batch-1"


def test_job_response_includes_eager_encode_progress(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    archive_dir = tmp_path / "archive"
    encoded_output = archive_dir / "camera" / "a.webm"
    active_output = archive_dir / "camera" / "b.webm"
    encoded_output.parent.mkdir(parents=True)
    encoded_output.write_bytes(b"encoded")
    active_output.write_bytes(b"active")
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "files": [
                {
                    "path": "camera/a.mp4",
                    "bytes": 1024,
                    "upload_id": "upload-a",
                    "consumed_at": "2026-06-05T00:00:00Z",
                },
                {
                    "path": "camera/b.mp4",
                    "bytes": 2048,
                    "upload_id": "upload-b",
                },
            ],
        }
    )
    job = {
        "job_id": "job-1",
        "input_upload_id": "upload-1",
        "groups": {
            "camera": {
                "archive_mode": "av1_nvenc",
                "tasks": ["archive_video"],
            }
        },
        "eager_archive": {
            "files": {
                "camera/a.mp4": {
                    "state": "encoded",
                    "group": "camera",
                    "input_bytes": 1024,
                    "output": str(encoded_output),
                    "output_bytes": 7,
                    "started_at": "2026-06-05T00:00:00Z",
                    "encoded_at": "2026-06-05T00:00:10Z",
                },
                "camera/b.mp4": {
                    "state": "encoding",
                    "group": "camera",
                    "input_bytes": 2048,
                    "output": str(active_output),
                    "started_at": "2026-06-05T00:00:05Z",
                },
            },
            "batches": {
                "batch-1": {
                    "state": "running",
                    "started_at": "2026-06-04T00:00:00Z",
                }
            },
        },
    }

    response = runner.job_response(job)
    upload_progress = response["upload_progress"]
    progress = response["encode_progress"]

    assert upload_progress["files_total"] == 2
    assert upload_progress["files_uploaded"] == 1
    assert upload_progress["bytes_total"] == 3072
    assert upload_progress["uploaded_bytes"] == 1024
    assert upload_progress["completed"] is False

    assert progress["files_total"] == 2
    assert progress["files_encoded"] == 1
    assert progress["files_encoding"] == 1
    assert progress["input_bytes_total"] == 3072
    assert progress["input_bytes_encoded"] == 1024
    assert progress["input_bytes_encoding"] == 2048
    assert progress["output_bytes"] == 7
    assert progress["active_output_bytes"] == 6
    assert progress["running_batches"] == 1
    assert progress["pipeline_batches"] == 3
    assert progress["started_at"] == "2026-06-04T00:00:00Z"


def test_job_response_includes_review_clip_progress_from_gpu_status(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "review",
        "gpu_statuses": {
            "gpu-job-1": {
                "state": "running",
                "items": {
                    "qcut_video": {
                        "status": "running",
                        "progress": {
                            "mode": "qcut_video",
                            "task": "qcut_video",
                            "phase": "encoding_clips",
                            "clips_total": 40,
                            "clips_done": 10,
                            "clips_running": 2,
                            "clips_failed": 0,
                            "percent_clips": 25.0,
                            "output_bytes": 1048576,
                            "active_output_bytes": 262144,
                            "output_rate_bytes_per_second": 131072,
                            "started_at": "2026-06-05T00:00:00Z",
                        },
                    }
                },
            }
        },
    }

    response = runner.job_response(job)
    progress = response["encode_progress"]

    assert progress["mode"] == "qcut_video"
    assert progress["phase"] == "encoding_clips"
    assert progress["clips_total"] == 40
    assert progress["clips_done"] == 10
    assert progress["clips_running"] == 2
    assert progress["files_total"] == 40
    assert progress["files_encoded"] == 10
    assert progress["files_encoding"] == 2
    assert progress["percent_clips"] == 25.0
    assert progress["percent_input_bytes"] == 25.0
    assert progress["output_bytes"] == 1048576
    assert progress["active_output_bytes"] == 262144
    assert progress["output_rate_bytes_per_second"] == 131072


def test_compact_job_response_keeps_operational_fields_only(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_input_upload(
        {
            "upload_id": "upload-1",
            "files": [{"path": "camera/a.mp4", "bytes": 1, "upload_id": "upload-a"}],
        }
    )
    job = {
        "job_id": "job-1",
        "state": "running",
        "phase": "eager_archive:pipeline=1/3",
        "input_upload_id": "upload-1",
        "collection_slug": "camera-collection-archive",
        "eager_archive": {
            "files": {},
            "batches": {"batch-1": {"state": "running"}},
            "gpu_results": {"batch-0": {"large": ["payload"]}},
        },
        "gpu_result": {"large": ["payload"]},
    }

    compact = runner.compact_job_response(job)

    assert compact["job_id"] == "job-1"
    assert compact["state"] == "running"
    assert compact["phase"] == "eager_archive:pipeline=1/3"
    assert compact["upload_progress"]["files_total"] == 1
    assert "eager_archive" not in compact
    assert "gpu_result" not in compact


def test_list_jobs_returns_recent_compact_jobs(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_job(
        {
            "job_id": "done",
            "state": "succeeded",
            "phase": "done",
            "updated_at": "2026-01-01T00:00:00Z",
            "eager_archive": {"gpu_results": {"large": ["payload"]}},
        }
    )
    runner.save_job(
        {
            "job_id": "active",
            "state": "running",
            "phase": "eager_archive:pipeline=1/3",
            "updated_at": "2026-01-02T00:00:00Z",
        }
    )

    active = runner.list_jobs()
    all_jobs = runner.list_jobs(terminal="all")

    assert [job["job_id"] for job in active["jobs"]] == ["active"]
    assert active["page"] == 1
    assert active["per_page"] == 25
    assert active["total"] == 1
    assert active["pages"] == 1
    assert [job["job_id"] for job in all_jobs["jobs"]] == ["active", "done"]
    assert all_jobs["total"] == 2
    assert "eager_archive" not in all_jobs["jobs"][1]


def test_list_jobs_pages_filters_and_searches_indexed_summaries(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_job(
        {
            "job_id": "job-1",
            "state": "running",
            "phase": "encoding",
            "collection_slug": "front-camera",
            "input_upload_id": "upload-1",
            "workflow_mode": "collection_archive",
            "collection_archive": {"destination": "riverhog"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-03T00:00:00Z",
            "riverhog": {"enabled": True},
        }
    )
    runner.save_job(
        {
            "job_id": "job-2",
            "state": "queued",
            "phase": "queued",
            "input_upload_id": "upload-2",
            "workflow_mode": "review",
            "review": {
                "device_id": "back-camera",
                "route_id": "back-camera",
                "profile_id": "webm-q42",
                "target": {"enabled": False},
            },
            "created_at": "2026-01-02T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        }
    )
    runner.save_job(
        {
            "job_id": "job-3",
            "state": "succeeded",
            "phase": "done",
            "collection_slug": "front-camera",
            "input_upload_id": "upload-3",
            "workflow_mode": "collection_archive",
            "collection_archive": {"destination": "riverhog"},
            "created_at": "2026-01-03T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "riverhog": {"enabled": True},
        }
    )

    page = runner.list_jobs(
        terminal="all",
        q="front",
        workflow_mode="collection_archive",
        collection_archive_destination="riverhog",
        sort="created_at",
        order="asc",
        page=1,
        per_page=1,
    )

    assert page["total"] == 2
    assert page["pages"] == 2
    assert page["query"] == "front"
    assert page["filters"]["workflow_mode"] == "collection_archive"
    assert page["filters"]["collection_archive_destination"] == "riverhog"
    assert [job["job_id"] for job in page["jobs"]] == ["job-1"]

    second_page = runner.list_jobs(
        terminal="all",
        q="front",
        workflow_mode="collection_archive",
        collection_archive_destination="riverhog",
        sort="created_at",
        order="asc",
        page=2,
        per_page=1,
    )

    assert [job["job_id"] for job in second_page["jobs"]] == ["job-3"]


def test_list_jobs_does_not_scan_all_job_states_for_current_page(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    runner.save_job(
        {
            "job_id": "queued",
            "state": "queued",
            "phase": "queued",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )

    def fail_job_states() -> list[dict[str, object]]:
        raise AssertionError("job list must not materialize every job state")

    monkeypatch.setattr(runner, "job_states", fail_job_states)

    page = runner.list_jobs(terminal="all")

    assert [job["job_id"] for job in page["jobs"]] == ["queued"]
    assert "queue" not in page["jobs"][0]


def test_acquire_job_gpu_reuses_persisted_lease_token(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    runner = load_runner(tmp_path, monkeypatch)
    runner.ensure_dirs()
    runner.init_state_store()
    requests: list[dict[str, object]] = []

    def fake_manager_request(
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        assert method == "POST"
        assert path == "/acquire"
        assert payload is not None
        requests.append(dict(payload))
        return {"lease_token": payload["lease_token"], "queued": False}

    monkeypatch.setattr(runner, "manager_request", fake_manager_request)
    job = {"job_id": "job-1", "gpu_lease_token": "saved-token"}
    runner.save_job(job)

    token = runner.acquire_job_gpu(job)

    assert token == "saved-token"
    assert requests[0]["lease_token"] == "saved-token"
    assert job["gpu_lease_token"] == "saved-token"
