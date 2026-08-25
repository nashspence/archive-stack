from __future__ import annotations

import pytest
from pydantic import ValidationError
from riverhog_protocol import (
    CollectionUploadFileBatchDocument,
    CollectionUploadFileIn,
    CollectionUploadLayoutDocument,
    validate_collection_upload_batch_against_layout,
)


def _file(path: str) -> dict[str, object]:
    return {
        "path": path,
        "bytes": 1,
        "sha256": "a" * 64,
        "provenance": {
            "status": "omitted",
            "omission_reason": "source did not expose provenance",
        },
    }


def _layout(*, pack_member_bytes: int = 1024, raw_part_bytes: int = 65536) -> dict[str, int]:
    return {
        "pack_source_bytes": 1024,
        "pack_files": 16,
        "pack_member_bytes": pack_member_bytes,
        "pack_part_plaintext_bytes": 65536,
        "raw_volume_plaintext_bytes": raw_part_bytes * 4,
        "raw_part_plaintext_bytes": raw_part_bytes,
    }


def test_direct_ingress_file_contract_is_canonical_and_reusable() -> None:
    file = CollectionUploadFileIn.model_validate(_file("camera/clip.mp4"))
    batch = CollectionUploadFileBatchDocument(files=[file])

    assert batch.model_dump(mode="json") == {
        "files": [{**_file("camera/clip.mp4"), "raw_parts": None}]
    }
    schema = CollectionUploadFileIn.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["path"] == {"$ref": "#/$defs/CanonicalRelPath"}
    assert schema["$defs"]["CanonicalRelPath"]["pattern"] == r"^[^/\\]+(?:/[^/\\]+)*$"


@pytest.mark.parametrize("path", (" camera/clip.mp4", "camera//clip.mp4", "camera/../clip.mp4"))
def test_direct_ingress_rejects_noncanonical_file_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        CollectionUploadFileIn.model_validate(_file(path))


def test_direct_ingress_batch_preserves_the_server_order_contract() -> None:
    with pytest.raises(ValidationError, match="canonical path order"):
        CollectionUploadFileBatchDocument.model_validate(
            {"files": [_file("z.txt"), _file("a.txt")]}
        )


def test_direct_ingress_batch_rejects_duplicate_file_paths() -> None:
    with pytest.raises(ValidationError, match="paths must be unique"):
        CollectionUploadFileBatchDocument.model_validate(
            {"files": [_file("a.txt"), _file("a.txt")]}
        )


def test_direct_ingress_layout_binds_raw_part_declarations() -> None:
    raw = {
        **_file("video.bin"),
        "bytes": 65537,
        "raw_parts": {
            "part_plaintext_bytes": 65536,
            "sha256s": ["b" * 64, "c" * 64],
        },
    }
    batch = CollectionUploadFileBatchDocument.model_validate({"files": [raw]})
    layout = CollectionUploadLayoutDocument.model_validate(_layout(pack_member_bytes=1))

    assert validate_collection_upload_batch_against_layout(batch, layout) is batch


@pytest.mark.parametrize(
    "file_payload",
    (
        {**_file("small.bin"), "raw_parts": {"part_plaintext_bytes": 65536, "sha256s": ["b" * 64]}},
        {**_file("large.bin"), "bytes": 1024},
        {
            **_file("large.bin"),
            "bytes": 1024,
            "raw_parts": {"part_plaintext_bytes": 131072, "sha256s": ["b" * 64]},
        },
        {
            **_file("large.bin"),
            "bytes": 65537,
            "raw_parts": {"part_plaintext_bytes": 65536, "sha256s": ["b" * 64]},
        },
    ),
)
def test_direct_ingress_layout_rejects_inconsistent_raw_parts(
    file_payload: dict[str, object],
) -> None:
    batch = CollectionUploadFileBatchDocument.model_validate({"files": [file_payload]})
    layout = CollectionUploadLayoutDocument.model_validate(_layout())

    with pytest.raises(ValueError):
        validate_collection_upload_batch_against_layout(batch, layout)


def test_direct_ingress_captured_provenance_uses_canonical_reference_ids() -> None:
    payload = {
        **_file("camera/clip.mp4"),
        "provenance": {
            "status": "captured",
            "journal_id": "journal-1",
            "current_state_id": "state-1",
        },
    }

    with pytest.raises(ValidationError):
        CollectionUploadFileIn.model_validate(payload)
