from __future__ import annotations

import importlib
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from .helpers import create_iso, create_collection, force_flush, register_iso, seal_collection, stage_collection_files
from .mock_data import MockFile, document_archive_files, patterned_bytes


def _stress_files(seed: int, *, count: int, oversized_bytes: int | None = None) -> list[MockFile]:
    rng = random.Random(seed)
    files: list[MockFile] = []
    for index in range(count):
        depth = 1 + rng.randrange(4)
        parents = [f"collection-{seed:02d}"] + [f"tier-{level}-{rng.randrange(5)}" for level in range(depth)]
        stem = f"asset-{index:02d}-" + ("segment-" * (1 + rng.randrange(3))) + f"{rng.randrange(10_000):04d}"
        ext = (".bin", ".pdf", ".mp4")[index % 3]
        size_bytes = rng.randint(18_000, 165_000)
        files.append(
            MockFile(
                "/".join([*parents, f"{stem}{ext}"]),
                patterned_bytes(f"stress-{seed}-{index}", size_bytes),
            )
        )
    if oversized_bytes is not None:
        files.append(
            MockFile(
                f"collection-{seed:02d}/oversized/master-reel-{seed:02d}.mov",
                patterned_bytes(f"stress-large-{seed}", oversized_bytes),
            )
        )
    return files


def test_registered_iso_download_endpoint_serves_registered_iso(app_factory):
    with app_factory() as harness:
        collection_id = create_collection(harness, description="downloadable iso archive")
        stage_collection_files(harness, collection_id, document_archive_files())

        sealed = seal_collection(harness, collection_id)
        container_id = (sealed["closed_containers"] or force_flush(harness))[0]

        iso_bytes = b"SIMULATED-ISO-DATA" * 4096
        register_iso(harness, container_id, iso_bytes)

        download = harness.client.get(
            f"/v1/containers/{container_id}/iso/content",
            headers=harness.auth_headers(),
        )
        assert download.status_code == 200
        assert download.content == iso_bytes
        assert download.headers["content-disposition"].endswith(f'"{container_id}.iso"')


def test_iso_size_estimate_matches_authored_iso_bytes(module_factory):
    with module_factory() as modules:
        iso_module = importlib.import_module("app.iso")
        cases = [
            document_archive_files(),
            _stress_files(7, count=18),
            _stress_files(19, count=28),
        ]

        for index, files in enumerate(cases, start=1):
            container_id = f"ESTIMATE-{index:02d}"
            root = modules.archive_root / "iso-estimate-cases" / container_id
            for sample in files:
                path = root / sample.relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(sample.content)

            estimated = iso_module.estimate_iso_size_from_container_root(root, requested_label=container_id)
            authored = iso_module.create_iso_from_container_root(container_id, root, requested_label=container_id)

            assert authored.stat().st_size == estimated


def test_containerization_never_overshoots_target_when_authoring_isos(app_factory):
    with app_factory(
        CONTAINER_TARGET_GB="0.0014",
        CONTAINER_FILL_GB="0.0013",
        CONTAINER_SPILL_FILL_GB="0.0011",
        CONTAINER_BUFFER_MAX_GB="0.0045",
    ) as harness:
        iso_module = importlib.import_module("app.iso")
        closed_container_ids: list[str] = []

        for index, seed in enumerate((5, 11, 23), start=1):
            collection_id = create_collection(harness, description=f"container stress archive {index}")
            stage_collection_files(
                harness,
                collection_id,
                _stress_files(seed, count=14, oversized_bytes=1_050_000 + (index * 90_000)),
            )
            closed_container_ids.extend(seal_collection(harness, collection_id)["closed_containers"])

        closed_container_ids.extend(force_flush(harness))
        deduped_container_ids = list(dict.fromkeys(closed_container_ids))
        assert len(deduped_container_ids) >= 4

        for container_id in deduped_container_ids:
            with harness.session() as session:
                container = session.get(harness.models.Container, container_id)
                assert container is not None
                root = Path(container.root_abs_path)

            estimated = iso_module.estimate_iso_size_from_container_root(root, requested_label=container_id)
            created = create_iso(harness, container_id)

            assert estimated == created["size_bytes"]
            assert created["size_bytes"] <= harness.config.CONTAINER_TARGET


def test_container_finalization_webhook_payload_includes_container_and_download_url(app_factory, monkeypatch):
    with app_factory(
        CONTAINER_WEBHOOK_DISPATCH_INTERVAL_SECONDS="3600",
        CONTAINER_FINALIZATION_WEBHOOK_URL="http://example.test/archive-hook",
        CONTAINER_FINALIZATION_REMINDER_INTERVAL_SECONDS="900",
    ) as harness:
        delivered: list[tuple[str, dict]] = []

        def fake_post_webhook(url: str, payload: dict[str, object]) -> None:
            delivered.append((url, payload))

        monkeypatch.setattr(harness.notifications, "_post_webhook", fake_post_webhook)

        collection_id = create_collection(harness, description="finalization webhook archive")
        stage_collection_files(harness, collection_id, document_archive_files())

        sealed = seal_collection(harness, collection_id)
        container_id = (sealed["closed_containers"] or force_flush(harness))[0]

        delivered_count = harness.notifications.deliver_due_container_finalization_notifications()
        assert delivered_count == 1
        assert len(delivered) == 1

        webhook_url, payload = delivered[0]
        assert webhook_url == "http://example.test/archive-hook"
        assert payload["event"] == "container.finalized"
        assert payload["container_id"] == container_id
        assert payload["download_url"] == f"{harness.config.API_BASE_URL}/v1/containers/{container_id}/iso/content"
        assert payload["request_burn_image_url"] == f"{harness.config.API_BASE_URL}/v1/containers/{container_id}/iso/create"
        assert payload["iso_available"] is False
        assert payload["reminder_interval_seconds"] == 900
        assert payload["reminder_count"] == 0

        iso_bytes = b"ON-DEMAND-ISO" * 2048
        register_iso(harness, container_id, iso_bytes)
        download_path = urlparse(str(payload["download_url"])).path
        download = harness.client.get(download_path, headers=harness.auth_headers())
        assert download.status_code == 200
        assert download.content == iso_bytes


def test_container_finalization_webhook_reminders_stop_after_burn_confirmation(app_factory, monkeypatch):
    with app_factory(
        CONTAINER_WEBHOOK_DISPATCH_INTERVAL_SECONDS="3600",
        CONTAINER_FINALIZATION_WEBHOOK_URL="http://example.test/archive-hook",
        CONTAINER_FINALIZATION_REMINDER_INTERVAL_SECONDS="60",
    ) as harness:
        delivered: list[dict[str, object]] = []

        def fake_post_webhook(_url: str, payload: dict[str, object]) -> None:
            delivered.append(payload)

        monkeypatch.setattr(harness.notifications, "_post_webhook", fake_post_webhook)

        collection_id = create_collection(harness, description="reminder archive")
        stage_collection_files(harness, collection_id, document_archive_files())

        sealed = seal_collection(harness, collection_id)
        container_id = (sealed["closed_containers"] or force_flush(harness))[0]

        initial_count = harness.notifications.deliver_due_container_finalization_notifications()
        assert initial_count == 1
        assert [payload["event"] for payload in delivered] == ["container.finalized"]

        with harness.session() as session:
            container = session.get(harness.models.Container, container_id)
            assert container is not None
            container.finalization_next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.commit()

        reminder_count = harness.notifications.deliver_due_container_finalization_notifications()
        assert reminder_count == 1
        assert [payload["event"] for payload in delivered] == [
            "container.finalized",
            "container.finalized.reminder",
        ]
        assert delivered[-1]["container_id"] == container_id
        assert delivered[-1]["reminder_count"] == 1

        register_iso(harness, container_id, b"burnable-iso")
        confirm = harness.client.post(
            f"/v1/containers/{container_id}/burn/confirm",
            headers=harness.auth_headers(),
        )
        assert confirm.status_code == 200, confirm.text

        with harness.session() as session:
            container = session.get(harness.models.Container, container_id)
            assert container is not None
            assert container.finalization_status == "completed"
            container.finalization_next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.commit()

        post_burn = harness.notifications.deliver_due_container_finalization_notifications()
        assert post_burn == 0
        assert [payload["event"] for payload in delivered] == [
            "container.finalized",
            "container.finalized.reminder",
        ]


def test_schema_bootstraps_current_tables(module_factory):
    with module_factory() as modules:
        modules.config.ensure_directories()
        modules.db.Base.metadata.create_all(bind=modules.db.engine)
        modules.db.migrate_schema()

        conn = sqlite3.connect(modules.sqlite_path)
        try:
            collection_columns = {row[1] for row in conn.execute("PRAGMA table_info(collections)")}
            container_columns = {row[1] for row in conn.execute("PRAGMA table_info(containers)")}
            container_entry_columns = {row[1] for row in conn.execute("PRAGMA table_info(container_entries)")}
        finally:
            conn.close()

        assert "keep_buffer_after_archive" in collection_columns
        assert "burn_confirmed_at" in container_columns
        assert "active_root_abs_path" in container_columns
        assert "finalization_status" in container_columns
        assert "finalization_next_attempt_at" in container_columns
        assert "stored_size_bytes" in container_entry_columns
        assert "stored_sha256" in container_entry_columns
