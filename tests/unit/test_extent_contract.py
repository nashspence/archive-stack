from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/contract_freeze.py"
ARTIFACT = REPO_ROOT / "qualification/contracts/riverhog-v1.json"
TRACE_ARTIFACT = REPO_ROOT / "qualification/contracts/riverhog-v1-trace.json"


def load_script() -> ModuleType:
    if str(SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("riverhog_extent_freeze", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _resolve_pointer(document: object, pointer: str) -> object:
    current = document
    assert pointer.startswith("/")
    for encoded in pointer[1:].split("/"):
        part = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(part)]
        else:
            assert isinstance(current, dict)
            current = current[part]
    return current


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def test_extent_projection_is_exhaustive_source_linked_and_self_identifying() -> None:
    projection = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    extents = projection["external_contract"]["extents"]
    decisions: list[dict[str, Any]] = extents["decisions"]
    content = {key: value for key, value in extents.items() if key != "sha256"}

    assert extents["sha256"] == _canonical_sha256(content)
    assert extents["coverage"]["classified"] == len(decisions)
    assert extents["coverage"]["discovered"] == len(decisions)
    assert {extents["coverage"][key] for key in ("missing", "duplicate", "stale", "undecided")} == {
        0
    }
    assert len({decision["id"] for decision in decisions}) == len(decisions)
    assert set(extents["rules"]) == {
        "bounded-segment/v1",
        "configuration-composition/v1",
        "configured-capacity/v1",
        "extension-contract/v1",
        "no-semantic-maximum/v1",
        "route-progression/v1",
        "schema-bound/v1",
    }
    assert {decision["policy"] for decision in decisions} == {
        "contract_max",
        "extension_owned",
        "fixed",
        "operational_policy",
        "segmented_no_total_max",
    }

    for decision in decisions:
        assert decision["rule"] in extents["rules"]
        assert decision["reason"]
        source = _resolve_pointer(projection, decision["source_pointer"])
        assert isinstance(source, dict)
        policy = decision["policy"]
        if policy in {"fixed", "contract_max"}:
            source_field = decision.get("source_constraint", {}).get("field")
            if source_field == "nargs":
                assert source["nargs"] == decision["maximum"]
            elif source_field == "type.maximum":
                assert source["type"]["maximum"] == decision["maximum"]
            elif decision["dimension"] == "cardinality":
                keyword = "maxProperties" if decision["unit"] == "entries" else "maxItems"
                assert source[keyword] == decision["maximum"]
            elif decision["dimension"] == "encoded-size":
                assert source["x-riverhog-encoded-bytes-max"] == decision["maximum"]
            elif decision["dimension"] == "length":
                if "source_constraint" in decision:
                    assert source["pattern"] == decision["source_constraint"]["pattern"]
                else:
                    assert source["maxLength"] == decision["maximum"]
            else:
                assert source["maximum"] == decision["maximum"]
        elif policy == "segmented_no_total_max" and "maximum" in decision:
            keyword = "maxProperties" if decision["unit"] == "entries" else "maxItems"
            assert source[keyword] == decision["maximum"]
        elif decision["dimension"] == "cardinality":
            if decision["unit"] == "items" and source.get("type") == "array":
                assert "maxItems" not in source
            elif decision["unit"] == "entries" and source.get("type") == "object":
                assert "maxProperties" not in source
        assert policy != "segmented_no_total_max" or "progression" in decision


def test_operation_parameter_extents_are_covered_from_the_openapi_authority() -> None:
    projection = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    decisions = {
        decision["id"]: decision
        for decision in projection["external_contract"]["extents"]["decisions"]
    }

    page_size = decisions[
        "http:riverhog:operation:list_collections:parameter:query:page_size:/:value"
    ]
    append_bytes = decisions[
        "http:riverhog:operation:append_collection_upload_session_provenance_journal:"
        "parameter:header:Content-Length:/:value"
    ]
    assert (page_size["policy"], page_size["maximum"]) == ("contract_max", 100)
    assert (append_bytes["policy"], append_bytes["maximum"]) == (
        "contract_max",
        1024 * 1024,
    )
    cli_page_size = decisions["cli:riverhog:riverhog:collection:list:parameter:page_size:value"]
    assert (cli_page_size["policy"], cli_page_size["maximum"]) == (
        page_size["policy"],
        page_size["maximum"],
    )


def test_schema_bounds_accept_the_boundary_and_reject_the_next_value() -> None:
    projection = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    decisions = projection["external_contract"]["extents"]["decisions"]

    exercised = 0
    for decision in decisions:
        if decision["policy"] not in {"fixed", "contract_max"}:
            continue
        _resolve_pointer(projection, decision["source_pointer"])
        source_field = decision.get("source_constraint", {}).get("field")
        if source_field in {"nargs", "type.maximum"}:
            continue
        maximum = decision["maximum"]
        minimum = decision.get("minimum")
        dimension = decision["dimension"]
        if dimension == "cardinality":
            if decision["unit"] == "entries":
                validator = Draft202012Validator(
                    {
                        "type": "object",
                        "maxProperties": maximum,
                        **({"minProperties": minimum} if minimum is not None else {}),
                    }
                )
                exact = {str(index): None for index in range(maximum)}
                above = {str(index): None for index in range(maximum + 1)}
            else:
                validator = Draft202012Validator(
                    {
                        "type": "array",
                        "maxItems": maximum,
                        **({"minItems": minimum} if minimum is not None else {}),
                    }
                )
                exact = [None] * maximum
                above = [None] * (maximum + 1)
        elif dimension == "encoded-size":
            assert maximum > 0
            continue
        elif dimension == "length":
            source_pattern = decision.get("source_constraint", {}).get("pattern")
            if source_pattern is not None:
                exact_text = "0" * maximum
                assert re.fullmatch(source_pattern, exact_text)
                validator = Draft202012Validator({"type": "string", "pattern": source_pattern})
                exact = exact_text
                above = f"{exact_text}0"
            else:
                validator = Draft202012Validator(
                    {
                        "type": "string",
                        "maxLength": maximum,
                        **({"minLength": minimum} if minimum is not None else {}),
                    }
                )
                exact = "x" * maximum
                above = f"{exact}x"
        else:
            validator = Draft202012Validator(
                {
                    "type": "number",
                    "maximum": maximum,
                    **({"minimum": minimum} if minimum is not None else {}),
                }
            )
            exact = maximum
            above = maximum + 1
        assert validator.is_valid(exact), decision["id"]
        assert not validator.is_valid(above), decision["id"]
        exercised += 1

    assert exercised > 500


def test_every_open_schema_map_is_classified_and_hidden_maxima_are_forbidden() -> None:
    projection = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    decisions = projection["external_contract"]["extents"]["decisions"]
    maps = [
        decision
        for decision in decisions
        if decision["dimension"] == "cardinality" and decision["unit"] == "entries"
    ]

    assert len(maps) > 100
    for decision in maps:
        if decision["policy"] == "operational_policy":
            assert decision["capacity_authority"] == {
                "owner": decision["owner"],
                "declared_maximum": None,
                "hidden_maximum": "forbidden",
            }


def test_bounded_carriers_do_not_become_domain_cardinality_maxima() -> None:
    projection = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    decisions = {
        decision["id"]: decision
        for decision in projection["external_contract"]["extents"]["decisions"]
    }

    registration = decisions[
        "http:riverhog:components:/schemas/RegisterCollectionUploadSessionFilesRequest/"
        "properties/files:cardinality"
    ]
    assert registration["policy"] == "segmented_no_total_max"
    assert registration["reason"] == "bounded-upload-registration"
    archive_parts = decisions[
        "protocol:https://nashspence.github.io/riverhog/v1/schemas/"
        "collection-archive-volume-v1.schema.json:/$defs/pack/properties/parts:cardinality"
    ]
    assert archive_parts["policy"] == "segmented_no_total_max"
    assert archive_parts["maximum"] == 1024

    semantic_set_maxima = {
        decision["reason"]
        for decision in decisions.values()
        if decision["dimension"] == "cardinality" and decision["policy"] == "contract_max"
    }
    assert semantic_set_maxima == {
        "bounded-diagnostic-sample-with-explicit-overflow-markers",
        "bounded-exact-tag-selector-batch",
        "bounded-exact-classification-admission-predicate",
        "bounded-object-identity-assertion-envelope",
        "state-conditioned-empty-set",
        "wildcard-access-grant-is-exclusive",
    }


def test_generated_protocols_remain_owned_by_the_product_contract_packages() -> None:
    projection = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    decisions = projection["external_contract"]["extents"]["decisions"]
    expected = {
        "generated:riverhog-storage-adapter": "riverhog-storage-adapter-protocol",
        "generated:stove0-observer": "stove0-observer-protocol",
        "generated:stove0-review-sampler": "stove0-review-sampler-protocol",
        "generated:stove0-target": "stove0-target-protocol",
    }

    for authority, owner in expected.items():
        selected = [
            decision for decision in decisions if f"protocol:{authority}:" in decision["id"]
        ]
        assert selected
        assert {decision["owner"] for decision in selected} == {owner}


def test_extent_relevant_deployment_configuration_is_source_linked() -> None:
    projection = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    decisions = {
        decision["id"]: decision
        for decision in projection["external_contract"]["extents"]["decisions"]
    }

    cache_lease = decisions[
        "configuration-environment:RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_LEASE:value"
    ]
    assert cache_lease["policy"] == "operational_policy"
    assert cache_lease["configuration"] == "RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_LEASE"
    source = _resolve_pointer(projection, cache_lease["source_pointer"])
    assert "riverhog-server" in source["consumers"]
    trace = json.loads(TRACE_ARTIFACT.read_text(encoding="utf-8"))
    trace_sources = {item["id"]: item for item in trace["sources"]}
    cache_lease_trace = trace_sources[
        "configuration-environment:RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_LEASE"
    ]
    assert cache_lease_trace["bindings"]
    assert all((REPO_ROOT / binding["path"]).exists() for binding in cache_lease_trace["bindings"])

    configuration_decisions = [
        decision
        for decision in decisions.values()
        if decision["id"].startswith(("configuration-environment:", "configuration-pattern:"))
    ]
    assert configuration_decisions
    for decision in configuration_decisions:
        assert decision["policy"] == "operational_policy"
        assert decision["owner"]
        source = _resolve_pointer(projection, decision["source_pointer"])
        if decision["id"].startswith("configuration-environment:"):
            assert decision["configuration"] == source["name"]
            assert source["consumers"]
            binding = trace_sources[f"configuration-environment:{source['name']}"]
            assert binding["bindings"]
        else:
            assert decision["configuration"] in source["parameters"]["setting"]


def test_every_route_owned_collection_extent_is_projected_with_exact_progression() -> None:
    projection = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    decisions = {
        decision["id"]: decision
        for decision in projection["external_contract"]["extents"]["decisions"]
    }

    observed: set[str] = set()
    for application, openapi in projection["external_contract"]["http_openapi"].items():
        for path_item in openapi["paths"].values():
            for operation in path_item.values():
                if not isinstance(operation, dict):
                    continue
                progression = operation.get("x-riverhog-read-collection")
                if progression is None:
                    continue
                identity = f"http:{application}:operation:{operation['operationId']}:logical-result"
                observed.add(identity)
                decision = decisions[identity]
                assert decision["policy"] == "segmented_no_total_max"
                assert decision["progression"] == progression

    assert observed
    assert observed <= set(decisions)


def test_trace_index_covers_every_extent_and_only_current_source_paths() -> None:
    projection = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    trace = json.loads(TRACE_ARTIFACT.read_text(encoding="utf-8"))
    decisions = projection["external_contract"]["extents"]["decisions"]
    links = trace["extent_sources"]

    semantic_payload = json.dumps(projection, separators=(",", ":"), sort_keys=True).encode()
    assert trace["contract_canonical_sha256"] == hashlib.sha256(semantic_payload).hexdigest()
    assert trace["contract_projection_sha256"] == hashlib.sha256(ARTIFACT.read_bytes()).hexdigest()
    assert {link["id"] for link in links} == {decision["id"] for decision in decisions}
    assert len(links) == len(decisions)
    witnesses = {item["id"]: item for item in trace["behavioral_witnesses"]}
    segmented = [
        decision for decision in decisions if decision["policy"] == "segmented_no_total_max"
    ]
    linked_segmented = [link for link in links if "behavioral_witnesses" in link]
    assert {link["id"] for link in linked_segmented} == {decision["id"] for decision in segmented}
    assert all(link["behavioral_witnesses"] for link in linked_segmented)
    assert {
        witness_id for link in linked_segmented for witness_id in link["behavioral_witnesses"]
    } == set(witnesses)
    for witness in witnesses.values():
        assert set(witness["claims"]) == {
            "bounded_step",
            "forward_progress",
            "multiple_segments",
            "no_silent_truncation",
            "restart",
        }
        assert witness["gates"]
        assert witness["test_node_ids"]
        for node_id in witness["test_node_ids"]:
            assert (REPO_ROOT / node_id.split("::", 1)[0]).is_file()
    assert len({source["id"] for source in trace["sources"]}) == len(trace["sources"])
    for source in trace["sources"]:
        direct = source.get("source", {})
        if "path" in direct:
            assert (REPO_ROOT / direct["path"]).exists()
        for route in source.get("routes", []):
            route_source = route["source"]
            if "path" in route_source:
                assert (REPO_ROOT / route_source["path"]).exists()
        for fixture in source.get("fixtures", []):
            assert (REPO_ROOT / fixture["path"]).exists()
        for binding in source.get("bindings", []):
            assert (REPO_ROOT / binding["path"]).exists()


def test_semantic_protocol_and_state_authorities_do_not_freeze_source_layout() -> None:
    projection = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    external = projection["external_contract"]

    assert all(
        authority.startswith(("generated:", "https://"))
        for authority in external["protocol_schemas"]
    )
    assert all(
        "fixture_sha256s" in owner and "fixtures" not in owner
        for owner in external["durable_state"]["owners"]
    )


def test_extent_generation_is_deterministic_from_current_authorities() -> None:
    module = load_script()

    first = module.contract_projection()
    second = module.contract_projection()

    assert first == second
    assert module.trace_projection(first) == module.trace_projection(second)
