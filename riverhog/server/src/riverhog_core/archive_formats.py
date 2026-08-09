from __future__ import annotations

PACK_VOLUME_STORAGE_FORMAT = "riverhog-pack-volume/v1"
RAW_VOLUME_STORAGE_FORMAT = "riverhog-raw-volume/v1"
ROOT_MANIFEST_STORAGE_FORMAT = "riverhog-collection-root/v1"
ROOT_PROOF_STORAGE_FORMAT = "riverhog-collection-root-proof/v1"
ATTESTATION_CHECKSUMS_STORAGE_FORMAT = "riverhog-archive-checksums/v1"
ATTESTATION_SIGNATURE_STORAGE_FORMAT = "riverhog-archive-signature/v1"
ATTESTATION_SIGNATURE_PROOF_STORAGE_FORMAT = "riverhog-archive-signature-proof/v1"

ARCHIVE_OBJECT_STORAGE_FORMATS = {
    "pack": PACK_VOLUME_STORAGE_FORMAT,
    "segment": RAW_VOLUME_STORAGE_FORMAT,
    "manifest": ROOT_MANIFEST_STORAGE_FORMAT,
    "proof": ROOT_PROOF_STORAGE_FORMAT,
    "checksums": ATTESTATION_CHECKSUMS_STORAGE_FORMAT,
    "signature": ATTESTATION_SIGNATURE_STORAGE_FORMAT,
    "signature-proof": ATTESTATION_SIGNATURE_PROOF_STORAGE_FORMAT,
}


def archive_object_storage_format(kind: str) -> str:
    try:
        return ARCHIVE_OBJECT_STORAGE_FORMATS[kind]
    except KeyError as exc:
        raise ValueError(f"unknown archive object kind: {kind}") from exc


__all__ = [
    "ARCHIVE_OBJECT_STORAGE_FORMATS",
    "ATTESTATION_CHECKSUMS_STORAGE_FORMAT",
    "ATTESTATION_SIGNATURE_PROOF_STORAGE_FORMAT",
    "ATTESTATION_SIGNATURE_STORAGE_FORMAT",
    "PACK_VOLUME_STORAGE_FORMAT",
    "RAW_VOLUME_STORAGE_FORMAT",
    "ROOT_MANIFEST_STORAGE_FORMAT",
    "ROOT_PROOF_STORAGE_FORMAT",
    "archive_object_storage_format",
]
