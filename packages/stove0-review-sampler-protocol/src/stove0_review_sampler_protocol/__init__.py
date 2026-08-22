"""Versioned terminal sampler documents shared by review targets and samplers."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from riverhog_protocol.paths import normalize_relpath
from stove0_protocol import JsonSchemaDocument, canonical_json_sha256

SAMPLER_PROTOCOL: Literal["stove0-review-sampler/v1"] = "stove0-review-sampler/v1"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
SEMANTIC_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9._/-]{0,158}[a-z0-9])?$"
ARTIFACT_ID_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,158}[A-Za-z0-9])?$"


class SamplerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SamplerDescriptorPayload(SamplerModel):
    protocol: Literal["stove0-review-sampler/v1"] = SAMPLER_PROTOCOL
    implementation_id: str = Field(pattern=SEMANTIC_ID_PATTERN)
    implementation_version: str = Field(min_length=1, max_length=120)
    source_revision: str = Field(min_length=1, max_length=200)
    image_digest: str = Field(pattern=SHA256_PATTERN)
    primary_operation_id: str = Field(pattern=SEMANTIC_ID_PATTERN)
    primary_operation_contract_sha256: str = Field(pattern=SHA256_PATTERN)
    portable_intent_schema: JsonSchemaDocument
    output_role: str = Field(pattern=SEMANTIC_ID_PATTERN)


class SamplerDescriptor(SamplerDescriptorPayload):
    descriptor_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        document = self.model_dump(mode="json", exclude={"descriptor_sha256"})
        if canonical_json_sha256(document) != self.descriptor_sha256:
            raise ValueError("sampler descriptor digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: SamplerDescriptorPayload) -> SamplerDescriptor:
        document = payload.model_dump(mode="json")
        return cls(**document, descriptor_sha256=canonical_json_sha256(document))


class SamplerInput(SamplerModel):
    id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    path: str = Field(min_length=1, max_length=4096)
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256_PATTERN)
    media_type: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        if normalize_relpath(value) != value or not value.startswith("input/"):
            raise ValueError("sampler input path must be canonical beneath input/")
        return value


class SamplerWindow(SamplerModel):
    id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    input_id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    start_ms: int = Field(ge=0)
    duration_ms: int = Field(ge=1, le=60 * 60 * 1000)
    output_path: str = Field(min_length=1, max_length=4096)

    @field_validator("output_path")
    @classmethod
    def canonical_output_path(cls, value: str) -> str:
        if normalize_relpath(value) != value or not value.startswith("output/"):
            raise ValueError("sampler output path must be canonical beneath output/")
        return value


class SamplerRequestPayload(SamplerModel):
    format: Literal["stove0-review-sampler-request/v1"] = "stove0-review-sampler-request/v1"
    sampler_descriptor_sha256: str = Field(pattern=SHA256_PATTERN)
    workspace_id: str = Field(pattern=SHA256_PATTERN)
    inputs: tuple[SamplerInput, ...] = Field(min_length=1, max_length=128)
    windows: tuple[SamplerWindow, ...] = Field(min_length=1, max_length=8192)
    portable_intent: dict[str, JsonValue]
    maximum_output_bytes: int = Field(ge=1, le=1024**4)
    timeout_seconds: int = Field(ge=1, le=86400)
    cancellation_path: str = Field(min_length=1, max_length=4096)

    @field_validator("inputs")
    @classmethod
    def canonical_inputs(cls, value: tuple[SamplerInput, ...]) -> tuple[SamplerInput, ...]:
        ids = [item.id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("sampler inputs must be unique and ordered")
        return value

    @field_validator("windows")
    @classmethod
    def canonical_windows(cls, value: tuple[SamplerWindow, ...]) -> tuple[SamplerWindow, ...]:
        ids = [item.id for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("sampler windows must be unique and ordered")
        return value

    @field_validator("cancellation_path")
    @classmethod
    def canonical_cancellation_path(cls, value: str) -> str:
        if normalize_relpath(value) != value or not value.startswith("control/"):
            raise ValueError("sampler cancellation path must be beneath control/")
        return value

    @model_validator(mode="after")
    def references_exact_inputs(self) -> Self:
        input_ids = {item.id for item in self.inputs}
        if any(window.input_id not in input_ids for window in self.windows):
            raise ValueError("sampler window references an unknown input")
        return self


class SamplerRequest(SamplerRequestPayload):
    request_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        document = self.model_dump(mode="json", exclude={"request_sha256"})
        if canonical_json_sha256(document) != self.request_sha256:
            raise ValueError("sampler request digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: SamplerRequestPayload) -> SamplerRequest:
        document = payload.model_dump(mode="json")
        return cls(**document, request_sha256=canonical_json_sha256(document))


class SamplerOutput(SamplerModel):
    id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    path: str = Field(min_length=1, max_length=4096)
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256_PATTERN)
    media_type: str = Field(min_length=1, max_length=255)
    derived_from: tuple[str, ...] = Field(min_length=1, max_length=128)

    @field_validator("path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        if normalize_relpath(value) != value or not value.startswith("output/"):
            raise ValueError("sampler result path must be canonical beneath output/")
        return value

    @field_validator("derived_from")
    @classmethod
    def canonical_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("sampler output sources must be unique and ordered")
        return value


class SamplerFailure(SamplerModel):
    code: str = Field(pattern=SEMANTIC_ID_PATTERN)
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool


class SamplerInapplicable(SamplerModel):
    code: str = Field(pattern=SEMANTIC_ID_PATTERN)
    message: str = Field(min_length=1, max_length=1000)


class SamplerResultPayload(SamplerModel):
    format: Literal["stove0-review-sampler-result/v1"] = "stove0-review-sampler-result/v1"
    request_sha256: str = Field(pattern=SHA256_PATTERN)
    sampler_descriptor_sha256: str = Field(pattern=SHA256_PATTERN)
    state: Literal["succeeded", "inapplicable", "failed", "canceled"]
    outputs: tuple[SamplerOutput, ...] = ()
    execution_evidence: dict[str, JsonValue] = Field(default_factory=dict)
    failure: SamplerFailure | None = None
    inapplicable: SamplerInapplicable | None = None

    @field_validator("outputs")
    @classmethod
    def canonical_outputs(cls, value: tuple[SamplerOutput, ...]) -> tuple[SamplerOutput, ...]:
        ids = [item.id for item in value]
        paths = [item.path for item in value]
        if ids != sorted(ids) or len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
            raise ValueError("sampler outputs must be unique and ordered")
        return value

    @model_validator(mode="after")
    def state_shape(self) -> Self:
        if self.state == "succeeded" and (
            not self.outputs or self.failure is not None or self.inapplicable is not None
        ):
            raise ValueError("successful sampler result requires only outputs")
        if self.state == "inapplicable" and (
            self.inapplicable is None or self.failure is not None or self.outputs
        ):
            raise ValueError("inapplicable sampler result requires only its outcome")
        if self.state == "failed" and (
            self.failure is None or self.inapplicable is not None or self.outputs
        ):
            raise ValueError("failed sampler result requires failure and no outputs")
        if self.state == "canceled" and (
            self.failure is not None or self.inapplicable is not None or self.outputs
        ):
            raise ValueError("canceled sampler result has no outputs or outcome")
        return self


class SamplerResult(SamplerResultPayload):
    result_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        document = self.model_dump(mode="json", exclude={"result_sha256"}, exclude_none=True)
        if canonical_json_sha256(document) != self.result_sha256:
            raise ValueError("sampler result digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: SamplerResultPayload) -> SamplerResult:
        document: dict[str, Any] = payload.model_dump(mode="json", exclude_none=True)
        return cls(**document, result_sha256=canonical_json_sha256(document))


def validate_result(
    result: SamplerResult,
    request: SamplerRequest,
    descriptor: SamplerDescriptor,
) -> None:
    if result.request_sha256 != request.request_sha256:
        raise ValueError("sampler result differs from the request identity")
    if result.sampler_descriptor_sha256 != descriptor.descriptor_sha256:
        raise ValueError("sampler result differs from the descriptor identity")
    if request.sampler_descriptor_sha256 != descriptor.descriptor_sha256:
        raise ValueError("sampler request differs from the descriptor identity")
    if result.state == "succeeded":
        expected = {item.id: (item.output_path, (item.input_id,)) for item in request.windows}
        actual = {item.id: (item.path, item.derived_from) for item in result.outputs}
        if actual != expected:
            raise ValueError("sampler outputs differ from the sealed windows")


__all__ = [
    "SAMPLER_PROTOCOL",
    "SamplerDescriptor",
    "SamplerDescriptorPayload",
    "SamplerFailure",
    "SamplerInapplicable",
    "SamplerInput",
    "SamplerOutput",
    "SamplerRequest",
    "SamplerRequestPayload",
    "SamplerResult",
    "SamplerResultPayload",
    "SamplerWindow",
    "validate_result",
]
