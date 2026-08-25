"""Executable validation for the public Stove0 observer contract."""

from __future__ import annotations

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from stove0_protocol.models import (
    ObservationRequest,
    ObservationResult,
    ObserverContractSupport,
    ObserverDescriptor,
    canonical_json_bytes,
)


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


def validate_observation_result(
    result: ObservationResult,
    request: ObservationRequest,
    descriptor: ObserverDescriptor,
) -> None:
    """Validate one result against its request and advertised fact contract."""

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


__all__ = ["validate_observation_request", "validate_observation_result"]
