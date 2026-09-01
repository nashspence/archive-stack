from __future__ import annotations

import hashlib

import pytest
from riverhog_protocol.collection_workflow_transport import (
    ArtifactDispositionOutputPageDocument,
    ArtifactDispositionPageDocument,
)
from riverhog_protocol.collection_workflows import (
    ArtifactDisposition,
    ArtifactDispositionOutput,
    ArtifactDispositionSetIdentity,
    canonical_json_bytes,
    canonical_json_sha256,
)
from riverhog_protocol.derivation_evidence import verify_derivation_evidence


def _fixture() -> tuple[list[bytes], list[bytes], ArtifactDispositionSetIdentity]:
    dispositions = [
        ArtifactDisposition(1, "a" * 64, "camera/a.mov", "transformed"),
        ArtifactDisposition(1, "a" * 64, "camera/b.mov", "transformed"),
    ]
    outputs = [
        ArtifactDispositionOutput(1, "a" * 64, "camera/a.mov", "derived/a.mkv"),
        ArtifactDispositionOutput(1, "a" * 64, "camera/b.mov", "derived/b.mkv"),
    ]
    disposition_digest = hashlib.sha256()
    for item in dispositions:
        disposition_digest.update(canonical_json_bytes(item.as_dict()) + b"\n")
    output_digest = hashlib.sha256()
    for item in outputs:
        output_digest.update(canonical_json_bytes(item.as_dict()) + b"\n")
    identity = ArtifactDispositionSetIdentity(
        disposition_count=2,
        output_edge_count=2,
        output_artifact_count=2,
        sha256=canonical_json_sha256(
            {
                "format": "riverhog-artifact-disposition-set/v1",
                "disposition_count": 2,
                "dispositions_sha256": disposition_digest.hexdigest(),
                "output_edge_count": 2,
                "output_artifact_count": 2,
                "outputs_sha256": output_digest.hexdigest(),
            }
        ),
    )
    disposition_pages = [
        ArtifactDispositionPageDocument.model_validate(
            {
                "authority": identity.as_dict(),
                "start_ordinal": index,
                "next_ordinal": index + 1 if index == 0 else None,
                "dispositions": [item.as_dict()],
            }
        )
        for index, item in enumerate(dispositions)
    ]
    output_pages = [
        ArtifactDispositionOutputPageDocument.model_validate(
            {
                "authority": identity.as_dict(),
                "start_ordinal": index,
                "next_ordinal": index + 1 if index == 0 else None,
                "outputs": [item.as_dict()],
            }
        )
        for index, item in enumerate(outputs)
    ]
    return (
        [
            canonical_json_bytes(page.model_dump(mode="json", exclude_none=True))
            for page in disposition_pages
        ],
        [
            canonical_json_bytes(page.model_dump(mode="json", exclude_none=True))
            for page in output_pages
        ],
        identity,
    )


def test_derivation_evidence_verifies_without_database_or_producer_ontology() -> None:
    dispositions, outputs, identity = _fixture()

    assert verify_derivation_evidence(dispositions, outputs, expected=identity) == identity


def test_derivation_evidence_fails_closed_on_reordered_pages() -> None:
    dispositions, outputs, identity = _fixture()

    with pytest.raises(ValueError, match="contiguous"):
        verify_derivation_evidence(reversed(dispositions), outputs, expected=identity)
