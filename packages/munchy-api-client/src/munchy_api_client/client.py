from __future__ import annotations

import builtins
import errno
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock
from typing import Any

import httpx
from file_download import DownloadProgress, verified_download
from http_api_contracts import parse_error_payload, safe_http_base_url
from lifecycle_events import EventPage
from tus_transport import DEFAULT_TUS_UPLOAD_CHUNK_MIB, TusHttpError, TusTransport

DEFAULT_BASE_URL = "http://127.0.0.1:8092"
BASE_URL_ENV = "MUNCHY_BASE_URL"
TOKEN_ENV = "MUNCHY_TOKEN"
ADMIN_TOKEN_ENV = "MUNCHY_ADMIN_TOKEN"
DEFAULT_HTTP_TIMEOUT_SECONDS = 300.0
PROGRESS_ENV = "MUNCHY_PROGRESS"
KEEP_AWAKE_ENV = "MUNCHY_KEEP_AWAKE"
DEFAULT_UPLOAD_WORKERS = 12
UPLOAD_RETRY_INITIAL_DELAY_SECONDS = 1.0
UPLOAD_RETRY_MAX_DELAY_SECONDS = 60.0
UPLOAD_RETRY_NOTICE_SECONDS = 60.0
UPLOAD_PROGRESS_LIVE_RENDER_INTERVAL_SECONDS = 1.0
UPLOAD_PROGRESS_TEXT_RENDER_INTERVAL_SECONDS = 15.0
UPLOAD_JOB_STATUS_INTERVAL_SECONDS = 5.0
TRANSIENT_ISSUE_RECOVERY_DISPLAY_SECONDS = 8.0
CLEANUP_REQUEST_TIMEOUT_SECONDS = 900.0
TRANSIENT_UPLOAD_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504, 507}
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
FIELD_STYLE = "bold #c0ad6c"
ENTITY_ID_STYLE = "bold #8ec9cc"
ATTENTION_STYLE = "bold #ff8933"
_KEEP_AWAKE_LOCK = Lock()
_KEEP_AWAKE_DEPTH = 0
_KEEP_AWAKE_PROCESS: subprocess.Popen[bytes] | None = None


@dataclass(frozen=True)
class SubmissionInputFile:
    source: Path
    rel_path: str
    bytes: int
    sha256: str
    provenance: dict[str, object] = field(default_factory=dict)
    provenance_journals: dict[str, builtins.bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class SubmissionPreflightInputFile:
    rel_path: str
    bytes: int


@dataclass(frozen=True)
class SubmissionPreflightRequest:
    template_id: str
    files: tuple[SubmissionPreflightInputFile, ...]
    inputs: dict[str, str] = field(default_factory=dict)
    run_id: str | None = None
    handoff_on_failure: str = "preserve_for_resume"
    event_context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubmissionUploadRequest:
    submission_id: str
    template_id: str
    files: tuple[SubmissionInputFile, ...]
    inputs: dict[str, str] = field(default_factory=dict)
    run_id: str | None = None
    handoff_on_failure: str = "preserve_for_resume"
    event_context: dict[str, Any] = field(default_factory=dict)
    upload_workers: int = DEFAULT_UPLOAD_WORKERS
    upload_chunk_mib: int = DEFAULT_TUS_UPLOAD_CHUNK_MIB

    @property
    def upload_chunk_bytes(self) -> int:
        return self.upload_chunk_mib * 1024 * 1024


UploadRequest = SubmissionUploadRequest


def submission_preflight_request(
    request: SubmissionUploadRequest,
) -> SubmissionPreflightRequest:
    return SubmissionPreflightRequest(
        template_id=request.template_id,
        files=tuple(
            SubmissionPreflightInputFile(rel_path=item.rel_path, bytes=item.bytes)
            for item in request.files
        ),
        inputs=dict(request.inputs),
        run_id=request.run_id,
        handoff_on_failure=request.handoff_on_failure,
        event_context=dict(request.event_context),
    )


def server_url_setting(
    server_url: str | None = None,
    *,
    allow_insecure_http: bool | None = None,
) -> str:
    return safe_http_base_url(
        server_url or os.getenv(BASE_URL_ENV) or DEFAULT_BASE_URL,
        setting=BASE_URL_ENV,
        allow_insecure_http=(
            _bool_env("MUNCHY_ALLOW_INSECURE_HTTP", False)
            if allow_insecure_http is None
            else allow_insecure_http
        ),
    )


def token_setting(token: str | None = None) -> str:
    return (token or os.getenv(TOKEN_ENV) or "").strip()


def admin_token_setting(token: str | None = None) -> str:
    return (token or os.getenv(ADMIN_TOKEN_ENV) or "").strip()


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _http_timeout_seconds() -> float:
    raw = os.getenv("MUNCHY_HTTP_TIMEOUT_SECONDS", str(DEFAULT_HTTP_TIMEOUT_SECONDS))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("MUNCHY_HTTP_TIMEOUT_SECONDS must be a positive number") from exc
    if value <= 0:
        raise ValueError("MUNCHY_HTTP_TIMEOUT_SECONDS must be a positive number")
    return value


def submission_payload(
    request: SubmissionUploadRequest,
    *,
    include_id: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "template_id": request.template_id,
        "inputs": request.inputs,
        "files": [
            {
                "path": item.rel_path,
                "bytes": item.bytes,
                "provenance": item.provenance,
                **({"sha256": item.sha256} if item.sha256 else {}),
            }
            for item in request.files
        ],
        "handoff_on_failure": request.handoff_on_failure,
    }
    if include_id:
        payload["submission_id"] = request.submission_id
    for key, value in (("run_id", request.run_id),):
        if value is not None:
            payload[key] = value
    if request.event_context:
        payload["event_context"] = request.event_context
    return payload


def submission_preflight_payload(request: SubmissionPreflightRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "template_id": request.template_id,
        "inputs": request.inputs,
        "files": [
            {
                "path": item.rel_path,
                "bytes": item.bytes,
            }
            for item in request.files
        ],
        "handoff_on_failure": request.handoff_on_failure,
    }
    if request.run_id is not None:
        payload["run_id"] = request.run_id
    if request.event_context:
        payload["event_context"] = request.event_context
    return payload


def keep_awake_enabled() -> bool:
    if sys.platform != "darwin":
        return False
    return os.getenv(KEEP_AWAKE_ENV, "1").strip().casefold() not in {
        "0",
        "false",
        "no",
        "off",
    }


def start_keep_awake(reason: str) -> None:
    global _KEEP_AWAKE_DEPTH, _KEEP_AWAKE_PROCESS
    if not keep_awake_enabled():
        return
    with _KEEP_AWAKE_LOCK:
        if _KEEP_AWAKE_DEPTH == 0:
            try:
                _KEEP_AWAKE_PROCESS = subprocess.Popen(
                    ["caffeinate", "-dimsu", "-w", str(os.getpid())],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                _KEEP_AWAKE_PROCESS = None
                print(f"warning: could not start caffeinate for {reason}: {exc}", file=sys.stderr)
        _KEEP_AWAKE_DEPTH += 1


def stop_keep_awake() -> None:
    global _KEEP_AWAKE_DEPTH, _KEEP_AWAKE_PROCESS
    with _KEEP_AWAKE_LOCK:
        if _KEEP_AWAKE_DEPTH <= 0:
            return
        _KEEP_AWAKE_DEPTH -= 1
        if _KEEP_AWAKE_DEPTH > 0:
            return
        proc = _KEEP_AWAKE_PROCESS
        _KEEP_AWAKE_PROCESS = None
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


@contextmanager
def keep_system_awake(reason: str = "munchy job") -> Any:
    start_keep_awake(reason)
    try:
        yield
    finally:
        stop_keep_awake()


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


def format_http_body(status: int, body: bytes) -> str:
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(parsed, dict):
        return text
    code, message, details = parse_error_payload(parsed, fallback_message=text)
    if code == "invalid_response":
        return text
    if status == 507 and code == "insufficient_storage":
        label = str(details.get("label") or "storage").strip()
        required = int(details.get("required_bytes") or 0)
        free = int(details.get("free_bytes") or 0)
        reserved = int(details.get("reserved_bytes") or 0)
        parts = [
            f"insufficient storage for {label}",
            f"need {format_bytes(required)} free",
            f"have {format_bytes(free)}",
        ]
        if reserved:
            parts.append(f"{format_bytes(reserved)} reserved by active uploads")
        return "; ".join(parts)
    return message


class MunchyHttpError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: bytes) -> None:
        text = format_http_body(status, body)
        message = f"{method} {url} returned HTTP {status}"
        if text:
            message = f"{message}: {text}"
        super().__init__(message)
        self.method = method
        self.url = url
        self.status = status
        self.body = body
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        self.code, self.message, self.details = parse_error_payload(
            payload,
            fallback_message=text or f"HTTP {status}",
        )


class JobTerminalDuringUpload(RuntimeError):
    def __init__(self, job: dict[str, Any]) -> None:
        self.job = job
        super().__init__(format_job_failure(job, label="Munchy job"))


def job_should_stop_upload(job: dict[str, Any]) -> bool:
    state = str(job.get("state") or "").lower()
    phase = str(job.get("phase") or "").lower()
    return (
        bool(job.get("cancel_requested"))
        or state in {"failed", "canceled", "cancel_requested"}
        or phase in {"canceled", "cancel_requested"}
    )


def is_transient_upload_error(exc: BaseException) -> bool:
    if isinstance(exc, MunchyHttpError):
        if exc.status == 400 and b"ERR_UPLOAD_INTERRUPTED" in exc.body:
            return True
        return exc.status in TRANSIENT_UPLOAD_HTTP_STATUSES
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError, http.client.HTTPException, socket.timeout)):
        return True
    if isinstance(exc, OSError):
        return exc.errno in TRANSIENT_UPLOAD_ERRNOS
    return False


def upload_was_removed(exc: BaseException) -> bool:
    return (
        isinstance(exc, MunchyHttpError)
        and exc.status == 404
        and b"ERR_UPLOAD_NOT_FOUND" in exc.body
    )


def next_upload_retry_delay(current_delay: float) -> float:
    return min(
        max(current_delay, UPLOAD_RETRY_INITIAL_DELAY_SECONDS) * 2.0,
        UPLOAD_RETRY_MAX_DELAY_SECONDS,
    )


class UploadRetryReporter:
    def __init__(self, *, label: str = "upload", renderer: ProgressRenderer | None = None) -> None:
        self.label = label
        self.renderer = renderer
        self.total_retries = 0
        self.files: set[str] = set()
        self.started_at: float | None = None
        self.last_notice = 0.0
        self.latest_path = ""
        self.latest_error = ""
        self.lock = Lock()

    def bind_renderer(self, renderer: ProgressRenderer) -> None:
        self.renderer = renderer

    def payload(
        self,
        *,
        rel_path: str,
        retry_count: int,
        retry_delay: float,
        exc: BaseException,
    ) -> dict[str, Any]:
        return {
            "label": self.label,
            "retries": self.total_retries,
            "files": len(self.files),
            "path": rel_path,
            "retry_count": retry_count,
            "next_retry_seconds": retry_delay,
            "error": short_error(exc),
        }

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
            if self.renderer is not None and self.renderer.is_live:
                self.renderer.update(
                    {
                        "transient_issue": self.payload(
                            rel_path=rel_path,
                            retry_count=retry_count,
                            retry_delay=retry_delay,
                            exc=exc,
                        )
                    },
                    force=True,
                )
                self.last_notice = now
                return
            elapsed = now - self.started_at
            print(
                (
                    f"{self.label} retrying after transient issue: "
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
            if self.renderer is not None and self.renderer.is_live:
                if getattr(self.renderer, "started", True):
                    self.renderer.update(
                        {
                            "transient_issue": {
                                "label": self.label,
                                "retries": self.total_retries,
                                "files": len(self.files),
                                "message": "recovered from transient issues",
                            }
                        },
                        force=True,
                    )
            else:
                print(
                    (
                        f"{self.label} recovered from transient issues: "
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
    clips_total = int(progress.get("clips_total") or 0)
    if clips_total:
        mode = str(progress.get("mode") or progress.get("task") or "review")
        label = "remote audio review" if mode == "audio_review" else "remote review"
        clips_done = int(progress.get("clips_done") or 0)
        clips_running = int(progress.get("clips_running") or 0)
        clips_failed = int(progress.get("clips_failed") or 0)
        pct = float(progress.get("percent_clips") or 0.0)
        output_bytes = int(progress.get("output_bytes") or 0)
        active_output_bytes = int(progress.get("active_output_bytes") or 0)
        output_rate = int(progress.get("output_rate_bytes_per_second") or 0)
        phase = str(progress.get("phase") or "").strip()
        parts = [
            f"{label} {clips_done}/{clips_total} clips",
            f"{pct:.2f}%",
        ]
        if phase and phase != "done":
            parts.append(phase.replace("_", " "))
        if output_rate:
            parts.append(f"{format_rate(output_rate)} output")
        if output_bytes:
            parts.append(f"{format_bytes(output_bytes)} written")
        if clips_running:
            parts.append(f"{clips_running} active")
        if active_output_bytes:
            parts.append(f"{format_bytes(active_output_bytes)} active output")
        if clips_failed:
            parts.append(f"{clips_failed} failed")
        return ", ".join(parts)

    files_total = int(progress.get("files_total") or 0)
    files_encoded = int(progress.get("files_encoded") or 0)
    files_encoding = int(progress.get("files_encoding") or 0)
    files_failed = int(progress.get("files_failed") or 0)
    file_pct = float(progress.get("percent_files") or 0.0)
    input_pct = float(progress.get("percent_input_bytes") or file_pct)
    input_done = int(progress.get("input_bytes_encoded") or 0)
    input_total = int(progress.get("input_bytes_total") or 0)
    output_bytes = int(progress.get("output_bytes") or 0)
    active_output_bytes = int(progress.get("active_output_bytes") or 0)
    input_rate = int(progress.get("input_rate_bytes_per_second") or 0)
    output_rate = int(progress.get("output_rate_bytes_per_second") or 0)
    running_batches = int(progress.get("running_batches") or 0)
    pipeline_batches = int(progress.get("pipeline_batches") or 0)
    parts = [
        f"remote encode {files_encoded}/{files_total} files",
        f"{file_pct:.2f}% files",
        f"{format_progress_bytes(input_done, input_total)} input",
        f"{input_pct:.2f}% input",
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
    pct = (
        (uploaded_bytes / bytes_total * 100.0)
        if bytes_total
        else progress_percent(
            progress,
            percent_key="percent_bytes",
        )
    )
    rate = int(progress.get("rate_bytes_per_second") or 0)
    parts = [
        f"remote upload {files_uploaded}/{files_total} files",
        format_progress_bytes(uploaded_bytes, bytes_total),
        f"{pct:.2f}%",
    ]
    if rate:
        parts.append(format_rate(rate))
    return ", ".join(parts)


def format_handoff_progress(progress: dict[str, Any]) -> str:
    stages = progress.get("stages")
    if not isinstance(stages, list):
        return "handoff: waiting"
    rendered = [format_handoff_stage(stage) for stage in stages if isinstance(stage, dict)]
    return " | ".join(rendered) if rendered else "handoff: waiting"


def format_handoff_stage(stage: dict[str, Any]) -> str:
    parts = [str(stage.get("label") or "Handoff")]
    state = str(stage.get("state") or "").strip()
    if state:
        parts.append(state)
    items_done = int(stage.get("items_done") or 0)
    items_total = int(stage.get("items_total") or 0)
    if items_total:
        item_label = str(stage.get("item_label") or "items")
        parts.append(f"{items_done}/{items_total} {item_label}")
    bytes_done = int(stage.get("bytes_done") or 0)
    bytes_total = int(stage.get("bytes_total") or 0)
    if bytes_done or bytes_total:
        parts.append(format_progress_bytes(bytes_done, bytes_total))
    rate = int(stage.get("rate_bytes_per_second") or 0)
    if rate:
        parts.append(format_rate(rate))
    return ", ".join(parts)


def job_finished_cleanly(job: dict[str, Any]) -> bool:
    if job.get("state") != "succeeded":
        return False
    handoff = job.get("handoff")
    return isinstance(handoff, dict) and bool(handoff.get("safe_to_delete"))


def handoff_safe_to_delete(job: dict[str, Any]) -> bool:
    handoff = job.get("handoff")
    return isinstance(handoff, dict) and bool(handoff.get("safe_to_delete"))


def handoff_failed(job: dict[str, Any]) -> bool:
    handoff = job.get("handoff")
    return isinstance(handoff, dict) and str(handoff.get("state") or "") == "failed"


def input_tree_progress(upload_progress: dict[str, Any]) -> dict[str, Any] | None:
    if (
        "input_tree_files_ready" not in upload_progress
        and "input_tree_bytes_ready" not in upload_progress
    ):
        return None
    files_total = int(
        upload_progress.get("input_tree_files_total") or upload_progress.get("files_total") or 0
    )
    bytes_total = int(
        upload_progress.get("input_tree_bytes_total") or upload_progress.get("bytes_total") or 0
    )
    files_done = min(int(upload_progress.get("input_tree_files_ready") or 0), files_total)
    bytes_done = min(int(upload_progress.get("input_tree_bytes_ready") or 0), bytes_total)
    if files_total <= 0 and bytes_total <= 0:
        return None
    if (
        files_total > 0
        and files_done >= files_total
        and (bytes_total <= 0 or bytes_done >= bytes_total)
    ):
        return None
    percent = (bytes_done / bytes_total * 100.0) if bytes_total else 0.0
    return {
        "files_done": files_done,
        "files_total": files_total,
        "bytes_done": bytes_done,
        "bytes_total": bytes_total,
        "percent_bytes": percent,
    }


def format_input_tree_progress(progress: dict[str, Any]) -> str:
    files_done = int(progress.get("files_done") or 0)
    files_total = int(progress.get("files_total") or 0)
    bytes_done = int(progress.get("bytes_done") or 0)
    bytes_total = int(progress.get("bytes_total") or 0)
    pct = progress_percent(progress, percent_key="percent_bytes")
    return (
        f"remote input tree {files_done}/{files_total} files, "
        f"{format_progress_bytes(bytes_done, bytes_total)}, {pct:.2f}%"
    )


def _float_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ratio_percent(done: Any, total: Any) -> float | None:
    done_value = _float_value(done)
    total_value = _float_value(total)
    if done_value is None or total_value is None or total_value <= 0:
        return None
    return done_value / total_value * 100.0


def progress_percent(progress: dict[str, Any], *, percent_key: str) -> float:
    uploaded_bytes_percent = _ratio_percent(
        progress.get("uploaded_bytes"),
        progress.get("bytes_total"),
    )
    bytes_done_percent = _ratio_percent(progress.get("bytes_done"), progress.get("bytes_total"))
    if percent_key == "percent_bytes":
        computed_byte_percent = uploaded_bytes_percent
        if computed_byte_percent is None:
            computed_byte_percent = bytes_done_percent
        if computed_byte_percent is not None:
            return max(0.0, min(100.0, computed_byte_percent))

    configured = _float_value(progress.get(percent_key))
    if configured is None:
        configured = _float_value(progress.get("percent_clips"))
    if configured is None:
        configured = _float_value(progress.get("percent_files"))

    computed_candidates = (
        uploaded_bytes_percent,
        bytes_done_percent,
        _ratio_percent(progress.get("input_bytes_encoded"), progress.get("input_bytes_total")),
        _ratio_percent(progress.get("clips_done"), progress.get("clips_total")),
        _ratio_percent(progress.get("files_encoded"), progress.get("files_total")),
        _ratio_percent(progress.get("files_uploaded"), progress.get("files_total")),
        _ratio_percent(progress.get("files_done"), progress.get("files_total")),
    )
    computed = next((pct for pct in computed_candidates if pct is not None), None)
    if configured is None or (configured <= 0.0 and computed is not None and computed > 0.0):
        configured = computed
    return max(0.0, min(100.0, configured or 0.0))


def local_progress_items(job: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    progress = job.get("local_progress")
    if isinstance(progress, dict):
        items: list[tuple[str, dict[str, Any]]] = []
        for stage in ("hash", "preflight"):
            item = progress.get(stage)
            if isinstance(item, dict):
                items.append((stage, item))
        for stage, item in progress.items():
            if stage in {"hash", "preflight"} or not isinstance(item, dict):
                continue
            items.append((str(stage), item))
        return items
    if isinstance(progress, list):
        return [
            (str(item.get("stage") or "local"), item) for item in progress if isinstance(item, dict)
        ]
    return []


def format_local_progress(stage: str, progress: dict[str, Any]) -> str:
    label = str(progress.get("label") or stage).replace("_", " ").strip() or "local"
    files_done = int(progress.get("files_done") or 0)
    files_total = int(progress.get("files_total") or 0)
    bytes_done = int(progress.get("bytes_done") or 0)
    bytes_total = int(progress.get("bytes_total") or 0)
    pct = float(
        progress.get("percent_bytes")
        or ((bytes_done / bytes_total * 100.0) if bytes_total else 100.0)
    )
    parts = [
        f"local {label} {files_done}/{files_total} files",
        format_progress_bytes(bytes_done, bytes_total),
        f"{pct:.2f}%",
    ]
    rate = int(progress.get("rate_bytes_per_second") or 0)
    if rate:
        suffix = str(progress.get("rate_label") or "").strip()
        parts.append(f"{format_rate(rate)}{(' ' + suffix) if suffix else ''}")
    elapsed = float(progress.get("elapsed_seconds") or 0.0)
    if elapsed >= 1:
        parts.append(f"{elapsed:.1f}s")
    cache_hits = int(progress.get("cache_hits") or 0)
    cache_misses = int(progress.get("cache_misses") or 0)
    cache_writes = int(progress.get("cache_writes") or 0)
    cache_seeded = int(progress.get("cache_seeded") or 0)
    if cache_hits or cache_misses or cache_writes or cache_seeded:
        cache = f"cache hits {cache_hits}, misses {cache_misses}"
        if cache_writes:
            cache += f", writes {cache_writes}"
        if cache_seeded:
            cache += f", seeded {cache_seeded}"
        parts.append(cache)
    failures = int(progress.get("failures") or progress.get("failed_files") or 0)
    if failures:
        parts.append(f"{failures} failed")
    message = str(progress.get("message") or "").strip()
    if message:
        parts.append(message)
    return ", ".join(parts)


def job_upload_progress(job: dict[str, Any]) -> dict[str, Any] | None:
    upload_progress = job.get("upload_progress")
    return upload_progress if isinstance(upload_progress, dict) else None


def format_progress_status_line(job: dict[str, Any]) -> str:
    pieces: list[str] = []
    for stage, progress in local_progress_items(job):
        pieces.append(format_local_progress(stage, progress))
    upload_progress = job_upload_progress(job)
    if upload_progress is not None:
        pieces.append(format_input_upload_progress(upload_progress))
        tree_progress = input_tree_progress(upload_progress)
        if tree_progress is not None:
            pieces.append(format_input_tree_progress(tree_progress))
    encode_progress = job.get("encode_progress")
    if isinstance(encode_progress, dict):
        pieces.append(format_encode_progress(encode_progress))
    handoff_progress = job.get("handoff_progress")
    if isinstance(handoff_progress, dict):
        pieces.append(format_handoff_progress(handoff_progress))
    issue = job.get("transient_issue")
    if isinstance(issue, dict):
        pieces.append(format_transient_issue(issue))
    return " | ".join(pieces) if pieces else "progress: waiting"


def format_job_status_line(job: dict[str, Any]) -> str:
    state = str(job.get("state") or "unknown")
    phase = str(job.get("phase") or "").strip()
    pieces = [f"job: {state}"]
    if phase:
        pieces.append(phase)
    if job.get("cleanup_completed_at"):
        removed = job.get("cleanup_removed")
        if isinstance(removed, list) and removed:
            pieces.append(f"cleanup complete ({len(removed)} item(s) removed)")
        elif job.get("cleanup_removed_count"):
            pieces.append(f"cleanup complete ({int(job['cleanup_removed_count'])} item(s) removed)")
        else:
            pieces.append("cleanup complete")
    queue = job.get("queue")
    if state == "queued" and isinstance(queue, dict):
        try:
            position = int(queue.get("position") or 0)
            running = int(queue.get("running_jobs") or 0)
            scheduled = int(queue.get("scheduled_jobs") or 0)
            limit = int(queue.get("running_job_limit") or 0)
        except (TypeError, ValueError):
            position = running = scheduled = limit = 0
        if position:
            if limit > 0:
                pieces.append(
                    f"encoder queue position {position} "
                    f"({running + scheduled}/{limit} running or starting)"
                )
            else:
                pieces.append(f"encoder queue position {position}")
    storage_wait = job.get("storage_wait")
    if isinstance(storage_wait, dict):
        label = str(storage_wait.get("label") or "storage").replace("_", " ")
        pieces.append(f"waiting for {label}")
    progress = format_progress_status_line(job)
    if progress != "progress: waiting":
        pieces.append(progress)
    return " | ".join(pieces)


def format_job_created_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown-time"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def format_job_identity(job: dict[str, Any]) -> str:
    template_id = str(job.get("template_id") or "unknown-template")
    return f"{template_id} · {format_job_created_timestamp(job.get('created_at'))}"


def format_job_summary_line(job: dict[str, Any]) -> str:
    job_id = str(job.get("job_id") or job.get("id") or "unknown")
    prefix = f"{format_job_identity(job)} [job {job_id}]"
    return f"{prefix} | {format_job_status_line(job)}"


JOB_FAILURE_DETAIL_KEYS = {
    "error",
    "error_code",
    "failed_reason",
    "latest_failure",
    "message",
    "reason",
}


def _format_failure_detail(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float, bool)):
        text = str(value)
    elif isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    text = " ".join(text.strip().split())
    return short_path(text, max_len=220)


def _job_failure_details(value: Any, *, prefix: str = "", limit: int = 6) -> list[tuple[str, str]]:
    if limit <= 0:
        return []
    details: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(key) in JOB_FAILURE_DETAIL_KEYS:
                detail = _format_failure_detail(item)
                if detail:
                    details.append((path, detail))
                    if len(details) >= limit:
                        return details
            if isinstance(item, (dict, list)):
                details.extend(_job_failure_details(item, prefix=path, limit=limit - len(details)))
                if len(details) >= limit:
                    return details
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if not isinstance(item, (dict, list)):
                continue
            details.extend(
                _job_failure_details(item, prefix=f"{prefix}[{index}]", limit=limit - len(details))
            )
            if len(details) >= limit:
                return details
    return details


def format_job_failure(job: dict[str, Any], *, label: str = "job") -> str:
    lines = [f"{label} did not succeed:"]
    job_id = str(job.get("job_id") or job.get("id") or "").strip()
    if job_id:
        lines.append(f"- job: {job_id}")
    lines.append(f"- identity: {format_job_identity(job)}")
    review = job.get("review")
    if isinstance(review, dict):
        route_id = str(review.get("route_id") or "").strip()
        profile_id = str(review.get("profile_id") or "").strip()
        if route_id:
            lines.append(f"- route: {route_id}")
        if profile_id:
            lines.append(f"- profile: {profile_id}")
    lines.append(f"- status: {format_job_status_line(job)}")

    seen: set[str] = set()
    for path, detail in _job_failure_details(job):
        if detail in seen:
            continue
        seen.add(detail)
        label_path = path.replace("_", " ")
        lines.append(f"- {label_path}: {detail}")
    return "\n".join(lines)


def compact_job_failure(job: dict[str, Any], *, label: str = "Munchy job") -> str:
    for _path, detail in _job_failure_details(job, limit=1):
        return f"{label} failed: {detail}"
    state = str(job.get("state") or "unknown").strip()
    phase = str(job.get("phase") or "").strip()
    status = state + (f" during {phase}" if phase else "")
    return f"{label} did not finish safely ({status})"


class ProgressRenderer:
    include_job: bool
    is_live: bool = False

    @classmethod
    def plain(cls, *, include_job: bool) -> ProgressRenderer:
        return PlainProgressRenderer(include_job=include_job)

    def __enter__(self) -> ProgressRenderer:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
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
    is_live = True

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
            transient=True,
        )
        self.started = False
        self.current_job: dict[str, Any] = {}
        self.transient_issue: dict[str, Any] | None = None
        self.transient_issue_expires_at: float | None = None
        self.transient_issue_clear_after_uploaded_bytes: int | None = None

    def __enter__(self) -> RichProgressRenderer:
        self.live.start(refresh=True)
        self.started = True
        return self

    def update(self, job: dict[str, Any], *, force: bool = False) -> None:
        if not self.started:
            self.__enter__()
        now = time.monotonic()
        self._prune_transient_issue(now)
        update = dict(job)
        if "transient_issue" in update:
            issue = update.pop("transient_issue")
            if isinstance(issue, dict):
                self.transient_issue = issue
                self.transient_issue_expires_at = self._transient_issue_expiry(issue, now=now)
                self.transient_issue_clear_after_uploaded_bytes = self._current_uploaded_bytes()
            else:
                self._clear_transient_issue()
        if update:
            self.current_job.update(update)
        self._clear_transient_issue_if_upload_advanced()
        self.live.update(self._render(self._render_job()), refresh=True)

    def stop(self) -> None:
        if self.started:
            self.live.stop()
            self.started = False

    def _transient_issue_expiry(self, issue: dict[str, Any], *, now: float) -> float | None:
        message = str(issue.get("message") or "").strip().lower()
        if message.startswith("recovered"):
            return now + TRANSIENT_ISSUE_RECOVERY_DISPLAY_SECONDS
        return None

    def _prune_transient_issue(self, now: float) -> None:
        if (
            self.transient_issue is not None
            and self.transient_issue_expires_at is not None
            and now >= self.transient_issue_expires_at
        ):
            self._clear_transient_issue()

    def _clear_transient_issue(self) -> None:
        self.transient_issue = None
        self.transient_issue_expires_at = None
        self.transient_issue_clear_after_uploaded_bytes = None

    def _current_uploaded_bytes(self) -> int | None:
        progress = job_upload_progress(self.current_job)
        if progress is None:
            return None
        try:
            return int(progress.get("uploaded_bytes") or 0)
        except (TypeError, ValueError):
            return None

    def _clear_transient_issue_if_upload_advanced(self) -> None:
        if self.transient_issue is None or self.transient_issue_clear_after_uploaded_bytes is None:
            return
        uploaded_bytes = self._current_uploaded_bytes()
        if (
            uploaded_bytes is not None
            and uploaded_bytes > self.transient_issue_clear_after_uploaded_bytes
        ):
            self._clear_transient_issue()

    def _render_job(self) -> dict[str, Any]:
        job = dict(self.current_job)
        if self.transient_issue is not None:
            job["transient_issue"] = self.transient_issue
        return job

    def _render(self, job: dict[str, Any]) -> Any:
        from rich import box
        from rich.panel import Panel
        from rich.table import Table

        table = Table.grid(padding=(0, 2))
        table.add_column(justify="right", style=FIELD_STYLE, no_wrap=True, width=24)
        table.add_column(ratio=1)

        state = str(job.get("state") or "running")
        phase = str(job.get("phase") or "").strip()
        if self.include_job:
            table.add_row("State", state)
            if phase:
                table.add_row("Phase", phase)

        for stage, progress in local_progress_items(job):
            label = f"Local {str(progress.get('label') or stage).replace('_', ' ').title()}"
            table.add_row(label, self._bar(progress, percent_key="percent_bytes"))
            table.add_row("", format_local_progress(stage, progress))

        upload_progress = job_upload_progress(job)
        if upload_progress is not None:
            table.add_row("Remote Upload", self._bar(upload_progress, percent_key="percent_bytes"))
            table.add_row("", format_input_upload_progress(upload_progress))
            tree_progress = input_tree_progress(upload_progress)
            if tree_progress is not None:
                table.add_row(
                    "Remote Input Tree",
                    self._bar(tree_progress, percent_key="percent_bytes"),
                )
                table.add_row("", format_input_tree_progress(tree_progress))

        encode_progress = job.get("encode_progress")
        if isinstance(encode_progress, dict):
            label = (
                "Remote Review" if int(encode_progress.get("clips_total") or 0) else "Remote Encode"
            )
            encode_percent_key = (
                "percent_clips" if int(encode_progress.get("clips_total") or 0) else "percent_files"
            )
            table.add_row(label, self._bar(encode_progress, percent_key=encode_percent_key))
            table.add_row("", format_encode_progress(encode_progress))

        handoff_progress = job.get("handoff_progress")
        if isinstance(handoff_progress, dict):
            stages = handoff_progress.get("stages")
            if isinstance(stages, list):
                for stage in stages:
                    if not isinstance(stage, dict):
                        continue
                    table.add_row(
                        str(stage.get("label") or "Handoff"),
                        self._bar(stage, percent_key="percent_bytes"),
                    )
                    table.add_row("", format_handoff_stage(stage))

        issue = job.get("transient_issue")
        issue = issue if isinstance(issue, dict) else None
        table.add_row(
            self._transient_issue_label(issue),
            self._transient_issue_renderable(issue),
        )

        if (
            not local_progress_items(job)
            and upload_progress is None
            and not isinstance(encode_progress, dict)
            and not isinstance(handoff_progress, dict)
        ):
            table.add_row("Progress", "waiting")

        return Panel(table, title=self.title, border_style=ENTITY_ID_STYLE, box=box.ROUNDED)

    def _transient_issue_renderable(self, issue: dict[str, Any] | None) -> Any:
        from rich.text import Text

        if issue is None:
            return Text(" ", no_wrap=True, overflow="ellipsis")
        return Text(
            format_transient_issue(issue),
            style=ATTENTION_STYLE,
            no_wrap=True,
            overflow="ellipsis",
        )

    def _transient_issue_label(self, issue: dict[str, Any] | None) -> str:
        if issue is None:
            return ""
        label = str(issue.get("label") or "").strip()
        if not label:
            return "Transient Issue"
        return f"{label.replace('_', ' ').title()} Issue"

    def _bar(self, progress: dict[str, Any], *, percent_key: str) -> Any:
        from rich.progress import BarColumn, Progress, TextColumn

        pct = progress_percent(progress, percent_key=percent_key)
        bar = Progress(
            TextColumn(""),
            BarColumn(bar_width=None),
            TextColumn(f"{pct:.2f}%"),
            expand=True,
        )
        bar.add_task("", total=100, completed=int(pct))
        return bar


def format_transient_issue(issue: dict[str, Any]) -> str:
    label = str(issue.get("label") or "remote").strip() or "remote"
    retries = int(issue.get("retries") or issue.get("retry_count") or 0)
    files = int(issue.get("files") or 0)
    next_retry = issue.get("next_retry_seconds")
    error = str(issue.get("error") or "").strip()
    message = str(issue.get("message") or "").strip()
    path = short_path(str(issue.get("path") or ""), max_len=56)
    parts = [f"{label} retry {retries}"]
    if files:
        parts.append(f"{files} file(s)")
    if next_retry is not None:
        try:
            parts.append(f"next in {float(next_retry):.0f}s")
        except (TypeError, ValueError):
            pass
    if error:
        parts.append(short_path(error, max_len=96))
    if message:
        parts.append(message)
    if path:
        parts.append(path)
    return ", ".join(parts)


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
        completed_files: int = 0,
        completed_bytes: int = 0,
        job_status_provider: Callable[[], dict[str, Any] | None] | None = None,
        renderer: ProgressRenderer | None = None,
        stop_event: Event | None = None,
    ) -> None:
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.completed_files = completed_files
        self.completed_bytes = completed_bytes
        self.initial_completed_bytes = completed_bytes
        self.started_at = time.monotonic()
        self.last_printed_at = self.started_at
        self.lock = Lock()
        self.job_status_provider = job_status_provider
        self.renderer = renderer or ProgressRenderer.plain(include_job=False)
        self.last_remote_uploaded_files: int | None = None
        self.last_remote_uploaded_bytes: int | None = None
        self.last_remote_rate_at: float | None = None
        self.last_job_checked_at = 0.0
        self.rendered_any = False
        self.inflight_uploaded_bytes: dict[str, int] = {}
        self.stop_event = stop_event

    def current_uploaded_bytes(self) -> int:
        return min(
            self.completed_bytes + sum(self.inflight_uploaded_bytes.values()),
            self.total_bytes,
        )

    def local_upload_progress(self, *, now: float) -> dict[str, Any]:
        uploaded_bytes = self.current_uploaded_bytes()
        elapsed = max(now - self.started_at, 0.001)
        session_uploaded_bytes = max(uploaded_bytes - self.initial_completed_bytes, 0)
        return {
            "files_uploaded": self.completed_files,
            "files_total": self.total_files,
            "uploaded_bytes": uploaded_bytes,
            "bytes_total": self.total_bytes,
            "rate_bytes_per_second": int(session_uploaded_bytes / elapsed),
        }

    def remote_upload_progress_with_rate(
        self,
        remote_upload_progress: dict[str, Any],
        *,
        now: float,
    ) -> dict[str, Any]:
        merged = dict(remote_upload_progress)
        existing_rate = merged.pop("rate_bytes_per_second", None)
        try:
            files_total = int(merged.get("files_total") or self.total_files)
        except (TypeError, ValueError):
            files_total = self.total_files
        try:
            bytes_total = int(merged.get("bytes_total") or self.total_bytes)
        except (TypeError, ValueError):
            bytes_total = self.total_bytes
        try:
            remote_files = int(merged.get("files_uploaded") or 0)
        except (TypeError, ValueError):
            remote_files = 0
        try:
            remote_uploaded_bytes = int(merged.get("uploaded_bytes") or 0)
        except (TypeError, ValueError):
            remote_uploaded_bytes = 0
        previous_files = self.last_remote_uploaded_files
        previous_bytes = self.last_remote_uploaded_bytes
        local_uploaded_bytes = self.current_uploaded_bytes()
        uploaded_files = min(
            max(remote_files, self.completed_files, previous_files or 0),
            files_total,
        )
        uploaded_bytes = min(
            max(remote_uploaded_bytes, local_uploaded_bytes, previous_bytes or 0),
            bytes_total,
        )
        merged["files_uploaded"] = uploaded_files
        merged["files_total"] = files_total
        merged["uploaded_bytes"] = uploaded_bytes
        merged["bytes_total"] = bytes_total
        if (
            previous_bytes is not None
            and self.last_remote_rate_at is not None
            and uploaded_bytes > previous_bytes
        ):
            elapsed = max(now - self.last_remote_rate_at, 0.001)
            merged["rate_bytes_per_second"] = int((uploaded_bytes - previous_bytes) / elapsed)
        elif existing_rate:
            try:
                merged["rate_bytes_per_second"] = int(existing_rate)
            except (TypeError, ValueError):
                pass
        if previous_bytes is None or uploaded_bytes > previous_bytes:
            self.last_remote_uploaded_bytes = uploaded_bytes
            self.last_remote_rate_at = now
        if previous_files is None or uploaded_files > previous_files:
            self.last_remote_uploaded_files = uploaded_files
        return merged

    def mark_uploaded(self, item: SubmissionInputFile, uploaded_bytes: int) -> None:
        with self.lock:
            clamped = max(0, min(int(uploaded_bytes), item.bytes))
            if clamped > 0:
                self.inflight_uploaded_bytes[item.rel_path] = clamped
            else:
                self.inflight_uploaded_bytes.pop(item.rel_path, None)
            self._maybe_render(now=time.monotonic(), force=False)

    def mark_complete(self, item: SubmissionInputFile) -> None:
        with self.lock:
            self.inflight_uploaded_bytes.pop(item.rel_path, None)
            self.completed_files += 1
            self.completed_bytes += item.bytes
            now = time.monotonic()
            self._maybe_render(now=now, force=self.completed_files == self.total_files)

    def _render_interval_seconds(self) -> float:
        if getattr(self.renderer, "is_live", False):
            return UPLOAD_PROGRESS_LIVE_RENDER_INTERVAL_SECONDS
        return UPLOAD_PROGRESS_TEXT_RENDER_INTERVAL_SECONDS

    def _maybe_render(self, *, now: float, force: bool) -> None:
        should_print = (
            force
            or not self.rendered_any
            or now - self.last_printed_at >= self._render_interval_seconds()
        )
        job_status_provider = self.job_status_provider
        should_check_job = job_status_provider is not None and (
            force or now - self.last_job_checked_at >= UPLOAD_JOB_STATUS_INTERVAL_SECONDS
        )
        remote_job: dict[str, Any] | None = None
        if should_check_job:
            assert job_status_provider is not None
            self.last_job_checked_at = now
            try:
                maybe_job = job_status_provider()
            except Exception:
                maybe_job = None
            if isinstance(maybe_job, dict):
                remote_job = maybe_job
                if job_should_stop_upload(remote_job):
                    if self.stop_event is not None:
                        self.stop_event.set()
                    self.renderer.update(remote_job, force=True)
                    raise JobTerminalDuringUpload(remote_job)
        if not should_print:
            return
        upload_progress = self.local_upload_progress(now=now)
        job: dict[str, Any] = {"upload_progress": upload_progress}
        if remote_job is not None:
            remote_upload_progress = job_upload_progress(remote_job)
            if remote_upload_progress is not None:
                job["upload_progress"] = self.remote_upload_progress_with_rate(
                    remote_upload_progress,
                    now=now,
                )
            encode_progress = remote_job.get("encode_progress")
            if isinstance(encode_progress, dict):
                job["encode_progress"] = encode_progress
            handoff_progress = remote_job.get("handoff_progress")
            if isinstance(handoff_progress, dict):
                job["handoff_progress"] = handoff_progress
        else:
            job["upload_progress"] = self.remote_upload_progress_with_rate(
                upload_progress,
                now=now,
            )
        self.renderer.update(job, force=force)
        self.last_printed_at = now
        self.rendered_any = True


class MunchyClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        allow_insecure_http: bool | None = None,
    ) -> None:
        self.allow_insecure_http = (
            _bool_env("MUNCHY_ALLOW_INSECURE_HTTP", False)
            if allow_insecure_http is None
            else allow_insecure_http
        )
        self.base_url = server_url_setting(
            base_url,
            allow_insecure_http=self.allow_insecure_http,
        )
        self.token = token_setting(token)
        self.http2 = _bool_env("MUNCHY_HTTP2", True)
        self.timeout_seconds = _http_timeout_seconds()
        self._http = httpx.Client(
            transport=transport,
            timeout=self.timeout_seconds,
            http2=self.http2,
        )
        tus_headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        self._tus = TusTransport(
            client=self._http,
            patch_client=self._http if transport is not None else None,
            headers=tus_headers,
            timeout_seconds=self.timeout_seconds,
        )

    def close(self) -> None:
        self._tus.close()
        self._http.close()

    def __enter__(self) -> MunchyClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def list_lifecycle_events(
        self,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> EventPage:
        query = urllib.parse.urlencode({"after": after or "0", "limit": limit})
        return EventPage.model_validate(self.json("GET", f"/v1/events?{query}"))

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        payload: dict[str, Any] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        expect: set[int] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes, Any]:
        if path_or_url.startswith(("http://", "https://")):
            url = path_or_url
        else:
            url = f"{self.base_url}{path_or_url}"
        request_headers = dict(headers or {})
        if self.token:
            request_headers.setdefault("Authorization", f"Bearer {self.token}")
        body = data
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request_options: dict[str, Any] = {
            "content": body,
            "headers": request_headers,
        }
        if timeout is not None:
            request_options["timeout"] = timeout
        response = self._http.request(method, url, **request_options)
        response_body = response.content
        status = response.status_code
        if (status >= 400 and (expect is None or status not in expect)) or (
            expect is not None and status not in expect
        ):
            raise MunchyHttpError(method, url, status, response_body)
        return status, response_body, response

    def json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        expect: set[int] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        _, body, _ = self.request(method, path, payload=payload, expect=expect, timeout=timeout)
        if not body:
            return {}
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise RuntimeError(f"{method} {path} did not return a JSON object")
        return parsed

    def record_submission_preflight_failure(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.json(
            "POST",
            "/v1/submissions/preflight-failures",
            payload=payload,
            expect={202},
        )

    def preflight_submission(self, request: SubmissionPreflightRequest) -> dict[str, Any]:
        return self._json_with_transient_retries(
            "POST",
            "/v1/submissions/preflight",
            payload=submission_preflight_payload(request),
            label="submission preflight",
            timeout=300.0,
        )

    def create_submission(self, request: SubmissionUploadRequest) -> dict[str, Any]:
        return self._json_with_transient_retries(
            "POST",
            "/v1/submissions",
            payload=submission_payload(request, include_id=True),
            expect={202},
            label="submission setup",
        )

    def get_submission(self, submission_id: str) -> dict[str, Any]:
        return self._json_with_transient_retries(
            "GET",
            f"/v1/submissions/{urllib.parse.quote(submission_id)}",
            label="submission status",
        )

    def create_or_resume_submission_file_upload(
        self,
        submission_id: str,
        rel_path: str,
    ) -> dict[str, Any]:
        escaped_submission_id = urllib.parse.quote(submission_id)
        escaped_rel_path = urllib.parse.quote(rel_path, safe="/")
        return self.json(
            "POST",
            f"/v1/submissions/{escaped_submission_id}/files/{escaped_rel_path}/upload",
            expect={201},
        )

    def put_submission_provenance_journal(
        self,
        submission_id: str,
        journal_id: str,
        *,
        content: bytes,
        sha256: str,
    ) -> dict[str, Any]:
        escaped_submission_id = urllib.parse.quote(submission_id)
        escaped_journal_id = urllib.parse.quote(journal_id)
        _, body, _ = self._request_with_transient_retries(
            "PUT",
            f"/v1/submissions/{escaped_submission_id}/provenance/journals/{escaped_journal_id}",
            label=f"provenance journal {journal_id}",
            timeout=300.0,
            expect={200},
            data=content,
            headers={
                "Content-Type": "application/json-seq",
                "X-Riverhog-Provenance-SHA256": sha256,
            },
        )
        value = json.loads(body)
        if not isinstance(value, dict):
            raise RuntimeError("Munchy returned an invalid provenance journal receipt")
        return value

    def _request_with_transient_retries(
        self,
        method: str,
        path: str,
        *,
        label: str,
        payload: dict[str, Any] | None = None,
        expect: set[int] | None = None,
        timeout: float | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, Any]:
        retry_delay = UPLOAD_RETRY_INITIAL_DELAY_SECONDS
        retry_count = 0
        retry_reporter = UploadRetryReporter(label=label)
        while True:
            try:
                result = self.request(
                    method,
                    path,
                    payload=payload,
                    expect=expect,
                    timeout=timeout,
                    data=data,
                    headers=headers,
                )
                retry_reporter.finish()
                return result
            except Exception as exc:
                if not is_transient_upload_error(exc):
                    raise
                retry_count += 1
                retry_reporter.mark_retry(
                    rel_path=path,
                    retry_count=retry_count,
                    retry_delay=retry_delay,
                    exc=exc,
                )
                time.sleep(retry_delay)
                retry_delay = next_upload_retry_delay(retry_delay)

    def _json_with_transient_retries(
        self,
        method: str,
        path: str,
        *,
        label: str,
        payload: dict[str, Any] | None = None,
        expect: set[int] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        retry_delay = UPLOAD_RETRY_INITIAL_DELAY_SECONDS
        retry_count = 0
        retry_reporter = UploadRetryReporter(label=label)
        while True:
            try:
                payload_doc = self.json(
                    method,
                    path,
                    payload=payload,
                    expect=expect,
                    timeout=timeout,
                )
                retry_reporter.finish()
                return payload_doc
            except Exception as exc:
                if not is_transient_upload_error(exc):
                    raise
                retry_count += 1
                retry_reporter.mark_retry(
                    rel_path=path,
                    retry_count=retry_count,
                    retry_delay=retry_delay,
                    exc=exc,
                )
                time.sleep(retry_delay)
                retry_delay = next_upload_retry_delay(retry_delay)

    def _job_status(self, request: UploadRequest) -> dict[str, Any]:
        return self.get_job(request.submission_id, compact=True)

    def upload_files(self, request: UploadRequest) -> dict[str, Any]:
        with keep_system_awake("munchy upload"):
            return self._upload_files(request)

    def _upload_files(self, request: UploadRequest) -> dict[str, Any]:
        self._upload_provenance_journals(request)
        total_files = len(request.files)
        total_bytes = sum(item.bytes for item in request.files)
        chunk_mib = request.upload_chunk_mib
        retry_reporter = UploadRetryReporter(label="remote upload")
        print(
            (
                f"uploading {total_files} files "
                f"({total_bytes / 1024**3:.2f} GiB) "
                f"with {request.upload_workers} workers, {chunk_mib} MiB chunks"
            ),
            file=sys.stderr,
        )
        if request.files:
            renderer = make_progress_renderer(include_job=False, title="Munchy Upload")
            retry_reporter.bind_renderer(renderer)
            if request.upload_workers == 1 or len(request.files) <= 1:
                with renderer:
                    self._upload_files_serial(
                        request,
                        list(request.files),
                        retry_reporter,
                        renderer,
                    )
                    retry_reporter.finish()
            else:
                with renderer:
                    self._upload_files_parallel(
                        request,
                        list(request.files),
                        retry_reporter,
                        renderer,
                    )
                    retry_reporter.finish()
        return {
            "state": "uploaded",
            "files_total": total_files,
            "files_uploaded": total_files,
            "bytes_total": total_bytes,
            "uploaded_bytes": total_bytes,
        }

    def _upload_provenance_journals(self, request: UploadRequest) -> None:
        journals: dict[str, bytes] = {}
        for item in request.files:
            for journal_id, content in item.provenance_journals.items():
                previous = journals.get(journal_id)
                if previous is not None and previous != content:
                    raise RuntimeError(f"provenance journal bytes disagree: {journal_id}")
                journals[journal_id] = content
        for journal_id, content in sorted(journals.items()):
            self.put_submission_provenance_journal(
                request.submission_id,
                journal_id,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            )

    def _upload_files_serial(
        self,
        request: UploadRequest,
        files: list[SubmissionInputFile],
        retry_reporter: UploadRetryReporter,
        renderer: ProgressRenderer,
        *,
        completed_files: int = 0,
        completed_bytes: int = 0,
    ) -> None:
        stop_event = Event()
        progress = UploadProgress(
            len(request.files),
            sum(item.bytes for item in request.files),
            completed_files=completed_files,
            completed_bytes=completed_bytes,
            renderer=renderer,
            stop_event=stop_event,
            job_status_provider=lambda: self._job_status(request),
        )
        for item in files:
            if stop_event.is_set():
                raise RuntimeError("upload stopped because the Munchy job reached a terminal state")
            self.upload_file(
                request.submission_id,
                item,
                chunk_bytes=request.upload_chunk_bytes,
                retry_reporter=retry_reporter,
                stop_event=stop_event,
                progress_callback=progress.mark_uploaded,
            )
            progress.mark_complete(item)

    def _upload_files_parallel(
        self,
        request: UploadRequest,
        files: list[SubmissionInputFile],
        retry_reporter: UploadRetryReporter,
        renderer: ProgressRenderer,
        *,
        completed_files: int = 0,
        completed_bytes: int = 0,
    ) -> None:
        stop_event = Event()
        progress = UploadProgress(
            len(request.files),
            sum(item.bytes for item in request.files),
            completed_files=completed_files,
            completed_bytes=completed_bytes,
            renderer=renderer,
            stop_event=stop_event,
            job_status_provider=lambda: self._job_status(request),
        )
        executor = ThreadPoolExecutor(max_workers=request.upload_workers)
        futures = {}
        for item in files:
            upload_kwargs: dict[str, Any] = {
                "chunk_bytes": request.upload_chunk_bytes,
                "retry_reporter": retry_reporter,
                "stop_event": stop_event,
                "progress_callback": progress.mark_uploaded,
            }
            futures[
                executor.submit(
                    self.upload_file,
                    request.submission_id,
                    item,
                    **upload_kwargs,
                )
            ] = item
        try:
            for future in as_completed(futures):
                item = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    stop_event.set()
                    for pending in futures:
                        pending.cancel()
                    raise RuntimeError(f"upload failed for {item.rel_path}: {exc}") from exc
                try:
                    progress.mark_complete(item)
                except Exception:
                    stop_event.set()
                    for pending in futures:
                        pending.cancel()
                    raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    def upload_file(
        self,
        submission_id: str,
        item: SubmissionInputFile,
        *,
        chunk_bytes: int,
        retry_reporter: UploadRetryReporter | None = None,
        stop_event: Event | None = None,
        progress_callback: Callable[[SubmissionInputFile, int], None] | None = None,
    ) -> None:
        retry_delay = UPLOAD_RETRY_INITIAL_DELAY_SECONDS
        retry_count = 0
        retry_reporter = retry_reporter or UploadRetryReporter(label="upload")
        while True:
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError("upload stopped because the Munchy job reached a terminal state")
            try:
                self._upload_file_once(
                    submission_id,
                    item,
                    chunk_bytes=chunk_bytes,
                    stop_event=stop_event,
                    progress_callback=progress_callback,
                )
                return
            except Exception as exc:
                if upload_was_removed(exc):
                    try:
                        submission = self.get_submission(submission_id)
                    except Exception:
                        submission = {}
                    job = submission.get("job")
                    if isinstance(job, dict) and job_should_stop_upload(job):
                        if stop_event is not None:
                            stop_event.set()
                        raise JobTerminalDuringUpload(job) from exc
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

    def _patch_upload_chunk(self, upload_url: str, *, offset: int, content: bytes) -> int:
        try:
            return self._tus.patch_chunk(
                upload_url,
                offset=offset,
                content=content,
            )
        except TusHttpError as exc:
            raise MunchyHttpError(
                "PATCH",
                upload_url,
                exc.status,
                exc.body,
            ) from exc

    def _upload_file_once(
        self,
        submission_id: str,
        item: SubmissionInputFile,
        *,
        chunk_bytes: int,
        stop_event: Event | None = None,
        progress_callback: Callable[[SubmissionInputFile, int], None] | None = None,
    ) -> None:
        upload = self.create_or_resume_submission_file_upload(submission_id, item.rel_path)
        upload_url = str(upload["upload_url"])
        offset = int(upload.get("offset") or 0)
        length = int(upload.get("length") or item.bytes)
        if length != item.bytes:
            raise RuntimeError(
                f"server expected {length} bytes for {item.rel_path}, local file has {item.bytes}"
            )
        if offset < 0:
            offset = 0
        if offset > item.bytes:
            raise RuntimeError(f"server upload offset is past EOF for {item.rel_path}: {offset}")
        if offset == item.bytes:
            return
        if offset > 0 and progress_callback is not None:
            progress_callback(item, offset)

        with item.source.open("rb") as fh:
            fh.seek(offset)
            while offset < item.bytes:
                if stop_event is not None and stop_event.is_set():
                    raise RuntimeError(
                        "upload stopped because the Munchy job reached a terminal state"
                    )
                chunk = fh.read(min(chunk_bytes, item.bytes - offset))
                if not chunk:
                    break
                offset = self._patch_upload_chunk(
                    upload_url,
                    offset=offset,
                    content=chunk,
                )
                if progress_callback is not None:
                    progress_callback(item, offset)
        if offset != item.bytes:
            raise RuntimeError(f"incomplete upload for {item.rel_path}: {offset} of {item.bytes}")

    def get_job(self, job_id: str, *, compact: bool = False) -> dict[str, Any]:
        path = f"/v1/jobs/{urllib.parse.quote(job_id)}"
        if compact:
            path += "?compact=true"
        return self.json("GET", path)

    def list_jobs(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "updated_at",
        order: str = "desc",
        query: str | None = None,
        terminal: str = "active",
        state: str | None = None,
        workflow_mode: str | None = None,
        handoff_destination: str | None = None,
        cancel_requested: bool | None = None,
        storage_wait: bool | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, str] = {
            "page": str(page),
            "per_page": str(per_page),
            "sort": sort,
            "order": order,
            "terminal": terminal,
        }
        if all_items:
            params["all"] = "true"
        if query:
            params["q"] = query
        if state:
            params["state"] = state
        if workflow_mode:
            params["workflow_mode"] = workflow_mode
        if handoff_destination:
            params["handoff_destination"] = handoff_destination
        if cancel_requested is not None:
            params["cancel_requested"] = "true" if cancel_requested else "false"
        if storage_wait is not None:
            params["storage_wait"] = "true" if storage_wait else "false"
        encoded_params = urllib.parse.urlencode(params)
        payload = self.json("GET", f"/v1/jobs?{encoded_params}")
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            raise RuntimeError(f"server returned invalid jobs page: {payload}")
        payload["jobs"] = [job for job in jobs if isinstance(job, dict)]
        return payload

    def cancel_job(self, job_id: str, *, cleanup: bool = False) -> dict[str, Any]:
        path = f"/v1/jobs/{urllib.parse.quote(job_id)}/cancel"
        if cleanup:
            path += "?cleanup=true"
        timeout = CLEANUP_REQUEST_TIMEOUT_SECONDS if cleanup else 60.0
        return self.json("POST", path, expect={202}, timeout=timeout)

    def resume_job(self, job_id: str) -> dict[str, Any]:
        return self.json(
            "POST",
            f"/v1/jobs/{urllib.parse.quote(job_id)}/resume",
            expect={202},
        )

    def wait_for_job(
        self,
        job_id: str,
        *,
        interval: float = 10.0,
        wait_for_safe_delete: bool = True,
    ) -> dict[str, Any]:
        with keep_system_awake("munchy job wait"):
            return self._wait_for_job(
                job_id,
                interval=interval,
                wait_for_safe_delete=wait_for_safe_delete,
            )

    def _wait_for_job(
        self,
        job_id: str,
        *,
        interval: float = 10.0,
        wait_for_safe_delete: bool = True,
    ) -> dict[str, Any]:
        retry_delay = UPLOAD_RETRY_INITIAL_DELAY_SECONDS
        retry_reporter = UploadRetryReporter(label="remote job status")
        renderer = make_progress_renderer(
            include_job=True,
            title=f"Munchy Job {short_path(job_id, max_len=48)}",
        )
        retry_reporter.bind_renderer(renderer)
        with renderer:
            while True:
                try:
                    job = self.get_job(job_id, compact=True)
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
                terminal = state in {"succeeded", "failed", "canceled"}
                handoff_pending = (
                    wait_for_safe_delete
                    and state == "succeeded"
                    and not handoff_safe_to_delete(job)
                )
                renderer.update(job, force=terminal and not handoff_pending)
                if handoff_failed(job):
                    retry_reporter.finish()
                    return job
                if terminal and not handoff_pending:
                    retry_reporter.finish()
                    return job
                time.sleep(interval)


class MunchyAdminClient(MunchyClient):
    def __init__(
        self,
        base_url: str | None = None,
        *,
        token: str | None = None,
        allow_insecure_http: bool | None = None,
    ) -> None:
        super().__init__(
            base_url,
            token=admin_token_setting(token),
            allow_insecure_http=allow_insecure_http,
        )

    def get_scheduler_status(self) -> dict[str, Any]:
        return self.json("GET", "/v1/admin/scheduler")

    def pause_scheduler(self) -> dict[str, Any]:
        return self.json("POST", "/v1/admin/scheduler/pause")

    def resume_scheduler(self) -> dict[str, Any]:
        return self.json("POST", "/v1/admin/scheduler/resume")

    def list_apps(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "name",
        order: str = "asc",
        query: str | None = None,
        active: bool | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, str] = {
            "page": str(page),
            "per_page": str(per_page),
            "sort": sort,
            "order": order,
        }
        if all_items:
            params["all"] = "true"
        if query:
            params["q"] = query
        if active is not None:
            params["active"] = "true" if active else "false"
        return self.json("GET", "/v1/admin/apps?" + urllib.parse.urlencode(params))

    def create_app_key(
        self,
        app: str,
        *,
        permissions: list[str],
        expires_in_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"permissions": permissions}
        if expires_in_seconds is not None:
            payload["expires_in_seconds"] = expires_in_seconds
        return self.json(
            "POST",
            f"/v1/admin/apps/{urllib.parse.quote(app)}/keys",
            payload=payload,
            expect={201},
        )

    def list_app_keys(
        self,
        app: str,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "created_at",
        order: str = "desc",
        query: str | None = None,
        active: bool | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, str] = {
            "page": str(page),
            "per_page": str(per_page),
            "sort": sort,
            "order": order,
        }
        if all_items:
            params["all"] = "true"
        if query:
            params["q"] = query
        if active is not None:
            params["active"] = "true" if active else "false"
        return self.json(
            "GET",
            f"/v1/admin/apps/{urllib.parse.quote(app)}/keys?" + urllib.parse.urlencode(params),
        )

    def revoke_app_key(self, app: str, key_id: str) -> dict[str, Any]:
        return self.json(
            "POST",
            f"/v1/admin/apps/{urllib.parse.quote(app)}/keys/{urllib.parse.quote(key_id)}/revoke",
        )

    def list_job_diagnostics(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "created_at",
        order: str = "desc",
        query: str | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, str] = {
            "page": str(page),
            "per_page": str(per_page),
            "sort": sort,
            "order": order,
        }
        if query:
            params["q"] = query
        if all_items:
            params["all"] = "true"
        payload = self.json(
            "GET",
            "/v1/admin/job-diagnostics?" + urllib.parse.urlencode(params),
        )
        diagnostics = payload.get("diagnostics")
        if not isinstance(diagnostics, list):
            raise RuntimeError(f"server returned invalid job-diagnostic page: {payload}")
        payload["diagnostics"] = [item for item in diagnostics if isinstance(item, dict)]
        return payload

    def get_job_diagnostic(self, job_id: str) -> dict[str, Any]:
        return self.json(
            "GET",
            f"/v1/admin/jobs/{urllib.parse.quote(job_id, safe='')}/diagnostic",
        )

    def download_job_diagnostic(
        self,
        job_id: str,
        *,
        output: Path,
        overwrite: bool = False,
        progress: DownloadProgress | None = None,
    ) -> dict[str, Any]:
        diagnostic = self.get_job_diagnostic(job_id)
        expected_bytes = int(diagnostic["bytes"])
        expected_sha256 = str(diagnostic["sha256"])
        encoded_job_id = urllib.parse.quote(job_id, safe="")
        url = f"{self.base_url}/v1/admin/jobs/{encoded_job_id}/diagnostic/content"
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        with self._http.stream("GET", url, headers=headers, timeout=3600.0) as response:
            if response.status_code >= 400:
                body = response.read()
                raise MunchyHttpError("GET", url, response.status_code, body)
            try:
                returned_bytes = int(response.headers.get("Content-Length", ""))
            except ValueError as exc:
                raise RuntimeError(
                    "diagnostic download returned an invalid Content-Length"
                ) from exc
            if returned_bytes != expected_bytes:
                raise RuntimeError(
                    "diagnostic Content-Length does not match its metadata: "
                    f"{returned_bytes} != {expected_bytes}"
                )
            returned_etag = response.headers.get("ETag", "").strip().strip('"').casefold()
            if returned_etag != expected_sha256.casefold():
                raise RuntimeError("diagnostic ETag does not match its metadata SHA-256")
            receipt = verified_download(
                response.iter_bytes(chunk_size=8 * 1024 * 1024),
                output=output,
                expected_bytes=expected_bytes,
                expected_sha256=expected_sha256,
                overwrite=overwrite,
                progress=progress,
            )
        return {
            "job_id": job_id,
            "output": str(receipt.output),
            "bytes": receipt.bytes,
            "sha256": receipt.sha256,
        }

    def remove_job_diagnostic(self, job_id: str) -> dict[str, Any]:
        return self.json(
            "DELETE",
            f"/v1/admin/jobs/{urllib.parse.quote(job_id, safe='')}/diagnostic",
        )

    def remove_terminal_job(self, job_id: str) -> dict[str, Any]:
        return self.json(
            "DELETE",
            f"/v1/admin/jobs/{urllib.parse.quote(job_id, safe='')}",
        )

    def get_retention_plan(self) -> dict[str, Any]:
        return self.json("GET", "/v1/admin/maintenance/retention")

    def apply_retention(self) -> dict[str, Any]:
        return self.json("POST", "/v1/admin/maintenance/retention")

    def list_job_templates(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "template_id",
        order: str = "asc",
        query: str | None = None,
        enabled: bool | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, str] = {
            "page": str(page),
            "per_page": str(per_page),
            "sort": sort,
            "order": order,
        }
        if all_items:
            params["all"] = "true"
        if query:
            params["q"] = query
        if enabled is not None:
            params["enabled"] = "true" if enabled else "false"
        payload = self.json(
            "GET",
            "/v1/admin/job-templates?" + urllib.parse.urlencode(params),
        )
        templates = payload.get("templates")
        if not isinstance(templates, list):
            raise RuntimeError(f"server returned invalid job-template page: {payload}")
        payload["templates"] = [item for item in templates if isinstance(item, dict)]
        return payload

    def get_job_template(self, template_id: str) -> dict[str, Any]:
        return self.json(
            "GET",
            f"/v1/admin/job-templates/{urllib.parse.quote(template_id)}",
        )

    def validate_job_template(
        self,
        template_id: str,
        definition: dict[str, Any],
    ) -> dict[str, Any]:
        return self.json(
            "POST",
            "/v1/admin/job-templates/validate",
            payload={"template_id": template_id, "definition": definition},
        )

    def create_job_template(
        self,
        template_id: str,
        definition: dict[str, Any],
        *,
        enabled: bool = True,
    ) -> dict[str, Any]:
        return self.json(
            "POST",
            "/v1/admin/job-templates",
            payload={"template_id": template_id, "definition": definition, "enabled": enabled},
            expect={201},
        )

    def replace_job_template(
        self,
        template_id: str,
        definition: dict[str, Any],
        *,
        expected_revision: int,
        enabled: bool = True,
    ) -> dict[str, Any]:
        return self.json(
            "PUT",
            f"/v1/admin/job-templates/{urllib.parse.quote(template_id)}",
            payload={
                "definition": definition,
                "enabled": enabled,
                "expected_revision": expected_revision,
            },
        )

    def enable_job_template(
        self,
        template_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        return self.json(
            "POST",
            f"/v1/admin/job-templates/{urllib.parse.quote(template_id)}/enable",
            payload={"expected_revision": expected_revision},
        )

    def disable_job_template(
        self,
        template_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        return self.json(
            "POST",
            f"/v1/admin/job-templates/{urllib.parse.quote(template_id)}/disable",
            payload={"expected_revision": expected_revision},
        )

    def delete_job_template(self, template_id: str, *, expected_revision: int) -> dict[str, Any]:
        query = urllib.parse.urlencode({"expected_revision": expected_revision})
        return self.json(
            "DELETE",
            f"/v1/admin/job-templates/{urllib.parse.quote(template_id)}?{query}",
        )
