from __future__ import annotations

import errno
import http.client
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

DEFAULT_RUNNER_URL = "http://127.0.0.1:8092"
RUNNER_URL_ENV = "MUNCHY_RUNNER_URL"
PROGRESS_ENV = "MUNCHY_PROGRESS"
DEFAULT_UPLOAD_CHUNK_MIB = 64
DEFAULT_UPLOAD_WORKERS = 12
UPLOAD_RETRY_INITIAL_DELAY_SECONDS = 1.0
UPLOAD_RETRY_MAX_DELAY_SECONDS = 60.0
UPLOAD_RETRY_NOTICE_SECONDS = 60.0
TRANSIENT_UPLOAD_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
TRANSIENT_UPLOAD_ERRNOS = {
    errno.ECONNABORTED,
    errno.ECONNRESET,
    errno.EHOSTDOWN,
    errno.EHOSTUNREACH,
    errno.ENETDOWN,
    errno.ENETRESET,
    errno.ENETUNREACH,
    errno.ETIMEDOUT,
    errno.EPIPE,
}


@dataclass(frozen=True)
class RunnerInputFile:
    source: Path
    rel_path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class RunnerUploadRequest:
    upload_id: str
    job_id: str
    files: tuple[RunnerInputFile, ...]
    storage_hint: dict[str, Any]
    job_payload: dict[str, Any]
    upload_workers: int = DEFAULT_UPLOAD_WORKERS
    upload_chunk_mib: int = DEFAULT_UPLOAD_CHUNK_MIB

    @property
    def upload_chunk_bytes(self) -> int:
        return self.upload_chunk_mib * 1024 * 1024


def runner_url_setting(runner_url: str | None = None) -> str:
    return (runner_url or os.getenv(RUNNER_URL_ENV) or DEFAULT_RUNNER_URL).rstrip("/")


def format_bytes(value: int | float | None) -> str:
    amount = float(value or 0)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{int(amount)} B"
    return f"{amount:.2f} {unit}"


def format_rate(value: int | float | None) -> str:
    return f"{format_bytes(value)}/s"


def short_error(exc: BaseException, *, max_len: int = 140) -> str:
    text = str(exc).strip().replace("\n", " ")
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "..."


def short_path(value: str, *, max_len: int = 80) -> str:
    if len(value) <= max_len:
        return value
    return "..." + value[-(max_len - 3) :]


class RunnerHttpError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: bytes) -> None:
        text = body.decode("utf-8", "replace").strip()
        message = f"{method} {url} returned HTTP {status}"
        if text:
            message = f"{message}: {text}"
        super().__init__(message)
        self.method = method
        self.url = url
        self.status = status
        self.body = body


def is_transient_upload_error(exc: BaseException) -> bool:
    if isinstance(exc, RunnerHttpError):
        return exc.status in TRANSIENT_UPLOAD_HTTP_STATUSES
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError, http.client.HTTPException, socket.timeout)):
        return True
    if isinstance(exc, OSError):
        return exc.errno in TRANSIENT_UPLOAD_ERRNOS
    return False


def next_upload_retry_delay(current_delay: float) -> float:
    return min(
        max(current_delay, UPLOAD_RETRY_INITIAL_DELAY_SECONDS) * 2.0,
        UPLOAD_RETRY_MAX_DELAY_SECONDS,
    )


class UploadRetryReporter:
    def __init__(self, *, label: str = "upload") -> None:
        self.label = label
        self.total_retries = 0
        self.files: set[str] = set()
        self.started_at: float | None = None
        self.last_notice = 0.0
        self.latest_path = ""
        self.latest_error = ""
        self.lock = Lock()

    def mark_retry(
        self,
        *,
        rel_path: str,
        retry_count: int,
        retry_delay: float,
        exc: BaseException,
    ) -> None:
        with self.lock:
            now = time.monotonic()
            self.started_at = self.started_at or now
            self.total_retries += 1
            self.files.add(rel_path)
            self.latest_path = rel_path
            self.latest_error = short_error(exc)
            if self.last_notice and now - self.last_notice < UPLOAD_RETRY_NOTICE_SECONDS:
                return
            elapsed = now - self.started_at
            print(
                (
                    f"{self.label} retrying after transient network issue: "
                    f"{self.total_retries} retries across {len(self.files)} file(s), "
                    f"latest {short_path(rel_path)} retry {retry_count}, "
                    f"next in {retry_delay:.0f}s"
                    + (f" ({self.latest_error})" if self.latest_error else "")
                    + (f", {elapsed:.0f}s elapsed" if elapsed >= 1 else "")
                ),
                file=sys.stderr,
            )
            self.last_notice = now

    def finish(self) -> None:
        with self.lock:
            if not self.total_retries:
                return
            print(
                (
                    f"{self.label} recovered from transient network issues: "
                    f"{self.total_retries} retries across {len(self.files)} file(s)"
                ),
                file=sys.stderr,
            )
            self.total_retries = 0
            self.files.clear()
            self.started_at = None
            self.last_notice = 0.0
            self.latest_path = ""
            self.latest_error = ""


def format_progress_bytes(done: int | float | None, total: int | float | None) -> str:
    return f"{format_bytes(done)} / {format_bytes(total)}"


def format_encode_progress(progress: dict[str, Any]) -> str:
    files_total = int(progress.get("files_total") or 0)
    files_encoded = int(progress.get("files_encoded") or 0)
    files_encoding = int(progress.get("files_encoding") or 0)
    files_failed = int(progress.get("files_failed") or 0)
    pct = float(progress.get("percent_input_bytes") or progress.get("percent_files") or 0.0)
    input_done = int(progress.get("input_bytes_encoded") or 0)
    input_total = int(progress.get("input_bytes_total") or 0)
    output_bytes = int(progress.get("output_bytes") or 0)
    active_output_bytes = int(progress.get("active_output_bytes") or 0)
    input_rate = int(progress.get("input_rate_bytes_per_second") or 0)
    output_rate = int(progress.get("output_rate_bytes_per_second") or 0)
    running_batches = int(progress.get("running_batches") or 0)
    pipeline_batches = int(progress.get("pipeline_batches") or 0)
    parts = [
        f"encode {files_encoded}/{files_total} files",
        f"{format_progress_bytes(input_done, input_total)} input",
        f"{pct:.2f}%",
        f"{format_rate(input_rate)} input",
        f"{format_rate(output_rate)} output",
        f"{format_bytes(output_bytes)} written",
    ]
    if files_encoding:
        parts.append(f"{files_encoding} active")
    if active_output_bytes:
        parts.append(f"{format_bytes(active_output_bytes)} active output")
    if running_batches or pipeline_batches:
        parts.append(f"batches {running_batches}/{pipeline_batches}")
    if files_failed:
        parts.append(f"{files_failed} failed")
    return ", ".join(parts)


def format_input_upload_progress(progress: dict[str, Any]) -> str:
    files_uploaded = int(progress.get("files_uploaded") or 0)
    files_total = int(progress.get("files_total") or 0)
    uploaded_bytes = int(progress.get("uploaded_bytes") or 0)
    bytes_total = int(progress.get("bytes_total") or 0)
    pct = float(
        progress.get("percent_bytes")
        or ((uploaded_bytes / bytes_total * 100.0) if bytes_total else 100.0)
    )
    rate = int(progress.get("rate_bytes_per_second") or 0)
    parts = [
        f"upload {files_uploaded}/{files_total} files",
        format_progress_bytes(uploaded_bytes, bytes_total),
        f"{pct:.2f}%",
    ]
    if rate:
        parts.append(format_rate(rate))
    return ", ".join(parts)


def job_upload_progress(job: dict[str, Any]) -> dict[str, Any] | None:
    upload_progress = job.get("upload_progress")
    if isinstance(upload_progress, dict):
        return upload_progress
    legacy_progress = job.get("input_upload_progress")
    if isinstance(legacy_progress, dict):
        return legacy_progress
    return None


def format_progress_status_line(job: dict[str, Any]) -> str:
    pieces: list[str] = []
    upload_progress = job_upload_progress(job)
    if upload_progress is not None:
        pieces.append(format_input_upload_progress(upload_progress))
    encode_progress = job.get("encode_progress")
    if isinstance(encode_progress, dict):
        pieces.append(format_encode_progress(encode_progress))
    return " | ".join(pieces) if pieces else "progress: waiting"


def format_job_status_line(job: dict[str, Any]) -> str:
    state = str(job.get("state") or "unknown")
    phase = str(job.get("phase") or "").strip()
    pieces = [f"job: {state}"]
    if phase:
        pieces.append(phase)
    progress = format_progress_status_line(job)
    if progress != "progress: waiting":
        pieces.append(progress)
    return " | ".join(pieces)


def format_job_summary_line(job: dict[str, Any]) -> str:
    job_id = str(job.get("job_id") or job.get("id") or "unknown")
    collection = str(job.get("collection_slug") or "").strip()
    prefix = job_id if not collection else f"{job_id} [{collection}]"
    return f"{prefix} | {format_job_status_line(job)}"


class ProgressRenderer:
    include_job: bool

    @classmethod
    def plain(cls, *, include_job: bool) -> ProgressRenderer:
        return PlainProgressRenderer(include_job=include_job)

    def __enter__(self) -> ProgressRenderer:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.stop()

    def update(self, job: dict[str, Any], *, force: bool = False) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        return


class PlainProgressRenderer(ProgressRenderer):
    def __init__(self, *, include_job: bool) -> None:
        self.include_job = include_job
        self.last_line = ""

    def update(self, job: dict[str, Any], *, force: bool = False) -> None:
        line = format_job_status_line(job) if self.include_job else format_progress_status_line(job)
        if force or line != self.last_line:
            print(line, file=sys.stderr)
            self.last_line = line


class RichProgressRenderer(ProgressRenderer):
    def __init__(self, *, include_job: bool, title: str) -> None:
        from rich.console import Console
        from rich.live import Live

        self.include_job = include_job
        self.title = title
        self.console = Console(stderr=True)
        self.live = Live(
            self._render({}),
            console=self.console,
            refresh_per_second=4,
            transient=False,
        )
        self.started = False

    def __enter__(self) -> RichProgressRenderer:
        self.live.start(refresh=True)
        self.started = True
        return self

    def update(self, job: dict[str, Any], *, force: bool = False) -> None:
        if not self.started:
            self.__enter__()
        self.live.update(self._render(job), refresh=True)

    def stop(self) -> None:
        if self.started:
            self.live.stop()
            self.started = False

    def _render(self, job: dict[str, Any]) -> Any:
        from rich import box
        from rich.panel import Panel
        from rich.table import Table

        table = Table.grid(padding=(0, 2))
        table.add_column(justify="right", style="cyan", no_wrap=True)
        table.add_column(ratio=1)

        state = str(job.get("state") or "running")
        phase = str(job.get("phase") or "").strip()
        if self.include_job:
            table.add_row("State", state)
            if phase:
                table.add_row("Phase", phase)

        upload_progress = job_upload_progress(job)
        if upload_progress is not None:
            table.add_row("Upload", self._bar(upload_progress, percent_key="percent_bytes"))
            table.add_row("", format_input_upload_progress(upload_progress))

        encode_progress = job.get("encode_progress")
        if isinstance(encode_progress, dict):
            table.add_row("Encode", self._bar(encode_progress, percent_key="percent_input_bytes"))
            table.add_row("", format_encode_progress(encode_progress))

        if upload_progress is None and not isinstance(encode_progress, dict):
            table.add_row("Progress", "waiting")

        return Panel(table, title=self.title, border_style="cyan", box=box.ROUNDED)

    def _bar(self, progress: dict[str, Any], *, percent_key: str) -> Any:
        from rich.progress import BarColumn, Progress, TextColumn

        pct = float(progress.get(percent_key) or progress.get("percent_files") or 0.0)
        pct = max(0.0, min(100.0, pct))
        bar = Progress(
            TextColumn(""),
            BarColumn(bar_width=None),
            TextColumn(f"{pct:.2f}%"),
            expand=True,
        )
        bar.add_task("", total=100.0, completed=pct)
        return bar


def make_progress_renderer(*, include_job: bool, title: str) -> ProgressRenderer:
    mode = os.getenv(PROGRESS_ENV, "auto").strip().lower()
    if mode in {"plain", "text", "off"}:
        return PlainProgressRenderer(include_job=include_job)
    if mode != "rich" and (not sys.stderr.isatty() or os.getenv("TERM", "") == "dumb"):
        return PlainProgressRenderer(include_job=include_job)
    try:
        return RichProgressRenderer(include_job=include_job, title=title)
    except Exception as exc:
        if mode == "rich":
            print(f"warning: rich progress unavailable; using plain text: {exc}", file=sys.stderr)
        return PlainProgressRenderer(include_job=include_job)


class UploadProgress:
    def __init__(
        self,
        total_files: int,
        total_bytes: int,
        *,
        job_status_provider: Any | None = None,
        renderer: ProgressRenderer | None = None,
    ) -> None:
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.completed_files = 0
        self.completed_bytes = 0
        self.started_at = time.monotonic()
        self.last_printed_at = self.started_at
        self.lock = Lock()
        self.job_status_provider = job_status_provider
        self.renderer = renderer or ProgressRenderer.plain(include_job=False)
        self.last_remote_uploaded_bytes: int | None = None
        self.last_remote_rate_at: float | None = None

    def remote_upload_progress_with_rate(
        self,
        remote_upload_progress: dict[str, Any],
        *,
        now: float,
    ) -> dict[str, Any]:
        merged = dict(remote_upload_progress)
        merged.pop("rate_bytes_per_second", None)
        try:
            uploaded_bytes = int(merged.get("uploaded_bytes") or 0)
        except (TypeError, ValueError):
            uploaded_bytes = 0
        if (
            self.last_remote_uploaded_bytes is not None
            and self.last_remote_rate_at is not None
            and uploaded_bytes > self.last_remote_uploaded_bytes
        ):
            elapsed = max(now - self.last_remote_rate_at, 0.001)
            merged["rate_bytes_per_second"] = int(
                (uploaded_bytes - self.last_remote_uploaded_bytes) / elapsed
            )
        self.last_remote_uploaded_bytes = uploaded_bytes
        self.last_remote_rate_at = now
        return merged

    def mark_complete(self, item: RunnerInputFile) -> None:
        with self.lock:
            self.completed_files += 1
            self.completed_bytes += item.bytes
            now = time.monotonic()
            should_print = (
                self.completed_files == self.total_files or now - self.last_printed_at >= 15
            )
            if not should_print:
                return
            elapsed = max(now - self.started_at, 0.001)
            upload_progress = {
                "files_uploaded": self.completed_files,
                "files_total": self.total_files,
                "uploaded_bytes": self.completed_bytes,
                "bytes_total": self.total_bytes,
                "rate_bytes_per_second": int(self.completed_bytes / elapsed),
            }
            job: dict[str, Any] = {"upload_progress": upload_progress}
            if self.job_status_provider is not None:
                try:
                    remote_job = self.job_status_provider()
                except Exception:
                    remote_job = None
                if isinstance(remote_job, dict):
                    remote_upload_progress = job_upload_progress(remote_job)
                    if remote_upload_progress is not None:
                        job["upload_progress"] = self.remote_upload_progress_with_rate(
                            remote_upload_progress,
                            now=now,
                        )
                    encode_progress = remote_job.get("encode_progress")
                    if isinstance(encode_progress, dict):
                        job["encode_progress"] = encode_progress
            self.renderer.update(
                job,
                force=self.completed_files == self.total_files,
            )
            self.last_printed_at = now


class MunchyRunnerClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        payload: dict[str, Any] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        expect: set[int] | None = None,
        timeout: float = 60.0,
    ) -> tuple[int, bytes, Any]:
        if path_or_url.startswith(("http://", "https://")):
            url = path_or_url
        else:
            url = f"{self.base_url}{path_or_url}"
        request_headers = dict(headers or {})
        body = data
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            response = urllib.request.urlopen(req, timeout=timeout)
            response_body = response.read()
            status = int(response.status)
        except urllib.error.HTTPError as exc:
            response_body = exc.read()
            status = int(exc.code)
            if expect is None or status not in expect:
                raise RunnerHttpError(method, url, status, response_body) from exc
            return status, response_body, exc
        if expect is not None and status not in expect:
            raise RunnerHttpError(method, url, status, response_body)
        return status, response_body, response

    def json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        expect: set[int] | None = None,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        _, body, _ = self.request(method, path, payload=payload, expect=expect, timeout=timeout)
        if not body:
            return {}
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise RuntimeError(f"{method} {path} did not return a JSON object")
        return parsed

    def check_ready(
        self,
        workflow_mode: str | None = None,
        *,
        requested_containers: list[str] | None = None,
    ) -> None:
        self.json("GET", "/health/ready")
        capabilities = self.json("GET", "/v1/capabilities")
        groups = capabilities.get("profile_groups", {})
        if (
            not isinstance(groups, dict)
            or groups.get("input_path_shape") != "<profile-group>/<file>"
        ):
            raise RuntimeError("runner does not advertise profile-group uploads")
        if workflow_mode:
            workflow_modes = capabilities.get("workflow_modes", [])
            if workflow_mode not in workflow_modes:
                raise RuntimeError(f"runner does not advertise {workflow_mode} jobs")
        storage = capabilities.get("storage", {})
        if not isinstance(storage, dict) or not storage.get("input_upload_storage_hint_required"):
            raise RuntimeError("runner does not advertise required input upload storage hints")
        if requested_containers:
            encode_profile_caps = capabilities.get("encode_profile", {})
            available_containers = (
                encode_profile_caps.get("containers", [])
                if isinstance(encode_profile_caps, dict)
                else []
            )
            missing = sorted(set(requested_containers) - set(available_containers))
            if missing:
                raise RuntimeError(
                    "runner does not advertise requested archive container(s): "
                    + ", ".join(missing)
                )

    def notify_preflight_failed(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return self.json(
                "POST",
                "/v1/notifications/preflight-failed",
                payload=payload,
                expect={202},
            )
        except Exception as exc:
            print(
                f"warning: failed to notify runner about preflight failure: {exc}",
                file=sys.stderr,
            )
            return None

    def create_or_get_input_upload(self, request: RunnerUploadRequest) -> dict[str, Any]:
        files = [
            {"path": item.rel_path, "bytes": item.bytes, "sha256": item.sha256}
            for item in request.files
        ]
        status, body, _ = self.request(
            "POST",
            "/v1/input-uploads",
            payload={
                "upload_id": request.upload_id,
                "files": files,
                "storage_hint": request.storage_hint,
            },
            expect={201, 409},
        )
        if status == 201:
            return json.loads(body.decode("utf-8"))
        existing = self.json("GET", f"/v1/input-uploads/{urllib.parse.quote(request.upload_id)}")
        self._validate_existing_upload(existing, files, request.storage_hint)
        return existing

    def _validate_existing_upload(
        self,
        upload: dict[str, Any],
        files: list[dict[str, Any]],
        storage_hint: dict[str, Any],
    ) -> None:
        existing = {
            str(item.get("path")): {
                "bytes": int(item.get("bytes") or 0),
                "sha256": item.get("sha256"),
            }
            for item in upload.get("files", [])
            if isinstance(item, dict)
        }
        expected = {
            str(item["path"]): {"bytes": int(item["bytes"]), "sha256": item.get("sha256")}
            for item in files
        }
        if existing != expected:
            raise RuntimeError(
                f"input upload {upload.get('upload_id')} already exists with different files"
            )
        if upload.get("storage_hint") != storage_hint:
            raise RuntimeError(
                f"input upload {upload.get('upload_id')} already exists with "
                "a different storage hint"
            )

    def upload_files(self, request: RunnerUploadRequest) -> dict[str, Any]:
        total_files = len(request.files)
        total_bytes = sum(item.bytes for item in request.files)
        chunk_mib = request.upload_chunk_mib
        retry_reporter = UploadRetryReporter(label="upload")
        current_upload = self.json(
            "GET",
            f"/v1/input-uploads/{urllib.parse.quote(request.upload_id)}",
        )
        completed_paths = {
            str(item.get("path"))
            for item in current_upload.get("files", [])
            if isinstance(item, dict) and item.get("complete")
        }
        pending_files = [item for item in request.files if item.rel_path not in completed_paths]
        pending_bytes = sum(item.bytes for item in pending_files)
        skipped_files = total_files - len(pending_files)
        print(
            (
                f"uploading {len(pending_files)}/{total_files} remaining files "
                f"({pending_bytes / 1024**3:.2f}/{total_bytes / 1024**3:.2f} GiB) "
                f"with {request.upload_workers} workers, {chunk_mib} MiB chunks"
            ),
            file=sys.stderr,
        )
        if skipped_files:
            print(
                f"upload resume: skipped {skipped_files} already complete files",
                file=sys.stderr,
            )
        if pending_files:
            renderer = make_progress_renderer(include_job=False, title="Munchy Upload")
            if request.upload_workers == 1 or len(pending_files) <= 1:
                with renderer:
                    self._upload_files_serial(request, pending_files, retry_reporter, renderer)
            else:
                with renderer:
                    self._upload_files_parallel(request, pending_files, retry_reporter, renderer)
        retry_reporter.finish()
        upload = self.json("GET", f"/v1/input-uploads/{urllib.parse.quote(request.upload_id)}")
        if upload.get("state") != "uploaded":
            raise RuntimeError(f"input upload did not complete: {upload}")
        return upload

    def _upload_files_serial(
        self,
        request: RunnerUploadRequest,
        files: list[RunnerInputFile],
        retry_reporter: UploadRetryReporter,
        renderer: ProgressRenderer,
    ) -> None:
        progress = UploadProgress(
            len(files),
            sum(item.bytes for item in files),
            renderer=renderer,
            job_status_provider=lambda: self.json(
                "GET",
                f"/v1/jobs/{urllib.parse.quote(request.job_id)}",
                timeout=5.0,
            ),
        )
        for item in files:
            self.upload_file(
                request.upload_id,
                item,
                chunk_bytes=request.upload_chunk_bytes,
                retry_reporter=retry_reporter,
            )
            progress.mark_complete(item)

    def _upload_files_parallel(
        self,
        request: RunnerUploadRequest,
        files: list[RunnerInputFile],
        retry_reporter: UploadRetryReporter,
        renderer: ProgressRenderer,
    ) -> None:
        progress = UploadProgress(
            len(files),
            sum(item.bytes for item in files),
            renderer=renderer,
            job_status_provider=lambda: self.json(
                "GET",
                f"/v1/jobs/{urllib.parse.quote(request.job_id)}",
                timeout=5.0,
            ),
        )
        executor = ThreadPoolExecutor(max_workers=request.upload_workers)
        futures = {
            executor.submit(
                self.upload_file,
                request.upload_id,
                item,
                chunk_bytes=request.upload_chunk_bytes,
                retry_reporter=retry_reporter,
            ): item
            for item in files
        }
        try:
            for future in as_completed(futures):
                item = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    for pending in futures:
                        pending.cancel()
                    raise RuntimeError(f"upload failed for {item.rel_path}: {exc}") from exc
                progress.mark_complete(item)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    def upload_file(
        self,
        upload_id: str,
        item: RunnerInputFile,
        *,
        chunk_bytes: int,
        retry_reporter: UploadRetryReporter | None = None,
    ) -> None:
        retry_delay = UPLOAD_RETRY_INITIAL_DELAY_SECONDS
        retry_count = 0
        retry_reporter = retry_reporter or UploadRetryReporter(label="upload")
        while True:
            try:
                self._upload_file_once(upload_id, item, chunk_bytes=chunk_bytes)
                return
            except Exception as exc:
                if not is_transient_upload_error(exc):
                    raise
                retry_count += 1
                retry_reporter.mark_retry(
                    rel_path=item.rel_path,
                    retry_count=retry_count,
                    retry_delay=retry_delay,
                    exc=exc,
                )
                time.sleep(retry_delay)
                retry_delay = next_upload_retry_delay(retry_delay)

    def _upload_file_once(
        self,
        upload_id: str,
        item: RunnerInputFile,
        *,
        chunk_bytes: int,
    ) -> None:
        escaped_upload_id = urllib.parse.quote(upload_id)
        escaped_rel = urllib.parse.quote(item.rel_path, safe="/")
        upload = self.json(
            "POST",
            f"/v1/input-uploads/{escaped_upload_id}/files/{escaped_rel}/upload",
            expect={201},
        )
        upload_url = str(upload["upload_url"])
        offset = int(upload.get("offset") or 0)
        length = int(upload.get("length") or item.bytes)
        if length != item.bytes:
            raise RuntimeError(
                f"runner expected {length} bytes for {item.rel_path}, "
                f"local file has {item.bytes}"
            )
        if offset < 0:
            offset = 0
        if offset > item.bytes:
            raise RuntimeError(f"runner upload offset is past EOF for {item.rel_path}: {offset}")
        if offset == item.bytes:
            return

        with item.source.open("rb") as fh:
            fh.seek(offset)
            while offset < item.bytes:
                chunk = fh.read(min(chunk_bytes, item.bytes - offset))
                if not chunk:
                    break
                _, _, response = self.request(
                    "PATCH",
                    upload_url,
                    data=chunk,
                    headers={
                        "Tus-Resumable": "1.0.0",
                        "Upload-Offset": str(offset),
                        "Content-Type": "application/offset+octet-stream",
                    },
                    expect={204},
                    timeout=300.0,
                )
                next_offset = response.headers.get("Upload-Offset")
                offset = int(next_offset) if next_offset else offset + len(chunk)
        if offset != item.bytes:
            raise RuntimeError(f"incomplete upload for {item.rel_path}: {offset} of {item.bytes}")

    def create_job(self, request: RunnerUploadRequest) -> dict[str, Any]:
        status, body, _ = self.request(
            "POST",
            "/v1/jobs",
            payload=request.job_payload,
            expect={202, 409},
        )
        if status == 202:
            return json.loads(body.decode("utf-8"))
        existing = self.json("GET", f"/v1/jobs/{urllib.parse.quote(request.job_id)}")
        if existing.get("input_upload_id") != request.upload_id:
            raise RuntimeError(f"job {request.job_id} already exists for a different upload")
        if existing.get("state") in {"failed", "cancelled"}:
            return self.json(
                "POST",
                f"/v1/jobs/{urllib.parse.quote(request.job_id)}/resume",
                expect={202},
            )
        return existing

    def delete_input_upload(self, upload_id: str) -> bool:
        status, _, _ = self.request(
            "DELETE",
            f"/v1/input-uploads/{urllib.parse.quote(upload_id)}",
            expect={202, 404, 409},
        )
        return status == 202

    def get_job(self, job_id: str, *, compact: bool = False) -> dict[str, Any]:
        path = f"/v1/jobs/{urllib.parse.quote(job_id)}"
        if compact:
            path += "?compact=true"
        return self.json("GET", path)

    def list_jobs(self, *, include_terminal: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode(
            {
                "include_terminal": "true" if include_terminal else "false",
                "limit": str(limit),
            }
        )
        payload = self.json("GET", f"/v1/jobs?{params}")
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            raise RuntimeError(f"runner returned invalid jobs list: {payload}")
        return [job for job in jobs if isinstance(job, dict)]

    def cancel_job(self, job_id: str, *, cleanup: bool = False) -> dict[str, Any]:
        path = f"/v1/jobs/{urllib.parse.quote(job_id)}/cancel"
        if cleanup:
            path += "?cleanup=true"
        return self.json("POST", path, expect={202})

    def wait_for_job(self, job_id: str, *, interval: float = 10.0) -> dict[str, Any]:
        retry_delay = UPLOAD_RETRY_INITIAL_DELAY_SECONDS
        retry_reporter = UploadRetryReporter(label="job status")
        renderer = make_progress_renderer(
            include_job=True,
            title=f"Munchy Job {short_path(job_id, max_len=48)}",
        )
        with renderer:
            while True:
                try:
                    job = self.get_job(job_id)
                    retry_delay = UPLOAD_RETRY_INITIAL_DELAY_SECONDS
                    retry_reporter.finish()
                except Exception as exc:
                    if not is_transient_upload_error(exc):
                        raise
                    retry_reporter.mark_retry(
                        rel_path=job_id,
                        retry_count=retry_reporter.total_retries + 1,
                        retry_delay=retry_delay,
                        exc=exc,
                    )
                    time.sleep(retry_delay)
                    retry_delay = next_upload_retry_delay(retry_delay)
                    continue
                state = str(job.get("state") or "")
                renderer.update(job, force=state in {"succeeded", "failed", "cancelled"})
                if state in {"succeeded", "failed", "cancelled"}:
                    retry_reporter.finish()
                    return job
                time.sleep(interval)
