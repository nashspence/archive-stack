from __future__ import annotations

from riverhog_protocol.pack_ingress import pack_upload_plan_sha256


def test_pack_plan_cardinality_is_defined_by_the_storage_plan() -> None:
    units = [
        {
            "unit": index,
            "payload_bytes": 1,
            "plaintext_start": index,
            "plaintext_bytes": 1,
            "final": index == 10_000,
            "sources": [
                {
                    "path": f"source/item-{index:05}.bin",
                    "bytes": 1,
                    "sha256": "1" * 64,
                }
            ],
        }
        for index in range(10_001)
    ]

    digest = pack_upload_plan_sha256(
        volume_id="pack-000000000001",
        plaintext_bytes=10_001,
        units=units,
    )

    assert len(digest) == 64
