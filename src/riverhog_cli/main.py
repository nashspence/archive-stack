from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict, cast

import httpx
import typer

from riverhog_cli.client import ApiClient
from riverhog_cli.output import (
    emit,
    format_collection_summary,
    format_collection_upload,
    format_collections,
    format_fetch,
    format_find,
    format_hot_pins,
    format_pin,
    format_release,
)
from riverhog_core.domain.errors import Conflict, NotFound, RiverhogError, ServiceUnavailable

app = typer.Typer(help="riverhog collection and hot-storage CLI")
collection_app = typer.Typer(help="collection catalog and upload operations")
hot_app = typer.Typer(help="pinned hot-storage set operations")
app.add_typer(collection_app, name="collection")
app.add_typer(hot_app, name="hot")

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
) -> dict[str, Any]:
    return _retry_transient_upload_operation(
        "Upload session create/resume",
        lambda: api.create_or_resume_collection_upload(
            slug,
            manifest,
            ingest_source=ingest_source,
            upload_timestamp=upload_timestamp,
        ),
    )


def _create_or_resume_collection_upload_session(
    api: ApiClient,
    slug: str,
    *,
    ingest_source: str | None,
    upload_timestamp: str | None,
) -> dict[str, Any]:
    return _retry_transient_upload_operation(
        "Upload session open/resume",
        lambda: api.create_or_resume_collection_upload_session(
            slug,
            ingest_source=ingest_source,
            upload_timestamp=upload_timestamp,
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
    glacier = collection.get("glacier")
    archived_bytes = 0
    if isinstance(glacier, dict):
        archived_bytes = int(glacier.get("stored_bytes") or 0)
    return {
        "collection_id": collection_id,
        "ingest_source": collection.get("ingest_source"),
        "state": "finalized",
        "files_total": files_total,
        "files_pending": 0,
        "files_partial": 0,
        "files_uploaded": files_total,
        "hot_promoted_files": files_total,
        "bytes_total": bytes_total,
        "uploaded_bytes": bytes_total,
        "hot_promoted_bytes": bytes_total,
        "missing_bytes": 0,
        "upload_state_expires_at": None,
        "latest_failure": None,
        "archive_phase": "completed",
        "archive_uploaded_bytes": archived_bytes or bytes_total,
        "archive_total_bytes": archived_bytes or bytes_total,
        "archive_uploaded_parts": None,
        "archive_total_parts": None,
        "files": files,
        "collection": collection,
    }


def _wait_for_finalized_collection(
    api: ApiClient,
    collection_id: str,
    manifest: list[CollectionManifestEntry] | None,
) -> tuple[dict[str, object], str]:
    poll_seconds = _upload_finalize_poll_seconds()
    timeout_seconds = _upload_finalize_timeout_seconds()
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    last_status_log_at = 0.0
    last_payload: dict[str, object] | None = None

    _log_upload("All files uploaded; waiting for Glacier archive verification")
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
                _log_upload(
                    "Waiting for collection finalization: "
                    f"{_upload_error_description(transient_error)} while polling; retrying"
                )
                last_status_log_at = now
        elif last_payload is not None:
            state = str(last_payload.get("state", "unknown"))
            if state == "failed":
                return last_payload, "failed"
            if now - last_status_log_at >= UPLOAD_FINALIZE_STATUS_INTERVAL_SECONDS:
                archive_status = _archive_wait_status(last_payload)
                _log_upload(
                    "Waiting for collection finalization: "
                    f"state={state}, "
                    f"{last_payload.get('files_uploaded', 0)}/"
                    f"{last_payload.get('files_total', 0)} files, "
                    f"{last_payload.get('uploaded_bytes', 0)}/"
                    f"{last_payload.get('bytes_total', 0)} bytes staged"
                    f"{archive_status}"
                )
                last_status_log_at = now
        elif now - last_status_log_at >= UPLOAD_FINALIZE_STATUS_INTERVAL_SECONDS:
            _log_upload("Waiting for collection finalization: upload session not visible yet")
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
            "hot_promoted_files": 0,
            "bytes_total": bytes_total,
            "uploaded_bytes": bytes_total,
            "hot_promoted_bytes": 0,
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
    )
    collection_id = str(session_payload["collection_id"])
    _log_upload(f"Upload session {collection_id}: registering files incrementally")

    manifest: list[CollectionManifestEntry] = []
    uploaded_bytes_this_run = 0
    total_discovered_bytes = 0
    last_progress_log_at = time.monotonic()

    def note_uploaded(delta: int) -> None:
        nonlocal uploaded_bytes_this_run, last_progress_log_at
        uploaded_bytes_this_run += delta
        now = time.monotonic()
        if now - last_progress_log_at < UPLOAD_PROGRESS_INTERVAL_SECONDS:
            return
        _log_upload(
            "Upload progress: "
            f"{_format_bytes(uploaded_bytes_this_run)} uploaded this run; "
            f"{_format_bytes(total_discovered_bytes)} discovered so far"
        )
        last_progress_log_at = now

    def upload_one(source_path: Path) -> None:
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
            progress=note_uploaded,
        )

    upload_one(first_source_path)
    for source_path in local_path_iter:
        upload_one(source_path)

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
    _log_upload("All files uploaded; collection finalization will continue in the background")
    if wait_mode == "finalized":
        final_payload, completion_state = _wait_for_finalized_collection(
            api,
            collection_id,
            manifest,
        )
        if completion_state == "timeout":
            raise typer.Exit(124)
        if completion_state == "failed":
            raise typer.Exit(1)
        return final_payload
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
    hot_promoted_bytes = payload.get("hot_promoted_bytes")
    bytes_total = payload.get("bytes_total")
    if (
        phase == "promoting"
        and isinstance(hot_promoted_bytes, int)
        and isinstance(bytes_total, int)
        and bytes_total > 0
    ):
        percent = hot_promoted_bytes / bytes_total * 100.0
        status += (
            f", hot={_format_bytes(hot_promoted_bytes)} / {_format_bytes(bytes_total)} "
            f"({percent:.1f}%)"
        )
    hot_promoted_files = payload.get("hot_promoted_files")
    files_total = payload.get("files_total")
    if (
        phase == "promoting"
        and isinstance(hot_promoted_files, int)
        and isinstance(files_total, int)
    ):
        status += f", hot_files={hot_promoted_files}/{files_total}"
    latest_failure = payload.get("latest_failure")
    if latest_failure:
        status += f", latest_failure={latest_failure}"
    return status


_COLLECTION_SORT_FIELDS = {
    "id",
    "bytes",
    "files",
    "hot_bytes",
    "archived_bytes",
    "pending_bytes",
    "protected_bytes",
}
_FIND_SORT_FIELDS = {"target", "collection", "path", "bytes", "hot", "archived"}


def _collection_sort_value(collection: Mapping[str, object], sort: str) -> int | str:
    if sort == "id":
        return str(collection.get("id", "")).casefold()
    return _optional_int(collection.get(sort)) or 0


def _collection_list_glacier_payload(collection: Mapping[str, object]) -> dict[str, object] | None:
    glacier = collection.get("glacier")
    if not isinstance(glacier, Mapping):
        return None
    return {
        key: glacier[key]
        for key in (
            "state",
            "storage_class",
            "stored_bytes",
            "last_uploaded_at",
            "last_verified_at",
            "failure",
        )
        if key in glacier
    }


def _collection_list_disc_payload(collection: Mapping[str, object]) -> dict[str, object] | None:
    disc_coverage = collection.get("disc_coverage")
    if not isinstance(disc_coverage, Mapping):
        return None
    return {
        key: disc_coverage[key]
        for key in ("state", "covered_bytes", "verified_physical_bytes")
        if key in disc_coverage
    }


def _collection_list_item_payload(collection: Mapping[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {
        key: collection[key]
        for key in (
            "id",
            "files",
            "bytes",
            "hot_bytes",
            "archived_bytes",
            "pending_bytes",
            "protected_bytes",
            "protection_state",
            "archive_format",
            "compression",
        )
        if key in collection
    }
    glacier = _collection_list_glacier_payload(collection)
    if glacier is not None:
        payload["glacier"] = glacier
    disc_coverage = _collection_list_disc_payload(collection)
    if disc_coverage is not None:
        payload["disc_coverage"] = disc_coverage
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
            "protection_state",
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
    protection_state: str | None,
    sort: str,
    order: str,
) -> dict[str, Any]:
    if sort not in _COLLECTION_SORT_FIELDS:
        raise typer.BadParameter(
            f"sort must be one of {', '.join(sorted(_COLLECTION_SORT_FIELDS))}",
            param_hint="--sort",
        )
    normalized_order = order.casefold()
    if normalized_order not in {"asc", "desc"}:
        raise typer.BadParameter("order must be asc or desc", param_hint="--order")

    first_page = api.list_collections(
        page=1,
        per_page=100,
        q=query,
        protection_state=protection_state,
    )
    collections = [
        collection
        for collection in first_page.get("collections", [])
        if isinstance(collection, dict)
    ]
    pages = _optional_int(first_page.get("pages")) or 0
    for page_number in range(2, pages + 1):
        next_page = api.list_collections(
            page=page_number,
            per_page=100,
            q=query,
            protection_state=protection_state,
        )
        collections.extend(
            collection
            for collection in next_page.get("collections", [])
            if isinstance(collection, dict)
        )

    collections.sort(
        key=lambda collection: _collection_sort_value(collection, sort),
        reverse=normalized_order == "desc",
    )
    total = len(collections)
    display_pages = (total + per_page - 1) // per_page if total else 0
    start = (page - 1) * per_page
    stop = start + per_page
    return {
        **first_page,
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": display_pages,
        "sort": sort,
        "order": normalized_order,
        "query": query,
        "protection_state": protection_state,
        "collections": collections[start:stop],
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
    protection_state: Annotated[
        str | None,
        typer.Option(
            "--protection",
            help="Filter by under_protected, cloud_only, physical_only, or fully_protected",
        ),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = _sorted_collection_page(
        client(),
        page=page,
        per_page=per_page,
        query=query,
        protection_state=protection_state,
        sort=sort,
        order=order,
    )
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
) -> None:
    wait_mode = _normalize_upload_wait_mode(wait)
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise typer.BadParameter("collection source must be a directory")

    api = client()
    if session_mode:
        payload = _upload_collection_via_session(
            api,
            slug,
            resolved_root,
            ingest_source=str(resolved_root),
            upload_timestamp=upload_timestamp,
            wait_mode=wait_mode,
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
    last_progress_log_at = time.monotonic()
    progress_lock = threading.Lock()

    def note_uploaded(delta: int) -> None:
        nonlocal uploaded_bytes, last_progress_log_at
        with progress_lock:
            uploaded_bytes += delta
            now = time.monotonic()
            if now - last_progress_log_at < UPLOAD_PROGRESS_INTERVAL_SECONDS:
                return
            percent = (uploaded_bytes / manifest_bytes * 100.0) if manifest_bytes else 100.0
            _log_upload(
                "Upload progress: "
                f"{_format_bytes(uploaded_bytes)} / {_format_bytes(manifest_bytes)} "
                f"({percent:.1f}%)"
            )
            last_progress_log_at = now

    _upload_collection_files(
        api,
        collection_id,
        resolved_root,
        upload_files,
        progress=note_uploaded,
        file_concurrency=_upload_file_concurrency(),
    )

    if wait_mode == "finalized":
        final_payload, completion_state = _wait_for_finalized_collection(
            api,
            collection_id,
            manifest,
        )
    else:
        _log_upload("All files uploaded; collection finalization will continue in the background")
        final_payload = _staged_collection_upload_payload(api, collection_id, manifest)
        completion_state = "staged"
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
        typer.Argument(help="Optional substring matched against projected file targets"),
    ] = None,
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "target",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    collection: Annotated[
        str | None,
        typer.Option("--collection", help="Restrict results to one collection"),
    ] = None,
    hot: Annotated[
        bool | None,
        typer.Option("--hot/--not-hot", help="Filter by hot-storage availability"),
    ] = None,
    archived: Annotated[
        bool | None,
        typer.Option("--archived/--not-archived", help="Filter by deep-archive coverage"),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
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
        archived=archived,
    )
    emit(payload if json_mode else format_find(payload), json_mode=json_mode)


@collection_app.command("show")
def show_cmd(
    collection: Annotated[str, typer.Argument(help="Collection id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    api = client()
    if json_mode:
        payload = api.get_collection(collection)
        emit(payload, json_mode=True)
        return
    payload = api.get_collection(collection, coverage_path_limit=4)
    glacier_payload = api.get_glacier_report(collection=collection)
    emit(format_collection_summary(payload, glacier_payload), json_mode=False)


@hot_app.command("pin")
def pin_cmd(
    target: Annotated[str, typer.Argument(help="Target selector")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().pin(target)
    emit(payload if json_mode else format_pin(payload), json_mode=json_mode)


@hot_app.command("unpin")
def release_cmd(
    target: Annotated[str, typer.Argument(help="Target selector")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().release(target)
    emit(payload if json_mode else format_release(payload), json_mode=json_mode)


@hot_app.command("list")
def pins_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().list_pins(page=page, per_page=per_page)
    emit(payload if json_mode else format_hot_pins(payload), json_mode=json_mode)


@hot_app.command("show")
def fetch_cmd(
    fetch_id: Annotated[str, typer.Argument(help="Fetch id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    api = client()
    if json_mode:
        summary = api.get_fetch(fetch_id)
        emit(summary, json_mode=True)
        return
    status = api.get_fetch_status(fetch_id)
    emit(format_fetch(status, {"entries": status.get("entries", [])}), json_mode=False)


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
    except (httpx.TransportError, RiverhogError) as exc:
        _emit_cli_error(exc, json_mode=_json_requested(sys.argv[1:]))
        raise typer.Exit(1) from None


if __name__ == "__main__":
    main()
