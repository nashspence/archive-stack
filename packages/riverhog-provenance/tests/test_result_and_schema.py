from __future__ import annotations

import json
from pathlib import Path

from riverhog_provenance import (
    ObservationRequest,
    UbuntuFileStateObserver,
    validate_entry_document,
    validate_graph_fragment,
)
from riverhog_provenance.common import canonical_json


def test_assertion_entry_template_validates_at_schema_level(tmp_path: Path, urn_factory) -> None:
    payload = tmp_path / "file"
    payload.write_bytes(b"abc")
    result = UbuntuFileStateObserver(enforce_ubuntu=False).observe(
        ObservationRequest(path=payload, lineage_id=urn_factory(), host_id=urn_factory())
    )
    validate_graph_fragment(result.graph_fragment())
    entry = result.make_assertion_entry(
        journal_id=urn_factory(),
        sequence=3,
        previous_entry_id=urn_factory(),
        previous_entry_json_sha256="0" * 64,
        previous_sequence=2,
    )
    assert entry["entry_kind"] == "assertion"
    assert entry["sequence"] == 3
    assert entry["body"] == result.assertion_body()
    validate_entry_document(entry)
    encoded = canonical_json(entry)
    assert json.loads(encoded) == entry


def test_observer_does_not_claim_payload_format(tmp_path: Path, urn_factory) -> None:
    payload = tmp_path / "misleading.jpg"
    payload.write_bytes(b"not actually a JPEG")
    result = UbuntuFileStateObserver(enforce_ubuntu=False).observe(
        ObservationRequest(path=payload, lineage_id=urn_factory(), host_id=urn_factory())
    )
    content = result.state["content"]
    assert set(content) == {"size_bytes", "digests"}
    assert "format" not in result.state


def test_policy_extension_is_digest_bound(tmp_path: Path, urn_factory) -> None:
    import copy

    import pytest

    payload = tmp_path / "bound"
    payload.write_bytes(b"bound")
    result = UbuntuFileStateObserver(enforce_ubuntu=False).observe(
        ObservationRequest(path=payload, lineage_id=urn_factory(), host_id=urn_factory())
    )
    fragment = copy.deepcopy(result.graph_fragment())
    policy = next(
        item for item in fragment["extensions"] if item["property"].endswith("/observation-policy")
    )
    policy["value"]["data"]["hash_chunk_bytes"] += 1
    with pytest.raises(ValueError, match="configuration digest"):
        validate_graph_fragment(fragment)
