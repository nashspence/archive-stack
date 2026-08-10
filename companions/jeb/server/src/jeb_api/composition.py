"""Jeb server configuration and service composition."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from http_api_contracts import safe_http_base_url
from jeb_core.adapters.munchy import MunchyTargetAdapter
from jeb_core.domain.models import EligibleFile, JebConfig, TargetConfig, current_time
from jeb_core.domain.sources import SourceConfig
from jeb_core.persistence.schema import validate_state
from jeb_core.persistence.source_registry import SourceRegistry
from jeb_core.persistence.sqlite_state import SQLiteJebStore
from jeb_core.ports.target import TargetAdapter, TargetContext
from jeb_core.runtime.config import (
    config_from_env as core_config_from_env,
)
from jeb_core.runtime.config import (
    env_bool,
    env_int,
    env_value_from,
)
from jeb_core.runtime.service import JebRuntime
from jeb_core.services.attempts import JebAttemptService
from jeb_core.services.events import JebEventService
from jeb_core.services.operations import JebServiceOperations
from jeb_core.services.sources import JebSourceService
from lifecycle_events import SQLiteEventCursorStore, SQLiteLifecycleEventLog

DEFAULT_MUNCHY_BASE_URL = "http://127.0.0.1:8092"


@dataclass(slots=True)
class JebServices:
    config: JebConfig
    store: SQLiteJebStore
    events: JebEventService
    operations: JebServiceOperations
    sources: JebSourceService
    attempts: JebAttemptService
    runtime: JebRuntime
    source_registry: SourceRegistry
    target_adapters: dict[str, TargetAdapter]
    event_log: SQLiteLifecycleEventLog
    event_cursors: SQLiteEventCursorStore

    @property
    def sleep(self) -> Callable[[float], None]:
        return self.runtime.sleep

    def connect(self) -> sqlite3.Connection:
        return self.store.connect()

    def target_by_name(self, name: str) -> TargetConfig:
        return self.sources.target_by_name(name)

    def source_by_id(self, source_id: str) -> SourceConfig:
        return self.sources.source_by_id(source_id)

    def load_attempt(self, attempt_id: str) -> sqlite3.Row:
        return self.store.load_attempt(attempt_id)

    def attempt_files(self, attempt_id: str) -> list[sqlite3.Row]:
        return self.store.attempt_files(attempt_id)

    def set_attempt_state(self, attempt_id: str, state: str) -> None:
        if str(self.store.load_attempt(attempt_id)["state"]) == "canceled":
            return
        self.store.set_attempt_state(attempt_id, state)

    def record_target_preflight_failure(
        self,
        *,
        source: SourceConfig,
        files: Sequence[EligibleFile],
        error: BaseException,
    ) -> None:
        self.sources.record_target_preflight_failure(
            source=source,
            files=files,
            error=error,
        )

    def clear_target_preflight_failure(self, source_id: str) -> None:
        self.store.clear_target_preflight_failure(source_id)

    def emit_target_preflight_failures(self, *, source_id: str | None = None) -> None:
        self.sources.emit_target_preflight_failures(source_id=source_id)


def config_from_env(env: Mapping[str, str] | None = None) -> JebConfig:
    values = os.environ if env is None else env
    allow_insecure_http = env_bool(values, "JEB_MUNCHY_ALLOW_INSECURE_HTTP", False)
    target = TargetConfig(
        name="munchy",
        url=safe_http_base_url(
            env_value_from(values, "JEB_MUNCHY_URL", DEFAULT_MUNCHY_BASE_URL)
            or DEFAULT_MUNCHY_BASE_URL,
            setting="JEB_MUNCHY_URL",
            allow_insecure_http=allow_insecure_http,
        ),
        token=env_value_from(values, "JEB_MUNCHY_TOKEN", "") or "",
        allow_insecure_http=allow_insecure_http,
        upload_workers=max(1, env_int(values, "JEB_MUNCHY_UPLOAD_WORKERS", 4)),
        upload_chunk_bytes=max(1, env_int(values, "JEB_MUNCHY_UPLOAD_CHUNK_MIB", 64)) * 1024 * 1024,
        wait_for_safe_delete=env_bool(values, "JEB_MUNCHY_WAIT_FOR_SAFE_DELETE", True),
    )
    return core_config_from_env(values, targets={target.name: target})


def create_services(
    config: JebConfig,
    *,
    target_adapters: Mapping[str, TargetAdapter] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> JebServices:
    adapters: dict[str, TargetAdapter] = {"munchy": MunchyTargetAdapter()}
    adapters.update(target_adapters or {})
    store = SQLiteJebStore(config)
    event_log = SQLiteLifecycleEventLog(store.connect)
    event_cursors = SQLiteEventCursorStore(store.connect)
    source_registry = SourceRegistry(
        database=config.service.state_db,
        landing_dir=config.ingress.landing_dir,
        ftp_projection=config.ingress.ftp_projection,
        ftp_uid=config.ingress.ftp_uid,
        ftp_gid=config.ingress.ftp_gid,
    )
    operation_lock = threading.RLock()
    holder: dict[str, JebServices] = {}

    def initialize() -> None:
        validate_state(config)
        source_registry.initialize()

    def target_context() -> TargetContext:
        return holder["services"]

    events = JebEventService(config, store, event_log)
    operations = JebServiceOperations(store, initialize)
    sources = JebSourceService(
        config,
        store,
        events,
        source_registry,
        adapters,
        current_time,
        target_context,
        initialize,
    )
    attempts = JebAttemptService(
        config,
        store,
        events,
        sources,
        adapters,
        current_time,
        operation_lock,
        target_context,
    )
    runtime = JebRuntime(
        config,
        store,
        events,
        sources,
        attempts,
        source_registry,
        adapters,
        operation_lock,
        target_context,
        initialize,
        sleep,
    )
    services = JebServices(
        config=config,
        store=store,
        events=events,
        operations=operations,
        sources=sources,
        attempts=attempts,
        runtime=runtime,
        source_registry=source_registry,
        target_adapters=adapters,
        event_log=event_log,
        event_cursors=event_cursors,
    )
    holder["services"] = services
    return services
