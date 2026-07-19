from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lifecycle_events import CloudEvent, EventPage, cloud_event, normalize_event_context
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionRecord,
    CollectionUploadRecord,
    LifecycleEventRecord,
    RetrievalJobRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.timestamps import format_utc_timestamp, parse_utc_timestamp, utc_now

RIVERHOG_EVENT_TYPE_PREFIX = "io.riverhog.riverhog."


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
    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._session_factory = make_session_factory(config.database_url)

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
    ) -> CloudEvent:
        normalized_type = type if type.startswith(RIVERHOG_EVENT_TYPE_PREFIX) else (
            RIVERHOG_EVENT_TYPE_PREFIX + type
        )
        event = cloud_event(
            source=self._config.event_source,
            type=normalized_type,
            subject=subject,
            data=data,
        )
        record = LifecycleEventRecord(
            event_id=event.id,
            owner_app=owner_app,
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
    ) -> EventPage:
        try:
            cursor = int((after or "0").strip())
        except ValueError as exc:
            raise ValueError("after must be a non-negative event cursor") from exc
        if cursor < 0:
            raise ValueError("after must be a non-negative event cursor")
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        with session_scope(self._session_factory) as session:
            current_text = format_utc_timestamp(utc_now())
            session.execute(
                update(LifecycleEventRecord)
                .where(
                    LifecycleEventRecord.context_json.is_not(None),
                    LifecycleEventRecord.context_expires_at.is_not(None),
                    LifecycleEventRecord.context_expires_at <= current_text,
                )
                .values(context_json=None, context_expires_at=None)
            )
            statement = select(LifecycleEventRecord).where(
                LifecycleEventRecord.sequence > cursor
            )
            if owner_app is not None:
                statement = statement.where(LifecycleEventRecord.owner_app == owner_app)
            rows = list(
                session.scalars(
                    statement.order_by(LifecycleEventRecord.sequence.asc()).limit(limit + 1)
                )
            )
        has_more = len(rows) > limit
        selected = rows[:limit]
        events: list[CloudEvent] = []
        for row in selected:
            event = CloudEvent.model_validate_json(row.event_json)
            if row.context_json is not None:
                data = dict(event.data)
                data["context"] = decode_event_context(row.context_json)
                event = event.model_copy(update={"data": data})
            events.append(event)
        return EventPage(
            events=events,
            next_cursor=str(selected[-1].sequence if selected else cursor),
            has_more=has_more,
        )

    def emit_collection(
        self,
        *,
        type: str,
        collection_id: str,
        details: Mapping[str, Any] | None = None,
        terminal: bool = False,
        session: Session | None = None,
    ) -> CloudEvent | None:
        if session is None:
            with session_scope(self._session_factory) as current_session:
                return self.emit_collection(
                    type=type,
                    collection_id=collection_id,
                    details=details,
                    terminal=terminal,
                    session=current_session,
                )
        upload = session.get(CollectionUploadRecord, collection_id)
        collection = session.get(CollectionRecord, collection_id)
        if upload is not None:
            owner_app = upload.initiated_by_app
            owner_key_id = upload.initiated_by_key_id
            context_json = upload.event_context_json
        elif collection is not None:
            owner_app = collection.created_by_app
            owner_key_id = collection.created_by_key_id
            context_json = None
        else:
            return None
        expires_at = terminal_context_expiry(self._config) if terminal else None
        if terminal and context_json is not None:
            self.expire_context(
                owner_app=owner_app,
                subject=collection_id,
                expires_at=expires_at,
                session=session,
            )
        data: dict[str, Any] = {
            "collection_id": collection_id,
            "actor": actor_data(app="riverhog"),
            "initiator": actor_data(app=owner_app, key_id=owner_key_id),
        }
        data.update(details or {})
        return self.emit(
            owner_app=owner_app,
            type=type,
            subject=collection_id,
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
    ) -> CloudEvent:
        expires_at = terminal_context_expiry(self._config) if terminal else None
        if terminal and job.event_context_json is not None:
            self.expire_context(
                owner_app=job.app,
                subject=job.id,
                expires_at=expires_at,
                session=session,
            )
        data: dict[str, Any] = {
            "retrieval_id": job.id,
            "state": job.state,
            "actor": actor_data(app="riverhog"),
            "initiator": actor_data(app=job.app, key_id=job.initiated_by_key_id),
        }
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
        rows = list(
            session.scalars(
                select(LifecycleEventRecord).where(
                    LifecycleEventRecord.owner_app == owner_app,
                    LifecycleEventRecord.context_json.is_not(None),
                    LifecycleEventRecord.context_expires_at.is_(None),
                )
            )
        )
        for row in rows:
            event = CloudEvent.model_validate_json(row.event_json)
            if event.subject == subject:
                row.context_expires_at = expires_at


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
