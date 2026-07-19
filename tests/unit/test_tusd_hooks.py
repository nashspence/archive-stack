from __future__ import annotations

import base64
import json
from asyncio import run
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from starlette.requests import Request

from riverhog_api.routers.internal import authorize_tusd_data_plane, handle_tusd_hook
from riverhog_core.app_permissions import (
    CATALOG_READ,
    COLLECTIONS_UPLOAD,
    ApplicationPrincipal,
)
from riverhog_core.stores.tusd_upload_store import TusdUploadStore
from riverhog_core.tusd_ids import tusd_upload_id_for_target_path


def _request(
    *,
    method: str,
    headers: dict[str, str],
    payload: dict[str, object] | None = None,
) -> Request:
    body = json.dumps(payload).encode() if payload is not None else b""
    delivered = False

    async def receive() -> dict[str, object]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/internal/tusd/hooks",
            "headers": [
                (name.casefold().encode(), value.encode()) for name, value in headers.items()
            ],
        },
        receive,
    )


def _hook_response(
    payload: dict[str, object],
    *,
    headers: dict[str, str],
) -> tuple[int, dict[str, object]]:
    response = run(handle_tusd_hook(_request(method="POST", headers=headers, payload=payload)))
    return response.status_code, json.loads(response.body)


def _hook_payload(kind: str, upload_id: str) -> dict[str, object]:
    return {
        "Type": kind,
        "Event": {"Upload": {"MetaData": {"upload_id": upload_id}}},
    }


def _container_with_permissions(*permissions: str) -> SimpleNamespace:
    principal = ApplicationPrincipal(
        app="uploader",
        key_id="key",
        permissions=frozenset(permissions),
    )

    class Keys:
        def authenticate(self, token: str) -> ApplicationPrincipal | None:
            return principal if token == "application-token" else None

    return SimpleNamespace(app_keys=Keys())


def test_precreate_hook_assigns_the_hook_authenticated_opaque_upload_id(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_TUSD_HOOK_SECRET", "hook-secret")
    upload_id = ".riverhog/uploads/by-target/" + "a" * 64

    status, payload = _hook_response(
        _hook_payload("pre-create", upload_id),
        headers={"X-Riverhog-Tusd-Hook-Secret": "hook-secret"},
    )

    assert status == 200
    assert payload == {"ChangeFileInfo": {"ID": upload_id}}


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Riverhog-Tusd-Hook-Secret": "hook-secret"},
        {"Authorization": "Bearer application-token"},
    ],
)
def test_post_finish_hook_syncs_the_catalog_file_by_opaque_id(
    monkeypatch, headers: dict[str, str]
) -> None:
    monkeypatch.setenv("RIVERHOG_TUSD_HOOK_SECRET", "hook-secret")
    upload_id = ".riverhog/uploads/by-target/" + "b" * 64
    calls: list[str] = []

    class Collections:
        def sync_finished_upload_id(self, value: str) -> None:
            calls.append(value)

    container = _container_with_permissions(COLLECTIONS_UPLOAD)
    container.collections = Collections()
    monkeypatch.setattr("riverhog_api.routers.internal.default_container", lambda: container)

    async def directly(function, *args):
        return function(*args)

    monkeypatch.setattr("riverhog_api.routers.internal.run_in_threadpool", directly)

    status, payload = _hook_response(
        _hook_payload("post-finish", upload_id),
        headers=headers,
    )

    assert status == 200
    assert payload == {}
    assert calls == [upload_id]


def test_tusd_metadata_contains_only_the_opaque_upload_id() -> None:
    target_path = ".riverhog/uploads/collections/2025/demo/private name.jpg"
    header = TusdUploadStore._metadata_header(object(), target_path)
    name, encoded = header.split(" ", 1)

    assert name == "upload_id"
    assert base64.b64decode(encoded).decode("ascii") == tusd_upload_id_for_target_path(target_path)


def test_tusd_data_plane_auth_requires_the_upload_permission() -> None:
    upload_id = ".riverhog/uploads/by-target/" + "c" * 64
    request = _request(
        method="GET",
        headers={
            "Authorization": "Bearer application-token",
            "X-Original-Method": "PATCH",
            "X-Original-Uri": f"/files/{quote(upload_id, safe='')}",
        },
    )

    response = authorize_tusd_data_plane(
        request,
        _container_with_permissions(COLLECTIONS_UPLOAD),
    )
    assert response.status_code == 204

    forbidden = authorize_tusd_data_plane(
        request,
        _container_with_permissions(CATALOG_READ),
    )
    assert forbidden.status_code == 403
