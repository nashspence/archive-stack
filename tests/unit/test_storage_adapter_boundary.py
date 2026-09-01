from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from riverhog_core.domain.archive import StoredArchivePart
from riverhog_core.ports.archive_objects import (
    CompletedObjectReceipt,
    ResumableWriteConstraints,
    WriteSegmentReceipt,
    WriteSession,
)
from riverhog_storage_adapter_protocol import AdapterDescriptor, StorageAdapterPort

REPO = Path(__file__).parents[2]


def test_core_archive_layout_and_resumable_write_vocabularies_are_distinct() -> None:
    assert {field.name for field in fields(StoredArchivePart)} == {
        "number",
        "plaintext_start",
        "plaintext_bytes",
        "plaintext_sha256",
        "stored_bytes",
        "stored_sha256",
    }
    assert {field.name for field in fields(WriteSession)} == {
        "object_path",
        "write_token",
        "expected_bytes",
    }
    assert {field.name for field in fields(WriteSegmentReceipt)} == {
        "number",
        "segment_token",
        "bytes",
        "sha256",
    }
    assert {field.name for field in fields(CompletedObjectReceipt)} >= {
        "object_path",
        "revision",
        "entity_token",
        "bytes",
    }
    assert {field.name for field in fields(ResumableWriteConstraints)} == {
        "minimum_nonfinal_segment_bytes",
        "maximum_segment_bytes",
        "maximum_segment_count",
    }


def test_public_storage_adapter_owns_provider_neutral_resumable_capabilities() -> None:
    assert {
        "begin_write",
        "write_segment",
        "list_segments",
        "complete_write",
        "find_completed_write",
        "abort_write",
    } <= set(StorageAdapterPort.__dict__)
    assert {
        "minimum_nonfinal_segment_bytes",
        "maximum_segment_bytes",
        "maximum_segment_count",
    } <= set(AdapterDescriptor.model_fields)


def test_provider_vocabulary_is_isolated_from_generic_adapter_and_core_sources() -> None:
    roots = (
        REPO / "packages/riverhog-storage-adapter-protocol/src",
        REPO / "packages/riverhog-storage-adapter-support/src",
        REPO / "packages/riverhog-storage-adapter-asgi-support/src",
        REPO / "riverhog/server/src/riverhog_core",
    )
    checked = [
        path
        for root in roots
        for path in root.rglob("*.py")
        if "state_migrations" not in path.parts
    ]

    provider_terms = (
        "multipart",
        "version_id",
        "upload_id",
        "part_token",
        "minimum_nonfinal_part",
        "maximum_part_bytes",
        "maximum_part_count",
        "uploadpart",
        "listparts",
    )
    violations = {
        path.relative_to(REPO).as_posix(): token
        for path in checked
        for token in provider_terms
        if token in path.read_text(encoding="utf-8").casefold()
    }

    assert violations == {}
