from __future__ import annotations

import yaml

from arc_core.planner.layout import (
    PreviewEntry,
    assign_paths,
    manifest_bytes,
    preview_image,
)


def test_preview_image_uses_default_root_estimator(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_estimator(*, image_root, volume_id: str, fallback_bytes: int) -> int:
        calls.append(
            {
                "image_root": image_root,
                "volume_id": volume_id,
                "fallback_bytes": fallback_bytes,
            }
        )
        return fallback_bytes + 123

    monkeypatch.setattr("arc_core.planner.layout.estimate_iso_size_from_root", fake_estimator)

    preview = preview_image(
        image_id="img_001",
        target_bytes=10_000,
        collections={
            "docs": [
                {
                    "file_id": 1,
                    "relpath": "/a.txt",
                    "sha256": "a" * 64,
                    "piece_count": 1,
                    "pieces": [
                        {
                            "collection": "docs",
                            "file_id": 1,
                            "relpath": "/a.txt",
                            "piece_index": 0,
                        }
                    ],
                }
            ]
        },
        pieces=[
            {
                "collection": "docs",
                "file_id": 1,
                "relpath": "/a.txt",
                "piece_index": 0,
                "piece_count": 1,
                "stored_size_bytes": 100,
                "sidecar_size_bytes": 20,
            }
        ],
        encrypt_size=lambda n: n + 10,
    )

    assert calls
    assert calls[0]["volume_id"] == "img_001"
    assert isinstance(calls[0]["fallback_bytes"], int)
    assert preview.image.used_bytes == calls[0]["fallback_bytes"] + 123
    assert preview.image.iso_overhead_bytes == 123


def test_preview_image_accepts_explicit_estimator() -> None:
    preview = preview_image(
        image_id="img_002",
        target_bytes=10_000,
        collections={
            "docs": [
                {
                    "file_id": 1,
                    "relpath": "/a.txt",
                    "sha256": "b" * 64,
                    "piece_count": 1,
                    "pieces": [
                        {
                            "collection": "docs",
                            "file_id": 1,
                            "relpath": "/a.txt",
                            "piece_index": 0,
                        }
                    ],
                }
            ]
        },
        pieces=[
            {
                "collection": "docs",
                "file_id": 1,
                "relpath": "/a.txt",
                "piece_index": 0,
                "piece_count": 1,
                "stored_size_bytes": 100,
                "sidecar_size_bytes": 20,
            }
        ],
        encrypt_size=lambda n: n,
        estimate_iso_size=lambda *, image_root, volume_id, fallback_bytes: fallback_bytes,
    )

    assert preview.image.iso_overhead_bytes == 0


def test_assign_paths_and_manifest_bytes_support_multipart_files_and_collection_artifacts() -> None:
    pieces = [
        {
            "collection": "docs",
            "file_id": 1,
            "relpath": "/a.txt",
            "piece_index": 0,
            "piece_count": 1,
        },
        {
            "collection": "docs",
            "file_id": 2,
            "relpath": "/video.bin",
            "piece_index": 0,
            "piece_count": 12,
        },
        {
            "collection": "docs",
            "file_id": 2,
            "relpath": "/video.bin",
            "piece_index": 1,
            "piece_count": 12,
        },
    ]
    path_map = assign_paths(pieces)

    assert path_map[("docs", 2, 0)] == ("files/000002.001.age", "files/000002.001.yml.age")
    assert path_map[("docs", 2, 1)] == ("files/000002.002.age", "files/000002.002.yml.age")

    manifest = yaml.safe_load(
        manifest_bytes(
            "img_003",
            {
                "docs": [
                    {
                        "file_id": 1,
                        "relpath": "/a.txt",
                        "sha256": "a" * 64,
                        "piece_count": 1,
                        "pieces": [pieces[0]],
                    },
                    {
                        "file_id": 2,
                        "relpath": "/video.bin",
                        "sha256": "b" * 64,
                        "piece_count": 12,
                        "pieces": [pieces[1], pieces[2]],
                    },
                ]
            },
            path_map,
            volume_id="ARC-IMG-003",
            collection_artifact_paths={
                "docs": ("collections/000001.yml.age", "collections/000001.ots.age")
            },
        ).decode("utf-8")
    )

    collection = manifest["collections"][0]
    assert collection["manifest"] == "collections/000001.yml.age"
    assert collection["proof"] == "collections/000001.ots.age"
    assert collection["files"][0]["object"] == "files/000001.age"
    assert collection["files"][1]["parts"] == {
        "count": 12,
        "present": [
            {"index": 1, "object": "files/000002.001.age", "sidecar": "files/000002.001.yml.age"},
            {"index": 2, "object": "files/000002.002.age", "sidecar": "files/000002.002.yml.age"},
        ],
    }


def test_preview_image_counts_artifact_entries_in_root_usage() -> None:
    preview = preview_image(
        image_id="img_004",
        target_bytes=10_000,
        collections={
            "docs": [
                {
                    "file_id": 1,
                    "relpath": "/a.txt",
                    "sha256": "c" * 64,
                    "piece_count": 1,
                    "pieces": [
                        {
                            "collection": "docs",
                            "file_id": 1,
                            "relpath": "/a.txt",
                            "piece_index": 0,
                        }
                    ],
                }
            ]
        },
        pieces=[
            {
                "collection": "docs",
                "file_id": 1,
                "relpath": "/a.txt",
                "piece_index": 0,
                "piece_count": 1,
                "stored_size_bytes": 100,
                "sidecar_size_bytes": 20,
            }
        ],
        encrypt_size=lambda n: n,
        estimate_iso_size=lambda *, image_root, volume_id, fallback_bytes: fallback_bytes,
        artifact_entries=[
            PreviewEntry(kind="artifact", relpath="collections/000001.yml.age", size=33)
        ],
    )

    assert preview.payload_bytes == 100
    assert any(entry.kind == "artifact" for entry in preview.image.entries)
    assert preview.image.used_bytes == preview.image.root_used_bytes
