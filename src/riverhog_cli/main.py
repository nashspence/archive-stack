from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

import httpx
import typer

from riverhog_cli.client import ApiClient
from riverhog_cli.output import (
    emit,
    format_archive_status,
    format_collection_files,
    format_collection_summary,
    format_collection_upload,
    format_copies,
    format_copy,
    format_fetch,
    format_files,
    format_glacier_report,
    format_pin,
    format_plan,
)
from riverhog_core.domain.errors import NotFound, ServiceUnavailable

app = typer.Typer(help="riverhog archival control CLI")
iso_app = typer.Typer(help="ISO operations")
copy_app = typer.Typer(help="copy registration")
app.add_typer(iso_app, name="iso")
app.add_typer(copy_app, name="copy")

PLAN_QUERY_HELP = (
    "Substring match over candidate id, collection ids, and represented projected file paths"
)
IMAGE_QUERY_HELP = "Substring match over id, filename, and collection ids"
HASH_CHUNK_BYTES = 8 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
UPLOAD_FILE_CONCURRENCY = 1
UPLOAD_FILE_LOG_BYTES = 1 * 1024 * 1024
UPLOAD_PROGRESS_INTERVAL_SECONDS = 5.0
DOWNLOAD_PROGRESS_INTERVAL_SECONDS = 5.0
UPLOAD_FINALIZE_POLL_SECONDS = 5.0
UPLOAD_FINALIZE_STATUS_INTERVAL_SECONDS = 30.0
TRANSIENT_UPLOAD_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
UPLOAD_RESUME_RETRY_ATTEMPTS = 5
UPLOAD_RESUME_RETRY_INITIAL_DELAY_SECONDS = 1.0
UPLOAD_RESUME_RETRY_MAX_DELAY_SECONDS = 10.0
UPLOAD_LOG_LOCK = threading.Lock()
UploadWaitMode = Literal["staged", "finalized"]


class CollectionManifestEntry(TypedDict):
    path: str
    bytes: int
    sha256: str


def client() -> ApiClient:
    return ApiClient()


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
        raise typer.BadParameter(
            "RIVERHOG_UPLOAD_FILE_LOG_BYTES must be a non-negative integer"
        )
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
    return os.getenv("RIVERHOG_UPLOAD_WAIT", "staged").strip().lower() or "staged"


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


def _download_progress_logger() -> Callable[[int, int | None], None]:
    started_at = time.monotonic()
    last_logged_at = started_at

    def progress(downloaded_bytes: int, total_bytes: int | None) -> None:
        nonlocal last_logged_at
        now = time.monotonic()
        if now - last_logged_at < DOWNLOAD_PROGRESS_INTERVAL_SECONDS:
            return
        elapsed = max(now - started_at, 0.001)
        rate = downloaded_bytes / elapsed
        if total_bytes is None or total_bytes <= 0:
            total_text = "unknown"
            percent_text = ""
        else:
            total_text = _format_bytes(total_bytes)
            percent_text = f" ({downloaded_bytes / total_bytes * 100.0:.1f}%)"
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
    if isinstance(exc, ServiceUnavailable):
        return exc.message
    return f"{type(exc).__name__}: {exc}"


def _create_or_resume_collection_file_upload(
    api: ApiClient,
    collection_id: str,
    path_value: str,
) -> dict[str, Any]:
    delay = UPLOAD_RESUME_RETRY_INITIAL_DELAY_SECONDS
    for attempt in range(UPLOAD_RESUME_RETRY_ATTEMPTS):
        try:
            return api.create_or_resume_collection_file_upload(collection_id, path_value)
        except (httpx.TransportError, httpx.HTTPStatusError, ServiceUnavailable) as exc:
            if not _is_transient_upload_error(exc) or attempt == UPLOAD_RESUME_RETRY_ATTEMPTS - 1:
                raise
            _log_upload(
                f"Upload resume check for {path_value} failed "
                f"({_upload_error_description(exc)}); retrying"
            )
            time.sleep(delay)
            delay = min(delay * 2, UPLOAD_RESUME_RETRY_MAX_DELAY_SECONDS)
    raise AssertionError("unreachable")


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


def _upload_collection_file(
    api: ApiClient,
    collection_id: str,
    source_path: Path,
    file_payload: dict[str, object],
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
            except (httpx.TransportError, httpx.HTTPStatusError, ServiceUnavailable) as exc:
                if not _is_transient_upload_error(exc):
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
    upload_files: list[dict[str, object]],
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
            executor.submit(upload_worker)
            for _ in range(min(file_concurrency, len(pending_files)))
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
    manifest: list[CollectionManifestEntry],
    collection: dict[str, object],
) -> dict[str, object]:
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
    return {
        "collection_id": collection_id,
        "ingest_source": collection.get("ingest_source"),
        "state": "finalized",
        "files_total": len(files),
        "files_pending": 0,
        "files_partial": 0,
        "files_uploaded": len(files),
        "hot_promoted_files": len(files),
        "bytes_total": bytes_total,
        "uploaded_bytes": bytes_total,
        "hot_promoted_bytes": bytes_total,
        "missing_bytes": 0,
        "upload_state_expires_at": None,
        "files": files,
        "collection": collection,
    }


def _wait_for_finalized_collection(
    api: ApiClient,
    collection_id: str,
    manifest: list[CollectionManifestEntry],
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


def _archive_wait_status(payload: dict[str, object]) -> str:
    phase = payload.get("archive_phase")
    if not phase:
        return ""
    status = f", archive_phase={phase}"
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


@app.command("upload")
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
            help="Wait until 'staged' server handoff or full 'finalized' collection archival",
        ),
    ] = _default_upload_wait_mode(),
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    wait_mode = _normalize_upload_wait_mode(wait)
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise typer.BadParameter("collection source must be a directory")

    api = client()
    _log_upload(f"Hashing collection manifest from {resolved_root}")
    manifest_started_at = time.monotonic()
    manifest = _local_collection_manifest(resolved_root)
    manifest_bytes = sum(item["bytes"] for item in manifest)
    _log_upload(
        "Manifest hashed: "
        f"{len(manifest)} files, {_format_bytes(manifest_bytes)} "
        f"in {time.monotonic() - manifest_started_at:.1f}s"
    )
    payload = api.create_or_resume_collection_upload(
        slug,
        manifest,
        ingest_source=str(resolved_root),
        upload_timestamp=upload_timestamp,
    )
    collection_id = str(payload["collection_id"])
    upload_files = payload["files"]
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
        _log_upload(
            "All files uploaded; collection finalization will continue in the background"
        )
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


@app.command("find")
def find_cmd(
    query: Annotated[str, typer.Argument(help="Search query")],
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 25,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    emit(client().search(query, limit), json_mode=json_mode)


@app.command("show")
def show_cmd(
    collection: Annotated[str, typer.Argument(help="Collection id")],
    files: Annotated[bool, typer.Option("--files", help="List files in the collection")] = False,
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    if files:
        payload = client().list_collection_files(collection, page=page, per_page=per_page)
        emit(payload if json_mode else format_collection_files(payload), json_mode=json_mode)
    else:
        api = client()
        payload = api.get_collection(collection)
        if json_mode:
            emit(payload, json_mode=True)
            return
        glacier_payload = api.get_glacier_report(collection=collection)
        emit(format_collection_summary(payload, glacier_payload), json_mode=False)


@app.command("status")
def status_cmd(
    target: Annotated[str, typer.Argument(help="Target selector")],
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().query_files(target, page=page, per_page=per_page)
    emit(payload if json_mode else format_files(payload), json_mode=json_mode)


@app.command("get")
def get_cmd(
    target: Annotated[str, typer.Argument(help="File target selector")],
    output: Annotated[Path | None, typer.Option("-o", "--output", help="Output path")] = None,
) -> None:
    import sys

    content = client().get_file_content(target, output)
    if output is None:
        sys.stdout.buffer.write(content)
    else:
        typer.echo(f"wrote {len(content)} bytes to {output}")


@app.command("plan")
def plan_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "fill",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "desc",
    query: Annotated[
        str | None,
        typer.Option(
            "--query",
            help=PLAN_QUERY_HELP,
        ),
    ] = None,
    collection: Annotated[
        str | None, typer.Option("--collection", help="Filter by exact contained collection id")
    ] = None,
    iso_ready: Annotated[
        bool | None,
        typer.Option(
            "--iso-ready/--not-ready", help="Filter by whether the candidate is ready to finalize"
        ),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().get_plan(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        query=query,
        collection=collection,
        iso_ready=iso_ready,
    )
    emit(payload if json_mode else format_plan(payload), json_mode=json_mode)


@app.command("images")
def images_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "finalized_at",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "desc",
    query: Annotated[
        str | None,
        typer.Option("--query", help=IMAGE_QUERY_HELP),
    ] = None,
    collection: Annotated[
        str | None, typer.Option("--collection", help="Filter by exact contained collection id")
    ] = None,
    has_copies: Annotated[
        bool | None,
        typer.Option(
            "--has-copies/--no-copies", help="Filter by whether the image has registered copies"
        ),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    api = client()
    payload = api.list_images(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        query=query,
        collection=collection,
        has_copies=has_copies,
    )
    if json_mode:
        emit(payload, json_mode=True)
        return

    ready_plan_payload = api.get_plan(
        page=page,
        per_page=per_page,
        sort="fill",
        order="desc",
        query=query,
        collection=collection,
        iso_ready=True,
    )
    backlog_plan_payload = api.get_plan(
        page=page,
        per_page=per_page,
        sort="fill",
        order="desc",
        query=query,
        collection=collection,
        iso_ready=False,
    )
    collections_query = collection or query
    unprotected_collections = api.list_collections(
        page=page,
        per_page=per_page,
        q=collections_query,
        protection_state="cloud_only",
    )
    partially_protected_collections = api.list_collections(
        page=page,
        per_page=per_page,
        q=collections_query,
        protection_state="under_protected",
    )
    protected_collections = api.list_collections(
        page=page,
        per_page=per_page,
        q=collections_query,
        protection_state="fully_protected",
    )
    emit(
        format_archive_status(
            ready_plan_payload,
            backlog_plan_payload,
            payload,
            unprotected_collections,
            partially_protected_collections,
            protected_collections,
        ),
        json_mode=False,
    )


@app.command("glacier")
def glacier_cmd(
    collection: Annotated[
        str | None, typer.Option("--collection", help="Filter to one exact collection id")
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().get_glacier_report(collection=collection)
    emit(payload if json_mode else format_glacier_report(payload), json_mode=json_mode)


@iso_app.command("get")
def iso_get_cmd(
    image_id: Annotated[str, typer.Argument(help="Image id")],
    output: Annotated[Path | None, typer.Option("-o", "--output", help="Output path")] = None,
) -> None:
    import sys

    if output is None:
        content = client().download_iso(image_id)
        if not isinstance(content, bytes):
            raise typer.Exit(code=1)
        sys.stdout.buffer.write(content)
        raise typer.Exit(code=0)

    typer.echo(f"downloading ISO {image_id} to {output}", err=True)
    content = client().download_iso(image_id, output, progress=_download_progress_logger())
    downloaded_bytes = len(content) if isinstance(content, bytes) else content
    typer.echo(f"wrote {downloaded_bytes} bytes to {output}")


@copy_app.command("add")
def copy_add_cmd(
    image_id: Annotated[str, typer.Argument(help="Finalized image id")],
    at: Annotated[str, typer.Option("--at", help="Physical location label")],
    copy_id: Annotated[
        str | None,
        typer.Option("--copy-id", help="Generated copy id to claim explicitly"),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().register_copy(image_id, at, copy_id=copy_id)
    emit(payload if json_mode else format_copy(payload["copy"]), json_mode=json_mode)


@copy_app.command("list")
def copy_list_cmd(
    image_id: Annotated[str, typer.Argument(help="Finalized image id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().list_copies(image_id)
    emit(payload if json_mode else format_copies(payload), json_mode=json_mode)


@copy_app.command("move")
def copy_move_cmd(
    image_id: Annotated[str, typer.Argument(help="Finalized image id")],
    copy_id: Annotated[str, typer.Argument(help="Generated copy id")],
    to: Annotated[str, typer.Option("--to", help="New physical location label")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().update_copy(image_id, copy_id, location=to)
    emit(payload if json_mode else format_copy(payload["copy"]), json_mode=json_mode)


@copy_app.command("mark")
def copy_mark_cmd(
    image_id: Annotated[str, typer.Argument(help="Finalized image id")],
    copy_id: Annotated[str, typer.Argument(help="Generated copy id")],
    state: Annotated[str, typer.Option("--state", help="Copy lifecycle state")],
    verification_state: Annotated[
        str | None,
        typer.Option("--verification-state", help="Verification state"),
    ] = None,
    at: Annotated[
        str | None,
        typer.Option("--at", help="Updated physical location label"),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().update_copy(
        image_id,
        copy_id,
        location=at,
        state=state,
        verification_state=verification_state,
    )
    emit(payload if json_mode else format_copy(payload["copy"]), json_mode=json_mode)


@app.command("pin")
def pin_cmd(
    target: Annotated[str, typer.Argument(help="Target selector")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().pin(target)
    emit(payload if json_mode else format_pin(payload), json_mode=json_mode)


@app.command("release")
def release_cmd(
    target: Annotated[str, typer.Argument(help="Target selector")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    emit(client().release(target), json_mode=json_mode)


@app.command("pins")
def pins_cmd(
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    emit(client().list_pins(), json_mode=json_mode)


@app.command("fetch")
def fetch_cmd(
    fetch_id: Annotated[str, typer.Argument(help="Fetch id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    summary = client().get_fetch(fetch_id)
    if json_mode:
        emit(summary, json_mode=True)
        return
    manifest = client().get_fetch_manifest(fetch_id)
    emit(format_fetch(summary, manifest), json_mode=False)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
