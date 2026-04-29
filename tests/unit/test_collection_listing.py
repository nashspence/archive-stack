from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from arc_core.domain.types import CollectionId
from tests.fixtures.acceptance import AcceptanceSystem


def test_collection_listing_can_include_protected_collections() -> None:
    with TemporaryDirectory() as tmp:
        system = AcceptanceSystem.create(Path(tmp))
        try:
            system.seed_planner_fixtures()
            system.planning.finalize_image("img_2026-04-20_01")
            system.copies.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-1")
            system.copies.register("20260420T040001Z", "Shelf B2", copy_id="20260420T040001Z-2")
            assert system.glacier_uploads.process_due_uploads(limit=10) == 1

            records = system.state.files_by_collection[CollectionId("docs")]
            covered_paths = {
                "tax/2022/invoice-123.pdf",
                "tax/2022/receipt-456.pdf",
            }
            for path in list(records):
                if path not in covered_paths:
                    del records[path]

            for record in records.values():
                record.hot = False
                record.archived = True

            listing = system.request(
                "GET",
                "/v1/collections",
                params={"protection_state": "protected"},
            )
            assert listing.status_code == 200
            assert [item["id"] for item in listing.json()["collections"]] == ["docs"]

            summary = system.request("GET", "/v1/collections/docs")
            assert summary.status_code == 200
            payload = summary.json()
            assert payload["protection_state"] == "protected"
            assert payload["protected_bytes"] == payload["bytes"]
        finally:
            system.close()
