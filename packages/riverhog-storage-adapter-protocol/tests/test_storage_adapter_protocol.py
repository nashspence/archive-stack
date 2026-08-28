from __future__ import annotations

import inspect
import subprocess
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError
from riverhog_storage_adapter_protocol import (
    ADAPTER_PRIVATE_ASSERTION_PREFIX,
    AbortIncompleteWritesRequest,
    AdapterDescriptor,
    CompletedObjectReceipt,
    CompletedWriteLookupRequest,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ImmutableObjectReceipt,
    ObjectHeadRequest,
    ObjectLocator,
    ObjectMetadataReceipt,
    ObjectReadReceipt,
    ObjectReadRequest,
    ObjectReadStream,
    ReadPreparationRequest,
    ReadReady,
    ReadRequested,
    ReadStatus,
    SmallObjectWriteRequest,
    StorageAdapterPort,
    ValidatedStorageAdapterPort,
    WriteCompleteRequest,
    WriteSegmentReceipt,
    WriteSegmentSet,
    WriteSession,
    WriteStartRequest,
    normalize_object_path,
    validate_completed_write_response,
    validate_object_metadata_response,
    validate_object_read_response,
    validate_read_status_response,
    validate_small_object_response,
    validate_write_completion_request,
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
        "read_object",
        "list_segments",
        "prepare_read",
        "put_small_object",
        "read_status",
        "write_segment",
    }
    assert {
        name
        for name, value in ValidatedStorageAdapterPort.__dict__.items()
        if not name.startswith("_") and inspect.isfunction(value)
    } == {
        name
        for name, value in StorageAdapterPort.__dict__.items()
        if not name.startswith("_") and inspect.isfunction(value)
    }


def test_validated_port_rejects_direct_response_and_stream_drift() -> None:
    head_request = ObjectHeadRequest(
        object=ObjectLocator(object_path="objects/item", revision="revision-1"),
        expected_placement="immediate",
    )
    invalid_head = ObjectMetadataReceipt(
        object_path="objects/other",
        revision="revision-1",
        stored_bytes=1,
        observed_identity_assertions={},
        verified_placement="immediate",
        completed_at="2026-08-25T00:00:00.000000Z",
    )
    head_adapter = cast(
        StorageAdapterPort,
        SimpleNamespace(head_object=lambda _request: invalid_head),
    )
    with pytest.raises(ValueError, match="metadata differs"):
        ValidatedStorageAdapterPort(head_adapter).head_object(head_request)

    closed: list[bool] = []
    mismatched_stream = ObjectReadStream(
        receipt=ObjectReadReceipt(
            object=ObjectLocator(object_path="objects/other"),
            total_bytes=6,
            offset=0,
            read_bytes=6,
        ),
        content=iter((b"unused",)),
        close=lambda: closed.append(True),
    )
    mismatched_adapter = cast(
        StorageAdapterPort,
        SimpleNamespace(read_object=lambda _request: mismatched_stream),
    )
    with pytest.raises(ValueError, match="object read path differs"):
        ValidatedStorageAdapterPort(mismatched_adapter).read_object(
            ObjectReadRequest(
                object=ObjectLocator(object_path="objects/item"),
                expected_bytes=6,
            )
        )
    assert closed == [True]

    read_adapter = cast(
        StorageAdapterPort,
        SimpleNamespace(
            read_object=lambda request: ObjectReadStream(
                receipt=ObjectReadReceipt(
                    object=request.object,
                    total_bytes=request.expected_bytes,
                    offset=0,
                    read_bytes=request.expected_bytes,
                ),
                content=iter((b"short",)),
            )
        ),
    )
    with pytest.raises(ValueError, match="observed byte count"):
        b"".join(
            ValidatedStorageAdapterPort(read_adapter)
            .read_object(
                ObjectReadRequest(
                    object=ObjectLocator(object_path="objects/item"),
                    expected_bytes=6,
                )
            )
            .content
        )

    descriptor = AdapterDescriptor(
        implementation_id="fixture.storage/v1",
        implementation_version="1.0.0",
        read_mode="immediate",
        minimum_nonfinal_segment_bytes=1,
    )
    session = WriteSession(object_path="objects/item", write_token="write-1")
    segment_adapter = cast(
        StorageAdapterPort,
        SimpleNamespace(
            descriptor=lambda: descriptor,
            write_segment=lambda **_kwargs: WriteSegmentReceipt(
                number=1,
                segment_token="segment-1",
                stored_bytes=3,
            ),
        ),
    )
    with pytest.raises(ValueError, match="did not consume"):
        ValidatedStorageAdapterPort(segment_adapter).write_segment(
            session=session,
            number=1,
            stored_bytes=3,
            content=iter((b"abc",)),
        )


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
        expected_content_type="application/vnd.riverhog.pack+age",
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
        expected_content_type="application/vnd.riverhog.pack+age",
        required_identity_assertions={"riverhog-format": "riverhog-pack-volume/v1"},
        expected_placement="archive",
    )
    receipt = CompletedObjectReceipt(
        object_path=request.object_path,
        revision="opaque-revision",
        entity_token="opaque-entity",
        stored_bytes=12,
        verified_content_type=request.expected_content_type,
        verified_identity_assertions=request.required_identity_assertions,
        verified_placement=request.expected_placement,
        completed_at="2026-08-25T00:00:00.000000Z",
    )

    validate_completed_write_response(request, receipt)
    completion = WriteCompleteRequest(
        session=WriteSession(object_path=request.object_path, write_token="opaque-write"),
        segments=(WriteSegmentReceipt(number=1, segment_token="opaque-part", stored_bytes=12),),
        expected_bytes=12,
        expected_content_type=request.expected_content_type,
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
    with pytest.raises(ValueError, match="content type"):
        validate_completed_write_response(
            request,
            receipt.model_copy(update={"verified_content_type": "application/octet-stream"}),
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
    assert "used only to identify and reconcile" in description
    assert "must not interpret" in description
    assert "routing, retrieval, retention, credentials, placement" in description
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


def test_write_session_is_a_persistable_restart_stable_continuation() -> None:
    session = WriteSession(
        object_path="archives/id/volumes/segment.bin.age",
        write_token="opaque-adapter-continuation",
    )

    assert WriteSession.model_validate_json(session.model_dump_json()) == session
    description = WriteSession.model_json_schema()["properties"]["write_token"]["description"]
    assert "persistable continuation handle" in description
    assert "process restarts" in description
    assert "incomplete-write reclamation" in description


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
        observed_identity_assertions={"riverhog-format": "riverhog-pack-volume/v1"},
        verified_placement="archive",
        completed_at="2026-08-21T00:00:00.000000Z",
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


def test_object_read_receipt_binds_observed_locator_and_range() -> None:
    request = ObjectReadRequest(
        object=ObjectLocator(object_path="archives/id/volume.age", revision="revision-1"),
        expected_bytes=10,
        offset=2,
        size=4,
    )
    receipt = ObjectReadReceipt(
        object=request.object,
        total_bytes=10,
        offset=2,
        read_bytes=4,
    )
    validate_object_read_response(request, receipt)

    with pytest.raises(ValueError, match="revision differs"):
        validate_object_read_response(
            request,
            receipt.model_copy(
                update={"object": request.object.model_copy(update={"revision": "revision-2"})}
            ),
        )
    with pytest.raises(ValueError, match="range differs"):
        validate_object_read_response(request, receipt.model_copy(update={"offset": 3}))


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
    descriptor = AdapterDescriptor(
        implementation_id="fixture.storage/v1",
        implementation_version="1.0.0",
        read_mode="immediate",
        minimum_nonfinal_segment_bytes=1,
        maximum_segment_count=2,
    )
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
    validate_write_segment_set_response(session, segment_set, descriptor)

    other_session = session.model_copy(update={"write_token": "other"})
    with pytest.raises(ValueError, match="segment set"):
        validate_write_segment_set_response(other_session, segment_set, descriptor)

    head_request = ObjectHeadRequest(
        object=ObjectLocator(object_path=start.object_path, revision="revision-1"),
        expected_placement="archive",
    )
    metadata = ObjectMetadataReceipt(
        object_path=start.object_path,
        revision="revision-1",
        stored_bytes=1,
        observed_identity_assertions=start.required_identity_assertions,
        verified_placement="archive",
        completed_at="2026-08-25T00:00:00.000000Z",
    )
    validate_object_metadata_response(head_request, metadata)

    read_request = ReadPreparationRequest(objects=(head_request.object,))
    status = ReadStatus(
        objects=read_request.objects,
        readiness=ReadRequested(estimated_ready_at="2026-08-25T01:00:00.000000Z"),
    )
    validate_read_status_response(read_request, status)
    assert (
        ReadStatus(
            objects=read_request.objects,
            readiness=ReadReady(available_until="2026-08-26T00:00:00.000000Z"),
        ).readiness.state
        == "ready"
    )


def test_listed_write_segments_allow_sparse_restart_state_but_completion_does_not() -> None:
    session = WriteSession(object_path="archives/id/object.age", write_token="opaque")
    second = WriteSegmentReceipt(number=2, segment_token="two", stored_bytes=1)
    listed = WriteSegmentSet(session=session, segments=(second,))
    descriptor = AdapterDescriptor(
        implementation_id="fixture.storage/v1",
        implementation_version="1.0.0",
        read_mode="immediate",
        minimum_nonfinal_segment_bytes=1,
        maximum_segment_count=2,
    )

    validate_write_segment_set_response(session, listed, descriptor)
    with pytest.raises(ValidationError, match="contiguous"):
        WriteCompleteRequest(
            session=session,
            segments=listed.segments,
            expected_bytes=1,
            expected_content_type="application/octet-stream",
            required_identity_assertions={"identity": "exact"},
            expected_placement="archive",
        )
    with pytest.raises(ValueError, match="count limit"):
        validate_write_segment_set_response(
            session,
            WriteSegmentSet(
                session=session,
                segments=(WriteSegmentReceipt(number=3, segment_token="three", stored_bytes=1),),
            ),
            descriptor,
        )
    with pytest.raises(ValidationError, match="unique and strictly ordered"):
        WriteSegmentSet(
            session=session,
            segments=(
                WriteSegmentReceipt(number=2, segment_token="two", stored_bytes=1),
                WriteSegmentReceipt(number=1, segment_token="one", stored_bytes=1),
            ),
        )


def test_segment_constraints_are_shared_by_listing_and_completion() -> None:
    session = WriteSession(object_path="archives/id/object.age", write_token="opaque")
    descriptor = AdapterDescriptor(
        implementation_id="fixture.storage/v1",
        implementation_version="1.0.0",
        read_mode="immediate",
        minimum_nonfinal_segment_bytes=5,
        maximum_segment_bytes=8,
        maximum_segment_count=2,
    )
    oversized = WriteSegmentReceipt(number=1, segment_token="one", stored_bytes=9)
    with pytest.raises(ValueError, match="byte limit"):
        validate_write_segment_set_response(
            session,
            WriteSegmentSet(session=session, segments=(oversized,)),
            descriptor,
        )

    completion = WriteCompleteRequest(
        session=session,
        segments=(
            WriteSegmentReceipt(number=1, segment_token="one", stored_bytes=4),
            WriteSegmentReceipt(number=2, segment_token="two", stored_bytes=1),
        ),
        expected_bytes=5,
        expected_content_type="application/octet-stream",
        required_identity_assertions={},
        expected_placement="archive",
    )
    with pytest.raises(ValueError, match="undersized non-final"):
        validate_write_completion_request(completion, descriptor)


def test_small_write_and_head_success_bind_exact_storage_predicates() -> None:
    request = SmallObjectWriteRequest(
        object_path="objects/item",
        content_type="application/octet-stream",
        required_identity_assertions={"identity": "exact"},
        placement="immediate",
        mode="create_only",
        stored_bytes=1,
        stored_sha256="a" * 64,
    )
    receipt = ImmutableObjectReceipt(
        object_path=request.object_path,
        stored_bytes=request.stored_bytes,
        stored_sha256=request.stored_sha256,
        verified_content_type=request.content_type,
        verified_identity_assertions=request.required_identity_assertions,
        verified_placement=request.placement,
        completed_at="2026-08-25T00:00:00.000000Z",
    )

    validate_small_object_response(request, receipt)
    with pytest.raises(ValueError, match="immutable-object receipt"):
        validate_small_object_response(
            request,
            receipt.model_copy(update={"verified_placement": "archive"}),
        )
    with pytest.raises(ValueError, match="immutable-object receipt"):
        validate_small_object_response(
            request,
            receipt.model_copy(update={"verified_identity_assertions": {"identity": "other"}}),
        )
    with pytest.raises(ValueError, match="immutable-object receipt"):
        validate_small_object_response(
            request,
            receipt.model_copy(update={"verified_content_type": "text/plain"}),
        )

    head = ObjectHeadRequest(
        object=ObjectLocator(object_path=request.object_path),
        expected_placement="immediate",
    )
    metadata = ObjectMetadataReceipt(
        object_path=request.object_path,
        stored_bytes=1,
        observed_identity_assertions=request.required_identity_assertions,
        verified_placement="archive",
        completed_at="2026-08-25T00:00:00.000000Z",
    )
    with pytest.raises(ValueError, match="metadata placement"):
        validate_object_metadata_response(head, metadata)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: AbortIncompleteWritesRequest(
            object_prefix="objects/", initiated_before="not-a-timestamp"
        ),
        lambda: ReadRequested(estimated_ready_at="2026-08-25T01:00:00Z"),
        lambda: ReadReady(available_until="2026-08-26T00:00:00+00:00"),
    ],
)
def test_storage_adapter_timestamps_require_canonical_utc(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(ValidationError, match="timestamp"):
        factory()


def test_protocol_imports_no_runtime_or_provider_implementation() -> None:
    code = (
        "import sys\n"
        "import riverhog_storage_adapter_protocol\n"
        "forbidden = {'boto3', 'botocore', 'fastapi', 'httpx', 'riverhog_core'}\n"
        "loaded = forbidden & set(sys.modules)\n"
        "assert not loaded, sorted(loaded)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
