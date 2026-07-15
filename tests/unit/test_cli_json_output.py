from __future__ import annotations

import json

from pytest import CaptureFixture

from riverhog_cli.main import _compact_collection_page
from riverhog_cli.output import emit


def test_collection_list_json_keeps_collection_summaries_compact() -> None:
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
                    "id": "2025/20250102T030405Z__docs",
                    "files": 2,
                    "bytes": 100,
                    "hot_bytes": 25,
                    "archive_copies": [
                        {
                            "store": "deep",
                            "state": "uploaded",
                            "storage_class": "DEEP_ARCHIVE",
                            "stored_bytes": 128,
                            "object_path": "riverhog/archives/opaque/archive.tar.age",
                        }
                    ],
                }
            ],
        }
    )

    assert payload["collections"] == [
        {
            "id": "2025/20250102T030405Z__docs",
            "files": 2,
            "bytes": 100,
            "hot_bytes": 25,
            "archive_copies": [
                {
                    "store": "deep",
                    "state": "uploaded",
                    "storage_class": "DEEP_ARCHIVE",
                    "stored_bytes": 128,
                }
            ],
        }
    ]


def test_emit_json_is_compact_machine_output(capsys: CaptureFixture[str]) -> None:
    emit({"b": [1, 2], "a": {"c": True}}, json_mode=True)

    stdout = capsys.readouterr().out
    assert stdout == '{"a":{"c":true},"b":[1,2]}\n'
    assert json.loads(stdout) == {"a": {"c": True}, "b": [1, 2]}
