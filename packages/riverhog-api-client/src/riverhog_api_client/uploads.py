from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Protocol, cast

import httpx
from riverhog_protocol import (
    CollectionId,
    CollectionUploadUnitAssignmentDocument,
    CollectionUploadUnitNumber,
    CollectionUploadUnitWorkDocument,
    CollectionUploadVolumeId,
    CollectionUploadWorkBatchDocument,
)
from riverhog_protocol.errors import Conflict, ServiceUnavailable

DEFAULT_UPLOAD_CONCURRENCY = 8
DEFAULT_UPLOAD_WINDOW = 16
MAX_UPLOAD_CONCURRENCY = 64
MAX_UPLOAD_WINDOW = 256
_TRANSIENT_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

UnitContent = Callable[[CollectionUploadUnitWorkDocument], bytes]
UploadProgress = Callable[[int], None]
RetryNotice = Callable[[str], None]


class CollectionUnitApi(Protocol):
    def acquire_collection_upload_session_work(
        self, collection_id: CollectionId, *, limit: int = 16
    ) -> CollectionUploadWorkBatchDocument: ...

    def get_collection_upload_session_unit(
        self,
        collection_id: CollectionId,
        volume_id: CollectionUploadVolumeId,
        unit: CollectionUploadUnitNumber,
    ) -> CollectionUploadUnitWorkDocument: ...

    def put_collection_upload_session_unit(
        self,
        collection_id: CollectionId,
        volume_id: CollectionUploadVolumeId,
        unit: CollectionUploadUnitNumber,
        *,
        plan_sha256: str,
        content: bytes,
    ) -> CollectionUploadUnitWorkDocument: ...


def configured_upload_concurrency(values: Mapping[str, str] | None = None) -> int:
    environment = os.environ if values is None else values
    raw_value = environment.get("RIVERHOG_UPLOAD_FILE_CONCURRENCY", "").strip()
    if not raw_value:
        return DEFAULT_UPLOAD_CONCURRENCY
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError("RIVERHOG_UPLOAD_FILE_CONCURRENCY must be a positive integer") from exc
    if value < 1 or value > MAX_UPLOAD_CONCURRENCY:
        raise ValueError(
            f"RIVERHOG_UPLOAD_FILE_CONCURRENCY must be between 1 and {MAX_UPLOAD_CONCURRENCY}"
        )
    return value


def configured_upload_window(
    values: Mapping[str, str] | None = None,
    *,
    concurrency: int | None = None,
) -> int:
    environment = os.environ if values is None else values
    resolved_concurrency = (
        configured_upload_concurrency(environment) if concurrency is None else concurrency
    )
    if resolved_concurrency < 1 or resolved_concurrency > MAX_UPLOAD_CONCURRENCY:
        raise ValueError(f"upload concurrency must be between 1 and {MAX_UPLOAD_CONCURRENCY}")
    raw_value = environment.get("RIVERHOG_UPLOAD_FILE_WINDOW", "").strip()
    if not raw_value:
        return min(resolved_concurrency * 2, MAX_UPLOAD_WINDOW)
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError("RIVERHOG_UPLOAD_FILE_WINDOW must be a positive integer") from exc
    if value < resolved_concurrency or value > MAX_UPLOAD_WINDOW:
        raise ValueError(
            "RIVERHOG_UPLOAD_FILE_WINDOW must be between upload concurrency "
            f"({resolved_concurrency}) and {MAX_UPLOAD_WINDOW}"
        )
    return value


def put_collection_upload_unit(
    api: CollectionUnitApi,
    collection_id: CollectionId,
    assignment: CollectionUploadUnitAssignmentDocument,
    *,
    content_for_unit: UnitContent,
    retry_notice: RetryNotice | None = None,
    retry_initial_delay_seconds: float = 1.0,
    retry_max_delay_seconds: float = 10.0,
) -> int:
    volume_id = assignment.volume.volume_id
    plan_sha256 = assignment.plan_sha256
    unit = assignment.unit
    unit_number = unit.unit
    payload_bytes = unit.payload_bytes
    if unit.state == "committed":
        return 0
    content = content_for_unit(unit)
    if len(content) != payload_bytes:
        raise RuntimeError("local sources did not match the Riverhog upload unit")
    delay = retry_initial_delay_seconds
    while True:
        try:
            result = api.put_collection_upload_session_unit(
                collection_id,
                volume_id,
                unit_number,
                plan_sha256=plan_sha256,
                content=content,
            )
            if result.state != "committed":
                raise RuntimeError("server did not commit the complete upload unit")
            return payload_bytes
        except (httpx.TransportError, httpx.HTTPStatusError, Conflict, ServiceUnavailable) as exc:
            if not isinstance(exc, Conflict) and not _is_transient(exc):
                raise
            try:
                current = api.get_collection_upload_session_unit(
                    collection_id,
                    volume_id,
                    unit_number,
                )
            except (httpx.TransportError, httpx.HTTPStatusError, ServiceUnavailable):
                current = None
            if current is not None and current.state == "committed":
                return payload_bytes
            if isinstance(exc, Conflict):
                raise
            if retry_notice is not None:
                retry_notice(
                    f"Upload unit {volume_id}/{unit_number} interrupted "
                    f"({_error_description(exc)}); retrying in {delay:.1f}s"
                )
            time.sleep(delay)
            delay = min(delay * 2, retry_max_delay_seconds)


def upload_collection_units(
    api: CollectionUnitApi,
    collection_id: CollectionId,
    *,
    content_for_unit: UnitContent,
    concurrency: int,
    window: int,
    client_factory: Callable[[], CollectionUnitApi] | None = None,
    on_committed: UploadProgress | None = None,
    on_resumed: UploadProgress | None = None,
    retry_notice: RetryNotice | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> int:
    if concurrency < 1 or concurrency > MAX_UPLOAD_CONCURRENCY:
        raise ValueError(f"upload concurrency must be between 1 and {MAX_UPLOAD_CONCURRENCY}")
    if window < concurrency or window > MAX_UPLOAD_WINDOW:
        raise ValueError(
            f"upload window must be between upload concurrency ({concurrency}) "
            f"and {MAX_UPLOAD_WINDOW}"
        )
    uploaded = 0
    resumed_reported = False
    while True:
        batch = api.acquire_collection_upload_session_work(
            collection_id,
            limit=min(window, MAX_UPLOAD_CONCURRENCY),
        )
        if on_resumed is not None and not resumed_reported:
            on_resumed(batch.committed_payload_bytes)
            resumed_reported = True
        if not batch.work:
            return uploaded
        uploaded += _upload_work(
            api,
            collection_id,
            assignments=batch.work,
            content_for_unit=content_for_unit,
            concurrency=concurrency,
            client_factory=client_factory,
            on_committed=on_committed,
            retry_notice=retry_notice,
            cancel_check=cancel_check,
        )


def _upload_work(
    api: CollectionUnitApi,
    collection_id: CollectionId,
    *,
    assignments: Sequence[CollectionUploadUnitAssignmentDocument],
    content_for_unit: UnitContent,
    concurrency: int,
    client_factory: Callable[[], CollectionUnitApi] | None,
    on_committed: UploadProgress | None,
    retry_notice: RetryNotice | None,
    cancel_check: Callable[[], None] | None,
) -> int:
    total_units = len(assignments)
    if total_units == 0:
        return 0
    worker_count = min(concurrency, total_units)
    resolved_factory = (client_factory or _client_factory(api)) if worker_count > 1 else None
    local = threading.local()
    clients: list[CollectionUnitApi] = []
    clients_lock = threading.Lock()

    def initialize_worker() -> None:
        if resolved_factory is None:
            local.api = api
            return
        worker_api = resolved_factory()
        local.api = worker_api
        if worker_api is not api:
            with clients_lock:
                clients.append(worker_api)

    def upload_one(
        item: CollectionUploadUnitAssignmentDocument,
    ) -> int:
        if cancel_check is not None:
            cancel_check()
        worker_api = cast(CollectionUnitApi, local.api)
        return put_collection_upload_unit(
            worker_api,
            collection_id,
            item,
            content_for_unit=content_for_unit,
            retry_notice=retry_notice,
        )

    pending: dict[Future[int], CollectionUploadUnitAssignmentDocument] = {}
    uploaded = 0
    try:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="riverhog-upload-unit",
            initializer=initialize_worker,
        ) as executor:
            for item in assignments:
                pending[executor.submit(upload_one, item)] = item
            try:
                while pending:
                    done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                    for future in done:
                        pending.pop(future)
                        accepted = future.result()
                        uploaded += accepted
                        if on_committed is not None:
                            on_committed(accepted)
            except BaseException:
                for future in pending:
                    future.cancel()
                raise
    finally:
        for worker_api in clients:
            close = getattr(worker_api, "close", None)
            if callable(close):
                close()
    return uploaded


def _client_factory(api: CollectionUnitApi) -> Callable[[], CollectionUnitApi]:
    spawn = getattr(api, "spawn", None)
    if not callable(spawn):
        raise ValueError("parallel uploads require an API client factory")
    return cast(Callable[[], CollectionUnitApi], spawn)


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUS_CODES
    return isinstance(exc, ServiceUnavailable)


def _error_description(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    message = getattr(exc, "message", None)
    return str(message) if message else f"{type(exc).__name__}: {exc}"


__all__ = [
    "CollectionUnitApi",
    "DEFAULT_UPLOAD_CONCURRENCY",
    "DEFAULT_UPLOAD_WINDOW",
    "MAX_UPLOAD_CONCURRENCY",
    "MAX_UPLOAD_WINDOW",
    "configured_upload_concurrency",
    "configured_upload_window",
    "put_collection_upload_unit",
    "upload_collection_units",
]
