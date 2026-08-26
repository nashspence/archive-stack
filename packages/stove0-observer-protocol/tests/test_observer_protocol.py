from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
import stove0_protocol
from stove0_observer_protocol import (
    JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
    JsonSchemaDocument,
    ObservationRequest,
    ObserverContract,
    ObserverContractPayload,
    SemanticValidationProfile,
    SemanticValidationProfilePayload,
    validate_observation_request,
)
from stove0_protocol import models as shared_models

_OBSERVER_AUTHOR_SYMBOLS = frozenset(
    {
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
        "SemanticFactsConformanceVector",
        "SemanticFactsConformanceVectors",
        "SemanticValidatorBinding",
        "SemanticValidatorProvider",
        "SemanticValidatorRegistry",
        "accept_observation_result",
        "require_semantic_validators",
        "validate_observation_request",
    }
)


def test_observer_contract_models_are_importable_without_runtime_support() -> None:
    contract = ObserverContract.seal(
        ObserverContractPayload(
            id="fixture.observation/v1",
            options_schema=JsonSchemaDocument.from_schema(
                "fixture.options/v1",
                {"type": "object", "additionalProperties": False},
            ),
            facts_schema=JsonSchemaDocument.from_schema(
                "fixture.facts/v1",
                {"type": "object", "additionalProperties": False},
            ),
            facts_semantics=JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
        )
    )
    assert contract.contract_sha256
    assert callable(validate_observation_request)

    forbidden = {
        "httpx",
        "riverhog_api_client",
        "riverhog_transform_sdk",
        "stove0_core",
        "stove0_observer_support",
    }
    code = (
        "import sys\n"
        "import stove0_observer_protocol\n"
        f"forbidden = {forbidden!r}\n"
        "loaded = forbidden & set(sys.modules)\n"
        "assert not loaded, sorted(loaded)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_observer_author_surface_reuses_one_model_implementation() -> None:
    assert ObservationRequest is shared_models.ObservationRequest
    assert ObserverContract is shared_models.ObserverContract
    assert _OBSERVER_AUTHOR_SYMBOLS.isdisjoint(stove0_protocol.__all__)


def test_only_the_exact_schema_only_profile_can_omit_conformance_vectors() -> None:
    altered = SemanticValidationProfile.seal(
        SemanticValidationProfilePayload(
            id=JSON_SCHEMA_ONLY_SEMANTIC_PROFILE.id,
            rules=("fixture.different-rule/v1",),
        )
    )
    with pytest.raises(ValueError, match="conformance-vector identity"):
        ObserverContractPayload(
            id="fixture.observation/v1",
            options_schema=JsonSchemaDocument.from_schema(
                "fixture.options/v1",
                {"type": "object", "additionalProperties": False},
            ),
            facts_schema=JsonSchemaDocument.from_schema(
                "fixture.facts/v1",
                {"type": "object", "additionalProperties": False},
            ),
            facts_semantics=altered,
        )


def test_repository_consumers_use_the_observer_author_surface() -> None:
    root = Path(__file__).resolve().parents[3]
    violations: list[str] = []
    for base in ("companions", "packages", "reference", "tests"):
        for path in root.joinpath(base).rglob("*.py"):
            implementation_models = (
                path.name == "models.py" and path.parent.name == "stove0_protocol"
            )
            if path == Path(__file__) or implementation_models:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.module != "stove0_protocol":
                    continue
                leaked = sorted(
                    alias.name for alias in node.names if alias.name in _OBSERVER_AUTHOR_SYMBOLS
                )
                if leaked:
                    violations.append(f"{path.relative_to(root)}: {', '.join(leaked)}")
    assert not violations, violations
