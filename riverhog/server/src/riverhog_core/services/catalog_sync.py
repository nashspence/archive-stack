from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Literal, cast

from http_api_contracts import canonical_json_bytes
from riverhog_protocol import (
    CATALOG_SYNC_CURSOR_BYTES_MAX,
    CATALOG_SYNC_PAGE_SIZE_MAX,
    MAX_CATALOG_SYNC_REVISION,
    CatalogSyncChange,
    CatalogSyncChangePage,
    CatalogSyncCheckpoint,
    CatalogSyncCollectionPage,
    CatalogSyncDelete,
    CatalogSyncDescriptor,
    CatalogSyncUpsert,
)
from riverhog_protocol.errors import (
    BadRequest,
    CatalogSyncCursorExpired,
    CatalogSyncHistoryExpired,
    CatalogSyncSourceChanged,
    CatalogSyncViewChanged,
    Forbidden,
)
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement
from state_schema import read_snapshot
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.app_permissions import CATALOG_READ, ApplicationPrincipal
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_events import catalog_event_projection
from riverhog_core.catalog_models import (
    CatalogEventRecord,
    CatalogSyncStateRecord,
    CollectionRecord,
)
from riverhog_core.collection_access import collection_access_filter
from riverhog_core.runtime_config import RuntimeConfig

_CURSOR_DOMAIN = b"riverhog-catalog-sync-cursor/v1\x00"
_CursorMode = Literal["catalog", "changes"]


class _CatalogSyncCursorCodec:
    def __init__(self, secret: str) -> None:
        self._key = secret.encode("utf-8")

    def issue(self, payload: Mapping[str, object]) -> str:
        body = base64.urlsafe_b64encode(canonical_json_bytes(dict(payload))).rstrip(b"=")
        signature = hmac.new(self._key, _CURSOR_DOMAIN + body, hashlib.sha256).digest()
        token = (body + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode("ascii")
        if len(token.encode("ascii")) > CATALOG_SYNC_CURSOR_BYTES_MAX:
            raise RuntimeError("catalog synchronization cursor exceeds its wire bound")
        return token

    def decode(self, token: str) -> dict[str, object]:
        try:
            if (
                not token.isascii()
                or not 1 <= len(token.encode("ascii")) <= CATALOG_SYNC_CURSOR_BYTES_MAX
            ):
                raise ValueError
            body, encoded_signature = token.split(".")
            signature = base64.b64decode(
                encoded_signature + "=" * (-len(encoded_signature) % 4),
                altchars=b"-_",
                validate=True,
            )
            expected = hmac.new(
                self._key,
                _CURSOR_DOMAIN + body.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            decoded = base64.b64decode(
                body + "=" * (-len(body) % 4),
                altchars=b"-_",
                validate=True,
            )
            value = json.loads(decoded)
            if not isinstance(value, dict) or self.issue(value) != token:
                raise ValueError
            return cast(dict[str, object], value)
        except (UnicodeError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            raise BadRequest("catalog synchronization cursor is invalid") from None


class SqlAlchemyCatalogSyncService:
    """Three bounded read operations over one transactionally published catalog log."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._session_factory = session_factory or make_session_factory(config.database_url)
        self._tokens = _CatalogSyncCursorCodec(config.browse_token_signing_key)
        self._bootstrap_lifetime = config.catalog_sync_bootstrap_lifetime
        self._cursor_lifetime = config.catalog_sync_cursor_lifetime
        self._history_retention = config.catalog_sync_history_retention
        self._page_size_max = config.catalog_sync_page_size_max
        self._history_reap_batch_size = config.catalog_sync_history_reap_batch_size

    def checkpoint(self, *, principal: ApplicationPrincipal) -> CatalogSyncCheckpoint:
        view = _authorization_view(principal)
        principal_identity = _principal_identity(principal)
        visible = collection_access_filter(
            CollectionRecord.id,
            principal,
            CATALOG_READ,
            published_filter=CollectionRecord.is_published.is_(True),
        )
        with read_snapshot(self._session_factory) as session:
            state = _state(session)
            upper = int(
                session.scalar(
                    select(CollectionRecord.id)
                    .where(visible)
                    .order_by(CollectionRecord.id.desc())
                    .limit(1)
                )
                or 0
            )
            cursor = self._tokens.issue(
                {
                    "after": 0,
                    "deadline": _deadline(self._bootstrap_lifetime),
                    "mode": "catalog",
                    "principal": principal_identity,
                    "source": state.source_identity,
                    "start": state.committed_revision,
                    "upper": upper,
                    "version": 1,
                    "view": view,
                }
            )
            return CatalogSyncCheckpoint(
                source_identity=state.source_identity,
                authorization_view_identity=view,
                catalog_cursor=cursor,
            )

    def collections(
        self,
        *,
        cursor: str,
        limit: int,
        principal: ApplicationPrincipal,
    ) -> CatalogSyncCollectionPage:
        self._validate_limit(limit)
        view = _authorization_view(principal)
        principal_identity = _principal_identity(principal)
        visible = collection_access_filter(
            CollectionRecord.id,
            principal,
            CATALOG_READ,
            published_filter=CollectionRecord.is_published.is_(True),
        )
        with read_snapshot(self._session_factory) as session:
            state = _state(session)
            payload = self._decode_cursor(
                cursor,
                mode="catalog",
                state=state,
                view=view,
                principal_identity=principal_identity,
            )
            after = _cursor_integer(payload, "after")
            upper = _cursor_integer(payload, "upper")
            start = _cursor_integer(payload, "start")
            _require_retained(state, start)
            rows = list(
                session.execute(
                    _catalog_collection_page_statement(
                        visible=visible,
                        after=after,
                        upper=upper,
                        limit=limit,
                    )
                )
            )
            has_more = len(rows) > limit
            page_rows = rows[:limit]
            descriptors = [
                _descriptor(
                    collection_id=int(row.id),
                    archive_root_sha256=row.archive_root_sha256,
                    content_identity=row.content_identity,
                    revision=row.catalog_revision,
                )
                for row in page_rows
            ]
            if has_more:
                next_cursor = self._tokens.issue(
                    {
                        **payload,
                        "after": int(page_rows[-1].id),
                    }
                )
                changes_cursor = None
            else:
                next_cursor = None
                changes_cursor = self._tokens.issue(
                    {
                        "after": start,
                        "bootstrap": True,
                        "deadline": _cursor_integer(payload, "deadline"),
                        "mode": "changes",
                        "principal": principal_identity,
                        "source": state.source_identity,
                        "through": state.committed_revision,
                        "version": 1,
                        "view": view,
                    }
                )
            return CatalogSyncCollectionPage(
                source_identity=state.source_identity,
                authorization_view_identity=view,
                collections=descriptors,
                next_cursor=next_cursor,
                changes_cursor=changes_cursor,
            )

    def changes(
        self,
        *,
        cursor: str,
        limit: int,
        principal: ApplicationPrincipal,
    ) -> CatalogSyncChangePage:
        self._validate_limit(limit)
        view = _authorization_view(principal)
        principal_identity = _principal_identity(principal)
        with read_snapshot(self._session_factory) as session:
            state = _state(session)
            payload = self._decode_cursor(
                cursor,
                mode="changes",
                state=state,
                view=view,
                principal_identity=principal_identity,
            )
            after = _cursor_integer(payload, "after")
            _require_retained(state, after)
            raw_through = payload.get("through")
            through = (
                state.committed_revision
                if raw_through is None
                else _cursor_integer(payload, "through")
            )
            if through > state.committed_revision:
                raise CatalogSyncSourceChanged(
                    "catalog synchronization horizon is ahead of the current source"
                )
            scanned = list(
                session.scalars(
                    _catalog_change_revision_statement(
                        after=after,
                        through=through,
                        limit=limit,
                    )
                )
            )
            has_more = len(scanned) > limit
            page_revisions = [int(value) for value in scanned[:limit] if value is not None]
            if (not page_revisions and after < through) or any(
                revision != after + offset
                for offset, revision in enumerate(page_revisions, start=1)
            ):
                raise CatalogSyncHistoryExpired(
                    "catalog synchronization history has a gap before its horizon"
                )
            page_through = page_revisions[-1] if has_more else through
            if page_revisions:
                visibility, projected_change = catalog_event_projection(principal, CATALOG_READ)
                rows = session.execute(
                    select(CatalogEventRecord, projected_change.label("projected_change"))
                    .where(
                        CatalogEventRecord.revision.in_(page_revisions),
                        visibility,
                    )
                    .order_by(CatalogEventRecord.revision)
                ).all()
            else:
                rows = []
            changes: list[CatalogSyncChange] = []
            for event, projected in rows:
                if event.revision is None:
                    raise RuntimeError("published catalog event has no revision")
                if projected == "deleted":
                    changes.append(
                        CatalogSyncDelete(
                            collection_id=event.collection_id,
                            revision=str(event.revision),
                        )
                    )
                else:
                    changes.append(
                        CatalogSyncUpsert(
                            collection_id=event.collection_id,
                            archive_root_sha256=event.archive_root_sha256,
                            content_identity=event.content_identity,
                            revision=str(event.revision),
                        )
                    )
            bootstrap = bool(payload.get("bootstrap"))
            next_cursor = self._tokens.issue(
                {
                    "after": page_through,
                    "bootstrap": bootstrap and has_more,
                    "deadline": (
                        _cursor_integer(payload, "deadline")
                        if bootstrap and has_more
                        else _deadline(self._cursor_lifetime)
                    ),
                    "mode": "changes",
                    "principal": principal_identity,
                    "source": state.source_identity,
                    "through": through if has_more else None,
                    "version": 1,
                    "view": view,
                }
            )
            return CatalogSyncChangePage(
                source_identity=state.source_identity,
                authorization_view_identity=view,
                changes=changes,
                next_cursor=next_cursor,
                caught_up=not has_more,
                through_revision=str(page_through),
            )

    def reap_expired_history(self, *, limit: int | None = None) -> int:
        limit = self._history_reap_batch_size if limit is None else limit
        if limit < 1:
            raise ValueError("catalog synchronization reaper limit must be positive")
        cutoff = format_utc_timestamp(utc_now() - self._history_retention)
        with session_scope(self._session_factory) as session:
            state = session.scalar(
                select(CatalogSyncStateRecord)
                .where(CatalogSyncStateRecord.singleton == 1)
                .with_for_update()
            )
            if state is None:
                raise RuntimeError("catalog synchronization state is unavailable")
            rows = list(
                session.scalars(
                    select(CatalogEventRecord)
                    .where(
                        CatalogEventRecord.published.is_(True),
                        CatalogEventRecord.revision > state.retained_revision,
                    )
                    .order_by(CatalogEventRecord.revision)
                    .limit(limit)
                )
            )
            expired = []
            for event in rows:
                if event.committed_at is None or event.committed_at >= cutoff:
                    break
                expired.append(event)
            if not expired:
                return 0
            last_revision = expired[-1].revision
            if last_revision is None:
                raise RuntimeError("published catalog event has no revision")
            session.execute(
                delete(CatalogEventRecord).where(
                    CatalogEventRecord.sequence.in_([event.sequence for event in expired])
                )
            )
            state.retained_revision = int(last_revision)
            return len(expired)

    def _decode_cursor(
        self,
        token: str,
        *,
        mode: _CursorMode,
        state: CatalogSyncStateRecord,
        view: str,
        principal_identity: str,
    ) -> dict[str, object]:
        payload = self._tokens.decode(token)
        expected = (
            {
                "after",
                "deadline",
                "mode",
                "principal",
                "source",
                "start",
                "upper",
                "version",
                "view",
            }
            if mode == "catalog"
            else {
                "after",
                "bootstrap",
                "deadline",
                "mode",
                "principal",
                "source",
                "through",
                "version",
                "view",
            }
        )
        if set(payload) != expected or payload.get("version") != 1 or payload.get("mode") != mode:
            raise BadRequest("catalog synchronization cursor is invalid")
        if payload.get("source") != state.source_identity:
            raise CatalogSyncSourceChanged("catalog synchronization source identity changed")
        if payload.get("principal") != principal_identity:
            raise Forbidden("catalog synchronization cursor belongs to another principal")
        if payload.get("view") != view:
            raise CatalogSyncViewChanged("catalog synchronization authorization view changed")
        if int(utc_now().timestamp()) >= _cursor_integer(payload, "deadline"):
            raise CatalogSyncCursorExpired("catalog synchronization cursor expired")
        return payload

    def _validate_limit(self, limit: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= min(self._page_size_max, CATALOG_SYNC_PAGE_SIZE_MAX)
        ):
            raise BadRequest("catalog synchronization page size is outside the configured bound")


def _state(session: Session) -> CatalogSyncStateRecord:
    state = session.get(CatalogSyncStateRecord, 1)
    if state is None:
        raise RuntimeError("catalog synchronization state is unavailable")
    return state


def _catalog_collection_page_statement(
    *,
    visible: ColumnElement[bool],
    after: int,
    upper: int,
    limit: int,
) -> Select[tuple[Any, ...]]:
    return (
        select(
            CollectionRecord.id,
            CollectionRecord.archive_root_sha256,
            CollectionRecord.content_identity,
            CollectionRecord.catalog_revision,
        )
        .where(
            visible,
            CollectionRecord.id > after,
            CollectionRecord.id <= upper,
        )
        .order_by(CollectionRecord.id)
        .limit(limit + 1)
    )


def _catalog_change_revision_statement(
    *,
    after: int,
    through: int,
    limit: int,
) -> Select[tuple[int | None]]:
    return (
        select(CatalogEventRecord.revision)
        .where(
            CatalogEventRecord.published.is_(True),
            CatalogEventRecord.revision.is_not(None),
            CatalogEventRecord.revision > after,
            CatalogEventRecord.revision <= through,
        )
        .order_by(CatalogEventRecord.revision)
        .limit(limit + 1)
    )


def _authorization_view(principal: ApplicationPrincipal) -> str:
    if principal.authorization_view_identity is not None:
        return principal.authorization_view_identity
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "access": sorted((item.permission, item.resource) for item in principal.access),
                "app": principal.app,
                "key_id": principal.key_id,
            }
        )
    ).hexdigest()


def _principal_identity(principal: ApplicationPrincipal) -> str:
    return hashlib.sha256(
        canonical_json_bytes({"app": principal.app, "key_id": principal.key_id})
    ).hexdigest()


def _deadline(lifetime: timedelta) -> int:
    return int((utc_now() + lifetime).timestamp())


def _cursor_integer(payload: Mapping[str, object], name: str) -> int:
    value = payload.get(name)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_CATALOG_SYNC_REVISION
    ):
        raise BadRequest("catalog synchronization cursor is invalid")
    return value


def _require_retained(state: CatalogSyncStateRecord, position: int) -> None:
    if position < state.retained_revision:
        raise CatalogSyncHistoryExpired("catalog synchronization history expired")
    if position > state.committed_revision:
        raise CatalogSyncSourceChanged(
            "catalog synchronization position is ahead of the current source"
        )


def _descriptor(
    *,
    collection_id: int,
    archive_root_sha256: str | None,
    content_identity: str,
    revision: int | None,
) -> CatalogSyncDescriptor:
    if archive_root_sha256 is None or revision is None:
        raise RuntimeError("published collection has no synchronization identity")
    return CatalogSyncDescriptor(
        collection_id=collection_id,
        archive_root_sha256=archive_root_sha256,
        content_identity=content_identity,
        revision=str(revision),
    )


__all__ = ["SqlAlchemyCatalogSyncService"]
