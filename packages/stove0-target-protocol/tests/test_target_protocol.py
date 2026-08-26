from __future__ import annotations

import subprocess
import sys

import pytest
from stove0_protocol import JsonSchemaDocument
from stove0_target_protocol import (
    JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
    InputArtifactContract,
    OperationContract,
    OperationContractPayload,
    OutputArtifactContract,
)


def test_target_contract_models_are_importable_without_runtime_support() -> None:
    operation = OperationContract.seal(
        OperationContractPayload(
            id="fixture.copy/v1",
            intent_schema=JsonSchemaDocument.from_schema(
                "fixture.copy-intent/v1",
                {"type": "object", "additionalProperties": False},
            ),
            intent_semantics=JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
            inputs=(
                InputArtifactContract(
                    role="fixture.source/v1",
                    allowed_dispositions=("transformed",),
                ),
            ),
            outputs=(
                OutputArtifactContract(
                    role="fixture.output/v1",
                    derived_from_roles=("fixture.source/v1",),
                ),
            ),
        )
    )
    assert operation.contract_sha256

    forbidden = {
        "httpx",
        "riverhog_api_client",
        "riverhog_transform_sdk",
        "stove0_target_support",
    }
    code = (
        "import sys\n"
        "import stove0_target_protocol\n"
        f"forbidden = {forbidden!r}\n"
        "loaded = forbidden & set(sys.modules)\n"
        "assert not loaded, sorted(loaded)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_operation_result_kind_owns_collection_disposition_semantics() -> None:
    schema = JsonSchemaDocument.from_schema(
        "fixture.effect-intent/v1",
        {"type": "object", "additionalProperties": False},
    )
    receipt = JsonSchemaDocument.from_schema(
        "fixture.effect-receipt/v1",
        {"type": "object", "additionalProperties": False},
    )
    with pytest.raises(ValueError, match="cannot declare input dispositions"):
        OperationContractPayload(
            id="fixture.effect/v1",
            result_kind="external-effect",
            intent_schema=schema,
            intent_semantics=JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
            inputs=(
                InputArtifactContract(
                    role="fixture.source/v1",
                    allowed_dispositions=("preserved",),
                ),
            ),
            effect_receipt_schema=receipt,
        )
    with pytest.raises(ValueError, match="requires explicit input dispositions"):
        OperationContractPayload(
            id="fixture.collection/v1",
            intent_schema=schema,
            intent_semantics=JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
            inputs=(InputArtifactContract(role="fixture.source/v1"),),
            outputs=(
                OutputArtifactContract(
                    role="fixture.output/v1",
                    derived_from_roles=("fixture.source/v1",),
                ),
            ),
        )
