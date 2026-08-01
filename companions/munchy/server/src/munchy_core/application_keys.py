from __future__ import annotations

import json
import math
import secrets
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from application_access import create_key_credentials, normalize_app_name, token_sha256

ALL_PERMISSIONS = "*"
SUBMISSIONS_MANAGE = "submissions:manage"
EVENTS_READ = "events:read"
EVENTS_READ_ALL = "events:read_all"
APPLICATION_PERMISSIONS = frozenset({SUBMISSIONS_MANAGE, EVENTS_READ, EVENTS_READ_ALL})

_APP_SORT_FIELDS = {"name", "keys", "active_keys", "last_used_at"}
_KEY_SORT_FIELDS = {"id", "created_at", "expires_at", "last_used_at"}


def _now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalize_permissions(values: Iterable[str]) -> tuple[str, ...]:
    permissions = tuple(sorted({str(value).strip().casefold() for value in values}))
    if not permissions or any(not permission for permission in permissions):
        raise ValueError("at least one application permission is required")
    unknown = set(permissions) - APPLICATION_PERMISSIONS - {ALL_PERMISSIONS}
    if unknown:
        raise ValueError(f"unknown application permission: {sorted(unknown)[0]}")
    if ALL_PERMISSIONS in permissions and len(permissions) != 1:
        raise ValueError("the wildcard application permission must be used alone")
    return permissions


@dataclass(frozen=True, slots=True)
class MunchyPrincipal:
    app: str
    key_id: str
    permissions: frozenset[str]

    def allows(self, permission: str) -> bool:
        return (
            ALL_PERMISSIONS in self.permissions
            or permission in self.permissions
            or (permission == EVENTS_READ and EVENTS_READ_ALL in self.permissions)
        )


def _validate_list(*, page: int, per_page: int, sort: str, order: str, fields: set[str]) -> None:
    if page < 1:
        raise ValueError("page must be greater than or equal to 1")
    if per_page < 1 or per_page > 100:
        raise ValueError("per_page must be between 1 and 100")
    if sort not in fields:
        raise ValueError(f"sort must be one of {', '.join(sorted(fields))}")
    if order not in {"asc", "desc"}:
        raise ValueError("order must be asc or desc")


def _page_metadata(*, page: int, per_page: int, total: int, all_items: bool) -> dict[str, int]:
    return {
        "page": 1 if all_items else page,
        "per_page": total if all_items else per_page,
        "total": total,
        "pages": (
            (1 if total else 0) if all_items else math.ceil(total / per_page) if total else 0
        ),
    }


class SQLiteApplicationKeyStore:
    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS application_keys (
                    id TEXT PRIMARY KEY,
                    app TEXT NOT NULL,
                    token_sha256 TEXT NOT NULL UNIQUE,
                    permissions_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    revoked_at TEXT,
                    last_used_at TEXT
                );
                CREATE INDEX IF NOT EXISTS application_keys_app_id
                ON application_keys(app, id);
                """
            )
            connection.commit()

    def authenticate(self, token: str) -> MunchyPrincipal | None:
        if not token:
            return None
        digest = token_sha256(token)
        now = _now_text()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM application_keys WHERE token_sha256 = ?",
                (digest,),
            ).fetchone()
            if row is None or not secrets.compare_digest(str(row["token_sha256"]), digest):
                return None
            if row["revoked_at"] is not None or (
                row["expires_at"] is not None and str(row["expires_at"]) <= now
            ):
                return None
            connection.execute(
                "UPDATE application_keys SET last_used_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            connection.commit()
        return MunchyPrincipal(
            app=str(row["app"]),
            key_id=str(row["id"]),
            permissions=frozenset(self._permissions(row)),
        )

    def create(
        self,
        *,
        app: str,
        permissions: Sequence[str],
        expires_in: timedelta | None = None,
    ) -> dict[str, object]:
        normalized_app = normalize_app_name(app)
        normalized_permissions = normalize_permissions(permissions)
        if expires_in is not None and expires_in.total_seconds() <= 0:
            raise ValueError("app key expiry must be positive")
        created = datetime.now(UTC)
        created_at = created.isoformat(timespec="microseconds").replace("+00:00", "Z")
        expires_at = (
            (created + expires_in).isoformat(timespec="microseconds").replace("+00:00", "Z")
            if expires_in is not None
            else None
        )
        with closing(self._connect()) as connection:
            while True:
                key_id, token, digest = create_key_credentials("mu_app_")
                exists = connection.execute(
                    "SELECT 1 FROM application_keys WHERE id = ? OR token_sha256 = ?",
                    (key_id, digest),
                ).fetchone()
                if exists is None:
                    break
            connection.execute(
                """
                INSERT INTO application_keys(
                    id, app, token_sha256, permissions_json, created_at,
                    expires_at, revoked_at, last_used_at
                ) VALUES(?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    key_id,
                    normalized_app,
                    digest,
                    json.dumps(normalized_permissions, separators=(",", ":")),
                    created_at,
                    expires_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM application_keys WHERE id = ?", (key_id,)
            ).fetchone()
            connection.commit()
        if row is None:  # pragma: no cover - the insert above guarantees a row
            raise RuntimeError("application key was not created")
        return {**self._payload(row, now=created_at), "token": token}

    def revoke(self, *, app: str, key_id: str) -> dict[str, object]:
        normalized_app = normalize_app_name(app)
        normalized_key_id = key_id.strip().casefold()
        now = _now_text()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM application_keys WHERE app = ? AND id = ?",
                (normalized_app, normalized_key_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"app key not found: {normalized_key_id}")
            if row["revoked_at"] is None:
                connection.execute(
                    "UPDATE application_keys SET revoked_at = ? WHERE id = ?",
                    (now, normalized_key_id),
                )
                row = connection.execute(
                    "SELECT * FROM application_keys WHERE id = ?", (normalized_key_id,)
                ).fetchone()
            connection.commit()
        if row is None:  # pragma: no cover - the initial lookup guarantees a row
            raise RuntimeError("application key disappeared during revocation")
        return self._payload(row, now=now)

    def list_apps(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        sort: str,
        order: str,
        active: bool | None = None,
        all_items: bool = False,
    ) -> dict[str, object]:
        _validate_list(
            page=page, per_page=per_page, sort=sort, order=order, fields=_APP_SORT_FIELDS
        )
        now = _now_text()
        clauses: list[str] = []
        params: list[object] = [now]
        query = q.strip().casefold() if q else None
        if query:
            clauses.append("lower(app) LIKE ? ESCAPE '\\'")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
        active_filter = ""
        if active is not None:
            active_filter = "HAVING active_keys > 0" if active else "HAVING active_keys = 0"
        direction = order.upper()
        limit_sql = "" if all_items else "LIMIT ? OFFSET ?"
        row_params = [*params]
        if not all_items:
            row_params.extend([per_page, (page - 1) * per_page])
        summary_sql = f"""
            SELECT app AS name,
                   COUNT(*) AS keys,
                   SUM(CASE WHEN revoked_at IS NULL
                                 AND (expires_at IS NULL OR expires_at > ?)
                            THEN 1 ELSE 0 END) AS active_keys,
                   MAX(last_used_at) AS last_used_at
            FROM application_keys
            {where_sql}
            GROUP BY app
            {active_filter}
        """
        with closing(self._connect()) as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) AS total FROM ({summary_sql})", params
                ).fetchone()["total"]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM ({summary_sql})
                ORDER BY {sort} {direction}, name ASC
                {limit_sql}
                """,
                row_params,
            ).fetchall()
        return {
            **_page_metadata(page=page, per_page=per_page, total=total, all_items=all_items),
            "sort": sort,
            "order": order,
            "query": query,
            "active": active,
            "apps": [dict(row) for row in rows],
        }

    def list_keys(
        self,
        *,
        app: str,
        page: int,
        per_page: int,
        q: str | None,
        sort: str,
        order: str,
        active: bool | None = None,
        all_items: bool = False,
    ) -> dict[str, object]:
        _validate_list(
            page=page, per_page=per_page, sort=sort, order=order, fields=_KEY_SORT_FIELDS
        )
        normalized_app = normalize_app_name(app)
        now = _now_text()
        clauses = ["app = ?"]
        params: list[object] = [normalized_app]
        query = q.strip().casefold() if q else None
        if query:
            clauses.append("lower(id) LIKE ? ESCAPE '\\'")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        if active is not None:
            active_sql = "revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?)"
            clauses.append(active_sql if active else f"NOT ({active_sql})")
            params.append(now)
        where_sql = " AND ".join(clauses)
        direction = order.upper()
        limit_sql = "" if all_items else "LIMIT ? OFFSET ?"
        row_params = [*params]
        if not all_items:
            row_params.extend([per_page, (page - 1) * per_page])
        with closing(self._connect()) as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) AS total FROM application_keys WHERE {where_sql}",
                    params,
                ).fetchone()["total"]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM application_keys
                WHERE {where_sql}
                ORDER BY {sort} {direction}, id ASC
                {limit_sql}
                """,
                row_params,
            ).fetchall()
        return {
            **_page_metadata(page=page, per_page=per_page, total=total, all_items=all_items),
            "sort": sort,
            "order": order,
            "query": query,
            "active": active,
            "app": normalized_app,
            "keys": [self._payload(row, now=now) for row in rows],
        }

    @staticmethod
    def _permissions(row: sqlite3.Row) -> tuple[str, ...]:
        try:
            raw = json.loads(str(row["permissions_json"]))
            if not isinstance(raw, list):
                raise ValueError
            return normalize_permissions(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError(f"application key {row['id']} has invalid permissions") from exc

    @classmethod
    def _payload(cls, row: sqlite3.Row, *, now: str) -> dict[str, object]:
        status = (
            "revoked"
            if row["revoked_at"] is not None
            else "expired"
            if row["expires_at"] is not None and str(row["expires_at"]) <= now
            else "active"
        )
        return {
            "id": str(row["id"]),
            "app": str(row["app"]),
            "permissions": list(cls._permissions(row)),
            "status": status,
            "created_at": str(row["created_at"]),
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
            "last_used_at": row["last_used_at"],
        }


__all__ = [
    "APPLICATION_PERMISSIONS",
    "EVENTS_READ",
    "EVENTS_READ_ALL",
    "MunchyPrincipal",
    "SQLiteApplicationKeyStore",
    "SUBMISSIONS_MANAGE",
]
