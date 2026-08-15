from __future__ import annotations

import hashlib

import pytest
from munchy_target_support.operations import (
    SOURCE_ROLE,
    VIDEO_ARCHIVE_OPERATION,
    VIDEO_ARCHIVE_ROLE,
    operation_contract,
)
from munchy_target_support.protocol import (
    Artifact,
    ExecutionToolEvidence,
    JsonSchemaDocument,
    TargetContract,
    TargetContractPayload,
    TargetExecutionEvidence,
    TargetFailure,
    TargetJobRequest,
    TargetJobRequestPayload,
    TargetJobStatus,
    TargetOperationSupport,
    TargetPreflightRequest,
    TargetPreflightResponse,
    TargetProgress,
    TransformPlan,
    TransformPlanPayload,
    canonical_json_bytes,
    validate_artifacts_against_operation,
    validate_preflight_response_against_request,
    validate_status_against_request,
)
from munchy_target_support.workspace import (
    publish_file_atomically,
    verify_artifact,
    workspace_artifact_path,
)

SHA = "a" * 64


def test_rfc8785_canonicalization_is_the_hash_authority() -> None:
    assert canonical_json_bytes({"string": "€$", "numbers": [333333333.33333329, 1e30]}) == (
        b'{"numbers":[333333333.3333333,1e+30],"string":"\xe2\x82\xac$"}'
    )


def test_preflight_plan_and_job_bind_complete_transform() -> None:
    operation = operation_contract(VIDEO_ARCHIVE_OPERATION)
    options = JsonSchemaDocument.from_schema(
        "example.nvenc.options/v1",
        {"type": "object", "additionalProperties": False},
    )
    target = TargetContract.seal(
        TargetContractPayload(
            implementation_id="example.nvenc/v1",
            implementation_version="1.0.0",
            source_revision="abc123",
            operations=(
                TargetOperationSupport(
                    operation_id=operation.id,
                    operation_contract_sha256=operation.contract_sha256,
                    options_schema=options,
                ),
            ),
        )
    )
    source = Artifact(
        id="camera-source",
        role=SOURCE_ROLE,
        path="camera/clip.mp4",
        bytes=123,
        sha256=SHA,
        media_type="video/mp4",
    )
    declaration = TargetPreflightRequest(
        operation_id=operation.id,
        operation_contract_sha256=operation.contract_sha256,
        workspace_id="job-1",
        inputs=(source,),
        intent={"archive": {"codec": "av1", "container": "mkv", "audio": {"codec": "opus"}}},
        target_options={},
    )
    plan = TransformPlan.seal(
        TransformPlanPayload(
            **declaration.model_dump(exclude={"protocol"}),
            target_implementation_id=target.implementation_id,
            target_contract_sha256=target.contract_sha256,
            effective_intent=declaration.intent,
            effective_target_options={},
        )
    )
    response = TargetPreflightResponse(target=target, plan=plan)
    validate_preflight_response_against_request(response, declaration)
    request = TargetJobRequest.seal(TargetJobRequestPayload(job_id="job-1", plan=plan))

    assert response.plan.inputs == (source,)
    assert request.plan.plan_sha256 == plan.plan_sha256
    assert request.request_sha256 != plan.plan_sha256

    status = TargetJobStatus(
        job_id=request.job_id,
        attempt=request.attempt,
        request_sha256=request.request_sha256,
        plan_sha256=plan.plan_sha256,
        state="succeeded",
        progress=TargetProgress(phase="succeeded", completed=1, total=1),
        outputs=(
            Artifact(
                id="camera-archive",
                role=VIDEO_ARCHIVE_ROLE,
                path="camera/clip.mkv",
                bytes=456,
                sha256="b" * 64,
                derived_from=(source.id,),
            ),
        ),
        execution_evidence=TargetExecutionEvidence(
            target=target,
            operation=operation,
            effective_intent=plan.effective_intent,
            effective_target_options=plan.effective_target_options,
        ),
        finished_at="2026-08-14T00:00:00Z",
    )
    validate_status_against_request(status, request, operation)
    with pytest.raises(ValueError, match="request digest"):
        validate_status_against_request(
            status.model_copy(update={"request_sha256": "c" * 64}),
            request,
            operation,
        )

    changed_plan = TransformPlan.seal(
        TransformPlanPayload(
            **plan.model_dump(
                exclude={
                    "protocol",
                    "plan_sha256",
                    "intent",
                    "effective_intent",
                }
            ),
            intent={"archive": {"codec": "different"}},
            effective_intent={"archive": {"codec": "different"}},
        )
    )
    with pytest.raises(ValueError, match="changed declared intent"):
        validate_preflight_response_against_request(
            TargetPreflightResponse(target=target, plan=changed_plan),
            declaration,
        )


def test_operation_contract_requires_explicit_output_derivation() -> None:
    operation = operation_contract(VIDEO_ARCHIVE_OPERATION)
    source = Artifact(
        id="source",
        role=SOURCE_ROLE,
        path="clip.mp4",
        bytes=1,
        sha256=SHA,
    )
    output = Artifact(
        id="archive",
        role=VIDEO_ARCHIVE_ROLE,
        path="clip.mkv",
        bytes=2,
        sha256="b" * 64,
        derived_from=("source",),
    )

    validate_artifacts_against_operation(operation, inputs=(source,), outputs=(output,))


def test_target_status_uses_common_progress_failure_and_exact_terminal_evidence() -> None:
    operation = operation_contract(VIDEO_ARCHIVE_OPERATION)
    options = JsonSchemaDocument.from_schema(
        "example.target.options/v1",
        {"type": "object", "additionalProperties": False},
    )
    target = TargetContract.seal(
        TargetContractPayload(
            implementation_id="example.target/v1",
            implementation_version="1.0.0",
            source_revision="abc123",
            operations=(
                TargetOperationSupport(
                    operation_id=operation.id,
                    operation_contract_sha256=operation.contract_sha256,
                    options_schema=options,
                ),
            ),
        )
    )
    evidence = TargetExecutionEvidence(
        target=target,
        operation=operation,
        effective_intent={"archive": {"codec": "av1"}},
        effective_target_options={},
        tools=(ExecutionToolEvidence(name="fixture", version="1.0.0"),),
    )
    failed = TargetJobStatus(
        job_id="job-1",
        attempt=1,
        request_sha256="b" * 64,
        plan_sha256="c" * 64,
        state="failed",
        progress=TargetProgress(phase="encode", completed=0, total=1),
        execution_evidence=evidence,
        failure=TargetFailure(
            code="target_execution_failed",
            message="fixture failure",
            retryable=False,
        ),
        finished_at="2026-08-14T00:00:00Z",
    )

    assert failed.execution_evidence == evidence
    assert failed.failure is not None and failed.failure.retryable is False
    with pytest.raises(ValueError, match="Extra inputs"):
        TargetProgress.model_validate({"phase": "encode", "completed": 0, "total": 1, "percent": 0})
    with pytest.raises(ValueError, match="structured failure"):
        TargetJobStatus.model_validate(failed.model_dump(exclude={"failure"}, exclude_none=True))


def test_shared_directory_rejects_traversal_and_symlink_escape(tmp_path) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "input" / "job-1"
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="parent segments"):
        workspace_artifact_path(tmp_path, "input", "job-1", "../outside/file")
    with pytest.raises(ValueError, match="symlink"):
        workspace_artifact_path(tmp_path, "input", "job-1", "escape/file")


def test_shared_directory_verifies_input_and_publishes_output_atomically(tmp_path) -> None:  # type: ignore[no-untyped-def]
    source = workspace_artifact_path(tmp_path, "input", "job-1", "camera/clip.mp4")
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    artifact = Artifact(
        id="source",
        role=SOURCE_ROLE,
        path="camera/clip.mp4",
        bytes=6,
        sha256=hashlib.sha256(b"source").hexdigest(),
    )

    assert verify_artifact(tmp_path, "input", "job-1", artifact) == source

    staged = tmp_path / "staged"
    staged.write_bytes(b"complete")
    destination = workspace_artifact_path(tmp_path, "output", "job-1", "camera/clip.mkv")
    publish_file_atomically(staged, destination)

    assert destination.read_bytes() == b"complete"
    assert not list(destination.parent.glob("*.part"))
