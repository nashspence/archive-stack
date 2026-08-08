from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from riverhog_api_client.client import ApiClient
from riverhog_protocol.errors import DownloadAllowanceExceeded, Forbidden, Unauthorized


class RecordingClient(ApiClient):
    def __init__(self) -> None:
        super().__init__(base_url="http://example.invalid")
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, path, kwargs))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, path))


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        ("unauthorized", Unauthorized),
        ("forbidden", Forbidden),
        ("download_allowance_exceeded", DownloadAllowanceExceeded),
    ],
)
def test_client_preserves_actionable_api_error_types(
    code: str,
    error_type: type[Exception],
) -> None:
    client = ApiClient(base_url="http://example.invalid")
    response = httpx.Response(
        400,
        json={"error": {"code": code, "message": "action denied"}},
        request=httpx.Request("GET", "http://example.invalid/v1/test"),
    )

    with pytest.raises(error_type, match="action denied"):
        client._raise_for_error(response)


def test_search_uses_current_collection_filters() -> None:
    client = RecordingClient()
    client.search(
        "tax",
        collection="1",
        sort="path",
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
                    "sort": "path",
                    "order": "desc",
                    "q": "tax",
                    "collection": "1",
                    "all": True,
                }
            },
        )
    ]


def test_collection_upload_selects_archive_store_without_materialization_policy() -> None:
    client = RecordingClient()

    client.create_or_resume_collection_upload_session("upload-one", ["docs"], archive_store="b2")
    client.register_collection_upload_session_files(
        1,
        [{"path": "one.txt", "bytes": 1, "sha256": "a" * 64}],
    )
    client.complete_collection_upload_session(1, files_total=1, content_etag="b" * 64)

    assert client.calls[0][2]["json"] == {
        "idempotency_key": "upload-one",
        "tags": ["docs"],
        "archive_store": "b2",
    }
    assert client.calls[1][2]["json"] == {
        "files": [{"path": "one.txt", "bytes": 1, "sha256": "a" * 64}],
    }
    assert client.calls[2][2]["json"] == {
        "files_total": 1,
        "content_etag": "b" * 64,
    }


def test_collection_upload_cancellation_allows_bounded_remote_cleanup() -> None:
    client = RecordingClient()

    client.cancel_collection_upload_session(1)

    assert client.calls == [
        (
            "POST",
            "/v1/collection-upload-sessions/1/cancel",
            {"timeout": 1800.0},
        )
    ]


def test_collection_deletion_carries_optional_event_context() -> None:
    client = RecordingClient()

    client.delete_collection(
        42,
        challenge="delete-challenge",
        event_context={"workflow": "direct-delete"},
    )

    assert client.calls == [
        (
            "POST",
            "/v1/collections/42/delete",
            {
                "json": {
                    "challenge": "delete-challenge",
                    "event_context": {"workflow": "direct-delete"},
                }
            },
        )
    ]


def test_retrieval_plan_and_job_share_exact_file_selection() -> None:
    client = RecordingClient()
    files = [(42, "invoice.pdf")]

    client.plan_retrieval(files, lease_seconds=3600)
    client.create_retrieval_job(files, plan_etag="a" * 64, lease_seconds=3600)

    payload = {
        "files": [
            {
                "collection_id": 42,
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


def test_retrieval_file_download_uses_the_logical_file_endpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = ApiClient(base_url="http://example.invalid")
    calls: list[tuple[str, Path]] = []
    output = tmp_path / "document.txt"

    def download(path: str, destination: Path, **kwargs: object) -> int:
        calls.append((path, destination))
        assert kwargs == {"expected_bytes": 42, "expected_sha256": "a" * 64, "progress": None}
        return 42

    monkeypatch.setattr(client, "_download", download)

    result = client.download_retrieval_file(
        "job-id",
        collection_id=42,
        path="docs/document.txt",
        output=output,
        expected_bytes=42,
        expected_sha256="a" * 64,
    )

    assert result == 42
    assert calls == [
        (
            "/v1/retrieval-jobs/job-id/content?collection_id=42&path=docs%2Fdocument.txt",
            output,
        )
    ]


def test_retrieval_file_download_streams_and_verifies_catalog_identity(
    tmp_path: Path,
) -> None:
    content = b"retrieved archive object"
    sha256 = hashlib.sha256(content).hexdigest()

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/retrieval-jobs/job-id/content"
        assert dict(request.url.params) == {
            "collection_id": "42",
            "path": "docs/document.txt",
        }
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Length": str(len(content)), "ETag": f'"{sha256}"'},
        )

    output = tmp_path / "document.txt"
    client = ApiClient(base_url="http://riverhog.test")
    client._download_client = httpx.Client(
        base_url=client.base_url,
        transport=httpx.MockTransport(handle),
    )
    try:
        result = client.download_retrieval_file(
            "job-id",
            collection_id=42,
            path="docs/document.txt",
            output=output,
            expected_bytes=len(content),
            expected_sha256=sha256,
        )
    finally:
        client.close()

    assert result == len(content)
    assert output.read_bytes() == content


def test_one_application_token_reaches_the_complete_client_surface(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_TOKEN", "application-token")

    client = ApiClient()
    assert client.token == "application-token"
    assert callable(client.plan_retrieval)
    assert callable(client.create_or_resume_collection_upload_session)
    assert callable(client.create_app_key)


def test_client_manages_application_keys_with_explicit_access() -> None:
    client = RecordingClient()

    client.list_apps(q="local", active=True, all_items=True)
    client.create_app_key(
        "local",
        access=[
            {"permission": "catalog:read", "resource": "tag:photos"},
            {"permission": "retrieval:manage", "resource": "tag:photos"},
        ],
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
                    "access": [
                        {"permission": "catalog:read", "resource": "tag:photos"},
                        {"permission": "retrieval:manage", "resource": "tag:photos"},
                    ],
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


def test_client_manages_explicit_tags() -> None:
    client = RecordingClient()
    client.create_tag("photos")
    client.get_tag("photos")
    client.list_tags(q="photo", all_items=True)
    assert client.calls == [
        ("POST", "/v1/tags", {"json": {"id": "photos"}}),
        ("GET", "/v1/tags/photos", {}),
        (
            "GET",
            "/v1/tags",
            {
                "params": {
                    "page": 1,
                    "per_page": 25,
                    "sort": "id",
                    "order": "asc",
                    "q": "photo",
                    "all": True,
                }
            },
        ),
    ]


def test_collection_upload_unit_uses_the_canonical_content_contract() -> None:
    client = RecordingClient()

    client.put_collection_upload_session_unit(
        42,
        "pack-000000000000",
        3,
        plan_sha256="a" * 64,
        content=b"source bytes",
    )

    assert client.calls == [
        (
            "PUT",
            "/v1/collection-upload-sessions/42/volumes/pack-000000000000/units/3",
            {
                "headers": {
                    "Content-Type": "application/octet-stream",
                    "If-Match": '"' + "a" * 64 + '"',
                },
                "content": b"source bytes",
            },
        )
    ]
