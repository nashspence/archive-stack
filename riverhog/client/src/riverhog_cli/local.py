from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from riverhog_api_client.client import ApiClient, RestorePolicy
from riverhog_api_client.downloads import (
    RetrievalDownload,
    configured_download_concurrency,
    configured_download_window,
    download_retrieval_files,
)
from riverhog_cli_support.output import emit, format_list_ids
from riverhog_protocol.errors import InvalidState, NotFound
from riverhog_protocol.paths import normalize_collection_id, normalize_relpath, normalize_tag
from riverhog_protocol.portable_collection import PortableCollectionRecord
from riverhog_protocol.transport import RETRIEVAL_FILE_BATCH_MAX
from riverhog_provenance import list_provenance_observers, resolve_provenance_observer
from state_schema import StateSchemaError
from time_formats import parse_utc_timestamp

from riverhog_cli.local_state import state_schema as local_state_schema
from riverhog_cli.output import format_local_collection, format_local_collections

local_app = typer.Typer(
    no_args_is_help=True,
    help="Maintain selected archive collections in a local directory.",
)
local_state_app = typer.Typer(no_args_is_help=True, help="Manage local durable state.")
local_provenance_observer_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect explicitly composable local provenance observers.",
)
local_app.add_typer(local_state_app, name="state")
local_app.add_typer(local_provenance_observer_app, name="provenance-observer")

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
RETRIEVAL_RENEW_INTERVAL_MAX_SECONDS = 60 * 60


@local_provenance_observer_app.command("list")
def provenance_observer_list(
    ids: Annotated[
        bool,
        typer.Option("--ids", help="Emit one provider name per line."),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List installed observer metadata without executing provider code."""

    if ids and json_mode:
        raise typer.BadParameter("--ids and --json cannot be used together")
    providers = [item.as_dict() for item in list_provenance_observers()]
    payload = {
        "format": "riverhog-provenance-observer-provider-list/v1",
        "providers": providers,
    }
    human = [f"provenance observers: {len(providers)}"]
    human.extend(
        f"- {item['name']}  distribution={item['distribution'] or 'unknown'}  "
        f"version={item['version'] or 'unknown'}"
        for item in providers
    )
    if ids:
        emit(format_list_ids(payload, "providers", id_key="name"), json_mode=False)
        return
    emit(payload if json_mode else "\n".join(human), json_mode=json_mode)


@local_provenance_observer_app.command("show")
def provenance_observer_show(
    name: Annotated[str, typer.Argument(help="Exact installed observer provider name")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Load one selected provider and show its exact observer/contract identity."""

    try:
        resolved = resolve_provenance_observer(name)
        payload = resolved.as_dict()
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="name") from exc
    human = "\n".join(
        (
            f"provenance observer {payload['name']}",
            f"observer: {payload['observer_id']}",
            f"contract provider: {payload['contract_provider']}",
            f"contract: {payload['contract_id']}",
            f"contract sha256: {payload['contract_sha256']}",
            f"schemas: {len(resolved.contract.schemas)}",
        )
    )
    emit(payload if json_mode else human, json_mode=json_mode)


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
    record: PortableCollectionRecord,
    *,
    created_at: str,
) -> None:
    collection_id = normalize_collection_id(record.collection)
    parse_utc_timestamp(created_at)
    tags = list(record.tags)
    etag = record.identity
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
    for current in record.files:
        db.execute(
            """
            INSERT INTO desired_files (collection_id, path, bytes, sha256)
            VALUES (?, ?, ?, ?)
            """,
            (collection_id, current.path, current.bytes, current.sha256),
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
    lease_seconds = int(job["lease_seconds"])
    job = api.renew_retrieval_job(
        str(job["id"]),
        lease_seconds=lease_seconds,
    )
    persisted_files = tuple(
        (
            int(row["collection_id"]),
            str(row["path"]),
            int(row["bytes"]),
            str(row["sha256"]),
        )
        for row in db.execute(
            "SELECT collection_id, path, bytes, sha256 FROM retrieval_job_files "
            "WHERE retrieval_job_id = ? ORDER BY ordinal",
            (str(job["id"]),),
        )
    )
    reported_files = tuple(
        (
            normalize_collection_id(current["collection_id"]),
            normalize_relpath(str(current["path"])),
            int(current["bytes"]),
            str(current["sha256"]),
        )
        for current in job["files"]
    )
    if reported_files != persisted_files:
        raise InvalidState("retrieval job membership changed after local checkpoint")
    expected: dict[tuple[int, str], tuple[int, str]] = {}
    for collection_id, path, expected_bytes, expected_sha256 in persisted_files:
        output = _output_path(target, collection_id, path)
        if output.exists():
            if _matches(output, byte_count=expected_bytes, sha256=expected_sha256):
                continue
            typer.echo(f"mismatch retained: {collection_id}/{path}", err=True)
            continue
        expected[(collection_id, path)] = (expected_bytes, expected_sha256)

    transfer_root = target / ".riverhog-local-transfers" / str(job["id"])
    staging_root = transfer_root / "files"
    shutil.rmtree(transfer_root, ignore_errors=True)
    try:
        downloads: list[RetrievalDownload] = []
        for (collection_id, path), (expected_bytes, expected_sha256) in expected.items():
            staging = _output_path(staging_root, collection_id, path)
            staging.parent.mkdir(parents=True, exist_ok=True)
            downloads.append(
                RetrievalDownload(
                    collection_id=collection_id,
                    path=path,
                    output=staging,
                    expected_bytes=expected_bytes,
                    expected_sha256=expected_sha256,
                )
            )
        concurrency = configured_download_concurrency()

        def maintain_lease() -> None:
            api.renew_retrieval_job(
                str(job["id"]),
                lease_seconds=lease_seconds,
            )

        download_retrieval_files(
            api,
            str(job["id"]),
            downloads,
            concurrency=concurrency,
            window=configured_download_window(concurrency=concurrency),
            heartbeat=maintain_lease,
            heartbeat_interval_seconds=max(
                0.1,
                min(RETRIEVAL_RENEW_INTERVAL_MAX_SECONDS, lease_seconds / 3),
            ),
        )
        for download in downloads:
            if not _matches(
                download.output,
                byte_count=download.expected_bytes,
                sha256=download.expected_sha256,
            ):
                raise InvalidState(
                    "retrieved file did not match its catalog identity: "
                    f"{download.collection_id}/{download.path}"
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


def _sync(
    *,
    wait: bool,
    repair: bool,
    restore_policy: str,
    json_mode: bool,
) -> dict[str, object]:
    if restore_policy not in {"allow", "never"}:
        raise typer.BadParameter("--restore-policy must be allow or never")
    policy = cast(RestorePolicy, restore_policy)
    target = _target()
    with closing(_connect(target)) as db, ApiClient() as api:
        _refresh_catalog(db, api)
        _reconcile_projection(db, target)
        materialized_files = 0
        unavailable: set[tuple[int, str]] = set()
        last_retrieval_id: str | None = None

        while True:
            active = db.execute(
                "SELECT id FROM retrieval_jobs ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            job: dict[str, Any] | None = None
            if active is not None:
                job = api.get_retrieval_job(str(active["id"]))
                if job["state"] in {"expired", "failed", "canceled"}:
                    db.execute("DELETE FROM retrieval_jobs WHERE id = ?", (job["id"],))
                    db.commit()
                    job = None
                elif job["state"] != "ready" and not wait:
                    db.commit()
                    return {
                        "status": str(job["state"]),
                        "retrieval": job,
                        "materialized_files": materialized_files,
                    }

            if job is None:
                missing = [
                    current
                    for current in _missing_files(db, target, repair=repair)
                    if current not in unavailable
                ]
                if not missing:
                    db.commit()
                    if unavailable:
                        return {
                            "status": "cache-miss",
                            "restore_policy": restore_policy,
                            "materialized_files": materialized_files,
                            "unavailable_files": len(unavailable),
                        }
                    payload: dict[str, object] = {
                        "status": "materialized" if materialized_files else "current",
                        "materialized_files": materialized_files,
                    }
                    if last_retrieval_id is not None:
                        payload["retrieval_id"] = last_retrieval_id
                    return payload

                batch = missing[:RETRIEVAL_FILE_BATCH_MAX]
                plan = api.plan_retrieval(batch, restore_policy=policy)
                if policy == "never" and plan.get("requires_restore"):
                    blocked = {
                        (
                            normalize_collection_id(current["collection_id"]),
                            normalize_relpath(str(placement["path"])),
                        )
                        for current in plan.get("objects", [])
                        if current.get("read_mode") == "restore_required"
                        for placement in current.get("placements", [])
                    }
                    unavailable.update(blocked)
                    batch = [current for current in batch if current not in blocked]
                    if not batch:
                        continue
                    plan = api.plan_retrieval(batch, restore_policy=policy)
                job = api.create_retrieval_job(
                    batch,
                    plan_etag=str(plan["etag"]),
                    restore_policy=policy,
                )
                db.execute(
                    "INSERT INTO retrieval_jobs (id, state) VALUES (?, ?)",
                    (job["id"], job["state"]),
                )
                db.executemany(
                    """
                    INSERT INTO retrieval_job_files (
                        retrieval_job_id, ordinal, collection_id, path, bytes, sha256
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            job["id"],
                            ordinal,
                            current["collection_id"],
                            current["path"],
                            current["bytes"],
                            current["sha256"],
                        )
                        for ordinal, current in enumerate(job["files"])
                    ),
                )
                db.commit()

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
                    "materialized_files": materialized_files,
                }

            materialized_files += _download_job(db, target, api, job)
            last_retrieval_id = str(job["id"])
            _reconcile_projection(db, target)
            db.commit()


def _format_sync_result(payload: dict[str, object]) -> str:
    status = str(payload.get("status") or "unknown")
    if status == "materialized":
        return f"materialized {payload.get('materialized_files', 0)} file(s)"
    if status == "current":
        return "materialization is current"
    if status == "cache-miss":
        raw_materialized = payload.get("materialized_files", 0)
        materialized = raw_materialized if isinstance(raw_materialized, int) else 0
        prefix = f"materialized {materialized} file(s); " if materialized else ""
        return prefix + (
            f"{payload.get('unavailable_files', 0)} remaining file(s) would require archive restore"
        )
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
        order_column = LOCAL_LIST_SORT_FIELDS[sort]
        if all_items:
            rows = db.execute(
                f"""
                {base_query}
                ORDER BY {order_column} {normalized_order.upper()}, collection_id ASC
                """,
                params,
            )
            _emit_local_collection_enumeration(
                rows,
                ids=ids,
                json_mode=json_mode,
            )
            return
        total = int(
            db.execute(
                f"SELECT COUNT(*) FROM ({base_query})",
                params,
            ).fetchone()[0]
        )
        offset = (page - 1) * per_page
        rows = db.execute(
            f"""
            {base_query}
            ORDER BY {order_column} {normalized_order.upper()}, collection_id ASC
            LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        )
        collections = [_local_collection_list_item(row) for row in rows]
    pages = (total + per_page - 1) // per_page if total else 0
    payload = {
        "page": page,
        "per_page": per_page,
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


def _local_collection_list_item(row: sqlite3.Row) -> dict[str, object]:
    return {
        "collection_id": int(row["collection_id"]),
        "created_at": str(row["created_at"]),
        "tags": json.loads(str(row["tags_json"])),
        "status": str(row["status"]),
        "files": int(row["files"]),
        "bytes": int(row["bytes"]),
    }


def _emit_local_collection_enumeration(
    rows: sqlite3.Cursor,
    *,
    ids: bool,
    json_mode: bool,
) -> None:
    count = 0
    if json_mode:
        sys.stdout.write('{"collections":[')
    first_chunk = True
    while current := rows.fetchmany(LOCAL_LIST_PAGE_SIZE_MAX):
        items = [_local_collection_list_item(row) for row in current]
        if ids:
            for item in items:
                typer.echo(str(item["collection_id"]))
        elif json_mode:
            for item in items:
                if count:
                    sys.stdout.write(",")
                sys.stdout.write(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                sys.stdout.flush()
                count += 1
        else:
            rendered = format_local_collections(
                {"collections": items, "_complete_enumeration": True}
            ).splitlines()
            if not first_chunk and rendered:
                rendered = rendered[1:]
            if rendered:
                typer.echo("\n".join(rendered))
            first_chunk = False
    if json_mode:
        sys.stdout.write(f'],"total":{count}}}\n')
    elif not ids and first_chunk:
        typer.echo(format_local_collections({"collections": [], "_complete_enumeration": True}))


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
    restore_policy: Annotated[
        str,
        typer.Option(
            "--restore-policy",
            help="Use allow for full retrieval or never for opportunistic-only materialization",
        ),
    ] = "allow",
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = _sync(
        wait=wait,
        repair=False,
        restore_policy=restore_policy,
        json_mode=json_mode,
    )
    emit(payload if json_mode else _format_sync_result(payload), json_mode=json_mode)


@local_app.command("repair")
def repair(
    wait: Annotated[bool, typer.Option(help="Wait while archival retrieval is pending")] = False,
    restore_policy: Annotated[
        str,
        typer.Option(
            "--restore-policy",
            help="Use allow for full retrieval or never for opportunistic-only repair",
        ),
    ] = "allow",
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = _sync(
        wait=wait,
        repair=True,
        restore_policy=restore_policy,
        json_mode=json_mode,
    )
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
