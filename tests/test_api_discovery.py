from __future__ import annotations

from .helpers import create_collection, force_flush, register_iso, seal_collection, stage_collection_files
from .mock_data import family_archive_files


def test_collection_and_container_list_endpoints_surface_summary_state(app_factory):
    with app_factory() as harness:
        sample = family_archive_files()[0]
        collection_id = create_collection(
            harness,
            description="discovery archive",
            keep_buffer_after_archive=True,
        )
        stage_collection_files(harness, collection_id, [sample])

        collections = harness.client.get("/v1/collections", headers=harness.auth_headers())
        assert collections.status_code == 200, collections.text

        body = collections.json()
        assert len(body["collections"]) == 1
        assert body["collections"][0] == {
            "collection_id": collection_id,
            "status": "open",
            "description": "discovery archive",
            "keep_buffer_after_archive": True,
            "file_count": 1,
            "directory_count": len(sample.relative_path.split("/")) - 1,
            "created_at": body["collections"][0]["created_at"],
            "sealed_at": None,
            "intake_path": str(harness.storage.collection_intake_root(collection_id)),
            "export_path": str(harness.storage.export_collection_root(collection_id)),
            "hash_manifest_path": None,
            "hash_proof_path": None,
        }
        assert body["collections"][0]["created_at"].endswith("Z")

        sealed = seal_collection(harness, collection_id)
        container_id = (sealed["closed_containers"] or force_flush(harness))[0]
        register_iso(harness, container_id, b"registered-iso")

        containers = harness.client.get("/v1/containers", headers=harness.auth_headers())
        assert containers.status_code == 200, containers.text

        payload = containers.json()
        assert len(payload["containers"]) == 1
        assert payload["containers"][0]["container_id"] == container_id
        assert payload["containers"][0]["status"] in {"inactive", "active"}
        assert payload["containers"][0]["entry_count"] > 0
        assert payload["containers"][0]["active_root_present"] is False
        assert payload["containers"][0]["iso_present"] is True
        assert payload["containers"][0]["iso_size_bytes"] == len(b"registered-iso")
        assert payload["containers"][0]["root_path"]
        assert payload["containers"][0]["active_root_path"] is None
        assert payload["containers"][0]["iso_path"]
        assert payload["containers"][0]["burn_confirmed_at"] is None
        assert payload["containers"][0]["created_at"].endswith("Z")

        pool = harness.client.get("/v1/containers/pool", headers=harness.auth_headers())
        assert pool.status_code == 200, pool.text
        assert pool.json()["state"] in {"empty", "ready", "waiting", "overflow"}
