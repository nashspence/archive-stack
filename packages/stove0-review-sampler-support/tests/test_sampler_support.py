from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from stove0_protocol import JsonSchemaDocument
from stove0_review_sampler_client import ReviewSamplerClient
from stove0_review_sampler_protocol import (
    SamplerDescriptor,
    SamplerDescriptorPayload,
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
        report = conformance_report(client, request=request)

    assert report["status"] == "conformant"
    assert report["image_digest"] == _sha("9")
    assert report["sample"]["state"] == "succeeded"
    assert sampler_schema_bundle()["endpoints"] == {
        "GET /v1/sampler": "SamplerDescriptor",
        "POST /v1/sample": {
            "request": "SamplerRequest",
            "response": "SamplerResult",
        },
    }


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
