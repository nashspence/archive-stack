from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path
from typing import Any

from riverhog_cli import local as local_materialization
from riverhog_protocol.errors import InvalidState
from typer.testing import CliRunner

COLLECTION_ID = 1
CREATED_AT = "2026-07-19T20:55:09.123456Z"
PROJECTION_NAME = "20260719T205509Z--1"
CONTENT = b"locally materialized archive file\n"
SECOND_CONTENT = b"another locally materialized file\n"
MANIFEST = {
    "format": "riverhog-collection/v1",
    "collection": COLLECTION_ID,
    "content_etag": "a" * 64,
    "metadata_revision": 1,
    "tags": ["docs"],
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
        ("riverhog_api_client.downloads", "RetrievalDownload"),
        ("riverhog_api_client.downloads", "configured_download_concurrency"),
        ("riverhog_api_client.downloads", "configured_download_window"),
        ("riverhog_api_client.downloads", "download_retrieval_files"),
        ("riverhog_protocol.errors", "InvalidState"),
        ("riverhog_protocol.errors", "NotFound"),
        ("riverhog_protocol.paths", "normalize_collection_id"),
        ("riverhog_protocol.paths", "normalize_relpath"),
        ("riverhog_protocol.paths", "normalize_tag"),
        ("riverhog_protocol.transport", "RETRIEVAL_FILE_BATCH_MAX"),
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
        self.tags = ["docs"]
        self.catalog_revision = 0

    def __enter__(self) -> FakeApi:
        return self

    def __exit__(self, *_args: object) -> None:
        return

    def spawn(self) -> FakeApi:
        return self

    def get_portable_collection_manifest(self, collection_id: int) -> dict[str, Any]:
        assert collection_id == COLLECTION_ID
        return {**MANIFEST, "tags": list(self.tags)}

    def get_collection(self, collection_id: int) -> dict[str, Any]:
        assert collection_id == COLLECTION_ID
        return {
            "id": collection_id,
            "created_at": CREATED_AT,
            "tags": list(self.tags),
        }

    def catalog_changes(self, *, after: int = 0) -> dict[str, Any]:
        if self.deleted and after < self.catalog_revision + 1:
            return {
                "cursor": self.catalog_revision + 1,
                "changes": [
                    {
                        "collection_id": COLLECTION_ID,
                        "change": "deleted",
                        "etag": hashlib.sha256(b"deleted").hexdigest(),
                    }
                ],
            }
        if after < self.catalog_revision:
            return {
                "cursor": self.catalog_revision,
                "changes": [
                    {
                        "collection_id": COLLECTION_ID,
                        "change": "updated",
                        "etag": hashlib.sha256(b"updated").hexdigest(),
                    }
                ],
            }
        return {"cursor": after, "changes": []}

    def replace_tags(self, *tags: str) -> None:
        self.tags = sorted(tags)
        self.catalog_revision += 1

    def plan_retrieval(self, files, **_kwargs: object) -> dict[str, object]:
        self.selection = list(files)
        return {"etag": "a" * 64}

    def create_retrieval_job(self, files, **_kwargs: object) -> dict[str, object]:
        assert list(files) == self.selection
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
        return {
            "etag": "a" * 64,
            "requires_restore": True,
            "objects": [
                {
                    "collection_id": collection_id,
                    "read_mode": "restore_required",
                    "placements": [{"path": path}],
                }
                for collection_id, path in self.selection
            ],
        }

    def create_retrieval_job(self, files, **_kwargs: object) -> dict[str, object]:
        raise AssertionError(
            f"opportunistic-only sync must not create a restore job: {list(files)}"
        )


class PartialCacheApi(CacheMissApi):
    def plan_retrieval(self, files, **_kwargs: object) -> dict[str, object]:
        self.selection = list(files)
        cold = [current for current in self.selection if current[1] == "notes/one.txt"]
        return {
            "etag": "a" * 64,
            "requires_restore": bool(cold),
            "objects": [
                {
                    "collection_id": collection_id,
                    "read_mode": "restore_required",
                    "placements": [{"path": path}],
                }
                for collection_id, path in cold
            ],
        }

    def create_retrieval_job(self, files, **_kwargs: object) -> dict[str, object]:
        assert list(files) == self.selection
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
    projection = target / "by-tag" / "docs" / PROJECTION_NAME
    assert projection.is_symlink()
    assert projection.resolve() == target / str(COLLECTION_ID)
    assert api.downloaded_files == ["notes/one.txt", "notes/two.txt"]
    assert api.acknowledged == ["job-1"]
    assert runner.invoke(local_materialization.local_app, ["audit"]).exit_code == 0

    projection.unlink()
    audit = runner.invoke(local_materialization.local_app, ["audit"])
    assert audit.exit_code == 1
    assert f"projection missing: by-tag/docs/{PROJECTION_NAME}" in audit.stdout
    assert runner.invoke(local_materialization.local_app, ["sync"]).exit_code == 0
    assert projection.is_symlink()

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
    assert projection.is_symlink()
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
    assert not (target / "by-tag" / "docs" / PROJECTION_NAME).exists()
    assert api.canceled == ["job-1"]
    assert (
        runner.invoke(local_materialization.local_app, ["list"]).stdout
        == "local collections: 0 (page 1/0)\n"
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
    assert not (target / "by-tag" / "docs" / PROJECTION_NAME).exists()


def test_local_projection_tracks_current_tags_without_moving_collection_bytes(
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
    assert runner.invoke(local_materialization.local_app, ["sync"]).exit_code == 0
    collection_dir = target / "1"

    api.replace_tags("photos", "reviewed")
    assert runner.invoke(local_materialization.local_app, ["sync"]).exit_code == 0

    assert collection_dir.joinpath("notes/one.txt").read_bytes() == CONTENT
    assert not (target / "by-tag" / "docs" / PROJECTION_NAME).exists()
    for tag, other_tag in (("photos", "reviewed"), ("reviewed", "photos")):
        link = target / "by-tag" / tag / f"{PROJECTION_NAME}--{other_tag}"
        assert link.is_symlink()
        assert link.resolve() == collection_dir

    api.replace_tags()
    assert runner.invoke(local_materialization.local_app, ["sync"]).exit_code == 0

    assert not (target / "by-tag" / "photos").exists()
    assert not (target / "by-tag" / "reviewed").exists()
    untagged = target / "untagged" / PROJECTION_NAME
    assert untagged.is_symlink()
    assert untagged.resolve() == collection_dir


def test_local_projection_names_append_other_tags_in_canonical_order() -> None:
    assert (
        local_materialization._projection_name(
            COLLECTION_ID,
            CREATED_AT,
            tags=["zebra", "alpha", "middle"],
            parent_tag="middle",
        )
        == f"{PROJECTION_NAME}--alpha--zebra"
    )

    bounded = local_materialization._projection_name(
        COLLECTION_ID,
        CREATED_AT,
        tags=[f"tag-{index}-{'x' * 70}" for index in range(8)],
        parent_tag="tag-0-" + "x" * 70,
    )

    assert len(bounded.encode("utf-8")) <= local_materialization.PROJECTION_NAME_BYTES_MAX
    assert bounded == local_materialization._projection_name(
        COLLECTION_ID,
        CREATED_AT,
        tags=[f"tag-{index}-{'x' * 70}" for index in reversed(range(8))],
        parent_tag="tag-0-" + "x" * 70,
    )


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

    human = runner.invoke(local_materialization.local_app, ["list", "--query", "docs"])
    machine = runner.invoke(
        local_materialization.local_app,
        ["list", "--query", "docs", "--json"],
    )
    identifiers = runner.invoke(
        local_materialization.local_app,
        ["list", "--query", "desired", "--ids"],
    )

    assert human.exit_code == 0
    assert "local collections: 1 (page 1/1)" in human.stdout
    assert "status=desired" in human.stdout
    assert "tags=docs" in human.stdout
    assert machine.exit_code == 0
    assert json.loads(machine.stdout) == {
        "collections": [
            {
                "bytes": len(CONTENT) + len(SECOND_CONTENT),
                "collection_id": COLLECTION_ID,
                "created_at": CREATED_AT,
                "files": 2,
                "status": "desired",
                "tags": ["docs"],
            }
        ],
        "order": "asc",
        "page": 1,
        "pages": 1,
        "per_page": 25,
        "query": "docs",
        "sort": "collection_id",
        "total": 1,
    }
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
            local_materialization._store_manifest(
                db,
                {
                    "format": "riverhog-collection/v1",
                    "collection": collection_id,
                    "tags": ["docs"],
                    "files": [
                        {
                            "path": "file.bin",
                            "bytes": byte_count,
                            "sha256": "a" * 64,
                        }
                    ],
                },
                created_at=f"2026-07-19T20:55:0{collection_id}.000000Z",
            )
        db.commit()
    finally:
        db.close()

    runner = CliRunner()
    page = runner.invoke(
        local_materialization.local_app,
        [
            "list",
            "--sort",
            "bytes",
            "--order",
            "desc",
            "--per-page",
            "1",
            "--page",
            "2",
            "--json",
        ],
    )
    all_ids = runner.invoke(
        local_materialization.local_app,
        ["list", "--sort", "bytes", "--order", "desc", "--all", "--ids"],
    )

    assert page.exit_code == 0
    payload = json.loads(page.stdout)
    assert (payload["page"], payload["per_page"], payload["total"], payload["pages"]) == (
        2,
        1,
        3,
        3,
    )
    assert payload["collections"][0]["collection_id"] == 3
    assert payload["collections"][0]["bytes"] == 200
    assert all_ids.exit_code == 0
    assert all_ids.stdout == "2\n3\n1\n"


def test_local_projection_refuses_an_unmanaged_root_symlink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "local"
    elsewhere = tmp_path / "elsewhere"
    target.mkdir()
    elsewhere.mkdir()
    (target / "by-tag").symlink_to(elsewhere, target_is_directory=True)
    api = FakeApi()
    monkeypatch.setenv("RIVERHOG_LOCAL_ROOT", str(target))
    _prepare_local(target)
    monkeypatch.setattr(local_materialization, "ApiClient", lambda: api)

    result = CliRunner().invoke(local_materialization.local_app, ["sync"])

    assert result.exit_code == 1
    assert isinstance(result.exception, InvalidState)
    assert "projection root must not be a symlink" in str(result.exception)
