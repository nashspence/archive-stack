from riverhog_api.schemas.collections import CollectionUploadFileIn


def test_captured_file_provenance_has_the_canonical_wire_shape() -> None:
    file = CollectionUploadFileIn.model_validate(
        {
            "path": "nested/example.bin",
            "bytes": 4,
            "sha256": "a" * 64,
            "provenance": {
                "status": "captured",
                "journal_id": "urn:uuid:00000000-0000-7000-8000-000000000001",
                "current_state_id": "urn:uuid:00000000-0000-7000-8000-000000000002",
            },
        }
    )

    assert file.model_dump()["provenance"] == {
        "status": "captured",
        "journal_id": "urn:uuid:00000000-0000-7000-8000-000000000001",
        "current_state_id": "urn:uuid:00000000-0000-7000-8000-000000000002",
    }


def test_omitted_file_provenance_has_the_canonical_wire_shape() -> None:
    file = CollectionUploadFileIn.model_validate(
        {
            "path": "nested/example.bin",
            "bytes": 4,
            "sha256": "a" * 64,
            "provenance": {
                "status": "omitted",
                "omission_reason": "source observation was explicitly declined",
            },
        }
    )

    assert file.model_dump()["provenance"] == {
        "status": "omitted",
        "omission_reason": "source observation was explicitly declined",
    }
