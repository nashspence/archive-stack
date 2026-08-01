from __future__ import annotations

import pytest
from munchy_core.ingest import InputFileSpec, MunchyJobRequest, StorageHint, groups_for_files
from munchy_workflows.profiles import EncodeProfile
from pydantic import ValidationError

SHA256 = "0" * 64


def test_input_files_must_include_a_group_prefix() -> None:
    file_spec = InputFileSpec(path="camera-main-video/clip001.mp4", bytes=12, sha256=SHA256)

    assert file_spec.group == "camera-main-video"


@pytest.mark.parametrize("path", ["C0001.MP4", "/video/C0001.MP4", "../video/C0001.MP4", "a\\b"])
def test_rejects_ambiguous_input_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        InputFileSpec(path=path, bytes=12, sha256=SHA256)


def test_job_requires_every_uploaded_group_to_be_configured() -> None:
    files = (
        InputFileSpec(path="video/C0001.MP4", bytes=12, sha256=SHA256),
        InputFileSpec(path="slowmo/C0002.MP4", bytes=13, sha256="1" * 64),
    )

    with pytest.raises(ValidationError, match="undefined group"):
        MunchyJobRequest(
            job_id="job-1",
            template_id="camera-archive",
            input_upload_id="upload-1",
            workflow_mode="collection_archive",
            handoff_destination="riverhog",
            run_id="20260605T120000Z",
            files=files,
            groups={"video": EncodeProfile()},
            storage_hint=StorageHint(source_bytes=25),
        )


def test_job_accepts_all_declared_groups() -> None:
    files = (
        InputFileSpec(path="video/C0001.MP4", bytes=12, sha256=SHA256),
        InputFileSpec(path="slowmo/C0002.MP4", bytes=13, sha256="1" * 64),
    )

    request = MunchyJobRequest(
        job_id="job-1",
        template_id="camera-review",
        input_upload_id="upload-1",
        workflow_mode="review",
        handoff_destination="rclone",
        run_id="20260605T120000Z",
        files=files,
        groups={"video": EncodeProfile(), "slowmo": EncodeProfile()},
        storage_hint=StorageHint(source_bytes=25),
    )

    assert groups_for_files(request.files) == {"video", "slowmo"}
