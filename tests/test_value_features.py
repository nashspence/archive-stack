from __future__ import annotations

from pathlib import Path

from .helpers import activation_container_from_root, create_iso, create_collection, force_flush, seal_collection, stage_collection_files
from .mock_data import MockFile, document_archive_files, patterned_bytes


def test_open_collection_tree_keeps_explicit_and_derived_directories(app_factory):
    with app_factory() as harness:
        collection_id = create_collection(harness, description="catalog coverage archive")

        sample = MockFile(
            "scans/2025/passports/alex/passport.pdf",
            patterned_bytes("passport-scan", 24_000),
        )
        stage_collection_files(
            harness,
            collection_id,
            [sample],
            directories=["scans/2025/receipts"],
        )

        tree = harness.client.get(
            f"/v1/collections/{collection_id}/tree",
            headers=harness.auth_headers(),
        )
        assert tree.status_code == 200, tree.text

        directories = {node["path"] for node in tree.json()["nodes"] if node["kind"] == "directory"}
        assert directories == {
            "scans",
            "scans/2025",
            "scans/2025/passports",
            "scans/2025/passports/alex",
            "scans/2025/receipts",
        }

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


def test_open_collection_content_is_available_on_the_filesystem(app_factory):
    with app_factory() as harness:
        sample = document_archive_files()[0]
        collection_id = create_collection(harness, description="open intake reads")
        intake_root = stage_collection_files(harness, collection_id, [sample])

        content_path = intake_root / sample.relative_path
        assert content_path.exists()
        assert content_path.read_bytes() == sample.content


def test_sealed_collection_rejects_new_seal_attempt_and_writes_hash_artifacts(app_factory):
    with app_factory() as harness:
        sample = document_archive_files()[0]
        collection_id = create_collection(harness, description="sealed archive behavior")
        stage_collection_files(harness, collection_id, [sample])
        seal_collection(harness, collection_id)

        reseal = harness.client.post(
            f"/v1/collections/{collection_id}/seal",
            headers=harness.auth_headers(),
        )
        assert reseal.status_code == 409
        assert reseal.json()["detail"] == "collection already sealed"

        assert harness.storage.inactive_collection_hash_manifest_path(collection_id).exists()
        assert harness.storage.inactive_collection_hash_proof_path(collection_id).exists()


def test_container_activation_and_evict_toggle_container_and_collection_visibility(app_factory):
    with app_factory() as harness:
        samples = document_archive_files()
        collection_id = create_collection(harness, description="activation visibility archive")
        stage_collection_files(harness, collection_id, samples)

        sealed = seal_collection(harness, collection_id)
        container_id = (sealed["closed_containers"] or force_flush(harness))[0]

        release = harness.client.post(
            f"/v1/collections/{collection_id}/buffer/release",
            headers=harness.auth_headers(),
        )
        assert release.status_code == 200, release.text

        export_path = harness.storage.export_collection_root(collection_id) / samples[0].relative_path
        assert not export_path.exists()

        _, complete = activation_container_from_root(harness, container_id)
        assert complete.status_code == 200, complete.text

        with harness.session() as session:
            container = session.get(harness.models.Container, container_id)
            assert container is not None
            payload_entry = (
                session.query(harness.models.ContainerEntry)
                .filter_by(container_id=container_id, kind="payload")
                .order_by(harness.models.ContainerEntry.relative_path.asc())
                .first()
            )
            assert payload_entry is not None
            payload_path = Path(container.active_root_abs_path) / payload_entry.relative_path
            assert payload_path.exists()

        assert export_path.exists()
        assert export_path.read_bytes() == samples[0].content

        evict = harness.client.delete(
            f"/v1/containers/{container_id}/activation",
            headers=harness.auth_headers(),
        )
        assert evict.status_code == 200, evict.text

        container_tree = harness.client.get(
            f"/v1/containers/{container_id}/tree",
            headers=harness.auth_headers(),
        )
        assert container_tree.status_code == 200
        assert all(node["active"] is False for node in container_tree.json()["nodes"] if node["kind"] == "file")

        assert not export_path.exists()


def test_iso_overwrite_and_burn_confirmation_are_idempotent(app_factory):
    with app_factory() as harness:
        collection_id = create_collection(harness, description="iso lifecycle archive")
        stage_collection_files(harness, collection_id, document_archive_files())

        sealed = seal_collection(harness, collection_id)
        container_id = (sealed["closed_containers"] or force_flush(harness))[0]

        first_iso = create_iso(harness, container_id, volume_label="FIRST LABEL")
        assert Path(first_iso["iso_path"]).exists()

        conflict = harness.client.post(
            f"/v1/containers/{container_id}/iso/create",
            headers=harness.auth_headers(),
            json={"overwrite": False},
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "iso already exists; pass overwrite=true to replace it"

        replaced_iso = create_iso(harness, container_id, overwrite=True, volume_label="SECOND LABEL")
        assert replaced_iso["iso_path"] == first_iso["iso_path"]
        assert replaced_iso["size_bytes"] == Path(replaced_iso["iso_path"]).stat().st_size

        confirmed_once = harness.client.post(
            f"/v1/containers/{container_id}/burn/confirm",
            headers=harness.auth_headers(),
        )
        assert confirmed_once.status_code == 200, confirmed_once.text

        confirmed_twice = harness.client.post(
            f"/v1/containers/{container_id}/burn/confirm",
            headers=harness.auth_headers(),
        )
        assert confirmed_twice.status_code == 200, confirmed_twice.text
        assert confirmed_twice.json()["burn_confirmed_at"] == confirmed_once.json()["burn_confirmed_at"]
        assert confirmed_twice.json()["released_collection_ids"] == []


def test_env_configured_webhook_backfills_existing_unconfirmed_containers(app_factory):
    with app_factory(CONTAINER_FINALIZATION_WEBHOOK_URL="http://example.test/archive-hook") as harness:
        collection_id = create_collection(harness, description="notification backfill archive")
        stage_collection_files(harness, collection_id, document_archive_files())

        sealed = seal_collection(harness, collection_id)
        container_id = (sealed["closed_containers"] or force_flush(harness))[0]

        with harness.session() as session:
            container = session.get(harness.models.Container, container_id)
            assert container is not None
            assert container.finalization_status == "pending"
            assert container.finalization_next_attempt_at is not None
