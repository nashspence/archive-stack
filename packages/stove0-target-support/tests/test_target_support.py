from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from riverhog_protocol.collection_workflows import (
    ArtifactDisposition,
    CollectionDerivation,
)
from riverhog_protocol.collection_workflows import (
    canonical_json_sha256 as riverhog_canonical_json_sha256,
)
from riverhog_transform_sdk import DerivedCollectionReceipt
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
from stove0_target_client import TransformTargetClient
from stove0_target_support import (
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
    TargetExecutionCanceled,
    TargetExecutionEvidence,
    TargetExecutionInapplicable,
    TargetExecutionRuntime,
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
            inputs=(InputArtifactContract(role="fixture.source/v1"),),
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
            content_etag=_sha("2"),
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
            content_etag=_sha("7"),
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

    with pytest.raises(ValidationError, match="failed target status cannot publish outputs"):
        TargetJobStatus(
            **status.model_dump(mode="python", exclude={"state", "failure"}),
            state="failed",
            failure={"code": "fixture.failure/v1", "message": "failed", "retryable": False},
        )


def test_target_runtime_builds_complete_success_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation, _target_contract, request = _request()
    expected = _success_status(operation, request)
    assert expected.derivation is not None
    derivation = CollectionDerivation.from_mapping(expected.derivation)
    output_collection = expected.output_collection
    assert output_collection is not None
    runtime = TargetExecutionRuntime(request, object())  # type: ignore[arg-type]

    def publish(*_args: object, **_kwargs: object) -> tuple[DerivedCollectionReceipt, object]:
        return (
            DerivedCollectionReceipt(
                collection_id=output_collection.collection_id,
                manifest_sha256=output_collection.manifest_sha256,
                content_etag=output_collection.content_etag,
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
        TransformTargetClient("http://target.example")
    assert TransformTargetClient("http://localhost:8000").base_url.startswith("http://")


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
