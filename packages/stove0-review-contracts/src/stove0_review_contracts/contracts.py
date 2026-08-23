"""Review-specific semantic contracts kept outside stove0 core.

This package is an example of the intended extension model: it depends only on
released protocol packages. Observer and target implementations may live in
other repositories and implement these exact contracts without importing stove0
core or Riverhog server code.
"""

from __future__ import annotations

from stove0_observer_protocol import (
    JsonSchemaDocument,
    ObserverContract,
    ObserverContractPayload,
)
from stove0_target_protocol import (
    InputArtifactContract,
    OperationContract,
    OperationContractPayload,
    OutputArtifactContract,
)

MEDIA_SAMPLING_OBSERVATION_ID = "stove0.review.media-sampling/v1"
MEDIA_SAMPLING_OPTIONS_SCHEMA_ID = "stove0.review.media-sampling-options/v1"
MEDIA_SAMPLING_FACTS_SCHEMA_ID = "stove0.review.media-sampling-facts/v1"
REVIEW_MATERIALIZE_OPERATION_ID = "stove0.review.materialize/v1"
REVIEW_RCLONE_DELIVER_OPERATION_ID = "stove0.review.rclone-deliver/v1"
REVIEW_MATERIALIZE_INTENT_SCHEMA_ID = "stove0.review.materialize-intent/v1"
REVIEW_RCLONE_RECEIPT_SCHEMA_ID = "stove0.review.rclone-receipt/v1"

REVIEW_SOURCE_ROLE = "stove0.review.source/v1"
REVIEW_AUDIO_ROLE = "stove0.review.audio/v1"
REVIEW_INDEX_ROLE = "stove0.review.index/v1"
REVIEW_VIDEO_ROLE = "stove0.review.video/v1"

MEDIA_SAMPLING_OPTIONS_SCHEMA = JsonSchemaDocument.from_schema(
    MEDIA_SAMPLING_OPTIONS_SCHEMA_ID,
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)

MEDIA_SAMPLING_FACTS_SCHEMA = JsonSchemaDocument.from_schema(
    MEDIA_SAMPLING_FACTS_SCHEMA_ID,
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["artifacts"],
        "properties": {
            "artifacts": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": [
                        "artifact_id",
                        "duration_ms",
                        "sampleable_ranges",
                    ],
                    "properties": {
                        "artifact_id": {"type": "string", "minLength": 1},
                        "duration_ms": {"type": "integer", "minimum": 1},
                        "sampleable_ranges": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "required": ["start_ms", "duration_ms"],
                                "properties": {
                                    "start_ms": {"type": "integer", "minimum": 0},
                                    "duration_ms": {"type": "integer", "minimum": 1},
                                },
                                "additionalProperties": False,
                            },
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    },
)

REVIEW_MATERIALIZE_INTENT_SCHEMA = JsonSchemaDocument.from_schema(
    REVIEW_MATERIALIZE_INTENT_SCHEMA_ID,
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["sample_plan", "variant"],
        "properties": {
            "sample_plan": {
                "type": "object",
                "required": [
                    "format",
                    "selection_method",
                    "samples_per_artifact",
                    "window_duration_ms",
                    "windows",
                    "sample_plan_sha256",
                ],
                "properties": {
                    "format": {"const": "stove0-review-sample-plan/v1"},
                    "selection_method": {"const": "evenly-spaced/v1"},
                    "samples_per_artifact": {
                        "type": "integer",
                        "minimum": 1,
                    },
                    "window_duration_ms": {
                        "type": "integer",
                        "minimum": 1,
                    },
                    "windows": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["artifact_id", "start_ms", "duration_ms"],
                            "properties": {
                                "artifact_id": {"type": "string", "minLength": 1},
                                "start_ms": {"type": "integer", "minimum": 0},
                                "duration_ms": {"type": "integer", "minimum": 1},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "sample_plan_sha256": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                    },
                },
                "additionalProperties": False,
            },
            "variant": {
                "type": "object",
                "required": ["id", "portable_intent"],
                "properties": {
                    "id": {
                        "type": "string",
                        "pattern": "^[a-z0-9](?:[a-z0-9._-]{0,158}[a-z0-9])?$",
                    },
                    "portable_intent": {"type": "object"},
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    },
)

MEDIA_SAMPLING_OBSERVER_CONTRACT = ObserverContract.seal(
    ObserverContractPayload(
        id=MEDIA_SAMPLING_OBSERVATION_ID,
        options_schema=MEDIA_SAMPLING_OPTIONS_SCHEMA,
        facts_schema=MEDIA_SAMPLING_FACTS_SCHEMA,
        maximum_result_bytes=256 * 1024,
    )
)

REVIEW_MATERIALIZE_OPERATION = OperationContract.seal(
    OperationContractPayload(
        id=REVIEW_MATERIALIZE_OPERATION_ID,
        intent_schema=REVIEW_MATERIALIZE_INTENT_SCHEMA,
        inputs=(
            InputArtifactContract(
                role=REVIEW_SOURCE_ROLE,
                minimum=1,
                allowed_dispositions=("preserved", "transformed"),
            ),
        ),
        outputs=(
            OutputArtifactContract(
                role=REVIEW_AUDIO_ROLE,
                minimum=0,
                derived_from_roles=(REVIEW_SOURCE_ROLE,),
            ),
            OutputArtifactContract(
                role=REVIEW_INDEX_ROLE,
                minimum=1,
                maximum=1,
                derived_from_roles=(REVIEW_SOURCE_ROLE,),
            ),
            OutputArtifactContract(
                role=REVIEW_VIDEO_ROLE,
                minimum=0,
                derived_from_roles=(REVIEW_SOURCE_ROLE,),
            ),
        ),
        source_retirement_permitted=False,
    )
)

REVIEW_RCLONE_RECEIPT_SCHEMA = JsonSchemaDocument.from_schema(
    REVIEW_RCLONE_RECEIPT_SCHEMA_ID,
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": [
            "format",
            "destination_identity",
            "delivery_id",
            "artifact_manifest_sha256",
            "artifact_count",
            "total_bytes",
        ],
        "properties": {
            "format": {"const": "stove0-review-rclone-receipt/v1"},
            "destination_identity": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "delivery_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "artifact_manifest_sha256": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
            "artifact_count": {"type": "integer", "minimum": 1},
            "total_bytes": {"type": "integer", "minimum": 0},
        },
        "additionalProperties": False,
    },
)

REVIEW_RCLONE_DELIVER_OPERATION = OperationContract.seal(
    OperationContractPayload(
        id=REVIEW_RCLONE_DELIVER_OPERATION_ID,
        result_kind="external-effect",
        intent_schema=REVIEW_MATERIALIZE_INTENT_SCHEMA,
        inputs=(
            InputArtifactContract(
                role=REVIEW_SOURCE_ROLE,
                minimum=1,
                allowed_dispositions=None,
            ),
        ),
        effect_receipt_schema=REVIEW_RCLONE_RECEIPT_SCHEMA,
        source_retirement_permitted=False,
    )
)


__all__ = [
    "MEDIA_SAMPLING_FACTS_SCHEMA",
    "MEDIA_SAMPLING_FACTS_SCHEMA_ID",
    "MEDIA_SAMPLING_OBSERVATION_ID",
    "MEDIA_SAMPLING_OBSERVER_CONTRACT",
    "MEDIA_SAMPLING_OPTIONS_SCHEMA",
    "MEDIA_SAMPLING_OPTIONS_SCHEMA_ID",
    "REVIEW_AUDIO_ROLE",
    "REVIEW_INDEX_ROLE",
    "REVIEW_MATERIALIZE_INTENT_SCHEMA",
    "REVIEW_MATERIALIZE_INTENT_SCHEMA_ID",
    "REVIEW_MATERIALIZE_OPERATION",
    "REVIEW_MATERIALIZE_OPERATION_ID",
    "REVIEW_RCLONE_DELIVER_OPERATION",
    "REVIEW_RCLONE_DELIVER_OPERATION_ID",
    "REVIEW_RCLONE_RECEIPT_SCHEMA",
    "REVIEW_RCLONE_RECEIPT_SCHEMA_ID",
    "REVIEW_SOURCE_ROLE",
    "REVIEW_VIDEO_ROLE",
]
