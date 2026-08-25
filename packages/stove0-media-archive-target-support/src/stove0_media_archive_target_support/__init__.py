"""Pure projection support bridging observation evidence into media archive target plans."""

from stove0_media_archive_target_support.projection import (
    MEDIA_PROJECTION_FORMAT,
    MediaArchiveProjection,
    MediaArchiveProjectionPayload,
    MediaProjectedValue,
    MediaProjectionItem,
    RetainedXmpSidecar,
    ffmpeg_container_metadata_args,
    render_projection_xmp,
    resolve_media_archive_projection,
)

__all__ = [
    "MEDIA_PROJECTION_FORMAT",
    "MediaArchiveProjection",
    "MediaArchiveProjectionPayload",
    "MediaProjectedValue",
    "MediaProjectionItem",
    "RetainedXmpSidecar",
    "ffmpeg_container_metadata_args",
    "render_projection_xmp",
    "resolve_media_archive_projection",
]
