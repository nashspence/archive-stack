from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
import pytest
from fastapi import FastAPI
from lifecycle_events import cloud_event
from pydantic import ValidationError
from riverhog_api.deps import get_container
from riverhog_api.routers.events import router as events_router
from riverhog_core.app_permissions import (
    EVENTS_READ,
    EVENTS_READ_ALL,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import LifecycleEventRecord
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService
from riverhog_protocol.lifecycle_events import (
    ARCHIVE_COPY_CANCELED,
    ARCHIVE_COPY_COMPLETED,
    ARCHIVE_COPY_ISSUE,
    ARCHIVE_COPY_REQUESTED,
    RIVERHOG_EVENT_TYPES,
    validate_riverhog_event,
)
from time_formats import utc_timestamp_now

from tests.unit.db_helpers import sqlite_url


def _tag_event_data(collection_id: int, owner: str) -> dict[str, object]:
    return {
        "collection_id": collection_id,
        "collection_created_at": utc_timestamp_now(),
        "collection_tags": [],
        "actor": {"app": "riverhog"},
        "initiator": {"app": owner},
    }


def test_every_emitted_riverhog_event_type_has_one_public_contract() -> None:
    root = Path(__file__).parents[2] / "riverhog/server/src/riverhog_core"
    emitted: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "type"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                    and keyword.value.value.startswith(
                        ("collection.", "archive_copy.", "retrieval.")
                    )
                ):
                    emitted.add("io.riverhog.riverhog." + keyword.value.value)

    assert emitted == RIVERHOG_EVENT_TYPES


@pytest.mark.parametrize(
    ("event_type", "state", "extra"),
    (
        (ARCHIVE_COPY_REQUESTED, "requested", {}),
        (ARCHIVE_COPY_COMPLETED, "completed", {}),
        (ARCHIVE_COPY_ISSUE, "failed", {"error": "provider unavailable"}),
        (ARCHIVE_COPY_CANCELED, "canceled", {}),
    ),
)
def test_archive_copy_event_type_binds_its_exact_lifecycle_state(
    event_type: str,
    state: str,
    extra: dict[str, str],
) -> None:
    data = {
        **_tag_event_data(1, "operator"),
        "source_store": "source",
        "destination_store": "destination",
        "state": state,
        **extra,
    }
    event = cloud_event(
        source="https://riverhog.invalid/events",
        type=event_type,
        subject="1",
        data=data,
    )

    assert validate_riverhog_event(event).data.state == state
    with pytest.raises(ValidationError):
        validate_riverhog_event(event.model_copy(update={"data": {**data, "state": "waiting"}}))


def test_context_expiry_targets_owner_and_subject_in_sql(tmp_path: Path) -> None:
    config = RuntimeConfig(database_url=sqlite_url(tmp_path / "catalog.sqlite3"))
    initialize_db(config.database_url)
    events = SqlAlchemyLifecycleEventService(config)
    events.emit(
        owner_app="alpha",
        type="collection.tags_changed",
        subject="1",
        data=_tag_event_data(1, "alpha"),
        context_json='{"route":"phone"}',
    )
    events.emit(
        owner_app="alpha",
        type="collection.tags_changed",
        subject="2",
        data=_tag_event_data(2, "alpha"),
        context_json='{"route":"desktop"}',
    )
    expires_at = "2026-08-02T00:00:00.000000Z"
    with session_scope(make_session_factory(config.database_url)) as session:
        events.expire_context(
            owner_app="alpha",
            subject="1",
            expires_at=expires_at,
            session=session,
        )

    with session_scope(make_session_factory(config.database_url)) as session:
        records = {
            record.subject: record
            for record in session.query(LifecycleEventRecord).order_by(
                LifecycleEventRecord.sequence
            )
        }
        assert records["1"].context_expires_at == expires_at
        assert records["2"].context_expires_at is None


def test_lifecycle_event_api_scopes_normal_readers_to_their_application(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(database_url=sqlite_url(tmp_path / "catalog.sqlite3"))
    initialize_db(config.database_url)
    app_keys = SqlAlchemyAppKeyService(config)
    events = SqlAlchemyLifecycleEventService(config)
    grantor = ApplicationPrincipal(
        app="bootstrap",
        key_id=None,
        access=frozenset(),
        unrestricted_delegation=True,
    )
    alpha_token = str(
        app_keys.create(app="alpha", access=[ApplicationAccess(EVENTS_READ)], grantor=grantor)[
            "token"
        ]
    )
    operator_token = str(
        app_keys.create(
            app="operator",
            access=[ApplicationAccess(EVENTS_READ_ALL)],
            grantor=grantor,
        )["token"]
    )
    events.emit(
        owner_app="alpha",
        type="collection.tags_changed",
        subject="1",
        data=_tag_event_data(1, "alpha"),
    )
    events.emit(
        owner_app="beta",
        type="collection.tags_changed",
        subject="2",
        data=_tag_event_data(2, "beta"),
    )
    container = SimpleNamespace(app_keys=app_keys, lifecycle_events=events)
    api = FastAPI()
    api.dependency_overrides[get_container] = lambda: container
    api.include_router(events_router, prefix="/v1")

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            alpha = await client.get(
                "/v1/events",
                headers={"Authorization": f"Bearer {alpha_token}"},
            )
            assert alpha.status_code == 200
            assert [event["subject"] for event in alpha.json()["events"]] == ["1"]

            operator = await client.get(
                "/v1/events",
                headers={"Authorization": f"Bearer {operator_token}"},
            )
            assert operator.status_code == 200
            assert [event["subject"] for event in operator.json()["events"]] == [
                "1",
                "2",
            ]

    anyio.run(exercise)
