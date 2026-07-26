from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import chain
from pathlib import Path
from queue import Queue
from typing import Annotated, Any, Literal, TypedDict, cast

import httpx
import typer
from cli_support.application_keys import (
    format_app_key_created,
    format_app_key_revoked,
    format_app_key_rotated,
    format_app_keys,
    format_apps,
)
from cli_support.output import emit, format_list_ids
from riverhog_age import plaintext_bytes_for_ciphertext_offset
from riverhog_api_client.client import ApiClient
from riverhog_api_client.ingress import (
    DEFAULT_INGRESS_PART_BYTES,
    iter_ingress_upload_parts,
)
from riverhog_protocol.errors import Conflict, NotFound, RiverhogError, ServiceUnavailable
from riverhog_protocol.paths import (
    PathNormalizationError,
    normalize_tag,
)
from time_formats import parse_duration, utc_timestamp_now

from riverhog_cli.local import local_app
from riverhog_cli.output import (
    format_app_access,
    format_app_access_set,
    format_archive_copy_job,
    format_archive_copy_retirement_plan,
    format_archive_copy_retirement_result,
    format_collection_deletion_plan,
    format_collection_deletion_result,
    format_collection_summary,
    format_collection_tags,
    format_collection_upload,
    format_collection_upload_plan,
    format_collections,
    format_download_quota,
    format_download_quotas,
    format_file_selectors,
    format_find,
    format_tag,
    format_tags,
)
from riverhog_cli.upload_progress import make_collection_upload_progress

app = typer.Typer(help="Riverhog collection archive CLI.")
collection_app = typer.Typer(help="Collection catalog and upload operations.")
collection_tag_app = typer.Typer(help="Collection tag assignments.")
archive_app = typer.Typer(help="Archive-store operations.")
application_app = typer.Typer(help="Application access.")
app_key_app = typer.Typer(help="Application key management.")
app_key_access_app = typer.Typer(help="Per-key permission and resource access.")
app_key_quota_app = typer.Typer(help="Per-key remote-download quotas.")
tag_app = typer.Typer(help="Collection tag catalog.")
app_key_app.add_typer(app_key_access_app, name="access")
app_key_app.add_typer(app_key_quota_app, name="quota")
application_app.add_typer(app_key_app, name="key")
app.add_typer(collection_app, name="collection")
collection_app.add_typer(collection_tag_app, name="tag")
app.add_typer(archive_app, name="archive")
app.add_typer(application_app, name="app")
app.add_typer(tag_app, name="tag")
app.add_typer(local_app, name="local")

HASH_CHUNK_BYTES = 8 * 1024 * 1024
UPLOAD_CHUNK_BYTES = DEFAULT_INGRESS_PART_BYTES
UPLOAD_FILE_CONCURRENCY = 8
UPLOAD_FILE_CONCURRENCY_MAX = 64
UPLOAD_FILE_WINDOW_MAX = 256
UPLOAD_FILE_LOG_BYTES = 1 * 1024 * 1024
UPLOAD_PROGRESS_INTERVAL_SECONDS = 5.0
UPLOAD_FINALIZE_POLL_SECONDS = 5.0
UPLOAD_FINALIZE_STATUS_INTERVAL_SECONDS = 30.0
TRANSIENT_UPLOAD_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
UPLOAD_RESUME_RETRY_INITIAL_DELAY_SECONDS = 1.0
UPLOAD_RESUME_RETRY_MAX_DELAY_SECONDS = 10.0
UPLOAD_RESUME_RETRY_LOG_INTERVAL_SECONDS = 30.0
UPLOAD_STORAGE_ROLLBACK_LIMIT = 3
UPLOAD_LOG_LOCK = threading.Lock()
UploadWaitMode = Literal["staged", "finalized"]
UploadCompletionState = Literal["staged", "finalized", "failed", "timeout"]
_API_CLIENT: ApiClient | None = None


class CollectionManifestEntry(TypedDict):
    path: str
    bytes: int
    sha256: str


class CollectionUploadFilePayload(CollectionManifestEntry, total=False):
    upload_state: str
    uploaded_bytes: int
    upload_state_expires_at: str | None


def client() -> ApiClient:
    global _API_CLIENT
    if _API_CLIENT is None:
        _API_CLIENT = ApiClient()
    return _API_CLIENT


def _close_client() -> None:
    global _API_CLIENT
    if _API_CLIENT is not None:
        _API_CLIENT.close()
        _API_CLIENT = None


@app.callback()
def _root(ctx: typer.Context) -> None:
    ctx.call_on_close(_close_client)


_APP_SORT_FIELDS = {"name", "keys", "active_keys", "last_used_at"}
_APP_KEY_SORT_FIELDS = {"id", "created_at", "expires_at", "last_used_at"}
_APP_KEY_ACCESS_SORT_FIELDS = {"permission", "resource", "created_at"}
_TAG_SORT_FIELDS = {"id", "created_at", "collections"}
_APP_KEY_QUOTA_SORT_FIELDS = {
    "app",
    "key_id",
    "monthly_bytes",
    "accounted_bytes",
    "reserved_bytes",
    "remaining_bytes",
}
_BYTE_SIZE_RE = re.compile(r"^(?P<count>\d+)(?P<unit>b|kib|mib|gib|tib)?$", re.IGNORECASE)
_BYTE_SIZE_FACTORS = {
    "": 1,
    "b": 1,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


def _list_order(sort: str, order: str, *, fields: set[str]) -> str:
    if sort not in fields:
        raise typer.BadParameter(
            f"sort must be one of {', '.join(sorted(fields))}",
            param_hint="--sort",
        )
    normalized_order = order.casefold()
    if normalized_order not in {"asc", "desc"}:
        raise typer.BadParameter("order must be asc or desc", param_hint="--order")
    return normalized_order


def _monthly_quota_bytes(value: str) -> int | None:
    candidate = value.strip().casefold()
    if candidate == "unlimited":
        return None
    match = _BYTE_SIZE_RE.fullmatch(candidate)
    if match is None:
        raise typer.BadParameter(
            "quota must be unlimited or a byte size such as 500GiB",
            param_hint="LIMIT",
        )
    return int(match.group("count")) * _BYTE_SIZE_FACTORS[match.group("unit") or ""]


def _access(value: str) -> dict[str, str]:
    permission, separator, resource = value.strip().partition("=")
    if not permission:
        raise typer.BadParameter("access requires a permission", param_hint="--allow")
    if separator and not resource:
        raise typer.BadParameter("access resource must not be empty", param_hint="--allow")
    return {"permission": permission, "resource": resource if separator else "*"}


@application_app.command("list")
def app_list_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "name",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Substring match over app names"),
    ] = None,
    active: Annotated[
        bool | None,
        typer.Option("--active/--inactive", help="Filter by usable key availability"),
    ] = None,
    all_items: Annotated[bool, typer.Option("--all", help="Return every matching app")] = False,
    ids: Annotated[bool, typer.Option("--ids", help="Emit one app name per line")] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List applications with key summaries."""

    if ids and json_mode:
        raise typer.BadParameter("--ids and --json cannot be used together")
    normalized_order = _list_order(sort, order, fields=_APP_SORT_FIELDS)
    payload = client().list_apps(
        page=page,
        per_page=per_page,
        q=query,
        sort=sort,
        order=normalized_order,
        active=active,
        all_items=all_items,
    )
    if ids:
        emit(format_list_ids(payload, "apps", id_key="name"), json_mode=False)
        return
    emit(payload if json_mode else format_apps(payload), json_mode=json_mode)


@tag_app.command("create")
def tag_create_cmd(
    tag: Annotated[str, typer.Argument(help="Canonical tag id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Create a tag and grant this key collection-creation access to it."""

    payload = client().create_tag(tag)
    emit(payload if json_mode else format_tag(payload), json_mode=json_mode)


@tag_app.command("show")
def tag_show_cmd(
    tag: Annotated[str, typer.Argument(help="Tag id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show one catalog-visible tag."""

    payload = client().get_tag(tag)
    emit(payload if json_mode else format_tag(payload), json_mode=json_mode)


@tag_app.command("list")
def tag_list_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "id",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Substring match over tag ids"),
    ] = None,
    all_items: Annotated[bool, typer.Option("--all", help="Return every matching tag")] = False,
    ids: Annotated[bool, typer.Option("--ids", help="Emit one tag id per line")] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List catalog-visible tags."""

    if ids and json_mode:
        raise typer.BadParameter("--ids and --json cannot be used together")
    normalized_order = _list_order(sort, order, fields=_TAG_SORT_FIELDS)
    payload = client().list_tags(
        page=page,
        per_page=per_page,
        q=query,
        sort=sort,
        order=normalized_order,
        all_items=all_items,
    )
    if ids:
        emit(format_list_ids(payload, "tags"), json_mode=False)
        return
    emit(payload if json_mode else format_tags(payload), json_mode=json_mode)


@collection_tag_app.command("list")
def collection_tag_list_cmd(
    collection_id: Annotated[int, typer.Argument(help="Collection id")],
    ids: Annotated[bool, typer.Option("--ids", help="Emit one tag id per line")] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List the tags assigned to one collection."""

    if ids and json_mode:
        raise typer.BadParameter("--ids and --json cannot be used together")
    payload = client().get_collection_tags(collection_id)
    if ids:
        emit("\n".join(str(tag) for tag in payload.get("tags", [])), json_mode=False)
        return
    emit(payload if json_mode else format_collection_tags(payload), json_mode=json_mode)


@collection_tag_app.command("replace")
def collection_tag_replace_cmd(
    collection_id: Annotated[int, typer.Argument(help="Collection id")],
    tags: Annotated[
        list[str] | None,
        typer.Option("--tag", help="Replacement tag; repeat to assign more than one"),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Replace all tags assigned to one collection."""

    payload = client().replace_collection_tags(collection_id, tags or [])
    emit(payload if json_mode else format_collection_tags(payload), json_mode=json_mode)


@app_key_app.command("create")
def app_key_create_cmd(
    app_name: Annotated[str, typer.Argument(help="Application name")],
    allow: Annotated[
        list[str],
        typer.Option(
            "--allow",
            help="PERMISSION or PERMISSION=RESOURCE; repeat for more than one.",
        ),
    ],
    expires_in: Annotated[
        str | None,
        typer.Option("--expires-in", help="Optional key lifetime such as 30d"),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Create a key and show its token once."""

    expires_in_seconds: int | None = None
    if expires_in is not None:
        try:
            expires_in_seconds = int(parse_duration(expires_in).total_seconds())
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--expires-in") from exc
        if expires_in_seconds < 1:
            raise typer.BadParameter(
                "expiry must be at least one second",
                param_hint="--expires-in",
            )
    payload = client().create_app_key(
        app_name,
        access=[_access(current) for current in allow],
        expires_in_seconds=expires_in_seconds,
    )
    emit(payload if json_mode else format_app_key_created(payload), json_mode=json_mode)


@app_key_app.command("list")
def app_key_list_cmd(
    app_name: Annotated[str, typer.Argument(help="Application name")],
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "created_at",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "desc",
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Substring match over key ids"),
    ] = None,
    active: Annotated[
        bool | None,
        typer.Option("--active/--inactive", help="Filter by key usability"),
    ] = None,
    all_items: Annotated[bool, typer.Option("--all", help="Return every matching key")] = False,
    ids: Annotated[bool, typer.Option("--ids", help="Emit one key id per line")] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List keys without exposing their tokens."""

    if ids and json_mode:
        raise typer.BadParameter("--ids and --json cannot be used together")
    normalized_order = _list_order(sort, order, fields=_APP_KEY_SORT_FIELDS)
    payload = client().list_app_keys(
        app_name,
        page=page,
        per_page=per_page,
        q=query,
        sort=sort,
        order=normalized_order,
        active=active,
        all_items=all_items,
    )
    if ids:
        emit(format_list_ids(payload, "keys"), json_mode=False)
        return
    emit(payload if json_mode else format_app_keys(payload), json_mode=json_mode)


@app_key_app.command("revoke")
def app_key_revoke_cmd(
    app_name: Annotated[str, typer.Argument(help="External application name")],
    key_id: Annotated[str, typer.Argument(help="Key id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Immediately revoke one application key."""

    payload = client().revoke_app_key(app_name, key_id)
    emit(payload if json_mode else format_app_key_revoked(payload), json_mode=json_mode)


@app_key_app.command("rotate")
def app_key_rotate_cmd(
    app_name: Annotated[str, typer.Argument(help="Application name")],
    key_id: Annotated[str, typer.Argument(help="Stable key id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Replace one key token while preserving its identity and policy."""

    payload = client().rotate_app_key(app_name, key_id)
    emit(payload if json_mode else format_app_key_rotated(payload), json_mode=json_mode)


@app_key_access_app.command("list")
def app_key_access_list_cmd(
    app_name: Annotated[str, typer.Argument(help="Application name")],
    key_id: Annotated[str, typer.Argument(help="Key id")],
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "permission",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Substring match over permissions and resources"),
    ] = None,
    all_items: Annotated[bool, typer.Option("--all", help="Return every matching grant")] = False,
    ids: Annotated[bool, typer.Option("--ids", help="Emit one access binding per line")] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List a key's exact permission and resource bindings."""

    if ids and json_mode:
        raise typer.BadParameter("--ids and --json cannot be used together")
    normalized_order = _list_order(sort, order, fields=_APP_KEY_ACCESS_SORT_FIELDS)
    payload = client().list_app_key_access(
        app_name,
        key_id,
        page=page,
        per_page=per_page,
        q=query,
        sort=sort,
        order=normalized_order,
        all_items=all_items,
    )
    if ids:
        emit(format_list_ids(payload, "access"), json_mode=False)
        return
    emit(payload if json_mode else format_app_access(payload), json_mode=json_mode)


@app_key_access_app.command("set")
def app_key_access_set_cmd(
    app_name: Annotated[str, typer.Argument(help="Application name")],
    key_id: Annotated[str, typer.Argument(help="Key id")],
    allow: Annotated[
        list[str],
        typer.Option(
            "--allow",
            help="Replacement PERMISSION or PERMISSION=RESOURCE; repeat as needed.",
        ),
    ],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Replace every permission and resource binding for a key."""

    payload = client().replace_app_key_access(
        app_name,
        key_id,
        access=[_access(current) for current in allow],
    )
    emit(payload if json_mode else format_app_access_set(payload), json_mode=json_mode)


@app_key_quota_app.command("show")
def app_key_quota_show_cmd(
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show the current key's remote-download quota and usage."""

    payload = client().get_download_quota()
    emit(payload if json_mode else format_download_quota(payload), json_mode=json_mode)


@app_key_quota_app.command("set")
def app_key_quota_set_cmd(
    app_name: Annotated[str, typer.Argument(help="Application name")],
    key_id: Annotated[str, typer.Argument(help="Key id")],
    limit: Annotated[
        str,
        typer.Argument(help="Monthly limit such as 500GiB, 0 to block, or unlimited"),
    ],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Assign a monthly remote-download quota without resetting usage."""

    payload = client().set_app_key_download_quota(
        app_name,
        key_id,
        monthly_bytes=_monthly_quota_bytes(limit),
    )
    emit(payload if json_mode else format_download_quota(payload), json_mode=json_mode)


@app_key_quota_app.command("list")
def app_key_quota_list_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "app",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Substring match over app names and key ids"),
    ] = None,
    app_name: Annotated[
        str | None,
        typer.Option("--app", help="Filter by application name"),
    ] = None,
    active: Annotated[
        bool | None,
        typer.Option("--active/--inactive", help="Filter by key usability"),
    ] = None,
    all_items: Annotated[bool, typer.Option("--all", help="Return every matching quota")] = False,
    ids: Annotated[bool, typer.Option("--ids", help="Emit one key id per line")] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List per-key quotas and current-month accounting."""

    if ids and json_mode:
        raise typer.BadParameter("--ids and --json cannot be used together")
    normalized_order = _list_order(sort, order, fields=_APP_KEY_QUOTA_SORT_FIELDS)
    payload = client().list_download_quotas(
        page=page,
        per_page=per_page,
        q=query,
        sort=sort,
        order=normalized_order,
        app=app_name,
        active=active,
        all_items=all_items,
    )
    if ids:
        emit(format_list_ids(payload, "quotas"), json_mode=False)
        return
    emit(payload if json_mode else format_download_quotas(payload), json_mode=json_mode)


def _response_upload_files(payload: dict[str, Any]) -> list[CollectionUploadFilePayload]:
    files = payload.get("files")
    if not isinstance(files, list):
        return []
    return cast(list[CollectionUploadFilePayload], files)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iter_file_chunks(
    path: Path,
    *,
    offset: int = 0,
    limit: int | None = None,
    chunk_size: int = UPLOAD_CHUNK_BYTES,
) -> Iterator[bytes]:
    remaining = limit
    with path.open("rb") as handle:
        if offset:
            handle.seek(offset)
        while remaining is None or remaining > 0:
            read_size = chunk_size if remaining is None else min(chunk_size, remaining)
            chunk = handle.read(read_size)
            if not chunk:
                return
            yield chunk
            if remaining is not None:
                remaining -= len(chunk)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for chunk in _iter_file_chunks(path, chunk_size=HASH_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def _upload_chunk_bytes() -> int:
    raw_value = os.getenv("RIVERHOG_UPLOAD_CHUNK_BYTES")
    if raw_value is None or raw_value.strip() == "":
        return UPLOAD_CHUNK_BYTES
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise typer.BadParameter("RIVERHOG_UPLOAD_CHUNK_BYTES must be a positive integer") from exc
    if value <= 0:
        raise typer.BadParameter("RIVERHOG_UPLOAD_CHUNK_BYTES must be a positive integer")
    return value


def _upload_file_concurrency() -> int:
    raw_value = os.getenv("RIVERHOG_UPLOAD_FILE_CONCURRENCY")
    if raw_value is None or raw_value.strip() == "":
        return UPLOAD_FILE_CONCURRENCY
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise typer.BadParameter(
            "RIVERHOG_UPLOAD_FILE_CONCURRENCY must be a positive integer"
        ) from exc
    if value <= 0 or value > UPLOAD_FILE_CONCURRENCY_MAX:
        raise typer.BadParameter(
            f"RIVERHOG_UPLOAD_FILE_CONCURRENCY must be between 1 and {UPLOAD_FILE_CONCURRENCY_MAX}"
        )
    return value


def _upload_file_window(file_concurrency: int) -> int:
    raw_value = os.getenv("RIVERHOG_UPLOAD_FILE_WINDOW")
    if raw_value is None or raw_value.strip() == "":
        return min(file_concurrency * 2, UPLOAD_FILE_WINDOW_MAX)
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise typer.BadParameter("RIVERHOG_UPLOAD_FILE_WINDOW must be an integer") from exc
    if value < file_concurrency or value > UPLOAD_FILE_WINDOW_MAX:
        raise typer.BadParameter(
            "RIVERHOG_UPLOAD_FILE_WINDOW must be between the configured file concurrency "
            f"and {UPLOAD_FILE_WINDOW_MAX}"
        )
    return value


def _new_upload_worker_client(api: ApiClient) -> ApiClient:
    worker = ApiClient(base_url=api.base_url, token=api.token)
    worker.host_header = api.host_header
    worker.verify_tls = api.verify_tls
    worker.http2 = api.http2
    worker.upload_base_url = api.upload_base_url
    worker.upload_http2 = api.upload_http2
    return worker


def _upload_file_log_bytes() -> int:
    raw_value = os.getenv("RIVERHOG_UPLOAD_FILE_LOG_BYTES")
    if raw_value is None or raw_value.strip() == "":
        return UPLOAD_FILE_LOG_BYTES
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise typer.BadParameter(
            "RIVERHOG_UPLOAD_FILE_LOG_BYTES must be a non-negative integer"
        ) from exc
    if value < 0:
        raise typer.BadParameter("RIVERHOG_UPLOAD_FILE_LOG_BYTES must be a non-negative integer")
    return value


def _upload_finalize_poll_seconds() -> float:
    raw_value = os.getenv("RIVERHOG_UPLOAD_FINALIZE_POLL_SECONDS")
    if raw_value is None or raw_value.strip() == "":
        return UPLOAD_FINALIZE_POLL_SECONDS
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise typer.BadParameter(
            "RIVERHOG_UPLOAD_FINALIZE_POLL_SECONDS must be a positive number"
        ) from exc
    if value <= 0:
        raise typer.BadParameter("RIVERHOG_UPLOAD_FINALIZE_POLL_SECONDS must be a positive number")
    return value


def _upload_finalize_timeout_seconds() -> float | None:
    raw_value = os.getenv("RIVERHOG_UPLOAD_FINALIZE_TIMEOUT_SECONDS")
    if raw_value is None or raw_value.strip() == "":
        return None
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise typer.BadParameter(
            "RIVERHOG_UPLOAD_FINALIZE_TIMEOUT_SECONDS must be a non-negative number"
        ) from exc
    if value < 0:
        raise typer.BadParameter(
            "RIVERHOG_UPLOAD_FINALIZE_TIMEOUT_SECONDS must be a non-negative number"
        )
    if value == 0:
        return None
    return value


def _default_upload_wait_mode() -> str:
    return os.getenv("RIVERHOG_UPLOAD_WAIT", "finalized").strip().lower() or "finalized"


def _normalize_upload_wait_mode(value: str) -> UploadWaitMode:
    normalized = value.strip().lower()
    if normalized not in {"staged", "finalized"}:
        raise typer.BadParameter("upload wait mode must be 'staged' or 'finalized'")
    return "staged" if normalized == "staged" else "finalized"


def _format_bytes(value: int) -> str:
    if value < 1000:
        return f"{value} B"

    scaled = float(value)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        scaled /= 1000.0
        if scaled < 1000.0 or unit == "PB":
            return f"{scaled:.1f} {unit}"
    raise AssertionError("unreachable")


def _log_upload(message: str) -> None:
    with UPLOAD_LOG_LOCK:
        typer.echo(message, err=True)


def _report_upload_status(status: Callable[[str], None] | None, message: str) -> None:
    if status is None:
        _log_upload(message)
        return
    status(message)


def _is_transient_upload_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in TRANSIENT_UPLOAD_STATUS_CODES
    if isinstance(exc, ServiceUnavailable):
        return True
    return False


def _upload_error_description(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, (Conflict, ServiceUnavailable)):
        return exc.message
    return f"{type(exc).__name__}: {exc}"


def _retry_transient_upload_operation(
    description: str,
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    delay = UPLOAD_RESUME_RETRY_INITIAL_DELAY_SECONDS
    last_log_at = 0.0
    attempt = 0
    while True:
        try:
            return operation()
        except (httpx.TransportError, httpx.HTTPStatusError, ServiceUnavailable) as exc:
            if not _is_transient_upload_error(exc):
                raise
            attempt += 1
            now = time.monotonic()
            if attempt == 1 or now - last_log_at >= UPLOAD_RESUME_RETRY_LOG_INTERVAL_SECONDS:
                _log_upload(
                    f"{description} failed ({_upload_error_description(exc)}); "
                    f"retrying in {delay:.1f}s"
                )
                last_log_at = now
            time.sleep(delay)
            delay = min(delay * 2, UPLOAD_RESUME_RETRY_MAX_DELAY_SECONDS)


def _create_or_resume_collection_upload(
    api: ApiClient,
    idempotency_key: str,
    tags: list[str],
    manifest: list[CollectionManifestEntry],
    *,
    ingest_source: str | None,
    archive_store: str | None = None,
) -> dict[str, Any]:
    return _retry_transient_upload_operation(
        "Upload session create/resume",
        lambda: api.create_or_resume_collection_upload(
            idempotency_key,
            tags,
            manifest,
            ingest_source=ingest_source,
            archive_store=archive_store,
        ),
    )


def _create_or_resume_collection_upload_session(
    api: ApiClient,
    idempotency_key: str,
    tags: list[str],
    *,
    ingest_source: str | None,
    archive_store: str | None = None,
) -> dict[str, Any]:
    return _retry_transient_upload_operation(
        "Upload session open/resume",
        lambda: api.create_or_resume_collection_upload_session(
            idempotency_key,
            tags,
            ingest_source=ingest_source,
            archive_store=archive_store,
        ),
    )


def _register_collection_upload_session_file(
    api: ApiClient,
    collection_id: int,
    file_payload: CollectionManifestEntry,
) -> dict[str, Any]:
    return _retry_transient_upload_operation(
        f"Upload session register file {file_payload['path']}",
        lambda: api.register_collection_upload_session_file(collection_id, file_payload),
    )


def _complete_collection_upload_session(
    api: ApiClient,
    collection_id: int,
) -> dict[str, Any]:
    return _retry_transient_upload_operation(
        "Upload session complete",
        lambda: api.complete_collection_upload_session(collection_id),
    )


def _create_or_resume_collection_file_upload(
    api: ApiClient,
    collection_id: int,
    path_value: str,
) -> dict[str, Any]:
    return _retry_transient_upload_operation(
        f"Upload resume check for {path_value}",
        lambda: api.create_or_resume_collection_file_upload(collection_id, path_value),
    )


def _local_collection_manifest(root: Path) -> list[CollectionManifestEntry]:
    files: list[CollectionManifestEntry] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": stat.st_size,
                "sha256": _file_sha256(path),
            }
        )
    if not files:
        raise typer.BadParameter("collection source must contain at least one file")
    return files


def _collection_upload_dry_run_plan(
    *,
    idempotency_key: str,
    tags: list[str],
    root: Path,
    manifest: list[CollectionManifestEntry],
    wait_mode: UploadWaitMode,
    session_mode: bool,
    archive_store: str | None = None,
) -> dict[str, object]:
    try:
        normalized_tags = sorted({normalize_tag(tag) for tag in tags})
    except PathNormalizationError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if len(normalized_tags) != len(tags):
        raise typer.BadParameter("collection tags must not contain duplicates")
    return {
        "dry_run": True,
        "status": "would_upload",
        "idempotency_key": idempotency_key,
        "tags": normalized_tags,
        "collection_id": None,
        "root": str(root),
        "ingest_source": str(root),
        "files_total": len(manifest),
        "bytes_total": sum(item["bytes"] for item in manifest),
        "wait_mode": wait_mode,
        "session": session_mode,
        "archive_store": archive_store,
        "server_validation": "not_run",
        "created_at": utc_timestamp_now(),
        "files_preview": manifest[:5],
    }


def _iter_local_collection_paths(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _upload_collection_file(
    api: ApiClient,
    collection_id: int,
    source_path: Path,
    file_payload: Mapping[str, object],
    *,
    progress: Callable[[int], None] | None = None,
    resumed: Callable[[int], None] | None = None,
) -> None:
    path_value = str(file_payload["path"])
    length_value = file_payload["bytes"]
    if not isinstance(length_value, int):
        raise RuntimeError(f"upload length for {path_value} is not an integer")
    plaintext_length = length_value
    session = _create_or_resume_collection_file_upload(api, collection_id, path_value)
    offset = int(session["offset"])
    length = int(session["length"])
    encryption = session.get("encryption")
    if not isinstance(encryption, Mapping):
        raise RuntimeError(f"upload encryption descriptor is missing for {path_value}")
    if int(encryption.get("plaintext_bytes", -1)) != plaintext_length:
        raise RuntimeError(f"upload plaintext length changed for {path_value}")
    if int(encryption.get("ciphertext_bytes", -1)) != length:
        raise RuntimeError(f"upload ciphertext length changed for {path_value}")
    if offset > length:
        raise RuntimeError(
            f"upload offset for {path_value} is {offset}, past expected length {length}"
        )
    encryption_state = encryption.get("state")
    if not isinstance(encryption_state, Mapping):
        raise RuntimeError(f"upload encryption state is missing for {path_value}")
    if resumed is not None and offset:
        resumed(
            plaintext_bytes_for_ciphertext_offset(
                state=encryption_state,
                plaintext_bytes=plaintext_length,
                ciphertext_bytes=length,
                ciphertext_offset=offset,
            )
        )
    log_file = plaintext_length >= _upload_file_log_bytes()
    if offset >= length:
        if log_file:
            _log_upload(f"Already uploaded {path_value} ({_format_bytes(plaintext_length)})")
        return

    if offset:
        _log_upload(f"Resuming encrypted upload {path_value} at {_format_bytes(offset)}")
    elif log_file:
        _log_upload(f"Uploading {path_value} ({_format_bytes(plaintext_length)})")

    def completed_plaintext(ciphertext_offset: int) -> int:
        return plaintext_bytes_for_ciphertext_offset(
            state=encryption_state,
            plaintext_bytes=plaintext_length,
            ciphertext_bytes=length,
            ciphertext_offset=ciphertext_offset,
        )

    highest_confirmed_offset = offset
    storage_rollbacks = 0
    while offset < length:
        restart_at_recovered_offset = False
        for part in iter_ingress_upload_parts(
            source_path,
            encryption,
            ciphertext_offset=offset,
            target_part_bytes=_upload_chunk_bytes(),
        ):
            chunk = part.ciphertext
            retry_delay = UPLOAD_RESUME_RETRY_INITIAL_DELAY_SECONDS
            last_retry_log_at = 0.0
            retry_attempt = 0
            while offset == part.ciphertext_offset:
                try:
                    upload_result = api.append_upload_chunk(
                        str(session["upload_url"]),
                        offset=offset,
                        checksum_algorithm=str(session["checksum_algorithm"]),
                        content=chunk,
                    )
                except (
                    httpx.TransportError,
                    httpx.HTTPStatusError,
                    Conflict,
                    ServiceUnavailable,
                ) as exc:
                    if not isinstance(exc, Conflict) and not _is_transient_upload_error(exc):
                        raise
                    session = _create_or_resume_collection_file_upload(
                        api,
                        collection_id,
                        path_value,
                    )
                    recovered_offset = int(session["offset"])
                    if recovered_offset == offset:
                        if isinstance(exc, Conflict):
                            raise RuntimeError(
                                f"server rejected upload chunk for {path_value} at "
                                f"{_format_bytes(offset)} without advancing the offset"
                            ) from exc
                        retry_attempt += 1
                        now = time.monotonic()
                        if (
                            retry_attempt == 1
                            or now - last_retry_log_at >= UPLOAD_RESUME_RETRY_LOG_INTERVAL_SECONDS
                        ):
                            _log_upload(
                                f"Upload interrupted for {path_value} at "
                                f"{_format_bytes(offset)}; {_upload_error_description(exc)}; "
                                f"server offset unchanged; retrying in {retry_delay:.1f}s"
                            )
                            last_retry_log_at = now
                        time.sleep(retry_delay)
                        retry_delay = min(
                            retry_delay * 2,
                            UPLOAD_RESUME_RETRY_MAX_DELAY_SECONDS,
                        )
                        continue
                    if recovered_offset > length:
                        raise RuntimeError(
                            f"server upload offset for {path_value} is {recovered_offset}, "
                            f"past expected length {length}"
                        ) from exc
                    completed_before = completed_plaintext(offset)
                    completed_after = completed_plaintext(recovered_offset)
                    if progress is not None and completed_after != completed_before:
                        progress(completed_after - completed_before)
                    chunk_end = offset + len(chunk)
                    previous_offset = offset
                    offset = recovered_offset
                    if recovered_offset == chunk_end:
                        _log_upload(
                            f"Server accepted chunk for {path_value} before the response "
                            f"was lost; continuing at {_format_bytes(recovered_offset)}"
                        )
                    elif recovered_offset < previous_offset:
                        storage_rollbacks += 1
                        if storage_rollbacks >= UPLOAD_STORAGE_ROLLBACK_LIMIT:
                            raise RuntimeError(
                                f"upload storage repeatedly rolled {path_value} back to "
                                f"{_format_bytes(recovered_offset)} after accepting later "
                                "offsets; cancel this upload session and retry"
                            ) from exc
                        _log_upload(
                            f"Upload storage rolled {path_value} back to its authoritative "
                            f"offset {_format_bytes(recovered_offset)}; resending from there"
                        )
                        restart_at_recovered_offset = True
                    else:
                        _log_upload(
                            f"Server retained part of the interrupted upload for "
                            f"{path_value}; continuing at {_format_bytes(recovered_offset)}"
                        )
                        restart_at_recovered_offset = True
                    break

                next_offset = int(upload_result["offset"])
                if next_offset != offset + len(chunk):
                    raise RuntimeError(f"upload offset advanced unexpectedly for {path_value}")
                completed_before = completed_plaintext(offset)
                completed_after = completed_plaintext(next_offset)
                if progress is not None and completed_after > completed_before:
                    progress(completed_after - completed_before)
                offset = next_offset
                if next_offset > highest_confirmed_offset:
                    highest_confirmed_offset = next_offset
                    storage_rollbacks = 0
            if restart_at_recovered_offset:
                break
        if restart_at_recovered_offset:
            continue
        break
    if offset != length:
        raise RuntimeError(f"upload for {path_value} stopped at {offset} of {length} bytes")
    if log_file:
        _log_upload(f"Uploaded {path_value} ({_format_bytes(plaintext_length)})")


def _upload_collection_files(
    api: ApiClient,
    collection_id: int,
    resolved_root: Path,
    upload_files: list[CollectionUploadFilePayload],
    *,
    progress: Callable[[int], None],
    file_complete: Callable[[], None] | None = None,
    file_concurrency: int,
    api_factory: Callable[[], ApiClient] | None = None,
) -> None:
    pending_files = [
        file_payload for file_payload in upload_files if file_payload["upload_state"] != "uploaded"
    ]
    if file_concurrency <= 1:
        for file_payload in pending_files:
            _upload_collection_file(
                api,
                collection_id,
                resolved_root / str(file_payload["path"]),
                file_payload,
                progress=progress,
            )
            if file_complete is not None:
                file_complete()
        return

    _log_upload(f"Uploading up to {file_concurrency} files concurrently")
    next_file_lock = threading.Lock()
    stop_event = threading.Event()
    pending_iter = iter(pending_files)
    worker_factory = api_factory or (lambda: _new_upload_worker_client(api))

    def upload_worker() -> None:
        worker_api = worker_factory()
        try:
            while not stop_event.is_set():
                with next_file_lock:
                    if stop_event.is_set():
                        return
                    try:
                        file_payload = next(pending_iter)
                    except StopIteration:
                        return
                _upload_collection_file(
                    worker_api,
                    collection_id,
                    resolved_root / str(file_payload["path"]),
                    file_payload,
                    progress=progress,
                )
                if file_complete is not None:
                    file_complete()
        finally:
            worker_api.close()

    with ThreadPoolExecutor(max_workers=file_concurrency) as executor:
        futures = [
            executor.submit(upload_worker) for _ in range(min(file_concurrency, len(pending_files)))
        ]
        try:
            for future in as_completed(futures):
                future.result()
        except BaseException:
            stop_event.set()
            for future in futures:
                future.cancel()
            raise


def _finalized_collection_upload_payload(
    collection_id: int,
    manifest: list[CollectionManifestEntry] | None,
    collection: dict[str, object],
) -> dict[str, object]:
    if manifest is None:
        bytes_value = collection.get("bytes")
        files_value = collection.get("files")
        bytes_total = int(bytes_value) if isinstance(bytes_value, (str, int, float)) else 0
        files_total = int(files_value) if isinstance(files_value, (str, int, float)) else 0
        files: list[dict[str, object]] = []
    else:
        bytes_total = sum(item["bytes"] for item in manifest)
        files = [
            {
                "path": item["path"],
                "bytes": item["bytes"],
                "sha256": item["sha256"],
                "upload_state": "uploaded",
                "uploaded_bytes": item["bytes"],
                "upload_state_expires_at": None,
            }
            for item in manifest
        ]
        files_total = len(files)
    archive_copies = collection.get("archive_copies")
    archive = archive_copies[0] if isinstance(archive_copies, list) and archive_copies else None
    archive_stored_bytes = 0
    if isinstance(archive, dict):
        archive_stored_bytes = int(archive.get("stored_bytes") or 0)
    return {
        "collection_id": collection_id,
        "ingest_source": collection.get("ingest_source"),
        "archive_store": archive.get("store") if isinstance(archive, dict) else None,
        "state": "finalized",
        "files_total": files_total,
        "files_pending": 0,
        "files_partial": 0,
        "files_uploaded": files_total,
        "bytes_total": bytes_total,
        "uploaded_bytes": bytes_total,
        "missing_bytes": 0,
        "upload_state_expires_at": None,
        "latest_failure": None,
        "archive_phase": "completed",
        "archive_uploaded_bytes": archive_stored_bytes or bytes_total,
        "archive_total_bytes": archive_stored_bytes or bytes_total,
        "archive_uploaded_parts": None,
        "archive_total_parts": None,
        "files": files,
        "collection": collection,
    }


def _wait_for_collection_state(
    api: ApiClient,
    collection_id: int,
    manifest: list[CollectionManifestEntry] | None,
    *,
    wait_mode: UploadWaitMode,
    status: Callable[[str], None] | None = None,
) -> tuple[dict[str, object], UploadCompletionState]:
    poll_seconds = _upload_finalize_poll_seconds()
    timeout_seconds = _upload_finalize_timeout_seconds()
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    last_status_log_at = 0.0
    last_payload: dict[str, object] | None = None
    wait_label = "archive verification" if wait_mode == "finalized" else "server handoff"
    success_state: UploadCompletionState = "finalized" if wait_mode == "finalized" else "staged"

    _report_upload_status(
        status,
        f"All files uploaded; waiting for {wait_label}",
    )
    while True:
        now = time.monotonic()
        transient_error: BaseException | None = None
        try:
            collection = api.get_collection(collection_id)
            return (
                _finalized_collection_upload_payload(collection_id, manifest, collection),
                success_state,
            )
        except NotFound:
            try:
                last_payload = api.get_collection_upload(collection_id)
            except NotFound:
                last_payload = None
            except Exception as exc:
                if not _is_transient_upload_error(exc):
                    raise
                transient_error = exc
        except Exception as exc:
            if not _is_transient_upload_error(exc):
                raise
            transient_error = exc

        if transient_error is not None:
            if now - last_status_log_at >= UPLOAD_FINALIZE_STATUS_INTERVAL_SECONDS:
                _report_upload_status(
                    status,
                    f"Waiting for {wait_label}: "
                    f"{_upload_error_description(transient_error)} while polling; retrying",
                )
                last_status_log_at = now
        elif last_payload is not None:
            state = str(last_payload.get("state", "unknown"))
            if wait_mode == "staged" and state == "archiving":
                return last_payload, "staged"
            if state == "failed":
                return last_payload, "failed"
            if state in {"canceled", "expired"}:
                raise RuntimeError(
                    f"collection upload became {state} before {wait_label} completed"
                )
            if now - last_status_log_at >= UPLOAD_FINALIZE_STATUS_INTERVAL_SECONDS:
                archive_status = (
                    _archive_wait_status(last_payload) if wait_mode == "finalized" else ""
                )
                _report_upload_status(
                    status,
                    f"Waiting for {wait_label}: "
                    f"state={state}, "
                    f"{last_payload.get('files_uploaded', 0)}/"
                    f"{last_payload.get('files_total', 0)} files, "
                    f"{last_payload.get('uploaded_bytes', 0)}/"
                    f"{last_payload.get('bytes_total', 0)} bytes staged"
                    f"{archive_status}",
                )
                last_status_log_at = now
        elif now - last_status_log_at >= UPLOAD_FINALIZE_STATUS_INTERVAL_SECONDS:
            _report_upload_status(
                status,
                f"Waiting for {wait_label}: upload session not visible yet",
            )
            last_status_log_at = now

        if deadline is not None and now >= deadline:
            if last_payload is not None:
                return last_payload, "timeout"
            files_total = len(manifest) if manifest is not None else 0
            bytes_total = sum(item["bytes"] for item in manifest or ())
            return (
                {
                    "collection_id": collection_id,
                    "state": "archiving" if wait_mode == "finalized" else "uploading",
                    "files": [],
                    "files_total": files_total,
                    "files_uploaded": 0,
                    "bytes_total": bytes_total,
                    "uploaded_bytes": 0,
                    "upload_state_expires_at": None,
                },
                "timeout",
            )
        sleep_seconds = poll_seconds
        if deadline is not None:
            sleep_seconds = max(0.0, min(poll_seconds, deadline - now))
        time.sleep(sleep_seconds)


def _wait_for_finalized_collection(
    api: ApiClient,
    collection_id: int,
    manifest: list[CollectionManifestEntry] | None,
    *,
    status: Callable[[str], None] | None = None,
) -> tuple[dict[str, object], UploadCompletionState]:
    return _wait_for_collection_state(
        api,
        collection_id,
        manifest,
        wait_mode="finalized",
        status=status,
    )


def _wait_for_staged_collection(
    api: ApiClient,
    collection_id: int,
    manifest: list[CollectionManifestEntry],
    *,
    status: Callable[[str], None] | None = None,
) -> tuple[dict[str, object], UploadCompletionState]:
    return _wait_for_collection_state(
        api,
        collection_id,
        manifest,
        wait_mode="staged",
        status=status,
    )


def _upload_collection_via_session(
    api: ApiClient,
    idempotency_key: str,
    tags: list[str],
    resolved_root: Path,
    *,
    ingest_source: str | None,
    wait_mode: UploadWaitMode,
    archive_store: str | None = None,
    json_mode: bool = False,
    file_concurrency: int,
    api_factory: Callable[[], ApiClient] | None = None,
) -> dict[str, object]:
    local_path_iter = _iter_local_collection_paths(resolved_root)
    try:
        first_source_path = next(local_path_iter)
    except StopIteration as exc:
        raise typer.BadParameter("collection source must contain at least one file") from exc

    _log_upload(f"Opening incremental upload session for {resolved_root}")
    session_payload = _create_or_resume_collection_upload_session(
        api,
        idempotency_key,
        tags,
        ingest_source=ingest_source,
        archive_store=archive_store,
    )
    collection_id = cast(int, session_payload["collection_id"])
    _log_upload(f"Upload session {collection_id}: registering files incrementally")

    manifest_by_path: dict[str, CollectionManifestEntry] = {}
    total_discovered_bytes = 0
    chunk_bytes = _upload_chunk_bytes()
    file_window = _upload_file_window(file_concurrency)

    upload_progress = make_collection_upload_progress(
        collection_id=collection_id,
        files_total=0,
        bytes_total=0,
        files_hashed=0,
        files_registered=0,
        file_concurrency=file_concurrency,
        chunk_bytes=chunk_bytes,
        discovery_complete=False,
        json_mode=json_mode,
        interval_seconds=UPLOAD_PROGRESS_INTERVAL_SECONDS,
    )

    work: Queue[tuple[Path, str, int] | None] = Queue(maxsize=file_window)
    manifest_lock = threading.Lock()
    failure_lock = threading.Lock()
    failures: list[BaseException] = []
    worker_factory = api_factory or (lambda: _new_upload_worker_client(api))

    def record_failure(exc: BaseException) -> None:
        with failure_lock:
            if not failures:
                failures.append(exc)

    def upload_worker() -> None:
        worker_api = api if file_concurrency == 1 else worker_factory()
        owns_api = worker_api is not api
        try:
            while True:
                item = work.get()
                try:
                    if item is None:
                        return
                    if failures:
                        continue
                    source_path, rel_path, byte_count = item
                    if byte_count >= _upload_file_log_bytes():
                        _log_upload(f"Hashing {rel_path} ({_format_bytes(byte_count)})")
                    entry: CollectionManifestEntry = {
                        "path": rel_path,
                        "bytes": byte_count,
                        "sha256": _file_sha256(source_path),
                    }
                    upload_progress.hashed_file()
                    registered_payload = _register_collection_upload_session_file(
                        worker_api,
                        collection_id,
                        entry,
                    )
                    upload_progress.registered_file()
                    file_payload = next(
                        (
                            current
                            for current in _response_upload_files(registered_payload)
                            if isinstance(current, dict) and current.get("path") == rel_path
                        ),
                        CollectionUploadFilePayload(
                            path=rel_path,
                            bytes=byte_count,
                            sha256=entry["sha256"],
                            upload_state="pending",
                            uploaded_bytes=0,
                        ),
                    )
                    _upload_collection_file(
                        worker_api,
                        collection_id,
                        source_path,
                        file_payload,
                        progress=upload_progress.uploaded,
                        resumed=upload_progress.resumed,
                    )
                    with manifest_lock:
                        manifest_by_path[rel_path] = entry
                    upload_progress.complete_file()
                except BaseException as exc:
                    record_failure(exc)
                finally:
                    work.task_done()
        finally:
            if owns_api:
                worker_api.close()

    with upload_progress:
        with ThreadPoolExecutor(max_workers=file_concurrency) as executor:
            futures = [executor.submit(upload_worker) for _ in range(file_concurrency)]
            discovery_complete = True
            try:
                for source_path in chain((first_source_path,), local_path_iter):
                    if failures:
                        discovery_complete = False
                        break
                    rel_path = source_path.relative_to(resolved_root).as_posix()
                    byte_count = source_path.stat().st_size
                    total_discovered_bytes += byte_count
                    upload_progress.set_totals(
                        files_total=upload_progress.files_total + 1,
                        bytes_total=total_discovered_bytes,
                    )
                    work.put((source_path, rel_path, byte_count))
            except BaseException as exc:
                discovery_complete = False
                record_failure(exc)
            finally:
                if discovery_complete:
                    upload_progress.finish_discovery()
                for _ in futures:
                    work.put(None)
                for future in futures:
                    future.result()

        if failures:
            upload_progress.notice(
                "Incremental upload interrupted; the open session remains resumable",
                phase="failed",
            )
            raise failures[0]

        manifest = [manifest_by_path[path] for path in sorted(manifest_by_path)]
        if len(manifest) != upload_progress.files_total:
            raise RuntimeError("incremental upload workers did not return every discovered file")

        upload_progress.notice("Reconciling the complete discovered file set", phase="reconciling")
        latest_payload = api.get_collection_upload(collection_id)
        local_path_set = {item["path"] for item in manifest}
        registered_paths = {
            str(item.get("path"))
            for item in _response_upload_files(latest_payload)
            if isinstance(item, dict)
        }
        if registered_paths != local_path_set:
            extra = sorted(registered_paths - local_path_set)
            missing = sorted(local_path_set - registered_paths)
            details: list[str] = []
            if extra:
                details.append(f"extra server files: {', '.join(extra[:5])}")
            if missing:
                details.append(f"missing server files: {', '.join(missing[:5])}")
            raise RuntimeError(
                "incremental upload session file set differs from local tree; "
                "not completing session" + (f" ({'; '.join(details)})" if details else "")
            )

        complete_payload = _complete_collection_upload_session(api, collection_id)
        upload_progress.notice(
            "All files uploaded; collection finalization will continue in the background",
            phase="finalizing",
        )
        if wait_mode == "finalized":
            final_payload, completion_state = _wait_for_finalized_collection(
                api,
                collection_id,
                manifest,
                status=lambda message: upload_progress.notice(message, phase="finalizing"),
            )
            if completion_state == "timeout":
                upload_progress.notice("Timed out waiting for finalization", phase="timeout")
                raise typer.Exit(124)
            if completion_state == "failed":
                upload_progress.notice("Collection finalization failed", phase="failed")
                raise typer.Exit(1)
            upload_progress.notice("Collection finalized", phase="finalized")
            return final_payload
        upload_progress.notice("Collection staged for background finalization", phase="staged")
        return complete_payload


def _archive_wait_status(payload: dict[str, object]) -> str:
    phase = payload.get("archive_phase")
    if not phase:
        return ""
    status = f", archive_phase={phase}"
    if phase == "planning":
        status += ", planning archive objects"
    uploaded_bytes = payload.get("archive_uploaded_bytes")
    total_bytes = payload.get("archive_total_bytes")
    if isinstance(uploaded_bytes, int) and isinstance(total_bytes, int) and total_bytes > 0:
        percent = uploaded_bytes / total_bytes * 100.0
        status += (
            f", archive={_format_bytes(uploaded_bytes)} / {_format_bytes(total_bytes)} "
            f"({percent:.1f}%)"
        )
    uploaded_parts = payload.get("archive_uploaded_parts")
    total_parts = payload.get("archive_total_parts")
    if isinstance(uploaded_parts, int) and isinstance(total_parts, int) and total_parts > 0:
        status += f", parts={uploaded_parts}/{total_parts}"
    latest_failure = payload.get("latest_failure")
    if latest_failure:
        status += f", latest_failure={latest_failure}"
    return status


_COLLECTION_SORT_FIELDS = {
    "id",
    "created_at",
    "bytes",
    "files",
}
_FIND_SORT_FIELDS = {
    "logical_path",
    "collection_id",
    "collection_path",
    "bytes",
}


def _collection_list_archive_copies_payload(
    collection: Mapping[str, object],
) -> list[dict[str, object]]:
    copies = collection.get("archive_copies")
    if not isinstance(copies, list):
        return []
    return [
        {
            key: copy[key]
            for key in (
                "store",
                "state",
                "storage_class",
                "stored_bytes",
                "last_uploaded_at",
                "last_verified_at",
                "failure",
            )
            if key in copy
        }
        for copy in copies
        if isinstance(copy, Mapping)
    ]


def _collection_list_item_payload(collection: Mapping[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {
        key: collection[key]
        for key in (
            "id",
            "created_at",
            "tags",
            "files",
            "bytes",
        )
        if key in collection
    }
    payload["archive_copies"] = _collection_list_archive_copies_payload(collection)
    return payload


def _compact_collection_page(payload: Mapping[str, object]) -> dict[str, object]:
    collections = payload.get("collections")
    compact_collections: list[dict[str, object]] = []
    if isinstance(collections, list):
        compact_collections = [
            _collection_list_item_payload(collection)
            for collection in collections
            if isinstance(collection, Mapping)
        ]
    page_payload = {
        key: payload[key]
        for key in (
            "page",
            "per_page",
            "total",
            "pages",
            "sort",
            "order",
            "query",
        )
        if key in payload
    }
    page_payload["collections"] = compact_collections
    return page_payload


def _sorted_collection_page(
    api: ApiClient,
    *,
    page: int,
    per_page: int,
    query: str | None,
    sort: str,
    order: str,
    all_items: bool = False,
) -> dict[str, Any]:
    if sort not in _COLLECTION_SORT_FIELDS:
        raise typer.BadParameter(
            f"sort must be one of {', '.join(sorted(_COLLECTION_SORT_FIELDS))}",
            param_hint="--sort",
        )
    normalized_order = order.casefold()
    if normalized_order not in {"asc", "desc"}:
        raise typer.BadParameter("order must be asc or desc", param_hint="--order")

    payload = api.list_collections(
        page=page,
        per_page=per_page,
        q=query,
        sort=sort,
        order=normalized_order,
        all_items=all_items,
    )
    return {
        **payload,
        "sort": sort,
        "order": normalized_order,
        "query": query,
    }


@collection_app.command("list")
def collection_list_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "id",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Substring match over collection ids"),
    ] = None,
    all_items: Annotated[
        bool,
        typer.Option("--all", help="Return every matching collection"),
    ] = False,
    ids: Annotated[
        bool,
        typer.Option("--ids", help="Emit one collection id per line"),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List collections with archive summaries."""

    if ids and json_mode:
        raise typer.BadParameter("--ids and --json cannot be used together")
    payload = _sorted_collection_page(
        client(),
        page=page,
        per_page=per_page,
        query=query,
        sort=sort,
        order=order,
        all_items=all_items,
    )
    if ids:
        emit(format_list_ids(payload, "collections"), json_mode=False)
        return
    emit(
        _compact_collection_page(payload) if json_mode else format_collections(payload),
        json_mode=json_mode,
    )


@collection_app.command("upload")
def upload_cmd(
    root: Annotated[Path, typer.Argument(help="Local collection root directory")],
    tags: Annotated[
        list[str] | None,
        typer.Option("--tag", help="Existing collection tag; repeat to assign more than one"),
    ] = None,
    idempotency_key: Annotated[
        str | None,
        typer.Option(
            "--idempotency-key",
            help="Stable retry key; defaults to a new UUID for this invocation",
        ),
    ] = None,
    archive_store: Annotated[
        str | None,
        typer.Option("--archive-store", help="Named archive store destination"),
    ] = None,
    wait: Annotated[
        str,
        typer.Option(
            "--wait",
            help="Wait until 'finalized' safe-to-delete archival or only 'staged' server handoff",
        ),
    ] = _default_upload_wait_mode(),
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
    session_mode: Annotated[
        bool,
        typer.Option(
            "--session",
            help="Register and upload files incrementally before explicitly completing",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Hash and preview without creating a session or uploading bytes",
        ),
    ] = False,
) -> None:
    """Upload a local directory as a collection."""

    wait_mode = _normalize_upload_wait_mode(wait)
    resolved_idempotency_key = idempotency_key or uuid.uuid4().hex
    resolved_tags = tags or []
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise typer.BadParameter("collection source must be a directory")

    if dry_run:
        _log_upload(f"Hashing collection manifest from {resolved_root}")
        manifest_started_at = time.monotonic()
        manifest = _local_collection_manifest(resolved_root)
        manifest_bytes = sum(item["bytes"] for item in manifest)
        _log_upload(
            "Manifest hashed: "
            f"{len(manifest)} files, {_format_bytes(manifest_bytes)} "
            f"in {time.monotonic() - manifest_started_at:.1f}s"
        )
        payload = _collection_upload_dry_run_plan(
            idempotency_key=resolved_idempotency_key,
            tags=resolved_tags,
            root=resolved_root,
            manifest=manifest,
            wait_mode=wait_mode,
            session_mode=session_mode,
            archive_store=archive_store,
        )
        emit(payload if json_mode else format_collection_upload_plan(payload), json_mode=json_mode)
        return

    api = client()
    file_concurrency = _upload_file_concurrency()
    if session_mode:
        payload = _upload_collection_via_session(
            api,
            resolved_idempotency_key,
            resolved_tags,
            resolved_root,
            ingest_source=str(resolved_root),
            archive_store=archive_store,
            wait_mode=wait_mode,
            json_mode=json_mode,
            file_concurrency=file_concurrency,
        )
        emit(payload if json_mode else format_collection_upload(payload), json_mode=json_mode)
        if payload.get("state") == "failed":
            raise typer.Exit(1)
        return

    _log_upload(f"Hashing collection manifest from {resolved_root}")
    manifest_started_at = time.monotonic()
    manifest = _local_collection_manifest(resolved_root)
    manifest_bytes = sum(item["bytes"] for item in manifest)
    _log_upload(
        "Manifest hashed: "
        f"{len(manifest)} files, {_format_bytes(manifest_bytes)} "
        f"in {time.monotonic() - manifest_started_at:.1f}s"
    )
    payload = _create_or_resume_collection_upload(
        api,
        resolved_idempotency_key,
        resolved_tags,
        manifest,
        ingest_source=str(resolved_root),
        archive_store=archive_store,
    )
    collection_id = cast(int, payload["collection_id"])
    upload_files = _response_upload_files(payload)
    uploaded_bytes = sum(
        min(int(file_payload.get("uploaded_bytes", 0)), int(file_payload["bytes"]))
        for file_payload in upload_files
    )
    uploaded_files = sum(
        1 for file_payload in upload_files if file_payload["upload_state"] == "uploaded"
    )
    _log_upload(
        f"Upload session {collection_id}: "
        f"{uploaded_files}/{len(upload_files)} files already uploaded, "
        f"{_format_bytes(uploaded_bytes)} / {_format_bytes(manifest_bytes)}"
    )
    chunk_bytes = _upload_chunk_bytes()
    upload_progress = make_collection_upload_progress(
        collection_id=collection_id,
        files_total=len(upload_files),
        bytes_total=manifest_bytes,
        files_uploaded=uploaded_files,
        uploaded_bytes=uploaded_bytes,
        file_concurrency=file_concurrency,
        chunk_bytes=chunk_bytes,
        json_mode=json_mode,
        interval_seconds=UPLOAD_PROGRESS_INTERVAL_SECONDS,
    )

    def note_uploaded(delta: int) -> None:
        upload_progress.uploaded(delta)

    with upload_progress:
        _upload_collection_files(
            api,
            collection_id,
            resolved_root,
            upload_files,
            progress=note_uploaded,
            file_complete=upload_progress.complete_file,
            file_concurrency=file_concurrency,
        )

        if wait_mode == "finalized":
            final_payload, completion_state = _wait_for_finalized_collection(
                api,
                collection_id,
                manifest,
                status=lambda message: upload_progress.notice(message, phase="finalizing"),
            )
            if completion_state == "timeout":
                upload_progress.notice("Timed out waiting for finalization", phase="timeout")
            elif completion_state == "failed":
                upload_progress.notice("Collection finalization failed", phase="failed")
            else:
                upload_progress.notice("Collection finalized", phase="finalized")
        else:
            final_payload, completion_state = _wait_for_staged_collection(
                api,
                collection_id,
                manifest,
                status=lambda message: upload_progress.notice(message, phase="finalizing"),
            )
            if completion_state == "timeout":
                upload_progress.notice("Timed out waiting for server handoff", phase="timeout")
            elif completion_state == "failed":
                upload_progress.notice("Collection finalization failed", phase="failed")
            else:
                upload_progress.notice(
                    "Collection staged for background finalization",
                    phase="staged",
                )
    emit(
        final_payload if json_mode else format_collection_upload(final_payload),
        json_mode=json_mode,
    )
    if completion_state == "failed":
        raise typer.Exit(1)
    if completion_state == "timeout":
        raise typer.Exit(124)


@collection_app.command("cancel")
def upload_cancel_cmd(
    collection_id: Annotated[int, typer.Argument(help="Open collection upload session id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Cancel an open collection upload session."""

    payload = client().cancel_collection_upload_session(collection_id)
    emit(payload if json_mode else format_collection_upload(payload), json_mode=json_mode)


@collection_app.command("watch")
def upload_watch_cmd(
    collection_id: Annotated[
        int,
        typer.Argument(help="Collection upload/session id to monitor until finalized"),
    ],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Wait for collection finalization to finish."""

    payload, completion_state = _wait_for_finalized_collection(
        client(),
        collection_id,
        None,
    )
    emit(payload if json_mode else format_collection_upload(payload), json_mode=json_mode)
    if completion_state == "failed":
        raise typer.Exit(1)
    if completion_state == "timeout":
        raise typer.Exit(124)


@app.command("find")
def find_cmd(
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Substring match over logical file paths"),
    ] = None,
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "logical_path",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    collection: Annotated[
        int | None,
        typer.Option("--collection", help="Restrict results to one collection"),
    ] = None,
    all_items: Annotated[
        bool,
        typer.Option("--all", help="Return every matching file"),
    ] = False,
    selectors: Annotated[
        bool,
        typer.Option("--selectors", help="Emit one COLLECTION_ID::PATH selector per line"),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Search logical files across collections."""

    if selectors and json_mode:
        raise typer.BadParameter("--selectors and --json cannot be used together")
    if sort not in _FIND_SORT_FIELDS:
        raise typer.BadParameter(
            f"sort must be one of {', '.join(sorted(_FIND_SORT_FIELDS))}",
            param_hint="--sort",
        )
    normalized_order = order.casefold()
    if normalized_order not in {"asc", "desc"}:
        raise typer.BadParameter("order must be asc or desc", param_hint="--order")
    payload = client().search(
        query,
        page=page,
        per_page=per_page,
        sort=sort,
        order=normalized_order,
        collection=collection,
        all_items=all_items,
    )
    if selectors:
        emit(format_file_selectors(payload), json_mode=False)
        return
    emit(payload if json_mode else format_find(payload), json_mode=json_mode)


@collection_app.command("show")
def show_cmd(
    collection: Annotated[int, typer.Argument(help="Collection id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show collection storage and archive details."""

    api = client()
    if json_mode:
        payload = api.get_collection(collection)
        emit(payload, json_mode=True)
        return
    payload = api.get_collection(collection)
    archive_payload = api.get_archive_report(collection=collection)
    emit(format_collection_summary(payload, archive_payload), json_mode=False)


@collection_app.command("delete")
def collection_delete_cmd(
    collection_id: Annotated[int, typer.Argument(help="Exact accepted collection id")],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            "--plan",
            help="Show the deletion plan and confirmation challenge without deleting",
        ),
    ] = False,
    confirm: Annotated[
        str | None,
        typer.Option(
            "--confirm",
            help="Short-lived confirmation challenge returned by a prior plan",
        ),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Permanently delete one accepted collection and its remote archive."""

    if dry_run and confirm is not None:
        raise typer.BadParameter("--dry-run and --confirm cannot be used together")
    api = client()
    if dry_run:
        payload = api.plan_collection_deletion(collection_id)
        emit(
            payload if json_mode else format_collection_deletion_plan(payload),
            json_mode=json_mode,
        )
        return
    if confirm is not None:
        payload = api.delete_collection(collection_id, challenge=confirm)
        emit(
            payload if json_mode else format_collection_deletion_result(payload),
            json_mode=json_mode,
        )
        return
    if json_mode:
        raise typer.BadParameter("--json requires --dry-run or --confirm")

    plan = api.plan_collection_deletion(collection_id)
    emit(format_collection_deletion_plan(plan), json_mode=False)
    blockers = plan.get("blockers")
    if isinstance(blockers, list) and blockers:
        raise typer.Exit(1)
    challenge = plan.get("challenge")
    if not isinstance(challenge, str) or not challenge:
        raise typer.BadParameter("server did not return a collection deletion challenge")
    typed_id = typer.prompt("Type the complete collection id to delete", type=int)
    if typed_id != collection_id:
        typer.echo("Collection id did not match; nothing was deleted.", err=True)
        raise typer.Exit(1)
    payload = api.delete_collection(collection_id, challenge=challenge)
    emit(format_collection_deletion_result(payload), json_mode=False)


@archive_app.command("copy")
def archive_copy_cmd(
    collection_id: Annotated[int, typer.Argument(help="Exact collection id")],
    destination_store: Annotated[
        str,
        typer.Option("--to", help="Destination archive store"),
    ],
    source_store: Annotated[
        str | None,
        typer.Option("--from", help="Source archive store; chosen automatically when omitted"),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Copy one collection between archive stores."""

    payload = client().create_or_resume_archive_copy(
        collection_id,
        destination_store=destination_store,
        source_store=source_store,
    )
    emit(payload if json_mode else format_archive_copy_job(payload), json_mode=json_mode)


@archive_app.command("retire")
def archive_retire_cmd(
    collection_id: Annotated[int, typer.Argument(help="Exact collection id")],
    store: Annotated[
        str,
        typer.Option("--store", help="Archive store whose copy will be retired"),
    ],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            "--plan",
            help="Show the retirement plan and confirmation challenge without deleting",
        ),
    ] = False,
    confirm: Annotated[
        str | None,
        typer.Option(
            "--confirm",
            help="Short-lived confirmation challenge returned by a prior plan",
        ),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Permanently retire one collection copy after verifying another store."""

    if dry_run and confirm is not None:
        raise typer.BadParameter("--dry-run and --confirm cannot be used together")
    api = client()
    if dry_run:
        payload = api.plan_archive_copy_retirement(collection_id, store=store)
        emit(
            payload if json_mode else format_archive_copy_retirement_plan(payload),
            json_mode=json_mode,
        )
        return
    if confirm is not None:
        payload = api.retire_archive_copy(
            collection_id,
            store=store,
            challenge=confirm,
        )
        emit(
            payload if json_mode else format_archive_copy_retirement_result(payload),
            json_mode=json_mode,
        )
        return
    if json_mode:
        raise typer.BadParameter("--json requires --dry-run or --confirm")

    plan = api.plan_archive_copy_retirement(collection_id, store=store)
    emit(format_archive_copy_retirement_plan(plan), json_mode=False)
    blockers = plan.get("blockers")
    if isinstance(blockers, list) and blockers:
        raise typer.Exit(1)
    challenge = plan.get("challenge")
    if not isinstance(challenge, str) or not challenge:
        raise typer.BadParameter("server did not return an archive copy retirement challenge")
    typed_id = typer.prompt("Type the complete collection id to retire from this store", type=int)
    if typed_id != collection_id:
        typer.echo("Collection id did not match; nothing was retired.", err=True)
        raise typer.Exit(1)
    typed_store = typer.prompt("Type the archive store to retire")
    if typed_store != store:
        typer.echo("Archive store did not match; nothing was retired.", err=True)
        raise typer.Exit(1)
    payload = api.retire_archive_copy(
        collection_id,
        store=store,
        challenge=challenge,
    )
    emit(format_archive_copy_retirement_result(payload), json_mode=False)


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code >= 500:
            return "service_unavailable"
        return "http_error"
    if isinstance(exc, httpx.TransportError):
        return "transport_error"
    if isinstance(exc, RiverhogError):
        return exc.code
    return "error"


def _error_message(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"service returned HTTP {exc.response.status_code}"
    return str(exc) or type(exc).__name__


def _json_requested(argv: list[str]) -> bool:
    return "--json" in argv


def _emit_cli_error(exc: BaseException, *, json_mode: bool) -> None:
    code = _error_code(exc)
    message = _error_message(exc)
    if json_mode:
        typer.echo(
            json.dumps(
                {"error": {"code": code, "message": message}},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    if isinstance(exc, httpx.TransportError):
        typer.echo(f"riverhog: transport error: {message}", err=True)
        return
    typer.echo(f"riverhog: {message}", err=True)


def main() -> int:
    try:
        app()
    except (
        httpx.HTTPStatusError,
        httpx.TransportError,
        RiverhogError,
        FileExistsError,
        FileNotFoundError,
        NotADirectoryError,
    ) as exc:
        _emit_cli_error(exc, json_mode=_json_requested(sys.argv[1:]))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
