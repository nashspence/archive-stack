"""Target-owned operations and wire schemas for maintained review work."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files

from stove0_protocol import (
    JsonSchemaDocument,
    SemanticValidationProfile,
    SemanticValidationProfilePayload,
)
from stove0_target_protocol import (
    InputArtifactContract,
    OperationContract,
    OperationContractPayload,
    OutputArtifactContract,
    SemanticIntentConformanceVectors,
)

from stove0_review_target_contracts.models import ReviewMaterializeIntent

REVIEW_MATERIALIZE_OPERATION_ID = "stove0.review.materialize/v1"
REVIEW_RCLONE_DELIVER_OPERATION_ID = "stove0.review.rclone-deliver/v1"
REVIEW_MATERIALIZE_INTENT_SCHEMA_ID = "stove0.review.materialize-intent/v1"
REVIEW_RCLONE_RECEIPT_SCHEMA_ID = "stove0.review.rclone-receipt/v1"

REVIEW_SOURCE_ROLE = "stove0.review.source/v1"
REVIEW_AUDIO_ROLE = "stove0.review.audio/v1"
REVIEW_INDEX_ROLE = "stove0.review.index/v1"
REVIEW_VIDEO_ROLE = "stove0.review.video/v1"

REVIEW_MATERIALIZE_INTENT_SCHEMA = JsonSchemaDocument.from_schema(
    REVIEW_MATERIALIZE_INTENT_SCHEMA_ID,
    ReviewMaterializeIntent.model_json_schema(),
)
REVIEW_MATERIALIZE_INTENT_CONFORMANCE_VECTORS = (
    SemanticIntentConformanceVectors.model_validate_json(
        files("stove0_review_target_contracts")
        .joinpath("vectors/materialize-intent-v1.json")
        .read_text(encoding="utf-8")
    )
)
REVIEW_MATERIALIZE_INTENT_SEMANTICS = SemanticValidationProfile.seal(
    SemanticValidationProfilePayload(
        id="stove0.review.materialize-intent-semantics/v1",
        rules=(
            "stove0.review.sample-plan.exact-declared-shape/v1",
            "stove0.review.sample-plan.identity-verification/v1",
        ),
        conformance_vectors_sha256=REVIEW_MATERIALIZE_INTENT_CONFORMANCE_VECTORS.sha256,
    )
)


def validate_review_materialize_intent(intent: Mapping[str, object]) -> None:
    ReviewMaterializeIntent.model_validate(dict(intent))


REVIEW_MATERIALIZE_OPERATION = OperationContract.seal(
    OperationContractPayload(
        id=REVIEW_MATERIALIZE_OPERATION_ID,
        intent_schema=REVIEW_MATERIALIZE_INTENT_SCHEMA,
        intent_semantics=REVIEW_MATERIALIZE_INTENT_SEMANTICS,
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
            "artifact_archive_root_sha256",
            "artifact_count",
            "total_bytes",
        ],
        "properties": {
            "format": {"const": "stove0-review-rclone-receipt/v1"},
            "destination_identity": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "delivery_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "artifact_archive_root_sha256": {
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
        intent_semantics=REVIEW_MATERIALIZE_INTENT_SEMANTICS,
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
    "REVIEW_AUDIO_ROLE",
    "REVIEW_INDEX_ROLE",
    "REVIEW_MATERIALIZE_INTENT_SCHEMA",
    "REVIEW_MATERIALIZE_INTENT_CONFORMANCE_VECTORS",
    "REVIEW_MATERIALIZE_INTENT_SCHEMA_ID",
    "REVIEW_MATERIALIZE_INTENT_SEMANTICS",
    "REVIEW_MATERIALIZE_OPERATION",
    "REVIEW_MATERIALIZE_OPERATION_ID",
    "REVIEW_RCLONE_DELIVER_OPERATION",
    "REVIEW_RCLONE_DELIVER_OPERATION_ID",
    "REVIEW_RCLONE_RECEIPT_SCHEMA",
    "REVIEW_RCLONE_RECEIPT_SCHEMA_ID",
    "REVIEW_SOURCE_ROLE",
    "REVIEW_VIDEO_ROLE",
    "validate_review_materialize_intent",
]
