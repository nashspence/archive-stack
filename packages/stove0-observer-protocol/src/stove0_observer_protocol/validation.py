"""Executable acceptance for the public Stove0 observer contract."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from stove0_protocol.models import (
    JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
    SHA256_PATTERN,
    ObservationRequest,
    ObservationResult,
    ObserverContractSupport,
    ObserverDescriptor,
    SemanticValidationProfile,
    canonical_json_bytes,
)

FactsSemanticValidator = Callable[[ObservationRequest, Mapping[str, object]], None]


@dataclass(frozen=True, slots=True)
class SemanticValidatorBinding:
    """One contract-owned validator bound to an exact portable profile identity."""

    profile_id: str
    profile_sha256: str
    validator: FactsSemanticValidator

    @classmethod
    def from_profile(
        cls,
        profile: SemanticValidationProfile,
        validator: FactsSemanticValidator,
    ) -> SemanticValidatorBinding:
        return cls(
            profile_id=profile.id,
            profile_sha256=profile.profile_sha256,
            validator=validator,
        )

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("semantic validator profile ID must be nonempty")
        if re.fullmatch(SHA256_PATTERN, self.profile_sha256) is None:
            raise ValueError("semantic validator profile digest must be SHA-256")
        if not callable(self.validator):
            raise TypeError("semantic validator must be callable")


class SemanticValidatorProvider(Protocol):
    """Resolve enabled contract semantics without importing them into generic consumers."""

    def resolve(
        self,
        profile_id: str,
        profile_sha256: str,
    ) -> FactsSemanticValidator | None: ...


class SemanticValidatorRegistry:
    """Immutable exact-profile registry assembled by application composition."""

    def __init__(self, bindings: Iterable[SemanticValidatorBinding] = ()) -> None:
        validators: dict[tuple[str, str], FactsSemanticValidator] = {}
        for binding in bindings:
            key = (binding.profile_id, binding.profile_sha256)
            if key in validators:
                raise ValueError("semantic validator profile is registered more than once")
            validators[key] = binding.validator
        self._validators = validators

    def resolve(
        self,
        profile_id: str,
        profile_sha256: str,
    ) -> FactsSemanticValidator | None:
        return self._validators.get((profile_id, profile_sha256))


def validate_observation_request(
    request: ObservationRequest,
    descriptor: ObserverDescriptor,
) -> ObserverContractSupport:
    """Validate one sealed request against the exact advertised contract."""

    if descriptor.descriptor_sha256 != request.observer_descriptor_sha256:
        raise ValueError("observer descriptor differs from the sealed request")
    support = descriptor.support_for(request.observer_contract_id)
    if support.contract_sha256 != request.observer_contract_sha256:
        raise ValueError("observer contract differs from the sealed request")
    if request.maximum_result_bytes > support.maximum_result_bytes:
        raise ValueError("observation request exceeds the observer contract result limit")
    try:
        Draft202012Validator(support.options_schema.document).validate(request.options)
    except JsonSchemaValidationError as exc:
        raise ValueError("observation request options violate their advertised schema") from exc
    return support


def require_semantic_validators(
    provider: SemanticValidatorProvider | None,
    descriptor: ObserverDescriptor,
) -> None:
    """Fail closed unless every advertised non-schema profile can be accepted locally."""

    for support in descriptor.contracts:
        profile = support.facts_semantics
        if profile == JSON_SCHEMA_ONLY_SEMANTIC_PROFILE:
            continue
        if provider is None or provider.resolve(profile.id, profile.profile_sha256) is None:
            raise ValueError(
                "semantic validator is unavailable for advertised observer profile "
                f"{profile.id}@{profile.profile_sha256}"
            )


def accept_observation_result(
    result: ObservationResult,
    request: ObservationRequest,
    descriptor: ObserverDescriptor,
    semantic_validators: SemanticValidatorProvider | None = None,
) -> None:
    """Apply the complete structural and semantic observer-result acceptance domain."""

    support = _validate_observation_result_structure(result, request, descriptor)
    if result.state != "observed":
        return
    assert result.facts is not None
    profile = support.facts_semantics
    if profile == JSON_SCHEMA_ONLY_SEMANTIC_PROFILE:
        return
    validator = (
        semantic_validators.resolve(profile.id, profile.profile_sha256)
        if semantic_validators is not None
        else None
    )
    if validator is None:
        raise ValueError(
            "semantic validator is unavailable for observer profile "
            f"{profile.id}@{profile.profile_sha256}"
        )
    validator(request, result.facts)


def _validate_observation_result_structure(
    result: ObservationResult,
    request: ObservationRequest,
    descriptor: ObserverDescriptor,
) -> ObserverContractSupport:
    support = validate_observation_request(request, descriptor)
    if result.request_id != request.request_id:
        raise ValueError("observation result does not bind the request")
    if (
        result.observer_contract_id != support.contract_id
        or result.observer_contract_sha256 != support.contract_sha256
        or result.observer.descriptor_sha256 != request.observer_descriptor_sha256
    ):
        raise ValueError("observation result does not bind the accepted observer contract")
    if result.subjects != request.subjects:
        raise ValueError("observation result subjects differ from the request")
    if result.state == "observed":
        if result.facts_schema != support.facts_schema or result.facts is None:
            raise ValueError("observation result uses an unexpected facts schema")
        try:
            Draft202012Validator(support.facts_schema.document).validate(result.facts)
        except JsonSchemaValidationError as exc:
            raise ValueError("observation facts violate their advertised schema") from exc
    encoded = canonical_json_bytes(result.model_dump(mode="json", exclude_none=True))
    if len(encoded) > request.maximum_result_bytes:
        raise ValueError("observation result exceeds the requested result-size limit")
    return support


__all__ = [
    "FactsSemanticValidator",
    "SemanticValidatorBinding",
    "SemanticValidatorProvider",
    "SemanticValidatorRegistry",
    "accept_observation_result",
    "require_semantic_validators",
    "validate_observation_request",
]
