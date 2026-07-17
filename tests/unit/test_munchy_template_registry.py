from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from munchy.job_templates import job_template_digest
from munchy.template_registry import (
    TemplateRegistryError,
    create_template_registry_snapshot,
    ensure_template_registry_schema,
    inspect_template_registry_snapshot,
    restore_template_registry_snapshot,
)


def _definition(label: str) -> dict[str, object]:
    return {"schema_version": 1, "kind": "munchy.job", "output": {"label": label}}


def _insert_template(
    conn: sqlite3.Connection,
    *,
    name: str,
    revision: int,
    enabled: bool,
) -> None:
    definition = _definition(name)
    conn.execute(
        """
        INSERT INTO job_templates(
            name, definition, resolved_job, digest, revision, enabled, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            json.dumps(definition, sort_keys=True),
            json.dumps({"output": name}, sort_keys=True),
            job_template_digest(definition),
            revision,
            int(enabled),
            "2026-07-17T00:00:00.000001Z",
            "2026-07-17T00:00:00.000002Z",
        ),
    )


def _rows(path: Path) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT * FROM job_templates ORDER BY name").fetchall()


def test_template_registry_snapshot_restores_only_authoritative_templates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "runner.sqlite3"
    snapshot = tmp_path / "template-registry.sqlite3"
    target = tmp_path / "restored-runner.sqlite3"
    with closing(sqlite3.connect(source)) as conn:
        ensure_template_registry_schema(conn)
        conn.execute("CREATE TABLE job_summaries(job_id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        conn.execute("INSERT INTO job_summaries VALUES('source-job', 'complete')")
        _insert_template(conn, name="archive-example", revision=7, enabled=True)
        _insert_template(conn, name="review-example", revision=3, enabled=False)
        conn.commit()

    assert create_template_registry_snapshot(source, snapshot) == 2
    assert inspect_template_registry_snapshot(snapshot) == 2
    with closing(sqlite3.connect(snapshot)) as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert tables == {"job_templates", "template_registry_snapshot"}

    with closing(sqlite3.connect(target)) as conn:
        conn.execute("CREATE TABLE job_summaries(job_id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        conn.execute("INSERT INTO job_summaries VALUES('target-job', 'running')")
        conn.commit()
    assert restore_template_registry_snapshot(snapshot, target) == 2
    assert _rows(target) == _rows(source)
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT * FROM job_summaries").fetchall() == [
            ("target-job", "running")
        ]


def test_template_registry_restore_requires_explicit_replacement(tmp_path: Path) -> None:
    source = tmp_path / "runner.sqlite3"
    snapshot = tmp_path / "template-registry.sqlite3"
    target = tmp_path / "target.sqlite3"
    for path, name in ((source, "source"), (target, "target")):
        with closing(sqlite3.connect(path)) as conn:
            ensure_template_registry_schema(conn)
            _insert_template(conn, name=name, revision=1, enabled=True)
            conn.commit()
    create_template_registry_snapshot(source, snapshot)

    with pytest.raises(TemplateRegistryError, match="not empty"):
        restore_template_registry_snapshot(snapshot, target)
    assert _rows(target)[0][0] == "target"

    assert restore_template_registry_snapshot(snapshot, target, replace=True) == 1
    assert _rows(target)[0][0] == "source"
