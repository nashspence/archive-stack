from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from ui.app import main as ui_main


def test_dashboard_and_detail_pages_render_with_api_data(monkeypatch):
    collection = {
        "collection_id": "demo-collection",
        "status": "open",
        "description": "demo collection",
        "keep_buffer_after_archive": False,
        "file_count": 2,
        "directory_count": 1,
        "created_at": "2026-04-18T00:00:00Z",
        "sealed_at": None,
        "intake_path": "/var/lib/uploads/collections/demo-collection",
        "export_path": "/var/lib/archive/exports/collections/demo-collection",
        "hash_manifest_path": None,
        "hash_proof_path": None,
    }
    container = {
        "container_id": "DEMO-001",
        "status": "inactive",
        "description": None,
        "total_root_bytes": 1024,
        "contents_hash": "abc123",
        "entry_count": 3,
        "active_root_present": False,
        "iso_present": True,
        "iso_size_bytes": 4096,
        "root_path": "/var/lib/archive/containers/roots/DEMO-001",
        "active_root_path": None,
        "iso_path": "/var/lib/archive/inactive/isos/DEMO-001.iso",
        "burn_confirmed_at": None,
        "created_at": "2026-04-18T00:00:00Z",
    }
    pool = {
        "state": "waiting",
        "status_message": "Waiting for more sealed collection data.",
        "pending_collection_count": 1,
        "pending_piece_group_count": 2,
        "pending_bytes": 1024,
        "target_bytes": 2048,
        "fill_bytes": 1536,
        "spill_fill_bytes": 1024,
        "buffer_max_bytes": 4096,
        "force_close_required": False,
        "closeable_now": False,
        "next_container_id": None,
        "next_container_bytes": None,
        "next_container_free_bytes": None,
        "next_container_collection_count": None,
        "next_container_piece_group_count": None,
    }

    def fake_load_json(path: str):
        if path == "/v1/collections":
            return {"collections": [collection]}, None
        if path == "/v1/containers":
            return {"containers": [container]}, None
        if path == "/v1/containers/pool":
            return pool, None
        if path == "/v1/collections/demo-collection/tree":
            return {
                "nodes": [
                    {
                        "path": "docs/file.txt",
                        "kind": "file",
                        "size_bytes": 10,
                        "active": True,
                        "source": "intake",
                        "container_ids": [],
                        "status": "open",
                    }
                ]
            }, None
        if path == "/v1/containers/DEMO-001/tree":
            return {
                "nodes": [
                    {
                        "path": "README.txt",
                        "kind": "file",
                        "size_bytes": 10,
                        "active": False,
                        "source": "container",
                        "container_ids": ["DEMO-001"],
                        "status": "inactive",
                    }
                ]
            }, None
        if path == "/v1/containers/DEMO-001/activation/sessions/session-123/expected":
            return {
                "session_id": "session-123",
                "container_id": "DEMO-001",
                "staging_path": "/var/lib/archive/active/activation/staging/session-123",
                "entries": [],
            }, None
        return None, "missing"

    monkeypatch.setattr(ui_main, "_load_json", fake_load_json)
    monkeypatch.setattr(ui_main, "_collection_summary", lambda collection_id: (collection, None))
    monkeypatch.setattr(ui_main, "_container_summary", lambda container_id: (container, None))

    with TestClient(ui_main.app) as client:
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "demo-collection" in dashboard.text
        assert "DEMO-001" in dashboard.text
        assert "waiting" in dashboard.text.lower()
        assert "/var/lib/uploads/collections/demo-collection" in dashboard.text

        collection_page = client.get("/collections/demo-collection")
        assert collection_page.status_code == 200
        assert "docs/file.txt" in collection_page.text
        assert "Place the full collection tree under" in collection_page.text
        assert "/var/lib/archive/exports/collections/demo-collection" in collection_page.text
        assert "/var/lib/uploads/collections/demo-collection" in collection_page.text

        container_page = client.get("/containers/DEMO-001?activation_session=session-123")
        assert container_page.status_code == 200
        assert "README.txt" in container_page.text
        assert "Create activation session" in container_page.text
        assert "Complete activation" in container_page.text
        assert "/var/lib/archive/inactive/isos/DEMO-001.iso" in container_page.text
        assert "/var/lib/archive/active/activation/staging/session-123" in container_page.text


def test_collection_urls_are_percent_encoded_for_collection_ids_with_spaces(monkeypatch):
    collection = {
        "collection_id": "demo collection",
        "status": "open",
        "description": "demo collection",
        "keep_buffer_after_archive": False,
        "file_count": 0,
        "directory_count": 0,
        "created_at": "2026-04-18T00:00:00Z",
        "sealed_at": None,
        "intake_path": "/var/lib/uploads/collections/demo collection",
        "export_path": "/var/lib/archive/exports/collections/demo collection",
        "hash_manifest_path": None,
        "hash_proof_path": None,
    }

    def fake_load_json(path: str):
        if path == "/v1/collections":
            return {"collections": [collection]}, None
        if path == "/v1/containers":
            return {"containers": []}, None
        if path == "/v1/containers/pool":
            return {
                "state": "empty",
                "status_message": "empty",
                "pending_collection_count": 0,
                "pending_piece_group_count": 0,
                "pending_bytes": 0,
                "target_bytes": 1,
                "fill_bytes": 1,
                "spill_fill_bytes": 1,
                "buffer_max_bytes": 1,
                "force_close_required": False,
                "closeable_now": False,
                "next_container_id": None,
                "next_container_bytes": None,
                "next_container_free_bytes": None,
                "next_container_collection_count": None,
                "next_container_piece_group_count": None,
            }, None
        if path == "/v1/collections/demo%20collection/tree":
            return {"nodes": []}, None
        return None, "missing"

    monkeypatch.setattr(ui_main, "_load_json", fake_load_json)
    monkeypatch.setattr(ui_main, "_collection_summary", lambda collection_id: (collection, None))

    with TestClient(ui_main.app) as client:
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert '/collections/demo%20collection' in dashboard.text

        collection_page = client.get("/collections/demo%20collection")
        assert collection_page.status_code == 200
        assert '/collections/demo%20collection/seal' in collection_page.text


def test_iso_download_forwards_range_requests(monkeypatch):
    class FakeApiClient:
        def __init__(self):
            self.last_request: httpx.Request | None = None
            self.closed = False

        def build_request(self, method: str, url: str, headers=None):
            return httpx.Request(method, url, headers=headers)

        def send(self, request: httpx.Request, stream: bool = False):
            self.last_request = request
            assert stream is True
            assert request.headers.get("range") == "bytes=0-3"
            return httpx.Response(
                206,
                headers={
                    "accept-ranges": "bytes",
                    "content-disposition": 'attachment; filename="demo.bin"',
                    "content-length": "4",
                    "content-range": "bytes 0-3/10",
                    "content-type": "application/octet-stream",
                    "etag": '"demo-etag"',
                    "last-modified": "Sat, 18 Apr 2026 00:00:00 GMT",
                },
                content=b"demo",
                request=request,
            )

        def close(self):
            self.closed = True

    fake_client = FakeApiClient()
    monkeypatch.setattr(ui_main, "_api_client", lambda: fake_client)

    with TestClient(ui_main.app) as client:
        response = client.get(
            "/containers/DEMO-001/iso/content",
            headers={"Range": "bytes=0-3"},
        )

    assert response.status_code == 206
    assert response.content == b"demo"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"] == "bytes 0-3/10"
    assert response.headers["content-length"] == "4"
    assert response.headers["etag"] == '"demo-etag"'
    assert fake_client.last_request is not None
    assert fake_client.closed is True
