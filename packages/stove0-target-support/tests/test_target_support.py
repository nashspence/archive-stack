from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from riverhog_protocol import Conflict, Unauthorized
from riverhog_protocol.collection_workflows import (
    ArtifactDisposition,
    CollectionDerivation,
)
from riverhog_protocol.collection_workflows import (
    canonical_json_sha256 as riverhog_canonical_json_sha256,
)
from riverhog_transform_sdk import (
    ClaimedCollectionRuntime,
    ClaimedCollectionRuntimeRegistry,
    CollectionTransformRuntime,
    DerivedCollectionReceipt,
)
from stove0_protocol import (
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
from stove0_target_client import TargetClient
from stove0_target_support import (
    DEFAULT_TERMINAL_STATE_RETENTION_SECONDS,
    EFFECT_TARGET_PROTOCOL,
    TARGET_TERMINAL_STATE_RETENTION_ENV,
    EffectPlan,
    EffectPlanPayload,
    InputArtifact,
    InputArtifactContract,
    OperationContract,
    OperationContractPayload,
    OutputArtifact,
    OutputArtifactContract,
    OutputCollectionRef,
    PersistentTargetService,
    TargetCancelRequest,
    TargetContract,
    TargetContractPayload,
    TargetEffectCommitUncertain,
    TargetExecutionCanceled,
    TargetExecutionEvidence,
    TargetExecutionFailure,
    TargetExecutionInapplicable,
    TargetExecutionRuntime,
    TargetExecutionSession,
    TargetHttpBinding,
    TargetJobDeclaration,
    TargetJobRequest,
    TargetJobStatus,
    TargetOperationSupport,
    TargetPreflightRequest,
    TargetPreflightResponse,
    TargetProgress,
    TargetRuntimeAuthority,
    TransformPlan,
    TransformPlanPayload,
    canonical_json_bytes,
    canonical_json_sha256,
    conformance_report,
    target_schema_bundle,
    terminal_state_retention_seconds,
    validate_preflight_response_against_request,
    validate_status_against_request,
)


def _sha(character: str) -> str:
    return character * 64


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, b"null"),
        (True, b"true"),
        (False, b"false"),
        (0, b"0"),
        (-12, b"-12"),
        (1.0, b"1"),
        (-0.0, b"0"),
        (1e-6, b"0.000001"),
        (1e-7, b"1e-7"),
        (1e20, b"100000000000000000000"),
        (1e21, b"1e+21"),
        (333333333.3333333, b"333333333.3333333"),
        ('quotes " controls \n and unicode å', '"quotes \\" controls \\n and unicode å"'.encode()),
        (
            {"z": 1, "a": [3, 2, 1], "😀": "astral", "€": "bmp"},
            '{"a":[3,2,1],"z":1,"€":"bmp","😀":"astral"}'.encode(),
        ),
    ],
)
def test_local_jcs_matches_ecmascript_for_representative_i_json_values(
    value: Any,
    expected: bytes,
) -> None:
    assert canonical_json_bytes(value) == expected


def _operation() -> OperationContract:
    return OperationContract.seal(
        OperationContractPayload(
            id="fixture.copy/v1",
            intent_schema=JsonSchemaDocument.from_schema(
                "fixture.copy-intent/v1",
                {
                    "type": "object",
                    "properties": {"suffix": {"type": "string"}},
                    "required": ["suffix"],
                    "additionalProperties": False,
                },
            ),
            inputs=(
                InputArtifactContract(
                    role="fixture.source/v1",
                    allowed_dispositions=("transformed",),
                ),
            ),
            outputs=(
                OutputArtifactContract(
                    role="fixture.output/v1",
                    derived_from_roles=("fixture.source/v1",),
                ),
            ),
            source_retirement_permitted=True,
        )
    )


def _target(operation: OperationContract) -> TargetContract:
    return TargetContract.seal(
        TargetContractPayload(
            implementation_id="fixture.target/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            image_digest=_sha("9"),
            operations=(
                TargetOperationSupport(
                    operation_id=operation.id,
                    operation_contract_sha256=operation.contract_sha256,
                    options_schema=JsonSchemaDocument.from_schema(
                        "fixture.target-options/v1",
                        {"type": "object", "additionalProperties": False},
                    ),
                ),
            ),
        )
    )


def _input() -> InputArtifact:
    return InputArtifact(
        id="source",
        role="fixture.source/v1",
        collection=CollectionRootRef(
            collection_id=1,
            manifest_sha256=_sha("1"),
            content_identity=_sha("2"),
        ),
        path="source/input.bin",
        bytes=12,
        sha256=_sha("3"),
    )


def _work() -> WorkIdentity:
    return WorkIdentity.seal(
        WorkPayload(
            recipe=RecipeRef(id="fixture.recipe/v1", revision=1, sha256=_sha("4")),
            inputs=(_input().collection,),
            effective_intent={"suffix": ".copy"},
        )
    )


def _plan(
    operation: OperationContract,
    target: TargetContract,
) -> TransformPlan:
    return TransformPlan.seal(
        TransformPlanPayload(
            target_implementation_id=target.implementation_id,
            target_contract_sha256=target.contract_sha256,
            operation_id=operation.id,
            operation_contract_sha256=operation.contract_sha256,
            inputs=(_input(),),
            intent={"suffix": ".copy"},
            target_options={},
        )
    )


def _controller_evidence(
    operation: OperationContract,
    target: TargetContract,
    plan: TransformPlan,
) -> ControllerEvidence:
    work = _work()
    workflow = WorkflowPlan.seal(
        WorkflowPlanPayload(
            work=work,
            operation=OperationRef(id=operation.id, sha256=operation.contract_sha256),
            target_registration_id="fixture-target",
            target_contract_sha256=target.contract_sha256,
            output_tags=("fixture-output",),
            retirement_policy="retain",
        )
    )
    binding = TargetPlanBinding(
        protocol=target.protocol,
        target_implementation_id=target.implementation_id,
        target_contract_sha256=target.contract_sha256,
        operation_contract_sha256=operation.contract_sha256,
        plan=plan.binding_document(),
        plan_sha256=plan.plan_sha256,
    )
    envelope = ExecutionEnvelope.seal(
        ExecutionEnvelopePayload(
            claim_id=work.work_id,
            fence=2,
            workflow_plan=workflow,
            target_plan=binding,
        )
    )
    return ControllerEvidence.seal(ControllerEvidencePayload(execution_envelope=envelope))


def _request() -> tuple[OperationContract, TargetContract, TargetJobRequest]:
    operation = _operation()
    target = _target(operation)
    plan = _plan(operation, target)
    evidence = _controller_evidence(operation, target, plan)
    declaration = TargetJobDeclaration(
        job_id=evidence.execution_envelope.execution_envelope_sha256,
        claim_id=evidence.execution_envelope.workflow_plan.work.work_id,
        fence=2,
        controller_evidence=evidence,
        plan=plan,
        workspace_assurance="ephemeral",
    )
    request = TargetJobRequest.seal(
        declaration,
        TargetRuntimeAuthority(
            riverhog_base_url="https://riverhog.invalid",
            capability_token="first-secret",
        ),
    )
    return operation, target, request


def _effect_request() -> tuple[OperationContract, TargetContract, TargetJobRequest]:
    operation = OperationContract.seal(
        OperationContractPayload(
            id="fixture.record-index/v1",
            result_kind="external-effect",
            intent_schema=JsonSchemaDocument.from_schema(
                "fixture.record-index-intent/v1",
                {"type": "object", "additionalProperties": False},
            ),
            inputs=(
                InputArtifactContract(
                    role="fixture.source/v1",
                    allowed_dispositions=None,
                ),
            ),
            effect_receipt_schema=JsonSchemaDocument.from_schema(
                "fixture.record-index-receipt/v1",
                {
                    "type": "object",
                    "required": ["format", "row_sha256"],
                    "properties": {
                        "format": {"const": "fixture-index-receipt/v1"},
                        "row_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    },
                    "additionalProperties": False,
                },
            ),
        )
    )
    target = TargetContract.seal(
        TargetContractPayload(
            protocol=EFFECT_TARGET_PROTOCOL,
            implementation_id="fixture.index-target/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            image_digest=_sha("8"),
            operations=(
                TargetOperationSupport(
                    operation_id=operation.id,
                    operation_contract_sha256=operation.contract_sha256,
                    result_kind="external-effect",
                    options_schema=JsonSchemaDocument.from_schema(
                        "fixture.index-target-options/v1",
                        {"type": "object", "additionalProperties": False},
                    ),
                ),
            ),
        )
    )
    plan = EffectPlan.seal(
        EffectPlanPayload(
            target_implementation_id=target.implementation_id,
            target_contract_sha256=target.contract_sha256,
            operation_id=operation.id,
            operation_contract_sha256=operation.contract_sha256,
            inputs=(_input(),),
            intent={},
            target_options={},
        )
    )
    work = _work()
    workflow = WorkflowPlan.seal(
        WorkflowPlanPayload(
            work=work,
            result_kind="external-effect",
            operation=OperationRef(id=operation.id, sha256=operation.contract_sha256),
            target_registration_id="fixture-index-target",
            target_contract_sha256=target.contract_sha256,
            retirement_policy="retain",
        )
    )
    binding = TargetPlanBinding(
        protocol=target.protocol,
        target_implementation_id=target.implementation_id,
        target_contract_sha256=target.contract_sha256,
        operation_contract_sha256=operation.contract_sha256,
        plan=plan.binding_document(),
        plan_sha256=plan.plan_sha256,
    )
    envelope = ExecutionEnvelope.seal(
        ExecutionEnvelopePayload(
            claim_id=work.work_id,
            fence=2,
            workflow_plan=workflow,
            target_plan=binding,
        )
    )
    evidence = ControllerEvidence.seal(ControllerEvidencePayload(execution_envelope=envelope))
    request = TargetJobRequest.seal(
        TargetJobDeclaration(
            job_id=envelope.execution_envelope_sha256,
            claim_id=work.work_id,
            fence=2,
            controller_evidence=evidence,
            plan=plan,
            workspace_assurance="ephemeral",
        ),
        TargetRuntimeAuthority(
            riverhog_base_url="https://riverhog.invalid",
            capability_token="effect-secret",
        ),
    )
    return operation, target, request


def _effect_success_status(
    operation: OperationContract,
    request: TargetJobRequest,
    *,
    attempt: int = 1,
) -> TargetJobStatus:
    return TargetExecutionRuntime(request, object()).effect_success(  # type: ignore[arg-type]
        {"format": "fixture-index-receipt/v1", "row_sha256": _sha("7")},
        operation=operation,
        execution_sha256=_sha("6"),
        attempt=attempt,
        runtime_evidence={"implementation": "fixture"},
    )


def test_preflight_job_identity_excludes_refreshable_capability_secret() -> None:
    operation, target, request = _request()
    second = TargetJobRequest.seal(
        request.declaration,
        request.runtime.model_copy(update={"capability_token": "replacement-secret"}),
    )
    assert second.request_sha256 == request.request_sha256

    preflight_request = TargetPreflightRequest(
        operation_id=operation.id,
        operation_contract_sha256=operation.contract_sha256,
        inputs=request.declaration.plan.inputs,
        intent=request.declaration.plan.intent,
        target_options=request.declaration.plan.target_options,
    )
    response = TargetPreflightResponse(target=target, plan=request.declaration.plan)
    validate_preflight_response_against_request(response, preflight_request)


def _success_status(
    operation: OperationContract,
    request: TargetJobRequest,
) -> TargetJobStatus:
    output = OutputArtifact(
        id="output",
        role="fixture.output/v1",
        path="output/result.bin",
        bytes=12,
        sha256=_sha("5"),
        derived_from=("source",),
    )
    declaration = request.declaration
    workflow = declaration.controller_evidence.execution_envelope.workflow_plan
    derivation = CollectionDerivation(
        execution_id=declaration.job_id,
        claim_id=declaration.claim_id,
        fence=declaration.fence,
        recipe=workflow.work.recipe.to_identity(),
        operation=workflow.operation.to_identity(),
        inputs=tuple(item.to_identity() for item in workflow.work.inputs),
        output_tags=workflow.output_tags,
        execution_envelope_sha256=declaration.job_id,
        execution_sha256=_sha("9"),
        controller_evidence=declaration.controller_evidence.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        ),
        controller_evidence_sha256=riverhog_canonical_json_sha256(
            declaration.controller_evidence.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
        ),
        dispositions=(
            ArtifactDisposition(
                input_collection_id=_input().collection.collection_id,
                input_manifest_sha256=_input().collection.manifest_sha256,
                input_path=_input().path,
                status="transformed",
                outputs=(output.path,),
            ),
        ),
    )
    return TargetJobStatus(
        job_id=request.declaration.job_id,
        state="succeeded",
        attempt=1,
        request_sha256=request.request_sha256,
        plan_sha256=request.declaration.plan.plan_sha256,
        progress=TargetProgress(phase="done", completed=1, total=1, unit="artifacts"),
        outputs=(output,),
        output_collection=OutputCollectionRef(
            collection_id=7,
            manifest_sha256=_sha("6"),
            content_identity=_sha("7"),
            derivation_sha256=derivation.sha256,
        ),
        execution_evidence=TargetExecutionEvidence(
            target_contract_sha256=request.declaration.plan.target_contract_sha256,
            operation_contract_sha256=operation.contract_sha256,
            plan_sha256=request.declaration.plan.plan_sha256,
            execution_sha256=_sha("9"),
            runtime={"tool": "fixture"},
        ),
        derivation=derivation.as_dict(),
    )


def test_success_status_is_operation_checked_and_failure_cannot_publish() -> None:
    operation, _target_contract, request = _request()
    status = _success_status(operation, request)
    validate_status_against_request(status, request, operation)

    with pytest.raises(ValidationError, match="failed target status cannot publish a result"):
        TargetJobStatus(
            **status.model_dump(mode="python", exclude={"state", "failure"}),
            state="failed",
            failure={"code": "fixture.failure/v1", "message": "failed", "retryable": False},
        )


def test_effect_success_is_canonical_bound_and_collection_free() -> None:
    operation, target, request = _effect_request()
    first = _effect_success_status(operation, request)
    second = _effect_success_status(operation, request)

    validate_status_against_request(first, request, operation)
    assert first == second
    assert first.protocol == EFFECT_TARGET_PROTOCOL
    assert first.outputs == ()
    assert first.output_collection is None
    assert first.derivation is None
    assert first.effect_receipt is not None
    assert first.effect_receipt.receipt_sha256 == second.effect_receipt.receipt_sha256  # type: ignore[union-attr]
    assert first.effect_receipt.target_contract_sha256 == target.contract_sha256

    with pytest.raises(ValidationError, match="requires only execution evidence"):
        TargetJobStatus.model_validate(
            {
                **first.model_dump(mode="json"),
                "outputs": [
                    {
                        "id": "forbidden",
                        "role": "fixture.output/v1",
                        "path": "forbidden.bin",
                        "bytes": 0,
                        "sha256": _sha("0"),
                        "derived_from": ["source"],
                    }
                ],
            }
        )


def test_effect_execution_uses_only_generic_claimed_collection_read_custody() -> None:
    _operation_contract, _target_contract, request = _effect_request()
    execution = TargetExecutionRuntime.from_request(request)
    try:
        assert isinstance(execution.runtime, ClaimedCollectionRuntime)
        assert not isinstance(execution.runtime, CollectionTransformRuntime)
        assert not hasattr(execution.runtime, "spec")
        assert not hasattr(execution.runtime, "writer")
        with pytest.raises(RuntimeError, match="cannot publish"):
            execution.publish(
                {},
                artifacts=(),
                execution_sha256=_sha("6"),
                dispositions=(),
            )
    finally:
        execution.runtime.close()


def test_effect_receipt_is_operation_schema_checked() -> None:
    operation, _target, request = _effect_request()
    status = _effect_success_status(operation, request)
    assert status.effect_receipt is not None
    changed = status.model_copy(
        update={
            "effect_receipt": status.effect_receipt.model_copy(
                update={"result": {"format": "fixture-index-receipt/v1"}}
            )
        }
    )
    with pytest.raises(Exception, match="row_sha256"):
        validate_status_against_request(changed, request, operation)


def test_target_runtime_builds_complete_success_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation, _target_contract, request = _request()
    expected = _success_status(operation, request)
    assert expected.derivation is not None
    derivation = CollectionDerivation.from_mapping(expected.derivation)
    output_collection = expected.output_collection
    assert output_collection is not None
    session = TargetExecutionSession(request, 1, ClaimedCollectionRuntimeRegistry())
    runtime = TargetExecutionRuntime(
        request,
        object(),  # type: ignore[arg-type]
        session=session,
    )

    def publish(*_args: object, **_kwargs: object) -> tuple[DerivedCollectionReceipt, object]:
        return (
            DerivedCollectionReceipt(
                collection_id=output_collection.collection_id,
                manifest_sha256=output_collection.manifest_sha256,
                content_identity=output_collection.content_identity,
                derivation=derivation,
            ),
            output_collection,
        )

    monkeypatch.setattr(runtime, "publish", publish)
    result = runtime.publish_success(
        {"output": object()},  # type: ignore[dict-item]
        artifacts=expected.outputs,
        operation=operation,
        execution_sha256=_sha("9"),
        dispositions=derivation.dispositions,
        runtime_evidence={"tool": "fixture"},
    )
    assert result == expected
    assert session.completed_status == expected


class FixtureTargetClient:
    def __init__(
        self,
        target: TargetContract,
        request: TargetJobRequest,
        status: TargetJobStatus,
    ) -> None:
        self.target = target
        self.request = request
        self.status_value = status

    def contract(self) -> TargetContract:
        return self.target

    def preflight(self, _request: TargetPreflightRequest) -> TargetPreflightResponse:
        return TargetPreflightResponse(target=self.target, plan=self.request.declaration.plan)

    def put_job(self, _request: TargetJobRequest) -> TargetJobStatus:
        return self.status_value

    def status(self, _job_id: str) -> TargetJobStatus:
        return self.status_value


def test_conformance_report_proves_preflight_and_idempotent_submission() -> None:
    operation, target, request = _request()
    status = _success_status(operation, request)
    report = conformance_report(
        FixtureTargetClient(target, request, status),
        operation=operation,
        job_request=request,
    )
    assert report["status"] == "conformant"
    assert report["transport"] == "riverhog-capability/v1"


def test_target_client_rejects_remote_plain_http_by_default() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        TargetClient("http://target.example")
    assert TargetClient("http://localhost:8000").base_url.startswith("http://")


def test_operation_contract_rejects_unpermitted_input_disposition() -> None:
    operation, _target_contract, request = _request()
    status = _success_status(operation, request)
    assert status.derivation is not None
    assert status.output_collection is not None
    derivation = CollectionDerivation.from_mapping(status.derivation)
    rejected = ArtifactDisposition(
        input_collection_id=derivation.dispositions[0].input_collection_id,
        input_manifest_sha256=derivation.dispositions[0].input_manifest_sha256,
        input_path=derivation.dispositions[0].input_path,
        status="rejected",
        code="fixture.rejected/v1",
        message="fixture rejection",
    )
    changed = CollectionDerivation(
        execution_id=derivation.execution_id,
        claim_id=derivation.claim_id,
        fence=derivation.fence,
        recipe=derivation.recipe,
        operation=derivation.operation,
        inputs=derivation.inputs,
        output_tags=derivation.output_tags,
        execution_envelope_sha256=derivation.execution_envelope_sha256,
        execution_sha256=derivation.execution_sha256,
        controller_evidence=derivation.controller_evidence,
        controller_evidence_sha256=derivation.controller_evidence_sha256,
        dispositions=(rejected,),
    )
    changed_status = TargetJobStatus.model_validate(
        {
            **status.model_dump(mode="json", exclude={"derivation", "output_collection"}),
            "derivation": changed.as_dict(),
            "output_collection": {
                **status.output_collection.model_dump(mode="json"),
                "derivation_sha256": changed.sha256,
            },
        }
    )
    with pytest.raises(ValueError, match="disposition is not permitted"):
        validate_status_against_request(changed_status, request, operation)


class BindingTargetService:
    def __init__(
        self,
        target: TargetContract,
        request: TargetJobRequest,
        status: TargetJobStatus,
    ) -> None:
        self.target = target
        self.request = request
        self.status_value = status

    def contract(self) -> TargetContract:
        return self.target

    def preflight(self, _request: TargetPreflightRequest) -> TargetPreflightResponse:
        return TargetPreflightResponse(
            target=self.target,
            plan=self.request.declaration.plan,
        )

    def put_job(self, request: TargetJobRequest) -> TargetJobStatus:
        assert request.accepted() == self.request.accepted()
        self.request = request
        return self.status_value

    def get_job(self, job_id: str) -> TargetJobStatus:
        assert job_id == self.request.declaration.job_id
        return self.status_value

    def cancel_job(
        self,
        job_id: str,
        _request: TargetCancelRequest,
    ) -> TargetJobStatus:
        assert job_id == self.request.declaration.job_id
        return self.status_value


def test_framework_neutral_target_http_binding() -> None:
    operation, target, request = _request()
    status = _success_status(operation, request)
    binding = TargetHttpBinding(BindingTargetService(target, request, status))

    contract_response = binding.handle("GET", "/v1/target")
    assert contract_response.status == 200
    assert TargetContract.model_validate_json(contract_response.body) == target

    preflight_request = TargetPreflightRequest(
        operation_id=operation.id,
        operation_contract_sha256=operation.contract_sha256,
        inputs=request.declaration.plan.inputs,
        intent=request.declaration.plan.intent,
        target_options=request.declaration.plan.target_options,
    )
    preflight_response = binding.handle(
        "POST",
        "/v1/preflight",
        preflight_request.model_dump_json(exclude_none=True).encode(),
    )
    assert preflight_response.status == 200
    assert TargetPreflightResponse.model_validate_json(preflight_response.body).target == target

    job_response = binding.handle(
        "PUT",
        f"/v1/jobs/{request.declaration.job_id}",
        request.model_dump_json(exclude_none=True).encode(),
    )
    assert job_response.status == 200
    assert TargetJobStatus.model_validate_json(job_response.body) == status
    assert binding.handle("PATCH", "/v1/target").status == 405


def test_target_schema_bundle_is_deterministic_and_self_validating() -> None:
    first = target_schema_bundle()
    second = target_schema_bundle()
    assert first == second
    digest = first.pop("bundle_sha256")
    assert canonical_json_sha256(first) == digest
    assert first["http_binding"]["PUT /v1/jobs/{job_id}"] == {
        "request": "TargetJobRequest",
        "response": "TargetJobStatus",
    }
    for schema in first["schemas"].values():
        Draft202012Validator.check_schema(schema)


def _write_model(path: Path, value: Any) -> None:
    path.write_bytes(
        canonical_json_bytes(value.model_dump(mode="json", by_alias=True, exclude_none=True))
    )


def test_terminal_state_retention_configuration_is_connected_and_fail_closed() -> None:
    assert terminal_state_retention_seconds({}) == DEFAULT_TERMINAL_STATE_RETENTION_SECONDS
    assert terminal_state_retention_seconds({TARGET_TERMINAL_STATE_RETENTION_ENV: "3600"}) == 3600
    with pytest.raises(ValueError, match="must be an integer"):
        terminal_state_retention_seconds({TARGET_TERMINAL_STATE_RETENTION_ENV: "one-day"})
    with pytest.raises(ValueError, match="must be positive"):
        terminal_state_retention_seconds({TARGET_TERMINAL_STATE_RETENTION_ENV: "0"})


def test_persistent_target_prunes_only_expired_terminal_request_pairs(
    tmp_path: Path,
) -> None:
    operation, target, request = _request()
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    expired_status = _success_status(operation, request)
    _write_model(
        state_root / f"{request.declaration.job_id}.accepted.json",
        request.accepted(),
    )
    expired_path = state_root / f"{request.declaration.job_id}.status.json"
    _write_model(expired_path, expired_status)

    interrupted = TargetJobStatus(
        job_id=_sha("b"),
        state="interrupted",
        attempt=1,
        request_sha256=_sha("c"),
        plan_sha256=_sha("d"),
        progress=TargetProgress(phase="interrupted", completed=0),
    )
    interrupted_path = state_root / f"{interrupted.job_id}.status.json"
    _write_model(interrupted_path, interrupted)
    fresh = TargetJobStatus(
        job_id=_sha("e"),
        state="canceled",
        attempt=1,
        request_sha256=_sha("f"),
        plan_sha256=_sha("0"),
        progress=TargetProgress(phase="canceled", completed=0),
    )
    fresh_path = state_root / f"{fresh.job_id}.status.json"
    _write_model(fresh_path, fresh)

    observed_now = time.time()
    expired_mtime = observed_now - 101
    for path in (expired_path, interrupted_path):
        path.touch()
        path.chmod(0o600)
        os.utime(path, (expired_mtime, expired_mtime))

    service = PersistentTargetService(
        contract=target,
        operations={operation.id: operation},
        state_root=state_root,
        execute=lambda *_args: expired_status,
        terminal_state_retention_seconds=100,
    )
    try:
        assert service.prune_terminal_state(now=observed_now) == {"jobs": 0, "bytes": 0}
        assert sorted(path.name for path in state_root.iterdir()) == [
            f"{interrupted.job_id}.status.json",
            f"{fresh.job_id}.status.json",
        ]
    finally:
        service.close()


def test_persistent_target_resumes_exact_declaration_without_storing_authority(
    tmp_path: Path,
) -> None:
    operation, target, request = _request()
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    job_id = request.declaration.job_id
    _write_model(state_root / f"{job_id}.accepted.json", request.accepted())
    _write_model(
        state_root / f"{job_id}.status.json",
        TargetJobStatus(
            job_id=job_id,
            state="running",
            attempt=1,
            request_sha256=request.request_sha256,
            plan_sha256=request.declaration.plan.plan_sha256,
            progress=TargetProgress(phase="transforming", completed=0),
        ),
    )

    def execute(
        _request: TargetJobRequest,
        _attempt: int,
        _cancellation: threading.Event,
        _session: TargetExecutionSession,
    ) -> TargetJobStatus:
        raise TargetExecutionInapplicable("unsupported-content", "fixture input")

    service = PersistentTargetService(
        contract=target,
        operations={operation.id: operation},
        state_root=state_root,
        execute=execute,
    )
    try:
        assert service.get_job(job_id).state == "interrupted"
        assert service.put_job(request).attempt == 2
        deadline = time.monotonic() + 5
        while service.get_job(job_id).state != "inapplicable":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert service.put_job(request) == service.get_job(job_id)
    finally:
        service.close()

    persisted = b"\n".join(path.read_bytes() for path in state_root.iterdir())
    assert b"first-secret" not in persisted
    assert b"riverhog.invalid" not in persisted


def test_persistent_target_shutdown_and_operator_cancel_have_distinct_state(
    tmp_path: Path,
) -> None:
    operation, target, request = _request()
    started = threading.Event()

    def block(
        _request: TargetJobRequest,
        _attempt: int,
        cancellation: threading.Event,
        _session: TargetExecutionSession,
    ) -> TargetJobStatus:
        started.set()
        assert cancellation.wait(timeout=5)
        raise TargetExecutionCanceled

    interrupted = PersistentTargetService(
        contract=target,
        operations={operation.id: operation},
        state_root=tmp_path / "interrupted",
        execute=block,
    )
    interrupted.put_job(request)
    assert started.wait(timeout=5)
    interrupted.close()
    assert interrupted.get_job(request.declaration.job_id).state == "interrupted"

    started.clear()
    canceled = PersistentTargetService(
        contract=target,
        operations={operation.id: operation},
        state_root=tmp_path / "canceled",
        execute=block,
    )
    try:
        canceled.put_job(request)
        assert started.wait(timeout=5)
        assert (
            canceled.cancel_job(
                request.declaration.job_id,
                TargetCancelRequest(reason="operator"),
            ).state
            == "canceling"
        )
        deadline = time.monotonic() + 5
        while canceled.get_job(request.declaration.job_id).state != "canceled":
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        canceled.close()


def test_running_target_receives_capability_refresh_without_persisting_secrets(
    tmp_path: Path,
) -> None:
    operation, target, request = _request()
    state_root = tmp_path / "state"
    bound = threading.Event()
    refreshed = threading.Event()
    finish = threading.Event()
    tokens: list[str] = []

    class Runtime:
        def refresh_capability(self, token: str) -> None:
            tokens.append(token)
            if token == "replacement-secret":
                refreshed.set()

        def close(self) -> None:
            pass

    def execute(
        _request: TargetJobRequest,
        _attempt: int,
        _cancellation: threading.Event,
        session: TargetExecutionSession,
    ) -> TargetJobStatus:
        with session.runtime_registry.bind(request.declaration.job_id, Runtime()):  # type: ignore[arg-type]
            bound.set()
            assert finish.wait(timeout=5)
        raise TargetExecutionInapplicable("fixture-inapplicable", "fixture input")

    service = PersistentTargetService(
        contract=target,
        operations={operation.id: operation},
        state_root=state_root,
        execute=execute,
    )
    try:
        service.put_job(request)
        assert bound.wait(timeout=5)
        replacement = TargetJobRequest.seal(
            request.declaration,
            request.runtime.model_copy(update={"capability_token": "replacement-secret"}),
        )
        assert service.put_job(replacement).state == "running"
        assert refreshed.wait(timeout=5)
        assert service.put_job(replacement).state == "running"
        finish.set()
        deadline = time.monotonic() + 5
        while service.get_job(request.declaration.job_id).state != "inapplicable":
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        finish.set()
        service.close()

    assert tokens == ["first-secret", "replacement-secret"]
    persisted = b"\n".join(path.read_bytes() for path in state_root.iterdir())
    assert b"first-secret" not in persisted
    assert b"replacement-secret" not in persisted
    assert b"riverhog.invalid" not in persisted


def test_restart_before_publication_preserves_semantic_execution_identity(
    tmp_path: Path,
) -> None:
    operation, target, request = _request()
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    job_id = request.declaration.job_id
    _write_model(state_root / f"{job_id}.accepted.json", request.accepted())
    _write_model(
        state_root / f"{job_id}.status.json",
        TargetJobStatus(
            job_id=job_id,
            state="running",
            attempt=1,
            request_sha256=request.request_sha256,
            plan_sha256=request.declaration.plan.plan_sha256,
            progress=TargetProgress(phase="publishing", completed=0),
        ),
    )
    first_attempt = _success_status(operation, request)

    def execute(
        _request: TargetJobRequest,
        attempt: int,
        _cancellation: threading.Event,
        session: TargetExecutionSession,
    ) -> TargetJobStatus:
        restarted = first_attempt.model_copy(update={"attempt": attempt})
        session.record_completed(restarted)
        return restarted

    service = PersistentTargetService(
        contract=target,
        operations={operation.id: operation},
        state_root=state_root,
        execute=execute,
    )
    try:
        assert service.get_job(job_id).state == "interrupted"
        assert service.put_job(request).attempt == 2
        deadline = time.monotonic() + 5
        restarted = service.get_job(job_id)
        while restarted.state != "succeeded":
            assert time.monotonic() < deadline
            time.sleep(0.01)
            restarted = service.get_job(job_id)
    finally:
        service.close()

    assert restarted.attempt == 2
    assert restarted.request_sha256 == first_attempt.request_sha256
    assert restarted.execution_evidence == first_attempt.execution_evidence
    assert restarted.output_collection == first_attempt.output_collection
    assert restarted.derivation == first_attempt.derivation


def test_persisted_publication_survives_lost_response_and_process_restart(
    tmp_path: Path,
) -> None:
    operation, target, request = _request()
    state_root = tmp_path / "state"
    finished = threading.Event()
    success = _success_status(operation, request)

    def publish(
        _request: TargetJobRequest,
        _attempt: int,
        _cancellation: threading.Event,
        session: TargetExecutionSession,
    ) -> TargetJobStatus:
        session.record_completed(success)
        finished.set()
        return success

    first = PersistentTargetService(
        contract=target,
        operations={operation.id: operation},
        state_root=state_root,
        execute=publish,
    )
    first.put_job(request)
    assert finished.wait(timeout=5)
    deadline = time.monotonic() + 5
    while first.get_job(request.declaration.job_id).state != "succeeded":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    first.close()

    called = False

    def unexpected_execution(
        _request: TargetJobRequest,
        _attempt: int,
        _cancellation: threading.Event,
        _session: TargetExecutionSession,
    ) -> TargetJobStatus:
        nonlocal called
        called = True
        raise AssertionError("published target output must not execute again")

    restarted = PersistentTargetService(
        contract=target,
        operations={operation.id: operation},
        state_root=state_root,
        execute=unexpected_execution,
    )
    try:
        replacement = TargetJobRequest.seal(
            request.declaration,
            request.runtime.model_copy(update={"capability_token": "replacement-secret"}),
        )
        assert restarted.put_job(replacement) == success
        assert not called
    finally:
        restarted.close()

    persisted = b"\n".join(path.read_bytes() for path in state_root.iterdir())
    assert b"first-secret" not in persisted
    assert b"replacement-secret" not in persisted


def test_persisted_effect_receipt_replays_without_repeating_external_effect(
    tmp_path: Path,
) -> None:
    operation, target, request = _effect_request()
    state_root = tmp_path / "effect-state"
    committed = threading.Event()
    calls = 0

    def execute(
        _request: TargetJobRequest,
        attempt: int,
        _cancellation: threading.Event,
        session: TargetExecutionSession,
    ) -> TargetJobStatus:
        nonlocal calls
        calls += 1
        status = _effect_success_status(operation, request, attempt=attempt)
        session.record_completed(status)
        committed.set()
        return status

    first = PersistentTargetService(
        contract=target,
        operations={operation.id: operation},
        state_root=state_root,
        execute=execute,
    )
    first.put_job(request)
    assert committed.wait(timeout=5)
    deadline = time.monotonic() + 5
    while first.get_job(request.declaration.job_id).state != "succeeded":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    expected = first.get_job(request.declaration.job_id)
    first.close()
    expired = time.time() - 100
    for path in state_root.iterdir():
        os.utime(path, (expired, expired))

    def repeat_forbidden(*_args: object) -> TargetJobStatus:
        raise AssertionError("a persisted external effect must not execute again")

    restarted = PersistentTargetService(
        contract=target,
        operations={operation.id: operation},
        state_root=state_root,
        execute=repeat_forbidden,  # type: ignore[arg-type]
        terminal_state_retention_seconds=1,
    )
    try:
        assert len(tuple(state_root.iterdir())) == 2
        assert restarted.put_job(request) == expected
    finally:
        restarted.close()
    assert calls == 1


def test_uncertain_effect_commit_stays_interrupted_and_never_auto_repeats(
    tmp_path: Path,
) -> None:
    operation, target, request = _effect_request()
    attempted = threading.Event()
    calls = 0

    def uncertain(
        _request: TargetJobRequest,
        _attempt: int,
        _cancellation: threading.Event,
        _session: TargetExecutionSession,
    ) -> TargetJobStatus:
        nonlocal calls
        calls += 1
        attempted.set()
        raise TargetEffectCommitUncertain("fixture external commit is uncertain")

    service = PersistentTargetService(
        contract=target,
        operations={operation.id: operation},
        state_root=tmp_path / "uncertain-effect-state",
        execute=uncertain,
    )
    try:
        service.put_job(request)
        assert attempted.wait(timeout=5)
        deadline = time.monotonic() + 5
        while service.get_job(request.declaration.job_id).state != "interrupted":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        interrupted = service.get_job(request.declaration.job_id)
        assert interrupted.progress.phase == "external-commit-uncertain"
        assert service.put_job(request) == interrupted
        assert (
            service.cancel_job(
                request.declaration.job_id,
                TargetCancelRequest(reason="operator inspected uncertainty"),
            )
            == interrupted
        )
        assert calls == 1
    finally:
        service.close()


def test_terminal_target_jobs_release_process_local_bookkeeping(tmp_path: Path) -> None:
    operation, target, request = _request()
    finished = threading.Event()

    def execute(
        _request: TargetJobRequest,
        _attempt: int,
        _cancellation: threading.Event,
        _session: TargetExecutionSession,
    ) -> TargetJobStatus:
        finished.set()
        raise TargetExecutionInapplicable("fixture-content", "fixture input")

    service = PersistentTargetService(
        contract=target,
        operations={operation.id: operation},
        state_root=tmp_path / "state",
        execute=execute,
    )
    try:
        service.put_job(request)
        assert finished.wait(timeout=5)
        deadline = time.monotonic() + 5
        while service._futures:  # noqa: SLF001 - white-box bounded-state proof
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert service._cancel == {}  # noqa: SLF001 - white-box bounded-state proof
        assert service._operator_canceled == set()  # noqa: SLF001
        assert service._shutdown_interrupted == set()  # noqa: SLF001
    finally:
        service.close()


def test_published_success_wins_late_cancel_and_cleanup_failure(tmp_path: Path) -> None:
    operation, target, request = _request()
    published = threading.Event()
    release = threading.Event()
    success = _success_status(operation, request)

    def execute(
        _request: TargetJobRequest,
        _attempt: int,
        _cancellation: threading.Event,
        session: TargetExecutionSession,
    ) -> TargetJobStatus:
        session.record_completed(success)
        published.set()
        assert release.wait(timeout=5)
        raise RuntimeError("post-publication cleanup failed")

    service = PersistentTargetService(
        contract=target,
        operations={operation.id: operation},
        state_root=tmp_path / "state",
        execute=execute,
    )
    try:
        service.put_job(request)
        assert published.wait(timeout=5)
        assert (
            service.cancel_job(
                request.declaration.job_id,
                TargetCancelRequest(reason="late operator request"),
            ).state
            == "canceling"
        )
        release.set()
        deadline = time.monotonic() + 5
        while service.get_job(request.declaration.job_id).state != "succeeded":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert (
            service.cancel_job(
                request.declaration.job_id,
                TargetCancelRequest(reason="later operator request"),
            )
            == success
        )
    finally:
        release.set()
        service.close()


@pytest.mark.parametrize(
    ("failure", "state", "code", "retryable"),
    [
        (
            TargetExecutionInapplicable("fixture-content", "unsupported fixture"),
            "inapplicable",
            "fixture-content",
            None,
        ),
        (
            TargetExecutionFailure("fixture-tool", "tool unavailable", retryable=True),
            "failed",
            "fixture-tool",
            True,
        ),
        (Unauthorized("expired capability", status=401), "failed", "target-authorization", True),
        (Conflict("stale fence", status=409), "failed", "target-conflict", True),
        (OSError("storage unavailable"), "failed", "target-infrastructure", True),
        (ValueError("unexpected implementation defect"), "failed", "target-software", True),
    ],
)
def test_target_failure_classes_remain_distinct_from_content_inapplicability(
    tmp_path: Path,
    failure: Exception,
    state: str,
    code: str,
    retryable: bool | None,
) -> None:
    operation, target, request = _request()

    def execute(
        _request: TargetJobRequest,
        _attempt: int,
        _cancellation: threading.Event,
        _session: TargetExecutionSession,
    ) -> TargetJobStatus:
        raise failure

    service = PersistentTargetService(
        contract=target,
        operations={operation.id: operation},
        state_root=tmp_path / code,
        execute=execute,
    )
    try:
        service.put_job(request)
        deadline = time.monotonic() + 5
        status = service.get_job(request.declaration.job_id)
        while status.state not in {"inapplicable", "failed"}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
            status = service.get_job(request.declaration.job_id)
    finally:
        service.close()

    assert status.state == state
    if state == "inapplicable":
        assert status.inapplicable is not None
        assert status.inapplicable.code == code
    else:
        assert status.failure is not None
        assert (status.failure.code, status.failure.retryable) == (code, retryable)
