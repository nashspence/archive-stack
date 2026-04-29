from __future__ import annotations

import base64
import hashlib

import httpx

from arc_cli.client import ApiClient


def test_create_or_resume_collection_upload_uses_collection_upload_endpoint(monkeypatch) -> None:
    captured: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, str(request.url), request.read().decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "collection_id": "tax/2022",
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
        "tax/2022",
        [{"path": "invoice.pdf", "bytes": 12, "sha256": "a" * 64}],
        ingest_source="/tmp/tax/2022",
    )

    assert captured[0][0] == "POST"
    assert captured[0][1] == "https://api.test/v1/collection-uploads"


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
    client.list_collections(page=1, per_page=10, q="docs", protection_state="protected")

    assert captured == [
        "https://api.test/v1/collections?page=1&per_page=10&q=docs&protection_state=protected"
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
    client.get_glacier_report(image_id="20260420T040001Z", collection="docs")

    assert captured == ["https://api.test/v1/glacier?image_id=20260420T040001Z&collection=docs"]


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
    assert request.headers["Content-Type"] == "application/offset+octet-stream"
    assert request.headers["Tus-Resumable"] == "1.0.0"
    assert request.headers["Upload-Offset"] == "0"
    assert request.headers["Upload-Checksum"] == f"sha256 {checksum}"
    assert request.read() == content


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
