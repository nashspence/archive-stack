from __future__ import annotations

from types import SimpleNamespace
from xml.etree import ElementTree

import httpx
from riverhog_api.routers.resourcesync import (
    get_portable_collection_manifest,
    resourcesync_change_list,
    resourcesync_resource_list,
    resourcesync_resource_list_page,
)
from riverhog_api_client.client import ApiClient
from starlette.requests import Request

COLLECTION_ID = 42
ETAG = "a" * 64


class RetrievalStub:
    def resource_list_pages(self, *, per_page: int, principal: object) -> int:
        assert principal == "app"
        assert per_page == 10_000
        return 1

    def resource_list_page(self, *, page: int, per_page: int, principal: object):
        assert principal == "app"
        assert page == 1
        return {
            "page": page,
            "per_page": per_page,
            "total": 1,
            "pages": 1,
            "resources": [{"collection_id": COLLECTION_ID, "etag": ETAG}],
        }

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
                "format": "riverhog-collection/v1",
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
    resource_page = resourcesync_resource_list_page(
        1,
        _request("/resourcesync/resourcelist/1.xml"),
        "app",
        container,
    )
    changes = resourcesync_change_list(
        _request("/resourcesync/changelist.xml"),
        "app",
        container,
        after=2,
    )

    resource_root = ElementTree.fromstring(resources.body)
    resource_page_root = ElementTree.fromstring(resource_page.body)
    change_root = ElementTree.fromstring(changes.body)
    assert str(COLLECTION_ID) in resource_page.body.decode()
    assert f"sha-256:{ETAG}" in resource_page.body.decode()
    assert resource_root.tag.endswith("sitemapindex")
    assert change_root.attrib["data-cursor"] == "3"
    assert change_root.attrib["data-has-more"] == "false"
    assert len([item for item in resource_page_root if item.tag.endswith("url")]) == 1
    assert len(change_root) == 1


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
    response = get_portable_collection_manifest(
        COLLECTION_ID,
        "app",
        SimpleNamespace(retrieval=RetrievalStub()),
    )

    assert response.headers["etag"] == f'"{ETAG}"'
    assert b"riverhog-collection/v1" in response.body
