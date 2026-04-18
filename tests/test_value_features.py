from __future__ import annotations

from pathlib import Path

from .helpers import activation_container_from_root, create_iso, create_collection, force_flush, seal_collection, upload_collection_file
from .mock_data import MockFile, document_archive_files, patterned_bytes


def test_catalog_tree_keeps_explicit_and_derived_directories(app_factory):
    with app_factory() as harness:
        collection_id = create_collection(harness, description="catalog coverage archive")

        created = harness.client.post(
            f"/v1/collections/{collection_id}/directories",
            headers=harness.auth_headers(),
            json={"relative_path": "scans/2025/receipts"},
        )
        assert created.status_code == 200, created.text

        sample = MockFile(
            "scans/2025/passports/alex/passport.pdf",
            patterned_bytes("passport-scan", 24_000),
        )
        upload_collection_file(harness, collection_id, sample)

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
                "source": "buffer",
                "container_ids": [],
                "status": "active",
                "extra": None,
            }
        ]


def test_upload_mtime_must_be_rfc3339_utc(app_factory):
    with app_factory() as harness:
        collection_id = create_collection(harness, description="timestamp validation archive")
        sample = document_archive_files()[0]
        payload = sample.upload_payload()

        for bad_mtime in ("2026-04-17T10:15:30+02:00", "2026-04-17T10:15:30"):
            response = harness.client.post(
                f"/v1/collections/{collection_id}/uploads",
                headers=harness.auth_headers(),
                json={**payload, "relative_path": f"bad/{bad_mtime[-6:].replace(':', '-')}.pdf", "mtime": bad_mtime},
            )
            assert response.status_code == 400
            assert response.json()["detail"] == "mtime must be an RFC3339 UTC timestamp"

        accepted = harness.client.post(
            f"/v1/collections/{collection_id}/uploads",
            headers=harness.auth_headers(),
            json=payload,
        )
        assert accepted.status_code == 200, accepted.text


def test_sealed_collection_rejects_new_mutations(app_factory):
    with app_factory() as harness:
        sample = document_archive_files()[0]
        collection_id = create_collection(harness, description="sealed archive behavior")
        upload_collection_file(harness, collection_id, sample)
        seal_collection(harness, collection_id)

        add_directory = harness.client.post(
            f"/v1/collections/{collection_id}/directories",
            headers=harness.auth_headers(),
            json={"relative_path": "late-arrival"},
        )
        assert add_directory.status_code == 409
        assert add_directory.json()["detail"] == "collection is sealed"

        add_upload = harness.client.post(
            f"/v1/collections/{collection_id}/uploads",
            headers=harness.auth_headers(),
            json=sample.upload_payload(),
        )
        assert add_upload.status_code == 409
        assert add_upload.json()["detail"] == "collection is sealed"

        reseal = harness.client.post(
            f"/v1/collections/{collection_id}/seal",
            headers=harness.auth_headers(),
        )
        assert reseal.status_code == 409
        assert reseal.json()["detail"] == "collection already sealed"

        bundle = harness.client.get(
            f"/v1/collections/{collection_id}/hash-manifest-proof",
            headers=harness.auth_headers(),
        )
        assert bundle.status_code == 200


def test_container_activation_and_evict_toggle_container_and_collection_visibility(app_factory):
    with app_factory() as harness:
        samples = document_archive_files()
        collection_id = create_collection(harness, description="activation visibility archive")
        for sample in samples:
            upload_collection_file(harness, collection_id, sample)

        sealed = seal_collection(harness, collection_id)
        container_id = (sealed["closed_containers"] or force_flush(harness))[0]

        release = harness.client.post(
            f"/v1/collections/{collection_id}/buffer/release",
            headers=harness.auth_headers(),
        )
        assert release.status_code == 200, release.text

        with harness.session() as session:
            container_entry = (
                session.query(harness.models.ContainerEntry)
                .filter_by(container_id=container_id, kind="payload")
                .order_by(harness.models.ContainerEntry.relative_path.asc())
                .first()
            )
            assert container_entry is not None
            container_relpath = container_entry.relative_path

        inactive_container = harness.client.get(
            f"/v1/containers/{container_id}/content/{container_relpath}",
            headers=harness.auth_headers(),
        )
        assert inactive_container.status_code == 409
        assert inactive_container.json()["error"] == "container_inactive"

        _, complete = activation_container_from_root(harness, container_id)
        assert complete.status_code == 200, complete.text

        active_container = harness.client.get(
            f"/v1/containers/{container_id}/content/{container_relpath}",
            headers=harness.auth_headers(),
        )
        assert active_container.status_code == 200

        active_collection = harness.client.get(
            f"/v1/collections/{collection_id}/content/{samples[0].relative_path}",
            headers=harness.auth_headers(),
        )
        assert active_collection.status_code == 200
        assert active_collection.content == samples[0].content

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

        inactive_again = harness.client.get(
            f"/v1/collections/{collection_id}/content/{samples[0].relative_path}",
            headers=harness.auth_headers(),
        )
        assert inactive_again.status_code == 409
        assert inactive_again.json()["error"] == "inactive_on_container"
        assert inactive_again.json()["container_ids"] == [container_id]


def test_iso_overwrite_and_burn_confirmation_are_idempotent(app_factory):
    with app_factory() as harness:
        collection_id = create_collection(harness, description="iso lifecycle archive")
        for sample in document_archive_files():
            upload_collection_file(harness, collection_id, sample)

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


def test_webhook_subscription_backfills_existing_unconfirmed_containers(app_factory):
    with app_factory() as harness:
        collection_id = create_collection(harness, description="notification backfill archive")
        for sample in document_archive_files():
            upload_collection_file(harness, collection_id, sample)

        sealed = seal_collection(harness, collection_id)
        container_id = (sealed["closed_containers"] or force_flush(harness))[0]

        subscribe = harness.client.post(
            "/v1/containers/finalization-webhooks",
            headers=harness.auth_headers(),
            json={"webhook_url": "http://example.test/archive-hook"},
        )
        assert subscribe.status_code == 200, subscribe.text
        assert subscribe.json()["pending_container_count"] == 1

        with harness.session() as session:
            notification = session.query(harness.models.ContainerFinalizationNotification).filter_by(container_id=container_id).one()
            assert notification.status == "pending"
            assert notification.next_attempt_at is not None
