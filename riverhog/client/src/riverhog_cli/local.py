from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from http_api_contracts import BrowseTokenCodec, BrowseTokenError
from riverhog_api_client.catalog_sync import CatalogReplica
from riverhog_api_client.client import ApiClient, RestorePolicy
from riverhog_api_client.downloads import (
    RetrievalDownload,
    configured_download_concurrency,
    configured_download_window,
    download_retrieval_files,
)
from riverhog_cli_support.output import emit, format_list_ids
from riverhog_protocol.errors import InvalidState, NotFound
from riverhog_protocol.paths import normalize_collection_id, normalize_relpath
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
LOCAL_LIST_TOKEN_LIFETIME_SECONDS = 24 * 60 * 60
LOCAL_LIST_SORT_FIELDS = {
    "bytes": "bytes",
    "collection_id": "collection_id",
    "created_at": "created_at",
    "files": "files",
    "status": "status",
}
LOCAL_AUDIT_SAMPLE_LIMIT = 100
LOCAL_CATALOG_RECONCILE_BATCH = 100
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


def _catalog_database(target: Path) -> Path:
    database = _database(target)
    return database.with_name(database.name + ".catalog")


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
        SELECT c.collection_id, c.created_at,
               CASE
                   WHEN c.remote_deleted = 1 THEN 'remote-deleted'
                   WHEN c.inventory_complete = 0 THEN 'synchronizing'
                   ELSE 'desired'
               END AS status,
               COUNT(f.path) AS files,
               COALESCE(SUM(f.bytes), 0) AS bytes
        FROM desired_collections AS c
        LEFT JOIN desired_files AS f USING (collection_id)
        WHERE c.collection_id = ?
        GROUP BY c.collection_id, c.created_at, c.remote_deleted, c.inventory_complete
        """,
        (collection_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"local collection not found: {collection_id}")
    return {
        "collection_id": int(row["collection_id"]),
        "created_at": str(row["created_at"]),
        "status": str(row["status"]),
        "files": int(row["files"]),
        "bytes": int(row["bytes"]),
    }


def _begin_inventory_refresh(
    db: sqlite3.Connection,
    *,
    collection_id: int,
    inventory_identity: str,
    created_at: str,
) -> None:
    parse_utc_timestamp(created_at)
    db.execute(
        """
        INSERT INTO desired_collections (
            collection_id,
            inventory_identity,
            inventory_cursor,
            inventory_complete,
            created_at,
            remote_deleted
        )
        VALUES (?, ?, NULL, 0, ?, 0)
        ON CONFLICT (collection_id) DO UPDATE SET
            inventory_identity = excluded.inventory_identity,
            inventory_cursor = NULL,
            inventory_complete = 0,
            created_at = excluded.created_at,
            remote_deleted = 0
        """,
        (collection_id, inventory_identity, created_at),
    )
    db.execute("DELETE FROM desired_files WHERE collection_id = ?", (collection_id,))
    db.commit()


def _store_inventory_page(
    db: sqlite3.Connection,
    *,
    collection_id: int,
    inventory_identity: str,
    files: Sequence[Any],
    next_cursor: str | None,
    complete: bool,
) -> None:
    for current in files:
        db.execute(
            """
            INSERT INTO desired_files (collection_id, path, bytes, sha256)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (collection_id, path) DO UPDATE SET
                bytes = excluded.bytes,
                sha256 = excluded.sha256
            """,
            (collection_id, current.path, current.bytes, current.sha256),
        )
    updated = db.execute(
        """
        UPDATE desired_collections
        SET inventory_cursor = ?, inventory_complete = ?
        WHERE collection_id = ? AND inventory_identity = ?
        """,
        (next_cursor, int(complete), collection_id, inventory_identity),
    )
    if updated.rowcount != 1:
        raise InvalidState("local collection inventory authority changed")
    db.commit()


def _refresh_collection(db: sqlite3.Connection, api: ApiClient, collection_id: int) -> None:
    summary = api.get_collection(collection_id)
    if normalize_collection_id(summary["id"]) != collection_id:
        raise InvalidState("Riverhog returned the wrong collection summary")
    inventory_identity = str(summary.get("inventory_identity") or "")
    state = db.execute(
        """
        SELECT inventory_identity, inventory_cursor, inventory_complete
        FROM desired_collections
        WHERE collection_id = ?
        """,
        (collection_id,),
    ).fetchone()
    if state is None or str(state["inventory_identity"]) != inventory_identity:
        _begin_inventory_refresh(
            db,
            collection_id=collection_id,
            inventory_identity=inventory_identity,
            created_at=str(summary["created_at"]),
        )
        cursor: str | None = None
        complete = False
    else:
        cursor = None if state["inventory_cursor"] is None else str(state["inventory_cursor"])
        complete = bool(state["inventory_complete"])
    while not complete:
        inventory = api.get_portable_collection_inventory(
            collection_id,
            cursor=cursor,
            limit=1000,
            inventory_identity=inventory_identity,
        )
        if inventory.authority.inventory_identity != inventory_identity:
            raise InvalidState("Riverhog returned the wrong collection inventory authority")
        _store_inventory_page(
            db,
            collection_id=collection_id,
            inventory_identity=inventory_identity,
            files=inventory.files,
            next_cursor=inventory.next_cursor,
            complete=inventory.complete,
        )
        cursor = inventory.next_cursor
        complete = inventory.complete
    observed = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM desired_files WHERE collection_id = ?",
        (collection_id,),
    ).fetchone()
    if observed is None or (int(observed[0]), int(observed[1])) != (
        int(summary["files"]),
        int(summary["bytes"]),
    ):
        raise InvalidState("local collection inventory is incomplete")


def _output_path(target: Path, collection_id: int, path: str) -> Path:
    output = (target / str(collection_id) / path).resolve()
    if not output.is_relative_to(target):
        raise InvalidState("materialization path escapes RIVERHOG_LOCAL_ROOT")
    return output


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _matches(path: Path, *, byte_count: int, sha256: str) -> bool:
    return path.is_file() and path.stat().st_size == byte_count and _sha256(path) == sha256


def _refresh_catalog(db: sqlite3.Connection, api: ApiClient, target: Path) -> None:
    """Advance one native catalog page and one bounded local reconciliation slice."""

    replica = CatalogReplica(_catalog_database(target))
    status = replica.status()
    if status["phase"] in {"new", "reset_required"}:
        status = replica.start(api)
    else:
        status = replica.step(api, limit=LOCAL_CATALOG_RECONCILE_BATCH)
    if status["usable"]:
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'catalog_reconcile_after'"
        ).fetchone()
        after = int(row["value"]) if row is not None else 0
        collection_ids = [
            int(current["collection_id"])
            for current in db.execute(
                "SELECT collection_id FROM desired_collections "
                "WHERE collection_id > ? ORDER BY collection_id LIMIT ?",
                (after, LOCAL_CATALOG_RECONCILE_BATCH),
            )
        ]
        if not collection_ids and after:
            after = 0
            collection_ids = [
                int(current["collection_id"])
                for current in db.execute(
                    "SELECT collection_id FROM desired_collections ORDER BY collection_id LIMIT ?",
                    (LOCAL_CATALOG_RECONCILE_BATCH,),
                )
            ]
        for collection_id in collection_ids:
            present = replica.get(collection_id) is not None
            db.execute(
                "UPDATE desired_collections SET remote_deleted = ? WHERE collection_id = ?",
                (int(not present), collection_id),
            )
        db.execute(
            """
            INSERT INTO settings (key, value) VALUES ('catalog_reconcile_after', ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            (str(collection_ids[-1] if collection_ids else after),),
        )
    replica.reclaim(limit=LOCAL_CATALOG_RECONCILE_BATCH)
    db.commit()


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
        WHERE c.remote_deleted = 0 AND c.inventory_complete = 1
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


def _retrieval_plan_files(
    api: ApiClient,
    plan: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    plan_id = str(plan["id"])
    plan_etag = str(plan["etag"])
    file_count = int(plan["file_count"])
    files: list[dict[str, Any]] = []
    start_ordinal = 0
    while True:
        page = api.list_retrieval_plan_files(
            plan_id,
            plan_etag=plan_etag,
            start_ordinal=start_ordinal,
            page_size=100,
        )
        current = page.get("files")
        if (
            page.get("plan_id") != plan_id
            or page.get("etag") != plan_etag
            or page.get("start_ordinal") != start_ordinal
            or not isinstance(current, list)
            or any(not isinstance(item, dict) for item in current)
        ):
            raise InvalidState("retrieval plan file page changed its authority")
        files.extend(current)
        if len(files) > file_count:
            raise InvalidState("retrieval plan file page exceeded its declared count")
        complete = page.get("complete")
        if not isinstance(complete, bool):
            raise InvalidState("retrieval plan file page omitted completion state")
        if complete:
            if page.get("next_ordinal") is not None or len(files) != file_count:
                raise InvalidState("retrieval plan file traversal ended inconsistently")
            return tuple(files)
        next_ordinal = page.get("next_ordinal")
        expected_next = start_ordinal + len(current)
        if not current or isinstance(next_ordinal, bool) or next_ordinal != expected_next:
            raise InvalidState("retrieval plan file traversal did not advance exactly")
        start_ordinal = expected_next


def _verify_retrieval_plan_selection(
    files: Sequence[dict[str, Any]],
    expected: Sequence[tuple[int, str]],
) -> None:
    actual = tuple(
        (
            normalize_collection_id(current["collection_id"]),
            normalize_relpath(str(current["path"])),
        )
        for current in files
    )
    if actual != tuple(expected):
        raise InvalidState("retrieval plan changed its requested file selection")


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
        _refresh_catalog(db, api, target)
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
                plan_files = _retrieval_plan_files(api, plan)
                _verify_retrieval_plan_selection(plan_files, batch)
                if policy == "never" and plan.get("requires_restore"):
                    blocked = {
                        (
                            normalize_collection_id(current["collection_id"]),
                            normalize_relpath(str(current["path"])),
                        )
                        for current in plan_files
                        if current.get("requires_restore") is True
                    }
                    unavailable.update(blocked)
                    batch = [current for current in batch if current not in blocked]
                    if not batch:
                        continue
                    plan = api.plan_retrieval(batch, restore_policy=policy)
                    plan_files = _retrieval_plan_files(api, plan)
                    _verify_retrieval_plan_selection(plan_files, batch)
                job = api.create_retrieval_job(
                    str(plan["id"]),
                    plan_etag=str(plan["etag"]),
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
                        for ordinal, current in enumerate(plan_files)
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
    page_size: Annotated[
        int,
        typer.Option("--page-size", min=1, max=LOCAL_LIST_PAGE_SIZE_MAX),
    ] = 25,
    page_token: Annotated[str | None, typer.Option("--page-token")] = None,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "collection_id",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Search collection id or status"),
    ] = None,
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
    normalized_query = (query or "").strip() or None
    selectors = {
        "order": normalized_order,
        "query": normalized_query,
        "sort": sort,
    }
    database = _database(target)
    token_codec = BrowseTokenCodec(
        hashlib.sha256(
            b"riverhog-local-list-token/v1\x00" + str(database).encode("utf-8")
        ).digest(),
        lifetime_seconds=LOCAL_LIST_TOKEN_LIFETIME_SECONDS,
    )
    try:
        position = token_codec.verify(
            page_token,
            operation="local.list_collections",
            principal=str(database),
            selectors=selectors,
        )
    except BrowseTokenError as exc:
        raise typer.BadParameter(str(exc), param_hint="--page-token") from exc
    if position is not None and len(position) != 2:
        raise typer.BadParameter("page token position is invalid", param_hint="--page-token")
    with closing(_connect(target)) as db:
        filters = ""
        params: list[object] = []
        if normalized_query:
            filters = "WHERE CAST(collection_id AS TEXT) LIKE ? OR status LIKE lower(?)"
            pattern = f"%{normalized_query}%"
            params.extend((pattern, pattern))
        base_query = f"""
                WITH local_collections AS (
                SELECT c.collection_id, c.created_at, c.remote_deleted,
                       CASE c.remote_deleted
                           WHEN 1 THEN 'remote-deleted'
                           ELSE 'desired'
                       END AS status,
                       COUNT(f.path) AS files,
                       COALESCE(SUM(f.bytes), 0) AS bytes
                FROM desired_collections AS c
                LEFT JOIN desired_files AS f USING (collection_id)
                GROUP BY c.collection_id, c.created_at, c.remote_deleted
                )
                SELECT * FROM local_collections
                {filters}
                """
        order_column = LOCAL_LIST_SORT_FIELDS[sort]
        order_column = LOCAL_LIST_SORT_FIELDS[sort]
        continuation = ""
        if position is not None:
            sort_value, collection_id = position
            if not isinstance(collection_id, int) or isinstance(collection_id, bool):
                raise typer.BadParameter(
                    "page token position is invalid", param_hint="--page-token"
                )
            comparison = ">" if normalized_order == "asc" else "<"
            continuation = (
                f"WHERE ({order_column} {comparison} ? "
                f"OR ({order_column} = ? AND collection_id > ?))"
            )
            params.extend((sort_value, sort_value, collection_id))
        rows = db.execute(
            f"""
            SELECT * FROM ({base_query})
            {continuation}
            ORDER BY {order_column} {normalized_order.upper()}, collection_id ASC
            LIMIT ?
            """,
            (*params, page_size + 1),
        ).fetchall()
        has_more = len(rows) > page_size
        page_rows = rows[:page_size]
        collections = [_local_collection_list_item(row) for row in page_rows]
    next_page_token = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_page_token = token_codec.issue(
            operation="local.list_collections",
            principal=str(database),
            selectors=selectors,
            position=(last[order_column], int(last["collection_id"])),
        )
    payload = {
        "page_size": page_size,
        "next_page_token": next_page_token,
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
        "status": str(row["status"]),
        "files": int(row["files"]),
        "bytes": int(row["bytes"]),
    }


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
        db.commit()
    payload = {
        "status": "evicted",
        "collection_id": normalized,
        "retrievals_canceled": canceled,
    }
    emit(payload if json_mode else f"evicted local collection: {normalized}", json_mode=json_mode)
