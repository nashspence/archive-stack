from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/contract_freeze.py"
ARTIFACT = REPO_ROOT / "qualification/contracts/riverhog-v1.json"
TRACE_ARTIFACT = REPO_ROOT / "qualification/contracts/riverhog-v1-trace.json"


def load_script() -> ModuleType:
    if str(SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("riverhog_contract_freeze", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_checked_contract_freeze_matches_every_executable_authority() -> None:
    module = load_script()

    rendered = module._render()

    assert ARTIFACT.read_text(encoding="utf-8") == rendered
    projection = json.loads(rendered)
    assert TRACE_ARTIFACT.read_text(encoding="utf-8") == module._render_trace(projection)
    assert projection["schema"] == "riverhog-contract-freeze/v1"
    assert set(projection) == {"schema", "series", "boundaries", "external_contract"}
    boundaries = projection["boundaries"]
    assert set(boundaries) == {
        "components",
        "entry_point_extensions",
        "process_extensions",
        "reference_policy",
        "runtime_images",
    }
    components = boundaries["components"]
    assert len(components) == 74
    roles = {component["distribution"]: component["role"] for component in components}
    extension_points = boundaries["entry_point_extensions"]
    assert {point["group"] for point in extension_points} == {
        "gogurt.listener-host-providers",
        "gogurt.mounted-volume-providers",
        "riverhog.provenance-contracts",
        "riverhog.provenance-observers",
        "stove0.observer-semantic-validators",
    }
    assert all(roles[point["owner"]] == "reusable_library" for point in extension_points)
    assert all(
        roles[provider["distribution"]] == "reference_component"
        for point in extension_points
        for provider in point["providers"]
    )
    process_extensions = boundaries["process_extensions"]
    assert {protocol for point in process_extensions for protocol in point["protocols"]} == {
        "riverhog-storage-adapter/v1",
        "stove0-content-observer/v1",
        "stove0-effect-target/v1",
        "stove0-review-sampler/v1",
        "stove0-transform-target/v1",
    }
    assert all(point["providers"] for point in process_extensions)
    assert all(
        roles[provider["distribution"]] == "reference_component"
        for point in process_extensions
        for provider in point["providers"]
    )
    external = projection["external_contract"]
    assert set(external) == {
        "cli",
        "configuration_documents",
        "configuration_environment",
        "configuration_environment_patterns",
        "durable_state",
        "extents",
        "http_openapi",
        "operations",
        "protocol_schemas",
        "python",
        "release",
    }
    assert set(external["cli"]) == {
        "gogurt",
        "riverhog",
        "riverhog-ftp-adapter",
        "riverhog-recover",
        "stove0",
    }
    assert set(external["cli"]["riverhog"]["commands"]) == {
        "app",
        "archive",
        "collection",
        "event",
        "find",
        "local",
        "retrieval",
        "tag",
    }
    assert "list" in external["cli"]["riverhog"]["commands"]["collection"]["commands"]
    assert set(external["http_openapi"]) == {
        "riverhog",
        "riverhog-ftp-adapter",
        "stove0",
    }
    assert len(external["operations"]) == 152
    assert len(external["python"]) == 25
    assert len(external["durable_state"]["owners"]) == 8
    extents = external["extents"]
    assert extents["schema"] == "riverhog-extent-contract/v1"
    assert extents["coverage"]["classified"] == extents["coverage"]["discovered"]
    assert extents["coverage"]["missing"] == 0
    assert extents["coverage"]["duplicate"] == 0
    assert extents["coverage"]["stale"] == 0
    assert extents["coverage"]["undecided"] == 0
    trace = json.loads(TRACE_ARTIFACT.read_text(encoding="utf-8"))
    assert trace["schema"] == "riverhog-contract-trace/v1"
    assert trace["coverage"]["source_authorities"] == len(trace["sources"])
    assert trace["coverage"]["source_kinds"] == {
        "cli": 5,
        "configuration": 6,
        "configuration-environment": 115,
        "openapi": 3,
        "protocol": 34,
        "python": 25,
        "release": 1,
        "state": 8,
    }
    assert trace["coverage"]["extent_decisions"] == len(extents["decisions"])
    assert trace["coverage"]["extent_source_links"] == len(extents["decisions"])


def test_extent_semantic_diff_is_grouped_by_owning_boundary() -> None:
    module = load_script()
    previous = {
        "external_contract": {
            "extents": {
                "decisions": [
                    {"id": "kept", "owner": "riverhog", "policy": "fixed"},
                    {"id": "changed", "owner": "stove0", "policy": "fixed"},
                    {"id": "removed", "owner": "stove0", "policy": "fixed"},
                ]
            }
        }
    }
    current = {
        "external_contract": {
            "extents": {
                "decisions": [
                    {"id": "kept", "owner": "riverhog", "policy": "fixed"},
                    {"id": "changed", "owner": "stove0", "policy": "contract_max"},
                    {"id": "added", "owner": "riverhog", "policy": "fixed"},
                ]
            }
        }
    }

    assert module._extent_diff(previous, current) == {
        "riverhog": {"added": 1, "changed": 0, "removed": 0},
        "stove0": {"added": 0, "changed": 1, "removed": 1},
    }
