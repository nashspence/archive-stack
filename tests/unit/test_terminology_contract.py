import json
from collections.abc import Iterator
from pathlib import Path

CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "terminology"
    / "user-facing-terms.v1.json"
)


def load_contract() -> dict[str, object]:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_terminology_contract_shape() -> None:
    contract = load_contract()
    assert contract["schema_version"] == 1
    term_types = set(contract["term_type_values"])
    audiences = set(contract["audience_values"])
    surfaces = set(contract["surface_values"])
    terms = contract["terms"]
    assert isinstance(terms, list)
    assert len(terms) >= 40

    ids = [term["id"] for term in terms if isinstance(term, dict)]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))

    for term in terms:
        assert isinstance(term, dict)
        assert term["id"]
        assert term["label"]
        assert term["term_type"] in term_types
        assert term["definition"]
        assert set(term["audiences"]) <= audiences
        assert term["audiences"]
        assert term["exposed_as"]
        for exposure in term["exposed_as"]:
            assert exposure["surface"] in surfaces
            assert exposure["spelling"]
            assert exposure["context"]


def test_terminology_contract_covers_current_top_level_surfaces() -> None:
    terms = {term["id"]: term for term in load_contract()["terms"]}
    required = {
        "archive_mode",
        "collection",
        "collection_preview",
        "collection_restore",
        "disc_rebuild",
        "encode_profile",
        "fetch",
        "held_unmatched",
        "hot_eviction",
        "hot_storage",
        "image_rebuild",
        "jeb",
        "job",
        "metadata_projection",
        "munchy",
        "profile_group",
        "profile_routing",
        "qcut_review",
        "recovery_session",
        "riverhog",
        "runner_task",
        "workflow_mode",
    }
    assert required <= set(terms)

    needed_surfaces = {"cli:riverhog", "cli:djdan", "cli:munchy", "cli:jeb", "webhook"}
    covered_surfaces = {
        exposure["surface"]
        for term in terms.values()
        for exposure in term["exposed_as"]
    }
    assert needed_surfaces <= covered_surfaces


def test_named_systems_are_intentional_software_agents() -> None:
    terms = {term["id"]: term for term in load_contract()["terms"]}
    assert terms["riverhog"]["term_type"] == "software_agent"
    assert terms["djdan"]["term_type"] == "software_agent"
    assert terms["munchy"]["term_type"] == "software_agent"
    assert terms["jeb"]["term_type"] == "software_agent"


def test_terms_expose_ontology_shape() -> None:
    terms = {term["id"]: term for term in load_contract()["terms"]}
    assert terms["disc_rebuild"]["term_type"] == "activity"
    assert terms["collection_restore"]["term_type"] == "enum_value"
    assert terms["image_rebuild"]["term_type"] == "enum_value"
    assert terms["capture_date"]["term_type"] == "metadata_property"
    assert terms["collection_id"]["term_type"] == "identifier"
    assert terms["profile_routing"]["term_type"] == "policy"
    assert terms["held_unmatched"]["term_type"] == "state"


def walk_values(value: object) -> Iterator[object]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from walk_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from walk_values(nested)
    else:
        yield value


def test_terminology_contract_does_not_reference_work_items() -> None:
    contract = load_contract()
    assert "issue" not in contract
    assert "status_values" not in contract
    assert all(value != "issue" for value in walk_values(contract))
    assert all(value != "status" for value in walk_values(contract))
    assert all(value != "needs_review" for value in walk_values(contract))
    assert not any(
        isinstance(value, str) and "github.com/" in value
        for value in walk_values(contract)
    )
