from __future__ import annotations

import ast
import hashlib
import inspect
from pathlib import Path
from typing import Any

from riverhog_cli import local as local_materialization
from typer.testing import CliRunner

COLLECTION_ID = "2026/20260102T030405Z__local"
CONTENT = b"locally materialized archive file\n"
MANIFEST = {
    "format": "riverhog-collection/v1",
    "collection": COLLECTION_ID,
    "files": [
        {
            "path": "notes/one.txt",
            "bytes": len(CONTENT),
            "sha256": hashlib.sha256(CONTENT).hexdigest(),
        }
    ],
}
JOB_FILES = [{"collection_id": COLLECTION_ID, **MANIFEST["files"][0]}]


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
        self.job_state = "ready"

    def __enter__(self) -> FakeApi:
        return self

    def __exit__(self, *_args: object) -> None:
        return

    def get_portable_collection_manifest(self, collection_id: str) -> dict[str, Any]:
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
        assert files == [(COLLECTION_ID, "notes/one.txt")]
        return {"etag": "a" * 64}

    def create_retrieval_job(self, files, **_kwargs: object) -> dict[str, object]:
        assert files == [(COLLECTION_ID, "notes/one.txt")]
        return {"id": "job-1", "state": self.job_state, "files": JOB_FILES}

    def get_retrieval_job(self, job_id: str) -> dict[str, object]:
        assert job_id == "job-1"
        return {"id": job_id, "state": self.job_state, "files": JOB_FILES}

    def cancel_retrieval_job(self, job_id: str) -> dict[str, object]:
        self.canceled.append(job_id)
        self.job_state = "canceled"
        return {"id": job_id, "state": "canceled", "files": JOB_FILES}

    def download_retrieval_file(
        self,
        job_id: str,
        *,
        collection_id: str,
        path: str,
        output: Path,
    ) -> int:
        assert (job_id, collection_id, path) == ("job-1", COLLECTION_ID, "notes/one.txt")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(CONTENT)
        return len(CONTENT)

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

    added = runner.invoke(local_materialization.local_app, ["add", COLLECTION_ID])
    synced = runner.invoke(local_materialization.local_app, ["sync"])
    output = target / COLLECTION_ID / "notes/one.txt"

    assert added.exit_code == 0
    assert synced.exit_code == 0
    assert output.read_bytes() == CONTENT
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

    assert runner.invoke(local_materialization.local_app, ["add", COLLECTION_ID]).exit_code == 0
    assert runner.invoke(local_materialization.local_app, ["sync"]).exit_code == 0
    removed = runner.invoke(local_materialization.local_app, ["remove", COLLECTION_ID])

    assert removed.exit_code == 0
    assert api.canceled == ["job-1"]
    assert runner.invoke(local_materialization.local_app, ["list"]).stdout == ""
