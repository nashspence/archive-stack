from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
from fastapi import FastAPI

from riverhog_api.deps import get_container
from riverhog_api.routers.events import router as events_router
from riverhog_core.app_permissions import (
    EVENTS_READ,
    EVENTS_READ_ALL,
    ApplicationPrincipal,
)
from riverhog_core.catalog_db import initialize_db
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService
from tests.unit.db_helpers import sqlite_url


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
        permissions=frozenset(),
        unrestricted_delegation=True,
    )
    alpha_token = str(
        app_keys.create(app="alpha", permissions=[EVENTS_READ], grantor=grantor)["token"]
    )
    operator_token = str(
        app_keys.create(
            app="operator",
            permissions=[EVENTS_READ_ALL],
            grantor=grantor,
        )["token"]
    )
    events.emit(owner_app="alpha", type="collection.finalized", subject="alpha-collection")
    events.emit(owner_app="beta", type="collection.finalized", subject="beta-collection")
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
            assert [event["subject"] for event in alpha.json()["events"]] == [
                "alpha-collection"
            ]

            operator = await client.get(
                "/v1/events",
                headers={"Authorization": f"Bearer {operator_token}"},
            )
            assert operator.status_code == 200
            assert [event["subject"] for event in operator.json()["events"]] == [
                "alpha-collection",
                "beta-collection",
            ]

    anyio.run(exercise)
