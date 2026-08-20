from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from stove0_media_contracts import AV1_OPUS_ARCHIVE_OPERATION
from stove0_nvenc_av1_opus_target import (
    NvencAv1OpusReviewSampler,
    NvencAv1OpusTargetService,
    source_artifacts,
)
from stove0_nvenc_av1_opus_target.app import create_sampler_app, create_target_app
from stove0_nvenc_av1_opus_target.common import NvencContentError, run_ffmpeg
from stove0_sampler_protocol import (
    SamplerInput,
    SamplerRequest,
    SamplerRequestPayload,
    SamplerWindow,
)


def _sha(character: str) -> str:
    return character * 64


def test_source_artifact_zstd_command_is_configurable(monkeypatch: Any) -> None:
    monkeypatch.setenv("STOVE0_NVENC_AV1_OPUS_TARGET_ZSTD", "fixture-zstd")
    monkeypatch.setattr(source_artifacts.shutil, "which", lambda command: f"/tools/{command}")

    assert source_artifacts._zstd_command() == "/tools/fixture-zstd"


def test_paired_nvenc_roles_bind_av1_opus_semantics_and_isolated_contracts(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    image_digest = _sha("9")
    workspace_root = tmp_path / "review-workspace"
    workspace_root.mkdir(mode=0o700)
    sampler = NvencAv1OpusReviewSampler(
        workspace_root=workspace_root,
        source_revision="fixture",
        image_digest=image_digest,
    )
    target = NvencAv1OpusTargetService(
        state_root=tmp_path / "target-state",
        workspace_root=tmp_path / "target-workspace",
        source_revision="fixture",
        image_digest=image_digest,
    )
    request = _request(workspace_root, sampler)

    def encode(command: list[str], **_kwargs: object) -> None:
        assert "av1_nvenc" in command
        assert "libopus" in command
        Path(command[-1]).write_bytes(b"representative-av1-opus")

    monkeypatch.setattr("stove0_nvenc_av1_opus_target.sampler.run_ffmpeg", encode)
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
            AV1_OPUS_ARCHIVE_OPERATION.id
        ]
        assert sampler_response.status_code == 200
        assert sampler_response.json()["primary_operation_id"] == (AV1_OPUS_ARCHIVE_OPERATION.id)
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


def _request(
    workspace_root: Path,
    sampler: NvencAv1OpusReviewSampler,
) -> SamplerRequest:
    payload = b"fixture-video"
    workspace_id = _sha("7")
    source = workspace_root / workspace_id / "input/source.mkv"
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
                    path="input/source.mkv",
                    bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                    media_type="video/x-matroska",
                ),
            ),
            windows=(
                SamplerWindow(
                    id="sample-0001",
                    input_id="source",
                    start_ms=0,
                    duration_ms=1000,
                    output_path="output/review/sample.mkv",
                ),
            ),
            portable_intent={
                "codec": "av1",
                "container": "mkv",
                "quality": 23,
                "audio_bitrate_kbps": 96,
                "salvage": "safe-remux",
            },
            maximum_output_bytes=1024,
            timeout_seconds=30,
            cancellation_path="control/cancel",
        )
    )


def test_source_artifact_bundle_is_canonical_and_audited(tmp_path: Path) -> None:
    work = tmp_path / "work"
    archive = tmp_path / "clip.mkv"
    archive.write_bytes(b"archive")
    artifacts = source_artifacts._assemble_source_artifact_bundle_inputs(
        work_dir=work,
        src="clip.mp4",
        output=archive.name,
        source_metadata={"format": {"format_name": "mov"}, "streams": []},
        source_container={"supported": True, "mode": "iso_bmff_rebuild"},
        container_inventory=[],
        container_artifacts=[],
        exports=[],
        stream_transforms=[],
        dropped_items=[],
        encode_cmd=["ffmpeg", "-i", "{source}", "{archive}"],
        selected_output_path=archive,
        encode_output_path=archive,
    )
    first = tmp_path / "first.source-artifacts.tar"
    second = tmp_path / "second.source-artifacts.tar"

    assert source_artifacts._build_source_artifacts_bundle(
        first,
        artifacts,
        src="clip.mp4",
        output=archive.name,
    )
    assert source_artifacts._build_source_artifacts_bundle(
        second,
        artifacts,
        src="clip.mp4",
        output=archive.name,
    )
    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, "r") as bundle:
        member = bundle.extractfile("manifest.json")
        assert member is not None
        manifest = json.loads(member.read())
        assert manifest["kind"] == "stove0.media.source-artifacts/v1"
        assert all(item.mtime == 0 for item in bundle.getmembers())
    audit = source_artifacts._audit_source_artifacts_bundle(first)
    assert audit["ok"] is True
    assert audit["rebuild_supported"] is True
    assert audit["artifacts_checked"] == len(artifacts)


def test_failed_nvenc_command_removes_bounded_diagnostic_log(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class FailedProcess:
        returncode = 7

        def poll(self) -> int:
            return self.returncode

    def popen(_command: list[str], **kwargs: Any) -> FailedProcess:
        kwargs["stderr"].write(b"fixture tool failure")
        kwargs["stderr"].flush()
        return FailedProcess()

    monkeypatch.setattr("stove0_nvenc_av1_opus_target.common.subprocess.Popen", popen)
    with pytest.raises(NvencContentError, match="fixture tool failure"):
        run_ffmpeg(
            ["fixture-tool"],
            log_root=tmp_path,
            timeout_seconds=1,
            canceled=lambda: False,
        )
    assert tuple(tmp_path.glob(".ffmpeg-*.log")) == ()
