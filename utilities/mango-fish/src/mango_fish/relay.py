from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import yaml
from lifecycle_events.models import CLOUDEVENTS_JSON_CONTENT_TYPE, EventPage
from pydantic import BaseModel, ConfigDict, Field, field_validator

LOG = logging.getLogger(__name__)


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    events_url: str = Field(min_length=1)
    token_env: str = Field(min_length=1)
    webhook_url_env: str = Field(min_length=1)


class MangoFishConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    state_path: Path
    poll_interval_seconds: float = Field(default=5.0, gt=0)
    request_timeout_seconds: float = Field(default=10.0, gt=0)
    batch_size: int = Field(default=100, ge=1, le=100)
    sources: tuple[SourceConfig, ...] = Field(min_length=1)

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("Mango Fish config version must be 1")
        return value

    @field_validator("sources")
    @classmethod
    def unique_sources(cls, value: tuple[SourceConfig, ...]) -> tuple[SourceConfig, ...]:
        names = [source.name for source in value]
        if len(names) != len(set(names)):
            raise ValueError("Mango Fish source names must be unique")
        return value


def load_config(path: Path) -> MangoFishConfig:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("Mango Fish config must be a YAML object")
    config = MangoFishConfig.model_validate(payload)
    for source in config.sources:
        if not os.getenv(source.token_env, "").strip():
            raise ValueError(f"Mango Fish source {source.name} requires {source.token_env}")
        if not os.getenv(source.webhook_url_env, "").strip():
            raise ValueError(f"Mango Fish source {source.name} requires {source.webhook_url_env}")
    return config


class CursorState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS source_cursors (
                    source TEXT PRIMARY KEY,
                    cursor TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def cursor(self, source: str) -> str:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT cursor FROM source_cursors WHERE source = ?", (source,)
            ).fetchone()
        return str(row[0]) if row is not None else "0"

    def advance(self, source: str, cursor: str) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO source_cursors(source, cursor) VALUES(?, ?)
                ON CONFLICT(source) DO UPDATE SET cursor = excluded.cursor
                """,
                (source, cursor),
            )
            connection.commit()


class MangoFish:
    def __init__(self, config: MangoFishConfig) -> None:
        self.config = config
        self.state = CursorState(config.state_path)
        self.state.initialize()

    def relay_source_once(
        self,
        source: SourceConfig,
        *,
        client: httpx.Client | None = None,
    ) -> int:
        owns_client = client is None
        http = client or httpx.Client(timeout=self.config.request_timeout_seconds)
        try:
            cursor = self.state.cursor(source.name)
            response = http.get(
                source.events_url,
                params={"after": cursor, "limit": self.config.batch_size},
                headers={"Authorization": f"Bearer {os.environ[source.token_env]}"},
            )
            response.raise_for_status()
            page = EventPage.model_validate(response.json())
            delivered = 0
            for event in page.events:
                delivery = http.post(
                    os.environ[source.webhook_url_env],
                    content=event.model_dump_json(exclude_none=True),
                    headers={"Content-Type": CLOUDEVENTS_JSON_CONTENT_TYPE},
                )
                delivery.raise_for_status()
                delivered += 1
            if page.events or page.next_cursor != cursor:
                self.state.advance(source.name, page.next_cursor)
            return delivered
        finally:
            if owns_client:
                http.close()

    def run_once(self) -> int:
        delivered = 0
        with httpx.Client(timeout=self.config.request_timeout_seconds) as client:
            for source in self.config.sources:
                try:
                    delivered += self.relay_source_once(source, client=client)
                except Exception:
                    LOG.exception("Mango Fish source failed: %s", source.name)
        return delivered

    def run(self) -> None:
        stop = threading.Event()
        threads = [
            threading.Thread(
                target=self._run_source,
                args=(source, stop),
                name=f"mango-fish-{source.name}",
                daemon=False,
            )
            for source in self.config.sources
        ]
        for thread in threads:
            thread.start()
        try:
            for thread in threads:
                thread.join()
        except KeyboardInterrupt:
            stop.set()
            for thread in threads:
                thread.join(timeout=self.config.request_timeout_seconds + 1)

    def _run_source(self, source: SourceConfig, stop: threading.Event) -> None:
        with httpx.Client(timeout=self.config.request_timeout_seconds) as client:
            while not stop.is_set():
                try:
                    delivered = self.relay_source_once(source, client=client)
                    if delivered:
                        continue
                except Exception:
                    LOG.exception("Mango Fish source failed: %s", source.name)
                stop.wait(self.config.poll_interval_seconds)


def summarize_config(config: MangoFishConfig) -> dict[str, Any]:
    return {
        "state_path": str(config.state_path),
        "sources": [source.name for source in config.sources],
        "poll_interval_seconds": config.poll_interval_seconds,
        "request_timeout_seconds": config.request_timeout_seconds,
        "batch_size": config.batch_size,
    }


__all__ = [
    "MangoFish",
    "MangoFishConfig",
    "SourceConfig",
    "load_config",
    "summarize_config",
]
