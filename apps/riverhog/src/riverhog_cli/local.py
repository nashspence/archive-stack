from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Annotated, Any

import typer
from riverhog_api_client.client import ApiClient
from riverhog_core.fs_paths import normalize_collection_id, normalize_relpath

local_app = typer.Typer(
    no_args_is_help=True,
    help="Maintain selected archive collections in a local directory.",
)


def _target() -> Path:
    raw = os.getenv("RIVERHOG_LOCAL_ROOT", "").strip()
    if not raw:
        raise typer.BadParameter("RIVERHOG_LOCAL_ROOT is required")
    target = Path(raw).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target


def _database(target: Path) -> Path:
    raw = os.getenv("RIVERHOG_LOCAL_DATABASE", "").strip()
    return Path(raw).expanduser().resolve() if raw else target / ".riverhog-local.sqlite3"


def _connect(target: Path) -> sqlite3.Connection:
    db = sqlite3.connect(_database(target))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS desired_collections (
            collection_id TEXT PRIMARY KEY,
            manifest_etag TEXT NOT NULL,
            remote_deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS desired_files (
            collection_id TEXT NOT NULL,
            path TEXT NOT NULL,
            bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            PRIMARY KEY (collection_id, path),
            FOREIGN KEY (collection_id) REFERENCES desired_collections(collection_id)
                ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS retrieval_jobs (
            id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            files_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    return db


def _store_manifest(db: sqlite3.Connection, payload: dict[str, Any]) -> None:
    collection_id = normalize_collection_id(str(payload["collection"]))
    files = payload.get("files")
    if payload.get("format") != "riverhog-collection/v1" or not isinstance(files, list):
        raise RuntimeError("Riverhog returned an unsupported collection manifest")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    etag = hashlib.sha256(canonical).hexdigest()
    db.execute(
        """
        INSERT INTO desired_collections (collection_id, manifest_etag, remote_deleted)
        VALUES (?, ?, 0)
        ON CONFLICT (collection_id) DO UPDATE SET
            manifest_etag = excluded.manifest_etag,
            remote_deleted = 0
        """,
        (collection_id, etag),
    )
    db.execute("DELETE FROM desired_files WHERE collection_id = ?", (collection_id,))
    for current in files:
        path = normalize_relpath(str(current["path"]))
        byte_count = int(current["bytes"])
        sha256 = str(current["sha256"])
        if byte_count < 0 or len(sha256) != 64:
            raise RuntimeError("Riverhog returned invalid file metadata")
        db.execute(
            """
            INSERT INTO desired_files (collection_id, path, bytes, sha256)
            VALUES (?, ?, ?, ?)
            """,
            (collection_id, path, byte_count, sha256),
        )


def _output_path(target: Path, collection_id: str, path: str) -> Path:
    output = (target / collection_id / path).resolve()
    if not output.is_relative_to(target):
        raise RuntimeError("materialization path escapes RIVERHOG_LOCAL_ROOT")
    return output


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _matches(path: Path, *, byte_count: int, sha256: str) -> bool:
    return path.is_file() and path.stat().st_size == byte_count and _sha256(path) == sha256


def _refresh_catalog(db: sqlite3.Connection, api: ApiClient) -> None:
    row = db.execute("SELECT value FROM settings WHERE key = 'catalog_cursor'").fetchone()
    after = int(row["value"]) if row is not None else 0
    changes = api.catalog_changes(after=after)
    for change in changes["changes"]:
        collection_id = str(change["collection_id"])
        desired = db.execute(
            "SELECT 1 FROM desired_collections WHERE collection_id = ?",
            (collection_id,),
        ).fetchone()
        if desired is None:
            continue
        if change["change"] == "deleted":
            db.execute(
                "UPDATE desired_collections SET remote_deleted = 1 WHERE collection_id = ?",
                (collection_id,),
            )
        elif change["change"] == "created":
            _store_manifest(db, api.get_portable_collection_manifest(collection_id))
    db.execute(
        """
        INSERT INTO settings (key, value) VALUES ('catalog_cursor', ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value
        """,
        (str(changes["cursor"]),),
    )


def _missing_files(
    db: sqlite3.Connection,
    target: Path,
    *,
    repair: bool,
) -> list[tuple[str, str]]:
    missing: list[tuple[str, str]] = []
    for row in db.execute(
        """
        SELECT f.collection_id, f.path, f.bytes, f.sha256
        FROM desired_files AS f
        JOIN desired_collections AS c USING (collection_id)
        WHERE c.remote_deleted = 0
        ORDER BY f.collection_id, f.path
        """
    ):
        output = _output_path(target, row["collection_id"], row["path"])
        if not output.exists():
            missing.append((row["collection_id"], row["path"]))
            continue
        if _matches(output, byte_count=row["bytes"], sha256=row["sha256"]):
            continue
        if not repair:
            typer.echo(f"mismatch retained: {row['collection_id']}/{row['path']}", err=True)
            continue
        quarantine = target / ".riverhog-local-quarantine" / row["collection_id"] / row["path"]
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        candidate = quarantine
        index = 1
        while candidate.exists():
            candidate = quarantine.with_name(f"{quarantine.name}.{index}")
            index += 1
        output.replace(candidate)
        missing.append((row["collection_id"], row["path"]))
    return missing


def _download_job(
    db: sqlite3.Connection,
    target: Path,
    api: ApiClient,
    job: dict[str, Any],
) -> int:
    downloaded = 0
    for current in job["files"]:
        collection_id = str(current["collection_id"])
        path = str(current["path"])
        output = _output_path(target, collection_id, path)
        expected_bytes = int(current["bytes"])
        expected_sha256 = str(current["sha256"])
        if output.exists():
            if _matches(output, byte_count=expected_bytes, sha256=expected_sha256):
                continue
            typer.echo(f"mismatch retained: {collection_id}/{path}", err=True)
            continue
        staging = output.with_name(f".{output.name}.riverhog-download")
        staging.unlink(missing_ok=True)
        api.download_retrieval_file(
            str(job["id"]),
            collection_id=collection_id,
            path=path,
            output=staging,
        )
        if not _matches(staging, byte_count=expected_bytes, sha256=expected_sha256):
            staging.unlink(missing_ok=True)
            raise RuntimeError(f"retrieved file did not match its manifest: {collection_id}/{path}")
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            staging.unlink(missing_ok=True)
            raise RuntimeError(f"target appeared during retrieval: {collection_id}/{path}")
        staging.replace(output)
        downloaded += 1
    api.acknowledge_retrieval_job(str(job["id"]))
    db.execute("DELETE FROM retrieval_jobs WHERE id = ?", (str(job["id"]),))
    return downloaded


def _cancel_active_retrievals(db: sqlite3.Connection, api: ApiClient) -> None:
    for row in db.execute("SELECT id FROM retrieval_jobs ORDER BY updated_at"):
        job = api.get_retrieval_job(str(row["id"]))
        if job["state"] in {"requested", "ready", "failed"}:
            api.cancel_retrieval_job(str(row["id"]))
    db.execute("DELETE FROM retrieval_jobs")


def _sync(*, wait: bool, repair: bool) -> None:
    target = _target()
    with closing(_connect(target)) as db, ApiClient() as api:
        _refresh_catalog(db, api)
        active = db.execute(
            "SELECT id FROM retrieval_jobs ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if active is not None:
            job = api.get_retrieval_job(str(active["id"]))
            if job["state"] in {"expired", "failed", "canceled"}:
                db.execute("DELETE FROM retrieval_jobs WHERE id = ?", (job["id"],))
                active = None
            elif job["state"] == "ready":
                count = _download_job(db, target, api, job)
                db.commit()
                typer.echo(f"materialized {count} file(s)")
                return
            elif not wait:
                db.commit()
                typer.echo(f"retrieval {job['id']} is {job['state']}; rerun sync later")
                return

        if active is None:
            missing = _missing_files(db, target, repair=repair)
            if not missing:
                db.commit()
                typer.echo("materialization is current")
                return
            plan = api.plan_retrieval(missing)
            job = api.create_retrieval_job(missing, plan_etag=str(plan["etag"]))
            db.execute(
                "INSERT INTO retrieval_jobs (id, state, files_json) VALUES (?, ?, ?)",
                (job["id"], job["state"], json.dumps(job["files"])),
            )
            db.commit()
        else:
            job = api.get_retrieval_job(str(active["id"]))

        while job["state"] == "requested" and wait:
            typer.echo(f"retrieval {job['id']} is waiting for archive availability")
            time.sleep(10)
            job = api.get_retrieval_job(str(job["id"]))
        if job["state"] != "ready":
            typer.echo(f"retrieval {job['id']} is {job['state']}; rerun sync later")
            return
        count = _download_job(db, target, api, job)
        db.commit()
        typer.echo(f"materialized {count} file(s)")


@local_app.command("add")
def add_collection(collection_id: Annotated[str, typer.Argument(help="Collection name")]) -> None:
    target = _target()
    normalized = normalize_collection_id(collection_id)
    with closing(_connect(target)) as db, ApiClient() as api:
        _store_manifest(db, api.get_portable_collection_manifest(normalized))
        db.commit()
    typer.echo(f"desired collection added: {normalized}")


@local_app.command("remove")
def remove_collection(
    collection_id: Annotated[str, typer.Argument(help="Collection name")],
) -> None:
    target = _target()
    normalized = normalize_collection_id(collection_id)
    with closing(_connect(target)) as db, ApiClient() as api:
        _cancel_active_retrievals(db, api)
        db.execute("DELETE FROM desired_collections WHERE collection_id = ?", (normalized,))
        db.commit()
    typer.echo(f"desired collection removed; local files retained: {normalized}")


@local_app.command("list")
def list_collections() -> None:
    target = _target()
    with closing(_connect(target)) as db:
        rows = list(
            db.execute(
                """
                SELECT c.collection_id, c.remote_deleted, COUNT(f.path) AS files,
                       COALESCE(SUM(f.bytes), 0) AS bytes
                FROM desired_collections AS c
                LEFT JOIN desired_files AS f USING (collection_id)
                GROUP BY c.collection_id, c.remote_deleted
                ORDER BY c.collection_id
                """
            )
        )
    for row in rows:
        status = "remote-deleted" if row["remote_deleted"] else "desired"
        typer.echo(f"{row['collection_id']}  {status}  {row['files']} files  {row['bytes']} bytes")


@local_app.command("sync")
def sync(
    wait: Annotated[bool, typer.Option(help="Wait while archival retrieval is pending")] = False,
) -> None:
    _sync(wait=wait, repair=False)


@local_app.command("repair")
def repair(
    wait: Annotated[bool, typer.Option(help="Wait while archival retrieval is pending")] = False,
) -> None:
    _sync(wait=wait, repair=True)


@local_app.command("audit")
def audit() -> None:
    target = _target()
    problems = 0
    with closing(_connect(target)) as db:
        for row in db.execute(
            "SELECT collection_id, path, bytes, sha256 "
            "FROM desired_files ORDER BY collection_id, path"
        ):
            output = _output_path(target, row["collection_id"], row["path"])
            if not output.exists():
                typer.echo(f"missing: {row['collection_id']}/{row['path']}")
                problems += 1
            elif not _matches(output, byte_count=row["bytes"], sha256=row["sha256"]):
                typer.echo(f"mismatch: {row['collection_id']}/{row['path']}")
                problems += 1
    if problems:
        raise typer.Exit(1)
    typer.echo("materialization matches all desired files")


@local_app.command("evict")
def evict(
    collection_id: Annotated[str, typer.Argument(help="Collection name")],
    confirm: Annotated[bool, typer.Option(help="Confirm local file removal")] = False,
) -> None:
    if not confirm:
        raise typer.BadParameter("--confirm is required")
    target = _target()
    normalized = normalize_collection_id(collection_id)
    with closing(_connect(target)) as db:
        rows = list(
            db.execute(
                "SELECT path FROM desired_files WHERE collection_id = ? ORDER BY path",
                (normalized,),
            )
        )
        for row in rows:
            _output_path(target, normalized, row["path"]).unlink(missing_ok=True)
        collection_dir = target / normalized
        if collection_dir.exists():
            for directory in sorted(collection_dir.rglob("*"), reverse=True):
                if directory.is_dir():
                    directory.rmdir()
            collection_dir.rmdir()
        db.execute("DELETE FROM desired_collections WHERE collection_id = ?", (normalized,))
        db.commit()
    typer.echo(f"evicted local collection: {normalized}")
