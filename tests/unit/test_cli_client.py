from __future__ import annotations

from typing import Any

import httpx

from riverhog_cli.client import ApiClient


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
