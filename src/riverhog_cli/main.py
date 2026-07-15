from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict, cast

import httpx
import typer

from riverhog_cli.client import ApiClient
from riverhog_cli.output import (
    emit,
    format_archive_copy_job,
    format_archive_copy_retirement_plan,
    format_archive_copy_retirement_result,
    format_collection_deletion_plan,
    format_collection_deletion_result,
    format_collection_summary,
    format_collection_upload,
    format_collection_upload_plan,
    format_collections,
    format_fetch,
    format_fetch_files,
    format_fetches,
    format_find,
    format_hot_evict,
)
from riverhog_cli.upload_progress import CollectionUploadProgress, make_collection_upload_progress
from riverhog_core.domain.errors import Conflict, NotFound, RiverhogError, ServiceUnavailable
from riverhog_core.fs_paths import (
    PathNormalizationError,
    collection_id_for_upload,
    normalize_upload_slug,
    normalize_upload_timestamp,
)

app = typer.Typer(help="Riverhog collection and hot-storage CLI.")
collection_app = typer.Typer(help="Collection catalog and upload operations.")
archive_app = typer.Typer(help="Archive-store operations.")
fetch_app = typer.Typer(help="Named whole-collection fetch operations.")
hot_app = typer.Typer(help="Hot-storage operations.")
app.add_typer(collection_app, name="collection")
app.add_typer(archive_app, name="archive")
app.add_typer(hot_app, name="hot")
hot_app.add_typer(fetch_app, name="fetch")

HASH_CHUNK_BYTES = 8 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
UPLOAD_FILE_CONCURRENCY = 1
UPLOAD_FILE_LOG_BYTES = 1 * 1024 * 1024
UPLOAD_PROGRESS_INTERVAL_SECONDS = 5.0
DOWNLOAD_PROGRESS_INTERVAL_SECONDS = 5.0
UPLOAD_FINALIZE_POLL_SECONDS = 5.0
UPLOAD_FINALIZE_STATUS_INTERVAL_SECONDS = 30.0
TRANSIENT_UPLOAD_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
UPLOAD_RESUME_RETRY_INITIAL_DELAY_SECONDS = 1.0
UPLOAD_RESUME_RETRY_MAX_DELAY_SECONDS = 10.0
UPLOAD_RESUME_RETRY_LOG_INTERVAL_SECONDS = 30.0
UPLOAD_LOG_LOCK = threading.Lock()
UploadWaitMode = Literal["staged", "finalized"]


class CollectionManifestEntry(TypedDict):
    path: str
    bytes: int
    sha256: str


class CollectionUploadFilePayload(CollectionManifestEntry, total=False):
    upload_state: str
    uploaded_bytes: int
    upload_state_expires_at: str | None


def client() -> ApiClient:
    return ApiClient()


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
    if value <= 0:
        raise typer.BadParameter("RIVERHOG_UPLOAD_FILE_CONCURRENCY must be a positive integer")
    return value


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


def _notify_upload_status(status: Callable[[str], None] | None, message: str) -> None:
    if status is None:
        _log_upload(message)
        return
    status(message)


def _download_progress_logger(
    estimated_total_bytes: int | None = None,
) -> Callable[[int, int | None], None]:
    started_at = time.monotonic()
    last_logged_at = started_at

    def progress(downloaded_bytes: int, total_bytes: int | None) -> None:
        nonlocal last_logged_at
        now = time.monotonic()
        if now - last_logged_at < DOWNLOAD_PROGRESS_INTERVAL_SECONDS:
            return
        elapsed = max(now - started_at, 0.001)
        rate = downloaded_bytes / elapsed
        display_total = total_bytes if total_bytes is not None else estimated_total_bytes
        if display_total is None or display_total <= 0:
            total_text = "unknown"
            percent_text = ""
        else:
            total_text = _format_bytes(display_total)
            percent_text = f" ({downloaded_bytes / display_total * 100.0:.1f}%)"
        _log_upload(
            "Download progress: "
            f"{_format_bytes(downloaded_bytes)} / {total_text}{percent_text} "
            f"at {_format_bytes(int(rate))}/s"
        )
        last_logged_at = now

    return progress


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
    slug: str,
    manifest: list[CollectionManifestEntry],
    *,
    ingest_source: str | None,
    upload_timestamp: str | None,
    retain_hot: bool,
    archive_store: str | None = None,
) -> dict[str, Any]:
    return _retry_transient_upload_operation(
        "Upload session create/resume",
        lambda: api.create_or_resume_collection_upload(
            slug,
            manifest,
            ingest_source=ingest_source,
            upload_timestamp=upload_timestamp,
            archive_store=archive_store,
            retain_hot=retain_hot,
        ),
    )


def _create_or_resume_collection_upload_session(
    api: ApiClient,
    slug: str,
    *,
    ingest_source: str | None,
    upload_timestamp: str | None,
    retain_hot: bool,
    archive_store: str | None = None,
) -> dict[str, Any]:
    return _retry_transient_upload_operation(
        "Upload session open/resume",
        lambda: api.create_or_resume_collection_upload_session(
            slug,
            ingest_source=ingest_source,
            upload_timestamp=upload_timestamp,
            archive_store=archive_store,
            retain_hot=retain_hot,
        ),
    )


def _register_collection_upload_session_file(
    api: ApiClient,
    collection_id: str,
    file_payload: CollectionManifestEntry,
) -> dict[str, Any]:
    return _retry_transient_upload_operation(
        f"Upload session register file {file_payload['path']}",
        lambda: api.register_collection_upload_session_file(collection_id, file_payload),
    )


def _complete_collection_upload_session(
    api: ApiClient,
    collection_id: str,
) -> dict[str, Any]:
    return _retry_transient_upload_operation(
        "Upload session complete",
        lambda: api.complete_collection_upload_session(collection_id),
    )


def _create_or_resume_collection_file_upload(
    api: ApiClient,
    collection_id: str,
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
    slug: str,
    root: Path,
    manifest: list[CollectionManifestEntry],
    upload_timestamp: str | None,
    wait_mode: UploadWaitMode,
    session_mode: bool,
    retain_hot: bool,
    archive_store: str | None = None,
) -> dict[str, object]:
    try:
        normalized_slug = normalize_upload_slug(slug)
        normalized_timestamp = (
            normalize_upload_timestamp(upload_timestamp) if upload_timestamp is not None else None
        )
    except PathNormalizationError as exc:
        raise typer.BadParameter(str(exc)) from exc
    collection_id = (
        collection_id_for_upload(normalized_slug, normalized_timestamp)
        if normalized_timestamp is not None
        else None
    )
    return {
        "dry_run": True,
        "status": "would_upload",
        "slug": slug,
        "normalized_slug": normalized_slug,
        "upload_timestamp": normalized_timestamp,
        "collection_id": collection_id,
        "root": str(root),
        "ingest_source": str(root),
        "files_total": len(manifest),
        "bytes_total": sum(item["bytes"] for item in manifest),
        "wait_mode": wait_mode,
        "session": session_mode,
        "archive_store": archive_store,
        "retain_hot": retain_hot,
        "server_validation": "not_run",
        "created_at": datetime.now(UTC).isoformat(),
        "files_preview": manifest[:5],
    }


def _iter_local_collection_paths(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _upload_collection_file(
    api: ApiClient,
    collection_id: str,
    source_path: Path,
    file_payload: Mapping[str, object],
    *,
    progress: Callable[[int], None] | None = None,
) -> None:
    path_value = str(file_payload["path"])
    length_value = file_payload["bytes"]
    if not isinstance(length_value, int):
        raise RuntimeError(f"upload length for {path_value} is not an integer")
    length = length_value
    session = _create_or_resume_collection_file_upload(api, collection_id, path_value)
    offset = int(session["offset"])
    if offset > length:
        raise RuntimeError(
            f"upload offset for {path_value} is {offset}, past expected length {length}"
        )
    log_file = length >= _upload_file_log_bytes()
    if offset >= length:
        if log_file:
            _log_upload(f"Already uploaded {path_value} ({_format_bytes(length)})")
        return

    if offset:
        _log_upload(f"Resuming {path_value} at {_format_bytes(offset)} of {_format_bytes(length)}")
    elif log_file:
        _log_upload(f"Uploading {path_value} ({_format_bytes(length)})")

    chunk_size = _upload_chunk_bytes()
    with source_path.open("rb") as handle:
        while offset < length:
            handle.seek(offset)
            chunk = handle.read(min(chunk_size, length - offset))
            if not chunk:
                break
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
                _log_upload(
                    f"Upload interrupted for {path_value} at {_format_bytes(offset)}; "
                    f"{_upload_error_description(exc)}; checking server offset"
                )
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
                    _log_upload(f"Server offset unchanged for {path_value}; retrying chunk")
                    continue
                if recovered_offset < offset:
                    raise RuntimeError(
                        f"server upload offset for {path_value} moved backward to "
                        f"{recovered_offset}; expected at least {offset}"
                    ) from exc
                if recovered_offset > length:
                    raise RuntimeError(
                        f"server upload offset for {path_value} is {recovered_offset}, "
                        f"past expected length {length}"
                    ) from exc
                if recovered_offset != offset + len(chunk):
                    raise RuntimeError(
                        f"server accepted a partial upload chunk for {path_value}: "
                        f"{recovered_offset} bytes, expected {offset} or "
                        f"{offset + len(chunk)}"
                    ) from exc
                _log_upload(
                    f"Server accepted chunk for {path_value} before the response was lost; "
                    f"continuing at {_format_bytes(recovered_offset)}"
                )
                if progress is not None:
                    progress(recovered_offset - offset)
                offset = recovered_offset
                continue

            next_offset = int(upload_result["offset"])
            if next_offset != offset + len(chunk):
                raise RuntimeError(f"upload offset advanced unexpectedly for {path_value}")
            if progress is not None:
                progress(len(chunk))
            offset = next_offset
    if offset != length:
        raise RuntimeError(f"upload for {path_value} stopped at {offset} of {length} bytes")
    if log_file:
        _log_upload(f"Uploaded {path_value} ({_format_bytes(length)})")


def _upload_collection_files(
    api: ApiClient,
    collection_id: str,
    resolved_root: Path,
    upload_files: list[CollectionUploadFilePayload],
    *,
    progress: Callable[[int], None],
    file_complete: Callable[[], None] | None = None,
    file_concurrency: int,
    api_factory: Callable[[], ApiClient] = client,
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

    def upload_worker() -> None:
        worker_api = api_factory()
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
    collection_id: str,
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
        "retain_hot": (_optional_int(collection.get("hot_files")) or 0) == files_total,
        "state": "finalized",
        "files_total": files_total,
        "files_pending": 0,
        "files_partial": 0,
        "files_uploaded": files_total,
        "hot_materialized_files": _optional_int(collection.get("hot_files")) or 0,
        "bytes_total": bytes_total,
        "uploaded_bytes": bytes_total,
        "hot_materialized_bytes": _optional_int(collection.get("hot_bytes")) or 0,
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


def _wait_for_finalized_collection(
    api: ApiClient,
    collection_id: str,
    manifest: list[CollectionManifestEntry] | None,
    *,
    status: Callable[[str], None] | None = None,
) -> tuple[dict[str, object], str]:
    poll_seconds = _upload_finalize_poll_seconds()
    timeout_seconds = _upload_finalize_timeout_seconds()
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    last_status_log_at = 0.0
    last_payload: dict[str, object] | None = None

    _notify_upload_status(
        status,
        "All files uploaded; waiting for archive verification",
    )
    while True:
        now = time.monotonic()
        transient_error: BaseException | None = None
        try:
            collection = api.get_collection(collection_id)
            return (
                _finalized_collection_upload_payload(collection_id, manifest, collection),
                "finalized",
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
                _notify_upload_status(
                    status,
                    "Waiting for collection finalization: "
                    f"{_upload_error_description(transient_error)} while polling; retrying",
                )
                last_status_log_at = now
        elif last_payload is not None:
            state = str(last_payload.get("state", "unknown"))
            if state == "failed":
                return last_payload, "failed"
            if now - last_status_log_at >= UPLOAD_FINALIZE_STATUS_INTERVAL_SECONDS:
                archive_status = _archive_wait_status(last_payload)
                _notify_upload_status(
                    status,
                    "Waiting for collection finalization: "
                    f"state={state}, "
                    f"{last_payload.get('files_uploaded', 0)}/"
                    f"{last_payload.get('files_total', 0)} files, "
                    f"{last_payload.get('uploaded_bytes', 0)}/"
                    f"{last_payload.get('bytes_total', 0)} bytes staged"
                    f"{archive_status}",
                )
                last_status_log_at = now
        elif now - last_status_log_at >= UPLOAD_FINALIZE_STATUS_INTERVAL_SECONDS:
            _notify_upload_status(
                status,
                "Waiting for collection finalization: upload session not visible yet",
            )
            last_status_log_at = now

        if deadline is not None and now >= deadline:
            if last_payload is not None:
                return last_payload, "timeout"
            return (
                {
                    "collection_id": collection_id,
                    "state": "archiving",
                    "files": [],
                    "files_total": 0,
                    "files_uploaded": 0,
                    "bytes_total": 0,
                    "uploaded_bytes": 0,
                    "upload_state_expires_at": None,
                },
                "timeout",
            )
        sleep_seconds = poll_seconds
        if deadline is not None:
            sleep_seconds = max(0.0, min(poll_seconds, deadline - now))
        time.sleep(sleep_seconds)


def _staged_collection_upload_payload(
    api: ApiClient,
    collection_id: str,
    manifest: list[CollectionManifestEntry],
) -> dict[str, object]:
    try:
        return api.get_collection_upload(collection_id)
    except NotFound:
        collection = api.get_collection(collection_id)
        return _finalized_collection_upload_payload(collection_id, manifest, collection)
    except Exception as exc:
        if not _is_transient_upload_error(exc):
            raise
        bytes_total = sum(item["bytes"] for item in manifest)
        return {
            "collection_id": collection_id,
            "state": "archiving",
            "files_total": len(manifest),
            "files_pending": 0,
            "files_partial": 0,
            "files_uploaded": len(manifest),
            "hot_materialized_files": 0,
            "bytes_total": bytes_total,
            "uploaded_bytes": bytes_total,
            "hot_materialized_bytes": 0,
            "missing_bytes": 0,
            "upload_state_expires_at": None,
            "files": [
                {
                    "path": item["path"],
                    "bytes": item["bytes"],
                    "sha256": item["sha256"],
                    "upload_state": "uploaded",
                    "uploaded_bytes": item["bytes"],
                    "upload_state_expires_at": None,
                }
                for item in manifest
            ],
            "collection": None,
        }


def _upload_collection_via_session(
    api: ApiClient,
    slug: str,
    resolved_root: Path,
    *,
    ingest_source: str | None,
    upload_timestamp: str | None,
    wait_mode: UploadWaitMode,
    retain_hot: bool,
    archive_store: str | None = None,
    json_mode: bool = False,
) -> dict[str, object]:
    local_path_iter = _iter_local_collection_paths(resolved_root)
    try:
        first_source_path = next(local_path_iter)
    except StopIteration as exc:
        raise typer.BadParameter("collection source must contain at least one file") from exc

    _log_upload(f"Opening incremental upload session for {resolved_root}")
    session_payload = _create_or_resume_collection_upload_session(
        api,
        slug,
        ingest_source=ingest_source,
        upload_timestamp=upload_timestamp,
        archive_store=archive_store,
        retain_hot=retain_hot,
    )
    collection_id = str(session_payload["collection_id"])
    _log_upload(f"Upload session {collection_id}: registering files incrementally")

    manifest: list[CollectionManifestEntry] = []
    total_discovered_bytes = 0
    chunk_bytes = _upload_chunk_bytes()

    upload_progress = make_collection_upload_progress(
        collection_id=collection_id,
        files_total=0,
        bytes_total=0,
        file_concurrency=1,
        chunk_bytes=chunk_bytes,
        json_mode=json_mode,
        interval_seconds=UPLOAD_PROGRESS_INTERVAL_SECONDS,
    )

    def upload_one(source_path: Path, progress: CollectionUploadProgress) -> None:
        nonlocal total_discovered_bytes

        rel_path = source_path.relative_to(resolved_root).as_posix()
        stat = source_path.stat()
        if stat.st_size >= _upload_file_log_bytes():
            _log_upload(f"Hashing {rel_path} ({_format_bytes(stat.st_size)})")
        entry: CollectionManifestEntry = {
            "path": rel_path,
            "bytes": stat.st_size,
            "sha256": _file_sha256(source_path),
        }
        manifest.append(entry)
        total_discovered_bytes += stat.st_size
        progress.set_totals(files_total=len(manifest), bytes_total=total_discovered_bytes)

        registered_payload = _register_collection_upload_session_file(
            api,
            collection_id,
            entry,
        )
        file_payload = next(
            (
                current
                for current in _response_upload_files(registered_payload)
                if isinstance(current, dict) and current.get("path") == rel_path
            ),
            CollectionUploadFilePayload(
                path=rel_path,
                bytes=stat.st_size,
                sha256=entry["sha256"],
                upload_state="pending",
                uploaded_bytes=0,
            ),
        )
        _upload_collection_file(
            api,
            collection_id,
            source_path,
            file_payload,
            progress=progress.uploaded,
        )
        progress.complete_file()

    with upload_progress:
        upload_one(first_source_path, upload_progress)
        for source_path in local_path_iter:
            upload_one(source_path, upload_progress)

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
    if phase == "packaging":
        status += ", building archive package"
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
    hot_materialized_bytes = payload.get("hot_materialized_bytes")
    bytes_total = payload.get("bytes_total")
    if (
        phase == "materializing_hot"
        and isinstance(hot_materialized_bytes, int)
        and isinstance(bytes_total, int)
        and bytes_total > 0
    ):
        percent = hot_materialized_bytes / bytes_total * 100.0
        status += (
            f", hot={_format_bytes(hot_materialized_bytes)} / {_format_bytes(bytes_total)} "
            f"({percent:.1f}%)"
        )
    hot_materialized_files = payload.get("hot_materialized_files")
    files_total = payload.get("files_total")
    if (
        phase == "materializing_hot"
        and isinstance(hot_materialized_files, int)
        and isinstance(files_total, int)
    ):
        status += f", hot_files={hot_materialized_files}/{files_total}"
    latest_failure = payload.get("latest_failure")
    if latest_failure:
        status += f", latest_failure={latest_failure}"
    return status


_COLLECTION_SORT_FIELDS = {
    "id",
    "bytes",
    "files",
    "hot_bytes",
}
_FIND_SORT_FIELDS = {
    "logical_path",
    "collection_id",
    "collection_path",
    "bytes",
    "hot",
}
_FETCH_FILE_SORT_FIELDS = _FIND_SORT_FIELDS


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
            "files",
            "bytes",
            "hot_bytes",
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
        typer.Option("--query", "--search", help="Substring match over collection ids"),
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
    """List collections with archive and hot-storage summaries."""

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
        collections = payload.get("collections")
        values = collections if isinstance(collections, list) else []
        emit(
            "\n".join(
                str(collection.get("id"))
                for collection in values
                if isinstance(collection, Mapping) and collection.get("id")
            ),
            json_mode=False,
        )
        return
    emit(
        _compact_collection_page(payload) if json_mode else format_collections(payload),
        json_mode=json_mode,
    )


@collection_app.command("upload")
def upload_cmd(
    slug: Annotated[str, typer.Argument(help="Human-readable collection slug")],
    root: Annotated[Path, typer.Argument(help="Local collection root directory")],
    upload_timestamp: Annotated[
        str | None,
        typer.Option(
            "--timestamp",
            help="Use UTC upload timestamp YYYYMMDDTHHMMSSZ in the collection id",
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
    archive_only: Annotated[
        bool,
        typer.Option("--archive-only", help="Skip hot storage after archival"),
    ] = False,
) -> None:
    """Upload a local directory as a collection."""

    wait_mode = _normalize_upload_wait_mode(wait)
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
            slug=slug,
            root=resolved_root,
            manifest=manifest,
            upload_timestamp=upload_timestamp,
            wait_mode=wait_mode,
            session_mode=session_mode,
            archive_store=archive_store,
            retain_hot=not archive_only,
        )
        emit(payload if json_mode else format_collection_upload_plan(payload), json_mode=json_mode)
        return

    api = client()
    if session_mode:
        payload = _upload_collection_via_session(
            api,
            slug,
            resolved_root,
            ingest_source=str(resolved_root),
            upload_timestamp=upload_timestamp,
            archive_store=archive_store,
            wait_mode=wait_mode,
            retain_hot=not archive_only,
            json_mode=json_mode,
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
        slug,
        manifest,
        ingest_source=str(resolved_root),
        upload_timestamp=upload_timestamp,
        archive_store=archive_store,
        retain_hot=not archive_only,
    )
    collection_id = str(payload["collection_id"])
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
    file_concurrency = _upload_file_concurrency()
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
            upload_progress.notice(
                "All files uploaded; collection finalization will continue in the background",
                phase="finalizing",
            )
            final_payload = _staged_collection_upload_payload(api, collection_id, manifest)
            completion_state = "staged"
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
    collection_id: Annotated[str, typer.Argument(help="Open collection upload session id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Cancel an open collection upload session."""

    payload = client().cancel_collection_upload_session(collection_id)
    emit(payload if json_mode else format_collection_upload(payload), json_mode=json_mode)


@collection_app.command("watch")
def upload_watch_cmd(
    collection_id: Annotated[
        str,
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
        typer.Argument(help="Optional substring matched against logical file paths"),
    ] = None,
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "logical_path",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    collection: Annotated[
        str | None,
        typer.Option("--collection", help="Restrict results to one collection"),
    ] = None,
    hot: Annotated[
        bool | None,
        typer.Option("--hot/--not-hot", help="Filter by hot-storage availability"),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Search logical files across collections."""

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
        hot=hot,
    )
    emit(payload if json_mode else format_find(payload), json_mode=json_mode)


@collection_app.command("show")
def show_cmd(
    collection: Annotated[str, typer.Argument(help="Collection id")],
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
    collection_id: Annotated[str, typer.Argument(help="Exact accepted collection id")],
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
    typed_id = typer.prompt("Type the complete collection id to delete")
    if typed_id != collection_id:
        typer.echo("Collection id did not match; nothing was deleted.", err=True)
        raise typer.Exit(1)
    payload = api.delete_collection(collection_id, challenge=challenge)
    emit(format_collection_deletion_result(payload), json_mode=False)


@archive_app.command("copy")
def archive_copy_cmd(
    collection_id: Annotated[str, typer.Argument(help="Exact collection id")],
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
    """Copy one collection between archive stores without using hot storage."""

    payload = client().create_or_resume_archive_copy(
        collection_id,
        destination_store=destination_store,
        source_store=source_store,
    )
    emit(payload if json_mode else format_archive_copy_job(payload), json_mode=json_mode)


@archive_app.command("retire")
def archive_retire_cmd(
    collection_id: Annotated[str, typer.Argument(help="Exact collection id")],
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
    typed_id = typer.prompt("Type the complete collection id to retire from this store")
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


@fetch_app.command("create")
def fetch_create_cmd(
    name: Annotated[str, typer.Option("--name", "-n", help="Human-readable fetch purpose")],
    collections: Annotated[
        list[str] | None,
        typer.Argument(help="Optional collection ids to add immediately"),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Create a named editable fetch."""

    api = client()
    payload = api.create_fetch(name=name, collections=collections or [])
    if json_mode:
        emit(payload, json_mode=True)
        return
    status = api.get_fetch_status(str(payload.get("id", "unknown")))
    emit(format_fetch(status), json_mode=False)


@hot_app.command("evict")
def hot_evict_cmd(
    collections: Annotated[list[str], typer.Argument(help="Collection ids to evict")],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview complete collections without evicting them"),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Evict complete collections backed by verified remote archives."""

    payload = client().evict_hot_collections(collections, dry_run=dry_run)
    emit(payload if json_mode else format_hot_evict(payload), json_mode=json_mode)


@fetch_app.command("add")
def fetch_add_cmd(
    fetch_id: Annotated[str, typer.Argument(help="Fetch id")],
    collections: Annotated[list[str], typer.Argument(help="Collection ids to add")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Add collections to an editable fetch."""

    api = client()
    payload = api.add_fetch_collections(fetch_id, collections)
    if json_mode:
        emit(payload, json_mode=True)
        return
    status = api.get_fetch_status(fetch_id)
    emit(format_fetch(status), json_mode=False)


@fetch_app.command("remove")
def fetch_remove_cmd(
    fetch_id: Annotated[str, typer.Argument(help="Fetch id")],
    collections: Annotated[list[str], typer.Argument(help="Collection ids to remove")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Remove collections from an editable fetch."""

    api = client()
    payload = api.remove_fetch_collections(fetch_id, collections)
    if json_mode:
        emit(payload, json_mode=True)
        return
    status = api.get_fetch_status(fetch_id)
    emit(format_fetch(status), json_mode=False)


@fetch_app.command("list")
def fetches_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    state: Annotated[str | None, typer.Option("--state", help="Filter by fetch state")] = None,
    query: Annotated[
        str | None, typer.Option("--query", "-q", help="Search id, name, or collections")
    ] = None,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "order",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    all_items: Annotated[
        bool,
        typer.Option("--all", help="Return every matching fetch"),
    ] = False,
    ids: Annotated[
        bool,
        typer.Option("--ids", help="Emit one fetch id per line"),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List named fetches."""

    if ids and json_mode:
        raise typer.BadParameter("--ids and --json cannot be used together")
    payload = client().list_fetches(
        page=page,
        per_page=per_page,
        state=state,
        query=query,
        sort=sort,
        order=order.casefold(),
        all_items=all_items,
    )
    if ids:
        fetches = payload.get("fetches")
        values = fetches if isinstance(fetches, list) else []
        emit(
            "\n".join(
                str(fetch.get("id"))
                for fetch in values
                if isinstance(fetch, Mapping) and fetch.get("id")
            ),
            json_mode=False,
        )
        return
    emit(payload if json_mode else format_fetches(payload), json_mode=json_mode)


@fetch_app.command("show")
def fetch_cmd(
    fetch_id: Annotated[str, typer.Argument(help="Fetch id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show fetch preflight and progress summary."""

    api = client()
    status = api.get_fetch_status(fetch_id)
    if json_mode:
        emit(status, json_mode=True)
        return
    emit(format_fetch(status), json_mode=False)


@fetch_app.command("files")
def fetch_files_cmd(
    fetch_id: Annotated[str, typer.Argument(help="Fetch id")],
    query: Annotated[
        str | None, typer.Option("--query", "-q", help="Search logical file paths")
    ] = None,
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "logical_path",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    hot: Annotated[
        bool | None,
        typer.Option("--hot/--not-hot", help="Filter by hot-storage availability"),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List files in a fetch's collections."""

    normalized_sort = sort.casefold()
    if normalized_sort not in _FETCH_FILE_SORT_FIELDS:
        raise typer.BadParameter(
            f"sort must be one of {', '.join(sorted(_FETCH_FILE_SORT_FIELDS))}",
            param_hint="--sort",
        )
    normalized_order = order.casefold()
    if normalized_order not in {"asc", "desc"}:
        raise typer.BadParameter("order must be asc or desc", param_hint="--order")
    payload = client().list_fetch_files(
        fetch_id,
        page=page,
        per_page=per_page,
        sort=normalized_sort,
        order=normalized_order,
        query=query,
        hot=hot,
    )
    emit(payload if json_mode else format_fetch_files(payload), json_mode=json_mode)


@fetch_app.command("start")
def fetch_start_cmd(
    fetch_id: Annotated[str, typer.Argument(help="Fetch id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Start a fetch, restoring complete collections when needed."""

    api = client()
    payload = api.start_fetch(fetch_id)
    if json_mode:
        emit(payload, json_mode=True)
        return
    status = api.get_fetch_status(fetch_id)
    emit(format_fetch(status), json_mode=False)


@fetch_app.command("cancel")
def fetch_cancel_cmd(
    fetch_id: Annotated[str, typer.Argument(help="Fetch id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Cancel an active fetch and return it to draft."""

    payload = client().cancel_fetch(fetch_id)
    emit(
        payload if json_mode else format_fetch(payload),
        json_mode=json_mode,
    )


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, httpx.TransportError):
        return "transport_error"
    if isinstance(exc, RiverhogError):
        return exc.code
    return "error"


def _json_requested(argv: list[str]) -> bool:
    return "--json" in argv


def _emit_cli_error(exc: BaseException, *, json_mode: bool) -> None:
    code = _error_code(exc)
    message = str(exc) or type(exc).__name__
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


def main() -> None:
    try:
        app()
    except (
        httpx.TransportError,
        RiverhogError,
        FileExistsError,
        FileNotFoundError,
        NotADirectoryError,
    ) as exc:
        _emit_cli_error(exc, json_mode=_json_requested(sys.argv[1:]))
        raise typer.Exit(1) from None


if __name__ == "__main__":
    main()
