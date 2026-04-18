from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from .helpers import create_job, force_flush, register_iso, seal_job, upload_job_file
from .mock_data import document_archive_files


def test_registered_iso_download_tracks_progress(app_factory):
    with app_factory() as harness:
        job_id = create_job(harness, description="downloadable iso archive")
        for sample in document_archive_files():
            upload_job_file(harness, job_id, sample)

        sealed = seal_job(harness, job_id)
        disc_id = (sealed["closed_discs"] or force_flush(harness))[0]

        iso_bytes = b"SIMULATED-ISO-DATA" * 4096
        register_iso(harness, disc_id, iso_bytes)

        create_session = harness.client.post(
            f"/v1/discs/{disc_id}/download-sessions",
            headers=harness.auth_headers(),
        )
        assert create_session.status_code == 200
        session_id = create_session.json()["session_id"]

        download = harness.client.get(
            f"/v1/discs/downloads/{session_id}/content",
            headers=harness.auth_headers(),
        )
        assert download.status_code == 200
        assert download.content == iso_bytes
        assert download.headers["content-disposition"].endswith(f'"{disc_id}.iso"')

        stream_messages = harness.redis_messages(harness.progress.download_stream_name(session_id))
        statuses = [fields["status"] for _, fields in stream_messages]
        assert statuses[0] == "ready"
        assert statuses[-1] == "completed"
        assert "streaming" in statuses[1:-1]
        assert int(stream_messages[-1][1]["bytes_sent"]) == len(iso_bytes)

        with harness.session() as session:
            download_session = session.get(harness.models.DownloadSession, session_id)
            assert download_session is not None
            assert download_session.status == "completed"
            assert download_session.bytes_sent == len(iso_bytes)


def test_disc_finalization_webhook_payload_includes_disc_and_download_url(app_factory, monkeypatch):
    with app_factory(DISC_WEBHOOK_DISPATCH_INTERVAL_SECONDS="3600") as harness:
        delivered: list[tuple[str, dict]] = []

        def fake_post_webhook(url: str, payload: dict[str, object]) -> None:
            delivered.append((url, payload))

        monkeypatch.setattr(harness.notifications, "_post_webhook", fake_post_webhook)

        subscribe = harness.client.post(
            "/v1/discs/finalization-webhooks",
            headers=harness.auth_headers(),
            json={"webhook_url": "http://example.test/archive-hook", "reminder_interval_seconds": 900},
        )
        assert subscribe.status_code == 200, subscribe.text
        assert subscribe.json()["pending_disc_count"] == 0

        job_id = create_job(harness, description="finalization webhook archive")
        for sample in document_archive_files():
            upload_job_file(harness, job_id, sample)

        sealed = seal_job(harness, job_id)
        disc_id = (sealed["closed_discs"] or force_flush(harness))[0]

        delivered_count = harness.notifications.deliver_due_disc_finalization_notifications()
        assert delivered_count == 1
        assert len(delivered) == 1

        webhook_url, payload = delivered[0]
        assert webhook_url == "http://example.test/archive-hook"
        assert payload["event"] == "disc.finalized"
        assert payload["disc_id"] == disc_id
        assert payload["download_url"] == f"{harness.config.API_BASE_URL}/v1/discs/{disc_id}/iso/content"
        assert payload["request_burn_image_url"] == f"{harness.config.API_BASE_URL}/v1/discs/{disc_id}/iso/create"
        assert payload["iso_available"] is False
        assert payload["reminder_interval_seconds"] == 900
        assert payload["reminder_count"] == 0

        iso_bytes = b"ON-DEMAND-ISO" * 2048
        register_iso(harness, disc_id, iso_bytes)
        download_path = urlparse(str(payload["download_url"])).path
        download = harness.client.get(download_path, headers=harness.auth_headers())
        assert download.status_code == 200
        assert download.content == iso_bytes


def test_disc_finalization_webhook_reminders_stop_after_burn_confirmation(app_factory, monkeypatch):
    with app_factory(DISC_WEBHOOK_DISPATCH_INTERVAL_SECONDS="3600") as harness:
        delivered: list[dict[str, object]] = []

        def fake_post_webhook(_url: str, payload: dict[str, object]) -> None:
            delivered.append(payload)

        monkeypatch.setattr(harness.notifications, "_post_webhook", fake_post_webhook)

        subscribe = harness.client.post(
            "/v1/discs/finalization-webhooks",
            headers=harness.auth_headers(),
            json={"webhook_url": "http://example.test/archive-hook", "reminder_interval_seconds": 60},
        )
        assert subscribe.status_code == 200, subscribe.text

        job_id = create_job(harness, description="reminder archive")
        for sample in document_archive_files():
            upload_job_file(harness, job_id, sample)

        sealed = seal_job(harness, job_id)
        disc_id = (sealed["closed_discs"] or force_flush(harness))[0]

        initial_count = harness.notifications.deliver_due_disc_finalization_notifications()
        assert initial_count == 1
        assert [payload["event"] for payload in delivered] == ["disc.finalized"]

        with harness.session() as session:
            notification = session.query(harness.models.DiscFinalizationNotification).filter_by(disc_id=disc_id).one()
            notification.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.commit()

        reminder_count = harness.notifications.deliver_due_disc_finalization_notifications()
        assert reminder_count == 1
        assert [payload["event"] for payload in delivered] == [
            "disc.finalized",
            "disc.finalized.reminder",
        ]
        assert delivered[-1]["disc_id"] == disc_id
        assert delivered[-1]["reminder_count"] == 1

        register_iso(harness, disc_id, b"burnable-iso")
        confirm = harness.client.post(
            f"/v1/discs/{disc_id}/burn/confirm",
            headers=harness.auth_headers(),
        )
        assert confirm.status_code == 200, confirm.text

        with harness.session() as session:
            notification = session.query(harness.models.DiscFinalizationNotification).filter_by(disc_id=disc_id).one()
            assert notification.status == "completed"
            notification.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.commit()

        post_burn = harness.notifications.deliver_due_disc_finalization_notifications()
        assert post_burn == 0
        assert [payload["event"] for payload in delivered] == [
            "disc.finalized",
            "disc.finalized.reminder",
        ]


def test_schema_migration_adds_critical_columns(module_factory):
    def prepare_legacy_db(env: dict[str, str], _base_dir):
        sqlite_path = env["SQLITE_PATH"]
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(sqlite_path)
        conn.executescript(
            """
            CREATE TABLE jobs (
              id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              description TEXT,
              sealed_at DATETIME,
              created_at DATETIME NOT NULL
            );
            CREATE TABLE discs (
              id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              description TEXT,
              root_abs_path TEXT NOT NULL,
              contents_hash TEXT NOT NULL,
              total_root_bytes INTEGER NOT NULL,
              cached_root_abs_path TEXT,
              iso_abs_path TEXT,
              iso_size_bytes INTEGER,
              created_at DATETIME NOT NULL
            );
            CREATE TABLE disc_entries (
              id TEXT PRIMARY KEY,
              disc_id TEXT NOT NULL,
              relative_path TEXT NOT NULL,
              kind TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              sha256 TEXT NOT NULL
            );
            """
        )
        conn.commit()
        conn.close()

    with module_factory(before_import=prepare_legacy_db) as modules:
        modules.db.Base.metadata.create_all(bind=modules.db.engine)
        modules.db.migrate_schema()

        conn = sqlite3.connect(modules.sqlite_path)
        try:
            job_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            disc_columns = {row[1] for row in conn.execute("PRAGMA table_info(discs)")}
            disc_entry_columns = {row[1] for row in conn.execute("PRAGMA table_info(disc_entries)")}
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()

        assert "keep_buffer_after_archive" in job_columns
        assert "burn_confirmed_at" in disc_columns
        assert "stored_size_bytes" in disc_entry_columns
        assert "stored_sha256" in disc_entry_columns
        assert "disc_finalization_webhook_subscriptions" in tables
        assert "disc_finalization_notifications" in tables
