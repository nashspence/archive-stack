from __future__ import annotations

import json

from riverhog_cli import main
from riverhog_protocol import (
    CatalogSyncChangePage,
    CatalogSyncCheckpoint,
    CatalogSyncCollectionPage,
    CatalogSyncDescriptor,
    CatalogSyncUpsert,
)
from typer.testing import CliRunner


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def create_catalog_sync_checkpoint(self) -> CatalogSyncCheckpoint:
        self.calls.append(("checkpoint",))
        return CatalogSyncCheckpoint(
            source_identity="a" * 64,
            authorization_view_identity="b" * 64,
            catalog_cursor="catalog-cursor",
        )

    def list_catalog_sync_collections(
        self, cursor: str, *, limit: int
    ) -> CatalogSyncCollectionPage:
        self.calls.append(("collections", cursor, limit))
        return CatalogSyncCollectionPage(
            source_identity="a" * 64,
            authorization_view_identity="b" * 64,
            collections=[
                CatalogSyncDescriptor(
                    collection_id=7,
                    archive_root_sha256="c" * 64,
                    content_identity="d" * 64,
                    revision="11",
                )
            ],
            changes_cursor="changes-cursor",
        )

    def list_catalog_sync_changes(self, cursor: str, *, limit: int) -> CatalogSyncChangePage:
        self.calls.append(("changes", cursor, limit))
        return CatalogSyncChangePage(
            source_identity="a" * 64,
            authorization_view_identity="b" * 64,
            changes=[
                CatalogSyncUpsert(
                    collection_id=7,
                    archive_root_sha256="c" * 64,
                    content_identity="d" * 64,
                    revision="12",
                )
            ],
            next_cursor="next-cursor",
            caught_up=True,
            through_revision="12",
        )


def test_catalog_sync_cli_preserves_one_request_steps_in_rich_and_json(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    client = _Client()
    monkeypatch.setattr(main, "client", lambda: client)
    runner = CliRunner()

    rich_checkpoint = runner.invoke(main.app, ["catalog-sync", "checkpoint"])
    json_checkpoint = runner.invoke(main.app, ["catalog-sync", "checkpoint", "--json"])
    rich_collections = runner.invoke(
        main.app,
        ["catalog-sync", "collections", "--cursor", "catalog-cursor", "--limit", "1"],
    )
    json_changes = runner.invoke(
        main.app,
        ["catalog-sync", "changes", "--cursor", "changes-cursor", "--limit", "1", "--json"],
    )

    assert all(
        result.exit_code == 0
        for result in (rich_checkpoint, json_checkpoint, rich_collections, json_changes)
    )
    assert "catalog cursor: catalog-cursor" in rich_checkpoint.stdout
    assert json.loads(json_checkpoint.stdout)["catalog_cursor"] == "catalog-cursor"
    assert "archive_root_sha256=" + "c" * 64 in rich_collections.stdout
    assert json.loads(json_changes.stdout)["changes"][0]["operation"] == "upsert"
    assert client.calls == [
        ("checkpoint",),
        ("checkpoint",),
        ("collections", "catalog-cursor", 1),
        ("changes", "changes-cursor", 1),
    ]
