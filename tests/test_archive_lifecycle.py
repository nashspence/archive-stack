from __future__ import annotations

from pathlib import Path

import yaml

from .helpers import cache_disc_from_root, closed_disc_roots, create_job, force_flush, register_iso, seal_job, upload_job_file
from .mock_data import document_archive_files, family_archive_files, oversized_master_reel


def test_closed_disc_can_be_cached_back_and_restore_job_reads(app_factory):
    with app_factory() as harness:
        job_id = create_job(harness, description="family and finance archive")
        for sample in family_archive_files():
            upload_job_file(harness, job_id, sample)

        sealed = seal_job(harness, job_id)
        disc_ids = sealed["closed_discs"] or force_flush(harness)
        assert len(disc_ids) == 1
        disc_id = disc_ids[0]

        release = harness.client.post(
            f"/v1/jobs/{job_id}/buffer/release",
            headers=harness.auth_headers(),
        )
        assert release.status_code == 200

        offline = harness.client.get(
            f"/v1/jobs/{job_id}/content/{family_archive_files()[0].relative_path}",
            headers=harness.auth_headers(),
        )
        assert offline.status_code == 409
        assert offline.json()["error"] == "offline_on_disc"
        assert offline.json()["disc_ids"] == [disc_id]

        _, complete = cache_disc_from_root(harness, disc_id)
        assert complete.status_code == 200
        assert complete.json()["status"] == "cached"

        restored = harness.client.get(
            f"/v1/jobs/{job_id}/content/{family_archive_files()[0].relative_path}",
            headers=harness.auth_headers(),
        )
        assert restored.status_code == 200
        assert restored.content == family_archive_files()[0].content

        disc_tree = harness.client.get(
            f"/v1/discs/{disc_id}/tree",
            headers=harness.auth_headers(),
        )
        assert disc_tree.status_code == 200
        assert any(node["path"] == "MANIFEST.yml" for node in disc_tree.json()["nodes"])
        assert any(node["path"] == "README.txt" for node in disc_tree.json()["nodes"])

        disc_roots = closed_disc_roots(harness, disc_ids)
        manifest_path = disc_roots[disc_id] / "MANIFEST.yml"
        sidecar_path = next((disc_roots[disc_id] / "files").glob("*.meta.yaml"))
        readme_path = disc_roots[disc_id] / "README.txt"
        assert manifest_path.exists()
        assert manifest_path.read_bytes().startswith(b"age-encryption.org/")
        assert sidecar_path.read_bytes().startswith(b"age-encryption.org/")
        assert readme_path.read_text().startswith(f"Archive disc: {disc_id}")
        assert "age -d -j batchpass MANIFEST.yml" in readme_path.read_text()

        with harness.session() as session:
            disc = session.get(harness.models.Disc, disc_id)
            assert disc is not None
            assert disc.cached_root_abs_path is not None
            cached_manifest = Path(disc.cached_root_abs_path) / "MANIFEST.yml"
            cached_sidecar = next((Path(disc.cached_root_abs_path) / "files").glob("*.meta.yaml"))
            cached_readme = Path(disc.cached_root_abs_path) / "README.txt"
            manifest = yaml.safe_load(cached_manifest.read_text())
            assert manifest["schema"] == "manifest/v1"
            assert manifest["partition"] == disc_id
            assert len(manifest["jobs"]) == 1
            assert manifest["jobs"][0]["name"] == job_id
            assert all("path" in file_entry and "sha256" in file_entry for file_entry in manifest["jobs"][0]["files"])
            assert any(isinstance(file_entry.get("archive"), str) for file_entry in manifest["jobs"][0]["files"])
            assert cached_sidecar.read_text().startswith("schema: sidecar/v1")
            assert cached_readme.read_text().startswith(f"Archive disc: {disc_id}")


def test_cache_verification_rejects_mutated_partition_contents(app_factory):
    with app_factory() as harness:
        job_id = create_job(harness, description="financial document set")
        for sample in document_archive_files():
            upload_job_file(harness, job_id, sample)

        sealed = seal_job(harness, job_id)
        disc_id = (sealed["closed_discs"] or force_flush(harness))[0]

        def mutate(relpath: str, content: bytes) -> bytes:
            if relpath.endswith(".meta.yaml"):
                return content
            return content[:-1] + bytes([content[-1] ^ 0x01])

        _, complete = cache_disc_from_root(harness, disc_id, mutate=mutate)
        assert complete.status_code == 409
        assert complete.json()["detail"] == "uploaded root does not match the known partition contents"

        with harness.session() as session:
            cache_session = session.query(harness.models.CacheSession).order_by(harness.models.CacheSession.created_at.desc()).first()
            assert cache_session is not None
            assert cache_session.status == "failed"


def test_split_file_materializes_only_when_all_required_discs_are_cached(app_factory):
    with app_factory(
        PARTITION_TARGET_GB="0.0015",
        PARTITION_FILL_GB="0.0009",
        PARTITION_SPILL_FILL_GB="0.0008",
        PARTITION_BUFFER_MAX_GB="0.0040",
    ) as harness:
        master = oversized_master_reel()
        job_id = create_job(harness, description="master home video reel")
        upload_job_file(harness, job_id, master)

        sealed = seal_job(harness, job_id)
        disc_ids = sealed["closed_discs"] + force_flush(harness)
        assert len(disc_ids) >= 2

        release = harness.client.post(
            f"/v1/jobs/{job_id}/buffer/release",
            headers=harness.auth_headers(),
        )
        assert release.status_code == 200

        cache_disc_from_root(harness, disc_ids[0])

        partially_online = harness.client.get(
            f"/v1/jobs/{job_id}/content/{master.relative_path}",
            headers=harness.auth_headers(),
        )
        assert partially_online.status_code == 409
        assert partially_online.json()["error"] == "offline_on_disc"
        assert sorted(partially_online.json()["disc_ids"]) == sorted(disc_ids)

        for disc_id in disc_ids[1:]:
            _, complete = cache_disc_from_root(harness, disc_id)
            assert complete.status_code == 200

        restored = harness.client.get(
            f"/v1/jobs/{job_id}/content/{master.relative_path}",
            headers=harness.auth_headers(),
        )
        assert restored.status_code == 200
        assert restored.content == master.content

        with harness.session() as session:
            job_file = session.query(harness.models.JobFile).filter_by(job_id=job_id).one()
            materialized_path = Path(job_file.materialized_abs_path)
            assert materialized_path.exists()
            assert materialized_path.read_bytes() == master.content


def test_buffer_cleanup_waits_for_all_disc_burns_and_respects_retention_override(app_factory):
    with app_factory(
        PARTITION_TARGET_GB="0.0015",
        PARTITION_FILL_GB="0.0009",
        PARTITION_SPILL_FILL_GB="0.0008",
        PARTITION_BUFFER_MAX_GB="0.0040",
    ) as harness:
        master = oversized_master_reel()
        job_id = create_job(harness, description="critical family reel")
        upload_job_file(harness, job_id, master)
        sealed = seal_job(harness, job_id)
        disc_ids = sealed["closed_discs"] + force_flush(harness)
        assert len(disc_ids) >= 2

        for disc_id in disc_ids:
            iso = register_iso(harness, disc_id, f"iso-{disc_id}".encode())
            assert iso["size_bytes"] > 0

        for disc_id in disc_ids[:-1]:
            confirm = harness.client.post(
                f"/v1/discs/{disc_id}/burn/confirm",
                headers=harness.auth_headers(),
            )
            assert confirm.status_code == 200
            assert confirm.json()["released_job_ids"] == []

        with harness.session() as session:
            job_file = session.query(harness.models.JobFile).filter_by(job_id=job_id).one()
            assert Path(job_file.buffer_abs_path).exists()

        last_confirm = harness.client.post(
            f"/v1/discs/{disc_ids[-1]}/burn/confirm",
            headers=harness.auth_headers(),
        )
        assert last_confirm.status_code == 200
        assert job_id in last_confirm.json()["released_job_ids"]

        with harness.session() as session:
            job_file = session.query(harness.models.JobFile).filter_by(job_id=job_id).one()
            assert job_file.buffer_abs_path is None

    with app_factory() as retained_harness:
        job_id = create_job(
            retained_harness,
            description="archive with retention lock",
            keep_buffer_after_archive=True,
        )
        for sample in document_archive_files():
            upload_job_file(retained_harness, job_id, sample)

        sealed = seal_job(retained_harness, job_id)
        disc_ids = sealed["closed_discs"] or force_flush(retained_harness)
        for disc_id in disc_ids:
            register_iso(retained_harness, disc_id, f"iso-{disc_id}".encode())
            confirm = retained_harness.client.post(
                f"/v1/discs/{disc_id}/burn/confirm",
                headers=retained_harness.auth_headers(),
            )
            assert confirm.status_code == 200
            assert job_id not in confirm.json()["released_job_ids"]

        with retained_harness.session() as session:
            job_files = session.query(retained_harness.models.JobFile).filter_by(job_id=job_id).all()
            assert all(job_file.buffer_abs_path for job_file in job_files)
