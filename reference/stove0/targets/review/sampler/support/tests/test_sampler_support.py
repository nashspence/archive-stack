from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from http_api_contracts import canonical_json_bytes, http_operation_inventory
from pydantic import ValidationError
from stove0_protocol import JsonSchemaDocument
from stove0_review_sampler_client import ReviewSamplerClient
from stove0_review_sampler_protocol import (
    SAMPLER_HTTP_OPERATIONS,
    SamplerDescriptor,
    SamplerDescriptorPayload,
    SamplerFailure,
    SamplerInapplicable,
    SamplerInput,
    SamplerOutput,
    SamplerRequest,
    SamplerRequestPayload,
    SamplerResult,
    SamplerResultPayload,
    SamplerWindow,
    validate_result,
)
from stove0_review_sampler_support import (
    SamplerHttpBinding,
    SamplerWorkspace,
    conformance_report,
    sampler_schema_bundle,
)


def _sha(character: str) -> str:
    return character * 64


def _descriptor() -> SamplerDescriptor:
    return SamplerDescriptor.seal(
        SamplerDescriptorPayload(
            implementation_id="fixture.opus-review-sampler/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            image_digest=_sha("9"),
            primary_operation_id="fixture.opus/v1",
            primary_operation_contract_sha256=_sha("8"),
            portable_intent_schema=JsonSchemaDocument.from_schema(
                "fixture.opus-intent/v1",
                {
                    "type": "object",
                    "properties": {"bitrate": {"type": "integer"}},
                    "required": ["bitrate"],
                    "additionalProperties": False,
                },
            ),
            output_role="fixture.review-audio/v1",
        )
    )


def _request(descriptor: SamplerDescriptor, payload: bytes = b"source") -> SamplerRequest:
    return SamplerRequest.seal(
        SamplerRequestPayload(
            sampler_descriptor_sha256=descriptor.descriptor_sha256,
            workspace_id=_sha("7"),
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
            portable_intent={"bitrate": 96},
            maximum_output_bytes=1024,
            timeout_seconds=30,
            cancellation_path="control/cancel",
        )
    )


def _result(descriptor: SamplerDescriptor, request: SamplerRequest) -> SamplerResult:
    return SamplerResult.seal(
        SamplerResultPayload(
            request_sha256=request.request_sha256,
            sampler_descriptor_sha256=descriptor.descriptor_sha256,
            state="succeeded",
            outputs=(
                SamplerOutput(
                    id="sample-0001",
                    path="output/review/sample.opus",
                    bytes=6,
                    sha256=hashlib.sha256(b"sample").hexdigest(),
                    media_type="audio/ogg",
                    derived_from=("source",),
                ),
            ),
            execution_evidence={"image_digest": descriptor.image_digest},
        )
    )


def test_sampler_protocol_does_not_reject_large_declared_review_work() -> None:
    descriptor = _descriptor()
    inputs = tuple(
        SamplerInput(
            id=f"source-{index:03d}",
            path=f"input/source-{index:03d}.wav",
            bytes=1,
            sha256=_sha("a"),
            media_type="audio/wav",
        )
        for index in range(257)
    )
    request = SamplerRequest.seal(
        SamplerRequestPayload(
            sampler_descriptor_sha256=descriptor.descriptor_sha256,
            workspace_id=_sha("7"),
            inputs=inputs,
            windows=tuple(
                SamplerWindow(
                    id=f"sample-{index:03d}",
                    input_id=item.id,
                    start_ms=0,
                    duration_ms=2 * 60 * 60 * 1000 if index == 256 else 100,
                    output_path=f"output/review/sample-{index:03d}.opus",
                )
                for index, item in enumerate(inputs)
            ),
            portable_intent={"bitrate": 96},
            maximum_output_bytes=1024**3,
            timeout_seconds=30,
            cancellation_path="control/cancel",
        )
    )
    output = SamplerOutput(
        id="sample-combined",
        path="output/review/combined.opus",
        bytes=1,
        sha256=_sha("b"),
        media_type="audio/ogg",
        derived_from=tuple(item.id for item in inputs),
    )

    assert len(request.inputs) == 257
    assert len(request.windows) == 257
    assert request.windows[-1].duration_ms == 7_200_000
    assert len(output.derived_from) == 257


class FixtureSampler:
    def __init__(self) -> None:
        self.value = _descriptor()

    def descriptor(self) -> SamplerDescriptor:
        return self.value

    def sample(self, request: SamplerRequest) -> SamplerResult:
        return _result(self.value, request)


def test_binding_client_and_conformance_share_the_exact_two_endpoint_contract() -> None:
    sampler = FixtureSampler()
    request = _request(sampler.descriptor())
    binding = SamplerHttpBinding(sampler)

    def transport(raw: httpx.Request) -> httpx.Response:
        response = binding.handle(raw.method, raw.url.path, raw.content)
        return httpx.Response(
            response.status,
            content=response.body,
            headers=dict(response.headers),
        )

    with ReviewSamplerClient(
        "http://sampler.test",
        "secret",
        allow_insecure_http=True,
        transport=httpx.MockTransport(transport),
    ) as client:
        inspected = conformance_report(client)
        report = conformance_report(client, request=request)

    assert inspected.format == "stove0-review-sampler-conformance-result/v1"
    assert inspected.status == "inspected"
    assert inspected.coverage.model_dump() == {"advertised": 1, "exercised": 0, "complete": False}
    assert report.status == "conformant"
    assert report.coverage.model_dump() == {"advertised": 1, "exercised": 1, "complete": True}
    assert report.sampler.image_digest == _sha("9")
    assert report.request == request
    assert report.sample is not None and report.sample.state == "succeeded"
    changed = report.model_dump(mode="json")
    changed.pop("request")
    with pytest.raises(ValidationError, match="inconsistent"):
        type(report).model_validate(changed)
    bundle = sampler_schema_bundle()
    second = sampler_schema_bundle()
    assert bundle == second
    digest = bundle.pop("bundle_sha256")
    assert hashlib.sha256(canonical_json_bytes(bundle)).hexdigest() == digest
    assert bundle["http_binding"]["operations"] == http_operation_inventory(SAMPLER_HTTP_OPERATIONS)
    assert bundle["semantic_acceptance"] == {
        "kind": "request-bound-result",
        "validator": "validate_result",
    }
    assert bundle["schemas"]["SamplerConformanceResult"]["properties"]["format"]["const"] == (
        "stove0-review-sampler-conformance-result/v1"
    )
    referenced = {
        value
        for operation in bundle["http_binding"]["operations"]
        for value in (
            operation["request"]["schema"],
            operation["response"]["schema"],
            operation["error_schema"],
        )
        if value is not None
    }
    assert referenced <= set(bundle["schemas"])
    assert "ErrorResponse" in referenced


def test_sampler_binding_serializes_workspace_execution_by_default() -> None:
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    active_peak = 0

    class BlockingSampler(FixtureSampler):
        def sample(self, request: SamplerRequest) -> SamplerResult:
            nonlocal active, active_peak
            with lock:
                active += 1
                active_peak = max(active_peak, active)
                entered.set()
            assert release.wait(timeout=5)
            try:
                return super().sample(request)
            finally:
                with lock:
                    active -= 1

    sampler = BlockingSampler()
    request = _request(sampler.descriptor())
    binding = SamplerHttpBinding(sampler)
    body = request.model_dump_json(exclude_none=True).encode()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(binding.handle, "POST", "/v1/sample", body)
        assert entered.wait(timeout=5)
        second = pool.submit(binding.handle, "POST", "/v1/sample", body)
        release.set()
        assert [first.result(timeout=5).status, second.result(timeout=5).status] == [200, 200]

    assert active_peak == 1


def test_workspace_verifies_immutable_inputs_and_confines_assigned_outputs(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor()
    request = _request(descriptor)
    job = tmp_path / request.workspace_id
    source = job / "input/source.wav"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    source.chmod(0o400)
    (job / "output").mkdir()
    (job / "control").mkdir()

    workspace = SamplerWorkspace(tmp_path, request)
    assert workspace.verify_input(request.inputs[0]) == source
    assert workspace.output(request.windows[0].output_path) == (job / "output/review/sample.opus")
    assert not workspace.canceled()

    (job / "control/cancel").write_text("stove0-review-cancel/v1\n")
    assert workspace.canceled()

    (job / "output/link").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        workspace.output("output/link/escape.opus")


def test_protocol_identities_reject_undeclared_or_ambiguous_results() -> None:
    descriptor = _descriptor()
    request = _request(descriptor)
    result = _result(descriptor, request)
    document = result.model_dump(mode="json")
    document["outputs"][0]["derived_from"] = ["another-source"]
    document.pop("result_sha256")
    altered = SamplerResult.seal(SamplerResultPayload.model_validate(document))
    with pytest.raises(ValueError, match="sealed windows"):
        validate_result(altered, request, descriptor)

    error = SamplerHttpBinding(FixtureSampler()).handle(
        "POST",
        "/v1/sample",
        json.dumps({"format": "stove0-review-sampler-request/v1"}).encode(),
    )
    assert error.status == 400


def test_sampler_implementation_value_error_is_a_server_fault() -> None:
    descriptor = _descriptor()
    request = _request(descriptor)

    class FaultingSampler(FixtureSampler):
        def sample(self, _request: SamplerRequest) -> SamplerResult:
            raise ValueError("private sampler defect")

    response = SamplerHttpBinding(FaultingSampler()).handle(
        "POST",
        "/v1/sample",
        request.model_dump_json(exclude_none=True).encode(),
    )

    assert response.status == 500
    assert json.loads(response.body)["error"]["code"] == "sampler_failed"
    assert b"private sampler defect" not in response.body


def test_sampler_terminal_outcomes_keep_inapplicability_and_failure_distinct() -> None:
    descriptor = _descriptor()
    request = _request(descriptor)
    inapplicable = SamplerResult.seal(
        SamplerResultPayload(
            request_sha256=request.request_sha256,
            sampler_descriptor_sha256=descriptor.descriptor_sha256,
            state="inapplicable",
            inapplicable=SamplerInapplicable(
                code="fixture-content",
                message="fixture cannot be sampled",
            ),
        )
    )
    failed = SamplerResult.seal(
        SamplerResultPayload(
            request_sha256=request.request_sha256,
            sampler_descriptor_sha256=descriptor.descriptor_sha256,
            state="failed",
            failure=SamplerFailure(
                code="fixture-infrastructure",
                message="fixture service is unavailable",
                retryable=True,
            ),
        )
    )

    assert inapplicable.inapplicable is not None
    assert inapplicable.failure is None
    assert failed.failure is not None and failed.failure.retryable is True
    assert failed.inapplicable is None
