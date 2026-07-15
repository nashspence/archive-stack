from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import cast

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionFileRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
)
from riverhog_core.collection_archives import CollectionArchivePackage
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadTracker,
    ArchiveStore,
    ArchiveUploadReceipt,
    CollectionArchiveUploadReceipt,
)
from riverhog_core.ports.hot_store import HotFileStat, HotStore
from riverhog_core.ports.upload_store import UploadStore
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_uploads import SqlAlchemyArchiveUploadService
from riverhog_core.services.collections import _collection_upload_target_path
from tests.fixtures.crypto import FixtureProofStamper
from tests.unit.db_helpers import sqlite_url

_COLLECTION_ID = "2026/20260102T030405Z__docs"
_CONTENT = b"archive upload policy\n"


class MemoryUploadStore:
    def __init__(self) -> None:
        self.targets: dict[str, bytes] = {}

    def iter_target(
        self,
        target_path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        content = self.targets[target_path]
        yield content[offset:] if size is None else content[offset : offset + size]

    def delete_target(self, target_path: str) -> None:
        self.targets.pop(target_path, None)


class MemoryHotStore:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str], bytes] = {}

    def stat_collection_file(self, collection_id: str, path: str) -> HotFileStat | None:
        content = self.files.get((collection_id, path))
        if content is None:
            return None
        return HotFileStat(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())

    def iter_collection_file(
        self,
        collection_id: str,
        path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        content = self.files[(collection_id, path)]
        yield content[offset:] if size is None else content[offset : offset + size]

    def put_collection_file_stream(
        self,
        collection_id: str,
        path: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
        sha256: str | None = None,
    ) -> None:
        _ = sha256
        content = b"".join(chunks)
        assert len(content) == content_length
        self.files[(collection_id, path)] = content

    def delete_collection_file(self, collection_id: str, path: str) -> None:
        self.files.pop((collection_id, path), None)


class RecordingArchiveStore:
    def __init__(self) -> None:
        self.packages: list[CollectionArchivePackage] = []
        self.catalog_entries: list[dict[str, object]] = []

    def new_collection_archive_storage_prefix(self) -> str:
        return "archive/archives/test-copy"

    def upload_collection_archive_package(
        self,
        *,
        collection_id: str,
        package: CollectionArchivePackage,
        archive_storage_prefix: str | None = None,
        multipart_tracker: ArchiveMultipartUploadTracker | None = None,
    ) -> CollectionArchiveUploadReceipt:
        _ = archive_storage_prefix, multipart_tracker
        assert collection_id == _COLLECTION_ID
        assert package.archive_bytes
        self.packages.append(package)
        uploaded_at = "2026-07-15T00:00:00Z"
        return CollectionArchiveUploadReceipt(
            archive=ArchiveUploadReceipt(
                object_path="archives/opaque-docs/archive.tar.age",
                stored_bytes=package.archive_size,
                backend="s3",
                storage_class="DEEP_ARCHIVE",
                uploaded_at=uploaded_at,
                verified_at=uploaded_at,
            ),
            manifest=ArchiveUploadReceipt(
                object_path="archives/opaque-docs/manifest.yml.age",
                stored_bytes=len(package.manifest_bytes),
                backend="s3",
                storage_class="STANDARD",
                uploaded_at=uploaded_at,
                verified_at=uploaded_at,
            ),
            proof=ArchiveUploadReceipt(
                object_path="archives/opaque-docs/manifest.yml.ots.age",
                stored_bytes=len(package.proof_bytes),
                backend="s3",
                storage_class="STANDARD",
                uploaded_at=uploaded_at,
                verified_at=uploaded_at,
            ),
            archive_sha256=package.archive_sha256,
            manifest_sha256=package.manifest_sha256,
            proof_sha256=package.proof_sha256,
            archive_format=package.archive_format,
            compression=package.compression,
        )

    def publish_restore_catalog(
        self,
        *,
        entries: Sequence[dict[str, object]],
        generated_at: str,
    ) -> None:
        assert generated_at.endswith("Z")
        self.catalog_entries = list(entries)


def _stage_upload(
    path: Path,
    upload_store: MemoryUploadStore,
    *,
    retain_hot: bool,
    archive_store: str = "deep",
) -> None:
    database_url = sqlite_url(path)
    initialize_db(database_url)
    digest = hashlib.sha256(_CONTENT).hexdigest()
    target_path = _collection_upload_target_path(_COLLECTION_ID, "document.txt")
    upload_store.targets[target_path] = _CONTENT
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        session.add(
            CollectionUploadRecord(
                collection_id=_COLLECTION_ID,
                archive_store=archive_store,
                state="archiving",
                retain_hot=retain_hot,
            )
        )
        session.add(
            CollectionUploadFileRecord(
                collection_id=_COLLECTION_ID,
                path="document.txt",
                file_order=1,
                bytes=len(_CONTENT),
                sha256=digest,
                uploaded_bytes=len(_CONTENT),
            )
        )


def _process(
    path: Path,
    upload_store: MemoryUploadStore,
    archive_store: RecordingArchiveStore,
    hot_store: HotStore | None,
    *,
    archive_store_name: str = "deep",
) -> None:
    service = SqlAlchemyArchiveUploadService(
        RuntimeConfig(database_url=sqlite_url(path)),
        ArchiveStoreRegistry(
            {archive_store_name: cast(ArchiveStore, archive_store)},
            default_store=archive_store_name,
        ),
        hot_store,
        cast(UploadStore, upload_store),
        proof_stamper=FixtureProofStamper(),
    )
    assert service.process_due_uploads(limit=1) == 1


def test_archive_only_upload_finalizes_without_hot_storage(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    upload_store = MemoryUploadStore()
    archive_store = RecordingArchiveStore()
    _stage_upload(path, upload_store, retain_hot=False)

    _process(path, upload_store, archive_store, None)

    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        file_record = session.get(CollectionFileRecord, (_COLLECTION_ID, "document.txt"))
        assert file_record is not None
        assert file_record.hot is False
        assert session.get(CollectionUploadRecord, _COLLECTION_ID) is None
    assert upload_store.targets == {}
    assert len(archive_store.packages) == 1
    assert [entry["collection_id"] for entry in archive_store.catalog_entries] == [_COLLECTION_ID]


def test_retained_hot_upload_materializes_before_finalizing(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    upload_store = MemoryUploadStore()
    archive_store = RecordingArchiveStore()
    hot_store = MemoryHotStore()
    _stage_upload(path, upload_store, retain_hot=True)

    _process(path, upload_store, archive_store, cast(HotStore, hot_store))

    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        file_record = session.get(CollectionFileRecord, (_COLLECTION_ID, "document.txt"))
        assert file_record is not None
        assert file_record.hot is True
    assert hot_store.files[(_COLLECTION_ID, "document.txt")] == _CONTENT
    assert upload_store.targets == {}


def test_upload_routes_the_archive_copy_to_the_selected_store(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    upload_store = MemoryUploadStore()
    b2_store = RecordingArchiveStore()
    _stage_upload(path, upload_store, retain_hot=False, archive_store="b2")

    _process(
        path,
        upload_store,
        b2_store,
        None,
        archive_store_name="b2",
    )

    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        copy = session.get(CollectionArchiveCopyRecord, (_COLLECTION_ID, "b2"))
        assert copy is not None
        assert copy.state == "uploaded"
