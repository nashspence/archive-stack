from __future__ import annotations

import subprocess
import sys

from stove0_observer_protocol import (
    JsonSchemaDocument,
    ObserverContract,
    ObserverContractPayload,
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
        )
    )
    assert contract.contract_sha256

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
