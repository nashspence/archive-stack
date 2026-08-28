from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from http_api_contracts import (
    http_operation_for_request,
    http_operation_inventory,
    operation_openapi,
)
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError
from riverhog_api_client import ProducerArtifactCustody, ProducerArtifactIdentity, ProducerFile
from riverhog_protocol import (
    CollectionUploadArtifactCustodyReceiptDocument,
    CollectionUploadCustodyObjectDocument,
    Conflict,
    DownloadAllowanceExceeded,
    Unauthorized,
)
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
    SemanticValidationProfile,
    SemanticValidationProfilePayload,
    TargetPlanBinding,
    WorkflowPlan,
    WorkflowPlanPayload,
    WorkIdentity,
    WorkPayload,
)
from stove0_target_client import TargetClient, TargetProtocolError
from stove0_target_protocol import (
    SemanticIntentConformanceVector,
    SemanticIntentConformanceVectors,
)
from stove0_target_support import (
    DEFAULT_TERMINAL_STATE_RETENTION_SECONDS,
    EFFECT_TARGET_PROTOCOL,
    JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
    TARGET_HTTP_OPERATIONS,
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
    TargetCollectionPublication,
    TargetConformanceCase,
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
    TargetServiceError,
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
            intent_semantics=JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
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
            archive_root_sha256=_sha("1"),
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


def _request_for(
    operation: OperationContract,
    target: TargetContract,
) -> TargetJobRequest:
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
    return request


def _request() -> tuple[OperationContract, TargetContract, TargetJobRequest]:
    operation = _operation()
    target = _target(operation)
    return operation, target, _request_for(operation, target)


def _effect_request() -> tuple[OperationContract, TargetContract, TargetJobRequest]:
    operation = OperationContract.seal(
        OperationContractPayload(
            id="fixture.record-index/v1",
            result_kind="external-effect",
            intent_schema=JsonSchemaDocument.from_schema(
                "fixture.record-index-intent/v1",
                {"type": "object", "additionalProperties": False},
            ),
            intent_semantics=JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
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
                input_archive_root_sha256=_input().collection.archive_root_sha256,
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
            archive_root_sha256=_sha("6"),
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

    running_with_failure = {
        "job_id": request.declaration.job_id,
        "state": "running",
        "attempt": 1,
        "request_sha256": request.request_sha256,
        "plan_sha256": request.declaration.plan.plan_sha256,
        "progress": {"phase": "running", "completed": 0},
        "failure": {"code": "fixture.failure/v1", "message": "failed", "retryable": False},
    }
    with pytest.raises(ValidationError, match="nonterminal target status"):
        TargetJobStatus.model_validate(running_with_failure)
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(TargetJobStatus.model_json_schema()).validate(running_with_failure)


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
                archive_root_sha256=output_collection.archive_root_sha256,
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


def test_incremental_publication_releases_local_output_only_after_exact_custody(
    tmp_path: Path,
) -> None:
    content = b"completed target output"
    local = tmp_path / "result.bin"
    local.write_bytes(content)
    output = OutputArtifact(
        id="output",
        role="fixture.output/v1",
        path="output/result.bin",
        bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        derived_from=("source",),
    )

    class Writer:
        custody_receipts: dict[str, ProducerArtifactCustody] = {}

    writer = Writer()

    class Runtime:
        def append_incremental_output(
            self,
            _writer: object,
            _source: object,
            *,
            identity: ProducerArtifactIdentity,
            sources: object,
        ) -> tuple[ProducerArtifactCustody, ...]:
            assert sources
            assert local.exists()
            receipt = ProducerArtifactCustody(
                identity,
                CollectionUploadArtifactCustodyReceiptDocument.seal(
                    collection_id=7,
                    path=identity.path,
                    bytes=identity.bytes,
                    sha256=identity.sha256,
                    archive_objects=(
                        CollectionUploadCustodyObjectDocument(
                            volume_id="pack-000000000000",
                            sealed_receipt_sha256=_sha("a"),
                        ),
                    ),
                ),
            )
            writer.custody_receipts[identity.path] = receipt
            return (receipt,)

    class Execution:
        runtime = Runtime()

        def inputs(self) -> tuple[tuple[InputArtifact, object], ...]:
            return ((_input(), object()),)

    publication = TargetCollectionPublication(Execution(), writer)  # type: ignore[arg-type]
    custody = publication.append(ProducerFile(local, output.path), output)

    assert custody[0].artifact == ProducerArtifactIdentity(
        output.path,
        output.bytes,
        output.sha256,
    )
    assert not local.exists()


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

    def put_job(
        self,
        _request: TargetJobRequest,
        *,
        operation: OperationContract,
    ) -> TargetJobStatus:
        assert operation.id == self.request.declaration.plan.operation_id
        return self.status_value

    def status(
        self,
        request: TargetJobRequest,
        *,
        operation: OperationContract,
    ) -> TargetJobStatus:
        assert request == self.request
        assert operation.id == self.request.declaration.plan.operation_id
        return self.status_value


def test_conformance_report_proves_preflight_and_idempotent_submission() -> None:
    operation, target, request = _request()
    status = _success_status(operation, request)
    report = conformance_report(
        FixtureTargetClient(target, request, status),
        cases=(TargetConformanceCase(operation=operation, job_request=request),),
    )
    assert report.format == "stove0-target-conformance-result/v1"
    assert report.status == "conformant"
    assert report.target.transport == "riverhog-capability/v1"
    assert report.coverage.model_dump() == {"advertised": 1, "exercised": 1, "complete": True}
    assert report.operation_evidence[0].semantic_conformance.status == "schema-only"
    assert report.operations[0].semantic_conformance == "schema-only"


def test_conformance_report_uses_one_exact_target_contract_snapshot() -> None:
    operation, target, request = _request()
    status = _success_status(operation, request)

    class SnapshotClient(FixtureTargetClient):
        def __init__(self) -> None:
            super().__init__(target, request, status)
            self.contract_calls = 0

        def contract(self) -> TargetContract:
            self.contract_calls += 1
            if self.contract_calls > 1:
                raise AssertionError("conformance report reread its target contract")
            return super().contract()

    client = SnapshotClient()
    report = conformance_report(
        client,
        cases=(TargetConformanceCase(operation=operation, job_request=request),),
    )

    assert report.target.contract_sha256 == target.contract_sha256
    assert report.coverage.model_dump() == {"advertised": 1, "exercised": 1, "complete": True}
    assert client.contract_calls == 1


def test_contract_only_target_report_does_not_claim_execution_conformance() -> None:
    operation, target, request = _request()
    report = conformance_report(
        FixtureTargetClient(target, request, _success_status(operation, request))
    )

    assert report.status == "inspected"
    assert report.coverage.model_dump() == {"advertised": 1, "exercised": 0, "complete": False}
    assert report.operations[0].semantic_conformance == "not-exercised"


def test_target_conformance_requires_every_advertised_operation() -> None:
    first = _operation()
    second_payload = first.model_dump(mode="python", exclude={"contract_sha256"})
    second_payload["id"] = "fixture.second/v1"
    second = OperationContract.seal(OperationContractPayload.model_validate(second_payload))
    options = JsonSchemaDocument.from_schema(
        "fixture.multi-target-options/v1",
        {"type": "object", "additionalProperties": False},
    )
    target = TargetContract.seal(
        TargetContractPayload(
            implementation_id="fixture.multi-target/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            image_digest=_sha("9"),
            operations=tuple(
                TargetOperationSupport(
                    operation_id=operation.id,
                    operation_contract_sha256=operation.contract_sha256,
                    options_schema=options,
                )
                for operation in (first, second)
            ),
        )
    )
    requests = {operation.id: _request_for(operation, target) for operation in (first, second)}
    statuses = {
        operation.id: _success_status(operation, requests[operation.id])
        for operation in (first, second)
    }

    class MultiOperationClient:
        def contract(self) -> TargetContract:
            return target

        def preflight(self, request: TargetPreflightRequest) -> TargetPreflightResponse:
            return TargetPreflightResponse(
                target=target,
                plan=requests[request.operation_id].declaration.plan,
            )

        def put_job(
            self,
            request: TargetJobRequest,
            *,
            operation: OperationContract,
        ) -> TargetJobStatus:
            assert request == requests[operation.id]
            return statuses[operation.id]

        def status(
            self,
            request: TargetJobRequest,
            *,
            operation: OperationContract,
        ) -> TargetJobStatus:
            return self.put_job(request, operation=operation)

    client = MultiOperationClient()
    partial = conformance_report(
        client,
        cases=(TargetConformanceCase(operation=first, job_request=requests[first.id]),),
    )
    assert partial.status == "partially-exercised"
    assert partial.coverage.model_dump() == {"advertised": 2, "exercised": 1, "complete": False}

    complete = conformance_report(
        client,
        cases=tuple(
            TargetConformanceCase(operation=operation, job_request=requests[operation.id])
            for operation in (first, second)
        ),
    )
    assert complete.status == "conformant"
    assert complete.coverage.model_dump() == {"advertised": 2, "exercised": 2, "complete": True}


def test_target_conformance_executes_the_exact_advertised_semantic_vectors() -> None:
    vectors = SemanticIntentConformanceVectors(
        profile_id="fixture.copy-intent-semantics/v1",
        vectors=(
            SemanticIntentConformanceVector(
                id="accepted",
                accepted=True,
                intent={"suffix": ".accepted"},
            ),
            SemanticIntentConformanceVector(
                id="rejected",
                accepted=False,
                intent={"suffix": ".rejected"},
            ),
        ),
    )
    base = _operation()
    semantics = SemanticValidationProfile.seal(
        SemanticValidationProfilePayload(
            id=vectors.profile_id,
            rules=("fixture.copy-intent.suffix-policy/v1",),
            conformance_vectors_sha256=vectors.sha256,
        )
    )
    payload = base.model_dump(mode="python", exclude={"contract_sha256"})
    payload["intent_semantics"] = semantics
    operation = OperationContract.seal(OperationContractPayload.model_validate(payload))
    target = _target(operation)
    request = _request_for(operation, target)
    status = _success_status(operation, request)

    class SemanticClient(FixtureTargetClient):
        def __init__(self) -> None:
            super().__init__(target, request, status)
            self.preflight_intents: list[dict[str, object]] = []
            self.job_submissions = 0

        def preflight(
            self,
            received: TargetPreflightRequest,
        ) -> TargetPreflightResponse:
            self.preflight_intents.append(dict(received.intent))
            if received.intent["suffix"] == ".rejected":
                raise TargetProtocolError(
                    "fixture semantic rejection",
                    failure_kind="remote_rejection",
                    code="invalid_target_request",
                    observed_status=400,
                )
            plan = TransformPlan.seal(
                TransformPlanPayload(
                    target_implementation_id=target.implementation_id,
                    target_contract_sha256=target.contract_sha256,
                    operation_id=received.operation_id,
                    operation_contract_sha256=received.operation_contract_sha256,
                    inputs=received.inputs,
                    intent=received.intent,
                    target_options=received.target_options,
                    observation_result_sha256s=tuple(
                        sorted(item.result.result_sha256 for item in received.observations)
                    ),
                )
            )
            return TargetPreflightResponse(target=target, plan=plan)

        def put_job(
            self,
            received: TargetJobRequest,
            *,
            operation: OperationContract,
        ) -> TargetJobStatus:
            self.job_submissions += 1
            return super().put_job(received, operation=operation)

    client = SemanticClient()
    report = conformance_report(
        client,
        cases=(
            TargetConformanceCase(
                operation=operation,
                job_request=request,
                semantic_vectors=vectors,
            ),
        ),
    )

    assert report.status == "conformant"
    assert report.operation_evidence[0].semantic_conformance.model_dump(mode="json") == {
        "profile_id": semantics.id,
        "profile_sha256": semantics.profile_sha256,
        "conformance_vectors_sha256": vectors.sha256,
        "accepted_vector_ids": ["accepted"],
        "rejected_vector_ids": ["rejected"],
        "status": "exercised",
    }
    assert client.preflight_intents == [
        {"suffix": ".accepted"},
        {"suffix": ".rejected"},
        {"suffix": ".copy"},
    ]
    assert client.job_submissions == 2


def test_target_conformance_requires_semantic_vectors_before_any_job() -> None:
    vectors = SemanticIntentConformanceVectors(
        profile_id="fixture.copy-intent-semantics/v1",
        vectors=(
            SemanticIntentConformanceVector(
                id="accepted",
                accepted=True,
                intent={"suffix": ".copy"},
            ),
            SemanticIntentConformanceVector(
                id="rejected",
                accepted=False,
                intent={"suffix": ".rejected"},
            ),
        ),
    )
    base = _operation()
    payload = base.model_dump(mode="python", exclude={"contract_sha256"})
    payload["intent_semantics"] = SemanticValidationProfile.seal(
        SemanticValidationProfilePayload(
            id=vectors.profile_id,
            rules=("fixture.copy-intent.suffix-policy/v1",),
            conformance_vectors_sha256=vectors.sha256,
        )
    )
    operation = OperationContract.seal(OperationContractPayload.model_validate(payload))
    target = _target(operation)
    request = _request_for(operation, target)
    client = FixtureTargetClient(target, request, _success_status(operation, request))

    with pytest.raises(ValueError, match="requires its exact semantic conformance vectors"):
        conformance_report(
            client,
            cases=(TargetConformanceCase(operation=operation, job_request=request),),
        )


def test_target_client_rejects_remote_plain_http_by_default() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        TargetClient("http://target.example")
    assert TargetClient("http://localhost:8000").base_url.startswith("http://")


def test_target_client_rejects_noncanonical_job_ids_before_transport() -> None:
    operation, _target, _job_request = _request()
    client = TargetClient("https://target.example")
    with pytest.raises(ValueError, match="accepted-request context"):
        client.status("not-a-job-id", operation=operation)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="accepted-request context"):
        client.cancel("A" * 64, operation=operation)  # type: ignore[arg-type]


def test_target_client_sends_cancellation_without_a_request_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation, _target, request = _request()
    expected = _success_status(operation, request)
    real_client = httpx.Client

    def respond(received: httpx.Request) -> httpx.Response:
        assert received.method == "POST"
        assert received.url.path == f"/v1/jobs/{request.declaration.job_id}/cancel"
        assert received.content == b""
        return httpx.Response(200, json=expected.model_dump(mode="json"))

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **_kwargs: real_client(transport=httpx.MockTransport(respond)),
    )

    assert TargetClient("https://target.example").cancel(request, operation=operation) == expected


def test_target_client_rejects_a_well_formed_status_for_different_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation, _target, request = _request()
    mismatched = {
        **_success_status(operation, request).model_dump(mode="json"),
        "request_sha256": _sha("f"),
    }
    real_client = httpx.Client

    def respond(_received: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mismatched)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **_kwargs: real_client(transport=httpx.MockTransport(respond)),
    )

    with pytest.raises(TargetProtocolError, match="inconsistent with the request"):
        TargetClient("https://target.example").put_job(request, operation=operation)


def test_target_http_operations_publish_exact_job_paths_and_empty_cancel() -> None:
    jobs = [operation for operation in TARGET_HTTP_OPERATIONS if "{job_id}" in operation.path]
    assert len(jobs) == 3
    for operation in jobs:
        extra = operation_openapi(operation)["openapi_extra"]
        assert extra["parameters"] == [
            {
                "name": "job_id",
                "in": "path",
                "required": True,
                "schema": {"pattern": "^[0-9a-f]{64}$", "type": "string"},
            }
        ]
    cancel = next(operation for operation in jobs if operation.path.endswith("/cancel"))
    assert "requestBody" not in operation_openapi(cancel)["openapi_extra"]


def test_operation_contract_rejects_unpermitted_input_disposition() -> None:
    operation, _target_contract, request = _request()
    status = _success_status(operation, request)
    assert status.derivation is not None
    assert status.output_collection is not None
    derivation = CollectionDerivation.from_mapping(status.derivation)
    rejected = ArtifactDisposition(
        input_collection_id=derivation.dispositions[0].input_collection_id,
        input_archive_root_sha256=derivation.dispositions[0].input_archive_root_sha256,
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

    def cancel_job(self, job_id: str) -> TargetJobStatus:
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
    assert (
        binding.handle(
            "POST",
            f"/v1/jobs/{request.declaration.job_id}/cancel",
            b'{"reason":"discarded"}',
        ).status
        == 400
    )
    assert binding.handle("PATCH", "/v1/target").status == 405


def test_target_http_binding_treats_untyped_service_schema_faults_as_server_faults() -> None:
    operation, target, request = _request()
    status = _success_status(operation, request)

    class SchemaRejectingService(BindingTargetService):
        def preflight(self, request: TargetPreflightRequest) -> TargetPreflightResponse:
            Draft202012Validator({"type": "integer"}).validate(request.intent)
            raise AssertionError("schema rejection must stop preflight")

    binding = TargetHttpBinding(SchemaRejectingService(target, request, status))
    preflight_request = TargetPreflightRequest(
        operation_id=operation.id,
        operation_contract_sha256=operation.contract_sha256,
        inputs=request.declaration.plan.inputs,
        intent=request.declaration.plan.intent,
        target_options=request.declaration.plan.target_options,
    )

    response = binding.handle(
        "POST",
        "/v1/preflight",
        preflight_request.model_dump_json(exclude_none=True).encode(),
    )

    assert response.status == 500
    assert b'"code":"target_failed"' in response.body


def test_target_operation_matching_enforces_the_declared_job_identity() -> None:
    valid_path = "/v1/jobs/" + _sha("a")

    assert http_operation_for_request(TARGET_HTTP_OPERATIONS, "GET", valid_path) is not None
    assert (
        http_operation_for_request(
            TARGET_HTTP_OPERATIONS,
            "GET",
            "/v1/jobs/not-a-sha",
        )
        is None
    )

    operation, target, request = _request()
    status = _success_status(operation, request)
    response = TargetHttpBinding(BindingTargetService(target, request, status)).handle(
        "GET",
        "/v1/jobs/not-a-sha",
    )
    assert response.status == 404
    assert b'"code":"not_found"' in response.body


def test_persistent_target_requires_and_executes_advertised_semantic_validation(
    tmp_path: Path,
) -> None:
    base = _operation()
    semantics = SemanticValidationProfile.seal(
        SemanticValidationProfilePayload(
            id="fixture.copy-intent-semantics/v1",
            rules=("fixture.copy-intent.suffix-policy/v1",),
            conformance_vectors_sha256=_sha("e"),
        )
    )
    payload = base.model_dump(mode="python", exclude={"contract_sha256"})
    payload["intent_semantics"] = semantics
    operation = OperationContract.seal(OperationContractPayload.model_validate(payload))
    target = _target(operation)

    with pytest.raises(ValueError, match="semantic validators"):
        PersistentTargetService(
            contract=target,
            operations={operation.id: operation},
            state_root=tmp_path / "missing-validator",
            execute=lambda *_args: pytest.fail("target execution must not start"),
        )

    def validate_intent(intent: Mapping[str, object]) -> None:
        if intent.get("suffix") != ".accepted":
            raise ValueError("fixture suffix policy rejected the intent")

    service = PersistentTargetService(
        contract=target,
        operations={operation.id: operation},
        state_root=tmp_path / "validated",
        execute=lambda *_args: pytest.fail("target execution must not start"),
        intent_semantic_validators={operation.intent_semantics.profile_sha256: validate_intent},
    )
    try:
        with pytest.raises(TargetServiceError, match="suffix policy"):
            service.preflight(
                TargetPreflightRequest(
                    operation_id=operation.id,
                    operation_contract_sha256=operation.contract_sha256,
                    inputs=(_input(),),
                    intent={"suffix": ".rejected"},
                    target_options={},
                )
            )
    finally:
        service.close()


def test_persistent_target_service_uses_canonical_public_error_codes(tmp_path: Path) -> None:
    operation, target, request = _request()
    status = _success_status(operation, request)
    service = PersistentTargetService(
        contract=target,
        operations={operation.id: operation},
        state_root=tmp_path / "target-state",
        execute=lambda *_args: status,
    )
    preflight = TargetPreflightRequest(
        operation_id=operation.id,
        operation_contract_sha256=operation.contract_sha256,
        inputs=request.declaration.plan.inputs,
        intent=request.declaration.plan.intent,
        target_options=request.declaration.plan.target_options,
    )
    try:
        with pytest.raises(TargetServiceError) as protocol_error:
            service.preflight(preflight.model_copy(update={"protocol": EFFECT_TARGET_PROTOCOL}))
        with pytest.raises(TargetServiceError) as contract_error:
            service.preflight(preflight.model_copy(update={"operation_contract_sha256": _sha("0")}))
        with pytest.raises(TargetServiceError) as operation_error:
            service.preflight(preflight.model_copy(update={"operation_id": "unknown/v1"}))
        with pytest.raises(TargetServiceError) as request_error:
            service.preflight(preflight.model_copy(update={"intent": {}}))
        with pytest.raises(TargetServiceError) as absence_error:
            service.get_job(_sha("0"))
    finally:
        service.close()

    assert {
        protocol_error.value.code,
        contract_error.value.code,
        operation_error.value.code,
        request_error.value.code,
        absence_error.value.code,
    } == {
        "target_protocol_mismatch",
        "operation_contract_mismatch",
        "unsupported_operation",
        "job_not_found",
        "invalid_target_request",
    }


def test_target_schema_bundle_is_deterministic_and_self_validating() -> None:
    first = target_schema_bundle()
    second = target_schema_bundle()
    assert first == second
    digest = first.pop("bundle_sha256")
    assert canonical_json_sha256(first) == digest
    assert first["http_binding"]["operations"] == http_operation_inventory(TARGET_HTTP_OPERATIONS)
    assert first["authorities"] == {
        "structural_models": "schemas",
        "http_operations": "http_binding.operations",
        "semantic_acceptance": "semantic_acceptance",
    }
    assert first["schemas"]["TargetConformanceResult"]["properties"]["format"]["const"] == (
        "stove0-target-conformance-result/v1"
    )
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
        assert canceled.cancel_job(request.declaration.job_id).state == "canceling"
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
        assert service.cancel_job(request.declaration.job_id) == interrupted
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
        assert service.cancel_job(request.declaration.job_id).state == "canceling"
        release.set()
        deadline = time.monotonic() + 5
        while service.get_job(request.declaration.job_id).state != "succeeded":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert service.cancel_job(request.declaration.job_id) == success
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
        (
            Unauthorized("expired capability", observed_status=401),
            "failed",
            "target-authorization",
            True,
        ),
        (
            Conflict("stale fence", observed_status=409),
            "failed",
            "target-conflict",
            True,
        ),
        (
            DownloadAllowanceExceeded("quota resets later", observed_status=429),
            "failed",
            "target-download-allowance",
            True,
        ),
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
