from __future__ import annotations

from riverhog_cli.output import (
    format_collection_summary,
    format_dashboard,
    format_glacier_report,
    format_images,
)


def test_format_images_omits_finalized_image_glacier_context() -> None:
    rendered = format_images(
        {
            "page": 1,
            "pages": 1,
            "per_page": 25,
            "total": 1,
            "sort": "finalized_at",
            "order": "desc",
            "images": [
                {
                    "id": "20260420T040001Z",
                    "filename": "20260420T040001Z.iso",
                    "finalized_at": "2026-04-20T04:00:01Z",
                    "collections": 1,
                    "collection_ids": ["docs"],
                    "physical_protection_state": "partially_protected",
                    "physical_copies_registered": 1,
                    "physical_copies_verified": 0,
                    "physical_copies_required": 2,
                    "glacier": {
                        "state": "failed",
                        "object_path": None,
                        "failure": "s3 timeout",
                    },
                }
            ],
        }
    )
    assert "verified=0/2" in rendered
    assert "glacier=" not in rendered
    assert "glacier_failure" not in rendered


def test_format_dashboard_surfaces_ready_backlog_and_noncompliant_collections() -> None:
    rendered = format_dashboard(
        {
            "total": 1,
            "unplanned_bytes": 6100,
            "candidates": [
                {
                    "candidate_id": "img_2026-04-20_01",
                    "fill": 0.84,
                    "collections": 1,
                    "collection_ids": ["docs"],
                }
            ],
        },
        {
            "total": 1,
            "candidates": [
                {
                    "candidate_id": "img_2026-04-20_02",
                    "fill": 0.12,
                    "collections": 1,
                    "collection_ids": ["photos-2024"],
                }
            ],
        },
        {
            "page": 1,
            "per_page": 25,
            "images": [
                {
                    "id": "20260420T040001Z",
                    "filename": "20260420T040001Z.iso",
                    "collections": 1,
                    "collection_ids": ["docs"],
                    "physical_protection_state": "partially_protected",
                    "physical_copies_registered": 1,
                    "physical_copies_verified": 0,
                    "physical_copies_required": 2,
                    "glacier": {"state": "pending"},
                }
            ],
        },
        {
            "collections": [
                {
                    "id": "photos-2024",
                    "bytes": 100,
                    "protected_bytes": 0,
                    "protection_state": "unprotected",
                    "recovery": {
                        "available": [],
                        "verified_physical": {"state": "none", "bytes": 0},
                        "glacier": {"state": "none", "bytes": 0},
                    },
                }
            ]
        },
        {
            "collections": [
                {
                    "id": "docs",
                    "bytes": 55,
                    "protected_bytes": 22,
                    "protection_state": "partially_protected",
                    "recovery": {
                        "available": [],
                        "verified_physical": {"state": "partial", "bytes": 22},
                        "glacier": {"state": "partial", "bytes": 22},
                    },
                }
            ]
        },
        {
            "collections": [
                {
                    "id": "receipts",
                    "bytes": 40,
                    "protected_bytes": 40,
                }
            ]
        },
        {
            "active_uploads": [
                {
                    "collection_id": "photos-2025",
                    "state": "archiving",
                    "files_total": 12,
                    "files_uploaded": 12,
                    "hot_promoted_files": 12,
                    "bytes_total": 4096,
                    "uploaded_bytes": 4096,
                    "archive_phase": "uploading",
                    "archive_uploaded_bytes": 2048,
                    "archive_total_bytes": 4096,
                    "archive_uploaded_parts": 1,
                    "archive_total_parts": 2,
                }
            ]
        },
    )
    assert "active_uploads:" in rendered
    assert (
        "photos-2025 state=archiving files=12/12 hot=12/12 bytes=4096/4096 "
        "phase=uploading archive_bytes=2048/4096 archive_parts=1/2"
    ) in rendered
    assert "ready_to_finalize:" in rendered
    assert "img_2026-04-20_01" in rendered
    assert "waiting_for_future_iso:" in rendered
    assert "img_2026-04-20_02" in rendered
    assert "next: burn, verify" in rendered
    assert "noncompliant_collections:" in rendered
    assert "photos-2024 state=unprotected" in rendered
    assert "docs state=partially_protected" in rendered
    assert "verified_physical=partial 22/55" in rendered
    assert "fully_protected_collections:" in rendered
    assert "receipts protected_bytes=40/40" in rendered


def test_format_collection_summary_surfaces_recovery_paths_labels_and_glacier_costs() -> None:
    rendered = format_collection_summary(
        {
            "id": "docs",
            "files": 2,
            "bytes": 33,
            "hot_bytes": 0,
            "archived_bytes": 33,
            "pending_bytes": 0,
            "protection_state": "partially_protected",
            "protected_bytes": 0,
            "recovery": {
                "available": ["glacier"],
                "verified_physical": {"state": "partial", "bytes": 18},
                "glacier": {"state": "full", "bytes": 33},
            },
            "image_coverage": [
                {
                    "id": "20260420T040001Z",
                    "filename": "20260420T040001Z.iso",
                    "physical_protection_state": "partially_protected",
                    "physical_copies_registered": 1,
                    "physical_copies_verified": 1,
                    "physical_copies_required": 2,
                    "covered_paths": ["tax/2022/invoice-123.pdf"],
                    "copies": [
                        {
                            "id": "20260420T040001Z-1",
                            "label_text": "20260420T040001Z-1",
                            "location": "Shelf B1",
                            "state": "verified",
                            "verification_state": "verified",
                        }
                    ],
                }
            ],
        },
        {
            "collections": [
                {
                    "id": "docs",
                    "bytes": 33,
                    "measured_storage_bytes": 8200,
                    "images": [
                        {
                            "image_id": "20260420T040001Z",
                            "filename": "20260420T040001Z.iso",
                            "represented_bytes": 33,
                        }
                    ],
                }
            ]
        },
    )
    assert "recovery: available=glacier" in rendered
    assert "verified_physical=partial 18/33" in rendered
    assert "glacier=full 33/33" in rendered
    assert "glacier_footprint: bytes=33 measured_storage_bytes=8200" in rendered
    assert "paths: tax/2022/invoice-123.pdf" in rendered
    assert "collection_archive_contribution: represented_bytes=33" in rendered
    assert "label=20260420T040001Z-1" in rendered
    assert "glacier/finalized-images" not in rendered


def test_format_glacier_report_surfaces_collection_storage() -> None:
    rendered = format_glacier_report(
        {
            "scope": "collection",
            "measured_at": "2026-04-28T00:00:00Z",
            "totals": {
                "collections": 1,
                "uploaded_collections": 1,
                "measured_storage_bytes": 8200,
            },
            "images": [
                {
                    "id": "20260420T040001Z",
                    "filename": "20260420T040001Z.iso",
                    "collection_ids": ["docs"],
                }
            ],
            "collections": [
                {
                    "id": "docs",
                    "bytes": 33,
                    "glacier": {"state": "uploaded"},
                    "collection_manifest": {
                        "object_path": "glacier/archives/opaque-abc/manifest.yml.age",
                        "ots_object_path": "glacier/archives/opaque-abc/manifest.yml.ots.age",
                    },
                    "archive_format": "tar",
                    "compression": "none",
                    "measured_storage_bytes": 8200,
                    "images": [
                        {
                            "image_id": "20260420T040001Z",
                            "filename": "20260420T040001Z.iso",
                            "represented_bytes": 33,
                        }
                    ],
                }
            ],
            "history": [
                {
                    "captured_at": "2026-04-28T00:00:00Z",
                    "uploaded_collections": 1,
                    "measured_storage_bytes": 8200,
                }
            ],
        }
    )
    assert "collections=1 uploaded_collections=1" in rendered
    assert "bytes=33 glacier=uploaded ots=uploaded" in rendered
    assert "measured_storage_bytes=8200" in rendered
    assert "pricing_basis:" not in rendered
    assert "billing:" not in rendered
    assert "estimated_billable_bytes=" not in rendered
    assert "estimated_monthly_cost_usd=" not in rendered
    assert "attribution=" not in rendered
    assert "derived_stored_bytes" not in rendered
    assert "glacier/finalized-images" not in rendered
