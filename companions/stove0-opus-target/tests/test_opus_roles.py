from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from stove0_media_contracts import AUDIO_ARCHIVE_OPERATION
from stove0_opus_target import OpusReviewSampler, OpusTargetService
from stove0_opus_target import app as opus_app
from stove0_opus_target.app import create_sampler_app, create_target_app
from stove0_sampler_protocol import (
    SamplerInput,
    SamplerRequest,
    SamplerRequestPayload,
    SamplerWindow,
)


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

    monkeypatch.setattr("stove0_opus_target.sampler.run_ffmpeg", encode)
    with (
        TestClient(create_target_app(token="target-secret", target=target)) as target_client,
        TestClient(create_sampler_app(token="sampler-secret", sampler=sampler)) as sampler_client,
    ):
        target_response = target_client.get(
            "/v1/target",
            headers={"Authorization": "Bearer target-secret"},
        )
        sampler_response = sampler_client.get(
            "/v1/sampler",
            headers={"Authorization": "Bearer sampler-secret"},
        )
        sampled = sampler_client.post(
            "/v1/sample",
            headers={"Authorization": "Bearer sampler-secret"},
            content=request.model_dump_json().encode(),
        )

        assert target_response.status_code == 200
        assert target_response.json()["image_digest"] == image_digest
        assert [item["operation_id"] for item in target_response.json()["operations"]] == [
            AUDIO_ARCHIVE_OPERATION.id
        ]
        assert sampler_response.status_code == 200
        assert sampler_response.json()["image_digest"] == image_digest
        assert sampled.status_code == 200
        assert sampled.json()["state"] == "succeeded"
        assert (
            target_client.get(
                "/v1/sampler",
                headers={"Authorization": "Bearer target-secret"},
            ).status_code
            == 404
        )
        assert (
            sampler_client.get(
                "/v1/target",
                headers={"Authorization": "Bearer sampler-secret"},
            ).status_code
            == 404
        )


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
