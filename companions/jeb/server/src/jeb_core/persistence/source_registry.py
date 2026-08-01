"""SQLite-backed Jeb source registry."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from jeb_protocol import MAX_LIST_PAGE_SIZE, SOURCE_LIST_SORT_FIELDS
from time_formats import format_utc_timestamp, utc_now

from jeb_core.domain.sources import (
    Cadence,
    Cleanup,
    SourceConfig,
    SourceRegistryError,
)
from jeb_core.persistence.sql import like_literal

SOURCE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
INGRESS_ADAPTERS = frozenset({"ftp", "tus"})
DEFAULT_INCLUDE_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".xml", ".json", ".txt"})
PASSWORD_HASHER = PasswordHasher()


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
                    target TEXT NOT NULL,
                    target_config_json TEXT NOT NULL,
                    threshold_bytes INTEGER NOT NULL,
                    cleanup TEXT NOT NULL,
                    cadence TEXT NOT NULL,
                    weekday INTEGER NOT NULL,
                    hour INTEGER NOT NULL,
                    minute INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
        self.write_ftp_projection()

    def add(
        self,
        source_id: str,
        *,
        adapters: Sequence[str],
        target_config: Mapping[str, Any],
        credential: str | None = None,
        enabled: bool = True,
        stable_seconds: int = 600,
        include_extensions: Sequence[str] = tuple(sorted(DEFAULT_INCLUDE_EXTENSIONS)),
        target: str = "munchy",
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
        normalized_target_config = _target_config(target_config)
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
                        stable_seconds, include_extensions_json, target, target_config_json,
                        threshold_bytes, cleanup, cadence, weekday, hour, minute,
                        created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_id,
                        int(enabled),
                        _json(normalized_adapters),
                        PASSWORD_HASHER.hash(secret),
                        secrets.token_hex(32),
                        stable_seconds,
                        _json(sorted(normalized_extensions)),
                        target.strip() or "munchy",
                        _json(normalized_target_config),
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
        except sqlite3.IntegrityError as exc:
            if getattr(exc, "sqlite_errorname", "") == "SQLITE_CONSTRAINT_PRIMARYKEY":
                raise SourceRegistryError(f"source already exists: {normalized_id}") from exc
            raise SourceRegistryError("source registry rejected source enrollment") from exc
        self.write_ftp_projection()
        return self.get(normalized_id), secret if credential is None else None

    def list(self) -> list[SourceConfig]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM sources ORDER BY id").fetchall()
            return [self._source(connection, row) for row in rows]

    def list_page(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "id",
        order: str = "asc",
        query: str | None = None,
        enabled: bool | None = None,
        adapter: str | None = None,
        target: str | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        if page < 1:
            raise SourceRegistryError("page must be >= 1")
        if not 1 <= per_page <= MAX_LIST_PAGE_SIZE:
            raise SourceRegistryError(f"per_page must be between 1 and {MAX_LIST_PAGE_SIZE}")
        if sort not in SOURCE_LIST_SORT_FIELDS:
            raise SourceRegistryError(
                "sort must be one of: " + ", ".join(sorted(SOURCE_LIST_SORT_FIELDS))
            )
        if order not in {"asc", "desc"}:
            raise SourceRegistryError("order must be asc or desc")
        if adapter is not None and adapter not in INGRESS_ADAPTERS:
            raise SourceRegistryError(
                "adapter must be one of: " + ", ".join(sorted(INGRESS_ADAPTERS))
            )

        clauses: list[str] = []
        values: list[object] = []
        if query:
            like = f"%{like_literal(query.casefold())}%"
            clauses.append(
                """
                (
                    lower(id) LIKE ? ESCAPE '\\'
                    OR lower(target) LIKE ? ESCAPE '\\'
                    OR lower(target_config_json) LIKE ? ESCAPE '\\'
                    OR lower(cadence) LIKE ? ESCAPE '\\'
                    OR lower(adapters_json) LIKE ? ESCAPE '\\'
                )
                """
            )
            values.extend((like,) * 5)
        if enabled is not None:
            clauses.append("enabled = ?")
            values.append(int(enabled))
        if adapter is not None:
            clauses.append("EXISTS (SELECT 1 FROM json_each(adapters_json) WHERE value = ?)")
            values.append(adapter)
        if target is not None:
            clauses.append("target = ?")
            values.append(target)

        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        sort_sql = {
            "cadence": "cadence",
            "created_at": "created_at",
            "enabled": "enabled",
            "id": "id",
            "target": "target",
            "updated_at": "updated_at",
        }[sort]
        order_sql = order.upper()
        selected_sql = f"""
            SELECT
                id,
                enabled,
                adapters_json,
                target,
                cadence,
                target_config_json,
                created_at,
                updated_at
            FROM sources
            {where}
            ORDER BY {sort_sql} {order_sql}, id ASC
        """
        with self.connect() as connection:
            total_row = connection.execute(
                f"SELECT COUNT(*) AS total FROM sources {where}",
                values,
            ).fetchone()
            total = int(total_row["total"] if total_row is not None else 0)
            if all_items:
                rows = connection.execute(selected_sql, values).fetchall()
            else:
                rows = connection.execute(
                    f"{selected_sql} LIMIT ? OFFSET ?",
                    [*values, per_page, (page - 1) * per_page],
                ).fetchall()
        result_page = 1 if all_items else page
        result_per_page = total if all_items else per_page
        result_pages = (1 if total else 0) if all_items else (total + per_page - 1) // per_page
        return {
            "page": result_page,
            "per_page": result_per_page,
            "total": total,
            "pages": result_pages,
            "sort": sort,
            "order": order,
            "query": query,
            "filters": {
                "enabled": enabled,
                "adapter": adapter,
                "target": target,
            },
            "sources": [
                {
                    "id": str(row["id"]),
                    "enabled": bool(row["enabled"]),
                    "adapters": list(json.loads(row["adapters_json"])),
                    "target": str(row["target"]),
                    "cadence": str(row["cadence"]),
                    "target_config": dict(json.loads(row["target_config_json"])),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                }
                for row in rows
            ],
        }

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
            "target",
            "threshold_bytes",
            "cleanup",
            "cadence",
            "weekday",
            "hour",
            "minute",
            "target_config",
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
        target_config = _target_config(cast(Mapping[str, Any], values["target_config"]))
        now = format_utc_timestamp(utc_now())
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE sources
                SET adapters_json = ?, stable_seconds = ?, include_extensions_json = ?,
                    target = ?, target_config_json = ?, threshold_bytes = ?,
                    cleanup = ?, cadence = ?, weekday = ?, hour = ?, minute = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    _json(adapters),
                    stable_seconds,
                    _json(sorted(extensions)),
                    str(values["target"]).strip() or current.target,
                    _json(target_config),
                    threshold_bytes,
                    cleanup,
                    cadence,
                    weekday,
                    hour,
                    minute,
                    now,
                    current.id,
                ),
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
        for row in rows:
            self._ensure_ftp_home(str(row["id"]))
        lines = [self._ftp_record(str(row["id"]), str(row["password_hash"])) for row in rows]
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

    def _ensure_ftp_home(self, source_id: str) -> None:
        home = self.landing_dir / source_id
        home.mkdir(parents=True, exist_ok=True)
        try:
            os.chown(home, self.ftp_uid, self.ftp_gid)
        except PermissionError as exc:
            current = home.stat()
            if (current.st_uid, current.st_gid) != (self.ftp_uid, self.ftp_gid):
                raise SourceRegistryError(
                    f"Jeb cannot provision FTP landing home ownership: {source_id}"
                ) from exc
        home.chmod(0o770)

    def _ftp_record(self, source_id: str, password_hash: str) -> str:
        home = (self.landing_dir / source_id).as_posix().rstrip("/") + "/./"
        return f"{source_id}:{password_hash}:{self.ftp_uid}:{self.ftp_gid}::{home}::::::::::::\n"

    def _source(self, connection: sqlite3.Connection, row: sqlite3.Row) -> SourceConfig:
        return SourceConfig(
            id=str(row["id"]),
            enabled=bool(row["enabled"]),
            path=self.landing_dir / str(row["id"]),
            adapters=tuple(json.loads(row["adapters_json"])),
            stable_seconds=int(row["stable_seconds"]),
            include_extensions=frozenset(json.loads(row["include_extensions_json"])),
            target=str(row["target"]),
            target_config=dict(json.loads(row["target_config_json"])),
            threshold_bytes=int(row["threshold_bytes"]),
            cleanup=cast(Cleanup, str(row["cleanup"])),
            cadence=cast(Cadence, str(row["cadence"])),
            weekday=int(row["weekday"]),
            hour=int(row["hour"]),
            minute=int(row["minute"]),
        )


def _source_id(value: str) -> str:
    normalized = value.strip()
    if not SOURCE_ID.fullmatch(normalized):
        raise SourceRegistryError(f"source must be a safe id: {value!r}")
    return normalized


def _target_config(value: Mapping[str, Any]) -> dict[str, Any]:
    if any(not isinstance(key, str) or not key.strip() for key in value):
        raise SourceRegistryError("target_config keys must be non-blank strings")
    try:
        normalized = json.loads(json.dumps(dict(value), sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise SourceRegistryError("target_config must contain JSON values") from exc
    if not isinstance(normalized, dict):
        raise SourceRegistryError("target_config must be an object")
    return cast(dict[str, Any], normalized)


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
