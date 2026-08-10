from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol, cast

import httpx
from riverhog_protocol.errors import Conflict, NotFound, ServiceUnavailable

DEFAULT_UPLOAD_CONCURRENCY = 8
MAX_UPLOAD_CONCURRENCY = 64
_TRANSIENT_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

UnitContent = Callable[[Mapping[str, object]], bytes]
UploadProgress = Callable[[int], None]
RetryNotice = Callable[[str], None]


class CollectionUnitApi(Protocol):
    def list_collection_upload_session_volumes(self, collection_id: int) -> dict[str, Any]: ...

    def get_collection_upload_session_unit(
        self,
        collection_id: int,
        volume_id: str,
        unit: int,
    ) -> dict[str, Any]: ...

    def put_collection_upload_session_unit(
        self,
        collection_id: int,
        volume_id: str,
        unit: int,
        *,
        plan_sha256: str,
        content: bytes,
    ) -> dict[str, Any]: ...


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


def put_collection_upload_unit(
    api: CollectionUnitApi,
    collection_id: int,
    volume: Mapping[str, object],
    unit: Mapping[str, object],
    *,
    content_for_unit: UnitContent,
    retry_notice: RetryNotice | None = None,
    retry_initial_delay_seconds: float = 1.0,
    retry_max_delay_seconds: float = 10.0,
) -> int:
    volume_id = volume.get("volume_id")
    plan_sha256 = volume.get("plan_sha256")
    unit_number = unit.get("unit")
    payload_bytes = unit.get("payload_bytes")
    if (
        not isinstance(volume_id, str)
        or not isinstance(plan_sha256, str)
        or not isinstance(unit_number, int)
        or not isinstance(payload_bytes, int)
        or payload_bytes < 0
    ):
        raise RuntimeError("server returned an invalid upload unit identity")
    if unit.get("state") == "committed":
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
            if result.get("state") != "committed":
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
                current = {}
            if current.get("state") == "committed":
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
    collection_id: int,
    *,
    content_for_unit: UnitContent,
    concurrency: int,
    client_factory: Callable[[], CollectionUnitApi] | None = None,
    on_committed: UploadProgress | None = None,
    on_resumed: UploadProgress | None = None,
    retry_notice: RetryNotice | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> int:
    if concurrency < 1 or concurrency > MAX_UPLOAD_CONCURRENCY:
        raise ValueError(f"upload concurrency must be between 1 and {MAX_UPLOAD_CONCURRENCY}")
    payload = api.list_collection_upload_session_volumes(collection_id)
    volumes = _volume_work(payload)
    uploaded = 0
    if on_resumed is not None:
        on_resumed(
            sum(
                _unit_payload_bytes(unit)
                for volume in volumes
                for unit in _volume_units(volume)
                if unit.get("state") == "committed"
            )
        )

    # Create one durable multipart checkpoint per volume before allowing
    # concurrent unit requests against that volume.
    for volume in volumes:
        first = next(
            (unit for unit in _volume_units(volume) if unit.get("state") != "committed"),
            None,
        )
        if first is None:
            continue
        if cancel_check is not None:
            cancel_check()
        accepted = put_collection_upload_unit(
            api,
            collection_id,
            volume,
            first,
            content_for_unit=content_for_unit,
            retry_notice=retry_notice,
        )
        uploaded += accepted
        if on_committed is not None:
            on_committed(accepted)

    try:
        volumes = _volume_work(api.list_collection_upload_session_volumes(collection_id))
    except NotFound:
        return uploaded
    pending = [
        (volume, unit)
        for volume in volumes
        for unit in _volume_units(volume)
        if unit.get("state") != "committed"
    ]
    if not pending:
        return uploaded

    worker_count = min(concurrency, len(pending))
    resolved_factory = client_factory or _client_factory(api) if worker_count > 1 else None
    buckets = [pending[index::worker_count] for index in range(worker_count)]

    def worker(items: list[tuple[dict[str, object], dict[str, object]]]) -> int:
        worker_api = api if resolved_factory is None else resolved_factory()
        owns_api = worker_api is not api
        accepted_bytes = 0
        try:
            for volume, unit in items:
                if cancel_check is not None:
                    cancel_check()
                accepted = put_collection_upload_unit(
                    worker_api,
                    collection_id,
                    volume,
                    unit,
                    content_for_unit=content_for_unit,
                    retry_notice=retry_notice,
                )
                accepted_bytes += accepted
                if on_committed is not None:
                    on_committed(accepted)
            return accepted_bytes
        finally:
            if owns_api:
                close = getattr(worker_api, "close", None)
                if callable(close):
                    close()

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(worker, bucket) for bucket in buckets]
        uploaded += sum(future.result() for future in futures)
    return uploaded


def _client_factory(api: CollectionUnitApi) -> Callable[[], CollectionUnitApi]:
    spawn = getattr(api, "spawn", None)
    if not callable(spawn):
        raise ValueError("parallel uploads require an API client factory")
    return cast(Callable[[], CollectionUnitApi], spawn)


def _unit_payload_bytes(unit: Mapping[str, object]) -> int:
    value = unit.get("payload_bytes")
    if not isinstance(value, int) or value < 0:
        raise RuntimeError("upload session returned an invalid unit byte count")
    return value


def _volume_work(payload: Mapping[str, object]) -> list[dict[str, object]]:
    values = payload.get("volumes")
    if not isinstance(values, list):
        raise RuntimeError("upload session returned an invalid volume list")
    result: list[dict[str, object]] = []
    for value in values:
        if not isinstance(value, dict):
            raise RuntimeError("upload session returned an invalid volume")
        result.append(dict(value))
    return result


def _volume_units(volume: Mapping[str, object]) -> list[dict[str, object]]:
    values = volume.get("units")
    if not isinstance(values, list):
        raise RuntimeError("upload session returned an invalid volume plan")
    result: list[dict[str, object]] = []
    for value in values:
        if not isinstance(value, dict):
            raise RuntimeError("upload session returned an invalid volume plan")
        result.append(dict(value))
    return result


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
    "MAX_UPLOAD_CONCURRENCY",
    "configured_upload_concurrency",
    "put_collection_upload_unit",
    "upload_collection_units",
]
