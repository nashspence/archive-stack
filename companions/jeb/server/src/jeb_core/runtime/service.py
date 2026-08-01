"""Jeb service initialization and polling runtime."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

import jeb_core.domain.models as domain_models
import jeb_core.persistence.sqlite_state as state_store
import jeb_core.services.attempts as attempt_service
import jeb_core.services.events as event_service
import jeb_core.services.sources as source_service
from jeb_core.domain.models import (
    TERMINAL_STATES,
)
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
            for attempt_id in self.store.active_attempt_ids():
                self.attempts.process_attempt(attempt_id)
            active_sources = {str(row["source_id"]) for row in self.store.active_attempts()}
            for source in self.source_registry.list():
                if source.enabled and source.id not in active_sources:
                    self.attempts.discover_source(source)
            self.sources.emit_target_preflight_failures()

    def status_summary(self, *, include_backlog: bool = True) -> dict[str, Any]:
        state_counts = self.store.batch_state_counts()
        total_batches = sum(state_counts.values())
        terminal_count = sum(
            count for state, count in state_counts.items() if state in TERMINAL_STATES
        )
        active_preflight_failures = [
            self.store._target_preflight_failure_summary(row)
            for row in self.store.target_preflight_failures(state="failed")
        ]
        return {
            "sources": self.sources.source_statuses(include_backlog=include_backlog),
            "batches": {
                "total": total_batches,
                "active": total_batches - terminal_count,
                "terminal": terminal_count,
                "states": state_counts,
            },
            "active_attempts": self.store.list_attempts(
                terminal="active",
                sort="updated_at",
                order="desc",
                page=1,
                per_page=10,
            ),
            "recent_failures": self.store.list_attempts(
                terminal="all",
                states=("failed", "cleanup_failed"),
                sort="updated_at",
                order="desc",
                page=1,
                per_page=5,
            ),
            "target_preflight_failures": {
                "total": len(active_preflight_failures),
                "failures": active_preflight_failures,
            },
            "incomplete_tus_uploads": incomplete_tus_upload_status(
                self.config.ingress,
                self.source_registry,
            ),
        }
