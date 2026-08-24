from __future__ import annotations

import subprocess
import sys

import pytest
from pydantic import ValidationError
from stove0_operator_contracts import EvaluationView, WorkView
from stove0_protocol import (
    CollectionRootRef,
    EvaluationDefinition,
    EvaluationDefinitionPayload,
    EvaluationMatrix,
    EvaluationMatrixPayload,
    EvaluationVariant,
    RecipeRef,
    WorkIdentity,
    WorkPayload,
)


def _work() -> WorkIdentity:
    return WorkIdentity.seal(
        WorkPayload(
            recipe=RecipeRef(id="fixture.recipe/v1", revision=1, sha256="1" * 64),
            inputs=(
                CollectionRootRef(
                    collection_id=1,
                    manifest_sha256="2" * 64,
                    content_identity="3" * 64,
                ),
            ),
        )
    )


def _evaluation() -> EvaluationDefinition:
    matrix = EvaluationMatrix.seal(
        EvaluationMatrixPayload(variants=(EvaluationVariant(id="variant-a"),))
    )
    return EvaluationDefinition.seal(
        EvaluationDefinitionPayload(
            purpose="trial",
            recipe=RecipeRef(id="fixture.recipe/v1", revision=1, sha256="1" * 64),
            inputs=_work().inputs,
            matrix=matrix,
        )
    )


def test_work_projection_adds_and_verifies_the_public_identity() -> None:
    work = _work()
    view = WorkView.from_record(
        {
            "format": "stove0-work-record/v1",
            "work": work.model_dump(mode="json"),
            "phase": "eligible",
            "revision": 1,
        }
    )

    assert view.format == "stove0-work-view/v1"
    assert view.work_id == work.work_id
    with pytest.raises(ValidationError, match="work view ID differs"):
        WorkView.model_validate({**view.model_dump(mode="json"), "work_id": "f" * 64})


def test_evaluation_projection_adds_and_verifies_the_public_identity() -> None:
    definition = _evaluation()
    child = definition.child_work("variant-a")
    view = EvaluationView.from_record(
        {
            "format": "stove0-evaluation-record/v1",
            "definition": definition.model_dump(mode="json"),
            "phase": "running",
            "revision": 1,
            "children": [
                {
                    "variant_id": "variant-a",
                    "work_id": child.work_id,
                    "state": "pending",
                }
            ],
        }
    )

    assert view.format == "stove0-evaluation-view/v1"
    assert view.evaluation_id == definition.evaluation_id


def test_operator_contracts_do_not_load_server_or_transport_implementations() -> None:
    forbidden = {
        "httpx",
        "stove0_api_client",
        "stove0_core",
        "stove0_observer_support",
        "stove0_target_support",
    }
    code = (
        "import sys\n"
        "import stove0_operator_contracts\n"
        f"forbidden = {forbidden!r}\n"
        "loaded = forbidden & set(sys.modules)\n"
        "assert not loaded, sorted(loaded)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
