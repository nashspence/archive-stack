from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from riverhog_api import app as api_app
from riverhog_api.deps import ServiceContainer


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
        requeue_interrupted_orphan_discards_for_startup=Mock(return_value=1),
        process_due_provenance_journal_validations=Mock(return_value=0),
        process_due_finalizations=Mock(return_value=1),
        reap_expired_custody_transfers=Mock(return_value=1),
    )
    archive_copies = SimpleNamespace(
        requeue_interrupted_copies_for_startup=Mock(return_value=0),
        process_due=Mock(return_value=0),
    )
    collection_workflows = SimpleNamespace(
        requeue_interrupted_disposition_sets_for_startup=Mock(return_value=0),
        reap_expired_claims=Mock(return_value=0),
        process_due_disposition_sets=Mock(return_value=0),
        process_due_outcome_sets=Mock(return_value=0),
    )
    provenance = SimpleNamespace(
        requeue_interrupted_verifications_for_startup=Mock(return_value=0),
        process_due_verifications=Mock(return_value=0),
    )
    lifecycle_events = SimpleNamespace(reap_expired_contexts=Mock(return_value=1))
    collection_deletions = SimpleNamespace(process_due=Mock(return_value=0))
    retrieval = SimpleNamespace(
        request_cache_accounting_reconciliation_for_startup=Mock(return_value=1),
        process_cache_accounting_reconciliation=Mock(return_value=0),
    )
    container = cast(
        ServiceContainer,
        SimpleNamespace(
            collection_uploads=collection_uploads,
            collection_workflows=collection_workflows,
            archive_copies=archive_copies,
            collection_deletions=collection_deletions,
            retrieval=retrieval,
            provenance=provenance,
            lifecycle_events=lifecycle_events,
        ),
    )

    assert api_app._process_archive_maintenance(container, startup_recovery=True) is True

    collection_uploads.requeue_interrupted_finalizations_for_startup.assert_called_once_with(
        limit=100
    )
    collection_uploads.requeue_interrupted_orphan_discards_for_startup.assert_called_once_with(
        limit=100
    )
    collection_uploads.process_due_finalizations.assert_called_once_with(limit=1)
    collection_uploads.reap_expired_custody_transfers.assert_called_once_with(limit=100)
    collection_workflows.reap_expired_claims.assert_called_once_with(limit=100)
    collection_workflows.process_due_disposition_sets.assert_called_once_with(limit=1)
    collection_workflows.process_due_outcome_sets.assert_called_once_with(limit=1)
    provenance.requeue_interrupted_verifications_for_startup.assert_called_once_with()
    provenance.process_due_verifications.assert_called_once_with(limit=1)
    collection_deletions.process_due.assert_called_once_with(limit=1)
    retrieval.request_cache_accounting_reconciliation_for_startup.assert_called_once_with()
    retrieval.process_cache_accounting_reconciliation.assert_called_once_with(limit=100)
    lifecycle_events.reap_expired_contexts.assert_called_once_with()


def test_archive_maintenance_drains_bounded_progress_before_idle_interval() -> None:
    async def exercise() -> None:
        finalizations = Mock(side_effect=[1, 1, 0])
        zero = Mock(return_value=0)
        container = cast(
            ServiceContainer,
            SimpleNamespace(
                collection_uploads=SimpleNamespace(
                    requeue_interrupted_finalizations_for_startup=zero,
                    requeue_interrupted_orphan_discards_for_startup=zero,
                    process_due_provenance_journal_validations=zero,
                    process_due_finalizations=finalizations,
                    reap_expired_custody_transfers=zero,
                ),
                collection_workflows=SimpleNamespace(
                    requeue_interrupted_disposition_sets_for_startup=zero,
                    reap_expired_claims=zero,
                    process_due_disposition_sets=zero,
                    process_due_outcome_sets=zero,
                ),
                archive_copies=SimpleNamespace(
                    requeue_interrupted_copies_for_startup=zero,
                    process_due=zero,
                ),
                collection_deletions=SimpleNamespace(process_due=zero),
                retrieval=SimpleNamespace(
                    request_cache_accounting_reconciliation_for_startup=zero,
                    process_cache_accounting_reconciliation=zero,
                ),
                provenance=SimpleNamespace(
                    requeue_interrupted_verifications_for_startup=zero,
                    process_due_verifications=zero,
                ),
                lifecycle_events=SimpleNamespace(reap_expired_contexts=zero),
            ),
        )
        task = asyncio.create_task(
            api_app._run_archive_upload_reaper(
                lambda: container,
                sweep_interval=timedelta(days=1),
                operation_lock=asyncio.Lock(),
            )
        )
        for _ in range(100):
            if finalizations.call_count >= 3:
                break
            await asyncio.sleep(0.001)
        assert finalizations.call_count == 3
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

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


def test_retrieval_restore_reaper_drains_restartable_steps_before_idle_interval() -> None:
    async def exercise() -> None:
        process_due = Mock(side_effect=[10, 1, 0])
        container = cast(
            ServiceContainer,
            SimpleNamespace(retrieval=SimpleNamespace(process_due=process_due)),
        )
        task = asyncio.create_task(
            api_app._run_retrieval_restore_reaper(
                lambda: container,
                poll_interval=timedelta(days=1),
            )
        )
        for _ in range(100):
            if process_due.call_count >= 3:
                break
            await asyncio.sleep(0.001)
        assert process_due.call_count == 3
        assert [current.kwargs for current in process_due.call_args_list] == [
            {"limit": 10},
            {"limit": 10},
            {"limit": 10},
        ]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
