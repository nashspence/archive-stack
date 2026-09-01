"""Bounded independent verification of immutable collection derivation evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

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


def verify_derivation_evidence(
    disposition_pages: Iterable[bytes],
    output_pages: Iterable[bytes],
    *,
    expected: ArtifactDispositionSetIdentity,
) -> ArtifactDispositionSetIdentity:
    """Verify one sealed generic derivation authority with bounded working state."""

    disposition_digest = hashlib.sha256()
    disposition_count = 0
    last_disposition: tuple[int, str] | None = None
    disposition_terminal = False
    for content in disposition_pages:
        if disposition_terminal:
            raise ValueError("derivation disposition evidence continues after completion")
        disposition_page = ArtifactDispositionPageDocument.model_validate_json(content)
        _require_canonical_page(content, disposition_page)
        _require_authority(disposition_page.authority.model_dump(mode="json"), expected)
        if disposition_page.start_ordinal != disposition_count or not disposition_page.dispositions:
            raise ValueError("derivation disposition evidence is not contiguous")
        for disposition_document in disposition_page.dispositions:
            disposition = ArtifactDisposition.from_mapping(
                disposition_document.model_dump(mode="json", exclude_none=True)
            )
            disposition_key = (disposition.input_collection_id, disposition.input_path)
            if last_disposition is not None and disposition_key <= last_disposition:
                raise ValueError("derivation dispositions are not canonically ordered")
            disposition_digest.update(canonical_json_bytes(disposition.as_dict()) + b"\n")
            last_disposition = disposition_key
            disposition_count += 1
        _require_next(disposition_page.next_ordinal, disposition_count)
        disposition_terminal = disposition_page.next_ordinal is None

    output_digest = hashlib.sha256()
    output_edge_count = 0
    output_artifact_count = 0
    last_output: tuple[str, int, str] | None = None
    last_output_path: str | None = None
    output_terminal = False
    for content in output_pages:
        if output_terminal:
            raise ValueError("derivation output evidence continues after completion")
        output_page = ArtifactDispositionOutputPageDocument.model_validate_json(content)
        _require_canonical_page(content, output_page)
        _require_authority(output_page.authority.model_dump(mode="json"), expected)
        if output_page.start_ordinal != output_edge_count or not output_page.outputs:
            raise ValueError("derivation output evidence is not contiguous")
        for output_document in output_page.outputs:
            output = ArtifactDispositionOutput.from_mapping(output_document.model_dump(mode="json"))
            output_key = (output.output_path, output.input_collection_id, output.input_path)
            if last_output is not None and output_key <= last_output:
                raise ValueError("derivation output edges are not canonically ordered")
            if output.output_path != last_output_path:
                output_artifact_count += 1
                last_output_path = output.output_path
            output_digest.update(canonical_json_bytes(output.as_dict()) + b"\n")
            last_output = output_key
            output_edge_count += 1
        _require_next(output_page.next_ordinal, output_edge_count)
        output_terminal = output_page.next_ordinal is None

    if not disposition_terminal or not output_terminal:
        raise ValueError("derivation evidence has no explicit completion")
    observed = ArtifactDispositionSetIdentity(
        disposition_count=disposition_count,
        output_edge_count=output_edge_count,
        output_artifact_count=output_artifact_count,
        sha256=canonical_json_sha256(
            {
                "format": "riverhog-artifact-disposition-set/v1",
                "disposition_count": disposition_count,
                "dispositions_sha256": disposition_digest.hexdigest(),
                "output_edge_count": output_edge_count,
                "output_artifact_count": output_artifact_count,
                "outputs_sha256": output_digest.hexdigest(),
            }
        ),
    )
    if observed != expected:
        raise ValueError("derivation evidence differs from its sealed authority")
    return observed


def _require_canonical_page(
    content: bytes,
    page: ArtifactDispositionPageDocument | ArtifactDispositionOutputPageDocument,
) -> None:
    document = page.model_dump(mode="json", exclude_none=True)
    if bytes(content) != canonical_json_bytes(document):
        raise ValueError("derivation evidence page is not canonical JSON")


def _require_authority(value: dict[str, object], expected: ArtifactDispositionSetIdentity) -> None:
    if ArtifactDispositionSetIdentity.from_mapping(value) != expected:
        raise ValueError("derivation evidence page belongs to another authority")


def _require_next(next_ordinal: int | None, observed: int) -> None:
    if next_ordinal is not None and next_ordinal != observed:
        raise ValueError("derivation evidence continuation is not contiguous")


__all__ = ["verify_derivation_evidence"]
