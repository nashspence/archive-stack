from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict, cast

import httpx
import typer
from riverhog_api_client.client import ApiClient, ProvenanceMode
from riverhog_api_client.producer import COLLECTION_UPLOAD_REGISTRATION_BATCH_FILES
from riverhog_api_client.uploads import (
    configured_upload_concurrency,
    configured_upload_window,
    put_collection_upload_unit,
    upload_collection_units,
)
from riverhog_cli_support.application_keys import (
    format_app_key_created,
    format_app_key_revoked,
    format_app_key_rotated,
    format_app_keys,
    format_apps,
)
from riverhog_cli_support.output import (
    emit,
    error_document,
    format_lifecycle_events,
    format_list_ids,
)
from riverhog_protocol.collection_upload_transport import (
    CollectionUploadRegistrationConstraintsDocument,
)
from riverhog_protocol.errors import Conflict, RiverhogError, ServiceUnavailable
from riverhog_protocol.manifest import collection_content_identity
from riverhog_protocol.paths import (
    PathNormalizationError,
    normalize_collection_id,
    normalize_tag,
)
from riverhog_protocol.raw_ingress import hash_raw_source
from riverhog_provenance import (
    SIDECAR_SUFFIX,
    FileProvenanceBinding,
    build_provenance_archive,
    prepare_file_provenance,
    user_installation_id,
)
from time_formats import parse_duration, utc_timestamp_now

from riverhog_cli.local import local_app
from riverhog_cli.output import (
    format_app_access,
    format_app_access_selectors,
    format_app_access_set,
    format_archive_copy_job,
    format_archive_copy_jobs,
    format_archive_copy_retirement_plan,
    format_archive_copy_retirement_result,
    format_archive_copy_selectors,
    format_archive_store,
    format_archive_stores,
    format_collection_deletion_plan,
    format_collection_deletion_result,
    format_collection_summary,
    format_collection_tags,
    format_collection_upload,
    format_collection_upload_plan,
    format_collection_uploads,
    format_collections,
    format_download_quota,
    format_download_quotas,
    format_file_provenance,
    format_file_selectors,
    format_find,
    format_provenance_files,
    format_provenance_trace,
    format_provenance_verification,
    format_retrieval_cache_object,
    format_retrieval_cache_objects,
    format_retrieval_cache_selectors,
    format_retrieval_cache_status,
    format_tag,
    format_tag_deletion_plan,
    format_tag_deletion_result,
    format_tags,
)
from riverhog_cli.upload_progress import make_collection_upload_progress

app = typer.Typer(help="Riverhog archive platform CLI.")
collection_app = typer.Typer(help="Collection catalog and upload operations.")
collection_tag_app = typer.Typer(help="Collection tag assignments.")
collection_upload_app = typer.Typer(help="Collection upload sessions.")
collection_provenance_app = typer.Typer(help="Collection file provenance.")
archive_app = typer.Typer(help="Archive-store operations.")
archive_store_app = typer.Typer(help="Configured archive stores.")
archive_copy_app = typer.Typer(help="Archive-copy jobs.")
application_app = typer.Typer(help="Application access.")
app_key_app = typer.Typer(help="Application key management.")
app_key_access_app = typer.Typer(help="Per-key permission and resource access.")
app_key_quota_app = typer.Typer(help="Per-key remote-download quotas.")
tag_app = typer.Typer(help="Collection tag catalog.")
event_app = typer.Typer(help="Lifecycle event inspection.")
retrieval_app = typer.Typer(help="Retrieval operations.")
retrieval_cache_app = typer.Typer(help="Retrieval-cache inspection.")
app_key_app.add_typer(app_key_access_app, name="access")
app_key_app.add_typer(app_key_quota_app, name="quota")
application_app.add_typer(app_key_app, name="key")
app.add_typer(collection_app, name="collection")
collection_app.add_typer(collection_tag_app, name="tag")
collection_app.add_typer(collection_upload_app, name="upload")
collection_app.add_typer(collection_provenance_app, name="provenance")
app.add_typer(archive_app, name="archive")
archive_app.add_typer(archive_store_app, name="store")
archive_app.add_typer(archive_copy_app, name="copy")
app.add_typer(application_app, name="app")
app.add_typer(tag_app, name="tag")
app.add_typer(event_app, name="event")
app.add_typer(retrieval_app, name="retrieval")
retrieval_app.add_typer(retrieval_cache_app, name="cache")
app.add_typer(local_app, name="local")

HASH_CHUNK_BYTES = 8 * 1024 * 1024
UPLOAD_FILE_LOG_BYTES = 1 * 1024 * 1024
UPLOAD_PROGRESS_INTERVAL_SECONDS = 5.0
UPLOAD_FINALIZE_POLL_SECONDS = 5.0
UPLOAD_FINALIZE_STATUS_INTERVAL_SECONDS = 30.0
TRANSIENT_UPLOAD_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
UPLOAD_RESUME_RETRY_INITIAL_DELAY_SECONDS = 1.0
UPLOAD_RESUME_RETRY_MAX_DELAY_SECONDS = 10.0
UPLOAD_RESUME_RETRY_LOG_INTERVAL_SECONDS = 30.0
UPLOAD_LOG_LOCK = threading.Lock()
UploadCompletionState = Literal["finalized", "failed", "timeout"]
_API_CLIENT: ApiClient | None = None


class RawPartsPayload(TypedDict):
    part_plaintext_bytes: int
    sha256s: list[str]


class CollectionManifestEntry(TypedDict, total=False):
    path: str
    bytes: int
    sha256: str
    raw_parts: RawPartsPayload
    provenance: dict[str, object]
    provenance_journals: dict[str, bytes]


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


def _version_callback(value: bool) -> None:
    if not value:
        return
    typer.echo(importlib.metadata.version("riverhog-client"))
    raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the installed Riverhog client version",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    ctx.call_on_close(_close_client)


@event_app.command("list")
def event_list_cmd(
    after: Annotated[
        str | None,
        typer.Option("--after", help="Return events after this cursor"),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 100,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List application-visible lifecycle events."""

    payload = (
        client()
        .list_lifecycle_events(after=after, limit=limit)
        .model_dump(mode="json", exclude_none=True)
    )
    emit(payload if json_mode else format_lifecycle_events(payload), json_mode=json_mode)


_APP_SORT_FIELDS = {"name", "keys", "active_keys", "last_used_at"}
_APP_KEY_SORT_FIELDS = {"id", "created_at", "expires_at", "last_used_at"}
_APP_KEY_ACCESS_SORT_FIELDS = {"app", "key_id", "permission", "resource", "created_at"}
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


def _access_selector(value: str) -> tuple[str, str, dict[str, str]]:
    app_name, separator, remainder = value.partition("::")
    key_id, second_separator, allow = remainder.partition("::")
    if not separator or not second_separator or not app_name or not key_id or not allow:
        raise typer.BadParameter(
            "access selector must be APP::KEY_ID::PERMISSION or APP::KEY_ID::PERMISSION=RESOURCE",
            param_hint="SELECTOR",
        )
    return app_name, key_id, _access(allow)


def _archive_copy_selector(value: str) -> tuple[int, str]:
    collection_id, separator, destination_store = value.partition("::")
    if not separator or not collection_id or not destination_store:
        raise typer.BadParameter(
            "archive-copy selector must be COLLECTION_ID::DESTINATION_STORE",
            param_hint="SELECTOR",
        )
    try:
        normalized_collection_id = normalize_collection_id(collection_id)
    except PathNormalizationError as exc:
        raise typer.BadParameter(str(exc), param_hint="SELECTOR") from exc
    return normalized_collection_id, destination_store


def _retrieval_cache_selector(value: str) -> tuple[int, str, str]:
    collection_id, separator, remainder = value.partition("::")
    source_store, second_separator, object_id = remainder.partition("::")
    if (
        not separator
        or not second_separator
        or not collection_id
        or not source_store
        or not object_id
    ):
        raise typer.BadParameter(
            "retrieval-cache selector must be COLLECTION_ID::SOURCE_STORE::OBJECT_ID",
            param_hint="SELECTOR",
        )
    try:
        normalized_collection_id = normalize_collection_id(collection_id)
    except PathNormalizationError as exc:
        raise typer.BadParameter(str(exc), param_hint="SELECTOR") from exc
    return normalized_collection_id, source_store, object_id


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
        sort=cast(Any, sort),
        order=cast(Any, normalized_order),
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
        sort=cast(Any, sort),
        order=cast(Any, normalized_order),
        all_items=all_items,
    )
    if ids:
        emit(format_list_ids(payload, "tags"), json_mode=False)
        return
    emit(payload if json_mode else format_tags(payload), json_mode=json_mode)


@tag_app.command("delete")
def tag_delete_cmd(
    tag: Annotated[str, typer.Argument(help="Exact tag id")],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            "--plan",
            help="Show blockers and a short-lived confirmation challenge",
        ),
    ] = False,
    confirm: Annotated[
        str | None,
        typer.Option("--confirm", help="Challenge returned by a prior plan"),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Delete an unused tag after an explicit, non-cascading preflight."""

    if dry_run and confirm is not None:
        raise typer.BadParameter("--dry-run and --confirm cannot be used together")
    api = client()
    if dry_run:
        payload = api.plan_tag_deletion(tag)
        emit(payload if json_mode else format_tag_deletion_plan(payload), json_mode=json_mode)
        return
    if confirm is not None:
        payload = api.delete_tag(tag, challenge=confirm)
        emit(payload if json_mode else format_tag_deletion_result(payload), json_mode=json_mode)
        return
    if json_mode:
        raise typer.BadParameter("--json requires --dry-run or --confirm")

    plan = api.plan_tag_deletion(tag)
    emit(format_tag_deletion_plan(plan), json_mode=False)
    blockers = plan.get("blockers")
    if isinstance(blockers, list) and blockers:
        raise typer.Exit(1)
    challenge = plan.get("challenge")
    if not isinstance(challenge, str) or not challenge:
        raise typer.BadParameter("server did not return a tag deletion challenge")
    typed_tag = typer.prompt("Type the complete tag id to delete")
    if typed_tag != tag:
        typer.echo("Tag id did not match; nothing was deleted.", err=True)
        raise typer.Exit(1)
    payload = api.delete_tag(tag, challenge=challenge)
    emit(format_tag_deletion_result(payload), json_mode=False)


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


@collection_tag_app.command("add")
def collection_tag_add_cmd(
    collection_id: Annotated[int, typer.Argument(help="Collection id")],
    tag: Annotated[str, typer.Argument(help="Tag id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Assign one existing tag to a collection."""

    payload = client().add_collection_tag(collection_id, tag)
    emit(payload if json_mode else format_collection_tags(payload), json_mode=json_mode)


@collection_tag_app.command("remove")
def collection_tag_remove_cmd(
    collection_id: Annotated[int, typer.Argument(help="Collection id")],
    tag: Annotated[str, typer.Argument(help="Tag id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Remove one tag from a collection."""

    payload = client().remove_collection_tag(collection_id, tag)
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
        sort=cast(Any, sort),
        order=cast(Any, normalized_order),
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
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "permission",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Substring match over permissions and resources"),
    ] = None,
    app_name: Annotated[
        str | None,
        typer.Option("--app", help="Restrict results to one application"),
    ] = None,
    key_id: Annotated[
        str | None,
        typer.Option("--key", help="Restrict results to one key"),
    ] = None,
    permission: Annotated[
        str | None,
        typer.Option("--permission", help="Restrict results to one exact permission"),
    ] = None,
    resource: Annotated[
        str | None,
        typer.Option("--resource", help="Restrict results to one exact resource"),
    ] = None,
    active: Annotated[
        bool | None,
        typer.Option("--active/--inactive", help="Filter by key usability"),
    ] = None,
    all_items: Annotated[bool, typer.Option("--all", help="Return every matching grant")] = False,
    selectors: Annotated[
        bool,
        typer.Option("--selectors", help="Emit one actionable access selector per line"),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List exact application-key permission and resource bindings."""

    if selectors and json_mode:
        raise typer.BadParameter("--selectors and --json cannot be used together")
    normalized_order = _list_order(sort, order, fields=_APP_KEY_ACCESS_SORT_FIELDS)
    payload = client().list_app_key_access(
        page=page,
        per_page=per_page,
        q=query,
        sort=cast(Any, sort),
        order=cast(Any, normalized_order),
        app=app_name,
        key_id=key_id,
        permission=permission,
        resource=resource,
        active=active,
        all_items=all_items,
    )
    if selectors:
        emit(format_app_access_selectors(payload), json_mode=False)
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


@app_key_access_app.command("add")
def app_key_access_add_cmd(
    app_name: Annotated[str, typer.Argument(help="Application name")],
    key_id: Annotated[str, typer.Argument(help="Key id")],
    allow: Annotated[str, typer.Argument(help="PERMISSION or PERMISSION=RESOURCE")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Add one exact permission and resource binding."""

    access = _access(allow)
    payload = client().add_app_key_access(
        app_name,
        key_id,
        permission=cast(Any, access["permission"]),
        resource=access["resource"],
    )
    emit(payload if json_mode else format_app_access_set(payload), json_mode=json_mode)


@app_key_access_app.command("remove")
def app_key_access_remove_cmd(
    selector: Annotated[
        str,
        typer.Argument(help="APP::KEY_ID::PERMISSION or APP::KEY_ID::PERMISSION=RESOURCE"),
    ],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Remove one exact permission and resource binding."""

    app_name, key_id, access = _access_selector(selector)
    payload = client().remove_app_key_access(
        app_name,
        key_id,
        permission=cast(Any, access["permission"]),
        resource=access["resource"],
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
    if ids and app_name is None:
        raise typer.BadParameter("--ids requires --app so each key id is actionable")
    normalized_order = _list_order(sort, order, fields=_APP_KEY_QUOTA_SORT_FIELDS)
    payload = client().list_download_quotas(
        page=page,
        per_page=per_page,
        q=query,
        sort=cast(Any, sort),
        order=cast(Any, normalized_order),
        app=app_name,
        active=active,
        all_items=all_items,
    )
    if ids:
        emit(format_list_ids(payload, "quotas"), json_mode=False)
        return
    emit(payload if json_mode else format_download_quotas(payload), json_mode=json_mode)


def _iter_file_chunks(
    path: Path,
    *,
    offset: int = 0,
    limit: int | None = None,
    chunk_size: int = HASH_CHUNK_BYTES,
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
    for chunk in _iter_file_chunks(path):
        digest.update(chunk)
    return digest.hexdigest()


def _upload_file_concurrency() -> int:
    try:
        return configured_upload_concurrency()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


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
    return None if value == 0 else value


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
    else:
        status(message)


def _is_transient_upload_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in TRANSIENT_UPLOAD_STATUS_CODES
    return isinstance(exc, ServiceUnavailable)


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


def _create_or_resume_collection_upload_session(
    api: ApiClient,
    idempotency_key: str,
    tags: list[str],
    *,
    ingest_source: str | None,
    archive_store: str | None = None,
    provenance_mode: ProvenanceMode,
    provenance_omission_reason: str | None,
) -> dict[str, Any]:
    return _retry_transient_upload_operation(
        "Upload session open/resume",
        lambda: api.create_or_resume_collection_upload_session(
            idempotency_key,
            tags,
            ingest_source=ingest_source,
            archive_store=archive_store,
            provenance_mode=provenance_mode,
            provenance_omission_reason=provenance_omission_reason,
        ),
    )


def _register_collection_upload_session_files(
    api: ApiClient,
    collection_id: int,
    file_payloads: list[CollectionManifestEntry],
    *,
    registration_constraints: CollectionUploadRegistrationConstraintsDocument,
) -> dict[str, Any]:
    return _retry_transient_upload_operation(
        f"Upload session register {len(file_payloads)} file(s)",
        lambda: api.register_collection_upload_session_files(
            collection_id,
            [
                {key: value for key, value in item.items() if key != "provenance_journals"}
                for item in file_payloads
            ],
            registration_constraints=registration_constraints,
        ),
    )


def _complete_collection_upload_session(
    api: ApiClient,
    collection_id: int,
    manifest: list[CollectionManifestEntry],
) -> dict[str, Any]:
    return _retry_transient_upload_operation(
        "Upload session complete",
        lambda: api.complete_collection_upload_session(
            collection_id,
            files_total=len(manifest),
            content_identity=collection_content_identity(
                (item["path"], item["bytes"], item["sha256"]) for item in manifest
            ),
            provenance_identity=_provenance_identity(manifest),
        ),
    )


def _local_collection_manifest(root: Path) -> list[CollectionManifestEntry]:
    files: list[CollectionManifestEntry] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _is_provenance_control_path(root, path):
            continue
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
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
        "archive_store": archive_store,
        "server_validation": "not_run",
        "created_at": utc_timestamp_now(),
        "files_preview": manifest[:5],
    }


def _session_registration_constraints(
    payload: Mapping[str, object],
) -> CollectionUploadRegistrationConstraintsDocument:
    constraints = payload.get("registration_constraints")
    if not isinstance(constraints, Mapping):
        raise RuntimeError("open upload session is missing registration constraints")
    try:
        return CollectionUploadRegistrationConstraintsDocument.model_validate(dict(constraints))
    except ValueError as exc:
        raise RuntimeError("upload session returned invalid registration constraints") from exc


def _hash_collection_source(
    root: Path,
    source_path: Path,
    *,
    pack_member_bytes: int,
    raw_part_plaintext_bytes: int,
    provenance: Path | None = None,
    omit_provenance: str | None = None,
) -> CollectionManifestEntry:
    rel_path = source_path.relative_to(root).as_posix()
    byte_count = source_path.stat().st_size
    if byte_count >= _upload_file_log_bytes():
        _log_upload(f"Hashing {rel_path} ({_format_bytes(byte_count)})")
    if byte_count < pack_member_bytes:
        result: CollectionManifestEntry = {
            "path": rel_path,
            "bytes": byte_count,
            "sha256": _file_sha256(source_path),
        }
    else:
        raw = hash_raw_source(
            path=rel_path,
            chunks=_iter_file_chunks(source_path),
            expected_bytes=byte_count,
            part_plaintext_bytes=raw_part_plaintext_bytes,
        )
        result = {
            "path": rel_path,
            "bytes": byte_count,
            "sha256": raw.sha256,
            "raw_parts": {
                "part_plaintext_bytes": raw.part_plaintext_bytes,
                "sha256s": list(raw.part_sha256s),
            },
        }
    prepared = prepare_file_provenance(
        source_path,
        relative_path=rel_path,
        host_id=user_installation_id("riverhog-client"),
        agent_name="riverhog-client",
        agent_version=importlib.metadata.version("riverhog-client"),
        provenance=provenance,
        omit_reason=omit_provenance,
    )
    result["provenance"] = {
        "status": prepared.binding.status,
        **(
            {
                "journal_id": prepared.binding.journal_id,
                "current_state_id": prepared.binding.current_state_id,
            }
            if prepared.binding.status == "captured"
            else {"omission_reason": prepared.binding.omission_reason}
        ),
    }
    result["provenance_journals"] = prepared.journals
    return result


def _server_manifest(api: ApiClient, collection_id: int) -> list[CollectionManifestEntry]:
    payload = api.list_collection_upload_session_files(collection_id, all_items=True)
    values = payload.get("files")
    if not isinstance(values, list):
        raise RuntimeError("upload session returned an invalid file list")
    result: list[CollectionManifestEntry] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise RuntimeError("upload session returned an invalid file record")
        path = value.get("path")
        byte_count = value.get("bytes")
        sha256 = value.get("sha256")
        if (
            not isinstance(path, str)
            or not isinstance(byte_count, int)
            or not isinstance(sha256, str)
        ):
            raise RuntimeError("upload session returned an invalid file identity")
        provenance = value.get("provenance")
        if not isinstance(provenance, Mapping):
            raise RuntimeError("upload session returned no file provenance identity")
        result.append(
            {
                "path": path,
                "bytes": byte_count,
                "sha256": sha256,
                "provenance": dict(provenance),
            }
        )
    return result


def _manifest_identity(manifest: list[CollectionManifestEntry]) -> list[tuple[object, ...]]:
    return [
        (
            item["path"],
            item["bytes"],
            item["sha256"],
            json.dumps(item.get("provenance"), sort_keys=True, separators=(",", ":")),
        )
        for item in manifest
    ]


def _provenance_identity(manifest: list[CollectionManifestEntry]) -> str | None:
    bindings: list[FileProvenanceBinding] = []
    journals: dict[str, bytes] = {}
    for item in manifest:
        provenance = item.get("provenance")
        if not isinstance(provenance, Mapping):
            raise RuntimeError("local manifest has no provenance accounting")
        status = str(provenance.get("status", ""))
        bindings.append(
            FileProvenanceBinding(
                path=item["path"],
                bytes=item["bytes"],
                sha256=item["sha256"],
                status=cast(Any, status),
                journal_id=cast(str | None, provenance.get("journal_id")),
                current_state_id=cast(str | None, provenance.get("current_state_id")),
                omission_reason=cast(str | None, provenance.get("omission_reason")),
            )
        )
        journals.update(item.get("provenance_journals", {}))
    if not journals:
        return None
    return build_provenance_archive(bindings=bindings, journals=journals).identity


def _put_provenance_journals(
    api: ApiClient,
    collection_id: int,
    manifest: list[CollectionManifestEntry],
) -> None:
    journals: dict[str, bytes] = {}
    for item in manifest:
        for journal_id, content in item.get("provenance_journals", {}).items():
            previous = journals.get(journal_id)
            if previous is not None and previous != content:
                raise Conflict(f"local provenance journal bytes disagree: {journal_id}")
            journals[journal_id] = content
    for journal_id, content in sorted(journals.items()):
        _retry_transient_upload_operation(
            f"Upload provenance journal {journal_id}",
            partial(
                api.put_collection_upload_session_provenance_journal,
                collection_id,
                journal_id,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            ),
        )


def _is_provenance_control_path(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    return path.name.endswith(SIDECAR_SUFFIX) or relative.parts[:2] == (
        ".riverhog",
        "provenance",
    )


def _upload_unit_content(
    root: Path,
    unit: Mapping[str, object],
) -> bytes:
    expected = unit.get("payload_bytes")
    sources = unit.get("sources")
    if not isinstance(expected, int) or expected < 0 or not isinstance(sources, list):
        raise RuntimeError("server returned an invalid upload unit")
    content = bytearray()
    for source in sources:
        if not isinstance(source, Mapping):
            raise RuntimeError("server returned an invalid upload unit source")
        path = source.get("path")
        offset = source.get("offset")
        byte_count = source.get("bytes")
        if (
            not isinstance(path, str)
            or not isinstance(offset, int)
            or offset < 0
            or not isinstance(byte_count, int)
            or byte_count < 0
        ):
            raise RuntimeError("server returned an invalid upload unit source")
        source_path = root / path
        for chunk in _iter_file_chunks(
            source_path,
            offset=offset,
            limit=byte_count,
        ):
            content.extend(chunk)
    if len(content) != expected:
        raise RuntimeError(
            f"local sources produced {len(content)} bytes for a {expected}-byte upload unit"
        )
    return bytes(content)


def _put_collection_upload_session_unit(
    api: ApiClient,
    collection_id: int,
    volume: Mapping[str, object],
    unit: Mapping[str, object],
    *,
    root: Path,
) -> int:
    return put_collection_upload_unit(
        api,
        collection_id,
        volume,
        unit,
        content_for_unit=lambda current: _upload_unit_content(root, current),
        retry_notice=_log_upload,
        retry_initial_delay_seconds=UPLOAD_RESUME_RETRY_INITIAL_DELAY_SECONDS,
        retry_max_delay_seconds=UPLOAD_RESUME_RETRY_MAX_DELAY_SECONDS,
    )


def _upload_planned_units(
    api: ApiClient,
    collection_id: int,
    root: Path,
    *,
    concurrency: int,
    progress: Any,
    api_factory: Callable[[], ApiClient] | None,
) -> None:
    upload_collection_units(
        api,
        collection_id,
        content_for_unit=lambda unit: _upload_unit_content(root, unit),
        concurrency=concurrency,
        window=configured_upload_window(concurrency=concurrency),
        client_factory=api_factory,
        on_committed=progress.uploaded,
        on_resumed=progress.resumed,
        retry_notice=_log_upload,
    )


def _wait_for_finalized_collection(
    api: ApiClient,
    collection_id: int,
    manifest: list[CollectionManifestEntry] | None,
    *,
    status: Callable[[str], None] | None = None,
) -> tuple[dict[str, object], UploadCompletionState]:
    poll_seconds = _upload_finalize_poll_seconds()
    timeout_seconds = _upload_finalize_timeout_seconds()
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    last_status_log_at = 0.0
    last_payload: dict[str, object] | None = None

    _report_upload_status(status, "Waiting for verified archive custody")
    while True:
        now = time.monotonic()
        try:
            last_payload = api.get_collection_upload_session(collection_id)
            state = str(last_payload.get("state", "unknown"))
            if state == "finalized":
                return last_payload, "finalized"
            if state == "failed":
                return last_payload, "failed"
            if state == "canceled":
                raise RuntimeError("collection upload was canceled before custody completed")
            if now - last_status_log_at >= UPLOAD_FINALIZE_STATUS_INTERVAL_SECONDS:
                _report_upload_status(
                    status,
                    f"Waiting for verified archive custody: state={state}"
                    f"{_archive_wait_status(last_payload)}",
                )
                last_status_log_at = now
        except Exception as exc:
            if not _is_transient_upload_error(exc):
                raise
            if now - last_status_log_at >= UPLOAD_FINALIZE_STATUS_INTERVAL_SECONDS:
                _report_upload_status(
                    status,
                    f"Custody status unavailable ({_upload_error_description(exc)}); retrying",
                )
                last_status_log_at = now

        if deadline is not None and now >= deadline:
            return last_payload or {
                "collection_id": collection_id,
                "state": "finalizing",
                "files_total": len(manifest or ()),
                "bytes_total": sum(item["bytes"] for item in manifest or ()),
            }, "timeout"
        sleep_seconds = poll_seconds
        if deadline is not None:
            sleep_seconds = max(0.0, min(poll_seconds, deadline - now))
        time.sleep(sleep_seconds)


def _upload_collection_via_session(
    api: ApiClient,
    idempotency_key: str,
    tags: list[str],
    resolved_root: Path,
    *,
    ingest_source: str | None,
    archive_store: str | None = None,
    json_mode: bool = False,
    file_concurrency: int,
    api_factory: Callable[[], ApiClient] | None = None,
    provenance: Path | None = None,
    omit_provenance: str | None = None,
) -> dict[str, object]:
    _log_upload(f"Opening direct-to-archive upload session for {resolved_root}")
    session_payload = _create_or_resume_collection_upload_session(
        api,
        idempotency_key,
        tags,
        ingest_source=ingest_source,
        archive_store=archive_store,
        provenance_mode="omitted" if omit_provenance is not None else "captured",
        provenance_omission_reason=omit_provenance,
    )
    collection_id = cast(int, session_payload["collection_id"])
    if session_payload.get("state") == "finalized":
        _log_upload(f"Collection {collection_id} already finalized for this retry key")
        return session_payload

    registration_constraints = _session_registration_constraints(session_payload)
    pack_member_bytes = registration_constraints.pack_member_bytes
    raw_part_plaintext_bytes = registration_constraints.raw_part_plaintext_bytes
    paths = [
        path
        for path in sorted(resolved_root.rglob("*"))
        if path.is_file() and not _is_provenance_control_path(resolved_root, path)
    ]
    if not paths:
        raise typer.BadParameter("collection source must contain at least one file")
    bytes_total = sum(path.stat().st_size for path in paths)
    progress = make_collection_upload_progress(
        collection_id=collection_id,
        files_total=len(paths),
        bytes_total=bytes_total,
        files_hashed=0,
        files_registered=0,
        file_concurrency=file_concurrency,
        chunk_bytes=raw_part_plaintext_bytes,
        json_mode=json_mode,
        interval_seconds=UPLOAD_PROGRESS_INTERVAL_SECONDS,
    )

    with progress:
        progress.notice("Hashing source identities", phase="hashing")

        def hash_one(path: Path) -> CollectionManifestEntry:
            return _hash_collection_source(
                resolved_root,
                path,
                pack_member_bytes=pack_member_bytes,
                raw_part_plaintext_bytes=raw_part_plaintext_bytes,
                provenance=provenance,
                omit_provenance=omit_provenance,
            )

        with ThreadPoolExecutor(max_workers=file_concurrency) as executor:
            manifest = []
            for entry in executor.map(hash_one, paths):
                manifest.append(entry)
                progress.hashed_file()

        state = str(session_payload.get("state") or "open")
        if state == "open":
            progress.notice("Uploading validated provenance journals", phase="registering")
            _put_provenance_journals(api, collection_id, manifest)
            progress.notice("Registering the canonical file manifest", phase="registering")
            for start in range(0, len(manifest), COLLECTION_UPLOAD_REGISTRATION_BATCH_FILES):
                batch = manifest[start : start + COLLECTION_UPLOAD_REGISTRATION_BATCH_FILES]
                _register_collection_upload_session_files(
                    api,
                    collection_id,
                    batch,
                    registration_constraints=registration_constraints,
                )
                for _ in batch:
                    progress.registered_file()
        else:
            _put_provenance_journals(api, collection_id, manifest)
            existing = _server_manifest(api, collection_id)
            if _manifest_identity(existing) != _manifest_identity(manifest):
                raise Conflict("local collection identity differs from the resumable session")
            for _ in manifest:
                progress.registered_file()

        progress.notice("Closing discovery and persisting final volume plans", phase="planning")
        _complete_collection_upload_session(api, collection_id, manifest)
        progress.notice("Uploading plaintext units over the authenticated API", phase="uploading")
        _upload_planned_units(
            api,
            collection_id,
            resolved_root,
            concurrency=file_concurrency,
            progress=progress,
            api_factory=api_factory,
        )
        for _ in manifest:
            progress.complete_file()
        final_payload, completion_state = _wait_for_finalized_collection(
            api,
            collection_id,
            manifest,
            status=lambda message: progress.notice(message, phase="finalizing"),
        )
        if completion_state == "timeout":
            progress.notice("Timed out waiting for verified custody", phase="timeout")
            raise typer.Exit(124)
        if completion_state == "failed":
            progress.notice("Collection finalization failed", phase="failed")
            raise typer.Exit(1)
        progress.notice("Collection finalized with verified archive custody", phase="finalized")
        return final_payload


def _archive_wait_status(payload: Mapping[str, object]) -> str:
    phase = payload.get("archive_phase")
    status = f", archive_phase={phase}" if phase else ""
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
    "file_ref",
    "collection_id",
    "path",
    "bytes",
}


def _sorted_collection_page(
    api: ApiClient,
    *,
    page: int,
    per_page: int,
    query: str | None,
    tag: str | None,
    encryption_format: str | None,
    passphrase_id: str | None,
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

    return api.list_collections(
        page=page,
        per_page=per_page,
        q=query,
        tag=tag,
        encryption_format=encryption_format,
        passphrase_id=passphrase_id,
        sort=cast(Any, sort),
        order=cast(Any, normalized_order),
        all_items=all_items,
    )


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
    tag: Annotated[
        str | None,
        typer.Option("--tag", help="Restrict results to one exact tag"),
    ] = None,
    encryption_format: Annotated[
        str | None,
        typer.Option("--encryption-format", help="Restrict results to one archive format"),
    ] = None,
    passphrase_id: Annotated[
        str | None,
        typer.Option("--passphrase-id", help="Restrict results to one opaque key ID"),
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
        tag=tag,
        encryption_format=encryption_format,
        passphrase_id=passphrase_id,
        sort=cast(Any, sort),
        order=order,
        all_items=all_items,
    )
    if ids:
        emit(format_list_ids(payload, "collections"), json_mode=False)
        return
    emit(payload if json_mode else format_collections(payload), json_mode=json_mode)


@collection_upload_app.command("start")
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
    provenance: Annotated[
        Path | None,
        typer.Option(
            "--provenance",
            help="Existing Riverhog provenance journal or recovered provenance set",
        ),
    ] = None,
    omit_provenance: Annotated[
        str | None,
        typer.Option(
            "--omit-provenance",
            help="Explicit reason to omit provenance for the whole collection",
        ),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Hash and preview without creating a session or uploading bytes",
        ),
    ] = False,
) -> None:
    """Upload a local directory as a collection."""

    resolved_idempotency_key = idempotency_key or uuid.uuid4().hex
    resolved_tags = tags or []
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise typer.BadParameter("collection source must be a directory")
    if provenance is not None and omit_provenance is not None:
        raise typer.BadParameter("--provenance and --omit-provenance are mutually exclusive")
    resolved_provenance = provenance.expanduser().resolve() if provenance is not None else None
    if resolved_provenance is not None and not resolved_provenance.exists():
        raise typer.BadParameter("--provenance path does not exist")

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
            archive_store=archive_store,
        )
        emit(payload if json_mode else format_collection_upload_plan(payload), json_mode=json_mode)
        return

    api = client()
    file_concurrency = _upload_file_concurrency()
    payload = _upload_collection_via_session(
        api,
        resolved_idempotency_key,
        resolved_tags,
        resolved_root,
        ingest_source=str(resolved_root),
        archive_store=archive_store,
        json_mode=json_mode,
        file_concurrency=file_concurrency,
        provenance=resolved_provenance,
        omit_provenance=omit_provenance,
    )
    emit(
        payload if json_mode else format_collection_upload(payload),
        json_mode=json_mode,
    )
    if payload.get("state") == "failed":
        raise typer.Exit(1)


_COLLECTION_UPLOAD_SORT_FIELDS = {"id", "created_at", "state", "bytes", "files"}


@collection_upload_app.command("list")
def upload_list_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "created_at",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "desc",
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Search session ids, tags, or ingest sources"),
    ] = None,
    tag: Annotated[
        str | None,
        typer.Option("--tag", help="Restrict results to one exact tag"),
    ] = None,
    state: Annotated[
        str | None,
        typer.Option("--state", help="Restrict results to one exact session state"),
    ] = None,
    all_items: Annotated[
        bool,
        typer.Option("--all", help="Return every matching upload session"),
    ] = False,
    ids: Annotated[
        bool,
        typer.Option("--ids", help="Emit one collection/upload id per line"),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List collection upload sessions visible to the current application."""

    if ids and json_mode:
        raise typer.BadParameter("--ids and --json cannot be used together")
    normalized_order = _list_order(sort, order, fields=_COLLECTION_UPLOAD_SORT_FIELDS)
    payload = client().list_collection_upload_sessions(
        page=page,
        per_page=per_page,
        q=query,
        tag=tag,
        state=cast(Any, state),
        sort=cast(Any, sort),
        order=cast(Any, normalized_order),
        all_items=all_items,
    )
    if ids:
        emit(
            format_list_ids(payload, "uploads", id_key="collection_id"),
            json_mode=False,
        )
        return
    emit(payload if json_mode else format_collection_uploads(payload), json_mode=json_mode)


@collection_upload_app.command("show")
def upload_show_cmd(
    collection_id: Annotated[int, typer.Argument(help="Collection upload session id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show one collection upload session."""

    payload = client().get_collection_upload_session(collection_id)
    emit(payload if json_mode else format_collection_upload(payload), json_mode=json_mode)


@collection_upload_app.command("cancel")
def upload_cancel_cmd(
    collection_id: Annotated[int, typer.Argument(help="Open collection upload session id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Cancel an open collection upload session."""

    payload = client().cancel_collection_upload_session(collection_id)
    emit(payload if json_mode else format_collection_upload(payload), json_mode=json_mode)


@collection_upload_app.command("watch")
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
        typer.Option("--query", "-q", help="Substring match over collection file references"),
    ] = None,
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "file_ref",
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
    """Search files across collections."""

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
        sort=cast(Any, sort),
        order=cast(Any, normalized_order),
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

    payload = client().get_collection(collection)
    emit(payload if json_mode else format_collection_summary(payload), json_mode=json_mode)


_PROVENANCE_SORT_FIELDS = {"path", "bytes", "status"}


@collection_provenance_app.command("list")
def provenance_list_cmd(
    collection_id: Annotated[int, typer.Argument(help="Collection id")],
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "path",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Substring match over collection paths"),
    ] = None,
    status: Annotated[
        str | None,
        typer.Option("--status", help="Restrict to captured or omitted files"),
    ] = None,
    all_items: Annotated[bool, typer.Option("--all", help="Return every matching file")] = False,
    selectors: Annotated[
        bool,
        typer.Option("--selectors", help="Emit one COLLECTION_ID::PATH selector per line"),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List every file's provenance accounting in one collection."""

    if selectors and json_mode:
        raise typer.BadParameter("--selectors and --json cannot be used together")
    normalized_order = _list_order(sort, order, fields=_PROVENANCE_SORT_FIELDS)
    if status is not None and status not in {"captured", "omitted"}:
        raise typer.BadParameter("status must be captured or omitted", param_hint="--status")
    payload = client().list_collection_provenance(
        collection_id,
        page=page,
        per_page=per_page,
        q=query,
        status=cast(Any, status),
        sort=cast(Any, sort),
        order=cast(Any, normalized_order),
        all_items=all_items,
    )
    if selectors:
        emit(format_file_selectors(payload), json_mode=False)
        return
    emit(payload if json_mode else format_provenance_files(payload), json_mode=json_mode)


@collection_provenance_app.command("show")
def provenance_show_cmd(
    collection_id: Annotated[int, typer.Argument(help="Collection id")],
    path: Annotated[str, typer.Argument(help="Collection-relative file path")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show one file's current provenance binding and journal summary."""

    payload = client().get_collection_file_provenance(collection_id, path)
    emit(payload if json_mode else format_file_provenance(payload), json_mode=json_mode)


@collection_provenance_app.command("trace")
def provenance_trace_cmd(
    collection_id: Annotated[int, typer.Argument(help="Collection id")],
    path: Annotated[str, typer.Argument(help="Collection-relative file path")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Trace one file lineage through every reachable ancestor journal."""

    payload = client().trace_collection_file_provenance(collection_id, path)
    emit(payload if json_mode else format_provenance_trace(payload), json_mode=json_mode)


@collection_provenance_app.command("export")
def provenance_export_cmd(
    collection_id: Annotated[int, typer.Argument(help="Collection id")],
    journal_id: Annotated[str, typer.Argument(help="Exact provenance journal id")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Destination .json-seq file")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit a JSON export receipt")] = False,
) -> None:
    """Export one exact canonical RFC 7464 journal."""

    content = client().export_collection_provenance_journal(collection_id, journal_id)
    destination = output.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_bytes(content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    if json_mode:
        emit(
            {
                "collection_id": collection_id,
                "journal_id": journal_id,
                "output": str(destination),
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
            json_mode=True,
        )
        return
    typer.echo(str(destination))


@collection_provenance_app.command("verify")
def provenance_verify_cmd(
    collection_id: Annotated[int, typer.Argument(help="Collection id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Verify exact journals, bindings, identity, and database projection."""

    payload = client().verify_collection_provenance(collection_id)
    emit(payload if json_mode else format_provenance_verification(payload), json_mode=json_mode)


_ARCHIVE_STORE_SORT_FIELDS = {
    "store",
    "read_mode",
    "read_priority",
    "collections",
    "objects",
    "stored_bytes",
}

_RETRIEVAL_CACHE_SORT_FIELDS = {
    "collection_id",
    "source_store",
    "object_id",
    "stored_bytes",
    "cached_at",
    "verified_at",
    "protected_until",
}


@retrieval_cache_app.command("status")
def retrieval_cache_status_cmd(
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show retrieval-cache policy and current authorized totals."""

    payload = client().retrieval_cache_status()
    emit(payload if json_mode else format_retrieval_cache_status(payload), json_mode=json_mode)


@retrieval_cache_app.command("list")
def retrieval_cache_list_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "cached_at",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "desc",
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Match collection, store, or object identity"),
    ] = None,
    tag: Annotated[str | None, typer.Option("--tag", help="Require a collection tag")] = None,
    collection_id: Annotated[
        int | None,
        typer.Option("--collection", min=1, help="Require one collection"),
    ] = None,
    source_store: Annotated[
        str | None,
        typer.Option("--source-store", help="Require one archive source store"),
    ] = None,
    state: Annotated[
        str | None,
        typer.Option("--state", help="Require ready, delete_pending, or deleting state"),
    ] = None,
    protection: Annotated[
        str | None,
        typer.Option("--protection", help="Require protected or unleased objects"),
    ] = None,
    expires_before: Annotated[
        str | None,
        typer.Option("--expires-before", help="Require protection expiry at or before timestamp"),
    ] = None,
    expires_after: Annotated[
        str | None,
        typer.Option("--expires-after", help="Require protection expiry at or after timestamp"),
    ] = None,
    all_items: Annotated[
        bool,
        typer.Option("--all", help="Return every matching cache object"),
    ] = False,
    selectors: Annotated[
        bool,
        typer.Option(
            "--selectors",
            help="Emit one COLLECTION_ID::SOURCE_STORE::OBJECT_ID selector per line",
        ),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List cache availability visible through authorized collection access."""

    if selectors and json_mode:
        raise typer.BadParameter("--selectors and --json cannot be used together")
    normalized_order = _list_order(sort, order, fields=_RETRIEVAL_CACHE_SORT_FIELDS)
    payload = client().list_retrieval_cache_objects(
        page=page,
        per_page=per_page,
        q=query,
        tag=tag,
        collection_id=collection_id,
        source_store=source_store,
        state=cast(Any, state),
        protection=cast(Any, protection),
        expires_before=expires_before,
        expires_after=expires_after,
        sort=cast(Any, sort),
        order=cast(Any, normalized_order),
        all_items=all_items,
    )
    if selectors:
        emit(format_retrieval_cache_selectors(payload), json_mode=False)
        return
    emit(payload if json_mode else format_retrieval_cache_objects(payload), json_mode=json_mode)


@retrieval_cache_app.command("show")
def retrieval_cache_show_cmd(
    selector: Annotated[
        str,
        typer.Argument(help="COLLECTION_ID::SOURCE_STORE::OBJECT_ID"),
    ],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show one cached encrypted archive object and its lease protection."""

    collection_id, source_store, object_id = _retrieval_cache_selector(selector)
    payload = client().get_retrieval_cache_object(collection_id, source_store, object_id)
    emit(payload if json_mode else format_retrieval_cache_object(payload), json_mode=json_mode)


@archive_store_app.command("list")
def archive_store_list_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "store",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Substring match over store configuration"),
    ] = None,
    all_items: Annotated[
        bool,
        typer.Option("--all", help="Return every matching archive store"),
    ] = False,
    ids: Annotated[
        bool,
        typer.Option("--ids", help="Emit one archive store name per line"),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List configured archive stores and their current storage totals."""

    if ids and json_mode:
        raise typer.BadParameter("--ids and --json cannot be used together")
    normalized_order = _list_order(sort, order, fields=_ARCHIVE_STORE_SORT_FIELDS)
    payload = client().list_archive_stores(
        page=page,
        per_page=per_page,
        q=query,
        sort=cast(Any, sort),
        order=cast(Any, normalized_order),
        all_items=all_items,
    )
    if ids:
        emit(format_list_ids(payload, "stores", id_key="store"), json_mode=False)
        return
    emit(payload if json_mode else format_archive_stores(payload), json_mode=json_mode)


@archive_store_app.command("show")
def archive_store_show_cmd(
    store: Annotated[str, typer.Argument(help="Archive store name")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show one archive store and its current storage totals."""

    payload = client().get_archive_store(store)
    emit(payload if json_mode else format_archive_store(payload), json_mode=json_mode)


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


@archive_copy_app.command("start")
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


_ARCHIVE_COPY_SORT_FIELDS = {
    "collection_id",
    "source_store",
    "destination_store",
    "state",
    "requested_at",
}


@archive_copy_app.command("list")
def archive_copy_list_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "requested_at",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "desc",
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Substring match over archive-copy jobs"),
    ] = None,
    state: Annotated[
        str | None,
        typer.Option("--state", help="Exact archive-copy state"),
    ] = None,
    all_items: Annotated[
        bool,
        typer.Option("--all", help="Return every matching archive-copy job"),
    ] = False,
    selectors: Annotated[
        bool,
        typer.Option(
            "--selectors",
            help="Emit one COLLECTION_ID::DESTINATION_STORE selector per line",
        ),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List current and terminal archive-copy jobs."""

    if selectors and json_mode:
        raise typer.BadParameter("--selectors and --json cannot be used together")
    normalized_order = _list_order(sort, order, fields=_ARCHIVE_COPY_SORT_FIELDS)
    payload = client().list_archive_copy_jobs(
        page=page,
        per_page=per_page,
        q=query,
        state=cast(Any, state),
        sort=cast(Any, sort),
        order=cast(Any, normalized_order),
        all_items=all_items,
    )
    if selectors:
        emit(format_archive_copy_selectors(payload), json_mode=False)
        return
    emit(payload if json_mode else format_archive_copy_jobs(payload), json_mode=json_mode)


@archive_copy_app.command("show")
def archive_copy_show_cmd(
    selector: Annotated[
        str,
        typer.Argument(help="COLLECTION_ID::DESTINATION_STORE"),
    ],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show one archive-copy job."""

    collection_id, destination_store = _archive_copy_selector(selector)
    payload = client().get_archive_copy_job(
        collection_id,
        destination_store=destination_store,
    )
    emit(payload if json_mode else format_archive_copy_job(payload), json_mode=json_mode)


@archive_copy_app.command("cancel")
def archive_copy_cancel_cmd(
    selector: Annotated[
        str,
        typer.Argument(help="COLLECTION_ID::DESTINATION_STORE"),
    ],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Cancel one active archive-copy job."""

    collection_id, destination_store = _archive_copy_selector(selector)
    payload = client().cancel_archive_copy_job(
        collection_id,
        destination_store=destination_store,
    )
    emit(payload if json_mode else format_archive_copy_job(payload), json_mode=json_mode)


@archive_copy_app.command("watch")
def archive_copy_watch_cmd(
    selector: Annotated[
        str,
        typer.Argument(help="COLLECTION_ID::DESTINATION_STORE"),
    ],
    interval: Annotated[
        float,
        typer.Option("--interval", min=0.1, help="Polling interval in seconds"),
    ] = 1.0,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Wait for one archive-copy job to finish."""

    collection_id, destination_store = _archive_copy_selector(selector)
    api = client()
    while True:
        payload = api.get_archive_copy_job(
            collection_id,
            destination_store=destination_store,
        )
        state = str(payload.get("state", ""))
        if state in {"completed", "failed", "canceled"}:
            emit(payload if json_mode else format_archive_copy_job(payload), json_mode=json_mode)
            if state != "completed":
                raise typer.Exit(1)
            return
        time.sleep(interval)


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
        details = exc.details if isinstance(exc, RiverhogError) else None
        typer.echo(
            json.dumps(
                error_document(code=code, message=message, details=details),
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
