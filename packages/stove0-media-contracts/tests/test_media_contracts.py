from __future__ import annotations

from stove0_media_contracts import (
    SOURCE_ARTIFACT_ROLE,
    VIDEO_ARCHIVE_OPERATION,
    VideoArchiveIntent,
)


def test_archive_video_retirement_always_requires_reconstructive_source_artifacts() -> None:
    source_artifacts = next(
        output for output in VIDEO_ARCHIVE_OPERATION.outputs if output.role == SOURCE_ARTIFACT_ROLE
    )

    assert VIDEO_ARCHIVE_OPERATION.source_retirement_permitted
    assert source_artifacts.minimum == 1
    assert "preserve_source_artifacts" not in VideoArchiveIntent.model_json_schema()["properties"]
