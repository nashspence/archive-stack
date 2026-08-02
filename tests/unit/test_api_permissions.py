from __future__ import annotations

from collections.abc import Iterable, Iterator
from types import SimpleNamespace
from typing import Any, cast

import anyio
import httpx
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from riverhog_api.app import create_app
from riverhog_core.app_permissions import (
    APPLICATION_PERMISSIONS,
    CATALOG_READ,
    ApplicationAccess,
    ApplicationPrincipal,
)


def _permission_dependencies(dependant: Dependant) -> Iterable[str]:
    permission = getattr(dependant.call, "riverhog_permission", None)
    if isinstance(permission, str):
        yield permission
    for child in dependant.dependencies:
        yield from _permission_dependencies(child)


def _api_routes(routes: Iterable[object], *, prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        original_router = getattr(route, "original_router", None)
        include_context = getattr(route, "include_context", None)
        if original_router is None or include_context is None:
            continue
        yield from _api_routes(
            original_router.routes,
            prefix=prefix + str(include_context.prefix),
        )


def test_every_public_riverhog_operation_declares_one_known_permission() -> None:
    app = create_app(container=cast(Any, object()))
    protected: list[tuple[str, str, str]] = []
    for path, route in _api_routes(app.routes):
        if path.startswith("/health/") or path.startswith("/internal/"):
            continue
        permissions = list(_permission_dependencies(route.dependant))
        assert len(permissions) == 1, (path, route.methods, permissions)
        permission = permissions[0]
        assert permission in APPLICATION_PERMISSIONS
        for method in route.methods:
            protected.append((method, path, permission))

    assert protected
    assert len(protected) == len(set(protected))


def test_application_authentication_uses_the_public_error_contract() -> None:
    principal = ApplicationPrincipal(
        app="reader",
        key_id="reader-key",
        access=frozenset({ApplicationAccess(CATALOG_READ)}),
    )

    class Keys:
        def authenticate(self, token: str) -> ApplicationPrincipal | None:
            return principal if token == "reader-token" else None

    app = create_app(container=cast(Any, SimpleNamespace(app_keys=Keys())))

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            unauthorized = await client.get("/v1/collections")
            assert unauthorized.status_code == 401
            assert unauthorized.headers["WWW-Authenticate"] == "Bearer"
            assert unauthorized.json() == {
                "error": {
                    "code": "unauthorized",
                    "message": "invalid application token",
                }
            }

            forbidden = await client.post(
                "/v1/collection-upload-sessions",
                headers={"Authorization": "Bearer reader-token"},
                json={"idempotency_key": "example", "tags": ["example"]},
            )
            assert forbidden.status_code == 403
            assert forbidden.json() == {
                "error": {
                    "code": "forbidden",
                    "message": "application permission required: collections:create",
                }
            }

    anyio.run(exercise)
