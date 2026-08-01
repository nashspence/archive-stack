"""Boundary implemented by Jeb delivery targets."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from lifecycle_events import SQLiteEventCursorStore, SQLiteLifecycleEventLog

from jeb_core.domain.models import EligibleFile, JebConfig, TargetConfig
from jeb_core.domain.sources import SourceConfig


class TargetContext(Protocol):
    config: JebConfig
    event_log: SQLiteLifecycleEventLog
    event_cursors: SQLiteEventCursorStore

    @property
    def sleep(self) -> Callable[[float], None]: ...

    def connect(self) -> sqlite3.Connection: ...

    def target_by_name(self, name: str) -> TargetConfig: ...

    def source_by_id(self, source_id: str) -> SourceConfig: ...

    def load_attempt(self, attempt_id: str) -> sqlite3.Row: ...

    def attempt_files(self, attempt_id: str) -> list[sqlite3.Row]: ...

    def set_attempt_state(self, attempt_id: str, state: str) -> None: ...

    def record_target_preflight_failure(
        self,
        *,
        source: SourceConfig,
        files: Sequence[EligibleFile],
        error: BaseException,
    ) -> None: ...

    def clear_target_preflight_failure(self, source_id: str) -> None: ...

    def emit_target_preflight_failures(self, *, source_id: str | None = None) -> None: ...


class TargetAdapter(Protocol):
    name: str

    def start(self, context: TargetContext) -> None: ...

    def is_transient_error(self, error: BaseException) -> bool: ...

    def advance(self, context: TargetContext, attempt_id: str) -> None: ...

    def cancel(self, context: TargetContext, attempt_id: str) -> None: ...

    def normalize_source_config(self, config: Mapping[str, Any]) -> dict[str, Any]: ...

    def preflight(
        self,
        context: TargetContext,
        source: SourceConfig,
        files: Sequence[EligibleFile],
        *,
        record_failures: bool,
    ) -> tuple[list[EligibleFile] | None, dict[str, Any]]: ...
