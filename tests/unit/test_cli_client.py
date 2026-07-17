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


def test_search_uses_current_filters() -> None:
    client = RecordingClient()
    client.search(
        "tax",
        collection="2025/20250102T030405Z__docs",
        hot=False,
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
                    "hot": False,
                    "all": True,
                }
            },
        )
    ]


def test_fetch_file_list_can_request_every_matching_selector() -> None:
    client = RecordingClient()
    client.list_fetch_files(
        1,
        query="invoice",
        hot=True,
        all_items=True,
    )

    assert client.calls == [
        (
            "GET",
            "/v1/fetches/1/files",
            {
                "params": {
                    "page": 1,
                    "per_page": 25,
                    "sort": "logical_path",
                    "order": "asc",
                    "q": "invoice",
                    "hot": True,
                    "all": True,
                }
            },
        )
    ]


def test_fetch_start_is_a_single_archive_aware_action() -> None:
    client = RecordingClient()
    client.start_fetch(1)

    assert client.calls == [("POST", "/v1/fetches/1/start", {})]


def test_fetch_delete_confirms_the_exact_resource() -> None:
    client = RecordingClient()
    client.delete_fetch(1, confirmation=1)

    assert client.calls == [
        (
            "DELETE",
            "/v1/fetches/1",
            {"json": {"confirmation": 1}},
        )
    ]


def test_archive_restore_list_filters_collection_and_state() -> None:
    client = RecordingClient()
    client.list_archive_restores(state="requested", collection="2025/20250102T030405Z__docs")

    _, path, kwargs = client.calls[0]
    assert path == "/v1/archive-restores"
    assert kwargs["params"]["state"] == "requested"
    assert kwargs["params"]["collection"] == "2025/20250102T030405Z__docs"


def test_collection_upload_requests_retain_hot_storage_by_default() -> None:
    client = RecordingClient()

    client.create_or_resume_collection_upload("docs", [])
    client.create_or_resume_collection_upload_session("docs")

    assert client.calls[0][2]["json"]["retain_hot"] is True
    assert client.calls[1][2]["json"]["retain_hot"] is True
