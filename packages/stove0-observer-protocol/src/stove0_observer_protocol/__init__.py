"""Dependency-light public contracts for external stove0 content observers.

The authoritative implementations live in :mod:`stove0_protocol.models` so
workflow plans and observer evidence share exact model classes. This package is
the sole top-level observer-author import surface and intentionally excludes the
HTTP client, Riverhog data plane, and stove0 core.
"""

from http_api_contracts import HttpErrorContract, HttpOperationContract
from stove0_protocol.models import (
    ARTIFACT_ID_PATTERN,
    JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
    OBSERVATION_REQUEST_FORMAT,
    OBSERVATION_RESULT_FORMAT,
    OBSERVER_PROTOCOL,
    RIVERHOG_CAPABILITY_TRANSPORT,
    SHA256_PATTERN,
    ArtifactSubject,
    CollectionRootRef,
    JsonSchemaDocument,
    ObservationEvidence,
    ObservationFailure,
    ObservationInapplicable,
    ObservationInvocation,
    ObservationRequest,
    ObservationRequestPayload,
    ObservationResult,
    ObservationResultPayload,
    ObservationState,
    ObserverContract,
    ObserverContractPayload,
    ObserverContractSupport,
    ObserverDescriptor,
    ObserverDescriptorPayload,
    ObserverImplementation,
    ObserverRuntimeAuthority,
    SemanticId,
    SemanticValidationProfile,
    SemanticValidationProfilePayload,
    Sha256,
    canonical_json_bytes,
    canonical_json_sha256,
)

from stove0_observer_protocol.conformance import (
    SemanticFactsConformanceVector,
    SemanticFactsConformanceVectors,
)
from stove0_observer_protocol.validation import (
    FactsSemanticValidator,
    SemanticValidatorBinding,
    SemanticValidatorProvider,
    SemanticValidatorRegistry,
    accept_observation_result,
    require_semantic_validators,
    validate_observation_request,
)

OBSERVER_HTTP_OPERATIONS = (
    HttpOperationContract(
        "GET",
        "/v1/observer",
        response_type=ObserverDescriptor,
        errors=(
            HttpErrorContract("bad_request", 400),
            HttpErrorContract("unauthorized", 401),
            HttpErrorContract("observer_failed", 500),
        ),
    ),
    HttpOperationContract(
        "POST",
        "/v1/observe",
        ObservationInvocation,
        ObservationResult,
        "json",
        errors=(
            HttpErrorContract("invalid_observation_request", 400),
            HttpErrorContract("unauthorized", 401),
            HttpErrorContract("request_too_large", 413),
            HttpErrorContract("observer_failed", 500),
        ),
    ),
)

__all__ = [
    "ARTIFACT_ID_PATTERN",
    "OBSERVER_PROTOCOL",
    "OBSERVER_HTTP_OPERATIONS",
    "JSON_SCHEMA_ONLY_SEMANTIC_PROFILE",
    "OBSERVATION_REQUEST_FORMAT",
    "OBSERVATION_RESULT_FORMAT",
    "RIVERHOG_CAPABILITY_TRANSPORT",
    "SHA256_PATTERN",
    "ArtifactSubject",
    "FactsSemanticValidator",
    "CollectionRootRef",
    "JsonSchemaDocument",
    "ObservationEvidence",
    "ObservationFailure",
    "ObservationInapplicable",
    "ObservationInvocation",
    "ObservationRequest",
    "ObservationRequestPayload",
    "ObservationResult",
    "ObservationResultPayload",
    "ObservationState",
    "ObserverContract",
    "ObserverContractPayload",
    "ObserverContractSupport",
    "ObserverDescriptor",
    "ObserverDescriptorPayload",
    "ObserverImplementation",
    "ObserverRuntimeAuthority",
    "SemanticId",
    "SemanticFactsConformanceVector",
    "SemanticFactsConformanceVectors",
    "SemanticValidatorBinding",
    "SemanticValidatorProvider",
    "SemanticValidatorRegistry",
    "SemanticValidationProfile",
    "SemanticValidationProfilePayload",
    "Sha256",
    "canonical_json_bytes",
    "canonical_json_sha256",
    "accept_observation_result",
    "require_semantic_validators",
    "validate_observation_request",
]
