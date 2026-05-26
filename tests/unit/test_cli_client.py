from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import httpx
import pytest

from riverhog_cli.client import ApiClient, _iter_upload_body


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

    monkeypatch.setenv("RIVERHOG_HOST_HEADER", "riverhog.0819870.xyz")
    monkeypatch.setenv("RIVERHOG_TLS_VERIFY", "false")
    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="https://192.168.152.1")
    assert client.verify_tls is False

    client.get_collection("docs")

    assert captured == ["riverhog.0819870.xyz"]


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


def test_download_iso_streams_to_output_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[str] = []
    content = b"fixture-iso\n"

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(content))},
            content=content,
        )

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

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

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setattr(ApiClient, "_client", fake_client)

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
        "expires_at": "2026-04-23T00:00:00Z",
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


def test_iter_upload_body_splits_configured_write_chunks() -> None:
    content = b"invoice fixture bytes\n"

    assert list(_iter_upload_body(content, chunk_bytes=7, delay_seconds=0)) == [
        b"invoice",
        b" fixtur",
        b"e bytes",
        b"\n",
    ]


def test_append_upload_chunk_can_override_absolute_upload_base_url(monkeypatch) -> None:
    captured: list[httpx.Request] = []
    content = b"invoice fixture bytes\n"

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204, headers={"Upload-Offset": str(len(content))})

    transport = httpx.MockTransport(handler)

    def fake_client(self: ApiClient) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, transport=transport)

    monkeypatch.setenv("RIVERHOG_UPLOAD_BASE_URL", "http://127.0.0.1:18080")
    monkeypatch.setattr(ApiClient, "_client", fake_client)

    client = ApiClient(base_url="http://127.0.0.1:18080")
    client.append_upload_chunk(
        "https://riverhog.0819870.xyz/v1/collection-uploads/docs/files/report.txt/upload",
        offset=0,
        checksum_algorithm="sha256",
        content=content,
    )

    assert len(captured) == 1
    assert (
        str(captured[0].url)
        == "http://127.0.0.1:18080/v1/collection-uploads/docs/files/report.txt/upload"
    )


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
