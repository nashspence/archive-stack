"""Dependency-light public contracts for external stove0 content observers.

The authoritative implementations live in :mod:`stove0_protocol.models` so
workflow plans and observer evidence share exact model classes. This package is
the sole top-level observer-author import surface and intentionally excludes the
HTTP client, Riverhog data plane, and stove0 core.
"""

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

from stove0_observer_protocol.validation import (
    validate_observation_request,
    validate_observation_result,
)

__all__ = [
    "ARTIFACT_ID_PATTERN",
    "OBSERVER_PROTOCOL",
    "JSON_SCHEMA_ONLY_SEMANTIC_PROFILE",
    "OBSERVATION_REQUEST_FORMAT",
    "OBSERVATION_RESULT_FORMAT",
    "RIVERHOG_CAPABILITY_TRANSPORT",
    "SHA256_PATTERN",
    "ArtifactSubject",
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
    "SemanticValidationProfile",
    "SemanticValidationProfilePayload",
    "Sha256",
    "canonical_json_bytes",
    "canonical_json_sha256",
    "validate_observation_request",
    "validate_observation_result",
]
