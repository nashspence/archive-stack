from __future__ import annotations

from arc_core.planner.layout import preview_image



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
