from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from riverhog_protocol.collection_workflows import PRODUCER_EVIDENCE_PATH
from stove0_observer_client import ContentObserverClient
from stove0_observer_support import (
    ObservationResultBuilder,
    ObservationRuntime,
    ObserverHttpBinding,
    conformance_report,
    observer_schema_bundle,
)
from stove0_protocol import (
    ArtifactSubject,
    CollectionRootRef,
    JsonSchemaDocument,
    ObservationInvocation,
    ObservationRequest,
    ObservationRequestPayload,
    ObservationResult,
    ObservationResultPayload,
    ObserverContract,
    ObserverContractPayload,
    ObserverContractSupport,
    ObserverDescriptor,
    ObserverDescriptorPayload,
    ObserverImplementation,
    ObserverRuntimeAuthority,
    canonical_json_sha256,
)


def _sha(character: str) -> str:
    return character * 64


class RetrievalApi:
    def __init__(self) -> None:
        self.data = b"immutable observer input"
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        self.acknowledged: list[str] = []
        self.canceled: list[str] = []

    def get_collection(self, collection_id: int) -> dict[str, Any]:
        return {
            "id": collection_id,
            "manifest_sha256": _sha("1"),
            "content_etag": _sha("2"),
        }

    def search(self, _query: str | None = None, **_kwargs: Any) -> dict[str, Any]:
        return {
            "files": [
                {
                    "collection_id": 1,
                    "path": PRODUCER_EVIDENCE_PATH,
                    "bytes": 2,
                    "sha256": hashlib.sha256(b"{}").hexdigest(),
                },
                {
                    "collection_id": 1,
                    "path": "camera/input.mov",
                    "bytes": len(self.data),
                    "sha256": self.sha256,
                },
            ]
        }

    def _rows(self, files: Sequence[tuple[int, str]]) -> list[dict[str, object]]:
        return [
            {
                "collection_id": collection_id,
                "path": path,
                "bytes": len(self.data),
                "sha256": self.sha256,
            }
            for collection_id, path in files
        ]

    def plan_retrieval(
        self,
        files: Sequence[tuple[int, str]],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return {"etag": _sha("9"), "files": self._rows(files)}

    def create_retrieval_job(
        self,
        files: Sequence[tuple[int, str]],
        *,
        plan_etag: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "id": "observer-retrieval",
            "state": "ready",
            "plan_etag": plan_etag,
            "files": self._rows(files),
        }

    def get_retrieval_job(self, job_id: str) -> dict[str, Any]:
        raise AssertionError(f"unexpected retrieval poll: {job_id}")

    def renew_retrieval_job(self, job_id: str, *, lease_seconds: int) -> dict[str, Any]:
        return {"id": job_id, "state": "ready", "lease_seconds": lease_seconds}

    def acknowledge_retrieval_job(self, job_id: str) -> dict[str, Any]:
        self.acknowledged.append(job_id)
        return {"id": job_id, "state": "completed"}

    def cancel_retrieval_job(self, job_id: str) -> dict[str, Any]:
        self.canceled.append(job_id)
        return {"id": job_id, "state": "canceled"}

    def download_retrieval_file(
        self,
        _job_id: str,
        *,
        output: Path,
        **_kwargs: Any,
    ) -> int:
        output.write_bytes(self.data)
        return len(self.data)

    @contextmanager
    def stream_retrieval_file(
        self,
        _job_id: str,
        *,
        start: int = 0,
        end: int | None = None,
        **_kwargs: Any,
    ) -> Iterator[Iterator[bytes]]:
        resolved_end = len(self.data) if end is None else end
        yield iter((self.data[start:resolved_end],))


def _contract() -> ObserverContract:
    return ObserverContract.seal(
        ObserverContractPayload(
            id="fixture.bytes/v1",
            options_schema=JsonSchemaDocument.from_schema(
                "fixture.bytes-options/v1",
                {"type": "object", "additionalProperties": False},
            ),
            facts_schema=JsonSchemaDocument.from_schema(
                "fixture.bytes-facts/v1",
                {
                    "type": "object",
                    "properties": {"bytes": {"type": "integer"}},
                    "required": ["bytes"],
                    "additionalProperties": False,
                },
            ),
            maximum_subjects=2,
            maximum_result_bytes=4096,
        )
    )


def _descriptor(contract: ObserverContract) -> ObserverDescriptor:
    return ObserverDescriptor.seal(
        ObserverDescriptorPayload(
            implementation_id="fixture.bytes-observer/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            image_digest=_sha("9"),
            contracts=(ObserverContractSupport.from_contract(contract),),
        )
    )


def _request(
    contract: ObserverContract,
    descriptor: ObserverDescriptor,
    api: RetrievalApi,
) -> ObservationRequest:
    return ObservationRequest.seal(
        ObservationRequestPayload(
            work_id=_sha("a"),
            observer_registration_id="fixture-observer",
            observer_descriptor_sha256=descriptor.descriptor_sha256,
            observer_contract_id=contract.id,
            observer_contract_sha256=contract.contract_sha256,
            subjects=(
                ArtifactSubject(
                    id="source",
                    role="fixture.source/v1",
                    collection=CollectionRootRef(
                        collection_id=1,
                        manifest_sha256=_sha("1"),
                        content_etag=_sha("2"),
                    ),
                    path="camera/input.mov",
                    bytes=len(api.data),
                    sha256=api.sha256,
                ),
            ),
            options={},
            timeout_seconds=30,
            maximum_result_bytes=4096,
        )
    )


def _result(
    request: ObservationRequest,
    contract: ObserverContract,
    descriptor: ObserverDescriptor,
    byte_count: int,
) -> ObservationResult:
    facts = {"bytes": byte_count}
    return ObservationResult.seal(
        ObservationResultPayload(
            request_id=request.request_id,
            state="observed",
            observer=ObserverImplementation(
                id=descriptor.implementation_id,
                version=descriptor.implementation_version,
                source_revision=descriptor.source_revision,
                descriptor_sha256=descriptor.descriptor_sha256,
            ),
            observer_contract_id=contract.id,
            observer_contract_sha256=contract.contract_sha256,
            subjects=request.subjects,
            facts_schema=contract.facts_schema,
            facts=facts,
            facts_sha256=canonical_json_sha256(facts),
            execution_evidence={"reader": "fixture"},
        )
    )


def test_observation_runtime_exposes_only_exact_requested_artifacts(tmp_path: Path) -> None:
    api = RetrievalApi()
    contract = _contract()
    descriptor = _descriptor(contract)
    request = _request(contract, descriptor, api)

    with ObservationRuntime(
        api,
        request=request,
        claim_id="claim-1",
        fence=3,
    ) as runtime:  # type: ignore[arg-type]
        resolved = runtime.subjects()
        assert [(subject.id, artifact.path) for subject, artifact in resolved] == [
            ("source", "camera/input.mov")
        ]
        assert runtime.read_bytes(request.subjects[0], maximum_bytes=1024) == api.data
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir(mode=0o700)
        workspace = runtime.open_workspace(workspace_root)
        materialized = runtime.materialize(request.subjects[0], workspace=workspace)
        assert materialized.read_bytes() == api.data
        workspace.release()

    assert api.acknowledged == ["observer-retrieval", "observer-retrieval"]


class FixtureObserverClient:
    def __init__(
        self,
        descriptor: ObserverDescriptor,
        result: ObservationResult,
    ) -> None:
        self._descriptor = descriptor
        self._result = result

    def descriptor(self) -> ObserverDescriptor:
        return self._descriptor

    def observe(self, _invocation: ObservationInvocation) -> ObservationResult:
        return self._result


def test_conformance_report_checks_contract_schemas_and_result_binding() -> None:
    api = RetrievalApi()
    contract = _contract()
    descriptor = _descriptor(contract)
    request = _request(contract, descriptor, api)
    result = _result(request, contract, descriptor, len(api.data))
    invocation = ObservationInvocation(
        request=request,
        claim_id="claim-1",
        fence=3,
        runtime=ObserverRuntimeAuthority(
            riverhog_base_url="https://riverhog.invalid",
            capability_token="secret-capability",
            workspace_assurance="ephemeral",
        ),
    )

    report = conformance_report(
        FixtureObserverClient(descriptor, result),
        invocation=invocation,
    )

    assert report["status"] == "conformant"
    assert report["observation"]["facts"] == {"bytes": len(api.data)}


def test_result_builder_binds_schema_identity_and_size_limits() -> None:
    api = RetrievalApi()
    contract = _contract()
    descriptor = _descriptor(contract)
    request = _request(contract, descriptor, api)
    builder = ObservationResultBuilder(descriptor, request)

    result = builder.observed(
        {"bytes": len(api.data)},
        execution_evidence={"reader": "fixture"},
    )
    assert result.facts == {"bytes": len(api.data)}
    assert result.observer.descriptor_sha256 == descriptor.descriptor_sha256

    failed = builder.failed(
        code="fixture.read-failed/v1",
        message="fixture failure",
        retryable=True,
    )
    assert failed.state == "failed"
    assert failed.failure is not None and failed.failure.retryable is True


def test_observer_client_rejects_remote_plain_http_by_default() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        ContentObserverClient("http://observer.example")
    assert ContentObserverClient("http://127.0.0.1:8000").base_url.startswith("http://")


class BindingObserver:
    def __init__(self, descriptor: ObserverDescriptor) -> None:
        self._descriptor = descriptor

    def descriptor(self) -> ObserverDescriptor:
        return self._descriptor

    def observe(
        self,
        request: ObservationRequest,
        _runtime: ObservationRuntime,
    ) -> ObservationResult:
        return ObservationResultBuilder(self._descriptor, request).observed(
            {"bytes": request.subjects[0].bytes},
            execution_evidence={"implementation": "fixture"},
        )


def test_framework_neutral_observer_http_binding() -> None:
    api = RetrievalApi()
    contract = _contract()
    descriptor = _descriptor(contract)
    request = _request(contract, descriptor, api)
    invocation = ObservationInvocation(
        request=request,
        claim_id="claim-1",
        fence=3,
        runtime=ObserverRuntimeAuthority(
            riverhog_base_url="https://riverhog.invalid",
            capability_token="secret-capability",
            workspace_assurance="ephemeral",
        ),
    )
    binding = ObserverHttpBinding(BindingObserver(descriptor))

    contract_response = binding.handle("GET", "/v1/observer")
    assert contract_response.status == 200
    assert ObserverDescriptor.model_validate_json(contract_response.body) == descriptor

    result_response = binding.handle(
        "POST",
        "/v1/observe",
        invocation.model_dump_json(exclude_none=True).encode(),
    )
    assert result_response.status == 200
    result = ObservationResult.model_validate_json(result_response.body)
    assert result.facts == {"bytes": len(api.data)}
    assert binding.handle("DELETE", "/v1/observer").status == 405


def test_observer_schema_bundle_is_deterministic_and_self_validating() -> None:
    first = observer_schema_bundle()
    second = observer_schema_bundle()
    assert first == second
    digest = first.pop("bundle_sha256")
    assert canonical_json_sha256(first) == digest
    assert first["http_binding"]["POST /v1/observe"] == {
        "request": "ObservationInvocation",
        "response": "ObservationResult",
    }
    for schema in first["schemas"].values():
        Draft202012Validator.check_schema(schema)
