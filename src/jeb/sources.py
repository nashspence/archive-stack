from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from riverhog_core.timestamps import format_utc_timestamp, utc_now

Cadence = Literal["weekly", "monthly", "seasonal", "manual"]
Cleanup = Literal["never", "after_target_success"]
SOURCE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
INGRESS_ADAPTERS = frozenset({"ftp", "tus"})
DEFAULT_INCLUDE_EXTENSIONS = frozenset(
    {".mp4", ".mov", ".mkv", ".webm", ".xml", ".json", ".txt"}
)
PASSWORD_HASHER = PasswordHasher()


class SourceRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class SourceConfig:
    id: str
    enabled: bool
    path: Path
    adapters: tuple[str, ...]
    stable_seconds: int
    include_extensions: frozenset[str]
    collection_slug: str
    target: str
    notify: Mapping[str, Any]
    threshold_bytes: int
    cleanup: Cleanup
    cadence: Cadence
    weekday: int
    hour: int
    minute: int
    policy: Mapping[str, Any] = field(default_factory=dict)
    policy_revision: int = 1

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "enabled": self.enabled,
            "adapters": list(self.adapters),
            "stable_seconds": self.stable_seconds,
            "include_extensions": sorted(self.include_extensions),
            "collection_slug": self.collection_slug,
            "target": self.target,
            "notify": dict(self.notify),
            "threshold_bytes": self.threshold_bytes,
            "cleanup": self.cleanup,
            "cadence": self.cadence,
            "weekday": self.weekday,
            "hour": self.hour,
            "minute": self.minute,
            "policy_revision": self.policy_revision,
            "policy": dict(self.policy),
        }


class SourceRegistry:
    def __init__(
        self,
        *,
        database: Path,
        landing_dir: Path,
        ftp_projection: Path,
        ftp_uid: int = 1000,
        ftp_gid: int = 1000,
    ) -> None:
        self.database = database
        self.landing_dir = landing_dir
        self.ftp_projection = ftp_projection
        self.ftp_uid = ftp_uid
        self.ftp_gid = ftp_gid

    def connect(self) -> sqlite3.Connection:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    adapters_json TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    upload_signing_key TEXT NOT NULL,
                    stable_seconds INTEGER NOT NULL,
                    include_extensions_json TEXT NOT NULL,
                    collection_slug TEXT NOT NULL,
                    target TEXT NOT NULL,
                    notify_json TEXT NOT NULL,
                    threshold_bytes INTEGER NOT NULL,
                    cleanup TEXT NOT NULL,
                    cadence TEXT NOT NULL,
                    weekday INTEGER NOT NULL,
                    hour INTEGER NOT NULL,
                    minute INTEGER NOT NULL,
                    policy_revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_policy_revisions (
                    source_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    policy_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(source_id, revision),
                    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
                );
                """
            )
        self.write_ftp_projection()

    def add(
        self,
        source_id: str,
        *,
        adapters: Sequence[str],
        policy: Mapping[str, Any],
        credential: str | None = None,
        enabled: bool = True,
        stable_seconds: int = 600,
        include_extensions: Sequence[str] = tuple(sorted(DEFAULT_INCLUDE_EXTENSIONS)),
        collection_slug: str | None = None,
        target: str = "munchy",
        notify: Mapping[str, Any] | None = None,
        threshold_bytes: int = 0,
        cleanup: Cleanup = "after_target_success",
        cadence: Cadence = "weekly",
        weekday: int = 0,
        hour: int = 3,
        minute: int = 0,
    ) -> tuple[SourceConfig, str | None]:
        normalized_id = _source_id(source_id)
        normalized_adapters = _adapters(adapters)
        _settings(
            stable_seconds=stable_seconds,
            threshold_bytes=threshold_bytes,
            cleanup=cleanup,
            cadence=cadence,
            weekday=weekday,
            hour=hour,
            minute=minute,
        )
        normalized_extensions = _extensions(include_extensions)
        normalized_policy = _json_object(policy, "policy")
        normalized_notify = _json_object(notify or {}, "notify")
        secret = credential or secrets.token_urlsafe(24)
        if not secret or "\n" in secret or "\r" in secret:
            raise SourceRegistryError("credential must be non-empty and single-line")
        now = format_utc_timestamp(utc_now())
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO sources(
                        id, enabled, adapters_json, password_hash, upload_signing_key,
                        stable_seconds, include_extensions_json, collection_slug, target,
                        notify_json, threshold_bytes, cleanup, cadence, weekday, hour, minute,
                        policy_revision, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        normalized_id,
                        int(enabled),
                        _json(normalized_adapters),
                        PASSWORD_HASHER.hash(secret),
                        secrets.token_hex(32),
                        stable_seconds,
                        _json(sorted(normalized_extensions)),
                        collection_slug or normalized_id,
                        target.strip() or "munchy",
                        _json(normalized_notify),
                        threshold_bytes,
                        cleanup,
                        cadence,
                        weekday,
                        hour,
                        minute,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO source_policy_revisions(
                        source_id, revision, policy_json, created_at
                    )
                    VALUES(?, 1, ?, ?)
                    """,
                    (normalized_id, _json(normalized_policy), now),
                )
        except sqlite3.IntegrityError as exc:
            raise SourceRegistryError(f"source already exists: {normalized_id}") from exc
        self.write_ftp_projection()
        return self.get(normalized_id), secret if credential is None else None

    def list(self) -> list[SourceConfig]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM sources ORDER BY id").fetchall()
            return [self._source(connection, row) for row in rows]

    def get(self, source_id: str) -> SourceConfig:
        normalized_id = _source_id(source_id)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE id = ?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise SourceRegistryError(f"source not found: {normalized_id}")
            return self._source(connection, row)

    def set_enabled(self, source_id: str, enabled: bool) -> SourceConfig:
        normalized_id = _source_id(source_id)
        with self.connect() as connection:
            changed = connection.execute(
                "UPDATE sources SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), format_utc_timestamp(utc_now()), normalized_id),
            ).rowcount
            if not changed:
                raise SourceRegistryError(f"source not found: {normalized_id}")
        self.write_ftp_projection()
        return self.get(normalized_id)

    def update(self, source_id: str, changes: Mapping[str, Any]) -> SourceConfig:
        current = self.get(source_id)
        allowed = {
            "adapters",
            "stable_seconds",
            "include_extensions",
            "collection_slug",
            "target",
            "notify",
            "threshold_bytes",
            "cleanup",
            "cadence",
            "weekday",
            "hour",
            "minute",
            "policy",
        }
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise SourceRegistryError("unknown source setting(s): " + ", ".join(unknown))
        values = current.summary()
        values.update(dict(changes))
        adapters = _adapters(values["adapters"])
        extensions = _extensions(values["include_extensions"])
        stable_seconds = int(values["stable_seconds"])
        threshold_bytes = int(values["threshold_bytes"])
        cleanup = str(values["cleanup"])
        cadence = str(values["cadence"])
        weekday = int(values["weekday"])
        hour = int(values["hour"])
        minute = int(values["minute"])
        _settings(
            stable_seconds=stable_seconds,
            threshold_bytes=threshold_bytes,
            cleanup=cleanup,
            cadence=cadence,
            weekday=weekday,
            hour=hour,
            minute=minute,
        )
        notify = _json_object(values["notify"], "notify")
        policy = _json_object(values["policy"], "policy")
        policy_changed = "policy" in changes and policy != dict(current.policy)
        revision = current.policy_revision + int(policy_changed)
        now = format_utc_timestamp(utc_now())
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE sources
                SET adapters_json = ?, stable_seconds = ?, include_extensions_json = ?,
                    collection_slug = ?, target = ?, notify_json = ?, threshold_bytes = ?,
                    cleanup = ?, cadence = ?, weekday = ?, hour = ?, minute = ?,
                    policy_revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    _json(adapters),
                    stable_seconds,
                    _json(sorted(extensions)),
                    str(values["collection_slug"]).strip() or current.id,
                    str(values["target"]).strip() or current.target,
                    _json(notify),
                    threshold_bytes,
                    cleanup,
                    cadence,
                    weekday,
                    hour,
                    minute,
                    revision,
                    now,
                    current.id,
                ),
            )
            if policy_changed:
                connection.execute(
                    """
                    INSERT INTO source_policy_revisions(
                        source_id, revision, policy_json, created_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (current.id, revision, _json(policy), now),
                )
        self.write_ftp_projection()
        return self.get(current.id)

    def rotate_credential(
        self,
        source_id: str,
        *,
        credential: str | None = None,
    ) -> tuple[SourceConfig, str | None]:
        current = self.get(source_id)
        secret = credential or secrets.token_urlsafe(24)
        if not secret or "\n" in secret or "\r" in secret:
            raise SourceRegistryError("credential must be non-empty and single-line")
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE sources
                SET password_hash = ?, upload_signing_key = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    PASSWORD_HASHER.hash(secret),
                    secrets.token_hex(32),
                    format_utc_timestamp(utc_now()),
                    current.id,
                ),
            )
        self.write_ftp_projection()
        return self.get(current.id), secret if credential is None else None

    def authenticate(self, source_id: str, credential: str, *, adapter: str) -> SourceConfig:
        source = self.get(source_id)
        if not source.enabled or adapter not in source.adapters:
            raise SourceRegistryError("invalid Jeb ingress credentials")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM sources WHERE id = ?",
                (source.id,),
            ).fetchone()
        try:
            PASSWORD_HASHER.verify(str(row["password_hash"]), credential)
        except (InvalidHashError, VerifyMismatchError) as exc:
            raise SourceRegistryError("invalid Jeb ingress credentials") from exc
        return source

    def signing_key(self, source_id: str) -> str:
        source = self.get(source_id)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT upload_signing_key FROM sources WHERE id = ?",
                (source.id,),
            ).fetchone()
        return str(row["upload_signing_key"])

    def delete(self, source_id: str) -> None:
        normalized_id = _source_id(source_id)
        with self.connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            changed = connection.execute(
                "DELETE FROM sources WHERE id = ?",
                (normalized_id,),
            ).rowcount
            if not changed:
                raise SourceRegistryError(f"source not found: {normalized_id}")
        self.write_ftp_projection()

    def write_ftp_projection(self) -> None:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, password_hash
                FROM sources
                WHERE enabled = 1 AND adapters_json LIKE '%\"ftp\"%'
                ORDER BY id
                """
            ).fetchall()
        lines = [
            self._ftp_record(str(row["id"]), str(row["password_hash"]))
            for row in rows
        ]
        self.ftp_projection.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.ftp_projection.with_name(
            f".{self.ftp_projection.name}.{uuid.uuid4().hex}.part"
        )
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write("".join(lines))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.ftp_projection)
        finally:
            temporary.unlink(missing_ok=True)

    def _ftp_record(self, source_id: str, password_hash: str) -> str:
        home = (self.landing_dir / source_id).as_posix().rstrip("/") + "/./"
        return (
            f"{source_id}:{password_hash}:{self.ftp_uid}:{self.ftp_gid}::{home}"
            "::::::::::::\n"
        )

    def _source(self, connection: sqlite3.Connection, row: sqlite3.Row) -> SourceConfig:
        revision = int(row["policy_revision"])
        policy_row = connection.execute(
            """
            SELECT policy_json FROM source_policy_revisions
            WHERE source_id = ? AND revision = ?
            """,
            (row["id"], revision),
        ).fetchone()
        if policy_row is None:
            raise SourceRegistryError(f"source policy is missing: {row['id']} revision {revision}")
        return SourceConfig(
            id=str(row["id"]),
            enabled=bool(row["enabled"]),
            path=self.landing_dir / str(row["id"]),
            adapters=tuple(json.loads(row["adapters_json"])),
            stable_seconds=int(row["stable_seconds"]),
            include_extensions=frozenset(json.loads(row["include_extensions_json"])),
            collection_slug=str(row["collection_slug"]),
            target=str(row["target"]),
            notify=json.loads(row["notify_json"]),
            threshold_bytes=int(row["threshold_bytes"]),
            cleanup=cast(Cleanup, str(row["cleanup"])),
            cadence=cast(Cadence, str(row["cadence"])),
            weekday=int(row["weekday"]),
            hour=int(row["hour"]),
            minute=int(row["minute"]),
            policy=json.loads(policy_row["policy_json"]),
            policy_revision=revision,
        )


def _source_id(value: str) -> str:
    normalized = value.strip()
    if not SOURCE_ID.fullmatch(normalized):
        raise SourceRegistryError(f"source must be a safe slug: {value!r}")
    return normalized


def _adapters(values: Sequence[Any]) -> tuple[str, ...]:
    adapters = tuple(dict.fromkeys(str(value).strip().casefold() for value in values))
    if not adapters:
        raise SourceRegistryError("source must enable at least one ingress adapter")
    unknown = sorted(set(adapters) - INGRESS_ADAPTERS)
    if unknown:
        raise SourceRegistryError("unknown ingress adapter(s): " + ", ".join(unknown))
    return adapters


def _extensions(values: Sequence[Any]) -> frozenset[str]:
    extensions = frozenset(str(value).strip().lower() for value in values)
    if not extensions or any(not value.startswith(".") for value in extensions):
        raise SourceRegistryError("include_extensions must contain file extensions")
    return extensions


def _settings(
    *,
    stable_seconds: int,
    threshold_bytes: int,
    cleanup: str,
    cadence: str,
    weekday: int,
    hour: int,
    minute: int,
) -> None:
    if stable_seconds < 0 or threshold_bytes < 0:
        raise SourceRegistryError("source ages and byte thresholds must be non-negative")
    if cleanup not in {"never", "after_target_success"}:
        raise SourceRegistryError("cleanup must be never or after_target_success")
    if cadence not in {"weekly", "monthly", "seasonal", "manual"}:
        raise SourceRegistryError("unknown source cadence")
    if not 0 <= weekday <= 6 or not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise SourceRegistryError("source schedule values are out of range")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceRegistryError(f"{name} must be an object")
    normalized = json.loads(_json(dict(value)))
    if not isinstance(normalized, dict):
        raise SourceRegistryError(f"{name} must be an object")
    return normalized
