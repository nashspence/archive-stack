from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path
from typing import Any

from riverhog_cli import local as local_materialization
from riverhog_protocol import (
    CatalogSyncChangePage,
    CatalogSyncCheckpoint,
    CatalogSyncCollectionPage,
    CatalogSyncDelete,
    CatalogSyncDescriptor,
    ImmutableFileIdentityDocument,
    PortableCollectionFile,
    PortableCollectionHeader,
    PortableCollectionInventoryAuthority,
    PortableCollectionInventoryPage,
    portable_collection_inventory_identity,
)
from typer.testing import CliRunner

COLLECTION_ID = 1
CREATED_AT = "2026-07-19T20:55:09.123456Z"
CONTENT = b"locally materialized archive file\n"
SECOND_CONTENT = b"another locally materialized file\n"
MANIFEST = {
    "format": "riverhog-collection/v1",
    "collection": COLLECTION_ID,
    "content_identity": "a" * 64,
    "encryption_format": "age-v1-scrypt",
    "passphrase_id": "collection-test-key-v1",
    "provenance_mode": "omitted",
    "provenance_identity": None,
    "files": [
        {
            "path": "notes/one.txt",
            "bytes": len(CONTENT),
            "sha256": hashlib.sha256(CONTENT).hexdigest(),
        },
        {
            "path": "notes/two.txt",
            "bytes": len(SECOND_CONTENT),
            "sha256": hashlib.sha256(SECOND_CONTENT).hexdigest(),
        },
    ],
}
JOB_FILES = [{"collection_id": COLLECTION_ID, **current} for current in MANIFEST["files"]]


def _inventory(
    *, collection_id: int = COLLECTION_ID, byte_count: int | None = None
) -> tuple[PortableCollectionHeader, tuple[PortableCollectionFile, ...], str]:
    header = PortableCollectionHeader(
        collection=collection_id,
        content_identity=str(MANIFEST["content_identity"]),
        encryption_format=str(MANIFEST["encryption_format"]),
        passphrase_id=str(MANIFEST["passphrase_id"]),
        provenance_mode="omitted",
        provenance_identity=None,
    )
    source = (
        ({"path": "file.bin", "bytes": byte_count, "sha256": "a" * 64},)
        if byte_count is not None
        else tuple(MANIFEST["files"])
    )
    files = tuple(PortableCollectionFile.from_mapping(item) for item in source)
    return header, files, portable_collection_inventory_identity(header, files)


def _prepare_local(target: Path) -> None:
    local_materialization.local_state_schema(target / ".riverhog-local.sqlite3").upgrade()


def test_local_materializer_depends_only_on_client_safe_riverhog_modules() -> None:
    imports = {
        (node.module, alias.name)
        for node in ast.walk(ast.parse(inspect.getsource(local_materialization)))
        if isinstance(node, ast.ImportFrom) and node.module is not None
        for alias in node.names
        if node.module.startswith("riverhog")
    }

    assert imports == {
        ("riverhog_api_client.client", "ApiClient"),
        ("riverhog_api_client.client", "RestorePolicy"),
        ("riverhog_api_client.catalog_sync", "CatalogReplica"),
        ("riverhog_api_client.downloads", "RetrievalDownload"),
        ("riverhog_api_client.downloads", "configured_download_concurrency"),
        ("riverhog_api_client.downloads", "configured_download_window"),
        ("riverhog_api_client.downloads", "download_retrieval_files"),
        ("riverhog_protocol", "validate_collection_tag"),
        ("riverhog_protocol.errors", "InvalidState"),
        ("riverhog_protocol.errors", "NotFound"),
        ("riverhog_protocol.paths", "normalize_collection_id"),
        ("riverhog_protocol.paths", "normalize_relpath"),
        ("riverhog_protocol.transport", "RETRIEVAL_FILE_BATCH_MAX"),
        ("riverhog_provenance", "list_provenance_observers"),
        ("riverhog_provenance", "resolve_provenance_observer"),
        ("riverhog_cli.output", "format_local_collection"),
        ("riverhog_cli.output", "format_local_collections"),
        ("riverhog_cli.local_state", "state_schema"),
        ("riverhog_cli_support.output", "emit"),
        ("riverhog_cli_support.output", "format_list_ids"),
    }


def test_local_state_commands_report_and_verify_the_current_revision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "local"
    monkeypatch.setenv("RIVERHOG_LOCAL_ROOT", str(target))
    runner = CliRunner()

    empty = runner.invoke(local_materialization.local_app, ["state", "status", "--json"])
    upgraded = runner.invoke(local_materialization.local_app, ["state", "upgrade", "--json"])
    verified = runner.invoke(local_materialization.local_app, ["state", "verify", "--json"])

    assert empty.exit_code == upgraded.exit_code == verified.exit_code == 0
    assert json.loads(empty.stdout)["condition"] == "empty"
    assert json.loads(upgraded.stdout)["current_revision"] == "v1_0001"
    assert json.loads(verified.stdout)["condition"] == "current"


def test_local_state_uses_the_configured_database_path(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "local"
    database = tmp_path / "state" / "local.sqlite3"
    database.parent.mkdir()
    monkeypatch.setenv("RIVERHOG_LOCAL_ROOT", str(target))
    monkeypatch.setenv("RIVERHOG_LOCAL_DATABASE", str(database))

    result = CliRunner().invoke(local_materialization.local_app, ["state", "upgrade", "--json"])

    assert result.exit_code == 0
    assert database.is_file()


class FakeApi:
    def __init__(self) -> None:
        self.deleted = False
        self.acknowledged: list[str] = []
        self.canceled: list[str] = []
        self.downloaded_files: list[str] = []
        self.job_state = "ready"
        self.selection = [(COLLECTION_ID, "notes/one.txt")]
        self.restore_paths: set[str] = set()

    def __enter__(self) -> FakeApi:
        return self

    def __exit__(self, *_args: object) -> None:
        return

    def spawn(self) -> FakeApi:
        return self

    def get_portable_collection_inventory(
        self,
        collection_id: int,
        **kwargs: object,
    ) -> PortableCollectionInventoryPage:
        assert collection_id == COLLECTION_ID
        assert kwargs["cursor"] is None
        header, files, inventory_identity = _inventory()
        return PortableCollectionInventoryPage(
            authority=PortableCollectionInventoryAuthority(
                header=header,
                inventory_identity=inventory_identity,
                file_count=len(files),
                file_bytes=sum(file.bytes for file in files),
            ),
            files=[
                ImmutableFileIdentityDocument.model_validate(file.to_mapping()) for file in files
            ],
            complete=True,
        )

    def get_collection(self, collection_id: int) -> dict[str, Any]:
        assert collection_id == COLLECTION_ID
        _header, files, inventory_identity = _inventory()
        return {
            "id": collection_id,
            "created_at": CREATED_AT,
            "inventory_identity": inventory_identity,
            "tag_revision": 1,
            "tag_set_identity": "1" * 64,
            "files": len(files),
            "bytes": sum(file.bytes for file in files),
        }

    def list_collection_tags(
        self,
        collection_id: int,
        *,
        revision: int,
        tag_set_identity: str,
        page_size: int,
        page_token: str | None,
    ) -> dict[str, object]:
        assert (collection_id, revision, tag_set_identity) == (COLLECTION_ID, 1, "1" * 64)
        assert page_size == 100
        assert page_token is None
        return {
            "collection_id": collection_id,
            "revision": revision,
            "tag_set_identity": tag_set_identity,
            "page_size": page_size,
            "next_page_token": None,
            "tags": ["source:camera"],
        }

    def create_catalog_sync_checkpoint(self) -> CatalogSyncCheckpoint:
        return CatalogSyncCheckpoint(
            source_identity="c" * 64,
            authorization_view_identity="d" * 64,
            catalog_cursor="catalog-1",
        )

    def list_catalog_sync_collections(
        self, cursor: str, *, limit: int = 100
    ) -> CatalogSyncCollectionPage:
        assert cursor == "catalog-1"
        assert limit == 100
        return CatalogSyncCollectionPage(
            source_identity="c" * 64,
            authorization_view_identity="d" * 64,
            collections=[
                CatalogSyncDescriptor(
                    collection_id=COLLECTION_ID,
                    archive_root_sha256="e" * 64,
                    content_identity=str(MANIFEST["content_identity"]),
                    description=None,
                    description_revision=0,
                    description_identity="f" * 64,
                    tag_revision=1,
                    tag_set_identity="1" * 64,
                    revision="1",
                )
            ],
            changes_cursor="changes-1",
        )

    def list_catalog_sync_changes(self, cursor: str, *, limit: int = 100) -> CatalogSyncChangePage:
        assert limit == 100
        if self.deleted:
            return CatalogSyncChangePage(
                source_identity="c" * 64,
                authorization_view_identity="d" * 64,
                changes=[CatalogSyncDelete(collection_id=COLLECTION_ID, revision="2")],
                next_cursor="follow-2",
                caught_up=True,
                through_revision="2",
            )
        assert cursor in {"changes-1", "follow-1"}
        return CatalogSyncChangePage(
            source_identity="c" * 64,
            authorization_view_identity="d" * 64,
            changes=[],
            next_cursor="follow-1",
            caught_up=True,
            through_revision="1",
        )

    def plan_retrieval(self, files, **_kwargs: object) -> dict[str, object]:
        self.selection = list(files)
        return {
            "id": "plan-1",
            "etag": "a" * 64,
            "file_count": len(self.selection),
            "requires_restore": False,
        }

    def list_retrieval_plan_files(self, plan_id: str, **kwargs: object) -> dict[str, object]:
        assert plan_id == "plan-1"
        selected = set(self.selection)
        return {
            "plan_id": plan_id,
            "etag": kwargs["plan_etag"],
            "start_ordinal": kwargs["start_ordinal"],
            "files": [
                {
                    **current,
                    "requires_restore": str(current["path"]) in self.restore_paths,
                }
                for current in JOB_FILES
                if (int(current["collection_id"]), str(current["path"])) in selected
            ],
            "complete": True,
            "next_ordinal": None,
        }

    def create_retrieval_job(self, plan_id: str, **_kwargs: object) -> dict[str, object]:
        assert plan_id == "plan-1"
        return self._job()

    def get_retrieval_job(self, job_id: str) -> dict[str, object]:
        assert job_id == "job-1"
        return self._job()

    def renew_retrieval_job(self, job_id: str, *, lease_seconds: int) -> dict[str, object]:
        assert job_id == "job-1"
        assert lease_seconds == 21_600
        return self._job()

    def cancel_retrieval_job(self, job_id: str) -> dict[str, object]:
        self.canceled.append(job_id)
        self.job_state = "canceled"
        return self._job()

    def _job(self) -> dict[str, object]:
        selected = set(self.selection)
        files = [
            current
            for current in JOB_FILES
            if (int(current["collection_id"]), str(current["path"])) in selected
        ]
        return {
            "id": "job-1",
            "state": self.job_state,
            "lease_seconds": 21_600,
            "files": files,
            "objects": [],
        }

    def download_retrieval_file(
        self,
        job_id: str,
        *,
        collection_id: int,
        path: str,
        output: Path,
        expected_bytes: int,
        expected_sha256: str,
    ) -> int:
        assert (job_id, collection_id) == ("job-1", COLLECTION_ID)
        content = {
            "notes/one.txt": CONTENT,
            "notes/two.txt": SECOND_CONTENT,
        }[path]
        assert expected_bytes == len(content)
        assert expected_sha256 == hashlib.sha256(content).hexdigest()
        self.downloaded_files.append(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
        return len(content)

    def acknowledge_retrieval_job(self, job_id: str) -> dict[str, object]:
        self.acknowledged.append(job_id)
        return {"id": job_id, "state": "completed"}


class CacheMissApi(FakeApi):
    def plan_retrieval(self, files, **_kwargs: object) -> dict[str, object]:
        self.selection = list(files)
        self.restore_paths = {path for _collection_id, path in self.selection}
        return {
            "id": "plan-1",
            "etag": "a" * 64,
            "file_count": len(self.selection),
            "requires_restore": True,
        }

    def create_retrieval_job(self, plan_id: str, **_kwargs: object) -> dict[str, object]:
        raise AssertionError(f"opportunistic-only sync must not create a restore job: {plan_id}")


class PartialCacheApi(CacheMissApi):
    def plan_retrieval(self, files, **_kwargs: object) -> dict[str, object]:
        self.selection = list(files)
        cold = [current for current in self.selection if current[1] == "notes/one.txt"]
        self.restore_paths = {path for _collection_id, path in cold}
        return {
            "id": "plan-1",
            "etag": "a" * 64,
            "file_count": len(self.selection),
            "requires_restore": bool(cold),
        }

    def create_retrieval_job(self, plan_id: str, **_kwargs: object) -> dict[str, object]:
        assert plan_id == "plan-1"
        return self._job()


def test_local_materializer_materializes_repairs_and_preserves_remote_deletions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "local"
    api = FakeApi()
    monkeypatch.setenv("RIVERHOG_LOCAL_ROOT", str(target))
    _prepare_local(target)
    monkeypatch.setattr(local_materialization, "ApiClient", lambda: api)
    runner = CliRunner()

    added = runner.invoke(local_materialization.local_app, ["add", str(COLLECTION_ID)])
    synced = runner.invoke(local_materialization.local_app, ["sync"])
    output = target / str(COLLECTION_ID) / "notes/one.txt"

    assert added.exit_code == 0
    assert synced.exit_code == 0
    assert output.read_bytes() == CONTENT
    assert (target / str(COLLECTION_ID) / "notes/two.txt").read_bytes() == SECOND_CONTENT
    assert api.downloaded_files == ["notes/one.txt", "notes/two.txt"]
    assert api.acknowledged == ["job-1"]
    assert runner.invoke(local_materialization.local_app, ["audit"]).exit_code == 0

    output.write_bytes(b"unexpected local bytes")
    repaired = runner.invoke(local_materialization.local_app, ["repair"])
    assert repaired.exit_code == 0
    assert output.read_bytes() == CONTENT
    assert list((target / ".riverhog-local-quarantine").rglob("one.txt"))
    repaired_json = runner.invoke(local_materialization.local_app, ["repair", "--json"])
    assert repaired_json.exit_code == 0
    assert json.loads(repaired_json.stdout)["status"] == "current"

    api.deleted = True
    after_deletion = runner.invoke(local_materialization.local_app, ["sync"])
    listed = runner.invoke(local_materialization.local_app, ["list"])

    assert after_deletion.exit_code == 0
    assert output.read_bytes() == CONTENT
    assert "remote-deleted" in listed.stdout


def test_local_opportunistic_only_sync_leaves_restore_required_files_unrequested(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "local"
    api = CacheMissApi()
    monkeypatch.setenv("RIVERHOG_LOCAL_ROOT", str(target))
    _prepare_local(target)
    monkeypatch.setattr(local_materialization, "ApiClient", lambda: api)
    runner = CliRunner()
    assert (
        runner.invoke(
            local_materialization.local_app,
            ["add", str(COLLECTION_ID)],
        ).exit_code
        == 0
    )

    result = runner.invoke(
        local_materialization.local_app,
        ["sync", "--restore-policy", "never", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "materialized_files": 0,
        "restore_policy": "never",
        "status": "cache-miss",
        "unavailable_files": 2,
    }
    assert api.downloaded_files == []


def test_local_opportunistic_only_sync_continues_past_cold_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "local"
    api = PartialCacheApi()
    monkeypatch.setenv("RIVERHOG_LOCAL_ROOT", str(target))
    _prepare_local(target)
    monkeypatch.setattr(local_materialization, "ApiClient", lambda: api)
    runner = CliRunner()
    assert runner.invoke(local_materialization.local_app, ["add", "1"]).exit_code == 0

    result = runner.invoke(
        local_materialization.local_app,
        ["sync", "--restore-policy", "never", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "materialized_files": 1,
        "restore_policy": "never",
        "status": "cache-miss",
        "unavailable_files": 1,
    }
    assert api.downloaded_files == ["notes/two.txt"]
    assert api.acknowledged == ["job-1"]


def test_local_sync_batches_large_selections_until_current(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "local"
    api = FakeApi()
    monkeypatch.setenv("RIVERHOG_LOCAL_ROOT", str(target))
    monkeypatch.setattr(local_materialization, "RETRIEVAL_FILE_BATCH_MAX", 1)
    _prepare_local(target)
    monkeypatch.setattr(local_materialization, "ApiClient", lambda: api)
    runner = CliRunner()
    assert runner.invoke(local_materialization.local_app, ["add", "1"]).exit_code == 0

    result = runner.invoke(local_materialization.local_app, ["sync", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["materialized_files"] == 2
    assert api.downloaded_files == ["notes/one.txt", "notes/two.txt"]
    assert api.acknowledged == ["job-1", "job-1"]


def test_local_removal_cancels_active_retrieval_before_changing_desired_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "local"
    api = FakeApi()
    api.job_state = "requested"
    monkeypatch.setenv("RIVERHOG_LOCAL_ROOT", str(target))
    _prepare_local(target)
    monkeypatch.setattr(local_materialization, "ApiClient", lambda: api)
    runner = CliRunner()

    assert (
        runner.invoke(local_materialization.local_app, ["add", str(COLLECTION_ID)]).exit_code == 0
    )
    assert runner.invoke(local_materialization.local_app, ["sync"]).exit_code == 0
    removed = runner.invoke(local_materialization.local_app, ["remove", str(COLLECTION_ID)])

    assert removed.exit_code == 0
    assert api.canceled == ["job-1"]
    assert (
        runner.invoke(local_materialization.local_app, ["list"]).stdout
        == "local collections: 0 in this page; next page token: -\n"
    )


def test_local_evict_cancels_active_retrieval_before_removing_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "local"
    api = FakeApi()
    api.job_state = "requested"
    monkeypatch.setenv("RIVERHOG_LOCAL_ROOT", str(target))
    _prepare_local(target)
    monkeypatch.setattr(local_materialization, "ApiClient", lambda: api)
    runner = CliRunner()

    assert runner.invoke(local_materialization.local_app, ["add", "1"]).exit_code == 0
    assert runner.invoke(local_materialization.local_app, ["sync"]).exit_code == 0
    evicted = runner.invoke(local_materialization.local_app, ["evict", "1", "--confirm", "--json"])

    assert evicted.exit_code == 0
    assert json.loads(evicted.stdout) == {
        "collection_id": 1,
        "retrievals_canceled": ["job-1"],
        "status": "evicted",
    }
    assert api.canceled == ["job-1"]
    assert runner.invoke(local_materialization.local_app, ["list", "--json"]).exit_code == 0


def test_local_show_and_actions_have_human_and_json_projections(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "local"
    api = FakeApi()
    monkeypatch.setenv("RIVERHOG_LOCAL_ROOT", str(target))
    _prepare_local(target)
    monkeypatch.setattr(local_materialization, "ApiClient", lambda: api)
    runner = CliRunner()

    added = runner.invoke(local_materialization.local_app, ["add", "1", "--json"])
    shown = runner.invoke(local_materialization.local_app, ["show", "1", "--json"])
    human = runner.invoke(local_materialization.local_app, ["show", "1"])
    audit = runner.invoke(local_materialization.local_app, ["audit", "--json"])
    removed = runner.invoke(local_materialization.local_app, ["remove", "1", "--json"])

    assert added.exit_code == shown.exit_code == human.exit_code == 0
    assert audit.exit_code == 1
    assert json.loads(added.stdout)["collection"]["collection_id"] == 1
    assert json.loads(shown.stdout) == json.loads(added.stdout)["collection"]
    assert "local collection 1" in human.stdout
    assert json.loads(audit.stdout) == {
        "problems": 2,
        "samples": ["missing: 1/notes/one.txt", "missing: 1/notes/two.txt"],
        "samples_truncated": False,
        "status": "issues",
    }
    assert json.loads(removed.stdout)["status"] == "removed"


def test_local_evict_removes_retained_nested_collection_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "local"
    api = FakeApi()
    monkeypatch.setenv("RIVERHOG_LOCAL_ROOT", str(target))
    _prepare_local(target)
    monkeypatch.setattr(local_materialization, "ApiClient", lambda: api)
    runner = CliRunner()

    assert (
        runner.invoke(local_materialization.local_app, ["add", str(COLLECTION_ID)]).exit_code == 0
    )
    assert runner.invoke(local_materialization.local_app, ["sync"]).exit_code == 0
    assert (
        runner.invoke(local_materialization.local_app, ["remove", str(COLLECTION_ID)]).exit_code
        == 0
    )
    assert (target / str(COLLECTION_ID) / "notes/one.txt").exists()

    evicted = runner.invoke(
        local_materialization.local_app,
        ["evict", str(COLLECTION_ID), "--confirm"],
    )

    assert evicted.exit_code == 0
    assert not (target / str(COLLECTION_ID)).exists()


def test_local_list_uses_standard_human_json_and_id_views(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "local"
    api = FakeApi()
    monkeypatch.setenv("RIVERHOG_LOCAL_ROOT", str(target))
    _prepare_local(target)
    monkeypatch.setattr(local_materialization, "ApiClient", lambda: api)
    runner = CliRunner()
    assert runner.invoke(local_materialization.local_app, ["add", "1"]).exit_code == 0

    human = runner.invoke(local_materialization.local_app, ["list", "--query", "desired"])
    machine = runner.invoke(
        local_materialization.local_app,
        ["list", "--query", "desired", "--json"],
    )
    identifiers = runner.invoke(
        local_materialization.local_app,
        ["list", "--query", "desired", "--ids"],
    )

    assert human.exit_code == 0
    assert "local collections: 1 in this page; next page token: -" in human.stdout
    assert "status=desired" in human.stdout
    assert machine.exit_code == 0
    assert json.loads(machine.stdout) == {
        "collections": [
            {
                "bytes": len(CONTENT) + len(SECOND_CONTENT),
                "collection_id": COLLECTION_ID,
                "created_at": CREATED_AT,
                "files": 2,
                "tag_count": 1,
                "status": "desired",
            }
        ],
        "order": "asc",
        "page_size": 25,
        "next_page_token": None,
        "query": "desired",
        "sort": "collection_id",
    }
    by_tag = runner.invoke(
        local_materialization.local_app,
        ["list", "--query", "camera", "--json"],
    )
    assert by_tag.exit_code == 0
    assert [item["collection_id"] for item in json.loads(by_tag.stdout)["collections"]] == [1]
    assert identifiers.exit_code == 0
    assert identifiers.stdout == "1\n"


def test_local_list_pages_and_sorts_database_aggregates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "local"
    monkeypatch.setenv("RIVERHOG_LOCAL_ROOT", str(target))
    target.mkdir()
    _prepare_local(target)
    db = local_materialization._connect(target)
    try:
        for collection_id, byte_count in ((1, 100), (2, 300), (3, 200)):
            _header, _files, inventory_identity = _inventory(
                collection_id=collection_id, byte_count=byte_count
            )
            local_materialization._begin_inventory_refresh(
                db,
                collection_id=collection_id,
                inventory_identity=inventory_identity,
                tag_revision=1,
                tag_set_identity="1" * 64,
                created_at=f"2026-07-19T20:55:0{collection_id}.000000Z",
            )
            local_materialization._store_inventory_page(
                db,
                collection_id=collection_id,
                inventory_identity=inventory_identity,
                files=_files,
                next_cursor=None,
                complete=True,
            )
        db.commit()
    finally:
        db.close()

    runner = CliRunner()
    first = runner.invoke(
        local_materialization.local_app,
        [
            "list",
            "--sort",
            "bytes",
            "--order",
            "desc",
            "--page-size",
            "1",
            "--json",
        ],
    )
    assert first.exit_code == 0
    first_payload = json.loads(first.stdout)
    page = runner.invoke(
        local_materialization.local_app,
        [
            "list",
            "--sort",
            "bytes",
            "--order",
            "desc",
            "--page-size",
            "1",
            "--page-token",
            str(first_payload["next_page_token"]),
            "--json",
        ],
    )
    all_ids = runner.invoke(
        local_materialization.local_app,
        ["list", "--sort", "bytes", "--order", "desc", "--page-size", "3", "--ids"],
    )

    assert page.exit_code == 0
    payload = json.loads(page.stdout)
    assert payload["page_size"] == 1
    assert payload["next_page_token"] is not None
    assert payload["collections"][0]["collection_id"] == 3
    assert payload["collections"][0]["bytes"] == 200
    assert all_ids.exit_code == 0
    assert all_ids.stdout == "2\n3\n1\n"
