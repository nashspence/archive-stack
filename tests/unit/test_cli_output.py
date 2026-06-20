from __future__ import annotations

import pytest
from rich.console import Console

from riverhog_cli.output import (
    format_collection_summary,
    format_collections,
    format_discs,
    format_glacier_report,
    format_hot_pins,
    format_images,
)


def _render(renderable: object) -> str:
    if isinstance(renderable, str):
        return renderable
    console = Console(record=True, width=120, color_system=None)
    console.print(renderable)
    return console.export_text()


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


def test_format_collections_uses_compact_collection_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render(
        format_collections(
            {
                "page": 1,
                "pages": 1,
                "per_page": 25,
                "total": 1,
                "collections": [
                    {
                        "id": "docs",
                        "files": 2,
                        "bytes": 100,
                        "hot_bytes": 25,
                        "archived_bytes": 100,
                        "protection_state": "partially_protected",
                        "disc_coverage": {
                            "state": "partial",
                            "verified_physical_bytes": 50,
                        },
                    }
                ],
            }
        )
    )

    assert "collections page 1/1" in rendered
    assert "Collection" in rendered
    assert "Protection" in rendered
    assert "docs" in rendered
    assert "partially_protected" in rendered
    assert "partial" in rendered


def test_format_collections_can_fall_back_to_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

    rendered = format_collections(
        {
            "page": 1,
            "pages": 1,
            "per_page": 25,
            "total": 1,
            "collections": [
                {
                    "id": "docs",
                    "files": 2,
                    "bytes": 100,
                    "hot_bytes": 25,
                    "archived_bytes": 100,
                    "protection_state": "partially_protected",
                    "disc_coverage": {
                        "state": "partial",
                        "verified_physical_bytes": 50,
                    },
                }
            ],
        }
    )

    assert isinstance(rendered, str)
    assert "collections: page 1/1" in rendered
    assert "docs protection=partially_protected" in rendered


def test_format_hot_pins_uses_compact_pin_table() -> None:
    rendered = _render(
        format_hot_pins(
            {
                "pins": [
                    {
                        "target": "docs/tax/2022/invoice-123.pdf",
                        "fetch": {
                            "id": "fx-1",
                            "state": "waiting_media",
                            "copies": [{"id": "20260420T040001Z-1"}],
                        },
                    }
                ]
            }
        )
    )

    assert "hot pins" in rendered
    assert "docs/tax/2022/invoice-123.pdf" in rendered
    assert "fx-1" in rendered
    assert "waiting_media" in rendered


def test_format_discs_uses_compact_disc_table() -> None:
    rendered = _render(
        format_discs(
            {
                "discs": [
                    {
                        "id": "20260420T040001Z-1",
                        "image_id": "20260420T040001Z",
                        "state": "verified",
                        "verification_state": "verified",
                        "location": "Shelf B1",
                    }
                ]
            }
        )
    )

    assert "discs" in rendered
    assert "20260420T040001Z-1" in rendered
    assert "verified" in rendered
    assert "Shelf B1" in rendered


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
