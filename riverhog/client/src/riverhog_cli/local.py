from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import time
from contextlib import closing
from pathlib import Path
from typing import Annotated, Any

import typer
from riverhog_api_client.client import ApiClient
from riverhog_protocol.paths import normalize_collection_id, normalize_relpath, normalize_tag
from time_formats import parse_utc_timestamp

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
            collection_id INTEGER PRIMARY KEY,
            record_etag TEXT NOT NULL,
            created_at TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            remote_deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS desired_files (
            collection_id INTEGER NOT NULL,
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


def _store_manifest(
    db: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    created_at: str,
) -> None:
    collection_id = normalize_collection_id(payload["collection"])
    files = payload.get("files")
    raw_tags = payload.get("tags")
    if (
        payload.get("format") != "riverhog-collection/v2"
        or not isinstance(files, list)
        or not isinstance(raw_tags, list)
    ):
        raise RuntimeError("Riverhog returned an unsupported collection manifest")
    parse_utc_timestamp(created_at)
    tags = sorted({normalize_tag(str(tag)) for tag in raw_tags})
    if tags != raw_tags:
        raise RuntimeError("Riverhog returned non-canonical collection tags")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    etag = hashlib.sha256(canonical).hexdigest()
    db.execute(
        """
        INSERT INTO desired_collections (
            collection_id,
            record_etag,
            created_at,
            tags_json,
            remote_deleted
        )
        VALUES (?, ?, ?, ?, 0)
        ON CONFLICT (collection_id) DO UPDATE SET
            record_etag = excluded.record_etag,
            created_at = excluded.created_at,
            tags_json = excluded.tags_json,
            remote_deleted = 0
        """,
        (collection_id, etag, created_at, json.dumps(tags, separators=(",", ":"))),
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


def _refresh_collection(db: sqlite3.Connection, api: ApiClient, collection_id: int) -> None:
    summary = api.get_collection(collection_id)
    if normalize_collection_id(summary["id"]) != collection_id:
        raise RuntimeError("Riverhog returned the wrong collection summary")
    _store_manifest(
        db,
        api.get_portable_collection_manifest(collection_id),
        created_at=str(summary["created_at"]),
    )


def _output_path(target: Path, collection_id: int, path: str) -> Path:
    output = (target / str(collection_id) / path).resolve()
    if not output.is_relative_to(target):
        raise RuntimeError("materialization path escapes RIVERHOG_LOCAL_ROOT")
    return output


def _projection_name(collection_id: int, created_at: str) -> str:
    timestamp = parse_utc_timestamp(created_at).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}--{collection_id}"


def _collection_is_materialized(
    db: sqlite3.Connection,
    target: Path,
    collection_id: int,
) -> bool:
    paths = [
        str(row["path"])
        for row in db.execute(
            "SELECT path FROM desired_files WHERE collection_id = ? ORDER BY path",
            (collection_id,),
        )
    ]
    collection_dir = target / str(collection_id)
    if not paths:
        return collection_dir.is_dir()
    return all(_output_path(target, collection_id, path).is_file() for path in paths)


def _expected_projection_links(
    db: sqlite3.Connection,
    target: Path,
) -> dict[Path, str]:
    expected: dict[Path, str] = {}
    for row in db.execute(
        """
        SELECT collection_id, created_at, tags_json
        FROM desired_collections
        ORDER BY collection_id
        """
    ):
        collection_id = normalize_collection_id(row["collection_id"])
        if not _collection_is_materialized(db, target, collection_id):
            continue
        name = _projection_name(collection_id, str(row["created_at"]))
        tags = json.loads(str(row["tags_json"]))
        if not isinstance(tags, list):
            raise RuntimeError(f"invalid local tag state for collection {collection_id}")
        directories = (
            [target / "by-tag" / normalize_tag(str(tag)) for tag in tags]
            if tags
            else [target / "untagged"]
        )
        collection_dir = target / str(collection_id)
        for directory in directories:
            link = directory / name
            expected[link] = os.path.relpath(collection_dir, start=directory)
    return expected


def _actual_projection_links(
    target: Path,
    *,
    create_roots: bool,
) -> dict[Path, str]:
    actual: dict[Path, str] = {}
    for root in (target / "by-tag", target / "untagged"):
        if root.is_symlink():
            raise RuntimeError(f"local projection root must not be a symlink: {root}")
        if root.exists() and not root.is_dir():
            raise RuntimeError(f"local projection root is not a directory: {root}")
        if create_roots:
            root.mkdir(parents=True, exist_ok=True)
        if not root.exists():
            continue
        for current in sorted(root.rglob("*")):
            if current.is_symlink():
                actual[current] = os.readlink(current)
            elif not current.is_dir():
                raise RuntimeError(f"local projection contains an unmanaged file: {current}")
    return actual


def _reconcile_projection(db: sqlite3.Connection, target: Path) -> None:
    expected = _expected_projection_links(db, target)
    actual = _actual_projection_links(target, create_roots=True)
    for current, destination in actual.items():
        if expected.get(current) != destination:
            current.unlink()

    for link, destination in sorted(expected.items()):
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() and os.readlink(link) == destination:
            continue
        if link.exists() or link.is_symlink():
            raise RuntimeError(f"local projection path is occupied: {link}")
        link.symlink_to(destination, target_is_directory=True)

    for root in (target / "by-tag", target / "untagged"):
        for directory in sorted(
            (
                current
                for current in root.rglob("*")
                if current.is_dir() and not current.is_symlink()
            ),
            key=lambda current: len(current.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _matches(path: Path, *, byte_count: int, sha256: str) -> bool:
    return path.is_file() and path.stat().st_size == byte_count and _sha256(path) == sha256


def _transfer_object_path(root: Path, *, collection_id: int, object_id: str) -> Path:
    digest = hashlib.sha256(f"{collection_id}\0{object_id}".encode()).hexdigest()
    return root / "objects" / digest


def _extract_pack(
    object_path: Path,
    *,
    placements: list[dict[str, Any]],
    staging_root: Path,
) -> set[tuple[int, str]]:
    selected: dict[str, tuple[int, str, int]] = {}
    for placement in placements:
        collection_id = normalize_collection_id(placement["collection_id"])
        path = normalize_relpath(str(placement["path"]))
        raw_member = placement["member"]
        if not isinstance(raw_member, str):
            raise RuntimeError(f"archive pack member mapping is missing: {collection_id}/{path}")
        member = normalize_relpath(raw_member)
        if member in selected:
            raise RuntimeError(f"duplicate archive pack member mapping: {member}")
        selected[member] = (collection_id, path, int(placement["bytes"]))

    extracted: set[tuple[int, str]] = set()
    with tarfile.open(object_path, mode="r:*") as archive:
        for info in archive:
            member = normalize_relpath(info.name)
            destination = selected.get(member)
            if destination is None:
                continue
            if not info.isfile():
                raise RuntimeError(f"archive pack member is not a file: {member}")
            collection_id, path, expected_bytes = destination
            identity = (collection_id, path)
            if identity in extracted:
                raise RuntimeError(f"duplicate archive pack member: {member}")
            source = archive.extractfile(info)
            if source is None:
                raise RuntimeError(f"archive pack member cannot be read: {member}")
            output = _output_path(staging_root, collection_id, path)
            output.parent.mkdir(parents=True, exist_ok=True)
            byte_count = 0
            with output.open("wb") as handle:
                while chunk := source.read(8 * 1024 * 1024):
                    handle.write(chunk)
                    byte_count += len(chunk)
            if byte_count != expected_bytes:
                raise RuntimeError(f"archive pack member has the wrong size: {member}")
            extracted.add(identity)
    expected = {(current[0], current[1]) for current in selected.values()}
    if extracted != expected:
        missing = sorted(path for collection_id, path in expected - extracted)
        raise RuntimeError(f"archive pack members are missing: {', '.join(missing)}")
    return extracted


def _place_raw_object(
    object_path: Path,
    *,
    placement: dict[str, Any],
    staging_root: Path,
) -> tuple[int, str]:
    collection_id = normalize_collection_id(placement["collection_id"])
    path = normalize_relpath(str(placement["path"]))
    expected_bytes = int(placement["bytes"])
    if object_path.stat().st_size != expected_bytes:
        raise RuntimeError(f"archive object has the wrong placement size: {collection_id}/{path}")
    output = _output_path(staging_root, collection_id, path)
    output.parent.mkdir(parents=True, exist_ok=True)
    mode = "r+b" if output.exists() else "w+b"
    with object_path.open("rb") as source, output.open(mode) as destination:
        destination.seek(int(placement["file_offset"]))
        shutil.copyfileobj(source, destination, length=8 * 1024 * 1024)
    return collection_id, path


def _refresh_catalog(db: sqlite3.Connection, api: ApiClient) -> None:
    row = db.execute("SELECT value FROM settings WHERE key = 'catalog_cursor'").fetchone()
    after = int(row["value"]) if row is not None else 0
    while True:
        changes = api.catalog_changes(after=after)
        for change in changes["changes"]:
            collection_id = normalize_collection_id(change["collection_id"])
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
            elif change["change"] in {"created", "updated"}:
                _refresh_collection(db, api, collection_id)
        cursor = int(changes["cursor"])
        db.execute(
            """
            INSERT INTO settings (key, value) VALUES ('catalog_cursor', ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            (str(cursor),),
        )
        if not changes.get("has_more"):
            return
        if cursor <= after:
            raise RuntimeError("ResourceSync cursor did not advance while changes remained")
        after = cursor


def _missing_files(
    db: sqlite3.Connection,
    target: Path,
    *,
    repair: bool,
) -> list[tuple[int, str]]:
    missing: list[tuple[int, str]] = []
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
        quarantine = target / ".riverhog-local-quarantine" / str(row["collection_id"]) / row["path"]
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
    expected: dict[tuple[int, str], tuple[int, str]] = {}
    for current in job["files"]:
        collection_id = normalize_collection_id(current["collection_id"])
        path = normalize_relpath(str(current["path"]))
        output = _output_path(target, collection_id, path)
        expected_bytes = int(current["bytes"])
        expected_sha256 = str(current["sha256"])
        if output.exists():
            if _matches(output, byte_count=expected_bytes, sha256=expected_sha256):
                continue
            typer.echo(f"mismatch retained: {collection_id}/{path}", err=True)
            continue
        expected[(collection_id, path)] = (expected_bytes, expected_sha256)

    transfer_root = target / ".riverhog-local-transfers" / str(job["id"])
    staging_root = transfer_root / "files"
    shutil.rmtree(transfer_root, ignore_errors=True)
    staged: set[tuple[int, str]] = set()
    try:
        for current in job["objects"]:
            kind = str(current["kind"])
            if kind not in {"pack", "file", "segment"}:
                continue
            collection_id = normalize_collection_id(current["collection_id"])
            placements = [
                {**placement, "collection_id": collection_id}
                for placement in current["placements"]
                if (collection_id, str(placement["path"])) in expected
            ]
            if not placements:
                continue
            object_id = str(current["object_id"])
            object_path = _transfer_object_path(
                transfer_root,
                collection_id=collection_id,
                object_id=object_id,
            )
            api.download_retrieval_object(
                str(job["id"]),
                collection_id=collection_id,
                object_id=object_id,
                output=object_path,
            )
            if not _matches(
                object_path,
                byte_count=int(current["plaintext_bytes"]),
                sha256=str(current["sha256"]),
            ):
                raise RuntimeError(
                    f"retrieved archive object did not match its manifest: "
                    f"{collection_id}/{object_id}"
                )
            if kind == "pack":
                staged.update(
                    _extract_pack(
                        object_path,
                        placements=placements,
                        staging_root=staging_root,
                    )
                )
            else:
                if len(placements) != 1 or placements[0]["member"] is not None:
                    raise RuntimeError(
                        f"raw archive object has invalid placement: {collection_id}/{object_id}"
                    )
                staged.add(
                    _place_raw_object(
                        object_path,
                        placement=placements[0],
                        staging_root=staging_root,
                    )
                )
            object_path.unlink()

        if staged != set(expected):
            missing = sorted(
                f"{collection_id}/{path}" for collection_id, path in set(expected) - staged
            )
            raise RuntimeError(
                f"retrieval did not materialize selected files: {', '.join(missing)}"
            )
        for (collection_id, path), (expected_bytes, expected_sha256) in expected.items():
            staging = _output_path(staging_root, collection_id, path)
            if not _matches(staging, byte_count=expected_bytes, sha256=expected_sha256):
                raise RuntimeError(
                    f"retrieved file did not match its manifest: {collection_id}/{path}"
                )
        for collection_id, path in expected:
            staging = _output_path(staging_root, collection_id, path)
            output = _output_path(target, collection_id, path)
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.exists():
                raise RuntimeError(f"target appeared during retrieval: {collection_id}/{path}")
            staging.replace(output)
    finally:
        shutil.rmtree(transfer_root, ignore_errors=True)
    api.acknowledge_retrieval_job(str(job["id"]))
    db.execute("DELETE FROM retrieval_jobs WHERE id = ?", (str(job["id"]),))
    return len(expected)


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
        _reconcile_projection(db, target)
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
                _reconcile_projection(db, target)
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
        _reconcile_projection(db, target)
        db.commit()
        typer.echo(f"materialized {count} file(s)")


@local_app.command("add")
def add_collection(collection_id: Annotated[int, typer.Argument(help="Collection ID")]) -> None:
    target = _target()
    normalized = normalize_collection_id(collection_id)
    with closing(_connect(target)) as db, ApiClient() as api:
        _refresh_collection(db, api, normalized)
        db.commit()
    typer.echo(f"desired collection added: {normalized}")


@local_app.command("remove")
def remove_collection(
    collection_id: Annotated[int, typer.Argument(help="Collection ID")],
) -> None:
    target = _target()
    normalized = normalize_collection_id(collection_id)
    with closing(_connect(target)) as db, ApiClient() as api:
        _cancel_active_retrievals(db, api)
        db.execute("DELETE FROM desired_collections WHERE collection_id = ?", (normalized,))
        _reconcile_projection(db, target)
        db.commit()
    typer.echo(f"desired collection removed; local files retained: {normalized}")


@local_app.command("list")
def list_collections() -> None:
    target = _target()
    with closing(_connect(target)) as db:
        rows = list(
            db.execute(
                """
                SELECT c.collection_id, c.created_at, c.tags_json, c.remote_deleted,
                       COUNT(f.path) AS files,
                       COALESCE(SUM(f.bytes), 0) AS bytes
                FROM desired_collections AS c
                LEFT JOIN desired_files AS f USING (collection_id)
                GROUP BY c.collection_id, c.created_at, c.tags_json, c.remote_deleted
                ORDER BY c.collection_id
                """
            )
        )
    for row in rows:
        status = "remote-deleted" if row["remote_deleted"] else "desired"
        tags = json.loads(str(row["tags_json"]))
        typer.echo(
            f"{row['collection_id']}  {status}  "
            f"{_projection_name(row['collection_id'], row['created_at'])}  "
            f"tags={','.join(tags) or 'none'}  {row['files']} files  {row['bytes']} bytes"
        )


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
        expected_links = _expected_projection_links(db, target)
        actual_links = _actual_projection_links(target, create_roots=False)
        for link in sorted(set(expected_links) | set(actual_links)):
            relative = link.relative_to(target)
            if link not in actual_links:
                typer.echo(f"projection missing: {relative}")
                problems += 1
            elif link not in expected_links:
                typer.echo(f"projection stale: {relative}")
                problems += 1
            elif actual_links[link] != expected_links[link]:
                typer.echo(f"projection mismatch: {relative}")
                problems += 1
    if problems:
        raise typer.Exit(1)
    typer.echo("materialization matches all desired files")


@local_app.command("evict")
def evict(
    collection_id: Annotated[int, typer.Argument(help="Collection ID")],
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
        collection_dir = target / str(normalized)
        if collection_dir.exists():
            shutil.rmtree(collection_dir)
        db.execute("DELETE FROM desired_collections WHERE collection_id = ?", (normalized,))
        _reconcile_projection(db, target)
        db.commit()
    typer.echo(f"evicted local collection: {normalized}")
