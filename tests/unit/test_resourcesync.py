from __future__ import annotations

from types import SimpleNamespace
from xml.etree import ElementTree

import httpx
from riverhog_api.routers.resourcesync import (
    collection_portable_manifest,
    resourcesync_change_list,
    resourcesync_resource_list,
)
from riverhog_api_client.client import ApiClient
from starlette.requests import Request

COLLECTION_ID = 42
ETAG = "a" * 64


class RetrievalStub:
    def resource_list(self, *, principal: object):
        assert principal == "app"
        return [{"collection_id": COLLECTION_ID, "etag": ETAG}]

    def change_list(self, *, after: int, principal: object):
        assert after == 2
        assert principal == "app"
        return {
            "cursor": 3,
            "has_more": False,
            "changes": [
                {
                    "change": "created",
                    "collection_id": COLLECTION_ID,
                    "occurred_at": "2026-07-18T00:00:00.000000Z",
                    "etag": ETAG,
                }
            ],
        }

    def collection_manifest(self, collection_id: int, *, principal: object):
        assert collection_id == COLLECTION_ID
        assert principal == "app"
        return (
            {
                "format": "riverhog-collection/v2",
                "collection": collection_id,
                "files": [],
            },
            ETAG,
        )


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("riverhog.example.test", 443),
            "path": path,
            "root_path": "",
            "query_string": b"",
            "headers": [],
        }
    )


def test_resourcesync_lists_portable_manifests_and_incremental_changes() -> None:
    container = SimpleNamespace(retrieval=RetrievalStub())
    resources = resourcesync_resource_list(
        _request("/resourcesync/resourcelist.xml"), "app", container
    )
    changes = resourcesync_change_list(
        _request("/resourcesync/changelist.xml"),
        "app",
        container,
        after=2,
    )

    resource_root = ElementTree.fromstring(resources.body)
    change_root = ElementTree.fromstring(changes.body)
    assert str(COLLECTION_ID) in resources.body.decode()
    assert f"sha-256:{ETAG}" in resources.body.decode()
    assert change_root.attrib["data-cursor"] == "3"
    assert change_root.attrib["data-has-more"] == "false"
    assert len(resource_root) == len(change_root) == 1


def test_cli_parses_resourcesync_cursor_and_collection_change() -> None:
    response = resourcesync_change_list(
        _request("/resourcesync/changelist.xml"),
        "app",
        SimpleNamespace(retrieval=RetrievalStub()),
        after=2,
    )

    class Client(ApiClient):
        def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
            assert (method, path, kwargs) == (
                "GET",
                "/resourcesync/changelist.xml",
                {"params": {"after": 2}},
            )
            return httpx.Response(
                200,
                content=response.body,
                request=httpx.Request(method, path),
            )

    assert Client(base_url="https://riverhog.example.test").catalog_changes(after=2) == {
        "cursor": 3,
        "has_more": False,
        "changes": [
            {
                "collection_id": COLLECTION_ID,
                "change": "created",
                "datetime": "2026-07-18T00:00:00.000000Z",
                "etag": ETAG,
            }
        ],
    }


def test_portable_manifest_response_has_a_content_etag() -> None:
    response = collection_portable_manifest(
        COLLECTION_ID,
        "app",
        SimpleNamespace(retrieval=RetrievalStub()),
    )

    assert response.headers["etag"] == f'"{ETAG}"'
    assert b"riverhog-collection/v2" in response.body
