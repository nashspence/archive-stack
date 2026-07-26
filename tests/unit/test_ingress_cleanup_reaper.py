from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from types import SimpleNamespace
from typing import cast

from riverhog_api import app as api_app
from riverhog_api.deps import ServiceContainer


class _ArchiveUploadService:
    def __init__(self) -> None:
        self.cleanup_started = threading.Event()
        self.release_cleanup = threading.Event()
        self.archive_processed = threading.Event()

    def requeue_interrupted_ingress_cleanup_for_startup(self) -> int:
        return 0

    def process_due_ingress_cleanup(self, *, limit: int = 100) -> int:
        self.cleanup_started.set()
        assert self.release_cleanup.wait(timeout=5)
        return 1

    def requeue_failed_uploads_for_startup(self, *, limit: int = 100) -> int:
        return 0

    def requeue_interrupted_metadata_publications_for_startup(self) -> int:
        return 0

    def process_due_uploads(self, *, limit: int = 1) -> int:
        self.archive_processed.set()
        return 1

    def process_due_metadata_publications(self, *, limit: int = 10) -> int:
        return 0


class _ArchiveCopyService:
    def requeue_interrupted_copies_for_startup(self, *, limit: int = 100) -> int:
        return 0

    def process_due(self, *, limit: int = 1) -> int:
        return 0


def test_ingress_cleanup_runs_independently_from_archive_operations() -> None:
    async def exercise() -> None:
        archive_uploads = _ArchiveUploadService()
        container = cast(
            ServiceContainer,
            SimpleNamespace(
                archive_uploads=archive_uploads,
                archive_copies=_ArchiveCopyService(),
            ),
        )
        cleanup_task = asyncio.create_task(
            api_app._run_ingress_cleanup_reaper(
                lambda: container,
                sweep_interval=timedelta(days=1),
            )
        )
        archive_task = asyncio.create_task(
            api_app._run_archive_upload_reaper(
                lambda: container,
                sweep_interval=timedelta(days=1),
                operation_lock=asyncio.Lock(),
            )
        )
        try:
            for _ in range(100):
                if archive_uploads.cleanup_started.is_set():
                    break
                await asyncio.sleep(0.001)
            assert archive_uploads.cleanup_started.is_set()
            for _ in range(100):
                if archive_uploads.archive_processed.is_set():
                    break
                await asyncio.sleep(0.001)
            assert archive_uploads.archive_processed.is_set()
        finally:
            archive_uploads.release_cleanup.set()
            cleanup_task.cancel()
            archive_task.cancel()
            for task in (cleanup_task, archive_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(exercise())
