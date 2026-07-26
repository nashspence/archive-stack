from __future__ import annotations

import ast
import hashlib
import inspect
import io
import tarfile
from pathlib import Path
from typing import Any

from riverhog_cli import local as local_materialization
from typer.testing import CliRunner

COLLECTION_ID = 1
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
        ("riverhog_protocol.paths", "normalize_collection_id"),
        ("riverhog_protocol.paths", "normalize_relpath"),
    }


class FakeApi:
    def __init__(self) -> None:
        self.deleted = False
        self.acknowledged: list[str] = []
        self.canceled: list[str] = []
        self.downloaded_objects: list[str] = []
        self.job_state = "ready"
        self.selection = [(COLLECTION_ID, "notes/one.txt")]

    def __enter__(self) -> FakeApi:
        return self

    def __exit__(self, *_args: object) -> None:
        return

    def get_portable_collection_manifest(self, collection_id: int) -> dict[str, Any]:
        assert collection_id == COLLECTION_ID
        return MANIFEST

    def catalog_changes(self, *, after: int = 0) -> dict[str, Any]:
        if self.deleted and after < 1:
            return {
                "cursor": 1,
                "changes": [
                    {
                        "collection_id": COLLECTION_ID,
                        "change": "deleted",
                        "etag": hashlib.sha256(b"deleted").hexdigest(),
                    }
                ],
            }
        return {"cursor": after, "changes": []}

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
    ) -> int:
        assert (job_id, collection_id, object_id) == (
            "job-1",
            COLLECTION_ID,
            "data-000000",
        )
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
    assert api.downloaded_objects == ["data-000000"]
    assert api.acknowledged == ["job-1"]

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
        runner.invoke(local_materialization.local_app, ["add", str(COLLECTION_ID)]).exit_code
        == 0
    )
    assert runner.invoke(local_materialization.local_app, ["sync"]).exit_code == 0
    removed = runner.invoke(local_materialization.local_app, ["remove", str(COLLECTION_ID)])

    assert removed.exit_code == 0
    assert api.canceled == ["job-1"]
    assert runner.invoke(local_materialization.local_app, ["list"]).stdout == ""


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
        runner.invoke(local_materialization.local_app, ["add", str(COLLECTION_ID)]).exit_code
        == 0
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
