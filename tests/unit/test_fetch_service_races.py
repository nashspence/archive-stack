from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionFileRecord,
    CollectionRecord,
    FetchEntryRecord,
    FetchRecord,
    FetchSelectorRecord,
    FileCopyRecord,
    FinalizedImageRecord,
    GlacierRecoverySessionCollectionRecord,
    GlacierRecoverySessionRecord,
)
from riverhog_core.domain.enums import FetchState, RecoverySessionState
from riverhog_core.domain.errors import InvalidState
from riverhog_core.recovery_payloads import encrypt_recovery_payload
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.fetches import SqlAlchemyFetchService, _sync_upload_progress
from tests.fixtures.crypto import FixtureRecoveryPayloadCodec
from tests.unit.db_helpers import sqlite_url

_RECOVERY_CODEC = FixtureRecoveryPayloadCodec()


def _add_fetch(
    session,
    *,
    target: str,
    fetch_id: str,
    fetch_order: int = 1,
    fetch_state: str,
) -> None:
    session.add(
        FetchRecord(
            fetch_id=fetch_id,
            name=target,
            fetch_order=fetch_order,
            fetch_state=fetch_state,
        )
    )
    session.add(
        FetchSelectorRecord(
            fetch_id=fetch_id,
            target=target,
            selector_order=1,
        )
    )


class _FakeHotStore:
    def __init__(self, files: dict[tuple[str, str], bytes]) -> None:
        self._files = dict(files)

    def put_collection_file(self, collection_id: str, path: str, content: bytes) -> None:
        self._files[(collection_id, path)] = content

    def put_collection_file_stream(
        self,
        collection_id: str,
        path: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
    ) -> None:
        content = b"".join(chunks)
        assert len(content) == content_length
        self._files[(collection_id, path)] = content

    def get_collection_file(self, collection_id: str, path: str) -> bytes:
        key = (collection_id, path)
        if key not in self._files:
            raise FileNotFoundError(f"{collection_id}/{path}")
        return self._files[key]

    def has_collection_file(self, collection_id: str, path: str) -> bool:
        return (collection_id, path) in self._files

    def delete_collection_file(self, collection_id: str, path: str) -> None:
        self._files.pop((collection_id, path), None)

    def list_collection_files(self, collection_id: str) -> list[tuple[str, int]]:
        return sorted(
            [
                (path, len(content))
                for (stored_collection_id, path), content in self._files.items()
                if stored_collection_id == collection_id
            ]
        )


class _RaceyUploadStore:
    def __init__(self, target_payloads: dict[str, bytes]) -> None:
        self._target_payloads = dict(target_payloads)
        self.cancelled_uploads: list[str] = []
        self.deleted_targets: list[str] = []

    def create_upload(self, target_path: str, length: int) -> str:
        raise AssertionError("create_upload should not be called")

    def get_offset(self, tus_url: str) -> int:
        return -1

    def append_upload_chunk(
        self,
        tus_url: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> tuple[int, str | None]:
        raise AssertionError("append_upload_chunk should not be called")

    def read_target(self, target_path: str) -> bytes:
        if target_path not in self._target_payloads:
            raise FileNotFoundError(target_path)
        return self._target_payloads[target_path]

    def iter_target(
        self,
        target_path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        content = self.read_target(target_path)
        yield content[offset:] if size is None else content[offset : offset + size]

    def delete_target(self, target_path: str) -> None:
        self.deleted_targets.append(target_path)
        self._target_payloads.pop(target_path, None)

    def cancel_upload(self, tus_url: str) -> None:
        self.cancelled_uploads.append(tus_url)


def _config(sqlite_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        object_store="s3",
        s3_endpoint_url="http://example.invalid:9000",
        s3_region="us-east-1",
        s3_bucket="riverhog",
        s3_access_key_id="test-access",
        s3_secret_access_key="test-secret",
        s3_force_path_style=True,
        tusd_base_url="http://example.invalid:1080/files",
        tusd_hook_secret="hook-secret",
        database_url=sqlite_url(sqlite_path),
    )


def test_done_fetch_summary_uses_aggregate_stats_without_materializing_entries(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    collection_id = "docs"
    files = {f"camera/file-{index:04d}.dat": f"payload-{index}\n".encode() for index in range(100)}
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        session.add(CollectionRecord(id=collection_id))
        for path, content in files.items():
            session.add(
                CollectionFileRecord(
                    collection_id=collection_id,
                    path=path,
                    bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    hot=True,
                    archived=True,
                )
            )
        _add_fetch(
            session,
            target=f"{collection_id}/",
            fetch_id="fx-done",
            fetch_state=FetchState.DONE.value,
        )

    service = SqlAlchemyFetchService(
        _config(sqlite_path),
        _FakeHotStore({(collection_id, path): content for path, content in files.items()}),
        _RaceyUploadStore({}),
        _RECOVERY_CODEC,
    )

    summary = service.get("fx-done")

    assert summary.state == FetchState.DONE
    assert summary.files == len(files)
    assert summary.bytes == sum(len(content) for content in files.values())
    assert summary.entries_total == len(files)
    assert summary.entries_uploaded == len(files)
    assert summary.entries_pending == 0
    assert summary.uploaded_bytes == summary.bytes
    assert summary.missing_bytes == 0
    assert summary.copies == []

    with session_scope(session_factory) as session:
        assert session.scalars(select(FetchEntryRecord)).all() == []


def test_waiting_fetch_status_uses_pending_preview_without_materializing_entries(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    collection_id = "docs"
    files = {f"camera/file-{index:04d}.dat": f"payload-{index}\n".encode() for index in range(100)}
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        session.add(CollectionRecord(id=collection_id))
        for path, content in files.items():
            session.add(
                CollectionFileRecord(
                    collection_id=collection_id,
                    path=path,
                    bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    hot=False,
                    archived=True,
                )
            )
        _add_fetch(
            session,
            target=f"{collection_id}/",
            fetch_id="fx-waiting",
            fetch_state=FetchState.QUEUED_DJDAN.value,
        )

    service = SqlAlchemyFetchService(
        _config(sqlite_path),
        _FakeHotStore({}),
        _RaceyUploadStore({}),
        _RECOVERY_CODEC,
    )

    summary = service.get("fx-waiting")

    assert summary.state == FetchState.QUEUED_DJDAN
    assert summary.entries_total == len(files)
    assert summary.entries_pending == len(files)
    assert summary.copies == []

    status = service.status("fx-waiting", limit=3)

    assert status["state"] == FetchState.QUEUED_DJDAN.value
    assert status["entries_total"] == len(files)
    assert status["entries_pending"] == len(files)
    assert status["entries_returned"] == 3
    assert [
        (entry["id"], entry["collection_id"], entry["upload_state"])
        for entry in status["entries"]
        if isinstance(entry, dict)
    ] == [
        ("e1", collection_id, "pending"),
        ("e2", collection_id, "pending"),
        ("e3", collection_id, "pending"),
    ]

    with session_scope(session_factory) as session:
        assert session.scalars(select(FetchEntryRecord)).all() == []


def test_waiting_fetch_notifications_are_delivered_and_reminded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    collection_id = "docs"
    path = "file.txt"
    target = f"{collection_id}/{path}"
    content = b"needs optical recovery\n"
    encrypted = encrypt_recovery_payload(content, _RECOVERY_CODEC)
    sha256 = hashlib.sha256(content).hexdigest()

    with session_scope(make_session_factory(sqlite_url(sqlite_path))) as session:
        session.add(CollectionRecord(id=collection_id))
        session.add(
            CollectionFileRecord(
                collection_id=collection_id,
                path=path,
                bytes=len(content),
                sha256=sha256,
                hot=False,
                archived=True,
            )
        )
        session.add(
            FileCopyRecord(
                collection_id=collection_id,
                path=path,
                copy_id="20260530T000000Z-1",
                volume_id="20260530T000000Z",
                location="red binder",
                disc_path="files/000001.age",
                enc_json="{}",
                recovery_bytes=len(encrypted),
                recovery_sha256=hashlib.sha256(encrypted).hexdigest(),
            )
        )
        _add_fetch(
            session,
            target=target,
            fetch_id="fx-1",
            fetch_state=FetchState.QUEUED_DJDAN.value,
        )
        session.add(
            FetchEntryRecord(
                fetch_id="fx-1",
                entry_id="e1",
                entry_order=1,
                collection_id=collection_id,
                path=path,
                bytes=len(content),
                sha256=sha256,
                recovery_bytes=len(encrypted),
                uploaded_bytes=0,
                upload_expires_at=None,
                tus_url=None,
            )
        )

    config = replace(
        _config(sqlite_path),
        operator_webhook_url="http://example.invalid/webhooks/operator",
        public_base_url="https://riverhog.example.test",
        operator_webhook_retry_delay=timedelta(seconds=1),
        operator_webhook_reminder_interval=timedelta(seconds=5),
    )
    service = SqlAlchemyFetchService(
        config,
        _FakeHotStore({}),
        _RaceyUploadStore({}),
        _RECOVERY_CODEC,
    )
    payloads: list[dict[str, object]] = []

    def _post_webhook(*, config, payload):
        payloads.append(payload)

    start = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("riverhog_core.services.fetches.utcnow", lambda: start)
    monkeypatch.setattr("riverhog_core.services.fetches.post_webhook", _post_webhook)

    assert service.deliver_due_queued_notifications(limit=10) == 1
    assert payloads[-1]["event"] == "fetches.queued_djdan"
    assert payloads[-1]["operator_action"] == "Run `djdan fetch fx-1`"

    monkeypatch.setattr(
        "riverhog_core.services.fetches.utcnow",
        lambda: start + timedelta(seconds=6),
    )
    assert service.deliver_due_queued_notifications(limit=10) == 1
    assert payloads[-1]["event"] == "fetches.queued_djdan.reminder"
    assert payloads[-1]["reminder_count"] == 1


def test_active_cloud_fetch_suppresses_media_fetch_notifications_and_uploads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    collection_id = "docs"
    path = "file.txt"
    target = f"{collection_id}/{path}"
    content = b"needs cloud recovery\n"
    encrypted = encrypt_recovery_payload(content, _RECOVERY_CODEC)
    sha256 = hashlib.sha256(content).hexdigest()

    with session_scope(make_session_factory(sqlite_url(sqlite_path))) as session:
        session.add(CollectionRecord(id=collection_id))
        session.add(
            CollectionFileRecord(
                collection_id=collection_id,
                path=path,
                bytes=len(content),
                sha256=sha256,
                hot=False,
                archived=True,
            )
        )
        _add_fetch(
            session,
            target=target,
            fetch_id="fx-1",
            fetch_state=FetchState.QUEUED_DJDAN.value,
        )
        session.add(
            FetchEntryRecord(
                fetch_id="fx-1",
                entry_id="e1",
                entry_order=1,
                collection_id=collection_id,
                path=path,
                bytes=len(content),
                sha256=sha256,
                recovery_bytes=len(encrypted),
                uploaded_bytes=0,
                upload_expires_at=None,
                tus_url=None,
            )
        )
        session.add(
            GlacierRecoverySessionRecord(
                session_id="rs-docs-restore-1",
                type="collection_restore",
                state=RecoverySessionState.RESTORE_REQUESTED.value,
                created_at="2026-05-31T12:00:00Z",
                restore_requested_at="2026-05-31T12:00:00Z",
                restore_ready_at=None,
                restore_next_poll_at=None,
                restore_expires_at=None,
                completed_at=None,
                canceled_at=None,
                paused_at=None,
                paused_from_state=None,
                latest_message=None,
                retrieval_tier="Bulk",
                hold_days=7,
                warnings_json="[]",
                restore_paths_json=json.dumps([path]),
            )
        )
        session.add(
            GlacierRecoverySessionCollectionRecord(
                session_id="rs-docs-restore-1",
                collection_id=collection_id,
                collection_order=0,
            )
        )

    config = replace(
        _config(sqlite_path),
        operator_webhook_url="http://example.invalid/webhooks/operator",
        public_base_url="https://riverhog.example.test",
    )
    service = SqlAlchemyFetchService(
        config,
        _FakeHotStore({}),
        _RaceyUploadStore({}),
        _RECOVERY_CODEC,
    )
    payloads: list[dict[str, object]] = []

    def _post_webhook(*, config, payload):
        payloads.append(payload)

    monkeypatch.setattr("riverhog_core.services.fetches.post_webhook", _post_webhook)

    assert service.deliver_due_queued_notifications(limit=10) == 0
    assert payloads == []
    with pytest.raises(InvalidState, match="actively being cloud-fetched"):
        service.create_or_resume_upload("fx-1", "e1")
    with pytest.raises(InvalidState, match="actively being cloud-fetched"):
        service.append_upload_chunk("fx-1", "e1", offset=0, checksum="", content=b"partial")
    with pytest.raises(InvalidState, match="actively being cloud-fetched"):
        service.complete("fx-1")


def test_stale_sync_does_not_rollback_completed_fetch_state(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    collection_id = "docs"
    path = "file.txt"
    target = f"{collection_id}/{path}"
    content = b"invoice payload\n"
    sha256 = hashlib.sha256(content).hexdigest()
    encrypted = encrypt_recovery_payload(content, _RECOVERY_CODEC)
    target_path = "/.riverhog/uploads/recovery/fx-1/e1.enc"
    tus_url = "/uploads/fx-1/e1"

    hot_store = _FakeHotStore({(collection_id, path): content})
    upload_store = _RaceyUploadStore({target_path: encrypted})
    config = RuntimeConfig(
        object_store="s3",
        s3_endpoint_url="http://example.invalid:9000",
        s3_region="us-east-1",
        s3_bucket="riverhog",
        s3_access_key_id="test-access",
        s3_secret_access_key="test-secret",
        s3_force_path_style=True,
        tusd_base_url="http://example.invalid:1080/files",
        tusd_hook_secret="hook-secret",
        database_url=sqlite_url(sqlite_path),
    )
    service = SqlAlchemyFetchService(config, hot_store, upload_store, _RECOVERY_CODEC)
    session_factory = make_session_factory(sqlite_url(sqlite_path))

    with session_scope(session_factory) as session:
        session.add(CollectionRecord(id=collection_id))
        session.add(
            CollectionFileRecord(
                collection_id=collection_id,
                path=path,
                bytes=len(content),
                sha256=sha256,
                hot=False,
                archived=True,
            )
        )
        session.add(
            FileCopyRecord(
                collection_id=collection_id,
                path=path,
                copy_id="copy-1",
                volume_id="vol-1",
                location="vault-a/shelf-01",
                disc_path="files/000001.age",
                enc_json="{}",
                part_index=None,
                part_count=None,
                part_bytes=None,
                part_sha256=None,
                recovery_bytes=len(encrypted),
                recovery_sha256=hashlib.sha256(encrypted).hexdigest(),
            )
        )
        _add_fetch(
            session,
            target=target,
            fetch_id="fx-1",
            fetch_state=FetchState.UPLOADING.value,
        )
        session.add(
            FetchEntryRecord(
                fetch_id="fx-1",
                entry_id="e1",
                entry_order=1,
                collection_id=collection_id,
                path=path,
                bytes=len(content),
                sha256=sha256,
                recovery_bytes=len(encrypted),
                uploaded_bytes=len(encrypted),
                upload_expires_at=None,
                tus_url=tus_url,
            )
        )

    with session_scope(session_factory) as stale_session:
        stale_fetch = stale_session.get(FetchRecord, "fx-1")
        stale_entries = stale_session.scalars(
            select(FetchEntryRecord)
            .where(FetchEntryRecord.fetch_id == "fx-1")
            .order_by(FetchEntryRecord.entry_order)
        ).all()
        assert stale_fetch is not None

        completed = service.complete("fx-1")
        assert completed["state"] == FetchState.DONE.value

        _sync_upload_progress(stale_fetch, stale_entries, upload_store)

    manifest = service.manifest("fx-1")
    assert manifest["entries"][0]["upload_state"] == "uploaded"
    assert manifest["entries"][0]["uploaded_bytes"] == len(encrypted)

    with session_scope(session_factory) as session:
        fetch_record = session.get(FetchRecord, "fx-1")
        entry_record = session.get(
            FetchEntryRecord,
            {
                "fetch_id": "fx-1",
                "entry_id": "e1",
            },
        )

        assert fetch_record is not None
        assert entry_record is not None
        assert fetch_record.fetch_state == FetchState.DONE.value
        assert entry_record.uploaded_bytes == len(encrypted)
        assert entry_record.tus_url is None

    assert upload_store.cancelled_uploads == [tus_url]
    assert upload_store.deleted_targets == [target_path]


def test_cold_fetch_manifest_uses_registered_disc_payload_metadata(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))

    collection_id = "docs"
    path = "file.txt"
    target = f"{collection_id}/{path}"
    content = b"cold payload\n"
    sha256 = hashlib.sha256(content).hexdigest()
    encrypted = encrypt_recovery_payload(content, _RECOVERY_CODEC)
    payload_path = image_root / "files/000001.age"
    payload_path.parent.mkdir(parents=True)
    payload_path.write_bytes(encrypted)

    hot_store = _FakeHotStore({})
    service = SqlAlchemyFetchService(
        _config(sqlite_path),
        hot_store,
        _RaceyUploadStore({}),
        _RECOVERY_CODEC,
    )
    session_factory = make_session_factory(sqlite_url(sqlite_path))

    with session_scope(session_factory) as session:
        session.add(CollectionRecord(id=collection_id))
        session.add(
            CollectionFileRecord(
                collection_id=collection_id,
                path=path,
                bytes=len(content),
                sha256=sha256,
                hot=False,
                archived=True,
            )
        )
        session.add(
            FinalizedImageRecord(
                image_id="vol-1",
                candidate_id="candidate-1",
                filename="vol-1.iso",
                bytes=len(encrypted),
                image_root=str(image_root),
                target_bytes=50_000_000_000,
            )
        )
        session.add(
            FileCopyRecord(
                collection_id=collection_id,
                path=path,
                copy_id="copy-1",
                volume_id="vol-1",
                location="vault-a/shelf-01",
                disc_path="files/000001.age",
                enc_json="{}",
                part_index=None,
                part_count=None,
                part_bytes=None,
                part_sha256=None,
            )
        )
        _add_fetch(
            session,
            target=target,
            fetch_id="fx-cold",
            fetch_state=FetchState.QUEUED_DJDAN.value,
        )

    manifest = service.manifest("fx-cold")
    entry = manifest["entries"][0]
    copy = entry["copies"][0]

    assert entry["collection_id"] == collection_id
    assert entry["recovery_bytes"] == len(encrypted)
    assert copy["recovery_bytes"] == len(encrypted)
    assert copy["recovery_sha256"] == hashlib.sha256(encrypted).hexdigest()


def test_cold_split_fetch_complete_uses_registered_part_sizes(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))

    collection_id = "docs"
    path = "file.txt"
    target = f"{collection_id}/{path}"
    content = b"split cold payload that spans media\n"
    first = content[: len(content) // 2]
    second = content[len(content) // 2 :]
    encrypted_first = encrypt_recovery_payload(first, _RECOVERY_CODEC)
    encrypted_second = encrypt_recovery_payload(second, _RECOVERY_CODEC)
    (image_root / "files").mkdir(parents=True)
    (image_root / "files/000001.age").write_bytes(encrypted_first)
    (image_root / "files/000002.age").write_bytes(encrypted_second)

    upload_payload = encrypted_first + encrypted_second
    target_path = "/.riverhog/uploads/recovery/fx-split/e1.enc"
    hot_store = _FakeHotStore({})
    upload_store = _RaceyUploadStore({target_path: upload_payload})
    service = SqlAlchemyFetchService(_config(sqlite_path), hot_store, upload_store, _RECOVERY_CODEC)
    session_factory = make_session_factory(sqlite_url(sqlite_path))

    with session_scope(session_factory) as session:
        session.add(CollectionRecord(id=collection_id))
        session.add(
            CollectionFileRecord(
                collection_id=collection_id,
                path=path,
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                hot=False,
                archived=True,
            )
        )
        session.add(
            FinalizedImageRecord(
                image_id="vol-1",
                candidate_id="candidate-1",
                filename="vol-1.iso",
                bytes=len(upload_payload),
                image_root=str(image_root),
                target_bytes=50_000_000_000,
            )
        )
        for index, disc_path in enumerate(("files/000001.age", "files/000002.age")):
            session.add(
                FileCopyRecord(
                    collection_id=collection_id,
                    path=path,
                    copy_id=f"copy-{index + 1}",
                    volume_id="vol-1",
                    location=f"vault-a/shelf-{index + 1:02d}",
                    disc_path=disc_path,
                    enc_json="{}",
                    part_index=index,
                    part_count=2,
                    part_bytes=None,
                    part_sha256=None,
                )
            )
        _add_fetch(
            session,
            target=target,
            fetch_id="fx-split",
            fetch_state=FetchState.UPLOADING.value,
        )
        session.add(
            FetchEntryRecord(
                fetch_id="fx-split",
                entry_id="e1",
                entry_order=1,
                collection_id=collection_id,
                path=path,
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                recovery_bytes=len(upload_payload),
                uploaded_bytes=len(upload_payload),
                upload_expires_at=None,
                tus_url="/uploads/fx-split/e1",
            )
        )

    completed = service.complete("fx-split")

    assert completed["state"] == FetchState.DONE.value
    assert hot_store.get_collection_file(collection_id, path) == content
    assert upload_store.deleted_targets == [target_path]


def test_cold_fetch_manifest_can_select_multiple_collections_by_prefix(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))

    path = "shared/name.txt"
    collections = [
        ("2025/20250712T213200Z__alpha", b"alpha collection payload\n"),
        ("2025/20250713T145436Z__beta", b"beta collection payload\n"),
    ]
    encrypted_by_collection = {
        collection_id: encrypt_recovery_payload(content, _RECOVERY_CODEC)
        for collection_id, content in collections
    }
    for index, (collection_id, _content) in enumerate(collections, start=1):
        payload_path = image_root / f"files/{index:06d}.age"
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        payload_path.write_bytes(encrypted_by_collection[collection_id])

    service = SqlAlchemyFetchService(
        _config(sqlite_path),
        _FakeHotStore({}),
        _RaceyUploadStore({}),
        _RECOVERY_CODEC,
    )
    session_factory = make_session_factory(sqlite_url(sqlite_path))

    with session_scope(session_factory) as session:
        for index, (collection_id, content) in enumerate(collections, start=1):
            encrypted = encrypted_by_collection[collection_id]
            session.add(CollectionRecord(id=collection_id))
            session.add(
                CollectionFileRecord(
                    collection_id=collection_id,
                    path=path,
                    bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    hot=False,
                    archived=True,
                )
            )
            session.add(
                FinalizedImageRecord(
                    image_id=f"vol-{index}",
                    candidate_id=f"candidate-{index}",
                    filename=f"vol-{index}.iso",
                    bytes=len(encrypted),
                    image_root=str(image_root),
                    target_bytes=50_000_000_000,
                )
            )
            session.add(
                FileCopyRecord(
                    collection_id=collection_id,
                    path=path,
                    copy_id=f"copy-{index}",
                    volume_id=f"vol-{index}",
                    location=f"vault-a/shelf-{index:02d}",
                    disc_path=f"files/{index:06d}.age",
                    enc_json="{}",
                    part_index=None,
                    part_count=None,
                    part_bytes=None,
                    part_sha256=None,
                )
            )
        _add_fetch(
            session,
            target="2025/",
            fetch_id="fx-multi",
            fetch_state=FetchState.QUEUED_DJDAN.value,
        )

    manifest = service.manifest("fx-multi")

    assert manifest["targets"] == ("2025/",)
    assert [
        (entry["id"], entry["collection_id"], entry["path"]) for entry in manifest["entries"]
    ] == [
        ("e1", collections[0][0], path),
        ("e2", collections[1][0], path),
    ]


def test_cold_fetch_complete_restores_matching_paths_across_collections(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(sqlite_url(sqlite_path))

    path = "shared/name.txt"
    collections = [
        ("2025/20250712T213200Z__alpha", b"alpha collection payload\n"),
        ("2025/20250713T145436Z__beta", b"beta collection payload\n"),
    ]
    encrypted_by_collection = {
        collection_id: encrypt_recovery_payload(content, _RECOVERY_CODEC)
        for collection_id, content in collections
    }
    for index, (collection_id, _content) in enumerate(collections, start=1):
        payload_path = image_root / f"files/{index:06d}.age"
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        payload_path.write_bytes(encrypted_by_collection[collection_id])

    upload_store = _RaceyUploadStore(
        {
            f"/.riverhog/uploads/recovery/fx-multi/e{index}.enc": encrypted_by_collection[
                collection_id
            ]
            for index, (collection_id, _content) in enumerate(collections, start=1)
        }
    )
    hot_store = _FakeHotStore({})
    service = SqlAlchemyFetchService(_config(sqlite_path), hot_store, upload_store, _RECOVERY_CODEC)
    session_factory = make_session_factory(sqlite_url(sqlite_path))

    with session_scope(session_factory) as session:
        for index, (collection_id, content) in enumerate(collections, start=1):
            encrypted = encrypted_by_collection[collection_id]
            session.add(CollectionRecord(id=collection_id))
            session.add(
                CollectionFileRecord(
                    collection_id=collection_id,
                    path=path,
                    bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    hot=False,
                    archived=True,
                )
            )
            session.add(
                FinalizedImageRecord(
                    image_id=f"vol-{index}",
                    candidate_id=f"candidate-{index}",
                    filename=f"vol-{index}.iso",
                    bytes=len(encrypted),
                    image_root=str(image_root),
                    target_bytes=50_000_000_000,
                )
            )
            session.add(
                FileCopyRecord(
                    collection_id=collection_id,
                    path=path,
                    copy_id=f"copy-{index}",
                    volume_id=f"vol-{index}",
                    location=f"vault-a/shelf-{index:02d}",
                    disc_path=f"files/{index:06d}.age",
                    enc_json="{}",
                    part_index=None,
                    part_count=None,
                    part_bytes=None,
                    part_sha256=None,
                )
            )
            session.add(
                FetchEntryRecord(
                    fetch_id="fx-multi",
                    entry_id=f"e{index}",
                    entry_order=index,
                    collection_id=collection_id,
                    path=path,
                    bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    recovery_bytes=len(encrypted),
                    uploaded_bytes=len(encrypted),
                    upload_expires_at=None,
                    tus_url=f"/uploads/fx-multi/e{index}",
                )
            )
        _add_fetch(
            session,
            target="2025/",
            fetch_id="fx-multi",
            fetch_state=FetchState.UPLOADING.value,
        )

    manifest = service.manifest("fx-multi")

    assert [(entry["collection_id"], entry["path"]) for entry in manifest["entries"]] == [
        (collection_id, path) for collection_id, _content in collections
    ]

    completed = service.complete("fx-multi")

    assert completed["state"] == FetchState.DONE.value
    for collection_id, content in collections:
        assert hot_store.get_collection_file(collection_id, path) == content
    assert upload_store.deleted_targets == [
        "/.riverhog/uploads/recovery/fx-multi/e1.enc",
        "/.riverhog/uploads/recovery/fx-multi/e2.enc",
    ]
