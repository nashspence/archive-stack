from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
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
    decode_collection_tag_node,
)
from riverhog_protocol.errors import (
    BadRequest,
    CatalogSyncCursorExpired,
    CatalogSyncHistoryExpired,
    CatalogSyncSourceChanged,
    CatalogSyncViewChanged,
    Forbidden,
)
from sqlalchemy import and_, delete, exists, or_, select
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
    CollectionTagMembershipRecord,
    CollectionTagMutationNodeReferenceRecord,
    CollectionTagMutationRecord,
    CollectionTagNodeEdgeRecord,
    CollectionTagNodeReclamationRecord,
    CollectionTagNodeRecord,
    CollectionTagPublicationFrontierRecord,
    CollectionTagPublicationRecord,
    CollectionTagRecord,
    CollectionTagRevisionRecord,
    CollectionTagVisibilityRecord,
    CollectionUploadTagNodeReferenceRecord,
    CollectionUploadTagPublicationFrontierRecord,
)
from riverhog_core.collection_access import collection_access_filter
from riverhog_core.runtime_config import RuntimeConfig

_CURSOR_DOMAIN = b"riverhog-catalog-sync-cursor/v1\x00"
_CursorMode = Literal["catalog", "changes"]


@dataclass(frozen=True, slots=True)
class _TagHistoryCleanupMetrics:
    selected_rows: int = 0
    locked_rows: int = 0
    changed_rows: int = 0
    deleted_rows: int = 0


@dataclass(frozen=True, slots=True)
class _TagHistoryCleanupAction:
    changed_rows: int
    deleted_rows: int


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
        self._browse_token_lifetime = config.browse_token_lifetime
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
                    description=row.description,
                    description_revision=row.description_revision,
                    description_identity=row.description_identity,
                    tag_revision=row.tag_revision,
                    tag_set_identity=row.tag_set_identity,
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
                            description=event.description,
                            description_revision=event.description_revision,
                            description_identity=event.description_identity,
                            tag_revision=event.tag_revision,
                            tag_set_identity=event.tag_set_identity,
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
        now = utc_now()
        cutoff = format_utc_timestamp(now - self._history_retention)
        cleanup_before = format_utc_timestamp(now - self._browse_token_lifetime)
        cleanup_started_at = format_utc_timestamp(now)
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
            if expired:
                last_revision = expired[-1].revision
                if last_revision is None:
                    raise RuntimeError("published catalog event has no revision")
                session.execute(
                    delete(CatalogEventRecord).where(
                        CatalogEventRecord.sequence.in_([event.sequence for event in expired])
                    )
                )
                state.retained_revision = int(last_revision)
                session.flush()
            _reap_unreferenced_tag_history(
                session,
                limit=limit - len(expired),
                cleanup_before=cleanup_before,
                cleanup_started_at=cleanup_started_at,
            )
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


def _reap_unreferenced_tag_history(
    session: Session,
    *,
    limit: int,
    cleanup_before: str,
    cleanup_started_at: str,
) -> _TagHistoryCleanupMetrics:
    """Apply at most ``limit`` durable row changes to retired tag history."""

    changed = 0
    deleted = 0
    while changed < max(0, limit):
        action = _reap_unreferenced_tag_history_rows(
            session,
            limit=limit - changed,
            cleanup_before=cleanup_before,
            cleanup_started_at=cleanup_started_at,
        )
        if action is None:
            break
        session.flush()
        changed += action.changed_rows
        deleted += action.deleted_rows
    return _TagHistoryCleanupMetrics(
        selected_rows=changed,
        locked_rows=changed,
        changed_rows=changed,
        deleted_rows=deleted,
    )


def _reap_unreferenced_tag_history_rows(
    session: Session,
    *,
    limit: int,
    cleanup_before: str,
    cleanup_started_at: str,
) -> _TagHistoryCleanupAction | None:
    reclamation = session.scalar(
        select(CollectionTagNodeReclamationRecord)
        .order_by(CollectionTagNodeReclamationRecord.node_digest)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if reclamation is not None:
        node = session.scalar(
            select(CollectionTagNodeRecord)
            .where(CollectionTagNodeRecord.digest == reclamation.node_digest)
            .with_for_update()
        )
        if node is None:
            session.delete(reclamation)
            return _TagHistoryCleanupAction(changed_rows=1, deleted_rows=1)
        if _tag_node_has_live_owner(session, reclamation.node_digest):
            expected_children = {
                child.digest for child in decode_collection_tag_node(node.encoded).children
            }
            actual_children = set(
                session.scalars(
                    select(CollectionTagNodeEdgeRecord.child_digest).where(
                        CollectionTagNodeEdgeRecord.parent_digest == reclamation.node_digest
                    )
                )
            )
            if actual_children != expected_children:
                raise RuntimeError("a partially reclaimed catalog tag node gained an owner")
            session.delete(reclamation)
            return _TagHistoryCleanupAction(changed_rows=1, deleted_rows=1)
        edge = session.scalar(
            select(CollectionTagNodeEdgeRecord)
            .where(CollectionTagNodeEdgeRecord.parent_digest == reclamation.node_digest)
            .order_by(CollectionTagNodeEdgeRecord.child_digest)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if edge is not None:
            session.delete(edge)
            return _TagHistoryCleanupAction(changed_rows=1, deleted_rows=1)
        session.delete(node)
        return _TagHistoryCleanupAction(changed_rows=1, deleted_rows=1)

    construction = list(
        session.scalars(
            select(CollectionTagMutationNodeReferenceRecord)
            .join(
                CollectionTagMutationRecord,
                and_(
                    CollectionTagMutationRecord.collection_id
                    == CollectionTagMutationNodeReferenceRecord.collection_id,
                    CollectionTagMutationRecord.operation_id
                    == CollectionTagMutationNodeReferenceRecord.operation_id,
                ),
            )
            .where(CollectionTagMutationRecord.state == "succeeded")
            .where(
                ~exists(
                    select(1)
                    .select_from(CollectionTagPublicationRecord)
                    .join(
                        CollectionTagPublicationFrontierRecord,
                        and_(
                            CollectionTagPublicationFrontierRecord.collection_id
                            == CollectionTagPublicationRecord.collection_id,
                            CollectionTagPublicationFrontierRecord.store
                            == CollectionTagPublicationRecord.store,
                        ),
                    )
                    .where(
                        CollectionTagPublicationRecord.collection_id
                        == CollectionTagMutationRecord.collection_id,
                        CollectionTagPublicationRecord.desired_head_identity
                        == CollectionTagMutationRecord.result_head_identity,
                        CollectionTagPublicationFrontierRecord.head_identity
                        == CollectionTagMutationRecord.result_head_identity,
                        CollectionTagPublicationFrontierRecord.expanded.is_(False),
                    )
                )
            )
            .order_by(
                CollectionTagMutationNodeReferenceRecord.collection_id,
                CollectionTagMutationNodeReferenceRecord.operation_id,
                CollectionTagMutationNodeReferenceRecord.node_digest,
            )
            .limit(limit)
            .with_for_update(
                skip_locked=True,
                of=CollectionTagMutationNodeReferenceRecord,
            )
        )
    )
    if construction:
        for construction_reference in construction:
            session.delete(construction_reference)
        return _TagHistoryCleanupAction(
            changed_rows=len(construction), deleted_rows=len(construction)
        )

    frontier = list(
        session.scalars(
            select(CollectionTagPublicationFrontierRecord)
            .join(
                CollectionTagRevisionRecord,
                and_(
                    CollectionTagRevisionRecord.collection_id
                    == CollectionTagPublicationFrontierRecord.collection_id,
                    CollectionTagRevisionRecord.head_identity
                    == CollectionTagPublicationFrontierRecord.head_identity,
                ),
            )
            .where(
                CollectionTagRevisionRecord.cleanup_started_at.is_not(None),
                CollectionTagRevisionRecord.cleanup_started_at < cleanup_before,
            )
            .order_by(
                CollectionTagRevisionRecord.cleanup_started_at,
                CollectionTagRevisionRecord.collection_id,
                CollectionTagRevisionRecord.revision,
                CollectionTagPublicationFrontierRecord.store,
                CollectionTagPublicationFrontierRecord.node_digest,
            )
            .limit(limit)
            .with_for_update(
                skip_locked=True,
                of=CollectionTagPublicationFrontierRecord,
            )
        )
    )
    if frontier:
        for frontier_row in frontier:
            session.delete(frontier_row)
        return _TagHistoryCleanupAction(changed_rows=len(frontier), deleted_rows=len(frontier))

    mutation = list(
        session.scalars(
            select(CollectionTagMutationRecord)
            .join(
                CollectionTagRevisionRecord,
                and_(
                    CollectionTagRevisionRecord.collection_id
                    == CollectionTagMutationRecord.collection_id,
                    CollectionTagRevisionRecord.revision
                    == CollectionTagMutationRecord.result_revision,
                ),
            )
            .where(
                CollectionTagRevisionRecord.cleanup_started_at.is_not(None),
                CollectionTagRevisionRecord.cleanup_started_at < cleanup_before,
                CollectionTagMutationRecord.state == "succeeded",
                ~exists(
                    select(1).where(
                        CollectionTagMutationNodeReferenceRecord.collection_id
                        == CollectionTagMutationRecord.collection_id,
                        CollectionTagMutationNodeReferenceRecord.operation_id
                        == CollectionTagMutationRecord.operation_id,
                    )
                ),
            )
            .order_by(
                CollectionTagRevisionRecord.cleanup_started_at,
                CollectionTagRevisionRecord.collection_id,
                CollectionTagRevisionRecord.revision,
                CollectionTagMutationRecord.operation_id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True, of=CollectionTagMutationRecord)
        )
    )
    if mutation:
        for mutation_row in mutation:
            session.delete(mutation_row)
        return _TagHistoryCleanupAction(changed_rows=len(mutation), deleted_rows=len(mutation))

    retiring = session.scalar(
        select(CollectionTagRevisionRecord)
        .where(
            CollectionTagRevisionRecord.cleanup_started_at.is_not(None),
            CollectionTagRevisionRecord.cleanup_started_at < cleanup_before,
            ~exists(
                select(1).where(
                    CollectionTagPublicationFrontierRecord.collection_id
                    == CollectionTagRevisionRecord.collection_id,
                    CollectionTagPublicationFrontierRecord.head_identity
                    == CollectionTagRevisionRecord.head_identity,
                )
            ),
            ~exists(
                select(1).where(
                    CollectionTagMutationRecord.collection_id
                    == CollectionTagRevisionRecord.collection_id,
                    CollectionTagMutationRecord.result_revision
                    == CollectionTagRevisionRecord.revision,
                    CollectionTagMutationRecord.state == "succeeded",
                )
            ),
        )
        .order_by(
            CollectionTagRevisionRecord.cleanup_started_at,
            CollectionTagRevisionRecord.collection_id,
            CollectionTagRevisionRecord.revision,
        )
        .limit(1)
        .with_for_update(skip_locked=True, of=CollectionTagRevisionRecord)
    )
    if retiring is not None:
        session.delete(retiring)
        return _TagHistoryCleanupAction(changed_rows=1, deleted_rows=1)

    eligible = session.scalar(
        select(CollectionTagRevisionRecord)
        .join(CollectionRecord, CollectionRecord.id == CollectionTagRevisionRecord.collection_id)
        .where(
            CollectionTagRevisionRecord.cleanup_started_at.is_(None),
            CollectionTagRevisionRecord.revision != CollectionRecord.tag_revision,
            ~exists(
                select(1).where(
                    CatalogEventRecord.collection_id == CollectionTagRevisionRecord.collection_id,
                    or_(
                        CatalogEventRecord.tag_revision == CollectionTagRevisionRecord.revision,
                        CatalogEventRecord.before_tag_revision
                        == CollectionTagRevisionRecord.revision,
                        CatalogEventRecord.after_tag_revision
                        == CollectionTagRevisionRecord.revision,
                    ),
                )
            ),
        )
        .order_by(
            CollectionTagRevisionRecord.collection_id,
            CollectionTagRevisionRecord.revision,
        )
        .limit(1)
        .with_for_update(skip_locked=True, of=CollectionTagRevisionRecord)
    )
    if eligible is not None:
        eligible.cleanup_started_at = cleanup_started_at
        return _TagHistoryCleanupAction(changed_rows=1, deleted_rows=0)

    visibility = session.scalar(
        select(CollectionTagVisibilityRecord)
        .where(
            or_(
                CollectionTagVisibilityRecord.end_revision.is_not(None),
                ~exists(
                    select(1).where(
                        CollectionRecord.id == CollectionTagVisibilityRecord.collection_id
                    )
                ),
            ),
            ~exists(
                select(1).where(
                    CatalogEventRecord.collection_id == CollectionTagVisibilityRecord.collection_id,
                    or_(
                        and_(
                            CatalogEventRecord.before_tag_revision.is_not(None),
                            CatalogEventRecord.before_tag_revision
                            >= CollectionTagVisibilityRecord.start_revision,
                            or_(
                                CollectionTagVisibilityRecord.end_revision.is_(None),
                                CatalogEventRecord.before_tag_revision
                                < CollectionTagVisibilityRecord.end_revision,
                            ),
                        ),
                        and_(
                            CatalogEventRecord.after_tag_revision.is_not(None),
                            CatalogEventRecord.after_tag_revision
                            >= CollectionTagVisibilityRecord.start_revision,
                            or_(
                                CollectionTagVisibilityRecord.end_revision.is_(None),
                                CatalogEventRecord.after_tag_revision
                                < CollectionTagVisibilityRecord.end_revision,
                            ),
                        ),
                    ),
                )
            ),
        )
        .order_by(
            CollectionTagVisibilityRecord.collection_id,
            CollectionTagVisibilityRecord.tag_sha256,
            CollectionTagVisibilityRecord.start_revision,
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if visibility is not None:
        session.delete(visibility)
        return _TagHistoryCleanupAction(changed_rows=1, deleted_rows=1)

    node = session.scalar(
        select(CollectionTagNodeRecord)
        .where(
            ~exists(
                select(1).where(
                    CollectionTagNodeReclamationRecord.node_digest == CollectionTagNodeRecord.digest
                )
            ),
            ~exists(
                select(1).where(
                    CollectionTagPublicationFrontierRecord.node_digest
                    == CollectionTagNodeRecord.digest
                )
            ),
            ~exists(
                select(1).where(
                    CollectionUploadTagPublicationFrontierRecord.node_digest
                    == CollectionTagNodeRecord.digest
                )
            ),
            ~exists(
                select(1).where(
                    CollectionUploadTagNodeReferenceRecord.node_digest
                    == CollectionTagNodeRecord.digest
                )
            ),
            ~exists(
                select(1).where(
                    CollectionTagMutationNodeReferenceRecord.node_digest
                    == CollectionTagNodeRecord.digest
                )
            ),
            ~exists(
                select(1).where(
                    CollectionTagNodeEdgeRecord.child_digest == CollectionTagNodeRecord.digest
                )
            ),
            ~exists(
                select(1).where(
                    CollectionTagRevisionRecord.root_sha256 == CollectionTagNodeRecord.digest
                )
            ),
        )
        .order_by(CollectionTagNodeRecord.digest)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if node is not None:
        session.add(
            CollectionTagNodeReclamationRecord(
                node_digest=node.digest,
                claimed_at=cleanup_started_at,
            )
        )
        return _TagHistoryCleanupAction(changed_rows=1, deleted_rows=0)

    unused_tag = session.scalar(
        select(CollectionTagRecord)
        .where(
            CollectionTagRecord.collection_count == 0,
            ~exists(
                select(1).where(
                    CollectionTagMembershipRecord.tag_sha256 == CollectionTagRecord.tag_sha256
                )
            ),
        )
        .order_by(CollectionTagRecord.tag_sha256)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if unused_tag is not None:
        session.delete(unused_tag)
        return _TagHistoryCleanupAction(changed_rows=1, deleted_rows=1)
    return None


def _tag_node_has_live_owner(session: Session, digest: str) -> bool:
    columns = (
        CollectionTagPublicationFrontierRecord.node_digest,
        CollectionUploadTagPublicationFrontierRecord.node_digest,
        CollectionUploadTagNodeReferenceRecord.node_digest,
        CollectionTagMutationNodeReferenceRecord.node_digest,
    )
    if any(session.scalar(select(exists().where(column == digest))) is True for column in columns):
        return True
    return any(
        (
            session.scalar(
                select(exists().where(CollectionTagNodeEdgeRecord.child_digest == digest))
            ),
            session.scalar(
                select(exists().where(CollectionTagRevisionRecord.root_sha256 == digest))
            ),
        )
    )


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
            CollectionRecord.description,
            CollectionRecord.description_revision,
            CollectionRecord.description_identity,
            CollectionRecord.tag_revision,
            CollectionRecord.tag_set_identity,
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
    description: str | None,
    description_revision: int,
    description_identity: str,
    tag_revision: int,
    tag_set_identity: str,
    revision: int | None,
) -> CatalogSyncDescriptor:
    if archive_root_sha256 is None or revision is None:
        raise RuntimeError("published collection has no synchronization identity")
    return CatalogSyncDescriptor(
        collection_id=collection_id,
        archive_root_sha256=archive_root_sha256,
        content_identity=content_identity,
        description=description,
        description_revision=description_revision,
        description_identity=description_identity,
        tag_revision=tag_revision,
        tag_set_identity=tag_set_identity,
        revision=str(revision),
    )


__all__ = ["SqlAlchemyCatalogSyncService"]
