from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from tests.fixtures.acceptance import AcceptanceSystem


def test_collection_listing_can_filter_by_full_disc_redundancy() -> None:
    with TemporaryDirectory() as tmp:
        system = AcceptanceSystem.create(Path(tmp))
        try:
            system.seed_planner_fixtures()
            system.planning.finalize_image("img_2026-04-20_01")
            system.discs.register("20260420T040001Z", "Shelf B1", disc_id="20260420T040001Z-1")
            system.discs.register("20260420T040001Z", "Shelf B2", disc_id="20260420T040001Z-2")
            system.discs.update(
                "20260420T040001Z",
                "20260420T040001Z-1",
                state="verified",
                verification_state="verified",
            )
            system.mark_collection_archive_uploaded("docs")

            system.constrain_collection_to_paths(
                "docs",
                [
                    "tax/2022/invoice-123.pdf",
                    "tax/2022/receipt-456.pdf",
                ],
                hot=False,
            )

            listing = system.request(
                "GET",
                "/v1/collections",
                params={"disc_redundancy": "full"},
            )
            assert listing.status_code == 200
            listed = listing.json()["collections"]
            assert [item["id"] for item in listed] == ["docs"]
            assert "image_coverage" not in listed[0]
            assert listed[0]["disc_coverage"]["state"] == "full"

            summary = system.request("GET", "/v1/collections/docs")
            assert summary.status_code == 200
            payload = summary.json()
            assert "image_coverage" in payload
            assert payload["disc_redundancy"] == {
                "state": "full",
                "bytes": payload["bytes"],
            }
            assert payload["archive"]["state"] == "uploaded"
            assert payload["disc_coverage"]["state"] == "full"

            preview = system.request(
                "GET",
                "/v1/collections/docs",
                params={"coverage_path_limit": 1},
            )
            assert preview.status_code == 200
            preview_image = preview.json()["image_coverage"][0]
            assert preview_image["covered_paths"] == ["tax/2022/invoice-123.pdf"]
            assert preview_image["covered_paths_total"] == 2
        finally:
            system.close()


def test_collection_disc_coverage_requires_all_split_parts() -> None:
    with TemporaryDirectory() as tmp:
        system = AcceptanceSystem.create(Path(tmp))
        try:
            system.seed_split_planner_fixtures()
            system.planning.finalize_image("img_2026-04-20_03")
            system.discs.register(
                "20260420T040003Z",
                "vault-a/shelf-03",
                disc_id="20260420T040003Z-1",
            )
            system.discs.update(
                "20260420T040003Z",
                "20260420T040003Z-1",
                state="verified",
                verification_state="verified",
            )
            system.constrain_collection_to_paths(
                "docs",
                ["tax/2022/invoice-123.pdf"],
                hot=False,
            )
            system.mark_collection_archive_uploaded("docs")

            summary = system.request("GET", "/v1/collections/docs")
            assert summary.status_code == 200
            payload = summary.json()
            assert payload["disc_coverage"]["state"] == "partial"

            system.planning.finalize_image("img_2026-04-20_04")
            system.discs.register(
                "20260420T040004Z",
                "vault-a/shelf-04",
                disc_id="20260420T040004Z-1",
            )
            system.discs.update(
                "20260420T040004Z",
                "20260420T040004Z-1",
                state="verified",
                verification_state="verified",
            )
            summary = system.request("GET", "/v1/collections/docs")
            assert summary.status_code == 200
            payload = summary.json()
            assert payload["disc_coverage"]["state"] == "full"
        finally:
            system.close()
