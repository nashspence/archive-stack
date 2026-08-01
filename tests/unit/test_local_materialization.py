from __future__ import annotations

import ast
import hashlib
import inspect
import io
import json
import tarfile
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
    "format": "riverhog-collection/v2",
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


def _pack_bytes() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for path, content in (("notes/one.txt", CONTENT), ("notes/two.txt", SECOND_CONTENT)):
            info = tarfile.TarInfo(path)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


PACK_BYTES = _pack_bytes()
JOB_OBJECTS = [
    {
        "collection_id": COLLECTION_ID,
        "source_store": "b2",
        "object_id": "data-000000",
        "kind": "pack",
        "plaintext_bytes": len(PACK_BYTES),
        "stored_bytes": len(PACK_BYTES) + 100,
        "sha256": hashlib.sha256(PACK_BYTES).hexdigest(),
        "read_mode": "immediate",
        "placements": [
            {
                "path": current["path"],
                "sequence": 0,
                "file_offset": 0,
                "bytes": current["bytes"],
                "member": current["path"],
            }
            for current in MANIFEST["files"]
        ],
    }
]


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
        ("riverhog_protocol.errors", "InvalidState"),
        ("riverhog_protocol.paths", "normalize_collection_id"),
        ("riverhog_protocol.paths", "normalize_relpath"),
        ("riverhog_protocol.paths", "normalize_tag"),
        ("riverhog_cli.output", "format_local_collections"),
        ("riverhog_cli_support.output", "emit"),
        ("riverhog_cli_support.output", "format_list_ids"),
    }


class FakeApi:
    def __init__(self) -> None:
        self.deleted = False
        self.acknowledged: list[str] = []
        self.canceled: list[str] = []
        self.downloaded_objects: list[str] = []
        self.job_state = "ready"
        self.selection = [(COLLECTION_ID, "notes/one.txt")]
        self.tags = ["docs"]
        self.catalog_revision = 0

    def __enter__(self) -> FakeApi:
        return self

    def __exit__(self, *_args: object) -> None:
        return

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
        objects = [
            {
                **JOB_OBJECTS[0],
                "placements": [
                    current
                    for current in JOB_OBJECTS[0]["placements"]
                    if (COLLECTION_ID, str(current["path"])) in selected
                ],
            }
        ]
        return {
            "id": "job-1",
            "state": self.job_state,
            "files": files,
            "objects": objects,
        }

    def download_retrieval_object(
        self,
        job_id: str,
        *,
        collection_id: int,
        object_id: str,
        output: Path,
        expected_bytes: int,
        expected_sha256: str,
    ) -> int:
        assert (job_id, collection_id, object_id) == (
            "job-1",
            COLLECTION_ID,
            "data-000000",
        )
        assert expected_bytes == len(PACK_BYTES)
        assert expected_sha256 == JOB_OBJECTS[0]["sha256"]
        self.downloaded_objects.append(object_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(PACK_BYTES)
        return len(PACK_BYTES)

    def acknowledge_retrieval_job(self, job_id: str) -> dict[str, object]:
        self.acknowledged.append(job_id)
        return {"id": job_id, "state": "completed"}


def test_local_materializer_materializes_repairs_and_preserves_remote_deletions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "local"
    api = FakeApi()
    monkeypatch.setenv("RIVERHOG_LOCAL_ROOT", str(target))
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
    assert api.downloaded_objects == ["data-000000"]
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

    api.deleted = True
    after_deletion = runner.invoke(local_materialization.local_app, ["sync"])
    listed = runner.invoke(local_materialization.local_app, ["list"])

    assert after_deletion.exit_code == 0
    assert output.read_bytes() == CONTENT
    assert projection.is_symlink()
    assert "remote-deleted" in listed.stdout


def test_local_removal_cancels_active_retrieval_before_changing_desired_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "local"
    api = FakeApi()
    api.job_state = "requested"
    monkeypatch.setenv("RIVERHOG_LOCAL_ROOT", str(target))
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


def test_local_evict_removes_retained_nested_collection_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "local"
    api = FakeApi()
    monkeypatch.setenv("RIVERHOG_LOCAL_ROOT", str(target))
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
    db = local_materialization._connect(target)
    try:
        for collection_id, byte_count in ((1, 100), (2, 300), (3, 200)):
            local_materialization._store_manifest(
                db,
                {
                    "format": "riverhog-collection/v2",
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
    monkeypatch.setattr(local_materialization, "ApiClient", lambda: api)

    result = CliRunner().invoke(local_materialization.local_app, ["sync"])

    assert result.exit_code == 1
    assert isinstance(result.exception, InvalidState)
    assert "projection root must not be a symlink" in str(result.exception)


def test_local_materializer_assembles_sequential_archive_segments(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"first ")
    second.write_bytes(b"second")

    common = {
        "collection_id": COLLECTION_ID,
        "path": "large.bin",
        "member": None,
    }
    local_materialization._place_raw_object(
        first,
        placement={**common, "file_offset": 0, "bytes": first.stat().st_size},
        staging_root=staging,
    )
    local_materialization._place_raw_object(
        second,
        placement={
            **common,
            "file_offset": first.stat().st_size,
            "bytes": second.stat().st_size,
        },
        staging_root=staging,
    )

    assert (staging / str(COLLECTION_ID) / "large.bin").read_bytes() == b"first second"
