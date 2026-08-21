from __future__ import annotations

import inspect
import subprocess
import sys

import pytest
from pydantic import ValidationError
from riverhog_storage_adapter_protocol import (
    AdapterDescriptor,
    DeleteObjectRequest,
    DeletePrefixRequest,
    MultipartCompleteRequest,
    MultipartCreateRequest,
    MultipartPartReceipt,
    MultipartUpload,
    ObjectLocator,
    ObjectMetadataReceipt,
    ObjectReadRequest,
    ReadPreparationRequest,
    SmallObjectWriteRequest,
    StorageAdapterPort,
    normalize_object_path,
)


def test_protocol_matches_the_existing_capability_port_inventory() -> None:
    assert {
        name
        for name, value in StorageAdapterPort.__dict__.items()
        if not name.startswith("_") and inspect.isfunction(value)
    } == {
        "abort_incomplete_uploads",
        "abort_multipart_upload",
        "cleanup_read",
        "complete_multipart_upload",
        "create_multipart_upload",
        "delete_object",
        "delete_prefix",
        "descriptor",
        "head_completed_object",
        "head_object",
        "iter_object",
        "list_parts",
        "prepare_read",
        "put_small_object",
        "read_status",
        "upload_part",
    }


def test_multipart_completion_preserves_optional_part_digests() -> None:
    upload = MultipartUpload(object_path="archives/id/volumes/pack.tar.age", upload_id="opaque")
    request = MultipartCompleteRequest(
        upload=upload,
        parts=(
            MultipartPartReceipt(
                number=1,
                part_token="provider-part-1",
                stored_bytes=5,
            ),
            MultipartPartReceipt(
                number=2,
                part_token="provider-part-2",
                stored_bytes=7,
                stored_sha256="a" * 64,
            ),
        ),
        expected_bytes=12,
        expected_identity_metadata={"Riverhog-Format": "riverhog-pack-volume/v1"},
        expected_placement="archive",
    )

    assert request.expected_identity_metadata == {"riverhog-format": "riverhog-pack-volume/v1"}
    assert request.parts[0].stored_sha256 is None
    assert "stored_sha256" not in MultipartCompleteRequest.model_fields


def test_small_object_digest_does_not_apply_to_multipart_objects() -> None:
    request = SmallObjectWriteRequest(
        object_path="README.md",
        content_type="text/markdown",
        identity_metadata={"archive-guidance-format": "encrypted-archive-readme-v1"},
        placement="immediate",
        mode="create_only",
        stored_bytes=0,
        stored_sha256="b" * 64,
    )

    assert request.stored_bytes == 0
    assert request.stored_sha256 == "b" * 64


def test_identity_metadata_is_bounded_canonical_and_opaque() -> None:
    request = MultipartCreateRequest(
        object_path="archives/id/volumes/segment.bin.age",
        content_type="application/octet-stream",
        identity_metadata={
            "Riverhog-Plan-Sha256": "a" * 64,
            "riverhog-format": "riverhog-raw-volume/v1",
        },
        placement="archive",
    )

    assert list(request.identity_metadata) == [
        "riverhog-format",
        "riverhog-plan-sha256",
    ]
    with pytest.raises(ValidationError, match="encoded-size bound"):
        MultipartCreateRequest(
            object_path="archives/id/object",
            content_type="application/octet-stream",
            identity_metadata={"identity": "x" * (16 * 1024 + 1)},
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
        identity_metadata={"riverhog-format": "riverhog-pack-volume/v1"},
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
        minimum_nonfinal_part_bytes=5,
        maximum_part_bytes=10,
        maximum_part_count=10_000,
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


def test_protocol_imports_no_runtime_or_provider_implementation() -> None:
    code = (
        "import sys\n"
        "import riverhog_storage_adapter_protocol\n"
        "forbidden = {'boto3', 'botocore', 'fastapi', 'httpx', 'riverhog_core'}\n"
        "loaded = forbidden & set(sys.modules)\n"
        "assert not loaded, sorted(loaded)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
