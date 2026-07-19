from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from riverhog_api.auth import CatalogReader
from riverhog_api.deps import get_container
from riverhog_api.routers.apps import router as apps_router
from riverhog_core.app_permissions import CATALOG_READ, COLLECTIONS_UPLOAD, KEYS_MANAGE
from riverhog_core.catalog_db import initialize_db
from riverhog_core.domain.errors import Forbidden
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from tests.unit.db_helpers import sqlite_url


def test_bootstrap_and_application_keys_enforce_permissions_immediately(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_BOOTSTRAP_TOKEN", "bootstrap-token")
    config = RuntimeConfig(database_url=sqlite_url(tmp_path / "catalog.sqlite3"))
    initialize_db(config.database_url)
    service = SqlAlchemyAppKeyService(config)
    container = SimpleNamespace(app_keys=service)
    api = FastAPI()
    api.dependency_overrides[get_container] = lambda: container

    @api.exception_handler(Forbidden)
    async def forbidden_handler(_request: object, exc: Forbidden) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": exc.message})

    api.include_router(apps_router, prefix="/v1")

    @api.get("/catalog")
    def catalog(principal: CatalogReader) -> dict[str, str]:
        return {"app": principal.app}

    async def exercise() -> None:
        bootstrap_headers = {"Authorization": "Bearer bootstrap-token"}
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            assert (
                await client.post(
                    "/v1/apps/manager/keys",
                    json={"permissions": [KEYS_MANAGE, CATALOG_READ]},
                )
            ).status_code == 401
            created_response = await client.post(
                "/v1/apps/manager/keys",
                json={"permissions": [KEYS_MANAGE, CATALOG_READ]},
                headers=bootstrap_headers,
            )
            assert created_response.status_code == 200
            created = created_response.json()
            manager_token = created.pop("token")
            manager_headers = {"Authorization": f"Bearer {manager_token}"}

            assert (await client.get("/catalog", headers=manager_headers)).json() == {
                "app": "manager"
            }
            delegated = await client.post(
                "/v1/apps/reader/keys",
                json={"permissions": [CATALOG_READ]},
                headers=manager_headers,
            )
            assert delegated.status_code == 200
            assert delegated.json()["permissions"] == [CATALOG_READ]
            forbidden = await client.post(
                "/v1/apps/uploader/keys",
                json={"permissions": [COLLECTIONS_UPLOAD]},
                headers=manager_headers,
            )
            assert forbidden.status_code == 403

            listed = (
                await client.get(
                    "/v1/apps/manager/keys?all=true",
                    headers=manager_headers,
                )
            ).json()
            listed_key = listed["keys"][0]
            assert {key: listed_key[key] for key in created if key != "last_used_at"} == {
                key: created[key] for key in created if key != "last_used_at"
            }
            assert listed_key["last_used_at"] is not None
            revoked = await client.post(
                f"/v1/apps/manager/keys/{created['id']}/revoke",
                headers=bootstrap_headers,
            )
            assert revoked.status_code == 200
            assert revoked.json()["status"] == "revoked"
            assert (await client.get("/catalog", headers=manager_headers)).status_code == 401

    anyio.run(exercise)
