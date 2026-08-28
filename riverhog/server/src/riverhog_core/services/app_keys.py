from __future__ import annotations

import hashlib
import math
import secrets
from collections.abc import Sequence
from datetime import timedelta

from riverhog_application_access import (
    validate_application_key_id,
    validate_application_name,
)
from riverhog_protocol.errors import BadRequest, Conflict, Forbidden, NotFound
from sqlalchemy import and_, asc, case, delete, desc, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.app_permissions import (
    CATALOG_READ,
    QUOTAS_MANAGE,
    ApplicationAccess,
    ApplicationPrincipal,
    normalize_access,
)
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    AppKeyAccessGrantRecord,
    AppKeyRecord,
    CollectionRecord,
    KeyDownloadReservationRecord,
    RetrievalCacheLeaseRecord,
    RetrievalJobRecord,
    TagRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService

_APP_SORT_FIELDS = {"name", "keys", "active_keys", "last_used_at"}
_KEY_SORT_FIELDS = {"id", "created_at", "expires_at", "last_used_at"}
_ACCESS_SORT_FIELDS = {"app", "key_id", "permission", "resource", "created_at"}


def _token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _create_key_credentials(prefix: str) -> tuple[str, str, str]:
    key_id = secrets.token_hex(8)
    token = f"{prefix}{secrets.token_urlsafe(32)}"
    return key_id, token, _token_sha256(token)


def normalize_app_name(value: str) -> str:
    try:
        return validate_application_name(value)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc


def normalize_key_id(value: str) -> str:
    try:
        return validate_application_key_id(value)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def active_key_filter(now: str) -> ColumnElement[bool]:
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
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._session_factory = session_factory or make_session_factory(config.database_url)
        self._lifecycle_events = SqlAlchemyLifecycleEventService(
            config,
            session_factory=self._session_factory,
        )

    def authenticate(self, token: str) -> ApplicationPrincipal | None:
        if not token:
            return None
        digest = _token_sha256(token)
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
                access=frozenset(_record_access(session, record.id)),
            )

    def create(
        self,
        *,
        app: str,
        access: Sequence[ApplicationAccess | tuple[str, str]],
        grantor: ApplicationPrincipal,
        expires_in: timedelta | None = None,
    ) -> dict[str, object]:
        normalized_app = normalize_app_name(app)
        normalized_access = normalize_access(access)
        if not grantor.can_grant(normalized_access):
            raise Forbidden("an application key cannot grant access it does not hold")
        if expires_in is not None and expires_in.total_seconds() <= 0:
            raise BadRequest("app key expiry must be positive")
        created = utc_now()
        created_at = format_utc_timestamp(created)
        expires_at = format_utc_timestamp(created + expires_in) if expires_in is not None else None
        with session_scope(self._session_factory) as session:
            _require_access_targets(session, normalized_access)
            while True:
                key_id, token, digest = _create_key_credentials("rh_app_")
                if session.get(AppKeyRecord, key_id) is None:
                    break
            record = AppKeyRecord(
                id=key_id,
                app=normalized_app,
                token_sha256=digest,
                monthly_download_quota_bytes=0,
                created_at=created_at,
                expires_at=expires_at,
                revoked_at=None,
                last_used_at=None,
            )
            session.add(record)
            session.flush()
            session.add_all(
                AppKeyAccessGrantRecord(
                    key_id=key_id,
                    permission=current.permission,
                    resource=current.resource,
                    created_at=created_at,
                )
                for current in normalized_access
            )
        return {
            **self._record_payload(record, now=created_at, access=normalized_access),
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
        normalized_key_id = normalize_key_id(key_id)
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            record = _require_key(
                session,
                app=normalized_app,
                key_id=normalized_key_id,
            )
            if _status(record, now=now) != "active":
                raise BadRequest("only an active application key can be rotated")
            access = _record_access(session, record.id)
            if not grantor.can_grant(access):
                raise Forbidden("an application key cannot rotate authority it does not hold")
            if record.monthly_download_quota_bytes != 0 and not grantor.allows(QUOTAS_MANAGE):
                raise Forbidden("quotas:manage is required to rotate a key with download allowance")
            _new_id, token, digest = _create_key_credentials("rh_app_")
            record.token_sha256 = digest
            return {
                **self._record_payload(
                    record,
                    now=now,
                    access=access,
                ),
                "token": token,
            }

    def revoke(self, *, app: str, key_id: str) -> dict[str, object]:
        normalized_app = normalize_app_name(app)
        normalized_key_id = normalize_key_id(key_id)
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
                access=_record_access(session, record.id),
            )

    def replace_access(
        self,
        *,
        app: str,
        key_id: str,
        access: Sequence[ApplicationAccess | tuple[str, str]],
        grantor: ApplicationPrincipal,
    ) -> dict[str, object]:
        normalized_app = normalize_app_name(app)
        normalized_key_id = normalize_key_id(key_id)
        normalized_access = normalize_access(access)
        if not grantor.can_grant(normalized_access):
            raise Forbidden("an application key cannot grant access it does not hold")
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            _require_key(session, app=normalized_app, key_id=normalized_key_id)
            _require_access_targets(session, normalized_access)
            session.execute(
                delete(AppKeyAccessGrantRecord).where(
                    AppKeyAccessGrantRecord.key_id == normalized_key_id
                )
            )
            session.add_all(
                AppKeyAccessGrantRecord(
                    key_id=normalized_key_id,
                    permission=current.permission,
                    resource=current.resource,
                    created_at=now,
                )
                for current in normalized_access
            )
        return {
            "app": normalized_app,
            "key_id": normalized_key_id,
            "access": [_access_payload(current) for current in normalized_access],
        }

    def add_access(
        self,
        *,
        app: str,
        key_id: str,
        access: ApplicationAccess | tuple[str, str],
        grantor: ApplicationPrincipal,
    ) -> dict[str, object]:
        normalized_app = normalize_app_name(app)
        normalized_key_id = normalize_key_id(key_id)
        added = normalize_access((access,))[0]
        if not grantor.can_grant((added,)):
            raise Forbidden("an application key cannot grant access it does not hold")
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            _require_key(session, app=normalized_app, key_id=normalized_key_id)
            current = tuple(_record_access(session, normalized_key_id))
            if added in current:
                raise Conflict(
                    f"application access already exists: {added.permission}={added.resource}"
                )
            updated = normalize_access((*current, added))
            _require_access_targets(session, (added,))
            session.add(
                AppKeyAccessGrantRecord(
                    key_id=normalized_key_id,
                    permission=added.permission,
                    resource=added.resource,
                    created_at=now,
                )
            )
        return _access_set_payload(normalized_app, normalized_key_id, updated)

    def remove_access(
        self,
        *,
        app: str,
        key_id: str,
        access: ApplicationAccess | tuple[str, str],
    ) -> dict[str, object]:
        normalized_app = normalize_app_name(app)
        normalized_key_id = normalize_key_id(key_id)
        removed = normalize_access((access,))[0]
        with session_scope(self._session_factory) as session:
            _require_key(session, app=normalized_app, key_id=normalized_key_id)
            current = tuple(_record_access(session, normalized_key_id))
            if removed not in current:
                raise NotFound(
                    f"application access not found: {removed.permission}={removed.resource}"
                )
            remaining = tuple(item for item in current if item != removed)
            if not remaining:
                raise BadRequest(
                    "cannot remove the final application access binding; revoke the key instead"
                )
            session.execute(
                delete(AppKeyAccessGrantRecord).where(
                    AppKeyAccessGrantRecord.key_id == normalized_key_id,
                    AppKeyAccessGrantRecord.permission == removed.permission,
                    AppKeyAccessGrantRecord.resource == removed.resource,
                )
            )
        return _access_set_payload(normalized_app, normalized_key_id, remaining)

    def list_access(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        sort: str,
        order: str,
        app: str | None = None,
        key_id: str | None = None,
        permission: str | None = None,
        resource: str | None = None,
        active: bool | None = None,
        all_items: bool = False,
    ) -> dict[str, object]:
        _validate_list(
            page=page,
            per_page=per_page,
            sort=sort,
            order=order,
            fields=_ACCESS_SORT_FIELDS,
        )
        normalized_app = normalize_app_name(app) if app is not None else None
        normalized_key_id = normalize_key_id(key_id) if key_id is not None else None
        normalized_permission = None
        if permission is not None:
            normalized_permission = normalize_access(((permission, "*"),))[0].permission
        normalized_resource = None
        if resource is not None:
            normalized_resource = normalize_access((ApplicationAccess(CATALOG_READ, resource),))[
                0
            ].resource
        query = q.strip() if q is not None else None
        now = format_utc_timestamp(utc_now())
        filters: list[ColumnElement[bool]] = []
        if normalized_app is not None:
            filters.append(AppKeyRecord.app == normalized_app)
        if normalized_key_id is not None:
            filters.append(AppKeyRecord.id == normalized_key_id)
        if normalized_permission is not None:
            filters.append(AppKeyAccessGrantRecord.permission == normalized_permission)
        if normalized_resource is not None:
            filters.append(AppKeyAccessGrantRecord.resource == normalized_resource)
        if active is not None:
            filters.append(active_key_filter(now) if active else ~active_key_filter(now))
        if query:
            filters.append(
                or_(
                    func.lower(AppKeyRecord.app).like(_like_pattern(query.casefold()), escape="\\"),
                    func.lower(AppKeyRecord.id).like(_like_pattern(query.casefold()), escape="\\"),
                    func.lower(AppKeyAccessGrantRecord.permission).like(
                        _like_pattern(query.casefold()), escape="\\"
                    ),
                    func.lower(AppKeyAccessGrantRecord.resource).like(
                        _like_pattern(query.casefold()), escape="\\"
                    ),
                )
            )
        direction = desc if order == "desc" else asc
        sort_column = {
            "app": AppKeyRecord.app,
            "key_id": AppKeyRecord.id,
            "permission": AppKeyAccessGrantRecord.permission,
            "resource": AppKeyAccessGrantRecord.resource,
            "created_at": AppKeyAccessGrantRecord.created_at,
        }[sort]
        statement = (
            select(AppKeyRecord, AppKeyAccessGrantRecord)
            .join(
                AppKeyAccessGrantRecord,
                AppKeyAccessGrantRecord.key_id == AppKeyRecord.id,
            )
            .where(*filters)
            .order_by(
                direction(sort_column),
                asc(AppKeyRecord.app),
                asc(AppKeyRecord.id),
                asc(AppKeyAccessGrantRecord.permission),
                asc(AppKeyAccessGrantRecord.resource),
            )
        )
        with session_scope(self._session_factory) as session:
            total = int(
                session.scalar(
                    select(func.count())
                    .select_from(AppKeyRecord)
                    .join(
                        AppKeyAccessGrantRecord,
                        AppKeyAccessGrantRecord.key_id == AppKeyRecord.id,
                    )
                    .where(*filters)
                )
                or 0
            )
            if not all_items:
                statement = statement.offset((page - 1) * per_page).limit(per_page)
            records = session.execute(statement).all()
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
            "filters": {
                "app": normalized_app,
                "key_id": normalized_key_id,
                "permission": normalized_permission,
                "resource": normalized_resource,
                "active": active,
            },
            "access": [
                {
                    "app": key.app,
                    "key_id": key.id,
                    "key_status": _status(key, now=now),
                    "permission": grant.permission,
                    "resource": grant.resource,
                    "created_at": grant.created_at,
                }
                for key, grant in records
            ],
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
        active_count = func.sum(case((active_key_filter(now), 1), else_=0))
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
            filters.append(active_key_filter(now) if active else ~active_key_filter(now))
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
            access_by_key = _record_access_for_records(records, session=session)
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
                    access=access_by_key.get(record.id, ()),
                )
                for record in records
            ],
        }

    @staticmethod
    def _record_payload(
        record: AppKeyRecord,
        *,
        now: str,
        access: Sequence[ApplicationAccess],
    ) -> dict[str, object]:
        return {
            "id": record.id,
            "app": record.app,
            "access": [_access_payload(current) for current in access],
            "monthly_download_quota_bytes": record.monthly_download_quota_bytes,
            "status": _status(record, now=now),
            "created_at": record.created_at,
            "expires_at": record.expires_at,
            "revoked_at": record.revoked_at,
            "last_used_at": record.last_used_at,
        }


def _record_access(session: Session, key_id: str) -> tuple[ApplicationAccess, ...]:
    return normalize_access(
        tuple(
            (record.permission, record.resource)
            for record in session.scalars(
                select(AppKeyAccessGrantRecord)
                .where(AppKeyAccessGrantRecord.key_id == key_id)
                .order_by(AppKeyAccessGrantRecord.permission, AppKeyAccessGrantRecord.resource)
            )
        )
    )


def _record_access_for_records(
    records: Sequence[AppKeyRecord],
    *,
    session: Session,
) -> dict[str, tuple[ApplicationAccess, ...]]:
    if not records:
        return {}
    grouped: dict[str, list[ApplicationAccess]] = {record.id: [] for record in records}
    rows = session.execute(
        select(
            AppKeyAccessGrantRecord.key_id,
            AppKeyAccessGrantRecord.permission,
            AppKeyAccessGrantRecord.resource,
        )
        .where(AppKeyAccessGrantRecord.key_id.in_(grouped))
        .order_by(
            AppKeyAccessGrantRecord.key_id,
            AppKeyAccessGrantRecord.permission,
            AppKeyAccessGrantRecord.resource,
        )
    ).all()
    for key_id, permission, resource in rows:
        access = normalize_access(((str(permission), str(resource)),))[0]
        grouped[str(key_id)].append(access)
    return {key_id: tuple(access) for key_id, access in grouped.items()}


def _access_payload(access: ApplicationAccess) -> dict[str, str]:
    return {"permission": access.permission, "resource": access.resource}


def _access_set_payload(
    app: str,
    key_id: str,
    access: Sequence[ApplicationAccess],
) -> dict[str, object]:
    return {
        "app": app,
        "key_id": key_id,
        "access": [_access_payload(current) for current in access],
    }


def _require_access_targets(session: Session, access: Sequence[ApplicationAccess]) -> None:
    for current in access:
        if current.resource == "*":
            continue
        if current.resource.startswith("tag:"):
            tag = current.resource.removeprefix("tag:")
            record = session.scalar(select(TagRecord).where(TagRecord.id == tag).with_for_update())
            if record is None:
                raise NotFound(f"tag not found: {tag}")
            continue
        collection_id = int(current.resource.removeprefix("collection:"))
        if session.get(CollectionRecord, collection_id) is None:
            raise NotFound(f"collection not found: {collection_id}")


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
