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
from riverhog_cli_support.output import emit, format_list_ids
from riverhog_protocol.errors import InvalidState, NotFound
from riverhog_protocol.paths import normalize_collection_id, normalize_relpath, normalize_tag
from state_schema import StateSchemaError
from time_formats import parse_utc_timestamp

from riverhog_cli.local_state import state_schema as local_state_schema
from riverhog_cli.output import format_local_collection, format_local_collections

local_app = typer.Typer(
    no_args_is_help=True,
    help="Maintain selected archive collections in a local directory.",
)
local_state_app = typer.Typer(no_args_is_help=True, help="Manage local durable state.")
local_app.add_typer(local_state_app, name="state")

LOCAL_LIST_PAGE_SIZE_MAX = 100
LOCAL_LIST_SORT_FIELDS = {
    "bytes": "bytes",
    "collection_id": "collection_id",
    "created_at": "created_at",
    "files": "files",
    "status": "status",
}
PROJECTION_NAME_BYTES_MAX = 240
LOCAL_AUDIT_SAMPLE_LIMIT = 100


def _target(*, create: bool = True) -> Path:
    raw = os.getenv("RIVERHOG_LOCAL_ROOT", "").strip()
    if not raw:
        raise typer.BadParameter("RIVERHOG_LOCAL_ROOT is required")
    target = Path(raw).expanduser().resolve()
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def _database(target: Path) -> Path:
    raw = os.getenv("RIVERHOG_LOCAL_DATABASE", "").strip()
    return Path(raw).expanduser().resolve() if raw else target / ".riverhog-local.sqlite3"


def _connect(target: Path) -> sqlite3.Connection:
    database = _database(target)
    local_state_schema(database).validate()
    db = sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def _state_command(command: str, *, json_mode: bool) -> None:
    target = _target(create=command == "upgrade")
    schema = local_state_schema(_database(target))
    try:
        if command == "status":
            status = schema.status()
        elif command == "upgrade":
            status = schema.upgrade()
        else:
            status = schema.validate()
    except StateSchemaError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    payload = status.as_dict()
    emit(
        payload
        if json_mode
        else (
            f"riverhog local state: {payload['condition']} "
            f"({payload['current_revision'] or 'none'} -> {payload['head_revision']})"
        ),
        json_mode=json_mode,
    )


@local_state_app.command("status")
def state_status(
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show the current and required local-state revisions."""

    _state_command("status", json_mode=json_mode)


@local_state_app.command("upgrade")
def state_upgrade(
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Explicitly upgrade local state to the current revision."""

    _state_command("upgrade", json_mode=json_mode)


@local_state_app.command("verify")
def state_verify(
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Verify the current revision and exact local-state schema."""

    _state_command("verify", json_mode=json_mode)


def _local_collection(db: sqlite3.Connection, collection_id: int) -> dict[str, object]:
    row = db.execute(
        """
        SELECT c.collection_id, c.created_at, c.tags_json,
               CASE c.remote_deleted
                   WHEN 1 THEN 'remote-deleted'
                   ELSE 'desired'
               END AS status,
               COUNT(f.path) AS files,
               COALESCE(SUM(f.bytes), 0) AS bytes
        FROM desired_collections AS c
        LEFT JOIN desired_files AS f USING (collection_id)
        WHERE c.collection_id = ?
        GROUP BY c.collection_id, c.created_at, c.tags_json, c.remote_deleted
        """,
        (collection_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"local collection not found: {collection_id}")
    return {
        "collection_id": int(row["collection_id"]),
        "created_at": str(row["created_at"]),
        "tags": json.loads(str(row["tags_json"])),
        "status": str(row["status"]),
        "files": int(row["files"]),
        "bytes": int(row["bytes"]),
    }


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
        raise InvalidState("Riverhog returned an unsupported collection manifest")
    parse_utc_timestamp(created_at)
    tags = sorted({normalize_tag(str(tag)) for tag in raw_tags})
    if tags != raw_tags:
        raise InvalidState("Riverhog returned non-canonical collection tags")
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
            raise InvalidState("Riverhog returned invalid file metadata")
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
        raise InvalidState("Riverhog returned the wrong collection summary")
    _store_manifest(
        db,
        api.get_portable_collection_manifest(collection_id),
        created_at=str(summary["created_at"]),
    )


def _output_path(target: Path, collection_id: int, path: str) -> Path:
    output = (target / str(collection_id) / path).resolve()
    if not output.is_relative_to(target):
        raise InvalidState("materialization path escapes RIVERHOG_LOCAL_ROOT")
    return output


def _projection_name(
    collection_id: int,
    created_at: str,
    *,
    tags: list[str] | None = None,
    parent_tag: str | None = None,
) -> str:
    timestamp = parse_utc_timestamp(created_at).strftime("%Y%m%dT%H%M%SZ")
    base = f"{timestamp}--{collection_id}"
    other_tags = sorted(tag for tag in tags or [] if tag != parent_tag)
    if not other_tags:
        return base
    full_name = "--".join((base, *other_tags))
    if len(full_name.encode("utf-8")) <= PROJECTION_NAME_BYTES_MAX:
        return full_name
    digest = hashlib.sha256(full_name.encode("utf-8")).hexdigest()[:12]
    suffix = f"--{digest}"
    budget = PROJECTION_NAME_BYTES_MAX - len(suffix)
    return f"{full_name[:budget].rstrip('-')}{suffix}"


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
        tags = json.loads(str(row["tags_json"]))
        if not isinstance(tags, list):
            raise InvalidState(f"invalid local tag state for collection {collection_id}")
        normalized_tags = [normalize_tag(str(tag)) for tag in tags]
        collection_dir = target / str(collection_id)
        if not normalized_tags:
            directory = target / "untagged"
            link = directory / _projection_name(collection_id, str(row["created_at"]))
            expected[link] = os.path.relpath(collection_dir, start=directory)
            continue
        for parent_tag in normalized_tags:
            directory = target / "by-tag" / parent_tag
            link = directory / _projection_name(
                collection_id,
                str(row["created_at"]),
                tags=normalized_tags,
                parent_tag=parent_tag,
            )
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
            raise InvalidState(f"local projection root must not be a symlink: {root}")
        if root.exists() and not root.is_dir():
            raise InvalidState(f"local projection root is not a directory: {root}")
        if create_roots:
            root.mkdir(parents=True, exist_ok=True)
        if not root.exists():
            continue
        for current in sorted(root.rglob("*")):
            if current.is_symlink():
                actual[current] = os.readlink(current)
            elif not current.is_dir():
                raise InvalidState(f"local projection contains an unmanaged file: {current}")
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
            raise InvalidState(f"local projection path is occupied: {link}")
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
            raise InvalidState(f"archive pack member mapping is missing: {collection_id}/{path}")
        member = normalize_relpath(raw_member)
        if member in selected:
            raise InvalidState(f"duplicate archive pack member mapping: {member}")
        selected[member] = (collection_id, path, int(placement["bytes"]))

    extracted: set[tuple[int, str]] = set()
    with tarfile.open(object_path, mode="r:*") as archive:
        for info in archive:
            member = normalize_relpath(info.name)
            destination = selected.get(member)
            if destination is None:
                continue
            if not info.isfile():
                raise InvalidState(f"archive pack member is not a file: {member}")
            collection_id, path, expected_bytes = destination
            identity = (collection_id, path)
            if identity in extracted:
                raise InvalidState(f"duplicate archive pack member: {member}")
            source = archive.extractfile(info)
            if source is None:
                raise InvalidState(f"archive pack member cannot be read: {member}")
            output = _output_path(staging_root, collection_id, path)
            output.parent.mkdir(parents=True, exist_ok=True)
            byte_count = 0
            with output.open("wb") as handle:
                while chunk := source.read(8 * 1024 * 1024):
                    handle.write(chunk)
                    byte_count += len(chunk)
            if byte_count != expected_bytes:
                raise InvalidState(f"archive pack member has the wrong size: {member}")
            extracted.add(identity)
    expected = {(current[0], current[1]) for current in selected.values()}
    if extracted != expected:
        missing = sorted(path for collection_id, path in expected - extracted)
        raise InvalidState(f"archive pack members are missing: {', '.join(missing)}")
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
        raise InvalidState(f"archive object has the wrong placement size: {collection_id}/{path}")
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
            raise InvalidState("ResourceSync cursor did not advance while changes remained")
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
            object_path.parent.mkdir(parents=True, exist_ok=True)
            api.download_retrieval_object(
                str(job["id"]),
                collection_id=collection_id,
                object_id=object_id,
                output=object_path,
                expected_bytes=int(current["plaintext_bytes"]),
                expected_sha256=str(current["sha256"]),
            )
            if not _matches(
                object_path,
                byte_count=int(current["plaintext_bytes"]),
                sha256=str(current["sha256"]),
            ):
                raise InvalidState(
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
                    raise InvalidState(
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
            raise InvalidState(
                f"retrieval did not materialize selected files: {', '.join(missing)}"
            )
        for (collection_id, path), (expected_bytes, expected_sha256) in expected.items():
            staging = _output_path(staging_root, collection_id, path)
            if not _matches(staging, byte_count=expected_bytes, sha256=expected_sha256):
                raise InvalidState(
                    f"retrieved file did not match its manifest: {collection_id}/{path}"
                )
        for collection_id, path in expected:
            staging = _output_path(staging_root, collection_id, path)
            output = _output_path(target, collection_id, path)
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.exists():
                raise InvalidState(f"target appeared during retrieval: {collection_id}/{path}")
            staging.replace(output)
    finally:
        shutil.rmtree(transfer_root, ignore_errors=True)
    api.acknowledge_retrieval_job(str(job["id"]))
    db.execute("DELETE FROM retrieval_jobs WHERE id = ?", (str(job["id"]),))
    return len(expected)


def _cancel_active_retrievals(db: sqlite3.Connection, api: ApiClient) -> list[str]:
    canceled: list[str] = []
    for row in db.execute("SELECT id FROM retrieval_jobs ORDER BY updated_at"):
        job = api.get_retrieval_job(str(row["id"]))
        if job["state"] in {"requested", "ready", "failed"}:
            api.cancel_retrieval_job(str(row["id"]))
            canceled.append(str(row["id"]))
    db.execute("DELETE FROM retrieval_jobs")
    return canceled


def _sync_notice(message: str, *, json_mode: bool) -> None:
    typer.echo(message, err=json_mode)


def _sync(*, wait: bool, repair: bool, json_mode: bool) -> dict[str, object]:
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
                return {
                    "status": "materialized",
                    "retrieval_id": str(job["id"]),
                    "materialized_files": count,
                }
            elif not wait:
                db.commit()
                return {
                    "status": str(job["state"]),
                    "retrieval": job,
                    "materialized_files": 0,
                }

        if active is None:
            missing = _missing_files(db, target, repair=repair)
            if not missing:
                db.commit()
                return {"status": "current", "materialized_files": 0}
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
            _sync_notice(
                f"retrieval {job['id']} is waiting for archive availability",
                json_mode=json_mode,
            )
            time.sleep(10)
            job = api.get_retrieval_job(str(job["id"]))
        if job["state"] != "ready":
            return {
                "status": str(job["state"]),
                "retrieval": job,
                "materialized_files": 0,
            }
        count = _download_job(db, target, api, job)
        _reconcile_projection(db, target)
        db.commit()
        return {
            "status": "materialized",
            "retrieval_id": str(job["id"]),
            "materialized_files": count,
        }


def _format_sync_result(payload: dict[str, object]) -> str:
    status = str(payload.get("status") or "unknown")
    if status == "materialized":
        return f"materialized {payload.get('materialized_files', 0)} file(s)"
    if status == "current":
        return "materialization is current"
    retrieval = payload.get("retrieval")
    retrieval_id = retrieval.get("id") if isinstance(retrieval, dict) else "unknown"
    return f"retrieval {retrieval_id} is {status}; rerun sync later"


@local_app.command("add")
def add_collection(
    collection_id: Annotated[int, typer.Argument(help="Collection ID")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    target = _target()
    normalized = normalize_collection_id(collection_id)
    with closing(_connect(target)) as db, ApiClient() as api:
        _refresh_collection(db, api, normalized)
        collection = _local_collection(db, normalized)
        db.commit()
    payload = {"status": "added", "collection": collection}
    emit(payload if json_mode else f"desired collection added: {normalized}", json_mode=json_mode)


@local_app.command("remove")
def remove_collection(
    collection_id: Annotated[int, typer.Argument(help="Collection ID")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    target = _target()
    normalized = normalize_collection_id(collection_id)
    with closing(_connect(target)) as db, ApiClient() as api:
        canceled = _cancel_active_retrievals(db, api)
        db.execute("DELETE FROM desired_collections WHERE collection_id = ?", (normalized,))
        _reconcile_projection(db, target)
        db.commit()
    payload = {
        "status": "removed",
        "collection_id": normalized,
        "local_files": "retained",
        "retrievals_canceled": canceled,
    }
    emit(
        payload if json_mode else f"desired collection removed; local files retained: {normalized}",
        json_mode=json_mode,
    )


@local_app.command("list")
def list_collections(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[
        int,
        typer.Option("--per-page", min=1, max=LOCAL_LIST_PAGE_SIZE_MAX),
    ] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "collection_id",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Search collection id, tag, or status"),
    ] = None,
    all_items: Annotated[
        bool,
        typer.Option("--all", help="Return every matching local collection"),
    ] = False,
    ids: Annotated[
        bool,
        typer.Option("--ids", help="Emit one collection id per line"),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    if ids and json_mode:
        raise typer.BadParameter("--ids and --json cannot be used together")
    if sort not in LOCAL_LIST_SORT_FIELDS:
        allowed = ", ".join(sorted(LOCAL_LIST_SORT_FIELDS))
        raise typer.BadParameter(f"--sort must be one of: {allowed}")
    normalized_order = order.strip().lower()
    if normalized_order not in {"asc", "desc"}:
        raise typer.BadParameter("--order must be asc or desc")

    target = _target()
    with closing(_connect(target)) as db:
        filters = ""
        params: list[object] = []
        normalized_query = (query or "").strip() or None
        if normalized_query:
            filters = (
                "WHERE CAST(collection_id AS TEXT) LIKE ? "
                "OR lower(tags_json) LIKE lower(?) "
                "OR status LIKE lower(?)"
            )
            pattern = f"%{normalized_query}%"
            params.extend((pattern, pattern, pattern))
        base_query = f"""
                WITH local_collections AS (
                SELECT c.collection_id, c.created_at, c.tags_json, c.remote_deleted,
                       CASE c.remote_deleted
                           WHEN 1 THEN 'remote-deleted'
                           ELSE 'desired'
                       END AS status,
                       COUNT(f.path) AS files,
                       COALESCE(SUM(f.bytes), 0) AS bytes
                FROM desired_collections AS c
                LEFT JOIN desired_files AS f USING (collection_id)
                GROUP BY c.collection_id, c.created_at, c.tags_json, c.remote_deleted
                )
                SELECT * FROM local_collections
                {filters}
                """
        total = int(
            db.execute(
                f"SELECT COUNT(*) FROM ({base_query})",
                params,
            ).fetchone()[0]
        )
        effective_page = 1 if all_items else page
        effective_per_page = total if all_items and total else per_page
        offset = (effective_page - 1) * effective_per_page
        order_column = LOCAL_LIST_SORT_FIELDS[sort]
        rows = db.execute(
            f"""
            {base_query}
            ORDER BY {order_column} {normalized_order.upper()}, collection_id ASC
            LIMIT ? OFFSET ?
            """,
            (*params, effective_per_page, offset),
        )
        collections = [
            {
                "collection_id": int(row["collection_id"]),
                "created_at": str(row["created_at"]),
                "tags": json.loads(str(row["tags_json"])),
                "status": str(row["status"]),
                "files": int(row["files"]),
                "bytes": int(row["bytes"]),
            }
            for row in rows
        ]
    pages = (total + effective_per_page - 1) // effective_per_page if total else 0
    payload = {
        "page": effective_page,
        "per_page": effective_per_page,
        "total": total,
        "pages": pages,
        "sort": sort,
        "order": normalized_order,
        "query": normalized_query,
        "collections": collections,
    }
    if ids:
        emit(
            format_list_ids(payload, "collections", id_key="collection_id"),
            json_mode=False,
        )
        return
    emit(payload if json_mode else format_local_collections(payload), json_mode=json_mode)


@local_app.command("show")
def show_collection(
    collection_id: Annotated[int, typer.Argument(help="Collection ID")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    target = _target()
    normalized = normalize_collection_id(collection_id)
    with closing(_connect(target)) as db:
        payload = _local_collection(db, normalized)
    emit(payload if json_mode else format_local_collection(payload), json_mode=json_mode)


@local_app.command("sync")
def sync(
    wait: Annotated[bool, typer.Option(help="Wait while archival retrieval is pending")] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = _sync(wait=wait, repair=False, json_mode=json_mode)
    emit(payload if json_mode else _format_sync_result(payload), json_mode=json_mode)


@local_app.command("repair")
def repair(
    wait: Annotated[bool, typer.Option(help="Wait while archival retrieval is pending")] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = _sync(wait=wait, repair=True, json_mode=json_mode)
    emit(payload if json_mode else _format_sync_result(payload), json_mode=json_mode)


@local_app.command("audit")
def audit(
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    target = _target()
    problems = 0
    samples: list[str] = []

    def record(message: str) -> None:
        nonlocal problems
        problems += 1
        if len(samples) < LOCAL_AUDIT_SAMPLE_LIMIT:
            samples.append(message)

    with closing(_connect(target)) as db:
        for row in db.execute(
            "SELECT collection_id, path, bytes, sha256 "
            "FROM desired_files ORDER BY collection_id, path"
        ):
            output = _output_path(target, row["collection_id"], row["path"])
            if not output.exists():
                record(f"missing: {row['collection_id']}/{row['path']}")
            elif not _matches(output, byte_count=row["bytes"], sha256=row["sha256"]):
                record(f"mismatch: {row['collection_id']}/{row['path']}")
        expected_links = _expected_projection_links(db, target)
        actual_links = _actual_projection_links(target, create_roots=False)
        for link in sorted(set(expected_links) | set(actual_links)):
            relative = link.relative_to(target)
            if link not in actual_links:
                record(f"projection missing: {relative}")
            elif link not in expected_links:
                record(f"projection stale: {relative}")
            elif actual_links[link] != expected_links[link]:
                record(f"projection mismatch: {relative}")
    payload = {
        "status": "ok" if not problems else "issues",
        "problems": problems,
        "samples": samples,
        "samples_truncated": problems > len(samples),
    }
    if problems:
        if json_mode:
            emit(payload, json_mode=True)
        else:
            typer.echo("\n".join(samples))
            if problems > len(samples):
                typer.echo(f"... {problems - len(samples)} more problem(s)")
        raise typer.Exit(1)
    emit(
        payload if json_mode else "materialization matches all desired files",
        json_mode=json_mode,
    )


@local_app.command("evict")
def evict(
    collection_id: Annotated[int, typer.Argument(help="Collection ID")],
    confirm: Annotated[bool, typer.Option(help="Confirm local file removal")] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    if not confirm:
        raise typer.BadParameter("--confirm is required")
    target = _target()
    normalized = normalize_collection_id(collection_id)
    with closing(_connect(target)) as db, ApiClient() as api:
        canceled = _cancel_active_retrievals(db, api)
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
    payload = {
        "status": "evicted",
        "collection_id": normalized,
        "retrievals_canceled": canceled,
    }
    emit(payload if json_mode else f"evicted local collection: {normalized}", json_mode=json_mode)
