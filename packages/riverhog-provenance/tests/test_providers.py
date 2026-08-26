from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import riverhog_provenance.providers as provider_module
import riverhog_provenance.schema as schema_module
from riverhog_provenance import (
    PROVENANCE_OBSERVER_BINDING_FORMAT,
    ProvenanceObserverBinding,
    create_observation_journal,
    list_provenance_observers,
    resolve_provenance_observer,
)
from riverhog_provenance.model import ObservationRequest, ObservationResult
from riverhog_provenance_contracts import ProvenanceContractBinding


class ExternalObserver:
    platform_family = "external-fixture"

    def observe(self, _request: ObservationRequest) -> ObservationResult:
        raise AssertionError("provider resolution must not perform observation")


@dataclass
class FixtureEntryPoint:
    name: str
    value: str
    binding: object
    loaded: bool = False
    dist: Any = None

    def load(self) -> object:
        self.loaded = True
        return self.binding


def _external_bindings() -> tuple[ProvenanceContractBinding, ProvenanceObserverBinding]:
    contract = ProvenanceContractBinding(
        contract_id="example-bsd-file-state/v1",
        schemas=(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://example.invalid/provenance/bsd-state/v1",
                "type": "object",
                "additionalProperties": False,
            },
        ),
    )
    observer = ProvenanceObserverBinding(
        observer_id="example-bsd-observer/v1",
        contract_provider="example-bsd",
        contract_id=contract.contract_id,
        contract_sha256=contract.contract_sha256,
        factory=ExternalObserver,
    )
    return contract, observer


def test_external_provider_resolves_without_a_first_party_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, observer = _external_bindings()
    entries = {
        provider_module.PROVENANCE_OBSERVER_ENTRY_POINT_GROUP: (
            FixtureEntryPoint("example-bsd", "example_observer:BINDING", observer),
        ),
        provider_module.PROVENANCE_CONTRACT_ENTRY_POINT_GROUP: (
            FixtureEntryPoint("example-bsd", "example_contract:BINDING", contract),
        ),
    }
    monkeypatch.setattr(
        provider_module.importlib.metadata,
        "entry_points",
        lambda *, group: entries.get(group, ()),
    )

    resolved = resolve_provenance_observer("example-bsd")

    assert resolved.binding.observer_id == "example-bsd-observer/v1"
    assert resolved.contract.contract_sha256 == contract.contract_sha256
    assert resolved.create().platform_family == "external-fixture"
    assert resolved.as_dict()["format"] == PROVENANCE_OBSERVER_BINDING_FORMAT


def test_installed_external_distribution_composes_without_workspace_registration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "external_contract.py").write_text(
        "\n".join(
            (
                "from riverhog_provenance_contracts import ProvenanceContractBinding",
                "BINDING = ProvenanceContractBinding(",
                "    contract_id='external-freebsd-state/v1',",
                "    schemas=({'$schema': 'https://json-schema.org/draft/2020-12/schema',",
                "              '$id': 'https://example.invalid/freebsd-state/v1',",
                "              'type': 'object'},),",
                ")",
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / "external_observer.py").write_text(
        "\n".join(
            (
                "from external_contract import BINDING as CONTRACT",
                "from riverhog_provenance import ProvenanceObserverBinding",
                "class Observer:",
                "    platform_family = 'freebsd'",
                "    def observe(self, request):",
                "        raise AssertionError('resolution must not observe')",
                "BINDING = ProvenanceObserverBinding(",
                "    observer_id='external-freebsd-observer/v1',",
                "    contract_provider='external-freebsd',",
                "    contract_id=CONTRACT.contract_id,",
                "    contract_sha256=CONTRACT.contract_sha256,",
                "    factory=Observer,",
                ")",
            )
        ),
        encoding="utf-8",
    )
    metadata = tmp_path / "external_riverhog_provenance-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: external-riverhog-provenance\nVersion: 1.0\n",
        encoding="utf-8",
    )
    (metadata / "entry_points.txt").write_text(
        "\n".join(
            (
                "[riverhog.provenance-contracts]",
                "external-freebsd = external_contract:BINDING",
                "[riverhog.provenance-observers]",
                "external-freebsd = external_observer:BINDING",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    resolved = resolve_provenance_observer("external-freebsd")

    assert resolved.metadata.distribution == "external-riverhog-provenance"
    assert resolved.metadata.version == "1.0"
    assert resolved.create().platform_family == "freebsd"


def test_provider_listing_does_not_execute_installed_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, observer = _external_bindings()
    entry = FixtureEntryPoint("example-bsd", "example_observer:BINDING", observer)
    monkeypatch.setattr(
        provider_module.importlib.metadata,
        "entry_points",
        lambda *, group: (
            (entry,) if group == provider_module.PROVENANCE_OBSERVER_ENTRY_POINT_GROUP else ()
        ),
    )

    assert [item.name for item in list_provenance_observers()] == ["example-bsd"]
    assert entry.loaded is False


def test_observer_and_contract_identity_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, observer = _external_bindings()
    wrong = ProvenanceContractBinding(
        contract_id=contract.contract_id,
        schemas=(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://example.invalid/provenance/different/v1",
                "type": "string",
            },
        ),
    )
    entries = {
        provider_module.PROVENANCE_OBSERVER_ENTRY_POINT_GROUP: (
            FixtureEntryPoint("example-bsd", "example_observer:BINDING", observer),
        ),
        provider_module.PROVENANCE_CONTRACT_ENTRY_POINT_GROUP: (
            FixtureEntryPoint("example-bsd", "example_contract:BINDING", wrong),
        ),
    }
    monkeypatch.setattr(
        provider_module.importlib.metadata,
        "entry_points",
        lambda *, group: entries.get(group, ()),
    )

    with pytest.raises(ValueError, match="identities disagree"):
        resolve_provenance_observer("example-bsd")


def test_contract_binding_identity_is_deterministic_and_isolated() -> None:
    source = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.invalid/provenance/state/v1",
        "type": "object",
    }
    first = ProvenanceContractBinding(contract_id="example/v1", schemas=(source,))
    source["type"] = "string"
    second = ProvenanceContractBinding(
        contract_id="example/v1",
        schemas=(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://example.invalid/provenance/state/v1",
                "type": "object",
            },
        ),
    )

    assert first.contract_sha256 == second.contract_sha256
    assert next(iter(first.schemas.values()))["type"] == "object"


def test_contract_pack_validation_resolves_references_within_the_exact_pack() -> None:
    common_id = "https://example.invalid/provenance/common/v1"
    state_id = "https://example.invalid/provenance/state/v1"
    contract = ProvenanceContractBinding(
        contract_id="example-references/v1",
        schemas=(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": common_id,
                "type": "string",
                "const": "accepted",
            },
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": state_id,
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"$ref": common_id}},
            },
        ),
    )
    fragment: Any = {
        "extensions": [
            {
                "id": "urn:uuid:00000000-0000-4000-8000-000000000675",
                "property": "https://example.invalid/property/v1",
                "subject_id": "urn:uuid:00000000-0000-4000-8000-000000000676",
                "value": {"type": "json", "schema": state_id, "data": {"value": "accepted"}},
            }
        ]
    }

    schema_module.validate_embedded_typed_values(
        fragment,
        contract_schemas=contract.schemas,
        require_known=True,
    )
    fragment["extensions"][0]["value"]["data"]["value"] = "rejected"
    with pytest.raises(ValueError, match="Observer-defined typed JSON value is invalid"):
        schema_module.validate_embedded_typed_values(
            fragment,
            contract_schemas=contract.schemas,
            require_known=True,
        )


def test_first_party_reference_uses_the_public_composition_and_contract_path(
    tmp_path: Path,
) -> None:
    selector = (
        "riverhog-linux"
        if sys.platform.startswith("linux")
        else "riverhog-macos"
        if sys.platform == "darwin"
        else "riverhog-windows"
    )
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"provider composition proof")
    resolved = resolve_provenance_observer(selector)

    journal = create_observation_journal(
        payload,
        relative_path=payload.name,
        host_id="urn:uuid:00000000-0000-4000-8000-000000000675",
        agent_name="provider-composition-test",
        agent_version="1.0.0",
        observer=resolved.create(),
    )

    assert journal.startswith(b"\x1e{")
    assert resolved.binding.contract_provider == selector
    frames = [json.loads(line) for line in journal.replace(b"\x1e", b"").splitlines() if line]
    capture_frame = next(
        frame for frame in frames if frame.get("body", {}).get("assertions", {}).get("captures")
    )
    observer = capture_frame["body"]["assertions"]["captures"][0]["detail"]["provenance_observer"]
    assert observer == resolved.observer_reference()
