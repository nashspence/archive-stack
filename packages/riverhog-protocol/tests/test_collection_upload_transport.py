from __future__ import annotations

import pytest
from pydantic import ValidationError
from riverhog_protocol import CollectionUploadFileBatchDocument, CollectionUploadFileIn


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
