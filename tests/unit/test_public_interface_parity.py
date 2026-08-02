from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from jeb_api.app import create_app as create_jeb_app
from jeb_api_client import JebApiClient
from munchy_api.app import app as munchy_app
from munchy_api_client.client import MunchyAdminClient, MunchyClient
from riverhog_api.app import create_app as create_riverhog_app
from riverhog_api_client.client import ApiClient

HTTP_METHODS = {"delete", "get", "patch", "post", "put"}


def public_operations(app: FastAPI) -> list[tuple[str, str]]:
    operations: list[tuple[str, str]] = []
    for path, path_item in app.openapi()["paths"].items():
        if not path.startswith("/v1"):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            operations.append((str(operation["operationId"]), f"{method.upper()} {path}"))
    return operations


@pytest.mark.parametrize(
    ("application", "app_factory", "client_types"),
    (
        ("riverhog", create_riverhog_app, (ApiClient,)),
        ("munchy", lambda: munchy_app, (MunchyClient, MunchyAdminClient)),
        ("jeb", create_jeb_app, (JebApiClient,)),
    ),
)
def test_every_public_api_operation_has_an_official_client_method(
    application: str,
    app_factory: Callable[[], FastAPI],
    client_types: tuple[type[Any], ...],
) -> None:
    operations = public_operations(app_factory())
    operation_ids = [operation_id for operation_id, _route in operations]
    uncovered = {
        operation_id: route
        for operation_id, route in operations
        if not any(
            callable(getattr(client_type, operation_id, None)) for client_type in client_types
        )
    }

    assert len(operation_ids) == len(set(operation_ids)), (
        f"{application} OpenAPI operation IDs must be unique: {operation_ids}"
    )
    assert uncovered == {}, f"{application} OpenAPI operations missing from its client: {uncovered}"
