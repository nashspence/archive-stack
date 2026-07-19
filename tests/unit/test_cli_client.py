from __future__ import annotations

from typing import Any

import httpx
from riverhog_api_client.client import ApiClient


class RecordingClient(ApiClient):
    def __init__(self) -> None:
        super().__init__(base_url="http://example.invalid")
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, path, kwargs))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, path))


def test_search_uses_current_collection_filters() -> None:
    client = RecordingClient()
    client.search(
        "tax",
        collection="2025/20250102T030405Z__docs",
        sort="collection_path",
        order="desc",
        all_items=True,
    )

    assert client.calls == [
        (
            "GET",
            "/v1/search",
            {
                "params": {
                    "page": 1,
                    "per_page": 25,
                    "sort": "collection_path",
                    "order": "desc",
                    "q": "tax",
                    "collection": "2025/20250102T030405Z__docs",
                    "all": True,
                }
            },
        )
    ]


def test_collection_upload_selects_archive_store_without_materialization_policy() -> None:
    client = RecordingClient()

    client.create_or_resume_collection_upload("docs", [], archive_store="b2")
    client.create_or_resume_collection_upload_session("docs", archive_store="b2")

    assert client.calls[0][2]["json"] == {
        "slug": "docs",
        "files": [],
        "archive_store": "b2",
    }
    assert client.calls[1][2]["json"] == {"slug": "docs", "archive_store": "b2"}


def test_retrieval_plan_and_job_share_exact_file_selection() -> None:
    client = RecordingClient()
    files = [("2025/20250102T030405Z__docs", "invoice.pdf")]

    client.plan_retrieval(files, lease_seconds=3600)
    client.create_retrieval_job(files, plan_etag="a" * 64, lease_seconds=3600)

    payload = {
        "files": [
            {
                "collection_id": "2025/20250102T030405Z__docs",
                "path": "invoice.pdf",
            }
        ],
        "lease_seconds": 3600,
    }
    assert client.calls == [
        ("POST", "/v1/retrieval-plans", {"json": payload}),
        (
            "POST",
            "/v1/retrieval-jobs",
            {"json": payload, "headers": {"If-Match": '"' + "a" * 64 + '"'}},
        ),
    ]


def test_one_application_token_reaches_the_complete_client_surface(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_TOKEN", "application-token")

    client = ApiClient()
    assert client.token == "application-token"
    assert callable(client.plan_retrieval)
    assert callable(client.create_or_resume_collection_upload)
    assert callable(client.create_app_key)


def test_client_manages_application_keys_with_explicit_permissions() -> None:
    client = RecordingClient()

    client.list_apps(q="local", active=True, all_items=True)
    client.create_app_key(
        "local",
        permissions=["catalog:read", "retrieval:manage"],
        expires_in_seconds=3600,
    )
    client.list_app_keys("local", active=False, all_items=True)
    client.revoke_app_key("local", "0123456789abcdef")

    assert client.calls == [
        (
            "GET",
            "/v1/apps",
            {
                "params": {
                    "page": 1,
                    "per_page": 25,
                    "sort": "name",
                    "order": "asc",
                    "q": "local",
                    "active": "true",
                    "all": True,
                }
            },
        ),
        (
            "POST",
            "/v1/apps/local/keys",
            {
                "json": {
                    "permissions": ["catalog:read", "retrieval:manage"],
                    "expires_in_seconds": 3600,
                }
            },
        ),
        (
            "GET",
            "/v1/apps/local/keys",
            {
                "params": {
                    "page": 1,
                    "per_page": 25,
                    "sort": "created_at",
                    "order": "desc",
                    "active": "false",
                    "all": True,
                }
            },
        ),
        (
            "POST",
            "/v1/apps/local/keys/0123456789abcdef/revoke",
            {},
        ),
    ]


def test_archive_upload_transport_defaults_to_http_1_1(monkeypatch) -> None:
    monkeypatch.delenv("RIVERHOG_UPLOAD_HTTP2", raising=False)
    monkeypatch.delenv("RIVERHOG_HTTP2", raising=False)

    client = ApiClient(base_url="https://example.invalid")

    assert client.http2 is True
    assert client.upload_http2 is False
