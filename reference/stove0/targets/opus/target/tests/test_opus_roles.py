from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from riverhog_protocol import canonical_json_sha256
from stove0_media_archive_target_contracts import (
    AUDIO_ARCHIVE_OPERATION,
    METADATA_XMP_ROLE,
    SOURCE_ARTIFACT_ROLE,
)
from stove0_media_archive_target_support import (
    MediaArchiveProjection,
)
from stove0_opus_review_sampler import OpusReviewSampler
from stove0_opus_review_sampler.app import create_app as create_sampler_app
from stove0_opus_target import OpusTargetService
from stove0_opus_target import app as opus_app
from stove0_opus_target import target as opus_target
from stove0_opus_target.app import create_target_app
from stove0_review_sampler_protocol import (
    SamplerInput,
    SamplerRequest,
    SamplerRequestPayload,
    SamplerWindow,
)
from stove0_review_sampler_support import SamplerHttpBinding
from stove0_target_support import (
    OutputArtifact,
    TargetHttpBinding,
    validate_preflight_response_against_request,
)

from tests.fixtures.stove0_media import media_preflight_request


def _sha(character: str) -> str:
    return character * 64


def test_paired_opus_roles_share_image_identity_but_not_runtime_contracts(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    image_digest = _sha("9")
    workspace_root = tmp_path / "review-workspace"
    workspace_root.mkdir(mode=0o700)
    sampler = OpusReviewSampler(
        workspace_root=workspace_root,
        source_revision="fixture",
        image_digest=image_digest,
    )
    target = OpusTargetService(
        state_root=tmp_path / "target-state",
        workspace_root=tmp_path / "target-workspace",
        source_revision="fixture",
        image_digest=image_digest,
    )
    request = _request(workspace_root, sampler)

    def encode(command: list[str], **_kwargs: object) -> None:
        Path(command[-1]).write_bytes(b"representative-opus")

    monkeypatch.setattr("stove0_opus_review_sampler.sampler.run_ffmpeg", encode)
    target_response = TargetHttpBinding(target).handle("GET", "/v1/target", b"")
    sampler_binding = SamplerHttpBinding(sampler)
    sampler_response = sampler_binding.handle("GET", "/v1/sampler", b"")
    sampled = sampler_binding.handle("POST", "/v1/sample", request.model_dump_json().encode())

    target_payload = json.loads(target_response.body)
    sampler_payload = json.loads(sampler_response.body)
    assert target_response.status == 200
    assert target_payload["image_digest"] == image_digest
    assert [item["operation_id"] for item in target_payload["operations"]] == [
        AUDIO_ARCHIVE_OPERATION.id
    ]
    assert sampler_response.status == 200
    assert sampler_payload["image_digest"] == image_digest
    assert sampled.status == 200
    assert json.loads(sampled.body)["state"] == "succeeded"
    assert {
        route.path for route in create_target_app(token="target-secret", target=target).routes
    } >= {
        "/v1/target",
        "/v1/preflight",
        "/v1/jobs/{job_id}",
        "/v1/jobs/{job_id}/cancel",
    }
    assert {
        route.path for route in create_sampler_app(token="sampler-secret", sampler=sampler).routes
    } >= {
        "/v1/sampler",
        "/v1/sample",
    }
    target.close()


def test_opus_target_uses_configured_ffmpeg(monkeypatch: Any) -> None:
    configured: dict[str, object] = {}
    monkeypatch.setenv("STOVE0_FFMPEG_BIN", "fixture-ffmpeg")
    monkeypatch.setattr(opus_app, "_image_digest", lambda _prefix: _sha("9"))
    monkeypatch.setattr(opus_app, "_secret", lambda _prefix: "fixture-secret")

    class ConfiguredTarget:
        def __init__(self, **kwargs: object) -> None:
            configured.update(kwargs)
            self.ffmpeg = str(kwargs["ffmpeg"])

        def close(self) -> None:
            pass

    monkeypatch.setattr(opus_app, "OpusTargetService", ConfiguredTarget)
    monkeypatch.setattr(opus_app.uvicorn, "run", lambda *_args, **_kwargs: None)

    assert opus_app.target_main([]) == 0
    assert configured["ffmpeg"] == "fixture-ffmpeg"


def test_opus_preflight_fixes_exact_unbounded_metadata_projection(tmp_path: Path) -> None:
    target = OpusTargetService(
        state_root=tmp_path / "state",
        workspace_root=tmp_path / "workspace",
        source_revision="fixture",
        image_digest=_sha("9"),
    )
    intent = {
        "codec": "opus",
        "container": "opus",
        "bitrate_kbps": 128,
        "metadata_projection": {
            "device_make": "Example Camera Corp",
            "device_model": "Example Camera One",
            "gps": {"latitude": 45.5, "longitude": -122.6},
            "creators": ["Alex Example", "River Example"],
            "tags": ["archive/example"],
            "field_preferences": [
                {
                    "name": "capture-time",
                    "fields": ["XMP-xmp:CreateDate", "EXIF:DateTimeOriginal"],
                }
            ],
        },
    }
    request = media_preflight_request(AUDIO_ARCHIVE_OPERATION, intent)

    response = target.preflight(request)
    validate_preflight_response_against_request(response, request)
    projection = MediaArchiveProjection.model_validate(
        response.plan.target_options["media_projection"]
    )

    assert response.plan.observation_result_sha256s == (
        request.observations[0].result.result_sha256,
    )
    assert projection.items[0].archive_path == "audio/primary/archive.opus"
    assert projection.items[0].xmp_path == "audio/primary/archive.opus.xmp"
    assert projection.items[0].derived_from == ("primary", "sidecar")
    assert projection.retained_xmp_sidecars[0].output_path == (
        "audio/~source-artifacts/sidecar.xmp"
    )
    assert {output.role for output in AUDIO_ARCHIVE_OPERATION.outputs} == {
        "stove0.media.audio-archive/v1",
        METADATA_XMP_ROLE,
        SOURCE_ARTIFACT_ROLE,
    }
    assert next(
        item for item in AUDIO_ARCHIVE_OPERATION.inputs if item.role == "stove0.media.xmp-source/v1"
    ).allowed_dispositions == ("transformed",)
    changed = target.preflight(
        media_preflight_request(
            AUDIO_ARCHIVE_OPERATION,
            intent,
            sidecar_capture_time="2025:02:03 04:05:07-0800",
        )
    )
    assert changed.plan.plan_sha256 != response.plan.plan_sha256
    target.close()


def test_opus_execution_identity_is_the_canonical_semantic_result() -> None:
    output = OutputArtifact(
        id="opus-source",
        role="stove0.media.audio-archive/v1",
        path="audio/source.opus",
        bytes=12,
        sha256=_sha("3"),
        media_type="audio/ogg",
        derived_from=("source",),
    )
    expected = canonical_json_sha256(
        {
            "format": "stove0-opus-target-execution/v1",
            "plan_sha256": _sha("1"),
            "image_digest": _sha("2"),
            "outputs": [output.model_dump(mode="json")],
        }
    )

    assert opus_target._execution_sha256(_sha("1"), _sha("2"), (output,)) == expected


def _request(workspace_root: Path, sampler: OpusReviewSampler) -> SamplerRequest:
    payload = b"fixture-wave"
    workspace_id = _sha("7")
    source = workspace_root / workspace_id / "input/source.wav"
    source.parent.mkdir(mode=0o700, parents=True)
    source.write_bytes(payload)
    source.chmod(0o400)
    (source.parents[1] / "output").mkdir()
    (source.parents[1] / "control").mkdir()
    return SamplerRequest.seal(
        SamplerRequestPayload(
            sampler_descriptor_sha256=sampler.descriptor().descriptor_sha256,
            workspace_id=workspace_id,
            inputs=(
                SamplerInput(
                    id="source",
                    path="input/source.wav",
                    bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                    media_type="audio/wav",
                ),
            ),
            windows=(
                SamplerWindow(
                    id="sample-0001",
                    input_id="source",
                    start_ms=0,
                    duration_ms=1000,
                    output_path="output/review/sample.opus",
                ),
            ),
            portable_intent={"codec": "opus", "container": "opus", "bitrate_kbps": 96},
            maximum_output_bytes=1024,
            timeout_seconds=30,
            cancellation_path="control/cancel",
        )
    )
