from __future__ import annotations

from types import SimpleNamespace
from xml.etree import ElementTree

import httpx
from riverhog_api.app import create_app
from riverhog_api.routers.resourcesync import (
    get_portable_collection_manifest,
    resourcesync_capability_list,
    resourcesync_change_list,
    resourcesync_resource_list,
    resourcesync_resource_list_page,
    well_known_resourcesync,
)
from riverhog_api_client.client import ApiClient
from riverhog_protocol import PortableCollectionRecord, portable_collection_json_schema
from starlette.requests import Request
from starlette.responses import Response

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
            PortableCollectionRecord.create(
                collection=collection_id,
                content_identity="b" * 64,
                encryption_format="age-v1-scrypt",
                passphrase_id="collection-test-key-v1",
                provenance_mode="omitted",
                provenance_identity=None,
                metadata_revision=0,
                tags=(),
                files=(("empty.txt", 0, "c" * 64),),
            ),
            ETAG,
        )


def _request(path: str, *, public_base_url: str | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "https",
        "server": ("riverhog.example.test", 443),
        "path": path,
        "root_path": "",
        "query_string": b"",
        "headers": [],
    }
    if public_base_url is not None:
        scope["app"] = SimpleNamespace(
            state=SimpleNamespace(public_base_url=public_base_url),
        )
    return Request(scope)


def test_resourcesync_uses_the_configured_public_url_authority() -> None:
    response = well_known_resourcesync(
        _request(
            "/.well-known/resourcesync",
            public_base_url="https://public.example.test/riverhog",
        ),
        "app",
    )

    assert b"https://public.example.test/riverhog/resourcesync/capabilitylist.xml" in response.body


def test_portable_manifest_openapi_uses_the_shipped_structural_projection() -> None:
    response_schema = create_app().openapi()["paths"][
        "/v1/catalog/collections/{collection_id}/manifest"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]

    assert response_schema == portable_collection_json_schema()


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


def test_api_client_parses_resourcesync_discovery_capabilities_and_resources() -> None:
    container = SimpleNamespace(retrieval=RetrievalStub())
    responses = {
        "/.well-known/resourcesync": well_known_resourcesync(
            _request("/.well-known/resourcesync"), "app"
        ),
        "/resourcesync/capabilitylist.xml": resourcesync_capability_list(
            _request("/resourcesync/capabilitylist.xml"), "app"
        ),
        "/resourcesync/resourcelist.xml": resourcesync_resource_list(
            _request("/resourcesync/resourcelist.xml"), "app", container
        ),
        "/resourcesync/resourcelist/1.xml": resourcesync_resource_list_page(
            1,
            _request("/resourcesync/resourcelist/1.xml"),
            "app",
            container,
        ),
    }

    class Client(ApiClient):
        def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
            assert method == "GET"
            assert kwargs == {}
            response = responses[path]
            return httpx.Response(
                200,
                content=response.body,
                request=httpx.Request(method, path),
            )

    client = Client(base_url="https://riverhog.example.test")

    assert client.resourcesync_discovery()["capabilities"] == [
        {
            "capability": "capabilitylist",
            "location": "https://riverhog.example.test/resourcesync/capabilitylist.xml",
        }
    ]
    assert {item["capability"] for item in client.resourcesync_capabilities()["capabilities"]} == {
        "resourcelist",
        "changelist",
    }
    assert client.resourcesync_resource_pages()["pages"] == [
        "https://riverhog.example.test/resourcesync/resourcelist/1.xml"
    ]
    assert client.resourcesync_resources(page=1)["resources"] == [
        {
            "collection_id": COLLECTION_ID,
            "etag": ETAG,
            "location": ("https://riverhog.example.test/v1/catalog/collections/42/manifest"),
        }
    ]


def test_portable_manifest_response_has_a_content_identity() -> None:
    response = Response()
    record = get_portable_collection_manifest(
        COLLECTION_ID,
        "app",
        SimpleNamespace(retrieval=RetrievalStub()),
        response,
    )

    assert response.headers["etag"] == f'"{ETAG}"'
    assert record.format == "riverhog-collection/v1"
