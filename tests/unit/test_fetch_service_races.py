from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import select

from arc_core.catalog_models import (
    ActivePinRecord,
    CollectionFileRecord,
    CollectionRecord,
    FetchEntryRecord,
    FileCopyRecord,
)
from arc_core.domain.enums import FetchState
from arc_core.recovery_payloads import encrypt_recovery_payload
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.fetches import SqlAlchemyFetchService, _sync_upload_progress
from arc_core.sqlite_db import initialize_db, make_session_factory, session_scope


class _FakeHotStore:
    def __init__(self, files: dict[tuple[str, str], bytes]) -> None:
        self._files = dict(files)

    def put_collection_file(self, collection_id: str, path: str, content: bytes) -> None:
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

    def iter_target(self, target_path: str) -> Iterator[bytes]:
        yield self.read_target(target_path)

    def delete_target(self, target_path: str) -> None:
        self.deleted_targets.append(target_path)
        self._target_payloads.pop(target_path, None)

    def cancel_upload(self, tus_url: str) -> None:
        self.cancelled_uploads.append(tus_url)


def test_stale_sync_does_not_rollback_completed_fetch_state(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

    collection_id = "docs"
    path = "file.txt"
    target = f"{collection_id}/{path}"
    content = b"invoice payload\n"
    sha256 = hashlib.sha256(content).hexdigest()
    encrypted = encrypt_recovery_payload(content)
    target_path = "/.arc/uploads/recovery/fx-1/e1.enc"
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
        sqlite_path=sqlite_path,
    )
    service = SqlAlchemyFetchService(config, hot_store, upload_store)
    session_factory = make_session_factory(str(sqlite_path))

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
            )
        )
        session.add(
            ActivePinRecord(
                target=target,
                fetch_id="fx-1",
                fetch_order=1,
                fetch_state=FetchState.UPLOADING.value,
            )
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
        stale_pin = stale_session.scalar(
            select(ActivePinRecord).where(ActivePinRecord.fetch_id == "fx-1")
        )
        stale_entries = stale_session.scalars(
            select(FetchEntryRecord)
            .where(FetchEntryRecord.fetch_id == "fx-1")
            .order_by(FetchEntryRecord.entry_order)
        ).all()
        assert stale_pin is not None

        completed = service.complete("fx-1")
        assert completed["state"] == FetchState.DONE.value

        _sync_upload_progress(stale_pin, stale_entries, upload_store)

    manifest = service.manifest("fx-1")
    assert manifest["entries"][0]["upload_state"] == "uploaded"
    assert manifest["entries"][0]["uploaded_bytes"] == len(encrypted)

    with session_scope(session_factory) as session:
        pin_record = session.scalar(
            select(ActivePinRecord).where(ActivePinRecord.fetch_id == "fx-1")
        )
        entry_record = session.get(
            FetchEntryRecord,
            {
                "fetch_id": "fx-1",
                "entry_id": "e1",
            },
        )

        assert pin_record is not None
        assert entry_record is not None
        assert pin_record.fetch_state == FetchState.DONE.value
        assert entry_record.uploaded_bytes == len(encrypted)
        assert entry_record.tus_url is None

    assert upload_store.cancelled_uploads == [tus_url]
    assert upload_store.deleted_targets == [target_path]
