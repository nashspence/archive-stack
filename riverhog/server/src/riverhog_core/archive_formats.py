from __future__ import annotations

PACK_VOLUME_STORAGE_FORMAT = "riverhog-pack-volume/v1"
RAW_VOLUME_STORAGE_FORMAT = "riverhog-raw-volume/v1"
ROOT_MANIFEST_STORAGE_FORMAT = "riverhog-collection-root/v1"
VOLUME_METADATA_STORAGE_FORMAT = "riverhog-collection-volume-metadata/v1"
RECOVERY_DESCRIPTOR_STORAGE_FORMAT = "riverhog-recovery-descriptor/v1"
PROVENANCE_ROOT_STORAGE_FORMAT = "riverhog-provenance-root/v1+age"
PROVENANCE_VOLUME_METADATA_STORAGE_FORMAT = "riverhog-provenance-volume/v1+age"
PROVENANCE_TERMINAL_STORAGE_FORMAT = "riverhog-provenance-terminal/v1+age"
PROVENANCE_BINDING_SEGMENT_STORAGE_FORMAT = "riverhog-provenance-bindings/v1+age"
PROVENANCE_JOURNAL_SEGMENT_STORAGE_FORMAT = "riverhog-provenance-journal-segment/v1+age"

ARCHIVE_OBJECT_STORAGE_FORMATS = {
    "pack": PACK_VOLUME_STORAGE_FORMAT,
    "segment": RAW_VOLUME_STORAGE_FORMAT,
    "manifest": ROOT_MANIFEST_STORAGE_FORMAT,
    "volume-metadata": VOLUME_METADATA_STORAGE_FORMAT,
    "volume-terminal": VOLUME_METADATA_STORAGE_FORMAT,
    "recovery-descriptor": RECOVERY_DESCRIPTOR_STORAGE_FORMAT,
    "provenance-root": PROVENANCE_ROOT_STORAGE_FORMAT,
    "provenance-volume-metadata": PROVENANCE_VOLUME_METADATA_STORAGE_FORMAT,
    "provenance-terminal": PROVENANCE_TERMINAL_STORAGE_FORMAT,
    "provenance-bindings": PROVENANCE_BINDING_SEGMENT_STORAGE_FORMAT,
    "provenance-journal-segment": PROVENANCE_JOURNAL_SEGMENT_STORAGE_FORMAT,
}


def archive_object_storage_format(kind: str) -> str:
    try:
        return ARCHIVE_OBJECT_STORAGE_FORMATS[kind]
    except KeyError as exc:
        raise ValueError(f"unknown archive object kind: {kind}") from exc


__all__ = [
    "ARCHIVE_OBJECT_STORAGE_FORMATS",
    "PACK_VOLUME_STORAGE_FORMAT",
    "RAW_VOLUME_STORAGE_FORMAT",
    "RECOVERY_DESCRIPTOR_STORAGE_FORMAT",
    "PROVENANCE_BINDING_SEGMENT_STORAGE_FORMAT",
    "PROVENANCE_JOURNAL_SEGMENT_STORAGE_FORMAT",
    "PROVENANCE_ROOT_STORAGE_FORMAT",
    "PROVENANCE_TERMINAL_STORAGE_FORMAT",
    "PROVENANCE_VOLUME_METADATA_STORAGE_FORMAT",
    "ROOT_MANIFEST_STORAGE_FORMAT",
    "VOLUME_METADATA_STORAGE_FORMAT",
    "archive_object_storage_format",
]
