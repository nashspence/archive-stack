from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import stove0_observer_client.providers as observer_provider_module
from jsonschema import Draft202012Validator
from riverhog_protocol.collection_workflows import PRODUCER_EVIDENCE_PATH
from stove0_observer_client import (
    ContentObserverClient,
    ObserverProtocolError,
    load_semantic_validator_registry,
)
from stove0_observer_protocol import (
    JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
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
    SemanticFactsConformanceVector,
    SemanticFactsConformanceVectors,
    SemanticValidatorBinding,
    SemanticValidatorRegistry,
)
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
    SemanticValidationProfile,
    SemanticValidationProfilePayload,
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
            "archive_root_sha256": _sha("1"),
            "content_identity": _sha("2"),
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
            facts_semantics=JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
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
                        archive_root_sha256=_sha("1"),
                        content_identity=_sha("2"),
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

    def observe(
        self,
        _invocation: ObservationInvocation,
        *,
        descriptor: ObserverDescriptor,
    ) -> ObservationResult:
        assert descriptor == self._descriptor
        return self._result


def test_conformance_report_checks_contract_schemas_and_result_binding() -> None:
    api = RetrievalApi()
    contract = _contract()
    descriptor = _descriptor(contract)
    request = _request(contract, descriptor, api)
    result = _result(request, contract, descriptor, len(api.data))
    inspected = conformance_report(FixtureObserverClient(descriptor, result))
    assert inspected["status"] == "inspected"
    assert inspected["coverage"] == {"advertised": 1, "exercised": 0, "complete": False}
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
        invocations=(invocation,),
    )

    assert report["status"] == "conformant"
    assert report["coverage"] == {"advertised": 1, "exercised": 1, "complete": True}
    assert report["observations"][0]["facts"] == {"bytes": len(api.data)}
    assert report["contracts"] == [
        {
            "contract_id": contract.id,
            "contract_sha256": contract.contract_sha256,
            "options_schema_sha256": contract.options_schema.sha256,
            "facts_schema_sha256": contract.facts_schema.sha256,
            "facts_semantics_id": contract.facts_semantics.id,
            "facts_semantics_sha256": contract.facts_semantics.profile_sha256,
            "facts_semantics_conformance_vectors_sha256": None,
            "preferred_subject_batch_size": 128,
            "maximum_result_bytes": contract.maximum_result_bytes,
            "execution": "exercised",
            "semantic_conformance": "schema-only",
        }
    ]


def test_conformance_report_exercises_semantics_locally_not_as_observer_calls() -> None:
    api = RetrievalApi()
    base = _contract()
    base_descriptor = _descriptor(base)
    base_request = _request(base, base_descriptor, api)
    vectors = SemanticFactsConformanceVectors(
        profile_id="fixture.bytes-semantics/v1",
        vectors=(
            SemanticFactsConformanceVector(
                id="accepted",
                accepted=True,
                subjects=base_request.subjects,
                options=base_request.options,
                facts={"bytes": 1},
            ),
            SemanticFactsConformanceVector(
                id="rejected",
                accepted=False,
                subjects=base_request.subjects,
                options=base_request.options,
                facts={"bytes": 0},
            ),
        ),
    )
    semantics = SemanticValidationProfile.seal(
        SemanticValidationProfilePayload(
            id=vectors.profile_id,
            rules=("fixture.bytes.positive/v1",),
            conformance_vectors_sha256=vectors.sha256,
        )
    )
    payload = base.model_dump(mode="python", exclude={"contract_sha256"})
    payload["facts_semantics"] = semantics
    contract = ObserverContract.seal(ObserverContractPayload.model_validate(payload))
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
    result = _result(request, contract, descriptor, len(api.data))
    observed_calls = 0

    class CountingClient(FixtureObserverClient):
        def observe(
            self,
            invocation: ObservationInvocation,
            *,
            descriptor: ObserverDescriptor,
        ) -> ObservationResult:
            nonlocal observed_calls
            observed_calls += 1
            return super().observe(invocation, descriptor=descriptor)

    def validate_positive(
        _request: ObservationRequest,
        facts: Mapping[str, object],
    ) -> None:
        if int(facts["bytes"]) < 1:
            raise ValueError("bytes must be positive")

    report = conformance_report(
        CountingClient(descriptor, result),
        invocations=(invocation,),
        semantic_vectors=(vectors,),
        semantic_validators=SemanticValidatorRegistry(
            (SemanticValidatorBinding.from_profile(semantics, validate_positive),)
        ),
    )

    assert report["status"] == "conformant"
    assert report["contracts"][0]["semantic_conformance"] == "exercised"
    assert observed_calls == 1


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

    inapplicable = builder.inapplicable(
        code="unsupported-input",
        message="The fixture cannot inspect this input.",
    )
    assert inapplicable.state == "inapplicable"
    assert inapplicable.inapplicable is not None
    assert inapplicable.inapplicable.code == "unsupported-input"

    with pytest.raises(ValueError, match="facts violate their advertised schema"):
        builder.observed({"bytes": "not-an-integer"})


def test_observer_binding_executes_advertised_request_options_schema() -> None:
    api = RetrievalApi()
    contract = _contract()
    descriptor = _descriptor(contract)
    request = _request(contract, descriptor, api)
    invalid_request = ObservationRequest.seal(
        ObservationRequestPayload.model_validate(
            {
                **request.model_dump(mode="python", exclude={"request_id"}),
                "options": {"undeclared": True},
            }
        )
    )
    invocation = ObservationInvocation(
        request=invalid_request,
        claim_id="claim-1",
        fence=3,
        runtime=ObserverRuntimeAuthority(
            riverhog_base_url="https://riverhog.invalid",
            capability_token="secret-capability",
            workspace_assurance="ephemeral",
        ),
    )

    response = ObserverHttpBinding(BindingObserver(descriptor)).handle(
        "POST",
        "/v1/observe",
        invocation.model_dump_json(exclude_none=True).encode(),
    )

    assert response.status == 400
    assert json.loads(response.body)["error"]["code"] == "invalid_observation_request"


def test_subject_batch_preference_is_not_a_request_limit() -> None:
    api = RetrievalApi()
    contract = _contract()
    descriptor = ObserverDescriptor.seal(
        ObserverDescriptorPayload(
            implementation_id="fixture.bytes-observer/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            image_digest=_sha("9"),
            contracts=(
                ObserverContractSupport.from_contract(
                    contract,
                    preferred_subject_batch_size=2,
                ),
            ),
        )
    )
    root = CollectionRootRef(
        collection_id=1,
        archive_root_sha256=_sha("1"),
        content_identity=_sha("2"),
    )
    subjects = tuple(
        ArtifactSubject(
            id=f"source-{index}",
            role="fixture.source/v1",
            collection=root,
            path=f"camera/input-{index}.mov",
            bytes=len(api.data),
            sha256=api.sha256,
        )
        for index in range(3)
    )
    request = ObservationRequest.seal(
        ObservationRequestPayload(
            work_id=_sha("a"),
            observer_registration_id="fixture-observer",
            observer_descriptor_sha256=descriptor.descriptor_sha256,
            observer_contract_id=contract.id,
            observer_contract_sha256=contract.contract_sha256,
            subjects=subjects,
            maximum_result_bytes=4096,
        )
    )

    result = ObservationResultBuilder(descriptor, request).observed({"bytes": len(api.data)})

    assert len(result.subjects) == 3
    assert descriptor.contracts[0].preferred_subject_batch_size == 2


def test_observer_client_rejects_remote_plain_http_by_default() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        ContentObserverClient("http://observer.example")
    assert ContentObserverClient("http://127.0.0.1:8000").base_url.startswith("http://")


def test_observer_client_rejects_a_well_formed_result_for_different_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = RetrievalApi()
    contract = _contract()
    descriptor = _descriptor(contract)
    request = _request(contract, descriptor, api)
    other_request_payload = request.model_dump(
        mode="python",
        exclude={"request_id"},
    )
    other_request_payload["work_id"] = _sha("b")
    other_request = ObservationRequest.seal(
        ObservationRequestPayload.model_validate(other_request_payload)
    )
    result = _result(other_request, contract, descriptor, len(api.data))
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
    real_client = httpx.Client

    def respond(_received: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=result.model_dump(mode="json"))

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **_kwargs: real_client(transport=httpx.MockTransport(respond)),
    )

    with pytest.raises(ObserverProtocolError, match="inconsistent with the invocation"):
        ContentObserverClient("https://observer.example").observe(
            invocation,
            descriptor=descriptor,
        )


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


def test_observer_binding_and_client_execute_the_exact_semantic_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = RetrievalApi()
    base = _contract()
    semantics = SemanticValidationProfile.seal(
        SemanticValidationProfilePayload(
            id="fixture.bytes-facts-semantics/v1",
            rules=("fixture.bytes-facts.nonzero/v1",),
            conformance_vectors_sha256=_sha("e"),
        )
    )
    payload = base.model_dump(mode="python", exclude={"contract_sha256"})
    payload["facts_semantics"] = semantics
    contract = ObserverContract.seal(ObserverContractPayload.model_validate(payload))
    descriptor = _descriptor(contract)

    assert (
        ObserverHttpBinding(BindingObserver(descriptor)).handle("GET", "/v1/observer").status == 500
    )
    assert (
        ObserverHttpBinding(
            BindingObserver(descriptor),
            semantic_validators=SemanticValidatorRegistry(
                (
                    SemanticValidatorBinding(
                        profile_id=semantics.id,
                        profile_sha256="f" * 64,
                        validator=lambda _request, _facts: None,
                    ),
                )
            ),
        )
        .handle("GET", "/v1/observer")
        .status
        == 500
    )
    assert (
        ObserverHttpBinding(
            BindingObserver(descriptor),
            semantic_validators=SemanticValidatorRegistry(
                (
                    SemanticValidatorBinding(
                        profile_id="fixture.other-semantics/v1",
                        profile_sha256=semantics.profile_sha256,
                        validator=lambda _request, _facts: None,
                    ),
                )
            ),
        )
        .handle("GET", "/v1/observer")
        .status
        == 500
    )

    called: list[Mapping[str, object]] = []

    def reject_facts(
        _request: ObservationRequest,
        facts: Mapping[str, object],
    ) -> None:
        called.append(facts)
        raise ValueError("fixture semantic policy rejected the facts")

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
    binding = ObserverHttpBinding(
        BindingObserver(descriptor),
        semantic_validators=SemanticValidatorRegistry(
            (SemanticValidatorBinding.from_profile(semantics, reject_facts),)
        ),
    )

    assert binding.handle("GET", "/v1/observer").status == 200
    response = binding.handle(
        "POST",
        "/v1/observe",
        invocation.model_dump_json(exclude_none=True).encode(),
    )
    assert response.status == 500
    assert called == [{"bytes": len(api.data)}]

    result = _result(request, contract, descriptor, len(api.data))
    real_client = httpx.Client

    def respond(received: httpx.Request) -> httpx.Response:
        if received.method == "GET":
            return httpx.Response(200, json=descriptor.model_dump(mode="json"))
        return httpx.Response(200, json=result.model_dump(mode="json"))

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **_kwargs: real_client(transport=httpx.MockTransport(respond)),
    )
    with pytest.raises(ObserverProtocolError, match="not enabled"):
        ContentObserverClient("https://observer.example").descriptor()

    client = ContentObserverClient(
        "https://observer.example",
        semantic_validators=SemanticValidatorRegistry(
            (SemanticValidatorBinding.from_profile(semantics, reject_facts),)
        ),
    )
    assert client.descriptor() == descriptor
    with pytest.raises(ObserverProtocolError, match="inconsistent with the invocation"):
        client.observe(invocation, descriptor=descriptor)


def test_semantic_validator_entry_points_are_loaded_only_by_explicit_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = SemanticValidatorBinding.from_profile(
        JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
        lambda _request, _facts: None,
    )

    class FixtureEntryPoint:
        name = "fixture"

        @staticmethod
        def load() -> SemanticValidatorBinding:
            return binding

    monkeypatch.setattr(
        observer_provider_module.importlib.metadata,
        "entry_points",
        lambda *, group: (FixtureEntryPoint(),),
    )

    registry = load_semantic_validator_registry(("fixture",))
    assert registry.resolve(binding.profile_id, binding.profile_sha256) is binding.validator
    assert registry.resolve(binding.profile_id, "0" * 64) is None
    assert (
        load_semantic_validator_registry(()).resolve(binding.profile_id, binding.profile_sha256)
        is None
    )
    with pytest.raises(ValueError, match="resolve exactly once"):
        load_semantic_validator_registry(("missing",))


def test_observer_descriptor_failure_uses_the_public_error_envelope() -> None:
    class FailingDescriptorObserver:
        def descriptor(self) -> ObserverDescriptor:
            raise RuntimeError("private descriptor failure")

        def observe(
            self,
            _request: ObservationRequest,
            _runtime: ObservationRuntime,
        ) -> ObservationResult:
            raise AssertionError("descriptor endpoint must not execute observation")

    response = ObserverHttpBinding(FailingDescriptorObserver()).handle(
        "GET",
        "/v1/observer",
    )

    assert response.status == 500
    assert json.loads(response.body) == {
        "error": {
            "code": "observer_failed",
            "message": "content observer descriptor failed",
        }
    }


def test_observer_implementation_value_error_is_a_server_fault() -> None:
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

    class FaultingObserver(BindingObserver):
        def observe(
            self,
            _request: ObservationRequest,
            _runtime: ObservationRuntime,
        ) -> ObservationResult:
            raise ValueError("private observer defect")

    response = ObserverHttpBinding(FaultingObserver(descriptor)).handle(
        "POST",
        "/v1/observe",
        invocation.model_dump_json(exclude_none=True).encode(),
    )

    assert response.status == 500
    assert json.loads(response.body)["error"]["code"] == "observer_failed"
    assert b"private observer defect" not in response.body


def test_observer_binding_serializes_workspace_execution_by_default() -> None:
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
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    active_peak = 0

    class BlockingObserver(BindingObserver):
        def observe(
            self,
            observed_request: ObservationRequest,
            runtime: ObservationRuntime,
        ) -> ObservationResult:
            nonlocal active, active_peak
            with lock:
                active += 1
                active_peak = max(active_peak, active)
                entered.set()
            assert release.wait(timeout=5)
            try:
                return super().observe(observed_request, runtime)
            finally:
                with lock:
                    active -= 1

    binding = ObserverHttpBinding(BlockingObserver(descriptor))
    body = invocation.model_dump_json(exclude_none=True).encode()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(binding.handle, "POST", "/v1/observe", body)
        assert entered.wait(timeout=5)
        second = pool.submit(binding.handle, "POST", "/v1/observe", body)
        release.set()
        assert [first.result(timeout=5).status, second.result(timeout=5).status] == [200, 200]

    assert active_peak == 1


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
