from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, cast

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from riverhog_api.app import create_app
from riverhog_core.app_permissions import APPLICATION_PERMISSIONS


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
        if path == "/healthz" or path.startswith("/internal/"):
            continue
        permissions = list(_permission_dependencies(route.dependant))
        assert len(permissions) == 1, (path, route.methods, permissions)
        permission = permissions[0]
        assert permission in APPLICATION_PERMISSIONS
        for method in route.methods:
            protected.append((method, path, permission))

    assert protected
    assert len(protected) == len(set(protected))
