from __future__ import annotations

import json
import math
import secrets
from collections.abc import Sequence
from datetime import timedelta

from application_access import (
    create_key_credentials,
    token_sha256,
)
from application_access import (
    normalize_app_name as normalize_application_name,
)
from riverhog_protocol.errors import BadRequest, Forbidden, NotFound
from sqlalchemy import and_, asc, case, delete, desc, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.app_permissions import (
    QUOTAS_MANAGE,
    ApplicationPrincipal,
    normalize_permissions,
)
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    AppKeyCollectionGrantRecord,
    AppKeyRecord,
    KeyDownloadReservationRecord,
    RetrievalCacheLeaseRecord,
    RetrievalJobRecord,
)
from riverhog_core.collection_grants import normalize_collection_grants
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService

_APP_SORT_FIELDS = {"name", "keys", "active_keys", "last_used_at"}
_KEY_SORT_FIELDS = {"id", "created_at", "expires_at", "last_used_at"}
_GRANT_SORT_FIELDS = {"grant", "created_at"}


def normalize_app_name(value: str) -> str:
    try:
        return normalize_application_name(value)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _active_expression(now: str) -> ColumnElement[bool]:
    return and_(
        AppKeyRecord.revoked_at.is_(None),
        or_(AppKeyRecord.expires_at.is_(None), AppKeyRecord.expires_at > now),
    )


def _status(record: AppKeyRecord, *, now: str) -> str:
    if record.revoked_at is not None:
        return "revoked"
    if record.expires_at is not None and record.expires_at <= now:
        return "expired"
    return "active"


def _validate_list(*, page: int, per_page: int, sort: str, order: str, fields: set[str]) -> None:
    if page < 1:
        raise BadRequest("page must be greater than or equal to 1")
    if per_page < 1 or per_page > 100:
        raise BadRequest("per_page must be between 1 and 100")
    if sort not in fields:
        raise BadRequest(f"sort must be one of {', '.join(sorted(fields))}")
    if order not in {"asc", "desc"}:
        raise BadRequest("order must be asc or desc")


def _page_metadata(
    *,
    page: int,
    per_page: int,
    total: int,
    all_items: bool,
) -> dict[str, int]:
    return {
        "page": 1 if all_items else page,
        "per_page": total if all_items else per_page,
        "total": total,
        "pages": (
            (1 if total else 0) if all_items else math.ceil(total / per_page) if total else 0
        ),
    }


class SqlAlchemyAppKeyService:
    def __init__(self, config: RuntimeConfig) -> None:
        self._session_factory = make_session_factory(config.database_url)
        self._lifecycle_events = SqlAlchemyLifecycleEventService(config)

    def authenticate(self, token: str) -> ApplicationPrincipal | None:
        if not token:
            return None
        digest = token_sha256(token)
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            record = session.scalar(select(AppKeyRecord).where(AppKeyRecord.token_sha256 == digest))
            if record is None or not secrets.compare_digest(record.token_sha256, digest):
                return None
            if _status(record, now=now) != "active":
                return None
            record.last_used_at = now
            return ApplicationPrincipal(
                app=record.app,
                key_id=record.id,
                permissions=frozenset(_record_permissions(record)),
                collection_grants=frozenset(_record_grants(session, record.id)),
            )

    def create(
        self,
        *,
        app: str,
        permissions: Sequence[str],
        grantor: ApplicationPrincipal,
        collection_grants: Sequence[str] = (),
        expires_in: timedelta | None = None,
    ) -> dict[str, object]:
        normalized_app = normalize_app_name(app)
        normalized_permissions = normalize_permissions(permissions)
        normalized_grants = normalize_collection_grants(collection_grants)
        if not grantor.can_grant(normalized_permissions):
            raise Forbidden("an application key cannot grant permissions it does not hold")
        if not grantor.can_grant_collections(normalized_grants):
            raise Forbidden("an application key cannot grant collection access it does not hold")
        if expires_in is not None and expires_in.total_seconds() <= 0:
            raise BadRequest("app key expiry must be positive")
        created = utc_now()
        created_at = format_utc_timestamp(created)
        expires_at = format_utc_timestamp(created + expires_in) if expires_in is not None else None
        with session_scope(self._session_factory) as session:
            while True:
                key_id, token, digest = create_key_credentials("rh_app_")
                if session.get(AppKeyRecord, key_id) is None:
                    break
            record = AppKeyRecord(
                id=key_id,
                app=normalized_app,
                token_sha256=digest,
                permissions_json=json.dumps(normalized_permissions, separators=(",", ":")),
                monthly_download_quota_bytes=0,
                created_at=created_at,
                expires_at=expires_at,
                revoked_at=None,
                last_used_at=None,
            )
            session.add(record)
            session.flush()
            session.add_all(
                AppKeyCollectionGrantRecord(
                    key_id=key_id,
                    grant=grant,
                    created_at=created_at,
                )
                for grant in normalized_grants
            )
        return {
            **self._record_payload(record, now=created_at, grants=normalized_grants),
            "token": token,
        }

    def rotate(
        self,
        *,
        app: str,
        key_id: str,
        grantor: ApplicationPrincipal,
    ) -> dict[str, object]:
        normalized_app = normalize_app_name(app)
        normalized_key_id = key_id.strip().casefold()
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            record = _require_key(
                session,
                app=normalized_app,
                key_id=normalized_key_id,
            )
            if _status(record, now=now) != "active":
                raise BadRequest("only an active application key can be rotated")
            grants = _record_grants(session, record.id)
            if not grantor.can_grant(_record_permissions(record)) or not (
                grantor.can_grant_collections(grants)
            ):
                raise Forbidden("an application key cannot rotate authority it does not hold")
            if (
                record.monthly_download_quota_bytes != 0
                and not grantor.allows(QUOTAS_MANAGE)
            ):
                raise Forbidden(
                    "quotas:manage is required to rotate a key with download allowance"
                )
            _new_id, token, digest = create_key_credentials("rh_app_")
            record.token_sha256 = digest
            return {
                **self._record_payload(
                    record,
                    now=now,
                    grants=grants,
                ),
                "token": token,
            }

    def revoke(self, *, app: str, key_id: str) -> dict[str, object]:
        normalized_app = normalize_app_name(app)
        normalized_key_id = key_id.strip().casefold()
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            record = _require_key(
                session,
                app=normalized_app,
                key_id=normalized_key_id,
            )
            if record.revoked_at is None:
                record.revoked_at = now
                jobs = list(
                    session.scalars(
                        select(RetrievalJobRecord).where(
                            RetrievalJobRecord.initiated_by_key_id == record.id,
                            RetrievalJobRecord.state.in_(("requested", "ready")),
                        )
                    )
                )
                for job in jobs:
                    job.state = "canceled"
                    job.canceled_at = now
                    job.next_poll_at = None
                    session.execute(
                        delete(RetrievalCacheLeaseRecord).where(
                            RetrievalCacheLeaseRecord.owner == f"job:{job.id}"
                        )
                    )
                    session.execute(
                        delete(KeyDownloadReservationRecord).where(
                            KeyDownloadReservationRecord.job_id == job.id,
                            KeyDownloadReservationRecord.kind == "job",
                        )
                    )
                    self._lifecycle_events.emit_retrieval(
                        type="retrieval.canceled",
                        job=job,
                        details={"reason": "initiating key revoked"},
                        terminal=True,
                        session=session,
                    )
            return self._record_payload(
                record,
                now=now,
                grants=_record_grants(session, record.id),
            )

    def replace_collection_grants(
        self,
        *,
        app: str,
        key_id: str,
        collection_grants: Sequence[str],
        grantor: ApplicationPrincipal,
    ) -> dict[str, object]:
        normalized_app = normalize_app_name(app)
        normalized_key_id = key_id.strip().casefold()
        normalized_grants = normalize_collection_grants(collection_grants)
        if not grantor.can_grant_collections(normalized_grants):
            raise Forbidden("an application key cannot grant collection access it does not hold")
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            _require_key(session, app=normalized_app, key_id=normalized_key_id)
            session.execute(
                delete(AppKeyCollectionGrantRecord).where(
                    AppKeyCollectionGrantRecord.key_id == normalized_key_id
                )
            )
            session.add_all(
                AppKeyCollectionGrantRecord(
                    key_id=normalized_key_id,
                    grant=grant,
                    created_at=now,
                )
                for grant in normalized_grants
            )
        return {
            "app": normalized_app,
            "key_id": normalized_key_id,
            "collection_grants": list(normalized_grants),
        }

    def list_collection_grants(
        self,
        *,
        app: str,
        key_id: str,
        page: int,
        per_page: int,
        q: str | None,
        sort: str,
        order: str,
        all_items: bool = False,
    ) -> dict[str, object]:
        _validate_list(
            page=page,
            per_page=per_page,
            sort=sort,
            order=order,
            fields=_GRANT_SORT_FIELDS,
        )
        normalized_app = normalize_app_name(app)
        normalized_key_id = key_id.strip().casefold()
        query = q.strip() if q is not None else None
        filters: list[ColumnElement[bool]] = [
            AppKeyCollectionGrantRecord.key_id == normalized_key_id
        ]
        if query:
            filters.append(
                func.lower(AppKeyCollectionGrantRecord.grant).like(
                    _like_pattern(query.casefold()),
                    escape="\\",
                )
            )
        direction = desc if order == "desc" else asc
        statement = (
            select(AppKeyCollectionGrantRecord)
            .where(*filters)
            .order_by(
                direction(getattr(AppKeyCollectionGrantRecord, sort)),
                asc(AppKeyCollectionGrantRecord.grant),
            )
        )
        with session_scope(self._session_factory) as session:
            _require_key(session, app=normalized_app, key_id=normalized_key_id)
            total = int(
                session.scalar(
                    select(func.count()).select_from(AppKeyCollectionGrantRecord).where(*filters)
                )
                or 0
            )
            if not all_items:
                statement = statement.offset((page - 1) * per_page).limit(per_page)
            records = session.scalars(statement).all()
        return {
            **_page_metadata(
                page=page,
                per_page=per_page,
                total=total,
                all_items=all_items,
            ),
            "sort": sort,
            "order": order,
            "query": query,
            "app": normalized_app,
            "key_id": normalized_key_id,
            "grants": [{"id": record.grant, "created_at": record.created_at} for record in records],
        }

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
            page=page,
            per_page=per_page,
            sort=sort,
            order=order,
            fields=_APP_SORT_FIELDS,
        )
        now = format_utc_timestamp(utc_now())
        active_count = func.sum(case((_active_expression(now), 1), else_=0))
        query = q.strip() if q is not None else None
        summary = select(
            AppKeyRecord.app.label("name"),
            func.count().label("keys"),
            active_count.label("active_keys"),
            func.max(AppKeyRecord.last_used_at).label("last_used_at"),
        )
        if query:
            summary = summary.where(
                func.lower(AppKeyRecord.app).like(
                    _like_pattern(query.casefold()),
                    escape="\\",
                )
            )
        summary = summary.group_by(AppKeyRecord.app)
        if active is not None:
            summary = summary.having(active_count > 0 if active else active_count == 0)
        grouped = summary.subquery()
        direction = desc if order == "desc" else asc
        sort_column = grouped.c[sort]
        statement = select(grouped).order_by(direction(sort_column), asc(grouped.c.name))
        with session_scope(self._session_factory) as session:
            total = int(session.scalar(select(func.count()).select_from(grouped)) or 0)
            if not all_items:
                statement = statement.offset((page - 1) * per_page).limit(per_page)
            rows = session.execute(statement).mappings().all()
        return {
            **_page_metadata(
                page=page,
                per_page=per_page,
                total=total,
                all_items=all_items,
            ),
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
            page=page,
            per_page=per_page,
            sort=sort,
            order=order,
            fields=_KEY_SORT_FIELDS,
        )
        normalized_app = normalize_app_name(app)
        now = format_utc_timestamp(utc_now())
        query = q.strip() if q is not None else None
        filters: list[ColumnElement[bool]] = [AppKeyRecord.app == normalized_app]
        if query:
            filters.append(
                func.lower(AppKeyRecord.id).like(
                    _like_pattern(query.casefold()),
                    escape="\\",
                )
            )
        if active is not None:
            filters.append(_active_expression(now) if active else ~_active_expression(now))
        direction = desc if order == "desc" else asc
        sort_column = getattr(AppKeyRecord, sort)
        statement = (
            select(AppKeyRecord)
            .where(*filters)
            .order_by(
                direction(sort_column),
                asc(AppKeyRecord.id),
            )
        )
        with session_scope(self._session_factory) as session:
            total = int(
                session.scalar(select(func.count()).select_from(AppKeyRecord).where(*filters)) or 0
            )
            if not all_items:
                statement = statement.offset((page - 1) * per_page).limit(per_page)
            records = session.scalars(statement).all()
            grants_by_key = _record_grants_for_records(records, session=session)
        return {
            **_page_metadata(
                page=page,
                per_page=per_page,
                total=total,
                all_items=all_items,
            ),
            "sort": sort,
            "order": order,
            "query": query,
            "active": active,
            "app": normalized_app,
            "keys": [
                self._record_payload(
                    record,
                    now=now,
                    grants=grants_by_key.get(record.id, ()),
                )
                for record in records
            ],
        }

    @staticmethod
    def _record_payload(
        record: AppKeyRecord,
        *,
        now: str,
        grants: Sequence[str],
    ) -> dict[str, object]:
        return {
            "id": record.id,
            "app": record.app,
            "permissions": list(_record_permissions(record)),
            "collection_grants": list(grants),
            "monthly_download_quota_bytes": record.monthly_download_quota_bytes,
            "status": _status(record, now=now),
            "created_at": record.created_at,
            "expires_at": record.expires_at,
            "revoked_at": record.revoked_at,
            "last_used_at": record.last_used_at,
        }


def _record_permissions(record: AppKeyRecord) -> tuple[str, ...]:
    try:
        payload = json.loads(record.permissions_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"application key {record.id} has invalid permissions") from exc
    if not isinstance(payload, list) or any(not isinstance(value, str) for value in payload):
        raise RuntimeError(f"application key {record.id} has invalid permissions")
    try:
        return normalize_permissions(payload)
    except BadRequest as exc:
        raise RuntimeError(f"application key {record.id} has invalid permissions") from exc


def _record_grants(session: Session, key_id: str) -> tuple[str, ...]:
    return tuple(
        session.scalars(
            select(AppKeyCollectionGrantRecord.grant)
            .where(AppKeyCollectionGrantRecord.key_id == key_id)
            .order_by(AppKeyCollectionGrantRecord.grant)
        )
    )


def _record_grants_for_records(
    records: Sequence[AppKeyRecord],
    *,
    session: Session,
) -> dict[str, tuple[str, ...]]:
    if not records:
        return {}
    grouped: dict[str, list[str]] = {record.id: [] for record in records}
    rows = session.execute(
        select(AppKeyCollectionGrantRecord.key_id, AppKeyCollectionGrantRecord.grant)
        .where(AppKeyCollectionGrantRecord.key_id.in_(grouped))
        .order_by(AppKeyCollectionGrantRecord.key_id, AppKeyCollectionGrantRecord.grant)
    ).all()
    for key_id, grant in rows:
        grouped[str(key_id)].append(str(grant))
    return {key_id: tuple(grants) for key_id, grants in grouped.items()}


def _require_key(session: Session, *, app: str, key_id: str) -> AppKeyRecord:
    record = session.scalar(
        select(AppKeyRecord).where(
            AppKeyRecord.id == key_id,
            AppKeyRecord.app == app,
        )
    )
    if record is None:
        raise NotFound(f"app key not found: {key_id}")
    return record


__all__ = ["SqlAlchemyAppKeyService", "normalize_app_name"]
