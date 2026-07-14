from __future__ import annotations

import pytest
from rich.console import Console

from riverhog_cli.output import (
    format_archive_report,
    format_archive_restore,
    format_archive_restores,
    format_collection_summary,
    format_collection_upload,
    format_collections,
    format_disc,
    format_discs,
    format_fetch,
    format_fetches,
    format_find,
    format_hot_evict,
    format_image,
    format_images,
    format_jeb_archive_plan,
    format_jeb_attempts,
    format_jeb_status,
)


def _render(renderable: object, *, width: int = 120) -> str:
    if isinstance(renderable, str):
        return renderable
    console = Console(record=True, width=width, color_system=None)
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


def test_format_find_uses_target_as_primary_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render_styled(
        format_find(
            {
                "page": 1,
                "pages": 1,
                "per_page": 25,
                "total": 1,
                "sort": "target",
                "order": "asc",
                "query": "invoice",
                "collection": "docs",
                "hot": None,
                "disc_coverage": None,
                "files": [
                    {
                        "target": "docs/tax/2022/invoice-123.pdf",
                        "collection": "docs",
                        "path": "tax/2022/invoice-123.pdf",
                        "bytes": 34,
                        "sha256": "c" * 64,
                        "hot": False,
                        "disc_coverage": True,
                    }
                ],
            }
        )
    )

    assert "docs/tax/2022/invoice-123.pdf" in rendered
    assert rendered.count("38;2;142;201;204") == 1


def test_format_collection_upload_uses_upload_color_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render_styled(
        format_collection_upload(
            {
                "collection_id": "2026/docs",
                "state": "partial",
                "files_uploaded": 1,
                "files_total": 2,
                "uploaded_bytes": 10,
                "bytes_total": 20,
                "files": [
                    {
                        "path": "tax/invoice.pdf",
                        "bytes": 20,
                        "uploaded_bytes": 10,
                        "upload_state": "partial",
                    }
                ],
            }
        )
    )

    assert "2026/docs" in rendered
    assert "tax/invoice.pdf" in rendered
    assert "38;2;192;173;108" in rendered
    assert "38;2;142;201;204" in rendered
    assert "38;2;255;137;51" in rendered


def test_format_archive_restores_use_restore_as_primary_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    payload = {
        "page": 1,
        "pages": 1,
        "per_page": 25,
        "total": 1,
        "sort": "created_at",
        "order": "desc",
        "terminal": "active",
        "type": "disc_rebuild",
        "state": None,
        "collection": None,
        "image": None,
        "restores": [
            {
                "id": "ar-20260420T040001Z-rebuild-1",
                "type": "disc_rebuild",
                "state": "requested",
                "ready_at": None,
                "expires_at": None,
                "collections": [{"id": "docs"}],
                "images": [{"id": "20260420T040001Z", "filename": "20260420T040001Z.iso"}],
            }
        ],
    }

    rendered = _render_styled(format_archive_restores(payload))

    assert "ar-20260420T040001Z-rebuild-1" in rendered
    assert "requested" in rendered
    assert "active" in rendered
    assert rendered.count("38;2;142;201;204") == 1
    assert "38;2;192;173;108" in rendered


def test_format_archive_restore_keeps_related_ids_plain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render_styled(
        format_archive_restore(
            {
                "id": "ar-docs-restore-1",
                "type": "fetch_materialization",
                "state": "ready",
                "created_at": "2026-04-20T04:00:00Z",
                "requested_at": "2026-04-20T04:00:01Z",
                "ready_at": "2026-04-20T04:00:02Z",
                "expires_at": "2026-04-21T04:00:02Z",
                "completed_at": None,
                "paths": ["tax/2022/invoice-123.pdf"],
                "latest_message": "Restored collection files are ready.",
                "warnings": [],
                "progress": {
                    "archive_verification": "completed",
                    "extraction": "pending",
                    "materialization": "pending",
                },
                "collections": [
                    {
                        "id": "docs",
                        "stored_bytes": 120,
                        "archive": {"state": "uploaded"},
                        "collection_manifest": {
                            "object_path": "archive/docs/manifest.yml.age",
                            "ots_state": "uploaded",
                        },
                    }
                ],
                "images": [],
            }
        )
    )

    assert "ar-docs-restore-1" in rendered
    assert "docs" in rendered
    assert rendered.count("38;2;142;201;204") == 1


def test_format_find_renders_long_targets_as_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render(
        format_find(
            {
                "page": 1,
                "pages": 965,
                "per_page": 25,
                "total": 24108,
                "sort": "bytes",
                "order": "desc",
                "query": "reolink",
                "collection": None,
                "hot": None,
                "disc_coverage": None,
                "files": [
                    {
                        "target": (
                            "2026/20260615T030000Z__weekly-device-artifacts/"
                            "reolink-duo-3v-poe-backyard-video/back-yard-camera/"
                            "Back Yard_00_20260603120819.webm"
                        ),
                        "collection": "2026/20260615T030000Z__weekly-device-artifacts",
                        "path": (
                            "reolink-duo-3v-poe-backyard-video/back-yard-camera/"
                            "Back Yard_00_20260603120819.webm"
                        ),
                        "bytes": 88200000,
                        "sha256": "c" * 64,
                        "hot": True,
                        "disc_coverage": True,
                    }
                ],
            }
        ),
        width=72,
    )

    normalized = rendered.replace("\n", "")
    assert "Target" not in rendered
    assert "Collection" not in rendered
    assert (
        "2026/20260615T030000Z__weekly-device-artifacts/"
        "reolink-duo-3v-poe-backyard-video/back-yard-camera/"
        "Back Yard_00_20260603120819.webm"
    ) in normalized
    assert "bytes: 88.2 MB" in rendered


def test_format_images_omits_finalized_image_archive_context(
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
                        "disc_redundancy_state": "partial",
                        "discs_registered": 1,
                        "discs_verified": 0,
                        "discs_required": 2,
                    }
                ],
            }
        )
    )
    assert "0/1/2" in rendered


def test_format_image_lists_collections_vertically(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render(
        format_image(
            {
                "id": "20260420T040001Z",
                "filename": "20260420T040001Z.iso",
                "finalized_at": "2026-04-20T04:00:01Z",
                "files": 2,
                "bytes": 100,
                "target_bytes": 200,
                "fill": 0.5,
                "disc_redundancy_state": "full",
                "discs_registered": 2,
                "discs_verified": 2,
                "discs_required": 2,
                "collections": 2,
                "collection_ids": [
                    "2026/20260414T010101Z__alpha",
                    "2026/20260415T010101Z__beta",
                ],
            }
        )
    )

    assert "20260414T010101Z__alpha, 20260415T010101Z__beta" not in rendered
    assert "2026/20260414T010101Z__alpha" in rendered
    assert "2026/20260415T010101Z__beta" in rendered
    assert rendered.index("2026/20260414T010101Z__alpha") < rendered.index(
        "2026/20260415T010101Z__beta"
    )

    styled = _render_styled(
        format_image(
            {
                "id": "20260420T040001Z",
                "filename": "20260420T040001Z.iso",
                "collection_ids": ["2026/20260414T010101Z__alpha"],
            }
        )
    )
    assert styled.count("38;2;142;201;204") == 1


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
                        "disc_coverage": {"state": "partial", "bytes": 50},
                        "disc_redundancy": {"state": "partial", "bytes": 50},
                    }
                ],
            }
        )
    )

    assert "collections page 1/1" in rendered
    assert "Collection" in rendered
    assert "Disc redundancy" in rendered
    assert "docs" in rendered
    assert "partial" in rendered
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
                        "disc_coverage": {"state": "partial", "bytes": 50},
                        "disc_redundancy": {"state": "partial", "bytes": 0},
                    }
                ],
            }
        )
    )

    assert "38;2;192;173;108" in rendered
    assert "38;2;142;201;204" in rendered
    assert "38;2;255;137;51" in rendered
    assert "docs" in rendered
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
                    "disc_coverage": {"state": "partial", "bytes": 50},
                    "disc_redundancy": {"state": "partial", "bytes": 50},
                }
            ],
        }
    )

    assert isinstance(rendered, str)
    assert "collections: page 1/1" in rendered
    assert "docs files=2 bytes=100" in rendered
    assert "redundancy=partial 50" in rendered


def test_format_fetches_uses_compact_fetch_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render(
        format_fetches(
            {
                "page": 1,
                "pages": 1,
                "per_page": 25,
                "total": 1,
                "fetches": [
                    {
                        "id": "fx-1",
                        "name": "Tax invoice",
                        "targets": ["docs/tax/2022/invoice-123.pdf"],
                        "state": "queued_djdan",
                        "files": 2,
                        "bytes": 33,
                        "missing_bytes": 11,
                    }
                ],
            }
        )
    )

    assert "fetches page 1/1" in rendered
    assert "docs/tax/2022/invoice-123.pdf" in rendered
    assert "fx-1" in rendered
    assert "queued_djdan" in rendered
    assert "2" in rendered
    assert "33 B" in rendered
    header_line = next(
        line for line in rendered.splitlines() if "Fetch" in line and "Targets" in line
    )
    row_line = next(line for line in rendered.splitlines() if "fx-1" in line)
    assert header_line.index("Fetch") < header_line.index("Targets")
    assert row_line.index("fx-1") < row_line.index("docs/tax/2022/invoice-123.pdf")


def test_format_jeb_attempts_uses_compact_operator_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render_styled(
        format_jeb_attempts(
            {
                "page": 1,
                "pages": 3,
                "per_page": 25,
                "total": 51,
                "sort": "updated_at",
                "order": "desc",
                "terminal": "all",
                "filters": {"account": "camera"},
                "attempts": [
                    {
                        "attempt_id": "20260713T010203Z__camera__abc123",
                        "account_id": "camera",
                        "collection_slug": "weekly",
                        "state": "cleanup_done",
                        "file_count": 8,
                        "total_bytes": 1200,
                        "cleanup": "after_target_success",
                        "updated_at": "2026-07-13T01:10:00Z",
                    }
                ],
            }
        )
    )

    assert "jeb attempts page 1/3" in rendered
    assert "20260713T010203Z__camera__abc123" in rendered
    assert "cleanup_done" in rendered
    assert "38;2;142;201;204" in rendered


def test_format_jeb_status_uses_accounts_and_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render(
        format_jeb_status(
            {
                "accounts": [
                    {
                        "id": "camera",
                        "enabled": True,
                        "path_exists": True,
                        "routing_preflight_failed": False,
                        "eligible_files": 2,
                        "eligible_bytes": 2048,
                        "collection_slug": "camera",
                    }
                ],
                "batches": {
                    "total": 4,
                    "active": 1,
                    "terminal": 3,
                    "states": {"munchy_uploaded": 1, "cleanup_done": 3},
                },
                "active_operation": {
                    "id": "op-1",
                    "operation": "archive-now",
                    "account": "camera",
                    "batch_id": "batch-1",
                },
                "active_attempts": {
                    "total": 1,
                    "attempts": [
                        {
                            "attempt_id": "batch-1",
                            "account_id": "camera",
                            "collection_slug": "camera",
                            "state": "munchy_uploaded",
                            "file_count": 2,
                            "total_bytes": 2048,
                            "updated_at": "2026-07-13T01:10:00Z",
                        }
                    ],
                },
                "recent_failures": {"total": 0, "attempts": []},
                "routing_preflight_failures": {"total": 0, "failures": []},
            }
        )
    )

    assert "jeb status" in rendered
    assert "active operation" in rendered
    assert "archive-now" in rendered
    assert "camera" in rendered
    assert "munchy_uploaded" in rendered


def test_format_jeb_archive_plan_can_fall_back_to_plain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

    rendered = format_jeb_archive_plan(
        {
            "status": "would_process",
            "account": "camera",
            "collection_slug": "camera",
            "target_name": "munchy",
            "upload_root": "camera",
            "file_count": 1,
            "total_bytes": 42,
            "cleanup": "delete",
            "process": True,
            "batch_id": "batch-plan",
            "job_id": "job-plan",
            "routing_preflight": {
                "configured": False,
                "ok": True,
                "status": "not_configured",
                "unmatched_count": 0,
                "left_count": 0,
            },
        }
    )

    assert "jeb archive dry-run" in rendered
    assert "status: would_process" in rendered
    assert "account: camera" in rendered
    assert "bytes: 42 B" in rendered
    assert "routing preflight: not_configured ok=true unmatched=0 left=0" in rendered


def test_format_jeb_attempts_can_fall_back_to_plain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

    rendered = format_jeb_attempts(
        {
            "page": 1,
            "pages": 0,
            "per_page": 25,
            "total": 0,
            "sort": "updated_at",
            "order": "desc",
            "terminal": "active",
            "filters": {},
            "attempts": [],
        }
    )

    assert rendered == (
        "jeb attempts: page 1/0 per_page=25 total=0 "
        "sort=updated_at order=desc terminal=active\n- none"
    )


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
                        "disc_id": "20260420T040001Z-1",
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

    styled = _render_styled(
        format_discs(
            {
                "page": 1,
                "pages": 1,
                "per_page": 25,
                "total": 1,
                "discs": [
                    {
                        "disc_id": "20260420T040001Z-1",
                        "image_id": "20260420T040001Z",
                        "state": "verified",
                        "verification_state": "verified",
                        "location": "Shelf B1",
                    }
                ],
            }
        )
    )
    assert styled.count("38;2;142;201;204") == 1


def test_format_disc_uses_detail_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render(
        format_disc(
            {
                "disc_id": "20260420T040001Z-1",
                "image_id": "20260420T040001Z",
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

    styled = _render_styled(
        format_disc(
            {
                "disc_id": "20260420T040001Z-1",
                "image_id": "20260420T040001Z",
                "label_text": "20260420T040001Z-1",
                "location": "Shelf B1",
                "state": "verified",
                "verification_state": "verified",
            }
        )
    )
    assert styled.count("38;2;142;201;204") == 1


def test_format_hot_evict_uses_detail_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render(
        format_hot_evict(
            {
                "targets": ["docs/"],
                "files": 2,
                "bytes": 33,
                "evicted_files": 1,
                "evicted_bytes": 12,
            }
        )
    )

    assert "hot evict" in rendered
    assert "docs/" in rendered
    assert "33 B" in rendered
    assert "12 B" in rendered


def test_format_fetch_uses_entry_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render(
        format_fetch(
            {
                "id": "fx-1",
                "name": "Docs",
                "targets": ["docs/"],
                "state": "queued_djdan",
                "files": 10,
            },
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


def test_format_fetch_omits_empty_entries_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render(
        format_fetch(
            {
                "id": "fx-4",
                "name": "Docs",
                "targets": ["docs/"],
                "state": "done",
                "files": 10,
            },
            {"entries": []},
        )
    )

    assert "entries" not in rendered
    assert "Status" not in rendered
    assert "partial" not in rendered


def test_format_fetch_partial_entries_do_not_use_coverage_attention_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_CLI_PLAIN", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = _render_styled(
        format_fetch(
            {
                "id": "fx-4",
                "name": "Docs",
                "targets": ["docs/"],
                "state": "queued_djdan",
                "files": 10,
            },
            {
                "entries": [
                    {
                        "collection_id": "docs",
                        "path": "tax/2022/invoice-123.pdf",
                        "bytes": 100,
                        "uploaded_bytes": 25,
                        "upload_state": "partial",
                        "upload_state_expires_at": "2026-04-21T00:00:00Z",
                    }
                ]
            },
        )
    )

    assert "partial" in rendered
    assert "38;2;255;137;51" not in rendered


def test_format_collection_summary_surfaces_disc_coverage_labels_and_archive_costs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

    rendered = format_collection_summary(
        {
            "id": "docs",
            "files": 2,
            "bytes": 33,
            "hot_bytes": 0,
            "disc_coverage": {"state": "partial", "bytes": 18},
            "disc_redundancy": {"state": "none", "bytes": 0},
            "image_coverage": [
                {
                    "id": "20260420T040001Z",
                    "filename": "20260420T040001Z.iso",
                    "disc_redundancy_state": "partial",
                    "discs_registered": 1,
                    "discs_verified": 1,
                    "discs_required": 2,
                    "covered_paths": ["tax/2022/invoice-123.pdf"],
                    "discs": [
                        {
                            "disc_id": "20260420T040001Z-1",
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
    assert "disc coverage: partial bytes=18" in rendered
    assert "disc redundancy: none bytes=0" in rendered
    assert "archive_footprint: bytes=33 measured_storage_bytes=8200" in rendered
    assert "paths: tax/2022/invoice-123.pdf" in rendered
    assert "collection_archive_contribution: represented_bytes=33" in rendered
    assert "label=20260420T040001Z-1" in rendered


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
                "collection_manifest": {
                    "object_path": "riverhog/archives/docs/manifest.yml.age",
                    "ots_object_path": "riverhog/archives/docs/manifest.yml.ots.age",
                    "ots_state": "uploaded",
                    "sha256": "abc123",
                },
                "disc_coverage": {"state": "partial", "bytes": 300},
                "disc_redundancy": {"state": "partial", "bytes": 300},
                "image_coverage": [
                    {
                        "id": "20260420T040001Z",
                        "filename": "20260420T040001Z.iso",
                        "disc_redundancy_state": "partial",
                        "discs_registered": 1,
                        "discs_verified": 1,
                        "discs_required": 2,
                        "covered_paths": [f"path-{index}.txt" for index in range(4)],
                        "covered_paths_total": 6,
                        "discs": [
                            {
                                "disc_id": "20260420T040001Z-1",
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
    assert "partial" in rendered
    assert "coverage" in rendered
    assert "20260420T040001Z" in rendered
    assert "path-0.txt" in rendered
    assert "... 2 more" in rendered
    assert "20260420T040001Z-1 @ Shelf B1" in rendered
    assert "300 B" in rendered
    header_line = next(
        line
        for line in rendered.splitlines()
        if "Image" in line and "Archive" in line and "Discs" in line and "Paths" in line
    )
    assert header_line.index("Counts") < header_line.index("Archive") < header_line.index("Discs")
    assert header_line.index("Discs") < header_line.index("Paths")


def test_format_archive_report_surfaces_collection_storage() -> None:
    rendered = format_archive_report(
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
                    "archive": {"state": "uploaded"},
                    "collection_manifest": {
                        "object_path": "archive/archives/opaque-abc/manifest.yml.age",
                        "ots_object_path": "archive/archives/opaque-abc/manifest.yml.ots.age",
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
    assert "bytes=33 archive=uploaded ots=uploaded" in rendered
    assert "measured_storage_bytes=8200" in rendered
    assert "pricing_basis:" not in rendered
    assert "billing:" not in rendered
    assert "estimated_billable_bytes=" not in rendered
    assert "estimated_monthly_cost_usd=" not in rendered
    assert "attribution=" not in rendered
    assert "derived_stored_bytes" not in rendered
    assert "archive/finalized-images" not in rendered
