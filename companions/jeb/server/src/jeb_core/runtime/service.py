"""Jeb service initialization and polling runtime."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from jeb_protocol import ATTEMPT_RESOLVED_STATES

import jeb_core.domain.models as domain_models
import jeb_core.persistence.sqlite_state as state_store
import jeb_core.services.attempts as attempt_service
import jeb_core.services.events as event_service
import jeb_core.services.sources as source_service
from jeb_core.ingress import (
    incomplete_tus_upload_status,
    reap_stale_incomplete_tus_uploads,
)
from jeb_core.persistence.source_registry import SourceRegistry
from jeb_core.ports.target import TargetAdapter, TargetContext

LOG = logging.getLogger("jeb")


class JebRuntime:
    def __init__(
        self,
        config: domain_models.JebConfig,
        store: state_store.SQLiteJebStore,
        events: event_service.JebEventService,
        sources: source_service.JebSourceService,
        attempts: attempt_service.JebAttemptService,
        source_registry: SourceRegistry,
        target_adapters: Mapping[str, TargetAdapter],
        operation_lock: threading.RLock,
        target_context: Callable[[], TargetContext],
        initialize: Callable[[], None],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.store = store
        self.events = events
        self.sources = sources
        self.attempts = attempts
        self.source_registry = source_registry
        self.target_adapters = dict(target_adapters)
        self.operation_lock = operation_lock
        self.target_context = target_context
        self._initialize = initialize
        self.sleep = sleep
        self._attempt_cursor: tuple[str, str] | None = None
        self._source_cursor: str | None = None

    def initialize(self) -> None:
        self._initialize()

    def run_forever(self) -> None:
        self.initialize()
        for adapter in self.target_adapters.values():
            adapter.start(self.target_context())
        while True:
            self.run_once()
            self.sleep(self.config.service.interval_seconds)

    def run_once(self) -> None:
        with self.operation_lock:
            self.initialize()
            reap = reap_stale_incomplete_tus_uploads(
                self.config.ingress,
                self.source_registry,
            )
            if reap["terminated"] or reap["already_absent"]:
                LOG.info(
                    "terminated %s stale incomplete TUS upload(s); %s already absent",
                    reap["terminated"],
                    reap["already_absent"],
                )
            if reap["failed"] or reap["scan_error"]:
                LOG.warning(
                    "incomplete TUS upload cleanup was not fully successful: "
                    "failed=%s scan_error=%s",
                    reap["failed"],
                    reap["scan_error"],
                )
            self.sources.resolve_inactive_target_preflight_failures()
            attempts = self.store.unresolved_attempts(after=self._attempt_cursor)
            if not attempts and self._attempt_cursor is not None:
                attempts = self.store.unresolved_attempts()
            for attempt in attempts:
                self.attempts.process_attempt(str(attempt["id"]))
            self._attempt_cursor = (
                (str(attempts[-1]["created_at"]), str(attempts[-1]["id"])) if attempts else None
            )
            sources = self.source_registry.enabled_after(
                after_id=self._source_cursor,
                limit=100,
            )
            if not sources and self._source_cursor is not None:
                sources = self.source_registry.enabled_after(after_id=None, limit=100)
            for source in sources:
                if self.store.source_has_unresolved_attempt(source.id):
                    continue
                self.attempts.discover_source(source)
            self._source_cursor = sources[-1].id if sources else None
            self.sources.emit_target_preflight_failures()

    def status_summary(self, *, include_backlog: bool = True) -> dict[str, Any]:
        state_counts = self.store.batch_state_counts()
        total_batches = sum(state_counts.values())
        resolved_count = sum(
            count for state, count in state_counts.items() if state in ATTEMPT_RESOLVED_STATES
        )
        active_preflight_failures = [
            self.store._target_preflight_failure_summary(row)
            for row in self.store.target_preflight_failures(state="failed", limit=10)
        ]
        return {
            "sources": self.sources.source_statuses(include_backlog=include_backlog),
            "batches": {
                "total": total_batches,
                "unresolved": total_batches - resolved_count,
                "resolved": resolved_count,
                "states": state_counts,
            },
            "unresolved_attempts": self.store.list_attempts(
                resolution="unresolved",
                sort="updated_at",
                order="desc",
                page=1,
                per_page=10,
            ),
            "recent_failures": self.store.list_attempts(
                resolution="all",
                states=("failed", "cleanup_failed"),
                sort="updated_at",
                order="desc",
                page=1,
                per_page=5,
            ),
            "target_preflight_failures": {
                "total": self.store.target_preflight_failure_count(state="failed"),
                "failures": active_preflight_failures,
            },
            "incomplete_tus_uploads": incomplete_tus_upload_status(
                self.config.ingress,
                self.source_registry,
            ),
        }
