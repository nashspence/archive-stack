from __future__ import annotations

import inspect
import subprocess
import sys

import pytest
from pydantic import ValidationError
from riverhog_storage_adapter_protocol import (
    ADAPTER_PRIVATE_ASSERTION_PREFIX,
    AdapterDescriptor,
    CompletedObjectReceipt,
    CompletedWriteLookupRequest,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ObjectHeadRequest,
    ObjectLocator,
    ObjectMetadataReceipt,
    ObjectReadRequest,
    ReadPreparationRequest,
    ReadReady,
    ReadRequested,
    ReadStatus,
    SmallObjectWriteRequest,
    StorageAdapterPort,
    WriteCompleteRequest,
    WriteSegmentReceipt,
    WriteSegmentSet,
    WriteSession,
    WriteStartRequest,
    normalize_object_path,
    validate_completed_write_response,
    validate_object_metadata_response,
    validate_read_status_response,
    validate_write_segment_set_response,
    validate_write_session_response,
)


def test_protocol_matches_the_existing_capability_port_inventory() -> None:
    assert {
        name
        for name, value in StorageAdapterPort.__dict__.items()
        if not name.startswith("_") and inspect.isfunction(value)
    } == {
        "abort_incomplete_writes",
        "abort_write",
        "cleanup_read",
        "complete_write",
        "begin_write",
        "delete_object",
        "delete_prefix",
        "descriptor",
        "find_completed_write",
        "head_object",
        "iter_object",
        "list_segments",
        "prepare_read",
        "put_small_object",
        "read_status",
        "write_segment",
    }


def test_write_completion_preserves_optional_digests_and_repeated_provider_tokens() -> None:
    session = WriteSession(object_path="archives/id/volumes/pack.tar.age", write_token="opaque")
    request = WriteCompleteRequest(
        session=session,
        segments=(
            WriteSegmentReceipt(
                number=1,
                segment_token="provider-part-1",
                stored_bytes=5,
            ),
            WriteSegmentReceipt(
                number=2,
                segment_token="provider-part-1",
                stored_bytes=7,
                stored_sha256="a" * 64,
            ),
        ),
        expected_bytes=12,
        required_identity_assertions={"Riverhog-Format": "riverhog-pack-volume/v1"},
        expected_placement="archive",
    )

    assert request.required_identity_assertions == {"riverhog-format": "riverhog-pack-volume/v1"}
    assert request.segments[0].segment_token == request.segments[1].segment_token
    assert request.segments[0].stored_sha256 is None
    assert "stored_sha256" not in WriteCompleteRequest.model_fields


def test_completed_write_attestation_binds_exact_identity_and_placement() -> None:
    request = CompletedWriteLookupRequest(
        object_path="archives/id/volumes/pack.tar.age",
        required_identity_assertions={"riverhog-format": "riverhog-pack-volume/v1"},
        expected_placement="archive",
    )
    receipt = CompletedObjectReceipt(
        object_path=request.object_path,
        revision="opaque-revision",
        entity_token="opaque-entity",
        stored_bytes=12,
        verified_identity_assertions=request.required_identity_assertions,
        verified_placement=request.expected_placement,
        completed_at="2026-08-25T00:00:00Z",
    )

    validate_completed_write_response(request, receipt)
    completion = WriteCompleteRequest(
        session=WriteSession(object_path=request.object_path, write_token="opaque-write"),
        segments=(WriteSegmentReceipt(number=1, segment_token="opaque-part", stored_bytes=12),),
        expected_bytes=12,
        required_identity_assertions=request.required_identity_assertions,
        expected_placement=request.expected_placement,
    )
    validate_completed_write_response(completion, receipt)
    with pytest.raises(ValueError, match="receipt differs"):
        validate_completed_write_response(
            completion,
            receipt.model_copy(update={"object_path": "archives/id/other.age"}),
        )
    with pytest.raises(ValueError, match="completed-object bytes"):
        validate_completed_write_response(
            completion,
            receipt.model_copy(update={"stored_bytes": 13}),
        )
    with pytest.raises(ValueError, match="identity assertions"):
        validate_completed_write_response(
            request,
            receipt.model_copy(
                update={"verified_identity_assertions": {"riverhog-format": "other/v1"}}
            ),
        )
    with pytest.raises(ValueError, match="placement"):
        validate_completed_write_response(
            request,
            receipt.model_copy(update={"verified_placement": "immediate"}),
        )


def test_small_object_digest_does_not_apply_to_resumable_writes() -> None:
    request = SmallObjectWriteRequest(
        object_path="README.md",
        content_type="text/markdown",
        required_identity_assertions={"archive-guidance-format": "encrypted-archive-readme-v1"},
        placement="immediate",
        mode="create_only",
        stored_bytes=0,
        stored_sha256="b" * 64,
    )

    assert request.stored_bytes == 0
    assert request.stored_sha256 == "b" * 64


def test_required_identity_assertions_is_bounded_canonical_and_opaque() -> None:
    request = WriteStartRequest(
        object_path="archives/id/volumes/segment.bin.age",
        content_type="application/octet-stream",
        required_identity_assertions={
            "Riverhog-Plan-Sha256": "a" * 64,
            "riverhog-format": "riverhog-raw-volume/v1",
        },
        placement="archive",
    )

    assert list(request.required_identity_assertions) == [
        "riverhog-format",
        "riverhog-plan-sha256",
    ]
    description = WriteStartRequest.model_json_schema()["properties"][
        "required_identity_assertions"
    ]["description"]
    assert "must contain" in description
    assert "additional adapter-private assertions" in description
    with pytest.raises(ValidationError, match="encoded-size bound"):
        WriteStartRequest(
            object_path="archives/id/object",
            content_type="application/octet-stream",
            required_identity_assertions={"identity": "x" * (16 * 1024 + 1)},
            placement="archive",
        )
    with pytest.raises(ValidationError, match="adapter-private namespace"):
        WriteStartRequest(
            object_path="archives/id/object",
            content_type="application/octet-stream",
            required_identity_assertions={
                f"{ADAPTER_PRIVATE_ASSERTION_PREFIX}private": "not-public"
            },
            placement="archive",
        )


@pytest.mark.parametrize(
    "path",
    ["/absolute", "../escape", "nested/../escape", "back\\slash", " spaced", "a//b"],
)
def test_object_paths_reject_noncanonical_input(path: str) -> None:
    with pytest.raises(ValueError):
        normalize_object_path(path)


def test_revision_and_deletion_modes_preserve_versioned_and_unversioned_targets() -> None:
    current = ObjectLocator(object_path="cache/object")
    versioned = ObjectLocator(object_path="cache/object", revision="provider-version")

    assert DeleteObjectRequest(object=current, mode="current").object.revision is None
    assert DeleteObjectRequest(object=versioned, mode="exact_revision").object.revision
    assert DeleteObjectRequest(object=current, mode="all_versions").mode == "all_versions"
    with pytest.raises(ValidationError, match="requires a revision"):
        DeleteObjectRequest(object=current, mode="exact_revision")
    with pytest.raises(ValidationError, match="must not name a revision"):
        DeleteObjectRequest(object=versioned, mode="all_versions")


def test_prefix_deletion_is_explicitly_version_aware() -> None:
    request = DeletePrefixRequest(object_prefix="archives/collection/")

    assert request.mode == "all_versions"


def test_object_metadata_keeps_large_object_digest_optional() -> None:
    receipt = ObjectMetadataReceipt(
        object_path="archives/id/volumes/pack.tar.age",
        stored_bytes=100,
        required_identity_assertions={"riverhog-format": "riverhog-pack-volume/v1"},
        completed_at="2026-08-21T00:00:00Z",
    )

    assert receipt.revision is None
    assert receipt.stored_sha256 is None


def test_range_requires_both_offset_and_size() -> None:
    locator = ObjectLocator(object_path="archives/id/volumes/pack.tar.age")
    assert ObjectReadRequest(object=locator, expected_bytes=10, offset=0, size=0).size == 0
    with pytest.raises(ValidationError, match="requires both"):
        ObjectReadRequest(object=locator, expected_bytes=10, offset=0)
    with pytest.raises(ValidationError, match="exceeds"):
        ObjectReadRequest(object=locator, expected_bytes=10, offset=8, size=3)


def test_descriptor_exposes_only_runtime_facts_needed_by_riverhog() -> None:
    descriptor = AdapterDescriptor(
        implementation_id="fixture.storage/v1",
        implementation_version="1.0.0",
        read_mode="restore_required",
        minimum_nonfinal_segment_bytes=5,
        maximum_segment_bytes=10,
        maximum_segment_count=10_000,
    )
    schema = str(AdapterDescriptor.model_json_schema()).casefold()

    assert descriptor.protocol == "riverhog-storage-adapter/v1"
    assert "bucket" not in schema
    assert "storage_class" not in schema
    assert "cloudfront" not in schema


def test_read_preparation_carries_only_exact_opaque_objects() -> None:
    request = ReadPreparationRequest(
        objects=(
            ObjectLocator(
                object_path="archives/id/volumes/segment.bin.age",
                revision="provider-version",
            ),
        )
    )
    schema = str(ReadPreparationRequest.model_json_schema()).casefold()

    assert request.objects[0].revision == "provider-version"
    assert "retrieval_tier" not in schema
    assert "hold_days" not in schema
    assert "storage_class" not in schema


def test_response_validators_bind_exact_requests_and_closed_readiness_states() -> None:
    start = WriteStartRequest(
        object_path="archives/id/object.age",
        content_type="application/octet-stream",
        required_identity_assertions={"riverhog-format": "fixture/v1"},
        placement="archive",
    )
    session = WriteSession(object_path=start.object_path, write_token="opaque")
    validate_write_session_response(start, session)
    segment_set = WriteSegmentSet(
        session=session,
        segments=(WriteSegmentReceipt(number=1, segment_token="one", stored_bytes=1),),
    )
    validate_write_segment_set_response(session, segment_set)

    other_session = session.model_copy(update={"write_token": "other"})
    with pytest.raises(ValueError, match="segment set"):
        validate_write_segment_set_response(other_session, segment_set)

    head_request = ObjectHeadRequest(
        object=ObjectLocator(object_path=start.object_path, revision="revision-1"),
        expected_placement="archive",
    )
    metadata = ObjectMetadataReceipt(
        object_path=start.object_path,
        revision="revision-1",
        stored_bytes=1,
        required_identity_assertions=start.required_identity_assertions,
        completed_at="2026-08-25T00:00:00Z",
    )
    validate_object_metadata_response(head_request, metadata)

    read_request = ReadPreparationRequest(objects=(head_request.object,))
    status = ReadStatus(
        objects=read_request.objects,
        readiness=ReadRequested(estimated_ready_at="2026-08-25T01:00:00Z"),
    )
    validate_read_status_response(read_request, status)
    assert (
        ReadStatus(
            objects=read_request.objects,
            readiness=ReadReady(available_until="2026-08-26T00:00:00Z"),
        ).readiness.state
        == "ready"
    )


def test_protocol_imports_no_runtime_or_provider_implementation() -> None:
    code = (
        "import sys\n"
        "import riverhog_storage_adapter_protocol\n"
        "forbidden = {'boto3', 'botocore', 'fastapi', 'httpx', 'riverhog_core'}\n"
        "loaded = forbidden & set(sys.modules)\n"
        "assert not loaded, sorted(loaded)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
