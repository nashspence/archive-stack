from __future__ import annotations

import pytest
from rich.console import Console

from riverhog_cli.output import (
    format_collection_summary,
    format_collections,
    format_disc,
    format_discs,
    format_fetch,
    format_glacier_report,
    format_hot_pins,
    format_images,
    format_pin,
)


def _render(renderable: object) -> str:
    if isinstance(renderable, str):
        return renderable
    console = Console(record=True, width=120, color_system=None)
    console.print(renderable)
    return console.export_text()


def _render_styled(renderable: object) -> str:
    if isinstance(renderable, str):
        return renderable
    console = Console(
        record=True,
        width=120,
        color_system="truecolor",
        force_terminal=True,
    )
    console.print(renderable)
    return console.export_text(styles=True)


def test_format_images_omits_finalized_image_glacier_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render(
        format_images(
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
    )
    assert "0/1/2" in rendered
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


def test_rich_operator_palette_marks_headers_ids_and_partial_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render_styled(
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

    assert "38;2;192;173;108" in rendered
    assert "38;2;142;201;204" in rendered
    assert "38;2;255;137;51" in rendered
    assert "docs" in rendered
    assert "partially_protected" in rendered


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


def test_format_hot_pins_uses_compact_pin_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render(
        format_hot_pins(
            {
                "page": 1,
                "pages": 1,
                "per_page": 25,
                "total": 1,
                "pins": [
                    {
                        "target": "docs/tax/2022/invoice-123.pdf",
                        "fetch": {
                            "id": "fx-1",
                            "state": "waiting_media",
                            "files": 2,
                            "bytes": 33,
                            "missing_bytes": 11,
                        },
                    }
                ],
            }
        )
    )

    assert "hot pins page 1/1" in rendered
    assert "docs/tax/2022/invoice-123.pdf" in rendered
    assert "fx-1" in rendered
    assert "waiting_media" in rendered
    assert "2" in rendered
    assert "33 B" in rendered


def test_format_discs_uses_compact_disc_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render(
        format_discs(
            {
                "page": 1,
                "pages": 1,
                "per_page": 25,
                "total": 1,
                "discs": [
                    {
                        "id": "20260420T040001Z-1",
                        "image_id": "20260420T040001Z",
                        "state": "verified",
                        "verification_state": "verified",
                        "location": "Shelf B1",
                    }
                ],
            }
        )
    )

    assert "discs page 1/1" in rendered
    assert "20260420T040001Z-1" in rendered
    assert "verified" in rendered
    assert "Shelf B1" in rendered


def test_format_disc_uses_detail_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render(
        format_disc(
            {
                "id": "20260420T040001Z-1",
                "image_id": "20260420T040001Z",
                "volume_id": "20260420T040001Z",
                "label_text": "20260420T040001Z-1",
                "location": "Shelf B1",
                "state": "verified",
                "verification_state": "verified",
                "history": [
                    {
                        "at": "2026-04-20T04:00:01Z",
                        "event": "registered",
                        "location": "Shelf B1",
                        "state": "verified",
                        "verification_state": "verified",
                    }
                ],
            }
        )
    )

    assert "disc" in rendered
    assert "20260420T040001Z-1" in rendered
    assert "Shelf B1" in rendered
    assert "history" in rendered
    assert "registered" in rendered


def test_format_pin_uses_detail_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render(
        format_pin(
            {
                "target": "docs/",
                "pin": True,
                "hot": {"state": "partial", "present_bytes": 12, "missing_bytes": 21},
                "fetch": {
                    "id": "fx-1",
                    "state": "waiting_media",
                    "files": 2,
                    "bytes": 33,
                    "missing_bytes": 21,
                    "copies": [
                        {
                            "id": "copy-1",
                            "label_text": "copy-1",
                            "location": "Shelf B1",
                            "verification_state": "verified",
                        }
                    ],
                },
            }
        )
    )

    assert "hot pin" in rendered
    assert "docs/" in rendered
    assert "waiting_media" in rendered
    assert "copy-1 @ Shelf B1" in rendered


def test_format_fetch_uses_entry_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render(
        format_fetch(
            {"id": "fx-1", "target": "docs/", "state": "waiting_media", "files": 10},
            {
                "entries": [
                    {"path": f"file-{index}.txt", "bytes": 1, "upload_state": "pending"}
                    for index in range(10)
                ]
            },
        )
    )

    assert "fetch" in rendered
    assert "docs/" in rendered
    assert "file-0.txt" in rendered
    assert "... 2 more" in rendered


def test_format_collection_summary_surfaces_recovery_paths_labels_and_glacier_costs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

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


def test_format_collection_summary_uses_rich_detail_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render(
        format_collection_summary(
            {
                "id": "docs",
                "files": 6,
                "bytes": 600,
                "hot_bytes": 600,
                "archived_bytes": 300,
                "pending_bytes": 300,
                "protection_state": "under_protected",
                "protected_bytes": 300,
                "recovery": {
                    "available": ["verified_physical", "glacier"],
                    "verified_physical": {"state": "partial", "bytes": 300},
                    "glacier": {"state": "full", "bytes": 600},
                },
                "collection_manifest": {
                    "object_path": "riverhog/archives/docs/manifest.yml.age",
                    "ots_object_path": "riverhog/archives/docs/manifest.yml.ots.age",
                    "ots_state": "uploaded",
                    "sha256": "abc123",
                },
                "disc_coverage": {
                    "state": "partial",
                    "verified_physical_bytes": 300,
                },
                "image_coverage": [
                    {
                        "id": "20260420T040001Z",
                        "filename": "20260420T040001Z.iso",
                        "physical_protection_state": "partially_protected",
                        "physical_copies_registered": 1,
                        "physical_copies_verified": 1,
                        "physical_copies_required": 2,
                        "covered_paths": [f"path-{index}.txt" for index in range(6)],
                        "copies": [
                            {
                                "id": "20260420T040001Z-1",
                                "label_text": "20260420T040001Z-1",
                                "location": "Shelf B1",
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
                        "bytes": 600,
                        "measured_storage_bytes": 700,
                        "images": [
                            {
                                "image_id": "20260420T040001Z",
                                "represented_bytes": 300,
                            }
                        ],
                    }
                ]
            },
        )
    )

    assert "collection docs" in rendered
    assert "under_protected" in rendered
    assert "coverage" in rendered
    assert "20260420T040001Z" in rendered
    assert "path-0.txt" in rendered
    assert "... 2 more" in rendered
    assert "20260420T040001Z-1 @ Shelf B1" in rendered
    assert "300 B" in rendered


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
