from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from typing import Any

import jeb_core.persistence.sqlite_state as state_store
from jeb_core.domain.models import UnrecoverableJebError, event_timestamp

LOG = logging.getLogger("jeb")


class JebServiceOperations:
    """Run API-triggered work while keeping its operator-visible state durable."""

    _HISTORY_LIMIT = 100

    def __init__(
        self,
        store: state_store.SQLiteJebStore,
        initialize: Callable[[], None],
    ) -> None:
        self._store = store
        self._initialize_state = initialize
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    def _initialize(self) -> None:
        self._initialize_state()

    def recover_interrupted(self) -> int:
        self._initialize()
        return self._store.recover_interrupted_service_operations()

    def _prune_locked(self) -> None:
        active = self._store.active_service_operation()
        if active is None:
            return
        operation_id = str(active["id"])
        thread = self._threads.get(operation_id)
        if thread is not None and not thread.is_alive():
            self._store.complete_service_operation(
                operation_id,
                state="failed",
                failure="operation thread ended without reporting a result",
                completed_at=event_timestamp(),
            )
            self._threads.pop(operation_id, None)

    def active_summary(self) -> dict[str, Any] | None:
        self._initialize()
        with self._lock:
            self._prune_locked()
            return self._store.active_service_operation()

    def get(self, operation_id: str) -> dict[str, Any]:
        self._initialize()
        with self._lock:
            self._prune_locked()
            return self._store.get_service_operation(operation_id)

    def list_page(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        query: str | None,
        state: str | None,
        all_items: bool,
    ) -> dict[str, Any]:
        self._initialize()
        with self._lock:
            self._prune_locked()
            return self._store.list_service_operations(
                page=page,
                per_page=per_page,
                sort=sort,
                order=order,
                query=query,
                state=state,
                all_items=all_items,
            )

    def _start_locked(
        self,
        *,
        operation: str,
        run: Callable[[], None],
        source: str | None,
        attempt_id: str | None,
    ) -> dict[str, Any]:
        operation_id = uuid.uuid4().hex[:12]
        started_at = event_timestamp()
        current = self._store.create_service_operation(
            operation_id=operation_id,
            operation=operation,
            started_at=started_at,
            source=source,
            attempt_id=attempt_id,
        )

        def target() -> None:
            failure: str | None = None
            try:
                run()
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
                LOG.exception(
                    "Jeb service operation failed",
                    extra={"operation": operation, "operation_id": operation_id},
                )
            finally:
                with self._lock:
                    self._store.complete_service_operation(
                        operation_id,
                        state="failed" if failure is not None else "succeeded",
                        failure=failure,
                        completed_at=event_timestamp(),
                        history_limit=self._HISTORY_LIMIT,
                    )
                    self._threads.pop(operation_id, None)

        thread = threading.Thread(
            target=target,
            name=f"jeb-{operation}-{operation_id}",
            daemon=True,
        )
        self._threads[operation_id] = thread
        try:
            thread.start()
        except Exception as exc:
            self._threads.pop(operation_id, None)
            self._store.complete_service_operation(
                operation_id,
                state="failed",
                failure=f"{type(exc).__name__}: {exc}",
                completed_at=event_timestamp(),
                history_limit=self._HISTORY_LIMIT,
            )
            raise
        return current

    def start(
        self,
        *,
        operation: str,
        run: Callable[[], None],
        source: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        self._initialize()
        with self._lock:
            self._prune_locked()
            active_summary = self._store.active_service_operation()
            if active_summary is not None:
                raise UnrecoverableJebError(
                    "Jeb operation already running: "
                    f"{active_summary['operation']} {active_summary['id']}"
                )
            return self._start_locked(
                operation=operation,
                run=run,
                source=source,
                attempt_id=attempt_id,
            )

    def prepare_and_start(
        self,
        *,
        operation: str,
        prepare: Callable[[], str | None],
        run: Callable[[str], None],
        source: str | None = None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        self._initialize()
        with self._lock:
            self._prune_locked()
            active_summary = self._store.active_service_operation()
            if active_summary is not None:
                raise UnrecoverableJebError(
                    "Jeb operation already running: "
                    f"{active_summary['operation']} {active_summary['id']}"
                )
            attempt_id = prepare()
            if attempt_id is None:
                return None, None
            return attempt_id, self._start_locked(
                operation=operation,
                run=lambda: run(attempt_id),
                source=source,
                attempt_id=attempt_id,
            )
