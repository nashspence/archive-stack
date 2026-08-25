"""Canonical contracts for collection producers and collection transforms.

The contracts in this module are deliberately transport-neutral. Riverhog owns
collection custody and immutable identities; adapters and companions exchange
only these sealed JSON documents and opaque capabilities.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from riverhog_protocol.paths import (
    CollectionId,
    normalize_relpath,
    normalize_tag,
    validate_collection_id,
)

PRODUCER_EVIDENCE_FORMAT: Literal["riverhog-collection-producer/v1"] = (
    "riverhog-collection-producer/v1"
)
TRANSFORM_INTENT_FORMAT: Literal["riverhog-collection-transform/v1"] = (
    "riverhog-collection-transform/v1"
)
DERIVATION_FORMAT: Literal["riverhog-collection-derivation/v1"] = (
    "riverhog-collection-derivation/v1"
)
PRODUCER_EVIDENCE_PATH = "riverhog/producer-evidence.json"
DERIVATION_EVIDENCE_PATH = "riverhog/collection-derivation.json"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEMANTIC_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._/-]{0,158}[a-z0-9])?$", re.ASCII)
_RETIREMENT_POLICIES = {"retain", "retire-after-verified-output"}
_DISPOSITION_STATES = {"transformed", "preserved", "omitted", "rejected"}

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
RetirementPolicy = Literal["retain", "retire-after-verified-output"]
DispositionState = Literal["transformed", "preserved", "omitted", "rejected"]


def canonical_json_bytes(value: object) -> bytes:
    """Encode a JSON value canonically for contract identities.

    V1 deliberately permits only finite JSON numbers and uses sorted UTF-8 JSON
    without insignificant whitespace. The result is deterministic across all
    in-repository Python implementations.
    """

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not canonical JSON: {exc}") from exc


def canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _sha256(value: object, label: str) -> str:
    text = str(value or "").casefold()
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a 64-character lowercase SHA-256")
    return text


def _semantic_id(value: object, label: str) -> str:
    text = str(value or "")
    if text != text.strip() or _SEMANTIC_ID_RE.fullmatch(text) is None:
        raise ValueError(f"{label} is not a canonical semantic identifier")
    return text


def _visible_text(value: object, label: str, *, maximum: int = 500) -> str:
    text = str(value or "")
    if not text or text != text.strip() or len(text) > maximum:
        raise ValueError(f"{label} must be visible canonical text")
    return text


def _uint(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative integer")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a non-negative integer") from exc
    if parsed < 0 or str(parsed) != str(value):
        raise ValueError(f"{label} must be a canonical non-negative integer")
    return parsed


def _positive_uint(value: object, label: str) -> int:
    parsed = _uint(value, label)
    if parsed < 1:
        raise ValueError(f"{label} must be positive")
    return parsed


def _json_object(value: object, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    normalized = cast(dict[str, JsonValue], json.loads(canonical_json_bytes(value)))
    if not isinstance(normalized, dict):  # defensive; Mapping above already guarantees this
        raise ValueError(f"{label} must be a JSON object")
    return normalized


def _canonical_tags(values: Sequence[object]) -> tuple[str, ...]:
    tags = tuple(sorted(normalize_tag(str(value)) for value in values))
    if not tags or len(tags) != len(set(tags)):
        raise ValueError("output tags must be nonempty, canonical, and unique")
    return tags


@dataclass(frozen=True, order=True, slots=True)
class CollectionRootIdentity:
    """Immutable identity used by collection workflow contracts."""

    collection_id: CollectionId
    archive_root_sha256: str
    content_identity: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "collection_id", validate_collection_id(self.collection_id))
        object.__setattr__(
            self,
            "archive_root_sha256",
            _sha256(self.archive_root_sha256, "collection archive-root identity"),
        )
        object.__setattr__(
            self,
            "content_identity",
            _sha256(self.content_identity, "collection content identity"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "collection_id": self.collection_id,
            "archive_root_sha256": self.archive_root_sha256,
            "content_identity": self.content_identity,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CollectionRootIdentity:
        if set(value) != {"collection_id", "archive_root_sha256", "content_identity"}:
            raise ValueError("collection root identity fields are invalid")
        return cls(
            collection_id=_positive_uint(value.get("collection_id"), "collection id"),
            archive_root_sha256=str(value.get("archive_root_sha256") or ""),
            content_identity=str(value.get("content_identity") or ""),
        )


@dataclass(frozen=True, order=True, slots=True)
class CollectionArtifactIdentity:
    """One exact immutable logical file within a finalized collection root."""

    collection: CollectionRootIdentity
    path: str
    bytes: int
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_relpath(self.path))
        object.__setattr__(self, "bytes", _uint(self.bytes, "artifact bytes"))
        object.__setattr__(self, "sha256", _sha256(self.sha256, "artifact identity"))

    def as_dict(self) -> dict[str, object]:
        return {
            "collection": self.collection.as_dict(),
            "path": self.path,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CollectionArtifactIdentity:
        if set(value) != {"collection", "path", "bytes", "sha256"}:
            raise ValueError("collection artifact identity fields are invalid")
        collection = value.get("collection")
        if not isinstance(collection, Mapping):
            raise ValueError("collection artifact identity has no collection root")
        return cls(
            collection=CollectionRootIdentity.from_mapping(collection),
            path=str(value.get("path") or ""),
            bytes=_uint(value.get("bytes"), "artifact bytes"),
            sha256=str(value.get("sha256") or ""),
        )


@dataclass(frozen=True, order=True, slots=True)
class CollectionProcessingOutcomeIdentity:
    """One verified output retained as an outcome of collection processing."""

    outcome_id: str
    source_claim_id: str
    output_collection: CollectionRootIdentity
    derivation_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "outcome_id",
            _semantic_id(self.outcome_id, "outcome id"),
        )
        object.__setattr__(
            self,
            "source_claim_id",
            _sha256(self.source_claim_id, "source claim identity"),
        )
        object.__setattr__(
            self,
            "derivation_sha256",
            _sha256(self.derivation_sha256, "derivation identity"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome_id": self.outcome_id,
            "source_claim_id": self.source_claim_id,
            "output_collection": self.output_collection.as_dict(),
            "derivation_sha256": self.derivation_sha256,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> CollectionProcessingOutcomeIdentity:
        if set(value) != {
            "outcome_id",
            "source_claim_id",
            "output_collection",
            "derivation_sha256",
        }:
            raise ValueError("collection processing outcome fields are invalid")
        output = value.get("output_collection")
        if not isinstance(output, Mapping):
            raise ValueError("outcome collection must be an object")
        return cls(
            outcome_id=str(value.get("outcome_id") or ""),
            source_claim_id=str(value.get("source_claim_id") or ""),
            output_collection=CollectionRootIdentity.from_mapping(output),
            derivation_sha256=str(value.get("derivation_sha256") or ""),
        )


@dataclass(frozen=True, slots=True)
class RecipeIdentity:
    id: str
    revision: int
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _semantic_id(self.id, "recipe id"))
        object.__setattr__(self, "revision", _positive_uint(self.revision, "recipe revision"))
        object.__setattr__(self, "sha256", _sha256(self.sha256, "recipe identity"))

    def as_dict(self) -> dict[str, object]:
        return {"id": self.id, "revision": self.revision, "sha256": self.sha256}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RecipeIdentity:
        if set(value) != {"id", "revision", "sha256"}:
            raise ValueError("recipe identity fields are invalid")
        return cls(
            id=str(value.get("id") or ""),
            revision=_positive_uint(value.get("revision"), "recipe revision"),
            sha256=str(value.get("sha256") or ""),
        )


@dataclass(frozen=True, slots=True)
class OperationIdentity:
    id: str
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _semantic_id(self.id, "operation id"))
        object.__setattr__(self, "sha256", _sha256(self.sha256, "operation identity"))

    def as_dict(self) -> dict[str, object]:
        return {"id": self.id, "sha256": self.sha256}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OperationIdentity:
        if set(value) != {"id", "sha256"}:
            raise ValueError("operation identity fields are invalid")
        return cls(id=str(value.get("id") or ""), sha256=str(value.get("sha256") or ""))


@dataclass(frozen=True, slots=True)
class ProducerEvidence:
    """Self-contained immutable evidence created by a protocol adapter."""

    producer_app: str
    adapter_id: str
    adapter_version: str
    source_event_id: str
    ingest_source: str
    source_context: dict[str, JsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "producer_app", _semantic_id(self.producer_app, "producer app"))
        object.__setattr__(self, "adapter_id", _semantic_id(self.adapter_id, "adapter id"))
        object.__setattr__(
            self,
            "adapter_version",
            _visible_text(self.adapter_version, "adapter version", maximum=120),
        )
        object.__setattr__(
            self,
            "source_event_id",
            _visible_text(self.source_event_id, "source event id", maximum=300),
        )
        object.__setattr__(
            self,
            "ingest_source",
            _visible_text(self.ingest_source, "ingest source", maximum=300),
        )
        object.__setattr__(
            self,
            "source_context",
            _json_object(self.source_context, "source context"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "format": PRODUCER_EVIDENCE_FORMAT,
            "producer_app": self.producer_app,
            "adapter": {"id": self.adapter_id, "version": self.adapter_version},
            "source_event_id": self.source_event_id,
            "ingest_source": self.ingest_source,
            "source_context": self.source_context,
        }

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json_bytes()).hexdigest()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ProducerEvidence:
        if (
            set(value)
            != {
                "format",
                "producer_app",
                "adapter",
                "source_event_id",
                "ingest_source",
                "source_context",
            }
            or value.get("format") != PRODUCER_EVIDENCE_FORMAT
        ):
            raise ValueError("producer evidence fields are invalid")
        adapter = value.get("adapter")
        if not isinstance(adapter, Mapping) or set(adapter) != {"id", "version"}:
            raise ValueError("producer adapter identity is invalid")
        context = value.get("source_context")
        if not isinstance(context, Mapping):
            raise ValueError("producer source context is invalid")
        return cls(
            producer_app=str(value.get("producer_app") or ""),
            adapter_id=str(adapter.get("id") or ""),
            adapter_version=str(adapter.get("version") or ""),
            source_event_id=str(value.get("source_event_id") or ""),
            ingest_source=str(value.get("ingest_source") or ""),
            source_context=dict(context),
        )


@dataclass(frozen=True, slots=True)
class TransformIntent:
    """Sealed identity for one collection-set-to-one-collection transformation."""

    transform_id: str
    recipe: RecipeIdentity
    operation: OperationIdentity
    inputs: tuple[CollectionRootIdentity, ...]
    effective_intent: dict[str, JsonValue]
    output_tags: tuple[str, ...]
    retirement_policy: RetirementPolicy = "retain"
    retirement_grace_seconds: int = 0

    def __post_init__(self) -> None:
        normalized_inputs = tuple(sorted(self.inputs))
        if not normalized_inputs or len(normalized_inputs) != len(set(normalized_inputs)):
            raise ValueError("transform inputs must be nonempty and unique")
        if tuple(self.inputs) != normalized_inputs:
            raise ValueError("transform inputs must be in canonical order")
        object.__setattr__(self, "transform_id", _sha256(self.transform_id, "transform id"))
        object.__setattr__(
            self,
            "effective_intent",
            _json_object(self.effective_intent, "effective transform intent"),
        )
        object.__setattr__(self, "output_tags", _canonical_tags(self.output_tags))
        policy = str(self.retirement_policy)
        if policy not in _RETIREMENT_POLICIES:
            raise ValueError("retirement policy is invalid")
        object.__setattr__(self, "retirement_policy", cast(RetirementPolicy, policy))
        grace = _uint(self.retirement_grace_seconds, "retirement grace seconds")
        if policy == "retain" and grace:
            raise ValueError("retain policy cannot have a retirement grace period")
        object.__setattr__(self, "retirement_grace_seconds", grace)
        if self.transform_id != self.identity_sha256():
            raise ValueError("transform id does not match its canonical intent")

    def identity_payload(self) -> dict[str, object]:
        return {
            "format": TRANSFORM_INTENT_FORMAT,
            "recipe": self.recipe.as_dict(),
            "operation": self.operation.as_dict(),
            "inputs": [item.as_dict() for item in self.inputs],
            "effective_intent": self.effective_intent,
            "output_tags": list(self.output_tags),
            "retirement": {
                "policy": self.retirement_policy,
                "grace_seconds": self.retirement_grace_seconds,
            },
        }

    def identity_sha256(self) -> str:
        return canonical_json_sha256(self.identity_payload())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "transform_id": self.transform_id}

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def seal(
        cls,
        *,
        recipe: RecipeIdentity,
        operation: OperationIdentity,
        inputs: Sequence[CollectionRootIdentity],
        effective_intent: Mapping[str, object],
        output_tags: Sequence[str],
        retirement_policy: RetirementPolicy = "retain",
        retirement_grace_seconds: int = 0,
    ) -> TransformIntent:
        normalized_inputs = tuple(sorted(inputs))
        normalized_intent = _json_object(effective_intent, "effective transform intent")
        normalized_tags = _canonical_tags(tuple(output_tags))
        payload = {
            "format": TRANSFORM_INTENT_FORMAT,
            "recipe": recipe.as_dict(),
            "operation": operation.as_dict(),
            "inputs": [item.as_dict() for item in normalized_inputs],
            "effective_intent": normalized_intent,
            "output_tags": list(normalized_tags),
            "retirement": {
                "policy": retirement_policy,
                "grace_seconds": retirement_grace_seconds,
            },
        }
        return cls(
            transform_id=canonical_json_sha256(payload),
            recipe=recipe,
            operation=operation,
            inputs=normalized_inputs,
            effective_intent=normalized_intent,
            output_tags=normalized_tags,
            retirement_policy=retirement_policy,
            retirement_grace_seconds=retirement_grace_seconds,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> TransformIntent:
        if (
            set(value)
            != {
                "format",
                "transform_id",
                "recipe",
                "operation",
                "inputs",
                "effective_intent",
                "output_tags",
                "retirement",
            }
            or value.get("format") != TRANSFORM_INTENT_FORMAT
        ):
            raise ValueError("transform intent fields are invalid")
        recipe = value.get("recipe")
        operation = value.get("operation")
        inputs = value.get("inputs")
        intent = value.get("effective_intent")
        output_tags = value.get("output_tags")
        retirement = value.get("retirement")
        if (
            not isinstance(recipe, Mapping)
            or not isinstance(operation, Mapping)
            or not isinstance(inputs, list)
            or not all(isinstance(item, Mapping) for item in inputs)
            or not isinstance(intent, Mapping)
            or not isinstance(output_tags, list)
            or not isinstance(retirement, Mapping)
            or set(retirement) != {"policy", "grace_seconds"}
        ):
            raise ValueError("transform intent nested fields are invalid")
        return cls(
            transform_id=str(value.get("transform_id") or ""),
            recipe=RecipeIdentity.from_mapping(recipe),
            operation=OperationIdentity.from_mapping(operation),
            inputs=tuple(CollectionRootIdentity.from_mapping(item) for item in inputs),
            effective_intent=dict(intent),
            output_tags=tuple(str(item) for item in output_tags),
            retirement_policy=cast(RetirementPolicy, str(retirement.get("policy") or "")),
            retirement_grace_seconds=_uint(
                retirement.get("grace_seconds"), "retirement grace seconds"
            ),
        )


@dataclass(frozen=True, order=True, slots=True)
class ArtifactDisposition:
    input_collection_id: CollectionId
    input_archive_root_sha256: str
    input_path: str
    status: DispositionState
    outputs: tuple[str, ...] = ()
    code: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_collection_id",
            validate_collection_id(self.input_collection_id),
        )
        object.__setattr__(
            self,
            "input_archive_root_sha256",
            _sha256(self.input_archive_root_sha256, "input archive-root identity"),
        )
        object.__setattr__(self, "input_path", normalize_relpath(self.input_path))
        state = str(self.status)
        if state not in _DISPOSITION_STATES:
            raise ValueError("artifact disposition state is invalid")
        object.__setattr__(self, "status", cast(DispositionState, state))
        outputs = tuple(sorted(normalize_relpath(item) for item in self.outputs))
        if len(outputs) != len(set(outputs)):
            raise ValueError("artifact disposition outputs must be unique")
        object.__setattr__(self, "outputs", outputs)
        if state == "transformed" and not outputs:
            raise ValueError("transformed artifact disposition requires output paths")
        if state != "transformed" and outputs:
            raise ValueError("only transformed artifact dispositions may name outputs")
        if state in {"omitted", "rejected"}:
            object.__setattr__(self, "code", _semantic_id(self.code, "disposition code"))
            object.__setattr__(self, "message", _visible_text(self.message, "disposition message"))
        elif self.code is not None or self.message is not None:
            raise ValueError("successful artifact dispositions cannot carry failure details")

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "input": {
                "collection_id": self.input_collection_id,
                "archive_root_sha256": self.input_archive_root_sha256,
                "path": self.input_path,
            },
            "status": self.status,
        }
        if self.outputs:
            payload["outputs"] = list(self.outputs)
        if self.code is not None:
            payload["failure"] = {"code": self.code, "message": self.message}
        return payload

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ArtifactDisposition:
        if not {"input", "status"}.issubset(value) or set(value) - {
            "input",
            "status",
            "outputs",
            "failure",
        }:
            raise ValueError("artifact disposition fields are invalid")
        input_value = value.get("input")
        if not isinstance(input_value, Mapping) or set(input_value) != {
            "collection_id",
            "archive_root_sha256",
            "path",
        }:
            raise ValueError("artifact disposition input fields are invalid")
        outputs = value.get("outputs", [])
        failure = value.get("failure")
        if not isinstance(outputs, list):
            raise ValueError("artifact disposition outputs are invalid")
        code: str | None = None
        message: str | None = None
        if failure is not None:
            if not isinstance(failure, Mapping) or set(failure) != {"code", "message"}:
                raise ValueError("artifact disposition failure is invalid")
            code = str(failure.get("code") or "")
            message = str(failure.get("message") or "")
        return cls(
            input_collection_id=_positive_uint(
                input_value.get("collection_id"), "input collection id"
            ),
            input_archive_root_sha256=str(input_value.get("archive_root_sha256") or ""),
            input_path=str(input_value.get("path") or ""),
            status=cast(DispositionState, str(value.get("status") or "")),
            outputs=tuple(str(item) for item in outputs),
            code=code,
            message=message,
        )


@dataclass(frozen=True, slots=True)
class CollectionDerivation:
    """Root-bound evidence for one finalized derived collection."""

    execution_id: str
    claim_id: str
    fence: int
    recipe: RecipeIdentity
    operation: OperationIdentity
    inputs: tuple[CollectionRootIdentity, ...]
    output_tags: tuple[str, ...]
    execution_envelope_sha256: str
    execution_sha256: str
    controller_evidence: dict[str, JsonValue]
    controller_evidence_sha256: str
    dispositions: tuple[ArtifactDisposition, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_id", _sha256(self.execution_id, "execution id"))
        object.__setattr__(self, "claim_id", _visible_text(self.claim_id, "claim id", maximum=160))
        object.__setattr__(self, "fence", _positive_uint(self.fence, "claim fence"))
        normalized_inputs = tuple(sorted(self.inputs))
        if not normalized_inputs or normalized_inputs != self.inputs:
            raise ValueError("derivation inputs must be nonempty and canonically ordered")
        object.__setattr__(self, "output_tags", _canonical_tags(self.output_tags))
        object.__setattr__(
            self,
            "execution_envelope_sha256",
            _sha256(self.execution_envelope_sha256, "execution envelope identity"),
        )
        object.__setattr__(
            self,
            "execution_sha256",
            _sha256(self.execution_sha256, "execution identity"),
        )
        normalized_controller_evidence = _json_object(
            self.controller_evidence,
            "controller evidence",
        )
        object.__setattr__(
            self,
            "controller_evidence",
            normalized_controller_evidence,
        )
        evidence_sha256 = _sha256(
            self.controller_evidence_sha256,
            "controller evidence identity",
        )
        if canonical_json_sha256(normalized_controller_evidence) != evidence_sha256:
            raise ValueError("controller evidence identity does not match its canonical document")
        object.__setattr__(self, "controller_evidence_sha256", evidence_sha256)
        dispositions = tuple(sorted(self.dispositions))
        if not dispositions or dispositions != self.dispositions:
            raise ValueError("artifact dispositions must be nonempty and canonically ordered")
        roots = {(item.collection_id, item.archive_root_sha256) for item in self.inputs}
        if any(
            (item.input_collection_id, item.input_archive_root_sha256) not in roots
            for item in dispositions
        ):
            raise ValueError("artifact disposition references an unknown input root")
        disposition_inputs = [
            (item.input_collection_id, item.input_archive_root_sha256, item.input_path)
            for item in dispositions
        ]
        if len(disposition_inputs) != len(set(disposition_inputs)):
            raise ValueError("artifact dispositions must identify each input at most once")

    def as_dict(self) -> dict[str, object]:
        return {
            "format": DERIVATION_FORMAT,
            "execution_id": self.execution_id,
            "claim": {"id": self.claim_id, "fence": self.fence},
            "recipe": self.recipe.as_dict(),
            "operation": self.operation.as_dict(),
            "inputs": [item.as_dict() for item in self.inputs],
            "output_tags": list(self.output_tags),
            "execution_envelope_sha256": self.execution_envelope_sha256,
            "execution_sha256": self.execution_sha256,
            "controller_evidence": self.controller_evidence,
            "controller_evidence_sha256": self.controller_evidence_sha256,
            "dispositions": [item.as_dict() for item in self.dispositions],
        }

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json_bytes()).hexdigest()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CollectionDerivation:
        if (
            set(value)
            != {
                "format",
                "execution_id",
                "claim",
                "recipe",
                "operation",
                "inputs",
                "output_tags",
                "execution_envelope_sha256",
                "execution_sha256",
                "controller_evidence",
                "controller_evidence_sha256",
                "dispositions",
            }
            or value.get("format") != DERIVATION_FORMAT
        ):
            raise ValueError("collection derivation fields are invalid")
        claim = value.get("claim")
        recipe = value.get("recipe")
        operation = value.get("operation")
        inputs = value.get("inputs")
        output_tags = value.get("output_tags")
        dispositions = value.get("dispositions")
        if (
            not isinstance(claim, Mapping)
            or set(claim) != {"id", "fence"}
            or not isinstance(recipe, Mapping)
            or not isinstance(operation, Mapping)
            or not isinstance(inputs, list)
            or not all(isinstance(item, Mapping) for item in inputs)
            or not isinstance(output_tags, list)
            or not isinstance(dispositions, list)
            or not all(isinstance(item, Mapping) for item in dispositions)
        ):
            raise ValueError("collection derivation nested fields are invalid")
        return cls(
            execution_id=str(value.get("execution_id") or ""),
            claim_id=str(claim.get("id") or ""),
            fence=_positive_uint(claim.get("fence"), "claim fence"),
            recipe=RecipeIdentity.from_mapping(recipe),
            operation=OperationIdentity.from_mapping(operation),
            inputs=tuple(CollectionRootIdentity.from_mapping(item) for item in inputs),
            output_tags=tuple(str(item) for item in output_tags),
            execution_envelope_sha256=str(value.get("execution_envelope_sha256") or ""),
            execution_sha256=str(value.get("execution_sha256") or ""),
            controller_evidence=_json_object(
                value.get("controller_evidence"),
                "controller evidence",
            ),
            controller_evidence_sha256=str(value.get("controller_evidence_sha256") or ""),
            dispositions=tuple(ArtifactDisposition.from_mapping(item) for item in dispositions),
        )


__all__ = [
    "ArtifactDisposition",
    "CollectionArtifactIdentity",
    "CollectionDerivation",
    "CollectionProcessingOutcomeIdentity",
    "CollectionRootIdentity",
    "DERIVATION_EVIDENCE_PATH",
    "DERIVATION_FORMAT",
    "DispositionState",
    "OperationIdentity",
    "PRODUCER_EVIDENCE_FORMAT",
    "PRODUCER_EVIDENCE_PATH",
    "ProducerEvidence",
    "RecipeIdentity",
    "RetirementPolicy",
    "TRANSFORM_INTENT_FORMAT",
    "TransformIntent",
    "canonical_json_bytes",
    "canonical_json_sha256",
]
