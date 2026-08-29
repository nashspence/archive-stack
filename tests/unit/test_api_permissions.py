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
    ALL_RESOURCES,
    APPLICATION_PERMISSIONS,
    CATALOG_READ,
    COLLECTION_TRANSFORMS_CONTROL,
    COLLECTION_TRANSFORMS_EXECUTE,
    COLLECTIONS_CREATE,
    PROVENANCE_EXPORT,
    PROVENANCE_READ,
    ApplicationAccess,
    ApplicationPrincipal,
)


def _permission_dependencies(dependant: Dependant) -> Iterable[tuple[str, ...]]:
    permission = getattr(dependant.call, "riverhog_permission", None)
    if isinstance(permission, str):
        yield (permission,)
    permissions = getattr(dependant.call, "riverhog_permissions", None)
    if isinstance(permissions, tuple) and all(isinstance(item, str) for item in permissions):
        yield permissions
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
        requirement = permissions[0]
        assert requirement
        assert set(requirement) <= APPLICATION_PERMISSIONS
        for method in route.methods:
            protected.append((method, path, "|".join(requirement)))

    assert protected
    assert len(protected) == len(set(protected))
    assert {
        (method, path): permission
        for method, path, permission in protected
        if "/provenance/" in path
    } == {
        (
            "PUT",
            "/v1/collection-upload-sessions/{collection_id}/provenance/journals/{journal_id}",
        ): COLLECTIONS_CREATE,
        (
            "GET",
            "/v1/collection-upload-sessions/{collection_id}/provenance/journals/{journal_id}",
        ): COLLECTIONS_CREATE,
        (
            "GET",
            "/v1/collections/{collection_id}/provenance/files",
        ): PROVENANCE_READ,
        (
            "GET",
            "/v1/collections/{collection_id}/provenance/files/stream",
        ): PROVENANCE_READ,
        (
            "GET",
            "/v1/collections/{collection_id}/provenance/files/{path:path}",
        ): PROVENANCE_READ,
        (
            "GET",
            "/v1/collections/{collection_id}/provenance/trace/{path:path}",
        ): PROVENANCE_READ,
        (
            "GET",
            "/v1/collections/{collection_id}/provenance/trace/{path:path}/stream",
        ): PROVENANCE_READ,
        (
            "GET",
            "/v1/collections/{collection_id}/provenance/journals/{journal_id}",
        ): PROVENANCE_EXPORT,
        (
            "POST",
            "/v1/collections/{collection_id}/provenance/verify",
        ): PROVENANCE_READ,
    }

    workflow_permissions = {
        (method, path): permission
        for method, path, permission in protected
        if "collection-processing-claims" in path or path.endswith("/derivation")
    }
    assert (
        workflow_permissions[("POST", "/v1/collection-processing-claims/{claim_id}/capabilities")]
        == COLLECTION_TRANSFORMS_EXECUTE
    )
    assert (
        workflow_permissions[("POST", "/v1/collection-processing-claims/{claim_id}/plan")]
        == COLLECTION_TRANSFORMS_EXECUTE
    )
    assert (
        workflow_permissions[("POST", "/v1/collection-processing-claims/{claim_id}/settle")]
        == COLLECTION_TRANSFORMS_CONTROL
    )
    assert (
        workflow_permissions[("POST", "/v1/collection-processing-claims/{claim_id}/retirement")]
        == COLLECTION_TRANSFORMS_CONTROL
    )
    assert (
        workflow_permissions[("POST", "/v1/collection-processing-claims/{claim_id}/renew")]
        == f"{COLLECTION_TRANSFORMS_CONTROL}|{COLLECTION_TRANSFORMS_EXECUTE}"
    )


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


def test_collection_upload_unit_accepts_the_documented_binary_body() -> None:
    principal = ApplicationPrincipal(
        app="uploader",
        key_id="uploader-key",
        access=frozenset({ApplicationAccess(COLLECTIONS_CREATE, ALL_RESOURCES)}),
    )

    class Keys:
        def authenticate(self, token: str) -> ApplicationPrincipal | None:
            return principal if token == "uploader-token" else None

    class Uploads:
        content: bytes | None = None

        def require_access(self, collection_id: int, current: ApplicationPrincipal) -> None:
            assert collection_id == 42
            assert current == principal

        def get_unit(self, collection_id: int, volume_id: str, unit: int) -> dict[str, object]:
            assert (collection_id, volume_id, unit) == (42, "pack-000000000000", 0)
            return {
                "unit": 0,
                "payload_bytes": 3,
                "plaintext_bytes": 3,
                "sources": [
                    {
                        "path": "camera/clip.bin",
                        "offset": 0,
                        "bytes": 3,
                        "artifact_sha256": "b" * 64,
                    }
                ],
                "state": "pending",
            }

        def upload_unit(
            self,
            collection_id: int,
            volume_id: str,
            unit: int,
            *,
            plan_sha256: str,
            content: bytes,
        ) -> dict[str, object]:
            assert (collection_id, volume_id, unit) == (42, "pack-000000000000", 0)
            assert plan_sha256 == "a" * 64
            self.content = content
            return {
                "unit": 0,
                "payload_bytes": 3,
                "plaintext_bytes": 3,
                "sources": [
                    {
                        "path": "camera/clip.bin",
                        "offset": 0,
                        "bytes": 3,
                        "artifact_sha256": "b" * 64,
                    }
                ],
                "state": "committed",
            }

    uploads = Uploads()
    app = create_app(
        container=cast(
            Any,
            SimpleNamespace(
                app_keys=Keys(),
                collection_uploads=uploads,
            ),
        )
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.put(
                "/v1/collection-upload-sessions/42/volumes/pack-000000000000/units/0",
                headers={
                    "Authorization": "Bearer uploader-token",
                    "Content-Type": "application/octet-stream",
                    "If-Match": '"' + "a" * 64 + '"',
                },
                content=b"\x00\xff\x80",
            )

            assert response.status_code == 200, response.text
            assert response.json()["state"] == "committed"
            assert uploads.content == b"\x00\xff\x80"

            for invalid_if_match in (
                "a" * 64,
                '"' + "A" * 64 + '"',
                'W/"' + "a" * 64 + '"',
                ' "' + "a" * 64 + '"',
            ):
                rejected = await client.put(
                    "/v1/collection-upload-sessions/42/volumes/pack-000000000000/units/0",
                    headers={
                        "Authorization": "Bearer uploader-token",
                        "Content-Type": "application/octet-stream",
                        "If-Match": invalid_if_match,
                    },
                    content=b"\x00\xff\x80",
                )
                assert rejected.status_code == 400

            negative = await client.put(
                "/v1/collection-upload-sessions/42/volumes/pack-000000000000/units/-1",
                headers={
                    "Authorization": "Bearer uploader-token",
                    "Content-Type": "application/octet-stream",
                    "If-Match": '"' + "a" * 64 + '"',
                },
                content=b"\x00\xff\x80",
            )
            assert negative.status_code == 400

    anyio.run(exercise)
