from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from riverhog_protocol import CollectionId

DEFAULT_DOWNLOAD_CONCURRENCY = 4
DEFAULT_DOWNLOAD_WINDOW = 8

DownloadProgress = Callable[["RetrievalDownload", int], None]
DownloadHeartbeat = Callable[[], None]


@dataclass(frozen=True, slots=True)
class RetrievalDownload:
    collection_id: CollectionId
    path: str
    output: Path
    expected_bytes: int
    expected_sha256: str


class RetrievalDownloadApi(Protocol):
    def download_retrieval_file(
        self,
        job_id: str,
        *,
        collection_id: CollectionId,
        path: str,
        output: Path,
        expected_bytes: int,
        expected_sha256: str,
    ) -> int: ...


def configured_download_concurrency(values: Mapping[str, str] | None = None) -> int:
    environment = os.environ if values is None else values
    raw_value = environment.get("RIVERHOG_DOWNLOAD_FILE_CONCURRENCY", "").strip()
    if not raw_value:
        return DEFAULT_DOWNLOAD_CONCURRENCY
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError("RIVERHOG_DOWNLOAD_FILE_CONCURRENCY must be a positive integer") from exc
    if value < 1:
        raise ValueError("RIVERHOG_DOWNLOAD_FILE_CONCURRENCY must be a positive integer")
    return value


def configured_download_window(
    values: Mapping[str, str] | None = None,
    *,
    concurrency: int | None = None,
) -> int:
    environment = os.environ if values is None else values
    resolved_concurrency = (
        configured_download_concurrency(environment) if concurrency is None else concurrency
    )
    if resolved_concurrency < 1:
        raise ValueError("download concurrency must be positive")
    raw_value = environment.get("RIVERHOG_DOWNLOAD_FILE_WINDOW", "").strip()
    if not raw_value:
        return resolved_concurrency * 2
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError("RIVERHOG_DOWNLOAD_FILE_WINDOW must be a positive integer") from exc
    if value < resolved_concurrency:
        raise ValueError(
            "RIVERHOG_DOWNLOAD_FILE_WINDOW must be at least download concurrency "
            f"({resolved_concurrency})"
        )
    return value


def download_retrieval_files(
    api: RetrievalDownloadApi,
    job_id: str,
    downloads: Sequence[RetrievalDownload],
    *,
    concurrency: int,
    window: int,
    client_factory: Callable[[], RetrievalDownloadApi] | None = None,
    on_downloaded: DownloadProgress | None = None,
    heartbeat: DownloadHeartbeat | None = None,
    heartbeat_interval_seconds: float = 60.0,
) -> int:
    if concurrency < 1:
        raise ValueError("download concurrency must be positive")
    if window < concurrency:
        raise ValueError(f"download window must be at least download concurrency ({concurrency})")
    if not downloads:
        return 0
    if heartbeat is not None and heartbeat_interval_seconds <= 0:
        raise ValueError("download heartbeat interval must be positive")

    worker_count = min(concurrency, len(downloads))
    resolved_factory = (client_factory or _client_factory(api)) if worker_count > 1 else None
    local = threading.local()
    clients: list[RetrievalDownloadApi] = []
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

    def download_one(download: RetrievalDownload) -> int:
        worker_api = cast(RetrievalDownloadApi, local.api)
        return worker_api.download_retrieval_file(
            job_id,
            collection_id=download.collection_id,
            path=download.path,
            output=download.output,
            expected_bytes=download.expected_bytes,
            expected_sha256=download.expected_sha256,
        )

    ready = deque(downloads)
    pending: dict[Future[int], RetrievalDownload] = {}
    downloaded_bytes = 0
    next_heartbeat = time.monotonic() + heartbeat_interval_seconds
    try:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="riverhog-download-file",
            initializer=initialize_worker,
        ) as executor:

            def fill() -> None:
                while ready and len(pending) < window:
                    download = ready.popleft()
                    pending[executor.submit(download_one, download)] = download

            fill()
            try:
                while pending:
                    timeout = (
                        max(0.0, next_heartbeat - time.monotonic())
                        if heartbeat is not None
                        else None
                    )
                    done, _ = wait(
                        tuple(pending),
                        timeout=timeout,
                        return_when=FIRST_COMPLETED,
                    )
                    if heartbeat is not None and time.monotonic() >= next_heartbeat:
                        heartbeat()
                        next_heartbeat = time.monotonic() + heartbeat_interval_seconds
                    for future in done:
                        download = pending.pop(future)
                        accepted = future.result()
                        if accepted != download.expected_bytes:
                            raise RuntimeError(
                                "retrieval download byte count differs from its planned identity"
                            )
                        downloaded_bytes += accepted
                        if on_downloaded is not None:
                            on_downloaded(download, accepted)
                    fill()
            except BaseException:
                for future in pending:
                    future.cancel()
                raise
    finally:
        for worker_api in clients:
            close = getattr(worker_api, "close", None)
            if callable(close):
                close()
    return downloaded_bytes


def _client_factory(api: RetrievalDownloadApi) -> Callable[[], RetrievalDownloadApi]:
    spawn = getattr(api, "spawn", None)
    if not callable(spawn):
        raise ValueError("parallel downloads require an API client factory")
    return cast(Callable[[], RetrievalDownloadApi], spawn)


__all__ = [
    "DownloadHeartbeat",
    "DEFAULT_DOWNLOAD_CONCURRENCY",
    "DEFAULT_DOWNLOAD_WINDOW",
    "RetrievalDownload",
    "RetrievalDownloadApi",
    "configured_download_concurrency",
    "configured_download_window",
    "download_retrieval_files",
]
