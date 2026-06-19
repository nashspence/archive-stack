from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from riverhog_cli.client import ApiClient
from riverhog_core.domain.errors import ServiceUnavailable
from riverhog_core.tus_upload import TusHttpClient


def test_api_client_uses_env_configured_control_timeout(monkeypatch) -> None:
    captured: list[float] = []

    def fake_make_client(self: ApiClient, *, timeout_seconds: float) -> httpx.Client:
        captured.append(timeout_seconds)
        return httpx.Client(base_url=self.base_url)

    monkeypatch.setenv("RIVERHOG_HTTP_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setattr(ApiClient, "_make_client", fake_make_client)

    client = ApiClient(base_url="https://api.test")
    try:
        client._client()
    finally:
        client.close()

    assert captured == [12.5]


def test_upload_client_default_timeout_is_tuned_for_large_chunks(monkeypatch) -> None:
    monkeypatch.delenv("RIVERHOG_UPLOAD_TIMEOUT_SECONDS", raising=False)

    client = ApiClient(base_url="https://api.test")
    try:
        assert client.tus_client()._timeout_seconds == 300.0
    finally:
        client.close()


def test_create_or_resume_collection_upload_uses_collection_upload_endpoint(monkeypatch) -> None:
    captured: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, str(request.url), request.read().decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "collection_id": "2026/20260524T190233Z__tax-2022",
                "ingest_source": "/tmp/tax/2022",
                "state": "uploading",
                "files_total": 1,
                "files_pending": 1,
                "files_partial": 0,
                "files_uploaded": 0,
                "bytes_total": 12,
                "uploaded_bytes": 0,
                "missing_bytes": 12,
                "upload_state_expires_at": None,
                "files": [],
                "collection": None,
            },
        )

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    client.create_or_resume_collection_upload(
        "Tax 2022",
        [{"path": "invoice.pdf", "bytes": 12, "sha256": "a" * 64}],
        ingest_source="/tmp/tax/2022",
        upload_timestamp="20250712T213200Z",
    )

    assert captured[0][0] == "POST"
    assert captured[0][1] == "https://api.test/v1/collection-uploads"
    assert '"slug":"Tax 2022"' in str(captured[0][2])
    assert '"upload_timestamp":"20250712T213200Z"' in str(captured[0][2])
    assert "collection_id" not in str(captured[0][2])


def test_create_or_resume_incremental_upload_session_uses_session_endpoint(
    monkeypatch,
) -> None:
    captured: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, str(request.url), request.read().decode("utf-8")))
        return httpx.Response(200, json={"collection_id": "docs", "state": "open"})

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    client.create_or_resume_collection_upload_session(
        "Tax 2022",
        ingest_source="/tmp/tax/2022",
        upload_timestamp="20250712T213200Z",
    )

    assert captured[0][0] == "POST"
    assert captured[0][1] == "https://api.test/v1/collection-upload-sessions"
    assert '"slug":"Tax 2022"' in str(captured[0][2])
    assert '"upload_timestamp":"20250712T213200Z"' in str(captured[0][2])


def test_incremental_upload_session_file_and_close_endpoints_quote_collection_id(
    monkeypatch,
) -> None:
    captured: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, str(request.url), request.read().decode("utf-8")))
        return httpx.Response(200, json={"collection_id": "tax/2022", "state": "open"})

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    client.register_collection_upload_session_file(
        "tax/2022",
        {"path": "invoice 123.pdf", "bytes": 12, "sha256": "a" * 64},
    )
    client.complete_collection_upload_session("tax/2022")
    client.cancel_collection_upload_session("tax/2022")

    assert captured[0][0] == "POST"
    assert captured[0][1] == "https://api.test/v1/collection-upload-sessions/tax/2022/files"
    assert '"path":"invoice 123.pdf"' in str(captured[0][2])
    assert captured[1][1] == "https://api.test/v1/collection-upload-sessions/tax/2022/complete"
    assert captured[2][1] == "https://api.test/v1/collection-upload-sessions/tax/2022/cancel"


def test_get_collection_quotes_reserved_characters_but_preserves_slashes(monkeypatch) -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(200, json={"id": "tax/2022 reports"})

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    client.get_collection("tax/2022 reports")

    assert captured == ["https://api.test/v1/collections/tax/2022%20reports"]


def test_client_can_override_host_header_for_pinned_lan_routes(monkeypatch) -> None:
    captured: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("host"))
        return httpx.Response(200, json={"id": "docs"})

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        headers = {"Accept": "application/json"}
        if self.host_header:
            headers["Host"] = self.host_header
        return httpx.Client(
            base_url=self.base_url,
            headers=headers,
            transport=transport,
            verify=self.verify_tls,
        )

    monkeypatch.setenv("RIVERHOG_HOST_HEADER", "riverhog.example.test")
    monkeypatch.setenv("RIVERHOG_TLS_VERIFY", "false")
    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://192.168.152.1")
    assert client.verify_tls is False

    client.get_collection("docs")

    assert captured == ["riverhog.example.test"]


def test_list_collections_uses_collection_listing_endpoint(monkeypatch) -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "page": 1,
                "per_page": 10,
                "total": 1,
                "pages": 1,
                "collections": [],
            },
        )

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    client.list_collections(page=1, per_page=10, q="docs", protection_state="fully_protected")

    assert captured == [
        "https://api.test/v1/collections?page=1&per_page=10&q=docs&protection_state=fully_protected"
    ]


def test_list_dashboard_collections_uses_dashboard_endpoint(monkeypatch) -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(200, json={"collections": []})

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    client.list_dashboard_collections(q="docs")

    assert captured == ["https://api.test/v1/dashboard/collections?q=docs"]


def test_create_or_resume_collection_file_upload_quotes_collection_and_path(monkeypatch) -> None:
    captured: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, str(request.url)))
        return httpx.Response(
            200,
            json={
                "path": "tax/2022 reports/invoice 123.pdf",
                "protocol": "tus",
                "upload_url": "https://uploads.test/collections/tax/2022/e1",
                "offset": 0,
                "length": 12,
                "checksum_algorithm": "sha256",
                "expires_at": "2026-04-23T00:00:00Z",
            },
        )

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    payload = client.create_or_resume_collection_file_upload(
        "tax/2022",
        "reports/invoice 123.pdf",
    )

    assert payload["upload_url"] == "https://uploads.test/collections/tax/2022/e1"
    assert captured == [
        (
            "POST",
            "https://api.test/v1/collection-uploads/tax/2022/files/reports/invoice%20123.pdf/upload",
        )
    ]


def test_create_or_resume_registered_collection_file_upload_posts_manifest_file(
    monkeypatch,
) -> None:
    captured: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, str(request.url), json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "collection_id": "tax/2022",
                "ingest_source": "/tmp/source",
                "state": "open",
                "file": {
                    "path": "reports/invoice 123.pdf",
                    "bytes": 12,
                    "sha256": "0" * 64,
                    "upload_state": "pending",
                    "uploaded_bytes": 0,
                    "upload_state_expires_at": None,
                },
                "path": "reports/invoice 123.pdf",
                "protocol": "tus",
                "upload_url": "https://uploads.test/collections/tax/2022/e1",
                "offset": 0,
                "length": 12,
                "checksum_algorithm": "sha256",
                "expires_at": "2026-04-23T00:00:00Z",
            },
        )

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    payload = client.create_or_resume_registered_collection_file_upload(
        "tax/2022",
        {
            "path": "reports/invoice 123.pdf",
            "bytes": 12,
            "sha256": "0" * 64,
        },
    )

    assert payload["upload_url"] == "https://uploads.test/collections/tax/2022/e1"
    assert captured == [
        (
            "POST",
            "https://api.test/v1/collection-upload-sessions/tax/2022/files/upload",
            {
                "path": "reports/invoice 123.pdf",
                "bytes": 12,
                "sha256": "0" * 64,
            },
        )
    ]


def test_list_collection_files_uses_unambiguous_collection_files_endpoint(monkeypatch) -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "collection_id": "tax/files",
                "page": 2,
                "per_page": 2,
                "total": 3,
                "pages": 2,
                "files": [],
            },
        )

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    client.list_collection_files("tax/files", page=2, per_page=2)

    assert captured == ["https://api.test/v1/collection-files/tax/files?page=2&per_page=2"]


def test_query_files_includes_pagination_params(monkeypatch) -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "target": "docs/",
                "page": 2,
                "per_page": 2,
                "total": 3,
                "pages": 2,
                "files": [],
            },
        )

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    client.query_files("docs/", page=2, per_page=2)

    assert captured == ["https://api.test/v1/files?target=docs%2F&page=2&per_page=2"]


def test_finalize_image_uses_candidate_finalize_endpoint(monkeypatch) -> None:
    captured: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, str(request.url)))
        return httpx.Response(200, json={"id": "20260420T040001Z"})

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    payload = client.finalize_image("img_2026-04-20_01")

    assert payload["id"] == "20260420T040001Z"
    assert captured == [("POST", "https://api.test/v1/plan/candidates/img_2026-04-20_01/finalize")]


def test_copy_registration_uses_long_timeout(monkeypatch) -> None:
    monkeypatch.delenv("RIVERHOG_COPY_REGISTRATION_TIMEOUT_SECONDS", raising=False)
    captured: list[tuple[str, str, float | None]] = []

    def fake_request(self: ApiClient, method: str, path: str, **kwargs) -> httpx.Response:
        _ = self
        captured.append((method, path, kwargs.get("timeout")))
        return httpx.Response(200, json={"copy": {"id": "20260420T040001Z-1"}})

    monkeypatch.setattr(ApiClient, "_request", fake_request)

    client = ApiClient(base_url="https://api.test")
    client.register_copy("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    client.update_copy(
        "20260420T040001Z",
        "20260420T040001Z-1",
        state="verified",
        verification_state="verified",
    )

    assert captured == [
        ("POST", "/v1/images/20260420T040001Z/copies", 3600.0),
        ("PATCH", "/v1/images/20260420T040001Z/copies/20260420T040001Z-1", 3600.0),
    ]


def test_download_iso_streams_to_output_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("RIVERHOG_DOWNLOAD_TIMEOUT_SECONDS", raising=False)
    captured: list[str] = []
    captured_timeouts: list[float] = []
    content = b"fixture-iso\n"

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(content))},
            content=content,
        )

    transport = httpx.MockTransport(handler)

    def fake_make_client(self: ApiClient, *, timeout_seconds: float) -> httpx.Client:
        captured_timeouts.append(timeout_seconds)
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_make_client", fake_make_client)

    progress: list[tuple[int, int | None]] = []
    output = tmp_path / "image.iso"
    client = ApiClient(base_url="https://api.test")
    downloaded = client.download_iso(
        "20260420T040001Z",
        output,
        progress=lambda downloaded, total: progress.append((downloaded, total)),
    )

    assert downloaded == len(content)
    assert output.read_bytes() == content
    assert not (tmp_path / ".image.iso.part").exists()
    assert progress == [(len(content), len(content))]
    assert captured == ["https://api.test/v1/images/20260420T040001Z/iso"]
    assert captured_timeouts == [3600.0]


def test_download_iso_discards_partial_file_after_stream_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = b"fixture-iso\n"

    class FailingStream(httpx.SyncByteStream):
        def __iter__(self):
            yield content
            raise httpx.RemoteProtocolError("fixture stream reset")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("range") is None
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(content) + 4096)},
            stream=FailingStream(),
        )

    transport = httpx.MockTransport(handler)

    def fake_make_client(self: ApiClient, *, timeout_seconds: float) -> httpx.Client:
        _ = timeout_seconds
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_make_client", fake_make_client)

    output = tmp_path / "image.iso"
    client = ApiClient(base_url="https://api.test")
    with pytest.raises(ServiceUnavailable, match="download stream was interrupted"):
        client.download_iso("20260420T040001Z", output)

    assert not output.exists()
    assert not (tmp_path / ".image.iso.part").exists()


def test_get_glacier_report_uses_glacier_endpoint_with_filters(monkeypatch) -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "scope": "image",
                "measured_at": "2026-04-28T00:00:00Z",
                "pricing_basis": {},
                "totals": {},
                "images": [],
                "collections": [],
                "history": [],
            },
        )

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    client.get_glacier_report(collection="docs")

    assert captured == ["https://api.test/v1/glacier?collection=docs"]


def test_create_or_resume_fetch_entry_upload_uses_manifest_entry_endpoint(monkeypatch) -> None:
    captured: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, str(request.url)))
        return httpx.Response(
            200,
            json={
                "entry": "e1",
                "protocol": "tus",
                "upload_url": "https://uploads.test/fx-1/e1",
                "offset": 0,
                "length": 12,
                "checksum_algorithm": "sha256",
                "expires_at": "2026-04-23T00:00:00Z",
            },
        )

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    payload = client.create_or_resume_fetch_entry_upload("fx-1", "e1")

    assert payload["upload_url"] == "https://uploads.test/fx-1/e1"
    assert captured == [("POST", "https://api.test/v1/fetches/fx-1/entries/e1/upload")]


def test_append_upload_chunk_uses_tus_patch_headers(monkeypatch) -> None:
    captured: list[httpx.Request] = []
    content = b"invoice fixture bytes\n"

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            204,
            headers={
                "Upload-Offset": str(len(content)),
                "Upload-Expires": "2026-04-23T00:00:00Z",
            },
        )

    transport = httpx.MockTransport(handler)

    def fake_upload_client(self: TusHttpClient) -> httpx.Client:
        return httpx.Client(
            headers=self._headers,
            transport=transport,
            verify=self._verify_tls,
            http2=self._http2,
            timeout=self._timeout_seconds,
        )

    monkeypatch.setattr(TusHttpClient, "_client_for_request", fake_upload_client)

    client = ApiClient(base_url="https://api.test")
    payload = client.append_upload_chunk(
        "https://uploads.test/fx-1/e1",
        offset=0,
        checksum_algorithm="sha256",
        content=content,
    )

    checksum = base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")

    assert payload == {
        "offset": len(content),
        "expires_at": None,
    }
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "PATCH"
    assert str(request.url) == "https://uploads.test/fx-1/e1"
    assert request.headers["Content-Length"] == str(len(content))
    assert request.headers["Content-Type"] == "application/offset+octet-stream"
    assert request.headers["Tus-Resumable"] == "1.0.0"
    assert request.headers["Upload-Offset"] == "0"
    assert request.headers["Upload-Checksum"] == f"sha256 {checksum}"
    assert request.read() == content


def test_append_upload_chunk_can_override_absolute_upload_base_url(monkeypatch) -> None:
    captured: list[httpx.Request] = []
    content = b"invoice fixture bytes\n"

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204, headers={"Upload-Offset": str(len(content))})

    transport = httpx.MockTransport(handler)

    def fake_upload_client(self: TusHttpClient) -> httpx.Client:
        return httpx.Client(
            headers=self._headers,
            transport=transport,
            verify=self._verify_tls,
            http2=self._http2,
            timeout=self._timeout_seconds,
        )

    monkeypatch.setenv("RIVERHOG_UPLOAD_BASE_URL", "http://127.0.0.1:18080")
    monkeypatch.setattr(TusHttpClient, "_client_for_request", fake_upload_client)

    client = ApiClient(base_url="http://127.0.0.1:18080")
    client.append_upload_chunk(
        "https://riverhog.example.test/v1/collection-uploads/docs/files/report.txt/upload",
        offset=0,
        checksum_algorithm="sha256",
        content=content,
    )

    assert len(captured) == 1
    assert (
        str(captured[0].url)
        == "http://127.0.0.1:18080/v1/collection-uploads/docs/files/report.txt/upload"
    )


def test_upload_client_can_disable_http2_independently(monkeypatch) -> None:
    created_http2: list[bool] = []

    def fake_upload_client(self: TusHttpClient) -> httpx.Client:
        created_http2.append(self._http2)
        return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))

    monkeypatch.setenv("RIVERHOG_HTTP2", "true")
    monkeypatch.setenv("RIVERHOG_UPLOAD_HTTP2", "false")
    monkeypatch.setattr(TusHttpClient, "_client_for_request", fake_upload_client)

    client = ApiClient(base_url="https://api.test")
    assert client.http2 is True
    assert client.upload_http2 is False

    assert client.tus_client().head_offset("https://uploads.test/fx-1/e1") == -1
    assert created_http2 == [False]


def test_api_client_reuses_http_client_for_requests(monkeypatch) -> None:
    created = 0
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "page": 1,
                "per_page": 25,
                "total": 0,
                "pages": 0,
                "collections": [],
            },
        )

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        nonlocal created
        created += 1
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    try:
        client.list_collections()
        client.list_collections()
    finally:
        client.close()

    assert created == 1
    assert seen == [
        "https://api.test/v1/collections?page=1&per_page=25",
        "https://api.test/v1/collections?page=1&per_page=25",
    ]


def test_api_client_closes_http_client_after_transport_error(monkeypatch) -> None:
    created = 0
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadError("broken stream", request=request)
        return httpx.Response(
            200,
            json={
                "page": 1,
                "per_page": 25,
                "total": 0,
                "pages": 0,
                "collections": [],
            },
        )

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        nonlocal created
        created += 1
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    with pytest.raises(httpx.ReadError):
        client.list_collections()
    client.list_collections()

    assert created == 2


def test_api_client_closes_http_client_after_transient_status(monkeypatch) -> None:
    created = 0
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(502, request=request, content=b"bad gateway")
        return httpx.Response(
            200,
            json={
                "page": 1,
                "per_page": 25,
                "total": 0,
                "pages": 0,
                "collections": [],
            },
        )

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        nonlocal created
        created += 1
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    with pytest.raises(httpx.HTTPStatusError):
        client.list_collections()
    client.list_collections()

    assert created == 2


def test_register_copy_uses_generated_copy_endpoint(monkeypatch) -> None:
    captured: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, str(request.url), request.read().decode("utf-8")))
        return httpx.Response(200, json={"copy": {"id": "20260420T040001Z-1"}})

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    client.register_copy("20260420T040001Z", "Shelf B1")

    assert captured == [
        (
            "POST",
            "https://api.test/v1/images/20260420T040001Z/copies",
            '{"location":"Shelf B1"}',
        )
    ]


def test_notify_copy_label_needed_uses_copy_endpoint(monkeypatch) -> None:
    captured: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, str(request.url), request.read().decode("utf-8")))
        return httpx.Response(200, json={"copy": {"id": "20260420T040001Z-1"}})

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    client.notify_copy_label_needed("20260420T040001Z", "20260420T040001Z-1")

    assert captured == [
        (
            "POST",
            "https://api.test/v1/images/20260420T040001Z/copies/20260420T040001Z-1/label-needed",
            "",
        )
    ]


def test_update_copy_uses_copy_patch_endpoint(monkeypatch) -> None:
    captured: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, str(request.url), request.read().decode("utf-8")))
        return httpx.Response(200, json={"copy": {"id": "20260420T040001Z-1"}})

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://api.test")
    client.update_copy(
        "20260420T040001Z",
        "20260420T040001Z-1",
        location="Shelf B2",
        state="verified",
        verification_state="verified",
    )

    assert captured == [
        (
            "PATCH",
            "https://api.test/v1/images/20260420T040001Z/copies/20260420T040001Z-1",
            '{"location":"Shelf B2","state":"verified","verification_state":"verified"}',
        )
    ]
