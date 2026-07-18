from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
from fastapi import FastAPI

from riverhog_api.auth import api_auth_dependencies
from riverhog_api.deps import get_container
from riverhog_api.external_auth import ExternalApp
from riverhog_api.routers.apps import router as apps_router
from riverhog_core.catalog_db import initialize_db
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from tests.unit.db_helpers import sqlite_url


def test_operator_manages_keys_and_external_auth_changes_immediately(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_API_TOKEN", "operator-token")
    config = RuntimeConfig(database_url=sqlite_url(tmp_path / "catalog.sqlite3"))
    initialize_db(config.database_url)
    service = SqlAlchemyAppKeyService(config)
    container = SimpleNamespace(app_keys=service)
    api = FastAPI()
    api.dependency_overrides[get_container] = lambda: container
    api.include_router(
        apps_router,
        prefix="/v1",
        dependencies=list(api_auth_dependencies()),
    )

    @api.get("/external")
    def external(app: ExternalApp) -> dict[str, str]:
        return {"app": app}

    async def exercise() -> None:
        operator_headers = {"Authorization": "Bearer operator-token"}
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            assert (await client.post("/v1/apps/local/keys", json={})).status_code == 401
            created_response = await client.post(
                "/v1/apps/local/keys",
                json={},
                headers=operator_headers,
            )
            assert created_response.status_code == 200
            created = created_response.json()
            token = created.pop("token")

            listed = (
                await client.get(
                    "/v1/apps/local/keys?all=true",
                    headers=operator_headers,
                )
            ).json()
            assert listed["keys"] == [created]
            assert (await client.get("/external")).status_code == 401
            assert (
                await client.get(
                    "/external",
                    headers={"Authorization": f"Bearer {token}"},
                )
            ).json() == {"app": "local"}

            revoked = await client.post(
                f"/v1/apps/local/keys/{created['id']}/revoke",
                headers=operator_headers,
            )
            assert revoked.status_code == 200
            assert revoked.json()["status"] == "revoked"
            assert (
                await client.get(
                    "/external",
                    headers={"Authorization": f"Bearer {token}"},
                )
            ).status_code == 401

    anyio.run(exercise)
