import json
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
    statuses = set(contract["status_values"])
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
        assert term["status"] in statuses
        assert term["definition"]
        assert set(term["audiences"]) <= audiences
        assert term["audiences"]
        assert term["exposed_as"]
        for exposure in term["exposed_as"]:
            assert exposure["surface"] in surfaces
            assert exposure["spelling"]
            assert exposure["context"]
        if term["status"] == "needs_review":
            assert term.get("issue") == contract["issue"]


def test_terminology_contract_covers_current_top_level_surfaces() -> None:
    terms = {term["id"]: term for term in load_contract()["terms"]}
    required = {
        "archive_mode",
        "collection",
        "collection_preview",
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


def test_review_terms_are_the_known_ontology_cleanup_surface() -> None:
    terms = {term["id"]: term for term in load_contract()["terms"]}
    assert terms["recovery_session"]["status"] == "needs_review"
    assert terms["image_rebuild"]["status"] == "needs_review"
    assert terms["collection_restore"]["status"] == "needs_review"
    assert terms["disc_rebuild"]["status"] == "preferred"
