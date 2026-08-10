from __future__ import annotations

import hashlib
from pathlib import Path

from riverhog_provenance import prepare_file_provenance, validate_journal


def test_live_platform_observer_produces_a_restorable_payload_binding(tmp_path: Path) -> None:
    payload = tmp_path / "native-observation.bin"
    content = b"Riverhog live platform provenance\x00\xff\n"
    payload.write_bytes(content)

    prepared = prepare_file_provenance(
        payload,
        relative_path="native-observation.bin",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
        agent_name="riverhog-platform-qualification",
        agent_version="1.0.0",
    )

    assert prepared.source == "captured"
    assert prepared.binding.status == "captured"
    assert prepared.binding.bytes == len(content)
    assert prepared.binding.sha256 == hashlib.sha256(content).hexdigest()
    journal_id = str(prepared.binding.journal_id)
    summary = validate_journal(prepared.journals[journal_id])
    assert summary.current_path == "native-observation.bin"
    assert summary.current_bytes == len(content)
    assert summary.current_sha256 == hashlib.sha256(content).hexdigest()
    assert summary.current_state_id == prepared.binding.current_state_id
    assertions = summary.frames[-1].document["body"]["assertions"]
    assert len(assertions["states"]) == 1
    assert assertions["states"][0]["filesystem_metadata"]["timestamps"]
    assert assertions["states"][0]["filesystem_metadata"]["native_identifiers"]
    assert len(assertions["environments"]) == 1
    assert assertions["environments"][0]["operating_system"]
    assert assertions["environments"][0]["filesystem"]
    assert len(assertions["captures"]) == 1
    capture = assertions["captures"][0]
    assert capture["consistency"] == "verified_unchanged"
    assert capture["coverage"]["content_fixity"] == "complete"
    assert capture["coverage"]["basic_filesystem"] == "complete"
    assert capture["outcome"] in {"success", "partial"}
    assert len(assertions["payload_bindings"]) == 1
