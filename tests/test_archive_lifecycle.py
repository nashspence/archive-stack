from __future__ import annotations

from pathlib import Path

import yaml

from .helpers import activation_container_from_root, closed_container_roots, create_collection, force_flush, register_iso, seal_collection, stage_collection_files
from .mock_data import document_archive_files, family_archive_files, oversized_master_reel


def test_closed_container_can_be_activated_and_restore_collection_reads(app_factory):
    with app_factory() as harness:
        collection_id = create_collection(harness, description="family and finance archive")
        stage_collection_files(harness, collection_id, family_archive_files())

        sealed = seal_collection(harness, collection_id)
        container_ids = sealed["closed_containers"] or force_flush(harness)
        assert len(container_ids) == 1
        container_id = container_ids[0]

        release = harness.client.post(
            f"/v1/collections/{collection_id}/buffer/release",
            headers=harness.auth_headers(),
        )
        assert release.status_code == 200

        export_path = harness.storage.export_collection_root(collection_id) / family_archive_files()[0].relative_path
        assert not export_path.exists()

        _, complete = activation_container_from_root(harness, container_id)
        assert complete.status_code == 200
        assert complete.json()["status"] == "active"

        assert export_path.exists()
        assert export_path.read_bytes() == family_archive_files()[0].content

        container_tree = harness.client.get(
            f"/v1/containers/{container_id}/tree",
            headers=harness.auth_headers(),
        )
        assert container_tree.status_code == 200
        assert any(node["path"] == "MANIFEST.yml" for node in container_tree.json()["nodes"])
        assert any(node["path"] == "README.txt" for node in container_tree.json()["nodes"])
        assert any(node["path"] == f"collections/{collection_id}/HASHES.yml" for node in container_tree.json()["nodes"])
        assert any(node["path"] == f"collections/{collection_id}/HASHES.yml.ots" for node in container_tree.json()["nodes"])

        container_roots = closed_container_roots(harness, container_ids)
        manifest_path = container_roots[container_id] / "MANIFEST.yml"
        sidecar_path = next((container_roots[container_id] / "files").glob("*.meta.yaml"))
        readme_path = container_roots[container_id] / "README.txt"
        hash_manifest_path = container_roots[container_id] / "collections" / collection_id / "HASHES.yml"
        hash_proof_path = container_roots[container_id] / "collections" / collection_id / "HASHES.yml.ots"
        assert manifest_path.exists()
        assert manifest_path.read_bytes().startswith(b"age-encryption.org/")
        assert sidecar_path.read_bytes().startswith(b"age-encryption.org/")
        assert readme_path.read_text().startswith(f"Archive container: {container_id}")
        assert "age -d -j batchpass MANIFEST.yml" in readme_path.read_text()
        assert hash_manifest_path.read_bytes().startswith(b"age-encryption.org/")
        assert hash_proof_path.read_bytes().startswith(b"age-encryption.org/")

        with harness.session() as session:
            container = session.get(harness.models.Container, container_id)
            assert container is not None
            assert container.active_root_abs_path is not None
            active_manifest = Path(container.active_root_abs_path) / "MANIFEST.yml"
            active_sidecar = next((Path(container.active_root_abs_path) / "files").glob("*.meta.yaml"))
            active_readme = Path(container.active_root_abs_path) / "README.txt"
            active_hash_manifest = Path(container.active_root_abs_path) / "collections" / collection_id / "HASHES.yml"
            active_hash_proof = Path(container.active_root_abs_path) / "collections" / collection_id / "HASHES.yml.ots"
            manifest = yaml.safe_load(active_manifest.read_text())
            collection_hash_manifest = yaml.safe_load(active_hash_manifest.read_text())
            assert manifest["schema"] == "manifest/v1"
            assert manifest["container"] == container_id
            assert len(manifest["collections"]) == 1
            assert manifest["collections"][0]["name"] == collection_id
            assert all("path" in file_entry and "sha256" in file_entry for file_entry in manifest["collections"][0]["files"])
            assert any(isinstance(file_entry.get("archive"), str) for file_entry in manifest["collections"][0]["files"])
            assert collection_hash_manifest["schema"] == "collection-hash-manifest/v1"
            assert collection_hash_manifest["collection_id"] == collection_id
            assert active_hash_proof.read_text().startswith("OpenTimestamps stub proof v1")
            assert active_sidecar.read_text().startswith("schema: sidecar/v1")
            assert active_readme.read_text().startswith(f"Archive container: {container_id}")


def test_activation_verification_rejects_mutated_container_contents(app_factory):
    with app_factory() as harness:
        collection_id = create_collection(harness, description="financial document set")
        stage_collection_files(harness, collection_id, document_archive_files())

        sealed = seal_collection(harness, collection_id)
        container_id = (sealed["closed_containers"] or force_flush(harness))[0]

        def mutate(relpath: str, content: bytes) -> bytes:
            if relpath.endswith(".meta.yaml"):
                return content
            return content[:-1] + bytes([content[-1] ^ 0x01])

        _, complete = activation_container_from_root(harness, container_id, mutate=mutate)
        assert complete.status_code == 409
        assert complete.json()["detail"] == "staged root does not match the known container contents"

        with harness.session() as session:
            activation_session = session.query(harness.models.ActivationSession).order_by(harness.models.ActivationSession.created_at.desc()).first()
            assert activation_session is not None
            assert activation_session.status == "failed"


def test_split_file_materializes_only_when_all_required_containers_are_active(app_factory):
    with app_factory(
        CONTAINER_TARGET_GB="0.0005",
        CONTAINER_FILL_GB="0.00035",
        CONTAINER_SPILL_FILL_GB="0.00030",
        CONTAINER_BUFFER_MAX_GB="0.0040",
    ) as harness:
        master = oversized_master_reel()
        collection_id = create_collection(harness, description="master home video reel")
        stage_collection_files(harness, collection_id, [master])

        sealed = seal_collection(harness, collection_id)
        container_ids = sealed["closed_containers"] + force_flush(harness)
        assert len(container_ids) >= 2

        release = harness.client.post(
            f"/v1/collections/{collection_id}/buffer/release",
            headers=harness.auth_headers(),
        )
        assert release.status_code == 200

        activation_container_from_root(harness, container_ids[0])

        export_path = harness.storage.export_collection_root(collection_id) / master.relative_path
        assert not export_path.exists()

        for container_id in container_ids[1:]:
            _, complete = activation_container_from_root(harness, container_id)
            assert complete.status_code == 200

        assert export_path.exists()
        assert export_path.read_bytes() == master.content

        with harness.session() as session:
            collection_file = session.query(harness.models.CollectionFile).filter_by(collection_id=collection_id).one()
            materialized_path = Path(collection_file.materialized_abs_path)
            assert materialized_path.exists()
            assert materialized_path.read_bytes() == master.content


def test_buffer_cleanup_waits_for_all_container_burns_and_respects_retention_override(app_factory):
    with app_factory(
        CONTAINER_TARGET_GB="0.0005",
        CONTAINER_FILL_GB="0.00035",
        CONTAINER_SPILL_FILL_GB="0.00030",
        CONTAINER_BUFFER_MAX_GB="0.0040",
    ) as harness:
        master = oversized_master_reel()
        collection_id = create_collection(harness, description="critical family reel")
        stage_collection_files(harness, collection_id, [master])
        sealed = seal_collection(harness, collection_id)
        container_ids = sealed["closed_containers"] + force_flush(harness)
        assert len(container_ids) >= 2

        for container_id in container_ids:
            iso = register_iso(harness, container_id, f"iso-{container_id}".encode())
            assert iso["size_bytes"] > 0

        for container_id in container_ids[:-1]:
            confirm = harness.client.post(
                f"/v1/containers/{container_id}/burn/confirm",
                headers=harness.auth_headers(),
            )
            assert confirm.status_code == 200
            assert confirm.json()["released_collection_ids"] == []

        with harness.session() as session:
            collection_file = session.query(harness.models.CollectionFile).filter_by(collection_id=collection_id).one()
            assert Path(collection_file.buffer_abs_path).exists()

        last_confirm = harness.client.post(
            f"/v1/containers/{container_ids[-1]}/burn/confirm",
            headers=harness.auth_headers(),
        )
        assert last_confirm.status_code == 200
        assert collection_id in last_confirm.json()["released_collection_ids"]

        with harness.session() as session:
            collection_file = session.query(harness.models.CollectionFile).filter_by(collection_id=collection_id).one()
            assert collection_file.buffer_abs_path is None

    with app_factory() as retained_harness:
        collection_id = create_collection(
            retained_harness,
            description="archive with retention lock",
            keep_buffer_after_archive=True,
        )
        stage_collection_files(retained_harness, collection_id, document_archive_files())

        sealed = seal_collection(retained_harness, collection_id)
        container_ids = sealed["closed_containers"] or force_flush(retained_harness)
        for container_id in container_ids:
            register_iso(retained_harness, container_id, f"iso-{container_id}".encode())
            confirm = retained_harness.client.post(
                f"/v1/containers/{container_id}/burn/confirm",
                headers=retained_harness.auth_headers(),
            )
            assert confirm.status_code == 200
            assert collection_id not in confirm.json()["released_collection_ids"]

        with retained_harness.session() as session:
            collection_files = session.query(retained_harness.models.CollectionFile).filter_by(collection_id=collection_id).all()
            assert all(collection_file.buffer_abs_path for collection_file in collection_files)
