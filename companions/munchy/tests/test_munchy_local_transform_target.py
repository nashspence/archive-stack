from __future__ import annotations

import hashlib
import time
from pathlib import Path

from munchy_core.adapters.transform_targets import (
    HttpTransformTargetPlatform,
    InProcessTargetRegistration,
)
from munchy_core.services import media
from munchy_target_support.operations import (
    AUDIO_ARCHIVE_OPERATION,
    AUDIO_ARCHIVE_ROLE,
    SOURCE_ROLE,
    AudioArchiveIntent,
    operation_contract,
)
from munchy_target_support.protocol import (
    Artifact,
    TargetCancelRequest,
    TargetJobRequest,
    TargetJobRequestPayload,
    TargetPreflightRequest,
)
from munchy_target_support.workspace import workspace_artifact_path


def _request(root: Path, target: media.LocalAudioTransformTarget) -> TargetJobRequest:
    content = b"audio source\n"
    source = workspace_artifact_path(root, "input", "audio-job", "voice/source.wav")
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    operation = operation_contract(AUDIO_ARCHIVE_OPERATION)
    accepted = target.preflight(
        TargetPreflightRequest(
            operation_id=operation.id,
            operation_contract_sha256=operation.contract_sha256,
            workspace_id="audio-job",
            inputs=(
                Artifact(
                    id="source",
                    role=SOURCE_ROLE,
                    path="voice/source.wav",
                    bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    media_type="audio/wav",
                ),
            ),
            intent=AudioArchiveIntent(archive={"codec": "opus", "container": "opus"}).model_dump(
                mode="json", exclude_none=True
            ),
            target_options={},
        )
    )
    return TargetJobRequest.seal(TargetJobRequestPayload(job_id="audio-job", plan=accepted.plan))


def _terminal(target: media.LocalAudioTransformTarget, job_id: str):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = target.status(job_id)
        if status.state in {"succeeded", "failed", "canceled"}:
            return status
        time.sleep(0.01)
    raise RuntimeError("local audio target did not become terminal")


def test_local_audio_uses_same_async_target_contract_without_external_registration(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "transform-runtime"
    monkeypatch.setenv("MUNCHY_SOURCE_REVISION", "local-audio-revision-test")
    target = media.LocalAudioTransformTarget(root)

    def encode(*, output_root: Path, **_kwargs):  # type: ignore[no-untyped-def]
        output = output_root / "voice" / "source.opus"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"encoded opus\n")
        return {"status": "succeeded"}

    monkeypatch.setattr(media, "run_archive_audio_group", encode)
    request = _request(root, target)
    contract = target.contract()
    assert contract.source_revision == "local-audio-revision-test"
    platform = HttpTransformTargetPlatform(
        registry={},
        in_process={
            "munchy-audio": InProcessTargetRegistration(
                registration_id="munchy-audio",
                target=target,
                workspace_root=root,
                expected_target_contract_sha256=contract.contract_sha256,
            )
        },
    )

    first = platform.put_job("munchy-audio", request)
    repeated = platform.put_job("munchy-audio", request)
    assert first.request_sha256 == repeated.request_sha256 == request.request_sha256
    succeeded = _terminal(target, request.job_id)

    assert succeeded.state == "succeeded"
    assert len(succeeded.outputs) == 1
    assert succeeded.outputs[0].role == AUDIO_ARCHIVE_ROLE
    assert succeeded.outputs[0].derived_from == ("source",)
    assert succeeded.execution_evidence is not None
    assert succeeded.execution_evidence.target == contract
    assert platform.registration_ids() == ("munchy-audio",)


def test_local_audio_cancellation_keeps_input_until_terminal_quiescence(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "transform-runtime"
    target = media.LocalAudioTransformTarget(root)

    def encode(*, output_root: Path, **_kwargs):  # type: ignore[no-untyped-def]
        time.sleep(0.2)
        output = output_root / "voice" / "source.opus"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"encoded opus\n")
        return {"status": "succeeded"}

    monkeypatch.setattr(media, "run_archive_audio_group", encode)
    request = _request(root, target)
    target.put_job(request)
    canceling = target.cancel(
        request.job_id,
        TargetCancelRequest(reason="local cancellation proof"),
    )
    assert canceling.state in {"canceling", "canceled"}
    assert workspace_artifact_path(root, "input", request.job_id, "voice/source.wav").is_file()

    canceled = _terminal(target, request.job_id)
    assert canceled.state == "canceled"
    assert canceled.failure is not None
    assert canceled.failure.code == "job_canceled"
    assert workspace_artifact_path(root, "input", request.job_id, "voice/source.wav").is_file()
