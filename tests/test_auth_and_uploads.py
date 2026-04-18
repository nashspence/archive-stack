from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

from .helpers import create_collection, reserve_collection_upload, simulate_tusd_upload
from .mock_data import family_archive_files


def test_auth_path_validation_and_hook_guards(app_factory):
    with app_factory() as harness:
        health = harness.client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        unauthorized = harness.client.post("/v1/collections", json={"description": "blocked"})
        assert unauthorized.status_code == 401

        collection_id = create_collection(harness, description="critical family archive")

        bad_directory = harness.client.post(
            f"/v1/collections/{collection_id}/directories",
            headers=harness.auth_headers(),
            json={"relative_path": "../escape"},
        )
        assert bad_directory.status_code == 400
        assert bad_directory.json()["detail"] == "path must not escape its root"

        invalid_mode = harness.client.post(
            f"/v1/collections/{collection_id}/uploads",
            headers=harness.auth_headers(),
            json={
                "relative_path": "finance/statement.pdf",
                "size_bytes": 12,
                "sha256": "a" * 64,
                "mode": "09AA",
                "mtime": "2026-04-17T10:15:30Z",
                "uid": 1000,
                "gid": 1000,
            },
        )
        assert invalid_mode.status_code == 400
        assert invalid_mode.json()["detail"] == "mode must be a zero-prefixed octal string like 0644"

        invalid_sha = harness.client.post(
            f"/v1/collections/{collection_id}/uploads",
            headers=harness.auth_headers(),
            json={
                "relative_path": "finance/statement.pdf",
                "size_bytes": 12,
                "sha256": "z" * 64,
                "mode": "0644",
                "mtime": "2026-04-17T10:15:30Z",
                "uid": 1000,
                "gid": 1000,
            },
        )
        assert invalid_sha.status_code == 400
        assert invalid_sha.json()["detail"] == "sha256 must be exactly 64 hexadecimal characters"

        forbidden_hook = harness.client.post(
            "/internal/tusd-hooks?hook_secret=wrong-secret",
            headers=harness.hook_headers("pre-create"),
            json={"ID": "x"},
        )
        assert forbidden_hook.status_code == 403


def test_collection_creation_uses_root_node_name_and_rejects_duplicates(app_factory):
    with app_factory() as harness:
        first = harness.client.post(
            "/v1/collections",
            headers=harness.auth_headers(),
            json={
                "root_node_name": "family-archive-root",
                "description": "critical family archive",
                "keep_buffer_after_archive": False,
            },
        )
        assert first.status_code == 200
        assert first.json()["collection_id"] == "family-archive-root"

        duplicate = harness.client.post(
            "/v1/collections",
            headers=harness.auth_headers(),
            json={
                "root_node_name": "family-archive-root",
                "description": "another archive",
                "keep_buffer_after_archive": False,
            },
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"] == "root node name already exists"


def test_upload_hooks_publish_progress_and_export_active_bytes(app_factory):
    with app_factory() as harness:
        sample = family_archive_files()[0]
        collection_id = create_collection(harness, description="home video ingest")
        slot = reserve_collection_upload(harness, collection_id, sample)

        simulate_tusd_upload(harness, slot, sample.content)

        upload_messages = harness.redis_messages(harness.progress.upload_stream_name(slot["upload_id"]))
        collection_messages = harness.redis_messages(harness.progress.collection_stream_name(collection_id))
        assert [fields["status"] for _, fields in upload_messages] == ["created", "uploading", "completed"]
        assert [fields["status"] for _, fields in collection_messages] == ["uploading", "uploading", "completed"]
        assert upload_messages[-1][1]["sha256"] == sample.sha256

        with harness.session() as session:
            collection_file = session.query(harness.models.CollectionFile).filter_by(collection_id=collection_id).one()
            slot_row = session.query(harness.models.UploadSlot).filter_by(upload_id=slot["upload_id"]).one()
            assert collection_file.status == "active"
            assert collection_file.actual_sha256 == sample.sha256
            assert Path(collection_file.buffer_abs_path).read_bytes() == sample.content
            assert slot_row.status == "completed"
            assert Path(slot_row.final_abs_path).read_bytes() == sample.content

        export_path = harness.storage.export_collection_root(collection_id) / sample.relative_path
        assert export_path.exists()
        assert export_path.read_bytes() == sample.content

        tree = harness.client.get(
            f"/v1/collections/{collection_id}/tree",
            headers=harness.auth_headers(),
        )
        assert tree.status_code == 200
        file_nodes = [node for node in tree.json()["nodes"] if node["kind"] == "file"]
        assert file_nodes == [
            {
                "path": sample.relative_path,
                "kind": "file",
                "size_bytes": sample.size_bytes,
                "active": True,
                "source": "buffer",
                "container_ids": [],
                "status": "active",
                "extra": None,
            }
        ]

        content = harness.client.get(
            f"/v1/collections/{collection_id}/content/{sample.relative_path}",
            headers=harness.auth_headers(),
        )
        assert content.status_code == 200
        assert content.content == sample.content

        bundle = harness.client.get(
            f"/v1/collections/{collection_id}/hash-manifest-proof",
            headers=harness.auth_headers(),
        )
        assert bundle.status_code == 200
        assert bundle.headers["content-disposition"].endswith(f'"{collection_id}-hash-manifest-proof.zip"')

        with zipfile.ZipFile(BytesIO(bundle.content)) as archive:
            assert sorted(archive.namelist()) == ["HASHES.yml", "HASHES.yml.ots"]
            manifest = archive.read("HASHES.yml").decode("utf-8")
            proof = archive.read("HASHES.yml.ots").decode("utf-8")

        assert "schema: collection-hash-manifest/v1" in manifest
        assert f"collection_id: {collection_id}" in manifest
        assert f"path: {sample.relative_path}" in manifest
        assert f"sha256: {sample.sha256}" in manifest
        assert "OpenTimestamps stub proof v1" in proof
