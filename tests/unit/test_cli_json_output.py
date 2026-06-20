from __future__ import annotations

import json

from pytest import CaptureFixture

from riverhog_cli.main import _compact_collection_page
from riverhog_cli.output import emit


def test_collection_list_json_omits_heavy_nested_coverage() -> None:
    payload = _compact_collection_page(
        {
            "page": 1,
            "per_page": 25,
            "total": 1,
            "pages": 1,
            "sort": "id",
            "order": "asc",
            "collections": [
                {
                    "id": "docs",
                    "files": 2,
                    "bytes": 100,
                    "hot_bytes": 25,
                    "archived_bytes": 100,
                    "pending_bytes": 0,
                    "protected_bytes": 50,
                    "protection_state": "under_protected",
                    "archive_format": "tar",
                    "compression": "none",
                    "glacier": {
                        "state": "uploaded",
                        "storage_class": "DEEP_ARCHIVE",
                        "stored_bytes": 128,
                        "object_path": "riverhog/archives/opaque/archive.tar.age",
                    },
                    "disc_coverage": {
                        "state": "partial",
                        "covered_bytes": 50,
                        "verified_physical_bytes": 50,
                    },
                    "image_coverage": [
                        {
                            "id": "20260420T040001Z",
                            "covered_paths": ["tax/2022/invoice-123.pdf"],
                        }
                    ],
                }
            ],
        }
    )

    assert payload["collections"] == [
        {
            "id": "docs",
            "files": 2,
            "bytes": 100,
            "hot_bytes": 25,
            "archived_bytes": 100,
            "pending_bytes": 0,
            "protected_bytes": 50,
            "protection_state": "under_protected",
            "archive_format": "tar",
            "compression": "none",
            "glacier": {
                "state": "uploaded",
                "storage_class": "DEEP_ARCHIVE",
                "stored_bytes": 128,
            },
            "disc_coverage": {
                "state": "partial",
                "covered_bytes": 50,
                "verified_physical_bytes": 50,
            },
        }
    ]


def test_emit_json_is_compact_machine_output(capsys: CaptureFixture[str]) -> None:
    emit({"b": [1, 2], "a": {"c": True}}, json_mode=True)

    stdout = capsys.readouterr().out
    assert stdout == '{"a":{"c":true},"b":[1,2]}\n'
    assert json.loads(stdout) == {"a": {"c": True}, "b": [1, 2]}
