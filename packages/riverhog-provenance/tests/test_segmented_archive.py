from __future__ import annotations

import hashlib

import pytest
from riverhog_provenance import (
    PROVENANCE_BINDING_SEGMENT_FILES_MAX,
    PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX,
    ProvenancePayloadIdentity,
    ProvenanceRootDocument,
    ProvenanceTerminalDocument,
    ProvenanceValidationError,
    ProvenanceVolumeDocument,
    binding_segment_bytes,
    format_provenance_sequence,
    parse_binding_segment,
    update_ordered_volume_commitment,
)

_SHA = "a" * 64
_JOURNAL = "urn:uuid:00000000-0000-4000-8000-000000000001"


def test_ordered_segmented_provenance_authority_round_trips() -> None:
    binding_payload = binding_segment_bytes(
        first_file_order=0,
        files=[
            {
                "path": "camera/clip.mp4",
                "bytes": 12,
                "sha256": _SHA,
                "status": "captured",
                "journal_id": _JOURNAL,
                "current_state_id": "state-1",
            }
        ],
    )
    first = ProvenanceVolumeDocument(
        archive_generation=_SHA,
        archive_tree_sha256=_SHA,
        sequence=0,
        payload=ProvenancePayloadIdentity(
            kind="bindings",
            path=f"provenance/payloads/volume-{format_provenance_sequence(0)}.bin.age",
            bytes=len(binding_payload),
            sha256=hashlib.sha256(binding_payload).hexdigest(),
        ),
        first_file_order=0,
        file_count=1,
    )
    journal_payload = b"journal\n"
    second = ProvenanceVolumeDocument(
        archive_generation=_SHA,
        archive_tree_sha256=_SHA,
        sequence=1,
        payload=ProvenancePayloadIdentity(
            kind="journal",
            path=f"provenance/payloads/volume-{format_provenance_sequence(1)}.bin.age",
            bytes=len(journal_payload),
            sha256=hashlib.sha256(journal_payload).hexdigest(),
        ),
        journal_id=_JOURNAL,
        journal_offset=0,
        journal_bytes=len(journal_payload),
        journal_sha256=hashlib.sha256(journal_payload).hexdigest(),
    )
    terminal = ProvenanceTerminalDocument(
        archive_generation=_SHA,
        archive_tree_sha256=_SHA,
        sequence=2,
    )
    digest = hashlib.sha256()
    update_ordered_volume_commitment(digest, first)
    update_ordered_volume_commitment(digest, second)
    update_ordered_volume_commitment(digest, terminal)
    root = ProvenanceRootDocument(
        archive_generation=_SHA,
        archive_tree_sha256=_SHA,
        ordered_volume_sha256=digest.hexdigest(),
    )

    assert ProvenanceVolumeDocument.from_json_bytes(first.to_json_bytes()) == first
    assert ProvenanceVolumeDocument.from_json_bytes(second.to_json_bytes()) == second
    assert ProvenanceRootDocument.from_json_bytes(root.to_json_bytes()) == root
    assert parse_binding_segment(binding_payload)[0] == 0


def test_provenance_segmentation_limits_one_volume_not_the_logical_total() -> None:
    with pytest.raises(ProvenanceValidationError, match="segmentation rule"):
        binding_segment_bytes(
            first_file_order=0,
            files=[{"path": "unused"}] * (PROVENANCE_BINDING_SEGMENT_FILES_MAX + 1),
        )
    with pytest.raises(ProvenanceValidationError, match="segmentation rule"):
        ProvenanceVolumeDocument(
            archive_generation=_SHA,
            archive_tree_sha256=_SHA,
            sequence=0,
            payload=ProvenancePayloadIdentity(
                kind="journal",
                path=f"provenance/payloads/volume-{format_provenance_sequence(0)}.bin.age",
                bytes=PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX + 1,
                sha256=_SHA,
            ),
            journal_id=_JOURNAL,
            journal_offset=0,
            journal_bytes=PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX + 1,
            journal_sha256=_SHA,
        )


def test_ordered_commitment_detects_reordering_and_duplication() -> None:
    payload = b"binding\n"
    first = ProvenanceVolumeDocument(
        archive_generation=_SHA,
        archive_tree_sha256=_SHA,
        sequence=0,
        payload=ProvenancePayloadIdentity(
            kind="bindings",
            path=f"provenance/payloads/volume-{format_provenance_sequence(0)}.bin.age",
            bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        ),
        first_file_order=0,
        file_count=1,
    )
    second = ProvenanceTerminalDocument(
        archive_generation=_SHA,
        archive_tree_sha256=_SHA,
        sequence=1,
    )
    canonical = hashlib.sha256()
    update_ordered_volume_commitment(canonical, first)
    update_ordered_volume_commitment(canonical, second)
    reordered = hashlib.sha256()
    update_ordered_volume_commitment(reordered, second)
    update_ordered_volume_commitment(reordered, first)
    duplicated = hashlib.sha256()
    update_ordered_volume_commitment(duplicated, first)
    update_ordered_volume_commitment(duplicated, first)

    assert len({canonical.hexdigest(), reordered.hexdigest(), duplicated.hexdigest()}) == 3
