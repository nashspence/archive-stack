from __future__ import annotations

from pathlib import Path

from .helpers import create_collection, stage_collection_files
from .mock_data import family_archive_files


def test_auth_and_collection_name_validation(app_factory):
    with app_factory() as harness:
        health = harness.client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        unauthorized = harness.client.post("/v1/collections", json={"description": "blocked"})
        assert unauthorized.status_code == 401

        invalid_name = harness.client.post(
            "/v1/collections",
            headers=harness.auth_headers(),
            json={"root_node_name": "../escape", "description": "blocked"},
        )
        assert invalid_name.status_code == 400
        assert invalid_name.json()["detail"] == "path must not escape its root"


def test_collection_creation_creates_intake_directory_and_rejects_duplicates(app_factory):
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
        intake_path = Path(first.json()["intake_path"])
        assert intake_path.is_dir()
        stat_result = intake_path.stat()
        assert stat_result.st_uid == int(harness.modules.env["PREFERRED_UID"])
        assert stat_result.st_gid == int(harness.modules.env["PREFERRED_GID"])
        assert (stat_result.st_mode & 0o7777) == 0o2775

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
        assert duplicate.json()["detail"] == "collection name already exists"


def test_sealing_collection_claims_staged_files_and_exports_active_bytes(app_factory):
    with app_factory() as harness:
        sample = family_archive_files()[0]
        collection_id = create_collection(harness, description="home video ingest")
        intake_root = stage_collection_files(harness, collection_id, [sample])

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
                "source": "intake",
                "container_ids": [],
                "status": "open",
                "extra": None,
            }
        ]

        seal = harness.client.post(
            f"/v1/collections/{collection_id}/seal",
            headers=harness.auth_headers(),
        )
        assert seal.status_code == 200, seal.text
        assert not intake_root.exists()

        with harness.session() as session:
            collection_file = session.query(harness.models.CollectionFile).filter_by(collection_id=collection_id).one()
            assert collection_file.status == "active"
            assert collection_file.actual_sha256 == sample.sha256
            assert Path(collection_file.buffer_abs_path).read_bytes() == sample.content

        export_path = harness.storage.export_collection_root(collection_id) / sample.relative_path
        assert export_path.exists()
        assert export_path.read_bytes() == sample.content

        export_path = harness.storage.export_collection_root(collection_id) / sample.relative_path
        assert export_path.exists()
        assert export_path.read_bytes() == sample.content

        manifest_path = harness.storage.inactive_collection_hash_manifest_path(collection_id)
        proof_path = harness.storage.inactive_collection_hash_proof_path(collection_id)
        assert manifest_path.exists()
        assert proof_path.exists()

        manifest = manifest_path.read_text(encoding="utf-8")
        proof = proof_path.read_text(encoding="utf-8")

        assert "schema: collection-hash-manifest/v1" in manifest
        assert f"collection_id: {collection_id}" in manifest
        assert f"path: {sample.relative_path}" in manifest
        assert f"sha256: {sample.sha256}" in manifest
        assert "OpenTimestamps stub proof v1" in proof


def test_seal_rejects_symlinks_in_staged_collection(app_factory):
    with app_factory() as harness:
        collection_id = create_collection(harness, description="symlink rejection archive")
        intake_root = harness.storage.collection_intake_root(collection_id)
        intake_root.mkdir(parents=True, exist_ok=True)
        target = intake_root / "real.txt"
        target.write_text("hello\n", encoding="utf-8")
        (intake_root / "linked.txt").symlink_to(target)

        seal = harness.client.post(
            f"/v1/collections/{collection_id}/seal",
            headers=harness.auth_headers(),
        )
        assert seal.status_code == 400
        assert seal.json()["detail"] == "symlinks are not supported in collections: linked.txt"
        assert intake_root.exists()
