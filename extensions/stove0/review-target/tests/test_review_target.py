from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from riverhog_protocol import canonical_json_sha256
from stove0_protocol import CollectionRootRef, JsonSchemaDocument
from stove0_review_contracts import REVIEW_MATERIALIZE_OPERATION, REVIEW_SOURCE_ROLE
from stove0_review_sampler_client import ReviewSamplerClient
from stove0_review_sampler_protocol import (
    SamplerDescriptor,
    SamplerDescriptorPayload,
    SamplerFailure,
    SamplerInapplicable,
    SamplerResult,
    SamplerResultPayload,
)
from stove0_review_target import ReviewTargetService, SamplerRegistration
from stove0_review_target import app as review_app
from stove0_review_target import target as review_target
from stove0_review_target.app import ReviewTargetConfig, SamplerConfig, create_app
from stove0_target_support import (
    InputArtifact,
    OutputArtifact,
    TargetExecutionCanceled,
    TargetExecutionFailure,
    TargetExecutionInapplicable,
    TargetPreflightRequest,
)


def _sha(character: str) -> str:
    return character * 64


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
    target = ReviewTargetService(
        state_root=tmp_path / "state",
        workspace_root=tmp_path / "workspace",
        samplers=(registration,),
        source_revision="fixture",
        image_digest=_sha("9"),
    )
    try:
        request = TargetPreflightRequest(
            operation_id=REVIEW_MATERIALIZE_OPERATION.id,
            operation_contract_sha256=REVIEW_MATERIALIZE_OPERATION.contract_sha256,
            inputs=(
                InputArtifact(
                    id="source",
                    role=REVIEW_SOURCE_ROLE,
                    collection=CollectionRootRef(
                        collection_id=1,
                        manifest_sha256=_sha("1"),
                        content_etag=_sha("2"),
                    ),
                    path="camera/source.wav",
                    bytes=12,
                    sha256=_sha("3"),
                    media_type="audio/wav",
                ),
            ),
            intent={
                "sample_plan": {
                    "format": "stove0-review-sample-plan/v1",
                    "selection_method": "evenly-spaced/v1",
                    "samples_per_artifact": 1,
                    "window_duration_ms": 1000,
                    "windows": [
                        {
                            "artifact_id": "source",
                            "start_ms": 0,
                            "duration_ms": 1000,
                        }
                    ],
                    "sample_plan_sha256": _sha("4"),
                },
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
        with pytest.raises(ValueError, match="sampler_image_digest"):
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
    finally:
        target.close()
    assert sampler_client.closed


def test_review_process_exposes_only_target_contract(tmp_path: Path) -> None:
    registration, _sampler_client = _sampler()
    target = ReviewTargetService(
        state_root=tmp_path / "state",
        workspace_root=tmp_path / "workspace",
        samplers=(registration,),
        source_revision="fixture",
        image_digest=_sha("9"),
    )
    with TestClient(create_app(token="review-secret", target=target)) as client:
        response = client.get(
            "/v1/target",
            headers={"Authorization": "Bearer review-secret"},
        )
        assert response.status_code == 200
        assert response.json()["implementation_id"] == "stove0.review-target/v1"
        assert (
            client.get(
                "/v1/sampler",
                headers={"Authorization": "Bearer review-secret"},
            ).status_code
            == 404
        )


def test_review_target_preserves_sampler_terminal_classification() -> None:
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
        review_target._require_sampler_success(retryable)
    assert retryable_error.value.retryable is True
    with pytest.raises(TargetExecutionInapplicable, match="fixture content"):
        review_target._require_sampler_success(inapplicable)
    with pytest.raises(TargetExecutionCanceled, match="canceled"):
        review_target._require_sampler_success(canceled)


def test_review_execution_identity_is_the_canonical_semantic_result() -> None:
    output = OutputArtifact(
        id="review-index",
        role="stove0.review.index/v1",
        path="review/index.json",
        bytes=12,
        sha256=_sha("4"),
        media_type="application/json",
        derived_from=("source",),
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
        review_target._execution_sha256(
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
    monkeypatch.setenv("STOVE0_REVIEW_TARGET_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("STOVE0_REVIEW_TARGET_TOKEN", raising=False)
    assert review_app._secret() == "file-secret"
    monkeypatch.delenv("STOVE0_REVIEW_TARGET_TOKEN_FILE")
    monkeypatch.setenv("STOVE0_REVIEW_TARGET_TOKEN", "direct-secret")

    registrations = (_sampler()[0],)
    sampler_file = tmp_path / "samplers.json"
    sampler_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("STOVE0_REVIEW_TARGET_SAMPLERS_JSON_FILE", str(sampler_file))
    monkeypatch.delenv("STOVE0_REVIEW_TARGET_SAMPLERS_JSON", raising=False)
    monkeypatch.setattr(review_app, "load_sampler_registrations", lambda path: registrations)
    assert review_app._sampler_registrations() == registrations
    monkeypatch.delenv("STOVE0_REVIEW_TARGET_SAMPLERS_JSON_FILE")
    monkeypatch.setenv("STOVE0_REVIEW_TARGET_SAMPLERS_JSON", "{}")
    monkeypatch.setattr(review_app, "parse_sampler_registrations", lambda document: registrations)
    assert review_app._sampler_registrations() == registrations

    monkeypatch.setenv("STOVE0_REVIEW_TARGET_HOST", "127.0.0.8")
    monkeypatch.setenv("STOVE0_REVIEW_TARGET_PORT", "8188")
    monkeypatch.setenv("STOVE0_REVIEW_TARGET_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("STOVE0_REVIEW_TARGET_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("STOVE0_REVIEW_TARGET_SOURCE_REVISION", "fixture-revision")
    monkeypatch.setenv("STOVE0_REVIEW_TARGET_IMAGE_DIGEST", _sha("9"))
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

    monkeypatch.setattr(review_app, "ReviewTargetService", ConfiguredTarget)
    monkeypatch.setattr(review_app.uvicorn, "run", run)

    assert review_app.main([]) == 0
    assert configured == {
        "state_root": tmp_path / "state",
        "workspace_root": tmp_path / "workspace",
        "samplers": registrations,
        "source_revision": "fixture-revision",
        "image_digest": _sha("9"),
        "terminal_state_retention_seconds": 2_592_000,
        "host": "127.0.0.8",
        "port": 8188,
    }


def test_review_target_registration_count_is_defined_by_deployment(tmp_path: Path) -> None:
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
