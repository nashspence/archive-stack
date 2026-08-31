from __future__ import annotations

import hashlib
import threading
from contextlib import AbstractContextManager
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from riverhog_protocol import canonical_json_sha256
from riverhog_transform_sdk import TransformWorkspace
from stove0_protocol import (
    ArtifactSelection,
    ArtifactSubject,
    CollectionRootRef,
    ControllerEvidence,
    ControllerEvidencePayload,
    ExecutionEnvelope,
    ExecutionEnvelopePayload,
    JsonSchemaDocument,
    OperationRef,
    RecipeRef,
    TargetPlanBinding,
    WorkflowPlan,
    WorkflowPlanPayload,
    WorkIdentity,
    WorkPayload,
)
from stove0_review_materialize_target import ReviewMaterializeTargetService
from stove0_review_materialize_target import app as materialize_app
from stove0_review_materialize_target.app import create_app
from stove0_review_rclone_effect_target import (
    RcloneReviewDestination,
    ReviewRcloneEffectTargetService,
)
from stove0_review_rclone_effect_target import app as effect_app
from stove0_review_rclone_effect_target import target as effect_target
from stove0_review_sampler_client import ReviewSamplerClient
from stove0_review_sampler_protocol import (
    SamplerDescriptor,
    SamplerDescriptorPayload,
    SamplerFailure,
    SamplerInapplicable,
    SamplerOutput,
    SamplerResult,
    SamplerResultPayload,
)
from stove0_review_target_contracts import (
    REVIEW_MATERIALIZE_OPERATION,
    REVIEW_RCLONE_DELIVER_OPERATION,
    REVIEW_SOURCE_ROLE,
    ReviewSamplePlan,
    ReviewSamplePlanPayload,
    ReviewSampleWindow,
)
from stove0_review_target_support import ReviewTargetConfig, SamplerConfig, SamplerRegistration
from stove0_review_target_support import target as review_support
from stove0_target_protocol import TargetCallbackAccess, TargetInputAuthority
from stove0_target_support import (
    InputArtifact,
    OutputArtifact,
    TargetExecutionCanceled,
    TargetExecutionFailure,
    TargetExecutionInapplicable,
    TargetExecutionRuntime,
    TargetJobDeclaration,
    TargetJobRequest,
    TargetJobStatus,
    TargetPreflightRequest,
    TargetRuntimeAuthority,
    TargetServiceError,
)


def _sha(character: str) -> str:
    return character * 64


def _input_authority(*inputs: InputArtifact) -> TargetInputAuthority:
    return TargetInputAuthority.from_selection(
        ArtifactSelection.seal(
            tuple(ArtifactSubject.model_validate(item.model_dump(mode="json")) for item in inputs)
        )
    )


def _sample_plan() -> ReviewSamplePlan:
    return ReviewSamplePlan.seal(
        ReviewSamplePlanPayload(
            samples_per_artifact=1,
            window_duration_ms=1000,
            windows=(
                ReviewSampleWindow(
                    artifact_id="source",
                    start_ms=0,
                    duration_ms=1000,
                ),
            ),
        )
    )


class FixtureSamplerClient:
    def __init__(self, descriptor: SamplerDescriptor) -> None:
        self.value = descriptor
        self.closed = False

    def descriptor(self, *, refresh: bool = False) -> SamplerDescriptor:
        del refresh
        return self.value

    def close(self) -> None:
        self.closed = True


def _sampler() -> tuple[SamplerRegistration, FixtureSamplerClient]:
    descriptor = SamplerDescriptor.seal(
        SamplerDescriptorPayload(
            implementation_id="fixture.opus-review-sampler/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            image_digest=_sha("8"),
            primary_operation_id="stove0.media.audio-archive/v1",
            primary_operation_contract_sha256=_sha("7"),
            portable_intent_schema=JsonSchemaDocument.from_schema(
                "fixture.opus-intent/v1",
                {
                    "type": "object",
                    "properties": {"bitrate_kbps": {"type": "integer"}},
                    "required": ["bitrate_kbps"],
                    "additionalProperties": False,
                },
            ),
            output_role="stove0.review.audio/v1",
        )
    )
    client = FixtureSamplerClient(descriptor)
    return (
        SamplerRegistration(
            id="opus",
            client=cast(ReviewSamplerClient, client),
            descriptor_sha256=descriptor.descriptor_sha256,
            image_digest=descriptor.image_digest,
        ),
        client,
    )


def test_review_preflight_seals_exact_sampler_identity_and_one_operation(
    tmp_path: Path,
) -> None:
    registration, sampler_client = _sampler()
    target = ReviewMaterializeTargetService(
        state_root=tmp_path / "state",
        workspace_root=tmp_path / "workspace",
        samplers=(registration,),
        source_revision="fixture",
        image_digest=_sha("9"),
        implementation_version="0.1.0",
    )
    try:
        request = TargetPreflightRequest(
            operation_id=REVIEW_MATERIALIZE_OPERATION.id,
            operation_contract_sha256=REVIEW_MATERIALIZE_OPERATION.contract_sha256,
            inputs=_input_authority(
                InputArtifact(
                    id="source",
                    role=REVIEW_SOURCE_ROLE,
                    collection=CollectionRootRef(
                        collection_id=1,
                        archive_root_sha256=_sha("1"),
                        content_identity=_sha("2"),
                    ),
                    path="camera/source.wav",
                    bytes=12,
                    sha256=_sha("3"),
                    media_type="audio/wav",
                )
            ),
            intent={
                "sample_plan": _sample_plan().model_dump(mode="json"),
                "variant": {
                    "id": "opus-96",
                    "portable_intent": {"bitrate_kbps": 96},
                },
            },
            target_options={"sampler_registration_id": "opus"},
        )
        preflight = target.preflight(request)

        assert target.contract().image_digest == _sha("9")
        assert [item.operation_id for item in target.contract().operations] == [
            REVIEW_MATERIALIZE_OPERATION.id
        ]
        assert preflight.plan.target_options == {
            "sampler_registration_id": "opus",
            "sampler_descriptor_sha256": registration.descriptor_sha256,
            "sampler_image_digest": registration.image_digest,
        }
        invalid_intent = request.intent.copy()
        invalid_plan = dict(invalid_intent["sample_plan"])
        invalid_plan["sample_plan_sha256"] = _sha("4")
        invalid_intent["sample_plan"] = invalid_plan
        with pytest.raises(TargetServiceError, match="intent is invalid"):
            target.preflight(request.model_copy(update={"intent": invalid_intent}))
        with pytest.raises(TargetServiceError, match="sampler_image_digest") as exc_info:
            target.preflight(
                request.model_copy(
                    update={
                        "target_options": {
                            **request.target_options,
                            "sampler_image_digest": _sha("6"),
                        }
                    }
                )
            )
        assert exc_info.value.status == 400
    finally:
        target.close()
    assert sampler_client.closed


def test_review_process_exposes_only_target_contract(tmp_path: Path) -> None:
    registration, _sampler_client = _sampler()
    target = ReviewMaterializeTargetService(
        state_root=tmp_path / "state",
        workspace_root=tmp_path / "workspace",
        samplers=(registration,),
        source_revision="fixture",
        image_digest=_sha("9"),
        implementation_version="0.1.0",
    )
    with TestClient(create_app(token="review-secret", target=target)) as client:
        response = client.get(
            "/v1/target",
            headers={"Authorization": "Bearer review-secret"},
        )
        assert response.status_code == 200
        assert response.json()["implementation_id"] == "stove0.review-materialize-target/v1"
        assert (
            client.get(
                "/v1/sampler",
                headers={"Authorization": "Bearer review-secret"},
            ).status_code
            == 404
        )


def test_review_support_preserves_sampler_terminal_classification() -> None:
    registration, _client = _sampler()
    common = {
        "request_sha256": _sha("1"),
        "sampler_descriptor_sha256": registration.descriptor_sha256,
    }
    retryable = SamplerResult.seal(
        SamplerResultPayload(
            **common,
            state="failed",
            failure=SamplerFailure(
                code="sampler-infrastructure",
                message="temporary sampler failure",
                retryable=True,
            ),
        )
    )
    inapplicable = SamplerResult.seal(
        SamplerResultPayload(
            **common,
            state="inapplicable",
            inapplicable=SamplerInapplicable(
                code="unsupported-content",
                message="fixture content is unsupported",
            ),
        )
    )
    canceled = SamplerResult.seal(SamplerResultPayload(**common, state="canceled"))

    with pytest.raises(TargetExecutionFailure) as retryable_error:
        review_support._require_sampler_success(retryable)
    assert retryable_error.value.retryable is True
    with pytest.raises(TargetExecutionInapplicable, match="fixture content"):
        review_support._require_sampler_success(inapplicable)
    with pytest.raises(TargetExecutionCanceled, match="canceled"):
        review_support._require_sampler_success(canceled)


def test_review_execution_identity_is_the_canonical_semantic_result() -> None:
    output = OutputArtifact(
        id="review-index",
        role="stove0.review.index/v1",
        path="review/index.json",
        bytes=12,
        sha256=_sha("4"),
        media_type="application/json",
    )
    expected = canonical_json_sha256(
        {
            "format": "stove0-review-target-execution/v1",
            "plan_sha256": _sha("1"),
            "image_digest": _sha("2"),
            "sampler_result_sha256": _sha("3"),
            "outputs": [output.model_dump(mode="json")],
        }
    )

    assert (
        review_support._execution_sha256(
            _sha("1"),
            _sha("2"),
            _sha("3"),
            (output,),
        )
        == expected
    )


def test_review_process_environment_is_connected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "review.token"
    token_file.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.setenv("STOVE0_REVIEW_MATERIALIZE_TARGET_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("STOVE0_REVIEW_MATERIALIZE_TARGET_TOKEN", raising=False)
    assert materialize_app._secret() == "file-secret"
    monkeypatch.delenv("STOVE0_REVIEW_MATERIALIZE_TARGET_TOKEN_FILE")
    monkeypatch.setenv("STOVE0_REVIEW_MATERIALIZE_TARGET_TOKEN", "direct-secret")

    registrations = (_sampler()[0],)
    sampler_file = tmp_path / "samplers.json"
    sampler_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("STOVE0_REVIEW_MATERIALIZE_TARGET_SAMPLERS_JSON_FILE", str(sampler_file))
    monkeypatch.delenv("STOVE0_REVIEW_MATERIALIZE_TARGET_SAMPLERS_JSON", raising=False)
    monkeypatch.setattr(materialize_app, "load_sampler_registrations", lambda path: registrations)
    assert materialize_app._sampler_registrations() == registrations
    monkeypatch.delenv("STOVE0_REVIEW_MATERIALIZE_TARGET_SAMPLERS_JSON_FILE")
    monkeypatch.setenv("STOVE0_REVIEW_MATERIALIZE_TARGET_SAMPLERS_JSON", "{}")
    monkeypatch.setattr(
        materialize_app, "parse_sampler_registrations", lambda document: registrations
    )
    assert materialize_app._sampler_registrations() == registrations

    monkeypatch.setenv("STOVE0_REVIEW_MATERIALIZE_TARGET_HOST", "127.0.0.8")
    monkeypatch.setenv("STOVE0_REVIEW_MATERIALIZE_TARGET_PORT", "8188")
    monkeypatch.setenv("STOVE0_REVIEW_MATERIALIZE_TARGET_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("STOVE0_REVIEW_MATERIALIZE_TARGET_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("STOVE0_REVIEW_MATERIALIZE_TARGET_SOURCE_REVISION", "fixture-revision")
    monkeypatch.setenv("STOVE0_REVIEW_MATERIALIZE_TARGET_IMAGE_DIGEST", _sha("9"))
    configured: dict[str, object] = {}

    class ConfiguredTarget:
        def __init__(self, **kwargs: object) -> None:
            configured.update(kwargs)

        def close(self) -> None:
            pass

        def readiness(self) -> None:
            pass

    def run(_app: object, *, host: str, port: int) -> None:
        configured["host"] = host
        configured["port"] = port

    monkeypatch.setattr(materialize_app, "ReviewMaterializeTargetService", ConfiguredTarget)
    monkeypatch.setattr(materialize_app.uvicorn, "run", run)

    assert materialize_app.main([]) == 0
    assert configured == {
        "state_root": tmp_path / "state",
        "workspace_root": tmp_path / "workspace",
        "samplers": registrations,
        "source_revision": "fixture-revision",
        "image_digest": _sha("9"),
        "implementation_version": "0.1.0",
        "terminal_state_retention_seconds": 2_592_000,
        "host": "127.0.0.8",
        "port": 8188,
    }


def test_review_support_registration_count_is_defined_by_deployment(tmp_path: Path) -> None:
    samplers = tuple(
        SamplerConfig(
            id=f"sampler-{index:03}",
            base_url=f"https://sampler-{index:03}.invalid",
            token_file=tmp_path / f"sampler-{index:03}.token",
            descriptor_sha256=_sha("1"),
            image_digest=_sha("2"),
        )
        for index in range(33)
    )

    assert ReviewTargetConfig(samplers=samplers).samplers == samplers


def test_review_effect_deployment_has_one_fixed_effect_contract(tmp_path: Path) -> None:
    registration, _sampler_client = _sampler()
    destination = RcloneReviewDestination(
        identity=_sha("d"),
        remote=str(tmp_path / "delivery"),
    )
    target = ReviewRcloneEffectTargetService(
        state_root=tmp_path / "state",
        workspace_root=tmp_path / "workspace",
        samplers=(registration,),
        source_revision="fixture",
        image_digest=_sha("9"),
        implementation_version="0.1.0",
        destination=destination,
    )
    try:
        request = TargetPreflightRequest(
            protocol="stove0-effect-target/v1",
            operation_id=REVIEW_RCLONE_DELIVER_OPERATION.id,
            operation_contract_sha256=REVIEW_RCLONE_DELIVER_OPERATION.contract_sha256,
            inputs=_input_authority(
                InputArtifact(
                    id="source",
                    role=REVIEW_SOURCE_ROLE,
                    collection=CollectionRootRef(
                        collection_id=1,
                        archive_root_sha256=_sha("1"),
                        content_identity=_sha("2"),
                    ),
                    path="camera/source.wav",
                    bytes=12,
                    sha256=_sha("3"),
                    media_type="audio/wav",
                )
            ),
            intent={
                "sample_plan": _sample_plan().model_dump(mode="json"),
                "variant": {"id": "opus-96", "portable_intent": {"bitrate_kbps": 96}},
            },
            target_options={"sampler_registration_id": "opus"},
        )
        preflight = target.preflight(request)
        assert target.contract().protocol == "stove0-effect-target/v1"
        assert target.contract().implementation_id == "stove0.review-rclone-effect-target/v1"
        assert [item.operation_id for item in target.contract().operations] == [
            REVIEW_RCLONE_DELIVER_OPERATION.id
        ]
        assert preflight.plan.protocol == "stove0-effect-target/v1"
        assert preflight.plan.target_options["destination_identity"] == _sha("d")
    finally:
        target.close()


def test_rclone_review_destination_commits_manifest_last_and_returns_opaque_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()
    sample = output_root / "sample.opus"
    sample.write_bytes(b"sample")
    manifest = tmp_path / "delivery-manifest.json"
    manifest.write_bytes(b'{"format":"stove0-review-delivery-manifest/v1"}')
    calls: list[list[str]] = []

    def run(arguments: list[str], **_kwargs: object) -> object:
        calls.append(arguments)
        return object()

    monkeypatch.setattr(effect_target.subprocess, "run", run)
    destination = RcloneReviewDestination(identity=_sha("d"), remote="fixture:review")
    result = destination.commit(
        delivery_id=_sha("e"),
        output_root=output_root,
        artifacts=(
            OutputArtifact(
                id="sample",
                role="stove0.review.audio/v1",
                path="sample.opus",
                bytes=6,
                sha256=canonical_json_sha256("sample"),
            ),
        ),
        manifest_path=manifest,
    )

    assert [call[1] for call in calls] == ["copy", "copyto"]
    assert calls[-1][-1].endswith("/manifest.json")
    assert result["destination_identity"] == _sha("d")
    assert result["delivery_id"] == _sha("e")
    assert result["artifact_count"] == 1
    assert "fixture:review" not in str(result)


def test_review_effect_environment_binds_nonsecret_identity_and_private_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "rclone.conf"
    monkeypatch.setenv("STOVE0_REVIEW_RCLONE_EFFECT_TARGET_DESTINATION_IDENTITY", _sha("d"))
    monkeypatch.setenv("STOVE0_REVIEW_RCLONE_EFFECT_TARGET_RCLONE_REMOTE", "private:review")
    monkeypatch.setenv("STOVE0_REVIEW_RCLONE_EFFECT_TARGET_RCLONE_CONFIG_FILE", str(config))
    monkeypatch.setenv("STOVE0_REVIEW_RCLONE_EFFECT_TARGET_RCLONE_TIMEOUT_SECONDS", "7200")
    monkeypatch.setenv("STOVE0_REVIEW_RCLONE_EFFECT_TARGET_RCLONE_BIN", "/opt/tools/rclone")
    destination = effect_app._effect_destination()
    assert destination.identity == _sha("d")
    assert destination.remote == "private:review"
    assert destination.config_path == config
    assert destination.executable == "/opt/tools/rclone"
    assert destination.timeout_seconds == 7200


def test_review_effect_executes_sampling_delivery_and_canonical_receipt_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(mode=0o700)
    descriptor = _sampler()[0].descriptor()

    class RenderingSampler(FixtureSamplerClient):
        def sample(self, request: object) -> SamplerResult:
            from stove0_review_sampler_protocol import SamplerRequest

            typed = cast(SamplerRequest, request)
            outputs = []
            for window in typed.windows:
                payload = f"review:{window.id}".encode()
                path = workspace_root / typed.workspace_id / window.output_path
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_bytes(payload)
                outputs.append(
                    SamplerOutput(
                        id=window.id,
                        path=window.output_path,
                        bytes=len(payload),
                        sha256=hashlib.sha256(payload).hexdigest(),
                        media_type="audio/ogg",
                        derived_from=(window.input_id,),
                    )
                )
            return SamplerResult.seal(
                SamplerResultPayload(
                    request_sha256=typed.request_sha256,
                    sampler_descriptor_sha256=self.value.descriptor_sha256,
                    state="succeeded",
                    outputs=tuple(outputs),
                )
            )

    sampler_client = RenderingSampler(descriptor)
    registration = SamplerRegistration(
        id="opus",
        client=cast(ReviewSamplerClient, sampler_client),
        descriptor_sha256=descriptor.descriptor_sha256,
        image_digest=descriptor.image_digest,
    )
    destination = RcloneReviewDestination(identity=_sha("d"), remote="fixture:review")
    target = ReviewRcloneEffectTargetService(
        state_root=tmp_path / "state",
        workspace_root=workspace_root,
        samplers=(registration,),
        source_revision="fixture",
        image_digest=_sha("9"),
        implementation_version="0.1.0",
        destination=destination,
    )
    sample_plan = ReviewSamplePlan.seal(
        ReviewSamplePlanPayload(
            samples_per_artifact=1,
            window_duration_ms=1000,
            windows=(ReviewSampleWindow(artifact_id="source", start_ms=0, duration_ms=1000),),
        )
    )
    source_payload = b"source-audio"
    source = InputArtifact(
        id="source",
        role=REVIEW_SOURCE_ROLE,
        collection=CollectionRootRef(
            collection_id=1,
            archive_root_sha256=_sha("1"),
            content_identity=_sha("2"),
        ),
        path="camera/source.wav",
        bytes=len(source_payload),
        sha256=hashlib.sha256(source_payload).hexdigest(),
        media_type="audio/wav",
    )
    preflight = target.preflight(
        TargetPreflightRequest(
            protocol="stove0-effect-target/v1",
            operation_id=REVIEW_RCLONE_DELIVER_OPERATION.id,
            operation_contract_sha256=REVIEW_RCLONE_DELIVER_OPERATION.contract_sha256,
            inputs=_input_authority(source),
            intent={
                "sample_plan": sample_plan.model_dump(mode="json"),
                "variant": {"id": "opus-96", "portable_intent": {"bitrate_kbps": 96}},
            },
            target_options={"sampler_registration_id": "opus"},
        )
    )
    work = WorkIdentity.seal(
        WorkPayload(
            recipe=RecipeRef(id="fixture.review-effect/v1", revision=1, sha256=_sha("4")),
            inputs=(source.collection,),
        )
    )
    workflow = WorkflowPlan.seal(
        WorkflowPlanPayload(
            work=work,
            result_kind="external-effect",
            operation=OperationRef(
                id=REVIEW_RCLONE_DELIVER_OPERATION.id,
                sha256=REVIEW_RCLONE_DELIVER_OPERATION.contract_sha256,
            ),
            target_registration_id="review-effect",
            target_contract_sha256=preflight.target.contract_sha256,
            retirement_policy="retain",
        )
    )
    binding = TargetPlanBinding(
        protocol=preflight.target.protocol,
        target_implementation_id=preflight.target.implementation_id,
        target_contract_sha256=preflight.target.contract_sha256,
        operation_contract_sha256=REVIEW_RCLONE_DELIVER_OPERATION.contract_sha256,
        plan=preflight.plan.binding_document(),
        plan_sha256=preflight.plan.plan_sha256,
    )
    envelope = ExecutionEnvelope.seal(
        ExecutionEnvelopePayload(
            claim_id=work.work_id,
            fence=1,
            workflow_plan=workflow,
            target_plan=binding,
        )
    )
    evidence = ControllerEvidence.seal(ControllerEvidencePayload(execution_envelope=envelope))
    request = TargetJobRequest.seal(
        TargetJobDeclaration(
            job_id=envelope.execution_envelope_sha256,
            claim_id=work.work_id,
            fence=1,
            controller_evidence=evidence,
            plan=preflight.plan,
            workspace_assurance="ephemeral",
        ),
        TargetRuntimeAuthority(
            riverhog_base_url="https://riverhog.invalid",
            capability_token="fixture-secret",
        ),
        TargetCallbackAccess(
            stove0_base_url="https://stove0.invalid",
            token="callback-secret",
        ),
    )

    class Retrieval(AbstractContextManager["Retrieval"]):
        def __enter__(self) -> Retrieval:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def download(self, _claimed: object, path: Path) -> None:
            path.write_bytes(source_payload)

    class Execution(AbstractContextManager["Execution"]):
        completed = False

        def __enter__(self) -> Execution:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def open_workspace(self, root: Path) -> TransformWorkspace:
            return TransformWorkspace.open(
                root,
                execution_id=request.declaration.job_id,
                assurance="ephemeral",
            )

        def iter_inputs(self) -> tuple[tuple[InputArtifact, object], ...]:
            return ((source, object()),)

        def prepare_inputs(self, _inputs: object) -> Retrieval:
            return Retrieval()

        def effect_success(self, result: object, **kwargs: object) -> TargetJobStatus:
            runtime = TargetExecutionRuntime(request, object())  # type: ignore[arg-type]
            status = runtime.effect_success(cast(dict[str, object], result), **kwargs)  # type: ignore[arg-type]
            self.completed = True
            return status

    execution = Execution()
    monkeypatch.setattr(
        review_support.TargetExecutionRuntime,
        "from_request",
        lambda *_args, **_kwargs: execution,
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        effect_target.subprocess,
        "run",
        lambda arguments, **_kwargs: commands.append(arguments),
    )
    try:
        status = target._execute(  # noqa: SLF001 - exact maintained-target integration proof
            request,
            1,
            threading.Event(),
            cast(object, None),  # type: ignore[arg-type]
        )
    finally:
        target.close()

    assert status.state == "succeeded"
    assert status.protocol == "stove0-effect-target/v1"
    assert (
        status.output_collection is None and status.production is None and status.derivation is None
    )
    assert status.effect_receipt is not None
    assert status.effect_receipt.result["destination_identity"] == _sha("d")
    assert status.effect_receipt.result["delivery_id"] == request.declaration.job_id
    assert [command[1] for command in commands] == ["copy", "copyto"]
