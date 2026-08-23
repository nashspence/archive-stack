"""Stable artifact roles for the maintained media archive operations."""

from typing import Final

SOURCE_ROLE: Final = "stove0.media.source/v1"
XMP_SOURCE_ROLE: Final = "stove0.media.xmp-source/v1"
AUDIO_ARCHIVE_ROLE: Final = "stove0.media.audio-archive/v1"
AV1_OPUS_ARCHIVE_ROLE: Final = "stove0.media.av1-opus-archive/v1"
METADATA_XMP_ROLE: Final = "stove0.media.metadata-xmp/v1"
SOURCE_ARTIFACT_ROLE: Final = "stove0.media.source-artifact/v1"

__all__ = [
    "AUDIO_ARCHIVE_ROLE",
    "AV1_OPUS_ARCHIVE_ROLE",
    "METADATA_XMP_ROLE",
    "SOURCE_ARTIFACT_ROLE",
    "SOURCE_ROLE",
    "XMP_SOURCE_ROLE",
]
