from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from riverhog_api import app as api_app
from riverhog_api.deps import ServiceContainer


class _ArchiveUploadService:
    def __init__(self, *, result: int = 2) -> None:
        self.initiated_before: datetime | None = None
        self.result = result

    def abort_incomplete_multipart_uploads(
        self,
        *,
        initiated_before: datetime,
    ) -> int:
        self.initiated_before = initiated_before
        return self.result

    def abort_incomplete_cache_multipart_uploads(
        self,
        *,
        initiated_before: datetime,
    ) -> int:
        self.initiated_before = initiated_before
        return self.result


class _RetrievalService:
    def __init__(self) -> None:
        self.processed = 0
        self.swept = 0
        self.requeued = 0

    def process_due(self, *, limit: int) -> int:
        assert limit == 10
        self.processed += 1
        return 0

    def requeue_interrupted_cache_cleanup_for_startup(self) -> int:
        self.requeued += 1
        return 0

    def sweep(self) -> int:
        self.swept += 1
        return 0


def test_archive_maintenance_sweep_recovers_and_processes_collection_finalizations() -> None:
    collection_uploads = SimpleNamespace(
        requeue_interrupted_finalizations_for_startup=Mock(return_value=2),
        process_due_finalizations=Mock(return_value=1),
    )
    archive_copies = SimpleNamespace(
        requeue_interrupted_copies_for_startup=Mock(return_value=0),
        process_due=Mock(return_value=0),
    )
    archive_maintenance = SimpleNamespace(
        requeue_interrupted_metadata_publications_for_startup=Mock(return_value=0),
        process_due_metadata_publications=Mock(return_value=0),
    )
    collection_workflows = SimpleNamespace(
        reap_expired_claims=Mock(return_value=0),
    )
    container = cast(
        ServiceContainer,
        SimpleNamespace(
            collection_uploads=collection_uploads,
            collection_workflows=collection_workflows,
            archive_copies=archive_copies,
            archive_maintenance=archive_maintenance,
        ),
    )

    api_app._process_archive_maintenance(container, startup_recovery=True)

    collection_uploads.requeue_interrupted_finalizations_for_startup.assert_called_once_with(
        limit=100
    )
    collection_uploads.process_due_finalizations.assert_called_once_with(limit=1)
    collection_workflows.reap_expired_claims.assert_called_once_with(limit=100)


def test_archive_multipart_sweep_uses_the_configured_max_age(monkeypatch) -> None:
    service = _ArchiveUploadService()
    cache_service = _ArchiveUploadService(result=3)
    container = cast(
        ServiceContainer,
        SimpleNamespace(archive_maintenance=service, retrieval=cache_service),
    )
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    monkeypatch.setattr(api_app, "utc_now", lambda: now)

    aborted = api_app._abort_incomplete_archive_multipart_uploads(
        container,
        max_age=timedelta(days=3),
    )

    assert aborted == 5
    assert service.initiated_before == datetime(2026, 7, 13, 12, tzinfo=UTC)
    assert cache_service.initiated_before == datetime(2026, 7, 13, 12, tzinfo=UTC)


def test_archive_multipart_sweep_waits_for_archive_operations() -> None:
    async def exercise() -> None:
        service = _ArchiveUploadService()
        cache_service = _ArchiveUploadService(result=0)
        container = cast(
            ServiceContainer,
            SimpleNamespace(archive_maintenance=service, retrieval=cache_service),
        )
        operation_lock = asyncio.Lock()
        await operation_lock.acquire()
        task = asyncio.create_task(
            api_app._run_archive_multipart_reaper(
                lambda: container,
                sweep_interval=timedelta(days=1),
                max_age=timedelta(days=3),
                operation_lock=operation_lock,
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert service.initiated_before is None

        operation_lock.release()
        for _ in range(100):
            if service.initiated_before is not None:
                break
            await asyncio.sleep(0.001)
        assert service.initiated_before is not None

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())


def test_retrieval_restore_poll_and_cache_cleanup_have_independent_cadences() -> None:
    async def exercise() -> None:
        retrieval = _RetrievalService()
        container = cast(ServiceContainer, SimpleNamespace(retrieval=retrieval))
        restore = asyncio.create_task(
            api_app._run_retrieval_restore_reaper(
                lambda: container,
                poll_interval=timedelta(days=1),
            )
        )
        for _ in range(100):
            if retrieval.processed:
                break
            await asyncio.sleep(0.001)
        assert retrieval.processed == 1
        assert retrieval.swept == 0
        restore.cancel()
        with pytest.raises(asyncio.CancelledError):
            await restore

        cache = asyncio.create_task(
            api_app._run_retrieval_cache_reaper(
                lambda: container,
                sweep_interval=timedelta(days=1),
            )
        )
        for _ in range(100):
            if retrieval.swept:
                break
            await asyncio.sleep(0.001)
        assert retrieval.swept == 1
        assert retrieval.processed == 1
        assert retrieval.requeued == 1
        cache.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cache

    asyncio.run(exercise())
