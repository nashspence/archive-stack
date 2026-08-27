"""Canonical Riverhog provenance identity and reference contracts."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any

import rfc8785
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

CANONICAL_UUID_URN_PATTERN = (
    r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
PROVENANCE_CONTRACT_ENTRY_POINT_GROUP = "riverhog.provenance-contracts"
PROVENANCE_CONTRACT_BINDING_FORMAT = "riverhog-provenance-contract-binding/v1"
PROVENANCE_CONTRACT_REFERENCE_FORMAT = "riverhog-provenance-contract-reference/v1"
PROVENANCE_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
PROVENANCE_SCHEMA_FORMAT_POLICY = "annotation-only"
SHA256_PATTERN = r"^[0-9a-f]{64}$"


def index_schema_documents(
    documents: Iterable[Mapping[str, Any]],
    *,
    owner: str,
) -> dict[str, dict[str, Any]]:
    """Index one contract pack without silently replacing a schema identity."""

    indexed: dict[str, dict[str, Any]] = {}
    for supplied in documents:
        document = dict(supplied)
        identifier = document.get("$id")
        if not isinstance(identifier, str) or not identifier or identifier != identifier.strip():
            raise ValueError(f"{owner} schema has no canonical $id")
        if identifier in indexed:
            raise ValueError(f"{owner} schema identity is duplicated: {identifier}")
        indexed[identifier] = document
    return dict(sorted(indexed.items()))


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return rfc8785.dumps(dict(value))


def _validate_schema_pack(
    schemas: Mapping[str, dict[str, Any]],
    *,
    owner: str,
) -> None:
    resources: list[tuple[str, Any]] = []
    for identifier, document in schemas.items():
        if document.get("$schema") != PROVENANCE_SCHEMA_DIALECT:
            raise ValueError(
                f"{owner} schema must declare the exact JSON Schema Draft 2020-12 "
                f"dialect: {identifier}"
            )
        try:
            Draft202012Validator.check_schema(document)
        except SchemaError as exc:
            raise ValueError(
                f"{owner} schema is not valid JSON Schema Draft 2020-12: {identifier}"
            ) from exc
        resources.append((identifier, DRAFT202012.create_resource(document)))

    registry = Registry().with_resources(resources)

    def visit(contents: Any, resolver: Any) -> None:
        if isinstance(contents, dict):
            for keyword in ("$ref", "$dynamicRef"):
                reference = contents.get(keyword)
                if reference is None:
                    continue
                try:
                    resolver.lookup(reference)
                except Unresolvable as exc:
                    raise ValueError(
                        f"{owner} schema reference is outside its sealed contract pack: {reference}"
                    ) from exc
        for subcontents in DRAFT202012.subresources_of(contents):
            subresource = DRAFT202012.create_resource(subcontents)
            visit(subcontents, resolver.in_subresource(subresource))

    for identifier, document in schemas.items():
        visit(document, registry.resolver(identifier))


@dataclass(frozen=True, slots=True, init=False)
class ProvenanceContractBinding:
    """One immutable, content-addressed provenance observation contract pack."""

    format: str
    contract_id: str
    contract_sha256: str
    schema_dialect: str
    format_policy: str
    _schemas_json: bytes

    def __init__(
        self,
        *,
        contract_id: str,
        schemas: Iterable[Mapping[str, Any]],
    ) -> None:
        if not contract_id or contract_id != contract_id.strip():
            raise ValueError("provenance contract ID must be nonempty and canonical")
        indexed = index_schema_documents(schemas, owner=contract_id)
        if not indexed:
            raise ValueError("provenance contract pack must contain at least one schema")
        _validate_schema_pack(indexed, owner=contract_id)
        document = {
            "format": PROVENANCE_CONTRACT_BINDING_FORMAT,
            "contract_id": contract_id,
            "schema_dialect": PROVENANCE_SCHEMA_DIALECT,
            "format_policy": PROVENANCE_SCHEMA_FORMAT_POLICY,
            "schemas": indexed,
        }
        encoded = _canonical_json(document)
        object.__setattr__(self, "format", PROVENANCE_CONTRACT_BINDING_FORMAT)
        object.__setattr__(self, "contract_id", contract_id)
        object.__setattr__(self, "schema_dialect", PROVENANCE_SCHEMA_DIALECT)
        object.__setattr__(self, "format_policy", PROVENANCE_SCHEMA_FORMAT_POLICY)
        object.__setattr__(self, "contract_sha256", hashlib.sha256(encoded).hexdigest())
        object.__setattr__(self, "_schemas_json", _canonical_json(indexed))

    @property
    def schemas(self) -> dict[str, dict[str, Any]]:
        """Return an isolated copy of the schemas sealed by this binding."""

        value = json.loads(self._schemas_json)
        if not isinstance(value, dict):  # pragma: no cover - constructor invariant
            raise RuntimeError("invalid sealed provenance contract schema map")
        return value

    def reference(self, provider: str) -> dict[str, str]:
        """Return the exact portable identity persisted with an observation."""

        if not provider or provider != provider.strip():
            raise ValueError("provenance contract provider name must be canonical")
        return {
            "format": PROVENANCE_CONTRACT_REFERENCE_FORMAT,
            "provider": provider,
            "contract_id": self.contract_id,
            "contract_sha256": self.contract_sha256,
        }


def require_canonical_uuid_urn(value: str, field: str = "identity") -> str:
    """Return one exact lowercase UUID URN or reject it."""

    prefix = "urn:uuid:"
    if not value.startswith(prefix):
        raise ValueError(f"{field} must be a lowercase UUID URN")
    try:
        parsed = uuid.UUID(value[len(prefix) :])
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid UUID URN") from exc
    canonical = f"urn:uuid:{parsed}"
    if value != canonical:
        raise ValueError(f"{field} must use canonical lowercase UUID URN syntax")
    return canonical


def _journal_id(value: str) -> str:
    return require_canonical_uuid_urn(value, "journal_id")


def _state_id(value: str) -> str:
    return require_canonical_uuid_urn(value, "current_state_id")


def _entry_id(value: str) -> str:
    return require_canonical_uuid_urn(value, "entry_id")


type ProvenanceJournalId = Annotated[
    str,
    Field(pattern=CANONICAL_UUID_URN_PATTERN),
    AfterValidator(_journal_id),
]
type ProvenanceStateId = Annotated[
    str,
    Field(pattern=CANONICAL_UUID_URN_PATTERN),
    AfterValidator(_state_id),
]
type ProvenanceEntryId = Annotated[
    str,
    Field(pattern=CANONICAL_UUID_URN_PATTERN),
    AfterValidator(_entry_id),
]


class ProvenanceJournalStateReference(BaseModel):
    """One exact current state in one Riverhog provenance journal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    journal_id: ProvenanceJournalId
    current_state_id: ProvenanceStateId


__all__ = [
    "CANONICAL_UUID_URN_PATTERN",
    "PROVENANCE_CONTRACT_BINDING_FORMAT",
    "PROVENANCE_CONTRACT_ENTRY_POINT_GROUP",
    "PROVENANCE_CONTRACT_REFERENCE_FORMAT",
    "PROVENANCE_SCHEMA_DIALECT",
    "PROVENANCE_SCHEMA_FORMAT_POLICY",
    "ProvenanceJournalId",
    "ProvenanceEntryId",
    "ProvenanceContractBinding",
    "ProvenanceJournalStateReference",
    "SHA256_PATTERN",
    "ProvenanceStateId",
    "index_schema_documents",
    "require_canonical_uuid_urn",
]
