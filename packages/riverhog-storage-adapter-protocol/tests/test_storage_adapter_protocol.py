from __future__ import annotations

import pytest
from pydantic import ValidationError
from riverhog_storage_adapter_protocol import (
    CompleteUploadRequest,
    ObjectReceipt,
    StorageAdapterDescriptor,
    StorageAdapterDescriptorPayload,
    StorageProfile,
    StorageProfilePayload,
    UploadDeclaration,
    UploadDeclarationPayload,
    UploadPartReceipt,
    WriteCondition,
    canonical_json_sha256,
    normalize_object_path,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def _profile() -> StorageProfile:
    return StorageProfile.seal(
        StorageProfilePayload(
            profile_id="example.archive/v1",
            read_mode="restore_required",
            egress_accounting_id="example-egress",
        )
    )


def _descriptor() -> StorageAdapterDescriptor:
    return StorageAdapterDescriptor.seal(
        StorageAdapterDescriptorPayload(
            implementation_id="example.adapter/v1",
            implementation_version="1.0.0",
            source_revision="a",
            profile=_profile(),
            minimum_nonfinal_part_bytes=5 * 1024**2,
            maximum_part_bytes=5 * 1024**3,
            maximum_part_count=10_000,
        )
    )


def test_profile_and_runtime_descriptor_have_distinct_identities() -> None:
    profile = _profile()
    first = StorageAdapterDescriptor.seal(
        StorageAdapterDescriptorPayload(
            implementation_id="example.adapter/v1",
            implementation_version="1.0.0",
            source_revision="a",
            profile=profile,
            minimum_nonfinal_part_bytes=5 * 1024**2,
            maximum_part_bytes=5 * 1024**3,
            maximum_part_count=10_000,
        )
    )
    replacement = StorageAdapterDescriptor.seal(
        StorageAdapterDescriptorPayload(
            implementation_id="replacement.adapter/v1",
            implementation_version="1.1.0",
            source_revision="b",
            profile=profile,
            minimum_nonfinal_part_bytes=8 * 1024**2,
            maximum_part_bytes=5 * 1024**3,
            maximum_part_count=10_000,
        )
    )

    assert first.profile == replacement.profile
    assert first.profile.profile_contract_sha256 == replacement.profile.profile_contract_sha256
    assert first.runtime_descriptor_sha256 != replacement.runtime_descriptor_sha256


def test_digest_models_reject_noncanonical_identity() -> None:
    profile = _profile()
    with pytest.raises(ValidationError, match="storage profile digest"):
        StorageProfile(
            **profile.model_dump(exclude={"profile_contract_sha256"}),
            profile_contract_sha256="0" * 64,
        )


def test_upload_request_binds_exact_stored_object_without_plaintext_identity() -> None:
    declaration = UploadDeclaration.seal(
        UploadDeclarationPayload(
            transfer_id="transfer-1",
            object_path="archives/a/volumes/pack-000001.tar.age",
            content_type="application/vnd.riverhog.pack-volume.v1+age",
            stored_bytes=8,
            runtime_descriptor_sha256=_descriptor().runtime_descriptor_sha256,
            condition=WriteCondition(),
        )
    )
    dumped = declaration.model_dump(mode="json", exclude_none=True)

    assert dumped["request_sha256"] == canonical_json_sha256(
        {key: value for key, value in dumped.items() if key != "request_sha256"}
    )
    assert "plaintext" not in repr(dumped).casefold()
    assert "logical" not in repr(dumped).casefold()


def test_completion_requires_contiguous_verified_parts_and_whole_digest() -> None:
    completion = CompleteUploadRequest(
        parts=(
            UploadPartReceipt(
                number=1,
                part_token="opaque-1",
                stored_bytes=3,
                stored_sha256=SHA_A,
            ),
            UploadPartReceipt(
                number=2,
                part_token="opaque-2",
                stored_bytes=5,
                stored_sha256=SHA_B,
            ),
        ),
        stored_bytes=8,
        stored_sha256=SHA_A,
    )
    assert completion.stored_bytes == 8

    with pytest.raises(ValidationError, match="stored_sha256"):
        CompleteUploadRequest(parts=(), stored_bytes=0)

    with pytest.raises(ValidationError, match="contiguous"):
        CompleteUploadRequest(
            parts=(
                UploadPartReceipt(
                    number=2,
                    part_token="opaque-2",
                    stored_bytes=8,
                    stored_sha256=SHA_A,
                ),
            ),
            stored_bytes=8,
            stored_sha256=SHA_A,
        )


@pytest.mark.parametrize(
    "path",
    ("", "/absolute", "a//b", "a/./b", "a/../b", "a\\b", "a/\x00/b", "a/b/"),
)
def test_object_paths_fail_closed(path: str) -> None:
    with pytest.raises(ValueError):
        normalize_object_path(path)


def test_object_receipt_requires_a_mandatory_opaque_revision() -> None:
    receipt = ObjectReceipt(
        object_path="archives/a/manifest.json.age",
        revision="adapter:01",
        content_type="application/octet-stream",
        stored_bytes=8,
        stored_sha256=SHA_A,
        completed_at="2026-08-21T00:00:00Z",
    )
    assert receipt.revision == "adapter:01"
