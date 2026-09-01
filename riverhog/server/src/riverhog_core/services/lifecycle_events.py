from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from lifecycle_events import CloudEvent, cloud_event, normalize_event_context
from riverhog_protocol.lifecycle_events import (
    RIVERHOG_EVENT_TYPE_PREFIX,
    RiverhogEventPage,
    RiverhogLifecycleEvent,
    normalize_riverhog_event_type,
    validate_lifecycle_event_cursor,
    validate_riverhog_event,
)
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from time_formats import format_utc_timestamp, parse_utc_timestamp, utc_now

from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionRecord,
    CollectionTagRecord,
    CollectionUploadRecord,
    CollectionUploadTagRecord,
    LifecycleEventRecord,
    RetrievalJobRecord,
    RetrievalPlanFileRecord,
)
from riverhog_core.runtime_config import RuntimeConfig


def event_context_json(value: Mapping[str, Any] | None) -> str | None:
    normalized = normalize_event_context(value)
    return (
        json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        if normalized is not None
        else None
    )


def decode_event_context(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    value = json.loads(raw)
    return normalize_event_context(value)


def terminal_context_expiry(config: RuntimeConfig, *, terminal_at: str | None = None) -> str:
    current = parse_utc_timestamp(terminal_at) if terminal_at is not None else utc_now()
    return format_utc_timestamp(current + config.event_context_retention)


class SqlAlchemyLifecycleEventService:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._config = config
        self._session_factory = session_factory or make_session_factory(config.database_url)

    def emit(
        self,
        *,
        owner_app: str,
        type: str,
        subject: str | None,
        data: Mapping[str, Any] | None = None,
        context_json: str | None = None,
        context_expires_at: str | None = None,
        session: Session | None = None,
    ) -> RiverhogLifecycleEvent:
        event = validate_riverhog_event(
            cloud_event(
                source=self._config.event_source,
                type=normalize_riverhog_event_type(type),
                subject=subject,
                data=data,
            )
        )
        record = LifecycleEventRecord(
            event_id=event.id,
            owner_app=owner_app,
            subject=subject,
            event_json=event.model_dump_json(exclude_none=True),
            context_json=context_json,
            context_expires_at=context_expires_at,
        )
        if session is not None:
            session.add(record)
        else:
            with session_scope(self._session_factory) as current_session:
                current_session.add(record)
        return event

    def page(
        self,
        *,
        owner_app: str | None,
        after: str | None,
        limit: int,
    ) -> RiverhogEventPage:
        cursor = int(validate_lifecycle_event_cursor("0" if after is None else after))
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        current_text = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            statement = select(LifecycleEventRecord).where(LifecycleEventRecord.sequence > cursor)
            if owner_app is not None:
                statement = statement.where(LifecycleEventRecord.owner_app == owner_app)
            rows = list(
                session.scalars(
                    statement.order_by(LifecycleEventRecord.sequence.asc()).limit(limit + 1)
                )
            )
        has_more = len(rows) > limit
        selected = rows[:limit]
        events: list[RiverhogLifecycleEvent] = []
        for row in selected:
            event = CloudEvent.model_validate_json(row.event_json)
            if row.context_json is not None and (
                row.context_expires_at is None or row.context_expires_at > current_text
            ):
                data = dict(event.data)
                data["context"] = decode_event_context(row.context_json)
                event = event.model_copy(update={"data": data})
            events.append(validate_riverhog_event(event))
        return RiverhogEventPage(
            events=events,
            next_cursor=str(selected[-1].sequence if selected else cursor),
            has_more=has_more,
        )

    def reap_expired_contexts(self) -> int:
        """Reclaim one configured, restartable batch of expired context payloads."""

        current_text = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            sequences = list(
                session.scalars(
                    select(LifecycleEventRecord.sequence)
                    .where(
                        LifecycleEventRecord.context_json.is_not(None),
                        LifecycleEventRecord.context_expires_at.is_not(None),
                        LifecycleEventRecord.context_expires_at <= current_text,
                    )
                    .order_by(
                        LifecycleEventRecord.context_expires_at,
                        LifecycleEventRecord.sequence,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(self._config.event_context_reap_batch_size)
                )
            )
            if not sequences:
                return 0
            session.execute(
                update(LifecycleEventRecord)
                .where(LifecycleEventRecord.sequence.in_(sequences))
                .values(context_json=None, context_expires_at=None)
            )
            return len(sequences)

    def emit_collection(
        self,
        *,
        type: str,
        collection_id: int,
        details: Mapping[str, Any] | None = None,
        terminal: bool = False,
        initiator: ApplicationPrincipal | None = None,
        event_context_json: str | None = None,
        session: Session | None = None,
    ) -> RiverhogLifecycleEvent | None:
        if session is None:
            with session_scope(self._session_factory) as current_session:
                return self.emit_collection(
                    type=type,
                    collection_id=collection_id,
                    details=details,
                    terminal=terminal,
                    initiator=initiator,
                    event_context_json=event_context_json,
                    session=current_session,
                )
        upload = session.get(CollectionUploadRecord, collection_id)
        collection = session.get(CollectionRecord, collection_id)
        if upload is None and collection is None:
            return None
        if initiator is not None:
            owner_app = initiator.app
            owner_key_id = initiator.key_id
            context_json = event_context_json
        elif upload is not None:
            owner_app = upload.initiated_by_app
            owner_key_id = upload.initiated_by_key_id
            context_json = upload.event_context_json
        else:
            assert collection is not None
            owner_app = collection.created_by_app
            owner_key_id = collection.created_by_key_id
            context_json = None
        expires_at = terminal_context_expiry(self._config) if terminal else None
        if expires_at is not None and context_json is not None:
            self.expire_context(
                owner_app=owner_app,
                subject=str(collection_id),
                expires_at=expires_at,
                session=session,
            )
        data: dict[str, Any] = {
            "collection_id": collection_id,
            "actor": actor_data(app="riverhog"),
            "initiator": actor_data(app=owner_app, key_id=owner_key_id),
        }
        if collection is not None:
            data["collection_created_at"] = collection.created_at
            data["collection_tag_count"] = int(
                session.scalar(
                    select(func.count())
                    .select_from(CollectionTagRecord)
                    .where(CollectionTagRecord.collection_id == collection_id)
                )
                or 0
            )
        elif upload is not None:
            data["collection_created_at"] = upload.opened_at
            data["collection_tag_count"] = int(
                session.scalar(
                    select(func.count())
                    .select_from(CollectionUploadTagRecord)
                    .where(CollectionUploadTagRecord.collection_id == collection_id)
                )
                or 0
            )
        data.update(details or {})
        return self.emit(
            owner_app=owner_app,
            type=type,
            subject=str(collection_id),
            data=data,
            context_json=context_json,
            context_expires_at=expires_at,
            session=session,
        )

    def emit_retrieval(
        self,
        *,
        type: str,
        job: RetrievalJobRecord,
        details: Mapping[str, Any] | None = None,
        terminal: bool = False,
        session: Session,
    ) -> RiverhogLifecycleEvent:
        expires_at = terminal_context_expiry(self._config) if terminal else None
        if expires_at is not None and job.event_context_json is not None:
            self.expire_context(
                owner_app=job.app,
                subject=job.id,
                expires_at=expires_at,
                session=session,
            )
        collection_ids = list(
            session.scalars(
                select(RetrievalPlanFileRecord.collection_id)
                .where(RetrievalPlanFileRecord.plan_id == job.plan_id)
                .distinct()
                .order_by(RetrievalPlanFileRecord.collection_id)
            )
        )
        data: dict[str, Any] = {
            "retrieval_id": job.id,
            "collection_ids": collection_ids,
            "state": job.state,
            "actor": actor_data(app="riverhog"),
            "initiator": actor_data(app=job.app, key_id=job.initiated_by_key_id),
        }
        if len(collection_ids) == 1:
            collection_id = collection_ids[0]
            collection = session.get(CollectionRecord, collection_id)
            data["collection_id"] = collection_id
            if collection is not None:
                data["collection_created_at"] = collection.created_at
                data["collection_tag_count"] = int(
                    session.scalar(
                        select(func.count())
                        .select_from(CollectionTagRecord)
                        .where(CollectionTagRecord.collection_id == collection_id)
                    )
                    or 0
                )
        data.update(details or {})
        return self.emit(
            owner_app=job.app,
            type=type,
            subject=job.id,
            data=data,
            context_json=job.event_context_json,
            context_expires_at=expires_at,
            session=session,
        )

    def expire_context(
        self,
        *,
        owner_app: str,
        subject: str,
        expires_at: str,
        session: Session,
    ) -> None:
        session.execute(
            update(LifecycleEventRecord)
            .where(
                LifecycleEventRecord.owner_app == owner_app,
                LifecycleEventRecord.subject == subject,
                LifecycleEventRecord.context_json.is_not(None),
                LifecycleEventRecord.context_expires_at.is_(None),
            )
            .values(context_expires_at=expires_at)
        )


def actor_data(*, app: str, key_id: str | None = None) -> dict[str, str]:
    payload = {"app": app}
    if key_id is not None:
        payload["key_id"] = key_id
    return payload


__all__ = [
    "RIVERHOG_EVENT_TYPE_PREFIX",
    "SqlAlchemyLifecycleEventService",
    "actor_data",
    "decode_event_context",
    "event_context_json",
    "terminal_context_expiry",
]
