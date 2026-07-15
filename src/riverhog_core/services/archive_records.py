from __future__ import annotations

from riverhog_core.archive_object_paths import archive_storage_prefix_from_object_path
from riverhog_core.catalog_models import CollectionArchiveCopyRecord
from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    CollectionArchivePackageIdentity,
    CollectionArchiveUploadReceipt,
)


def apply_archive_receipt(
    archive: CollectionArchiveCopyRecord,
    receipt: CollectionArchiveUploadReceipt,
) -> None:
    archive.state = "uploaded"
    archive.archive_storage_prefix = archive_storage_prefix_from_object_path(
        receipt.archive.object_path
    )
    archive.object_path = receipt.archive.object_path
    archive.stored_bytes = receipt.archive.stored_bytes
    archive.sha256 = receipt.archive_sha256
    archive.backend = receipt.archive.backend
    archive.storage_class = receipt.archive.storage_class
    archive.last_uploaded_at = receipt.archive.uploaded_at
    archive.last_verified_at = receipt.archive.verified_at
    archive.failure = None
    archive.archive_format = receipt.archive_format
    archive.compression = receipt.compression
    archive.manifest_object_path = receipt.manifest.object_path
    archive.manifest_sha256 = receipt.manifest_sha256
    archive.manifest_stored_bytes = receipt.manifest.stored_bytes
    archive.manifest_uploaded_at = receipt.manifest.uploaded_at
    archive.ots_object_path = receipt.proof.object_path
    archive.ots_sha256 = receipt.proof_sha256
    archive.ots_stored_bytes = receipt.proof.stored_bytes
    archive.ots_uploaded_at = receipt.proof.uploaded_at


def archive_copy_is_complete(archive: CollectionArchiveCopyRecord) -> bool:
    return bool(
        archive.state == "uploaded"
        and archive.object_path
        and archive.stored_bytes is not None
        and archive.sha256
        and archive.manifest_object_path
        and archive.manifest_stored_bytes is not None
        and archive.manifest_sha256
        and archive.ots_object_path
        and archive.ots_stored_bytes is not None
        and archive.ots_sha256
        and archive.last_verified_at
    )


def archive_copy_identity(
    archive: CollectionArchiveCopyRecord,
) -> CollectionArchivePackageIdentity:
    return CollectionArchivePackageIdentity(
        archive=ArchiveObjectIdentity(
            object_path=str(archive.object_path),
            stored_bytes=int(archive.stored_bytes or 0),
            sha256=str(archive.sha256),
        ),
        manifest=ArchiveObjectIdentity(
            object_path=str(archive.manifest_object_path),
            stored_bytes=int(archive.manifest_stored_bytes or 0),
            sha256=str(archive.manifest_sha256),
        ),
        proof=ArchiveObjectIdentity(
            object_path=str(archive.ots_object_path),
            stored_bytes=int(archive.ots_stored_bytes or 0),
            sha256=str(archive.ots_sha256),
        ),
    )


def archive_copy_stored_bytes(archive: CollectionArchiveCopyRecord) -> int:
    return (
        int(archive.stored_bytes or 0)
        + int(archive.manifest_stored_bytes or 0)
        + int(archive.ots_stored_bytes or 0)
    )
