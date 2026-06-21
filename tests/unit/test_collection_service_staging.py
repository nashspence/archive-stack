from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    ImageCopyRecord,
    PlannedCandidateRecord,
)
from riverhog_core.collection_archives import (
    CollectionArchiveFile,
    build_collection_archive_package,
)
from riverhog_core.domain.errors import Conflict, NotFound
from riverhog_core.finalized_image_coverage import (
    read_finalized_image_collection_artifacts,
    read_finalized_image_coverage_parts,
)
from riverhog_core.planner.manifest import MANIFEST_FILENAME
from riverhog_core.ports.archive_store import ArchiveUploadReceipt, CollectionArchiveUploadReceipt
from riverhog_core.ports.hot_store import HotFileStat
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services import collections as collections_service
from riverhog_core.services.collections import SqlAlchemyCollectionService
from riverhog_core.services.glacier_uploads import SqlAlchemyGlacierUploadService
from riverhog_core.services.planning import (
    SqlAlchemyPlanningService,
    _load_plan_files,
    cache_collection_manifest_artifacts,
    refresh_provisional_plan,
)
from tests.fixtures.crypto import FixtureProofStamper, FixtureRecoveryPayloadCodec
from tests.fixtures.data import DOCS_FILES
from tests.unit.db_helpers import sqlite_url


class _FakeHotStore:
    def __init__(self) -> None:
        self._files: dict[tuple[str, str], bytes] = {}
        self.put_paths: list[tuple[str, str]] = []

    def put_collection_file(self, collection_id: str, path: str, content: bytes) -> None:
        self.put_paths.append((collection_id, path))
        self._files[(collection_id, path)] = content

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
        self.put_paths.append((collection_id, path))
        self._files[(collection_id, path)] = content

    def get_collection_file(self, collection_id: str, path: str) -> bytes:
        return self._files[(collection_id, path)]

    def iter_collection_file(
        self,
        collection_id: str,
        path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        content = self.get_collection_file(collection_id, path)
        yield content[offset:] if size is None else content[offset : offset + size]

    def has_collection_file(self, collection_id: str, path: str) -> bool:
        return (collection_id, path) in self._files

    def stat_collection_file(self, collection_id: str, path: str):
        content = self._files.get((collection_id, path))
        if content is None:
            return None
        return HotFileStat(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())

    def delete_collection_file(self, collection_id: str, path: str) -> None:
        self._files.pop((collection_id, path), None)

    def list_collection_files(self, collection_id: str) -> list[tuple[str, int]]:
        return [
            (path, len(content))
            for (stored_collection_id, path), content in sorted(self._files.items())
            if stored_collection_id == collection_id
        ]


class _FailOnceRecoveryPayloadCodec:
    def __init__(self, *, fail_after_successes: int) -> None:
        self._fail_after_successes = fail_after_successes
        self._successes = 0

    @property
    def metadata(self):
        return {"alg": "age-plugin-batchpass/v1", "fixture": True}

    def encrypt(self, content: bytes) -> bytes:
        if self._successes >= self._fail_after_successes:
            raise RuntimeError("synthetic planner encryption failure")
        self._successes += 1
        return FixtureRecoveryPayloadCodec().encrypt(content)

    def decrypt(self, content: bytes) -> bytes:
        return FixtureRecoveryPayloadCodec().decrypt(content)


class _FailingHotStore(_FakeHotStore):
    def __init__(self, *, fail_path: str) -> None:
        super().__init__()
        self._fail_path = fail_path

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
        if path == self._fail_path:
            raise RuntimeError("hot store unavailable")
        super().put_collection_file_stream(
            collection_id,
            path,
            chunks,
            content_length=content_length,
            sha256=sha256,
        )


class _SlowHotStore(_FakeHotStore):
    def __init__(self, *, delay_seconds: float) -> None:
        super().__init__()
        self._delay_seconds = delay_seconds
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def put_collection_file_stream(
        self,
        collection_id: str,
        path: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
        sha256: str | None = None,
    ) -> None:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(self._delay_seconds)
            super().put_collection_file_stream(
                collection_id,
                path,
                chunks,
                content_length=content_length,
                sha256=sha256,
            )
        finally:
            with self._lock:
                self._active -= 1


class _FakeUploadStore:
    def __init__(self) -> None:
        self._target_by_url: dict[str, str] = {}
        self._content_by_target: dict[str, bytes] = {}
        self.deleted_targets: list[str] = []
        self.get_offset_calls = 0

    def create_upload(self, target_path: str, length: int) -> str:
        tus_url = f"/uploads/{len(self._target_by_url) + 1}"
        self._target_by_url[tus_url] = target_path
        self._content_by_target.setdefault(target_path, b"")
        return tus_url

    def get_offset(self, tus_url: str) -> int:
        self.get_offset_calls += 1
        target_path = self._target_by_url.get(tus_url)
        if target_path is None:
            return -1
        return len(self._content_by_target[target_path])

    def append_upload_chunk(
        self,
        tus_url: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> tuple[int, str | None]:
        target_path = self._target_by_url[tus_url]
        current = self._content_by_target[target_path]
        assert len(current) == offset
        algo, encoded = checksum.split(" ", 1)
        assert algo == "sha256"
        assert base64.b64decode(encoded) == hashlib.sha256(content).digest()
        updated = current + content
        self._content_by_target[target_path] = updated
        return len(updated), None

    def read_target(self, target_path: str) -> bytes:
        return self._content_by_target[target_path]

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
        if target_path in self._content_by_target:
            self.deleted_targets.append(target_path)
            self._content_by_target.pop(target_path, None)

    def cancel_upload(self, tus_url: str) -> None:
        self._target_by_url.pop(tus_url, None)


class _StreamingOnlyUploadStore(_FakeUploadStore):
    def read_target(self, target_path: str) -> bytes:
        raise AssertionError(f"read_target should not be used for upload promotion: {target_path}")

    def iter_target(
        self,
        target_path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        content = self._content_by_target[target_path]
        content = content[offset:] if size is None else content[offset : offset + size]
        midpoint = len(content) // 2
        yield content[:midpoint]
        yield content[midpoint:]


def _set_upload_last_activity(sqlite_path: Path, collection_id: str, value: str) -> None:
    with session_scope(make_session_factory(sqlite_url(sqlite_path))) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        upload.last_activity_at = value


def _get_upload_last_activity(sqlite_path: Path, collection_id: str) -> str | None:
    with session_scope(make_session_factory(sqlite_url(sqlite_path))) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        return upload.last_activity_at


class _FakeArchiveStore:
    def upload_collection_archive_package(
        self,
        *,
        collection_id,
        package,
        archive_storage_prefix=None,
        multipart_tracker=None,
    ):
        _ = multipart_tracker
        prefix = archive_storage_prefix or f"glacier/archives/fake-{collection_id}"
        object_path = f"{prefix}/archive.tar.age"
        manifest_object_path = f"{prefix}/manifest.yml.age"
        proof_object_path = f"{prefix}/manifest.yml.ots.age"
        return CollectionArchiveUploadReceipt(
            archive=ArchiveUploadReceipt(
                object_path=object_path,
                stored_bytes=len(package.archive_bytes),
                backend="s3",
                storage_class="DEEP_ARCHIVE",
                uploaded_at="2026-04-20T04:00:00Z",
                verified_at="2026-04-20T04:00:01Z",
            ),
            manifest=ArchiveUploadReceipt(
                object_path=manifest_object_path,
                stored_bytes=len(package.manifest_bytes),
                backend="s3",
                storage_class="STANDARD",
                uploaded_at="2026-04-20T04:00:00Z",
                verified_at="2026-04-20T04:00:01Z",
            ),
            proof=ArchiveUploadReceipt(
                object_path=proof_object_path,
                stored_bytes=len(package.proof_bytes),
                backend="s3",
                storage_class="STANDARD",
                uploaded_at="2026-04-20T04:00:00Z",
                verified_at="2026-04-20T04:00:01Z",
            ),
            archive_sha256=package.archive_sha256,
            manifest_sha256=package.manifest_sha256,
            proof_sha256=package.proof_sha256,
            archive_format=package.archive_format,
            compression=package.compression,
        )


class _ReadableArchiveStore(_FakeArchiveStore):
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def store_collection_artifacts(
        self,
        *,
        manifest_object_path: str,
        manifest_bytes: bytes,
        proof_object_path: str,
        proof_bytes: bytes,
    ) -> None:
        self._objects[manifest_object_path] = manifest_bytes
        self._objects[proof_object_path] = proof_bytes

    def read_restored_collection_manifest(
        self,
        *,
        collection_id: str,
        object_path: str,
    ) -> bytes:
        _ = collection_id
        return self._objects[object_path]

    def read_restored_collection_manifest_proof(
        self,
        *,
        collection_id: str,
        object_path: str,
    ) -> bytes:
        _ = collection_id
        return self._objects[object_path]


class _CountingArchiveStore(_FakeArchiveStore):
    def __init__(self) -> None:
        self.uploads = 0

    def upload_collection_archive_package(
        self,
        *,
        collection_id,
        package,
        archive_storage_prefix=None,
        multipart_tracker=None,
    ):
        self.uploads += 1
        return super().upload_collection_archive_package(
            collection_id=collection_id,
            package=package,
            archive_storage_prefix=archive_storage_prefix,
            multipart_tracker=multipart_tracker,
        )


class _RecordingArchiveStore(_FakeArchiveStore):
    def __init__(self) -> None:
        self.collection_ids: list[str] = []

    def upload_collection_archive_package(
        self,
        *,
        collection_id,
        package,
        archive_storage_prefix=None,
        multipart_tracker=None,
    ):
        self.collection_ids.append(collection_id)
        return super().upload_collection_archive_package(
            collection_id=collection_id,
            package=package,
            archive_storage_prefix=archive_storage_prefix,
            multipart_tracker=multipart_tracker,
        )


class _AlwaysFailingArchiveStore(_FakeArchiveStore):
    def __init__(self) -> None:
        self.uploads = 0

    def upload_collection_archive_package(
        self,
        *,
        collection_id,
        package,
        archive_storage_prefix=None,
        multipart_tracker=None,
    ):
        _ = collection_id, package, archive_storage_prefix, multipart_tracker
        self.uploads += 1
        raise RuntimeError("archive bucket unavailable")


class _FailOnceArchiveStore(_FakeArchiveStore):
    def __init__(self) -> None:
        self.uploads = 0

    def upload_collection_archive_package(
        self,
        *,
        collection_id,
        package,
        archive_storage_prefix=None,
        multipart_tracker=None,
    ):
        self.uploads += 1
        if self.uploads == 1:
            raise RuntimeError("archive bucket unavailable")
        return super().upload_collection_archive_package(
            collection_id=collection_id,
            package=package,
            archive_storage_prefix=archive_storage_prefix,
            multipart_tracker=multipart_tracker,
        )


class _CountingProofStamper:
    def __init__(self) -> None:
        self.stamps = 0
        self._stamper = FixtureProofStamper()

    def stamp(self, manifest_path: Path) -> Path:
        self.stamps += 1
        return self._stamper.stamp(manifest_path)


def _config(sqlite_path: Path, **overrides: object) -> RuntimeConfig:
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
        **overrides,
    )


def _chunk_checksum(content: bytes) -> str:
    return "sha256 " + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")


def _seed_docs_collection_with_finalized_image(sqlite_path: Path, image_root: Path) -> None:
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        session.add(CollectionRecord(id="docs"))
        invoice = DOCS_FILES["tax/2022/invoice-123.pdf"]
        session.add(
            CollectionFileRecord(
                collection_id="docs",
                path="tax/2022/invoice-123.pdf",
                bytes=len(invoice),
                sha256=hashlib.sha256(invoice).hexdigest(),
                hot=False,
                archived=True,
            )
        )
        session.add(
            FinalizedImageRecord(
                image_id="20260420T040003Z",
                candidate_id="img_2026-04-20_03",
                filename="20260420T040003Z.iso",
                bytes=5100,
                image_root=str(image_root),
                target_bytes=10_000,
                required_copy_count=2,
            )
        )
        session.add(
            FinalizedImageCoveredPathRecord(
                image_id="20260420T040003Z",
                collection_id="docs",
                path="tax/2022/invoice-123.pdf",
            )
        )
        session.add(
            FinalizedImageCoveragePartRecord(
                image_id="20260420T040003Z",
                collection_id="docs",
                path="tax/2022/invoice-123.pdf",
                part_index=0,
                part_count=2,
            )
        )
        session.add(
            FinalizedImageRecord(
                image_id="20260420T040004Z",
                candidate_id="img_2026-04-20_04",
                filename="20260420T040004Z.iso",
                bytes=5100,
                image_root=str(image_root),
                target_bytes=10_000,
                required_copy_count=2,
            )
        )
        session.add(
            FinalizedImageCoveredPathRecord(
                image_id="20260420T040004Z",
                collection_id="docs",
                path="tax/2022/invoice-123.pdf",
            )
        )
        session.add(
            FinalizedImageCoveragePartRecord(
                image_id="20260420T040004Z",
                collection_id="docs",
                path="tax/2022/invoice-123.pdf",
                part_index=1,
                part_count=2,
            )
        )


def test_partial_collection_upload_does_not_publish_committed_hot_file(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    hot_store = _FakeHotStore()
    upload_store = _StreamingOnlyUploadStore()
    service = SqlAlchemyCollectionService(_config(sqlite_path), hot_store, upload_store)

    content = b"hello world\n"
    sha256 = hashlib.sha256(content).hexdigest()
    upload_slug = "Photos 2024"
    relpath = "albums/day-01.txt"

    payload = service.create_or_resume_upload(
        upload_slug=upload_slug,
        files=[{"path": relpath, "bytes": len(content), "sha256": sha256}],
        ingest_source="/tmp/source",
    )
    collection_id = str(payload["collection_id"])
    session = service.create_or_resume_file_upload(collection_id, relpath)
    service.append_upload_chunk(
        collection_id,
        relpath,
        offset=int(session["offset"]),
        checksum="sha256 " + base64.b64encode(hashlib.sha256(content[:5]).digest()).decode("ascii"),
        content=content[:5],
    )

    assert not hot_store.has_collection_file(collection_id, relpath)


def test_incremental_collection_upload_session_requires_explicit_complete(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    service = SqlAlchemyCollectionService(
        _config(sqlite_path),
        _FakeHotStore(),
        _StreamingOnlyUploadStore(),
    )

    content = b"hello session\n"
    relpath = "albums/day-01.txt"
    opened = service.create_or_resume_upload_session(
        upload_slug="Photos 2024",
        ingest_source="/tmp/source",
        upload_timestamp="20250712T213200Z",
    )
    collection_id = str(opened["collection_id"])
    assert collection_id == "2025/20250712T213200Z__photos-2024"
    assert opened["state"] == "open"
    assert opened["files_total"] == 0

    registered = service.register_upload_session_file(
        collection_id,
        {
            "path": relpath,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    assert registered["state"] == "open"
    assert registered["file"]["path"] == relpath
    assert registered["file"]["bytes"] == len(content)
    assert registered["file"]["upload_state"] == "pending"
    assert registered["file"]["uploaded_bytes"] == 0

    file_upload = service.create_or_resume_file_upload(collection_id, relpath)
    service.append_upload_chunk(
        collection_id,
        relpath,
        offset=int(file_upload["offset"]),
        checksum=_chunk_checksum(content),
        content=content,
    )

    staged = service.get_upload(collection_id)
    assert staged["state"] == "open"
    assert staged["files_uploaded"] == 1

    completed = service.complete_upload_session(collection_id)
    assert completed["state"] == "archiving"
    assert completed["files_uploaded"] == 1


def test_collection_upload_session_can_register_file_and_open_upload_in_one_call(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    upload_store = _StreamingOnlyUploadStore()
    service = SqlAlchemyCollectionService(
        _config(sqlite_path),
        _FakeHotStore(),
        upload_store,
    )

    content = b"hello combined session\n"
    relpath = "albums/day-01.txt"
    opened = service.create_or_resume_upload_session(
        upload_slug="Photos 2024",
        ingest_source="/tmp/source",
        upload_timestamp="20250712T213200Z",
    )
    collection_id = str(opened["collection_id"])

    file_upload = service.create_or_resume_registered_file_upload(
        collection_id,
        {
            "path": relpath,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    )

    assert file_upload["collection_id"] == collection_id
    assert file_upload["state"] == "open"
    assert file_upload["file"]["path"] == relpath
    assert file_upload["file"]["upload_state"] == "pending"
    assert file_upload["path"] == relpath
    assert file_upload["protocol"] == "tus"
    assert file_upload["offset"] == 0
    assert file_upload["length"] == len(content)
    assert file_upload["upload_url"]

    service.append_upload_chunk(
        collection_id,
        relpath,
        offset=int(file_upload["offset"]),
        checksum=_chunk_checksum(content),
        content=content,
    )

    staged = service.get_upload(collection_id)
    assert staged["state"] == "open"
    assert staged["files_uploaded"] == 1
    assert staged["uploaded_bytes"] == len(content)


def test_collection_upload_file_hot_paths_do_not_touch_parent_session_activity(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    upload_store = _StreamingOnlyUploadStore()
    service = SqlAlchemyCollectionService(
        _config(sqlite_path),
        _FakeHotStore(),
        upload_store,
    )

    content = b"hot path session bytes\n"
    relpath = "camera/day-01.webm"
    opened = service.create_or_resume_upload_session(
        upload_slug="Camera 2024",
        ingest_source="/tmp/source",
        upload_timestamp="20250712T213200Z",
    )
    collection_id = str(opened["collection_id"])
    stable_activity = "2035-01-01T00:00:00Z"
    _set_upload_last_activity(sqlite_path, collection_id, stable_activity)

    file_upload = service.create_or_resume_registered_file_upload(
        collection_id,
        {
            "path": relpath,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    assert _get_upload_last_activity(sqlite_path, collection_id) == stable_activity

    upload_store.append_upload_chunk(
        str(file_upload["upload_url"]),
        offset=int(file_upload["offset"]),
        checksum=_chunk_checksum(content),
        content=content,
    )
    synced = service.sync_finished_upload_target(
        f".riverhog/uploads/collections/{collection_id}/{relpath}"
    )

    assert synced is not None
    assert synced["file"]["upload_state"] == "uploaded"
    assert _get_upload_last_activity(sqlite_path, collection_id) == stable_activity

    completed = service.complete_upload_session(collection_id)
    assert completed["state"] == "archiving"
    assert _get_upload_last_activity(sqlite_path, collection_id) != stable_activity


def test_collection_upload_session_complete_force_syncs_direct_tusd_bytes(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    upload_store = _StreamingOnlyUploadStore()
    service = SqlAlchemyCollectionService(
        _config(sqlite_path),
        _FakeHotStore(),
        upload_store,
    )

    content = b"direct tusd data-plane bytes\n"
    relpath = "camera/day-01.webm"
    opened = service.create_or_resume_upload_session(
        upload_slug="Camera 2024",
        ingest_source="/tmp/source",
        upload_timestamp="20250712T213200Z",
    )
    collection_id = str(opened["collection_id"])
    service.register_upload_session_file(
        collection_id,
        {
            "path": relpath,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    file_upload = service.create_or_resume_file_upload(collection_id, relpath)

    upload_store.append_upload_chunk(
        str(file_upload["upload_url"]),
        offset=int(file_upload["offset"]),
        checksum=_chunk_checksum(content),
        content=content,
    )

    completed = service.complete_upload_session(collection_id)

    assert completed["state"] == "archiving"
    assert completed["files_uploaded"] == 1


def test_collection_upload_post_finish_syncs_registered_direct_tusd_file(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    upload_store = _StreamingOnlyUploadStore()
    service = SqlAlchemyCollectionService(
        _config(sqlite_path),
        _FakeHotStore(),
        upload_store,
    )

    content = b"post finish direct tusd bytes\n"
    relpath = "camera/day-01.webm"
    opened = service.create_or_resume_upload_session(
        upload_slug="Camera 2024",
        ingest_source="/tmp/source",
        upload_timestamp="20250712T213200Z",
    )
    collection_id = str(opened["collection_id"])
    service.register_upload_session_file(
        collection_id,
        {
            "path": relpath,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    file_upload = service.create_or_resume_file_upload(collection_id, relpath)

    upload_store.append_upload_chunk(
        str(file_upload["upload_url"]),
        offset=int(file_upload["offset"]),
        checksum=_chunk_checksum(content),
        content=content,
    )

    synced = service.sync_finished_upload_target(
        f".riverhog/uploads/collections/{collection_id}/{relpath}"
    )
    assert synced is not None
    assert synced["file"]["upload_state"] == "uploaded"
    assert synced["file"]["uploaded_bytes"] == len(content)

    staged = service.get_upload(collection_id)
    assert staged["files_uploaded"] == 1
    assert staged["uploaded_bytes"] == len(content)


def test_collection_upload_post_finish_marks_uploaded_without_extra_offset_sync(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    upload_store = _StreamingOnlyUploadStore()
    service = SqlAlchemyCollectionService(
        _config(sqlite_path),
        _FakeHotStore(),
        upload_store,
    )

    content = b"actual bytes\n"
    relpath = "camera/day-01.webm"
    opened = service.create_or_resume_upload_session(
        upload_slug="Camera 2024",
        ingest_source="/tmp/source",
        upload_timestamp="20250712T213200Z",
    )
    collection_id = str(opened["collection_id"])
    service.register_upload_session_file(
        collection_id,
        {
            "path": relpath,
            "bytes": len(content),
            "sha256": hashlib.sha256(b"different bytes").hexdigest(),
        },
    )
    file_upload = service.create_or_resume_file_upload(collection_id, relpath)
    upload_store.append_upload_chunk(
        str(file_upload["upload_url"]),
        offset=int(file_upload["offset"]),
        checksum=_chunk_checksum(content),
        content=content,
    )
    upload_store.get_offset_calls = 0

    synced = service.sync_finished_upload_target(
        f".riverhog/uploads/collections/{collection_id}/{relpath}"
    )

    assert synced is not None
    assert synced["file"]["upload_state"] == "uploaded"
    assert upload_store.get_offset_calls == 0
    staged = service.get_upload(collection_id)
    assert staged["files_uploaded"] == 1
    assert staged["uploaded_bytes"] == len(content)


def test_incremental_collection_upload_session_cancel_cleans_staged_files(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    upload_store = _FakeUploadStore()
    service = SqlAlchemyCollectionService(_config(sqlite_path), _FakeHotStore(), upload_store)

    content = b"partial session"
    opened = service.create_or_resume_upload_session(upload_slug="photos 2024")
    collection_id = str(opened["collection_id"])
    service.register_upload_session_file(
        collection_id,
        {
            "path": "partial.txt",
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    file_upload = service.create_or_resume_file_upload(collection_id, "partial.txt")
    service.append_upload_chunk(
        collection_id,
        "partial.txt",
        offset=int(file_upload["offset"]),
        checksum=_chunk_checksum(content[:7]),
        content=content[:7],
    )

    canceled = service.cancel_upload_session(collection_id)

    assert canceled["state"] == "canceled"
    assert canceled["files_total"] == 0
    assert upload_store.deleted_targets == [
        f"/.riverhog/uploads/collections/{collection_id}/partial.txt"
    ]
    with pytest.raises(NotFound, match="not found"):
        service.get_upload(collection_id)
    with pytest.raises(NotFound, match="not found"):
        service.create_or_resume_file_upload(collection_id, "partial.txt")


def test_incremental_collection_upload_session_idle_ttl_expires_to_audit_state(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    upload_store = _FakeUploadStore()
    service = SqlAlchemyCollectionService(
        _config(sqlite_path, upload_session_idle_ttl=timedelta(seconds=1)),
        _FakeHotStore(),
        upload_store,
    )

    content = b"stale session"
    opened = service.create_or_resume_upload_session(upload_slug="photos 2024")
    collection_id = str(opened["collection_id"])
    service.register_upload_session_file(
        collection_id,
        {
            "path": "stale.txt",
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    service.create_or_resume_file_upload(collection_id, "stale.txt")

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        upload.last_activity_at = "2020-01-01T00:00:00Z"

    service.expire_stale_uploads()
    expired = service.get_upload(collection_id)

    assert expired["state"] == "expired"
    assert expired["files_total"] == 0
    assert upload_store.deleted_targets == [
        f"/.riverhog/uploads/collections/{collection_id}/stale.txt"
    ]


def test_incremental_session_file_registration_uses_cheap_limit_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    hot_store = _FakeHotStore()
    upload_store = _FakeUploadStore()
    setup_service = SqlAlchemyCollectionService(
        _config(sqlite_path),
        hot_store,
        upload_store,
    )
    opened = setup_service.create_or_resume_upload_session(upload_slug="camera")
    collection_id = str(opened["collection_id"])

    def fail_coverage(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("session file registration must not scan collection coverage")

    monkeypatch.setattr(collections_service, "_collection_image_coverage", fail_coverage)
    limited_service = SqlAlchemyCollectionService(
        _config(sqlite_path, unburned_collection_bytes_limit=100),
        hot_store,
        upload_store,
    )
    content = b"hot path"

    registered = limited_service.register_upload_session_file(
        collection_id,
        {
            "path": "clips/001.webm",
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    )

    assert registered["file"]["path"] == "clips/001.webm"


def test_incremental_session_file_registration_enforces_active_upload_limit(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    service = SqlAlchemyCollectionService(
        _config(sqlite_path, unburned_collection_bytes_limit=10),
        _FakeHotStore(),
        _FakeUploadStore(),
    )
    opened = service.create_or_resume_upload_session(upload_slug="camera")
    collection_id = str(opened["collection_id"])
    first = b"12345678"
    second = b"123"
    service.register_upload_session_file(
        collection_id,
        {
            "path": "clips/001.webm",
            "bytes": len(first),
            "sha256": hashlib.sha256(first).hexdigest(),
        },
    )

    with pytest.raises(Conflict, match="unburned collection limit exceeded"):
        service.register_upload_session_file(
            collection_id,
            {
                "path": "clips/002.webm",
                "bytes": len(second),
                "sha256": hashlib.sha256(second).hexdigest(),
            },
        )


def test_file_upload_resume_does_not_sync_unrelated_upload_files(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    upload_store = _FakeUploadStore()
    service = SqlAlchemyCollectionService(_config(sqlite_path), _FakeHotStore(), upload_store)

    first_content = b"first-file"
    second_content = b"second-file"
    payload = service.create_or_resume_upload(
        upload_slug="photos 2024",
        files=[
            {
                "path": "first.txt",
                "bytes": len(first_content),
                "sha256": hashlib.sha256(first_content).hexdigest(),
            },
            {
                "path": "second.txt",
                "bytes": len(second_content),
                "sha256": hashlib.sha256(second_content).hexdigest(),
            },
        ],
    )
    collection_id = str(payload["collection_id"])
    first_session = service.create_or_resume_file_upload(collection_id, "first.txt")
    service.append_upload_chunk(
        collection_id,
        "first.txt",
        offset=int(first_session["offset"]),
        checksum="sha256 "
        + base64.b64encode(hashlib.sha256(first_content[:5]).digest()).decode("ascii"),
        content=first_content[:5],
    )

    upload_store.get_offset_calls = 0
    second_session = service.create_or_resume_file_upload(collection_id, "second.txt")

    assert second_session["path"] == "second.txt"
    assert upload_store.get_offset_calls == 0


def test_completed_collection_upload_promotes_from_staging_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    webhook_payloads: list[dict[str, object]] = []
    monkeypatch.setattr(
        "riverhog_core.services.collections.post_webhook",
        lambda *, config, payload: webhook_payloads.append(payload),
    )
    hot_store = _FakeHotStore()
    upload_store = _StreamingOnlyUploadStore()
    service = SqlAlchemyCollectionService(
        _config(
            sqlite_path,
            operator_webhook_url="http://example.invalid/webhook",
        ),
        hot_store,
        upload_store,
    )

    content = b"hello world\n"
    sha256 = hashlib.sha256(content).hexdigest()
    upload_slug = "photos 2024"
    relpath = "albums/day-01.txt"

    payload = service.create_or_resume_upload(
        upload_slug=upload_slug,
        files=[{"path": relpath, "bytes": len(content), "sha256": sha256}],
        ingest_source="/tmp/source",
    )
    collection_id = str(payload["collection_id"])
    assert collection_id.endswith("__photos-2024")
    staging_target = f"/.riverhog/uploads/collections/{collection_id}/{relpath}"
    session = service.create_or_resume_file_upload(collection_id, relpath)
    service.append_upload_chunk(
        collection_id,
        relpath,
        offset=int(session["offset"]),
        checksum="sha256 " + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
        content=content,
    )
    upload_service = SqlAlchemyGlacierUploadService(
        _config(sqlite_path),
        _FakeArchiveStore(),
        hot_store,
        upload_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )
    assert upload_service.process_due_uploads() == 1

    assert hot_store.get_collection_file(collection_id, relpath) == content
    assert staging_target in upload_store.deleted_targets
    assert [payload["event"] for payload in webhook_payloads] == ["collections.upload_staged"]


def test_glacier_archive_worker_prioritizes_resumable_multipart_upload(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    config = _config(sqlite_path)
    upload_store = _FakeUploadStore()
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    resumed_id = "20250712T213200Z__resume-first"
    pending_id = "20250712T213201Z__pending-second"

    for collection_id, content in (
        (resumed_id, b"resume this archive first\n"),
        (pending_id, b"pending archive should wait\n"),
    ):
        relpath = "albums/day-01.txt"
        target_path = f"/.riverhog/uploads/collections/{collection_id}/{relpath}"
        tus_url = upload_store.create_upload(target_path, len(content))
        upload_store.append_upload_chunk(
            tus_url,
            offset=0,
            checksum="sha256 " + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
            content=content,
        )
        with session_scope(session_factory) as session:
            upload = CollectionUploadRecord(
                collection_id=collection_id,
                state="archiving",
                archive_phase="uploading" if collection_id == resumed_id else "packaging",
                archive_next_attempt_at=(
                    "2026-01-01T00:00:02Z"
                    if collection_id == resumed_id
                    else "2026-01-01T00:00:01Z"
                ),
                archive_multipart_upload_id=(
                    "archive-upload-1" if collection_id == resumed_id else "archive-upload-2"
                ),
                archive_multipart_uploaded_bytes=4 if collection_id == resumed_id else 0,
                archive_multipart_uploaded_parts=1 if collection_id == resumed_id else 0,
            )
            upload.files.append(
                CollectionUploadFileRecord(
                    collection_id=collection_id,
                    path=relpath,
                    file_order=0,
                    bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    uploaded_bytes=len(content),
                    tus_url=tus_url,
                )
            )
            session.add(upload)

    archive_store = _RecordingArchiveStore()
    upload_service = SqlAlchemyGlacierUploadService(
        config,
        archive_store,
        _FakeHotStore(),
        upload_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert upload_service.process_due_uploads() == 1
    assert archive_store.collection_ids == [resumed_id]
    with session_scope(session_factory) as session:
        assert session.get(CollectionUploadRecord, pending_id) is not None


def test_planner_worker_restores_missing_artifact_cache_from_archive_store(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    config = _config(
        sqlite_path,
        planner_image_root=tmp_path / "images",
        planner_disc_target_bytes=1_000_000,
        planner_min_fill_bytes=1,
    )
    hot_store = _FakeHotStore()
    archive_store = _ReadableArchiveStore()
    collection_id = "20250712T213200Z__photos"
    relpath = "albums/day-01.txt"
    content = b"recover artifact cache from archive store\n"
    sha256 = hashlib.sha256(content).hexdigest()
    hot_store.put_collection_file(collection_id, relpath, content)
    package = build_collection_archive_package(
        collection_id=collection_id,
        files=[CollectionArchiveFile(path=relpath, content=content, sha256=sha256)],
        stamper=FixtureProofStamper(),
    )
    manifest_object_path = f"glacier/archives/opaque-{collection_id}/manifest.yml.age"
    proof_object_path = f"glacier/archives/opaque-{collection_id}/manifest.yml.ots.age"
    archive_store.store_collection_artifacts(
        manifest_object_path=manifest_object_path,
        manifest_bytes=package.manifest_bytes,
        proof_object_path=proof_object_path,
        proof_bytes=package.proof_bytes,
    )

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        collection = CollectionRecord(id=collection_id)
        collection.files.append(
            CollectionFileRecord(
                collection_id=collection_id,
                path=relpath,
                bytes=len(content),
                sha256=sha256,
                hot=True,
                archived=False,
            )
        )
        session.add(collection)
        session.add(
            CollectionArchiveRecord(
                collection_id=collection_id,
                state="uploaded",
                object_path=f"glacier/archives/opaque-{collection_id}/archive.tar.age",
                stored_bytes=package.archive_size,
                sha256=package.archive_sha256,
                manifest_object_path=manifest_object_path,
                manifest_sha256=package.manifest_sha256,
                ots_object_path=proof_object_path,
                ots_sha256=package.proof_sha256,
            )
        )
    planning = SqlAlchemyPlanningService(
        config,
        hot_store,
        archive_store,
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert planning.process_due_refresh() == 1
    assert planning.process_due_refresh() == 0
    with session_scope(session_factory) as session:
        candidates = session.scalars(select(PlannedCandidateRecord)).all()
        assert len(candidates) == 1
        assert candidates[0].state == "ready"


def test_failed_hot_promotion_keeps_staging_for_retry(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    hot_store = _FailingHotStore(fail_path="albums/day-02.txt")
    upload_store = _StreamingOnlyUploadStore()
    config = _config(sqlite_path)
    service = SqlAlchemyCollectionService(config, hot_store, upload_store)

    files = [
        ("albums/day-01.txt", b"hello world\n"),
        ("albums/day-02.txt", b"goodbye world\n"),
    ]
    payload = service.create_or_resume_upload(
        upload_slug="photos 2024",
        files=[
            {"path": path, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for path, content in files
        ],
        ingest_source="/tmp/source",
    )
    collection_id = str(payload["collection_id"])
    for relpath, content in files:
        upload_session = service.create_or_resume_file_upload(collection_id, relpath)
        service.append_upload_chunk(
            collection_id,
            relpath,
            offset=int(upload_session["offset"]),
            checksum="sha256 " + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
            content=content,
        )

    archive_store = _CountingArchiveStore()
    upload_service = SqlAlchemyGlacierUploadService(
        config,
        archive_store,
        hot_store,
        upload_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )
    assert upload_service.process_due_uploads() == 1

    first_target = f"/.riverhog/uploads/collections/{collection_id}/albums/day-01.txt"
    second_target = f"/.riverhog/uploads/collections/{collection_id}/albums/day-02.txt"
    assert upload_store.deleted_targets == [first_target]
    assert hot_store.get_collection_file(collection_id, "albums/day-01.txt") == files[0][1]
    assert archive_store.uploads == 1
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        assert upload.state == "archiving"
        assert upload.archive_failure == "hot store unavailable"
        assert upload.archive_receipt_json is not None
        assert upload.collection_manifest_bytes_b64 is not None
        assert upload.collection_manifest_proof_bytes_b64 is not None
        upload_files_by_path = {file_record.path: file_record for file_record in upload.files}
        assert upload_files_by_path["albums/day-01.txt"].hot_promoted_at is not None
        assert upload_files_by_path["albums/day-02.txt"].hot_promoted_at is None
        assert first_target not in upload_store._content_by_target
        assert second_target in upload_store._content_by_target
        assert session.get(CollectionRecord, collection_id) is None
        upload.archive_next_attempt_at = "2026-04-20T04:00:00Z"

    hot_store._fail_path = ""

    assert upload_service.process_due_uploads() == 1

    assert archive_store.uploads == 1
    assert sorted(upload_store.deleted_targets) == sorted([first_target, second_target])
    with session_scope(session_factory) as session:
        assert session.get(CollectionUploadRecord, collection_id) is None
        collection = session.get(CollectionRecord, collection_id)
        assert collection is not None
        assert len(collection.files) == 2


def test_hot_promotion_uses_bounded_concurrency(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    hot_store = _SlowHotStore(delay_seconds=0.03)
    upload_store = _StreamingOnlyUploadStore()
    config = _config(sqlite_path, hot_promotion_concurrency=3)
    service = SqlAlchemyCollectionService(config, hot_store, upload_store)

    files = [(f"albums/day-{index:02}.txt", f"content {index}\n".encode()) for index in range(6)]
    payload = service.create_or_resume_upload(
        upload_slug="photos 2024",
        files=[
            {"path": path, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for path, content in files
        ],
        ingest_source="/tmp/source",
    )
    collection_id = str(payload["collection_id"])
    for relpath, content in files:
        upload_session = service.create_or_resume_file_upload(collection_id, relpath)
        service.append_upload_chunk(
            collection_id,
            relpath,
            offset=int(upload_session["offset"]),
            checksum="sha256 " + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
            content=content,
        )

    upload_service = SqlAlchemyGlacierUploadService(
        config,
        _CountingArchiveStore(),
        hot_store,
        upload_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert upload_service.process_due_uploads() == 1

    assert 1 < hot_store.max_active <= 3
    with session_scope(make_session_factory(sqlite_url(sqlite_path))) as session:
        assert session.get(CollectionUploadRecord, collection_id) is None
        collection = session.get(CollectionRecord, collection_id)
        assert collection is not None
        assert len(collection.files) == len(files)


def test_packaged_archive_artifacts_are_reused_after_upload_failure(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    hot_store = _FakeHotStore()
    upload_store = _StreamingOnlyUploadStore()
    config = _config(sqlite_path, glacier_upload_retry_delay=timedelta(seconds=0))
    service = SqlAlchemyCollectionService(config, hot_store, upload_store)

    content = b"hello world\n"
    relpath = "albums/day-01.txt"
    payload = service.create_or_resume_upload(
        upload_slug="photos 2024",
        files=[
            {
                "path": relpath,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        ingest_source="/tmp/source",
    )
    collection_id = str(payload["collection_id"])
    upload_session = service.create_or_resume_file_upload(collection_id, relpath)
    service.append_upload_chunk(
        collection_id,
        relpath,
        offset=int(upload_session["offset"]),
        checksum="sha256 " + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
        content=content,
    )

    archive_store = _FailOnceArchiveStore()
    proof_stamper = _CountingProofStamper()
    upload_service = SqlAlchemyGlacierUploadService(
        config,
        archive_store,
        hot_store,
        upload_store,
        proof_stamper=proof_stamper,
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert upload_service.process_due_uploads() == 1
    assert archive_store.uploads == 1
    assert proof_stamper.stamps == 1

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        assert upload.archive_phase == "retry_wait"
        assert upload.collection_manifest_bytes_b64 is not None
        assert upload.collection_manifest_proof_bytes_b64 is not None
        assert upload.archive_multipart_content_length is not None
        assert upload.archive_multipart_sha256 is not None
        upload.archive_next_attempt_at = "2026-04-20T04:00:00Z"

    assert upload_service.process_due_uploads() == 1
    assert archive_store.uploads == 2
    assert proof_stamper.stamps == 1

    with session_scope(session_factory) as session:
        assert session.get(CollectionUploadRecord, collection_id) is None
        collection = session.get(CollectionRecord, collection_id)
        assert collection is not None
        assert len(collection.files) == 1


def test_packaged_archive_artifacts_reuse_survives_multipart_content_length(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    hot_store = _FakeHotStore()
    upload_store = _StreamingOnlyUploadStore()
    config = _config(sqlite_path, glacier_upload_retry_delay=timedelta(seconds=0))
    service = SqlAlchemyCollectionService(config, hot_store, upload_store)

    content = b"hello restartable multipart archive\n" * 8
    relpath = "albums/day-01.txt"
    sha256 = hashlib.sha256(content).hexdigest()
    payload = service.create_or_resume_upload(
        upload_slug="photos 2024",
        files=[
            {
                "path": relpath,
                "bytes": len(content),
                "sha256": sha256,
            }
        ],
        ingest_source="/tmp/source",
    )
    collection_id = str(payload["collection_id"])
    upload_session = service.create_or_resume_file_upload(collection_id, relpath)
    service.append_upload_chunk(
        collection_id,
        relpath,
        offset=int(upload_session["offset"]),
        checksum=_chunk_checksum(content),
        content=content,
    )

    package = build_collection_archive_package(
        collection_id=collection_id,
        files=[
            CollectionArchiveFile(
                path=relpath,
                content=content,
                sha256=sha256,
            )
        ],
        stamper=FixtureProofStamper(),
    )

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        upload.state = "archiving"
        upload.archive_phase = "uploading"
        upload.archive_next_attempt_at = "2026-04-20T04:00:00Z"
        upload.collection_manifest_bytes_b64 = base64.b64encode(package.manifest_bytes).decode(
            "ascii"
        )
        upload.collection_manifest_proof_bytes_b64 = base64.b64encode(package.proof_bytes).decode(
            "ascii"
        )
        upload.archive_object_path = f"glacier/archives/{collection_id}/archive.tar.age"
        upload.archive_multipart_upload_id = "archive-upload-1"
        upload.archive_multipart_part_size = 64 * 1024 * 1024
        upload.archive_multipart_content_length = package.archive_size + 123
        upload.archive_multipart_sha256 = package.archive_sha256
        upload.archive_multipart_uploaded_bytes = 64
        upload.archive_multipart_uploaded_parts = 1
        upload.archive_multipart_total_parts = 2

    archive_store = _CountingArchiveStore()
    proof_stamper = _CountingProofStamper()
    upload_service = SqlAlchemyGlacierUploadService(
        config,
        archive_store,
        hot_store,
        upload_store,
        proof_stamper=proof_stamper,
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert upload_service.process_due_uploads() == 1
    assert archive_store.uploads == 1
    assert proof_stamper.stamps == 0

    with session_scope(session_factory) as session:
        assert session.get(CollectionUploadRecord, collection_id) is None
        collection = session.get(CollectionRecord, collection_id)
        assert collection is not None
        assert len(collection.files) == 1


def test_archive_failures_retry_indefinitely_with_throttled_operator_notifications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    webhook_payloads: list[dict[str, object]] = []
    monkeypatch.setattr(
        "riverhog_core.services.glacier_uploads.post_webhook",
        lambda *, config, payload: webhook_payloads.append(payload),
    )
    current = datetime(2026, 4, 20, 4, 0, tzinfo=UTC)
    monkeypatch.setattr("riverhog_core.services.glacier_uploads.utcnow", lambda: current)
    monkeypatch.setattr(
        "riverhog_core.services.collections._utc_now",
        lambda: current.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    hot_store = _FakeHotStore()
    upload_store = _StreamingOnlyUploadStore()
    archive_store = _AlwaysFailingArchiveStore()
    config = _config(
        sqlite_path,
        glacier_upload_retry_delay=timedelta(seconds=0),
        operator_webhook_url="http://example.invalid/webhook",
        operator_failure_notification_interval=timedelta(days=1),
    )
    service = SqlAlchemyCollectionService(_config(sqlite_path), hot_store, upload_store)

    content = b"hello world\n"
    relpath = "albums/day-01.txt"
    payload = service.create_or_resume_upload(
        upload_slug="photos 2024",
        files=[
            {
                "path": relpath,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        ingest_source="/tmp/source",
    )
    collection_id = str(payload["collection_id"])
    upload_session = service.create_or_resume_file_upload(collection_id, relpath)
    service.append_upload_chunk(
        collection_id,
        relpath,
        offset=int(upload_session["offset"]),
        checksum="sha256 " + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
        content=content,
    )

    upload_service = SqlAlchemyGlacierUploadService(
        config,
        archive_store,
        hot_store,
        upload_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert upload_service.process_due_uploads() == 1
    assert archive_store.uploads == 1
    assert [payload["event"] for payload in webhook_payloads] == [
        "collections.archive_retrying",
    ]
    assert webhook_payloads[0]["attempts"] == 1

    current = datetime(2026, 4, 20, 5, 0, tzinfo=UTC)
    assert upload_service.process_due_uploads() == 1
    assert archive_store.uploads == 2
    assert [payload["event"] for payload in webhook_payloads] == [
        "collections.archive_retrying",
    ]

    current = datetime(2026, 4, 21, 4, 0, 1, tzinfo=UTC)
    assert upload_service.process_due_uploads() == 1
    assert archive_store.uploads == 3
    assert [payload["event"] for payload in webhook_payloads] == [
        "collections.archive_retrying",
        "collections.archive_retrying",
    ]
    assert webhook_payloads[-1]["attempts"] == 3

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        assert upload.state == "archiving"
        assert upload.archive_phase == "retry_wait"
        assert upload.archive_failure == "archive bucket unavailable"
        assert upload.archive_last_failure_notification_at == "2026-04-21T04:00:01Z"
        assert session.get(CollectionRecord, collection_id) is None


def test_archive_validation_failure_stops_without_retrying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    webhook_payloads: list[dict[str, object]] = []
    monkeypatch.setattr(
        "riverhog_core.services.glacier_uploads.post_webhook",
        lambda *, config, payload: webhook_payloads.append(payload),
    )

    hot_store = _FakeHotStore()
    upload_store = _StreamingOnlyUploadStore()
    archive_store = _CountingArchiveStore()
    config = _config(
        sqlite_path,
        glacier_upload_retry_delay=timedelta(seconds=0),
        operator_webhook_url="http://example.invalid/webhook",
    )
    service = SqlAlchemyCollectionService(_config(sqlite_path), hot_store, upload_store)

    content = b"hello world\n"
    relpath = "albums/day-01.txt"
    payload = service.create_or_resume_upload(
        upload_slug="photos 2024",
        files=[
            {
                "path": relpath,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        ingest_source="/tmp/source",
    )
    collection_id = str(payload["collection_id"])
    upload_session = service.create_or_resume_file_upload(collection_id, relpath)
    service.append_upload_chunk(
        collection_id,
        relpath,
        offset=int(upload_session["offset"]),
        checksum="sha256 " + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
        content=content,
    )
    for target_path in list(upload_store._content_by_target):
        upload_store._content_by_target[target_path] = b"hello world?"

    upload_service = SqlAlchemyGlacierUploadService(
        config,
        archive_store,
        hot_store,
        upload_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert upload_service.process_due_uploads() == 1
    assert archive_store.uploads == 0
    assert [payload["event"] for payload in webhook_payloads] == [
        "collections.archive_failed",
    ]
    assert webhook_payloads[0]["operator_urgency"] == "critical"
    assert "sha256 mismatch" in str(webhook_payloads[0]["error"])

    assert upload_service.process_due_uploads() == 0

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        assert upload.state == "failed"
        assert upload.archive_phase == "failed"
        assert upload.archive_next_attempt_at is None
        assert "sha256 mismatch" in str(upload.archive_failure)
        assert session.get(CollectionRecord, collection_id) is None


def test_startup_requeue_resumes_failed_archive_after_fix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    webhook_payloads: list[dict[str, object]] = []
    monkeypatch.setattr(
        "riverhog_core.services.glacier_uploads.post_webhook",
        lambda *, config, payload: webhook_payloads.append(payload),
    )

    hot_store = _FakeHotStore()
    upload_store = _StreamingOnlyUploadStore()
    archive_store = _CountingArchiveStore()
    config = _config(
        sqlite_path,
        glacier_upload_retry_delay=timedelta(seconds=0),
        operator_webhook_url="http://example.invalid/webhook",
    )
    service = SqlAlchemyCollectionService(_config(sqlite_path), hot_store, upload_store)

    content = b"hello world\n"
    relpath = "albums/day-01.txt"
    payload = service.create_or_resume_upload(
        upload_slug="photos 2024",
        files=[
            {
                "path": relpath,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        ingest_source="/tmp/source",
    )
    collection_id = str(payload["collection_id"])
    upload_session = service.create_or_resume_file_upload(collection_id, relpath)
    service.append_upload_chunk(
        collection_id,
        relpath,
        offset=int(upload_session["offset"]),
        checksum="sha256 " + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
        content=content,
    )
    for target_path in list(upload_store._content_by_target):
        upload_store._content_by_target[target_path] = b"hello world?"

    upload_service = SqlAlchemyGlacierUploadService(
        config,
        archive_store,
        hot_store,
        upload_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert upload_service.process_due_uploads() == 1
    assert archive_store.uploads == 0
    assert [payload["event"] for payload in webhook_payloads] == [
        "collections.archive_failed",
    ]

    for target_path in list(upload_store._content_by_target):
        upload_store._content_by_target[target_path] = content

    assert upload_service.requeue_failed_uploads_for_startup() == 1
    assert upload_service.process_due_uploads() == 1
    assert archive_store.uploads == 1
    assert [payload["event"] for payload in webhook_payloads] == [
        "collections.archive_failed",
        "collections.finalized",
    ]

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is None
        assert session.get(CollectionRecord, collection_id) is not None


def test_startup_requeue_renotifies_when_deterministic_archive_failure_remains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    webhook_payloads: list[dict[str, object]] = []
    monkeypatch.setattr(
        "riverhog_core.services.glacier_uploads.post_webhook",
        lambda *, config, payload: webhook_payloads.append(payload),
    )

    hot_store = _FakeHotStore()
    upload_store = _StreamingOnlyUploadStore()
    archive_store = _CountingArchiveStore()
    config = _config(
        sqlite_path,
        glacier_upload_retry_delay=timedelta(seconds=0),
        operator_webhook_url="http://example.invalid/webhook",
    )
    service = SqlAlchemyCollectionService(_config(sqlite_path), hot_store, upload_store)

    content = b"hello world\n"
    relpath = "albums/day-01.txt"
    payload = service.create_or_resume_upload(
        upload_slug="photos 2024",
        files=[
            {
                "path": relpath,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        ingest_source="/tmp/source",
    )
    collection_id = str(payload["collection_id"])
    upload_session = service.create_or_resume_file_upload(collection_id, relpath)
    service.append_upload_chunk(
        collection_id,
        relpath,
        offset=int(upload_session["offset"]),
        checksum="sha256 " + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
        content=content,
    )
    for target_path in list(upload_store._content_by_target):
        upload_store._content_by_target[target_path] = b"hello world?"

    upload_service = SqlAlchemyGlacierUploadService(
        config,
        archive_store,
        hot_store,
        upload_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert upload_service.process_due_uploads() == 1
    assert upload_service.requeue_failed_uploads_for_startup() == 1
    assert upload_service.process_due_uploads() == 1

    assert archive_store.uploads == 0
    assert [payload["event"] for payload in webhook_payloads] == [
        "collections.archive_failed",
        "collections.archive_failed",
    ]
    assert all(payload["operator_urgency"] == "critical" for payload in webhook_payloads)


def test_successful_archive_emits_only_finalized_operator_webhook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    webhook_payloads: list[dict[str, object]] = []
    monkeypatch.setattr(
        "riverhog_core.services.glacier_uploads.post_webhook",
        lambda *, config, payload: webhook_payloads.append(payload),
    )

    hot_store = _FakeHotStore()
    upload_store = _StreamingOnlyUploadStore()
    archive_store = _CountingArchiveStore()
    config = _config(
        sqlite_path,
        operator_webhook_url="http://example.invalid/webhook",
    )
    service = SqlAlchemyCollectionService(config, hot_store, upload_store)

    content = b"hello world\n"
    relpath = "albums/day-01.txt"
    payload = service.create_or_resume_upload(
        upload_slug="photos 2024",
        files=[
            {
                "path": relpath,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        ingest_source="/tmp/source",
    )
    collection_id = str(payload["collection_id"])
    upload_session = service.create_or_resume_file_upload(collection_id, relpath)
    service.append_upload_chunk(
        collection_id,
        relpath,
        offset=int(upload_session["offset"]),
        checksum="sha256 " + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
        content=content,
    )

    upload_service = SqlAlchemyGlacierUploadService(
        config,
        archive_store,
        hot_store,
        upload_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert upload_service.process_due_uploads() == 1

    assert archive_store.uploads == 1
    assert [payload["event"] for payload in webhook_payloads] == ["collections.finalized"]
    assert webhook_payloads[0]["collection_id"] == collection_id
    assert webhook_payloads[0]["archive_object_path"].endswith("/archive.tar.age")
    assert webhook_payloads[0]["files_uploaded"] == 1


def test_finalized_collection_upload_is_idempotent_for_same_slug_and_manifest(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    hot_store = _FakeHotStore()
    upload_store = _FakeUploadStore()
    config = _config(sqlite_path)
    service = SqlAlchemyCollectionService(config, hot_store, upload_store)

    content = b"hello world\n"
    relpath = "albums/day-01.txt"
    files = [
        {
            "path": relpath,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    ]
    initial = service.create_or_resume_upload(upload_slug="Photos 2024", files=files)
    collection_id = str(initial["collection_id"])
    session = service.create_or_resume_file_upload(collection_id, relpath)
    service.append_upload_chunk(
        collection_id,
        relpath,
        offset=int(session["offset"]),
        checksum="sha256 " + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
        content=content,
    )

    upload_service = SqlAlchemyGlacierUploadService(
        config,
        _FakeArchiveStore(),
        hot_store,
        upload_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )
    assert upload_service.process_due_uploads() == 1

    resumed = service.create_or_resume_upload(upload_slug="photos 2024", files=files)

    assert resumed["collection_id"] == collection_id
    assert resumed["state"] == "finalized"
    assert resumed["files_uploaded"] == 1
    assert isinstance(resumed["collection"], dict)


def test_same_upload_slug_with_different_manifest_mints_distinct_collection_id(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    service = SqlAlchemyCollectionService(
        _config(sqlite_path),
        _FakeHotStore(),
        _FakeUploadStore(),
    )

    first = b"first\n"
    second = b"second\n"
    first_payload = service.create_or_resume_upload(
        upload_slug="Mom iPhone Photos",
        files=[
            {
                "path": "one.txt",
                "bytes": len(first),
                "sha256": hashlib.sha256(first).hexdigest(),
            }
        ],
    )
    second_payload = service.create_or_resume_upload(
        upload_slug="mom iphone photos",
        files=[
            {
                "path": "two.txt",
                "bytes": len(second),
                "sha256": hashlib.sha256(second).hexdigest(),
            }
        ],
    )

    assert first_payload["collection_id"] != second_payload["collection_id"]
    assert str(first_payload["collection_id"]).endswith("__mom-iphone-photos")
    assert str(second_payload["collection_id"]).endswith("__mom-iphone-photos")


def test_collection_upload_can_use_explicit_migration_timestamp(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    service = SqlAlchemyCollectionService(
        _config(sqlite_path),
        _FakeHotStore(),
        _FakeUploadStore(),
    )

    content = b"migrated payload\n"
    payload = service.create_or_resume_upload(
        upload_slug="Mom iPhone Photos",
        upload_timestamp="20250712T213200Z",
        files=[
            {
                "path": "one.txt",
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    )

    assert payload["collection_id"] == "2025/20250712T213200Z__mom-iphone-photos"


def test_matching_upload_with_different_explicit_timestamp_is_rejected(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    service = SqlAlchemyCollectionService(
        _config(sqlite_path),
        _FakeHotStore(),
        _FakeUploadStore(),
    )

    content = b"migrated payload\n"
    files = [
        {
            "path": "one.txt",
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    ]
    service.create_or_resume_upload(
        upload_slug="Mom iPhone Photos",
        upload_timestamp="20250712T213200Z",
        files=files,
    )

    with pytest.raises(Conflict, match="different timestamp"):
        service.create_or_resume_upload(
            upload_slug="mom iphone photos",
            upload_timestamp="20250713T213200Z",
            files=files,
        )


def test_completed_glacier_upload_refreshes_provisional_disc_plan(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    config = _config(
        sqlite_path,
        planner_disc_target_bytes=10_000_000,
        planner_min_fill_bytes=1,
        planner_image_root=tmp_path / "images",
    )
    hot_store = _FakeHotStore()
    upload_store = _FakeUploadStore()
    service = SqlAlchemyCollectionService(config, hot_store, upload_store)

    content = b"planner payload\n"
    sha256 = hashlib.sha256(content).hexdigest()
    upload_slug = "photos 2024"
    relpath = "albums/day-01.txt"

    payload = service.create_or_resume_upload(
        upload_slug=upload_slug,
        files=[{"path": relpath, "bytes": len(content), "sha256": sha256}],
        ingest_source="/tmp/source",
    )
    collection_id = str(payload["collection_id"])
    upload_session = service.create_or_resume_file_upload(collection_id, relpath)
    service.append_upload_chunk(
        collection_id,
        relpath,
        offset=int(upload_session["offset"]),
        checksum="sha256 " + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
        content=content,
    )

    upload_service = SqlAlchemyGlacierUploadService(
        config,
        _FakeArchiveStore(),
        hot_store,
        upload_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )
    assert upload_service.process_due_uploads() == 1

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        candidate = session.scalars(select(PlannedCandidateRecord)).one()
        image_root = Path(candidate.image_root)
        assert candidate.target_bytes == 10_000_000
        assert candidate.min_fill_bytes == 1
        assert candidate.iso_ready is True
        assert [(cp.collection_id, cp.path) for cp in candidate.covered_paths] == [
            (collection_id, relpath)
        ]

    assert (image_root / MANIFEST_FILENAME).exists()
    assert (
        read_finalized_image_coverage_parts(
            image_root,
            FixtureRecoveryPayloadCodec(),
        )[0].path
        == relpath
    )
    assert (
        read_finalized_image_collection_artifacts(
            image_root,
            FixtureRecoveryPayloadCodec(),
        )[0].collection_id
        == collection_id
    )


def test_ready_disc_candidate_sends_operator_webhook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    config = _config(
        sqlite_path,
        planner_disc_target_bytes=10_000_000,
        planner_min_fill_bytes=1,
        planner_image_root=tmp_path / "images",
        operator_webhook_url="http://example.invalid/webhook",
        operator_webhook_reminder_interval=timedelta(hours=1),
    )
    hot_store = _FakeHotStore()
    upload_store = _FakeUploadStore()
    service = SqlAlchemyCollectionService(config, hot_store, upload_store)

    content = b"planner payload\n"
    sha256 = hashlib.sha256(content).hexdigest()
    relpath = "albums/day-01.txt"

    payload = service.create_or_resume_upload(
        upload_slug="photos 2024",
        files=[{"path": relpath, "bytes": len(content), "sha256": sha256}],
        ingest_source="/tmp/source",
    )
    collection_id = str(payload["collection_id"])
    upload_session = service.create_or_resume_file_upload(collection_id, relpath)
    service.append_upload_chunk(
        collection_id,
        relpath,
        offset=int(upload_session["offset"]),
        checksum="sha256 " + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
        content=content,
    )

    upload_service = SqlAlchemyGlacierUploadService(
        config,
        _FakeArchiveStore(),
        hot_store,
        upload_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )
    assert upload_service.process_due_uploads() == 1

    webhook_payloads: list[dict[str, object]] = []
    monkeypatch.setattr(
        "riverhog_core.services.planning.post_webhook",
        lambda *, config, payload: webhook_payloads.append(payload),
    )
    planning = SqlAlchemyPlanningService(
        config,
        hot_store,
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert planning.process_due_refresh() == 1
    assert planning.process_due_refresh() == 0

    assert [payload["event"] for payload in webhook_payloads] == ["images.ready"]
    image = webhook_payloads[0]["images"][0]
    assert image["filename"].endswith(".iso")
    assert image["download_url"].endswith(f"/v1/images/{image['image_id']}/iso")
    with session_scope(make_session_factory(sqlite_url(sqlite_path))) as session:
        candidate = session.scalars(select(PlannedCandidateRecord)).one()
        assert candidate.ready_notification_sent_at is not None
        assert candidate.ready_notification_next_attempt_at is not None
        assert candidate.ready_notification_count == 1


def test_provisional_disc_plan_materialization_resumes_partial_candidate(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    config = _config(
        sqlite_path,
        planner_disc_target_bytes=10_000_000,
        planner_min_fill_bytes=1,
        planner_image_root=tmp_path / "images",
    )
    collection_id = "2026/20260525T000000Z__docs"
    path = "albums/day-01.txt"
    content = b"planner payload\n"
    sha256 = hashlib.sha256(content).hexdigest()
    hot_store = _FakeHotStore()
    hot_store.put_collection_file(collection_id, path, content)

    archive_package = build_collection_archive_package(
        collection_id=collection_id,
        files=(
            CollectionArchiveFile(
                path=path,
                content=content,
                sha256=sha256,
            ),
        ),
        stamper=FixtureProofStamper(),
    )
    package = _FakeArchiveStore().upload_collection_archive_package(
        collection_id=collection_id,
        package=archive_package,
    )
    cache_collection_manifest_artifacts(
        config,
        collection_id=collection_id,
        manifest_bytes=archive_package.manifest_bytes,
        proof_bytes=archive_package.proof_bytes,
    )
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        collection = CollectionRecord(id=collection_id)
        collection.files.append(
            CollectionFileRecord(
                collection_id=collection_id,
                path=path,
                bytes=len(content),
                sha256=sha256,
                hot=True,
                archived=False,
            )
        )
        session.add(collection)
        session.add(
            CollectionArchiveRecord(
                collection_id=collection_id,
                state="uploaded",
                object_path=package.archive.object_path,
                stored_bytes=package.archive.stored_bytes,
                sha256=package.archive_sha256,
                backend="s3",
                storage_class="DEEP_ARCHIVE",
                manifest_object_path=package.manifest.object_path,
                manifest_sha256=package.manifest_sha256,
                manifest_stored_bytes=package.manifest.stored_bytes,
                ots_object_path=package.proof.object_path,
                ots_sha256=package.proof_sha256,
                ots_stored_bytes=package.proof.stored_bytes,
            )
        )

    with pytest.raises(RuntimeError, match="synthetic planner encryption failure"):
        refresh_provisional_plan(
            config=config,
            hot_store=hot_store,
            recovery_payload_codec=_FailOnceRecoveryPayloadCodec(fail_after_successes=1),
        )
    with session_scope(session_factory) as session:
        failed = session.scalars(select(PlannedCandidateRecord)).one()
        assert failed.state == "failed"
        failed_candidate_id = failed.candidate_id
        tmp_root = Path(failed.image_root).with_name(f".{failed.candidate_id}.tmp")
        assert tmp_root.exists()
        assert any(path.is_file() for path in tmp_root.rglob("*"))

    refresh_provisional_plan(
        config=config,
        hot_store=hot_store,
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    with session_scope(session_factory) as session:
        candidate = session.scalars(select(PlannedCandidateRecord)).one()
        assert candidate.candidate_id == failed_candidate_id
        assert candidate.state == "ready"
        assert candidate.failure is None
        assert Path(candidate.image_root).exists()
        assert not Path(candidate.image_root).with_name(f".{candidate.candidate_id}.tmp").exists()


def test_underfilled_tail_candidate_waits_without_materializing(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    config = _config(
        sqlite_path,
        planner_disc_target_bytes=10_000_000,
        planner_min_fill_bytes=9_000_000,
        planner_image_root=tmp_path / "images",
    )
    collection_id = "2026/20260525T000000Z__tail"
    path = "tail.txt"
    content = b"tail payload\n"
    sha256 = hashlib.sha256(content).hexdigest()
    hot_store = _FakeHotStore()
    hot_store.put_collection_file(collection_id, path, content)

    archive_package = build_collection_archive_package(
        collection_id=collection_id,
        files=(
            CollectionArchiveFile(
                path=path,
                content=content,
                sha256=sha256,
            ),
        ),
        stamper=FixtureProofStamper(),
    )
    package = _FakeArchiveStore().upload_collection_archive_package(
        collection_id=collection_id,
        package=archive_package,
    )
    cache_collection_manifest_artifacts(
        config,
        collection_id=collection_id,
        manifest_bytes=archive_package.manifest_bytes,
        proof_bytes=archive_package.proof_bytes,
    )
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        collection = CollectionRecord(id=collection_id)
        collection.files.append(
            CollectionFileRecord(
                collection_id=collection_id,
                path=path,
                bytes=len(content),
                sha256=sha256,
                hot=True,
                archived=False,
            )
        )
        session.add(collection)
        session.add(
            CollectionArchiveRecord(
                collection_id=collection_id,
                state="uploaded",
                object_path=package.archive.object_path,
                stored_bytes=package.archive.stored_bytes,
                sha256=package.archive_sha256,
                backend="s3",
                storage_class="DEEP_ARCHIVE",
                manifest_object_path=package.manifest.object_path,
                manifest_sha256=package.manifest_sha256,
                manifest_stored_bytes=package.manifest.stored_bytes,
                ots_object_path=package.proof.object_path,
                ots_sha256=package.proof_sha256,
                ots_stored_bytes=package.proof.stored_bytes,
            )
        )

    planning = SqlAlchemyPlanningService(
        config,
        hot_store,
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert planning.process_due_refresh() == 1
    assert planning.process_due_refresh() == 0

    with session_scope(session_factory) as session:
        candidate = session.scalars(select(PlannedCandidateRecord)).one()
        assert candidate.state == "waiting"
        assert candidate.bytes == 0
        assert candidate.iso_ready is False
        assert [(cp.collection_id, cp.path) for cp in candidate.covered_paths] == [
            (collection_id, path)
        ]
        image_root = Path(candidate.image_root)
        assert not image_root.exists()
        assert not image_root.with_name(f".{candidate.candidate_id}.tmp").exists()

    plan = planning.get_plan(
        page=1,
        per_page=25,
        sort="fill",
        order="desc",
        q=None,
        collection=None,
        iso_ready=None,
    )
    assert plan["ready"] is False
    assert plan["candidates"] == []
    assert plan["unplanned_bytes"] == len(content)

    with session_scope(session_factory) as session:
        candidate = session.scalars(select(PlannedCandidateRecord)).one()
        candidate_id = candidate.candidate_id
        candidate.plan_fingerprint = "stale"

    assert planning.process_due_refresh() == 1

    with session_scope(session_factory) as session:
        candidate = session.get(PlannedCandidateRecord, candidate_id)
        assert candidate is not None
        assert candidate.state == "waiting"
        assert candidate.plan_fingerprint != "stale"


def test_saturated_underfilled_candidate_still_waits_without_split_path(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    config = _config(
        sqlite_path,
        planner_disc_target_bytes=10_000_000,
        planner_min_fill_bytes=9_000_000,
        planner_unplanned_saturation_bytes=1,
        planner_image_root=tmp_path / "images",
    )
    collection_id = "2026/20260525T000000Z__saturated-tail"
    path = "tail.txt"
    content = b"tail payload\n"
    sha256 = hashlib.sha256(content).hexdigest()
    hot_store = _FakeHotStore()
    hot_store.put_collection_file(collection_id, path, content)

    archive_package = build_collection_archive_package(
        collection_id=collection_id,
        files=(
            CollectionArchiveFile(
                path=path,
                content=content,
                sha256=sha256,
            ),
        ),
        stamper=FixtureProofStamper(),
    )
    package = _FakeArchiveStore().upload_collection_archive_package(
        collection_id=collection_id,
        package=archive_package,
    )
    cache_collection_manifest_artifacts(
        config,
        collection_id=collection_id,
        manifest_bytes=archive_package.manifest_bytes,
        proof_bytes=archive_package.proof_bytes,
    )
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        collection = CollectionRecord(id=collection_id)
        collection.files.append(
            CollectionFileRecord(
                collection_id=collection_id,
                path=path,
                bytes=len(content),
                sha256=sha256,
                hot=True,
                archived=False,
            )
        )
        session.add(collection)
        session.add(
            CollectionArchiveRecord(
                collection_id=collection_id,
                state="uploaded",
                object_path=package.archive.object_path,
                stored_bytes=package.archive.stored_bytes,
                sha256=package.archive_sha256,
                backend="s3",
                storage_class="DEEP_ARCHIVE",
                manifest_object_path=package.manifest.object_path,
                manifest_sha256=package.manifest_sha256,
                manifest_stored_bytes=package.manifest.stored_bytes,
                ots_object_path=package.proof.object_path,
                ots_sha256=package.proof_sha256,
                ots_stored_bytes=package.proof.stored_bytes,
            )
        )

    planning = SqlAlchemyPlanningService(
        config,
        hot_store,
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert planning.process_due_refresh() == 1

    with session_scope(session_factory) as session:
        candidate = session.scalars(select(PlannedCandidateRecord)).one()
        assert candidate.state == "waiting"
        assert candidate.bytes < candidate.min_fill_bytes
        assert candidate.iso_ready is False
        assert [(cp.collection_id, cp.path) for cp in candidate.covered_paths] == [
            (collection_id, path)
        ]
        assert not Path(candidate.image_root).exists()


def test_planner_continues_after_partially_finalized_collection(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    config = _config(
        sqlite_path,
        planner_disc_target_bytes=10_000_000,
        planner_min_fill_bytes=9_000_000,
        planner_image_root=tmp_path / "images",
    )
    collection_id = "2026/20260525T000000Z__partial"
    covered_path = "covered.txt"
    tail_path = "tail.txt"
    covered_content = b"covered payload\n"
    tail_content = b"tail payload\n"
    hot_store = _FakeHotStore()
    hot_store.put_collection_file(collection_id, covered_path, covered_content)
    hot_store.put_collection_file(collection_id, tail_path, tail_content)

    archive_package = build_collection_archive_package(
        collection_id=collection_id,
        files=(
            CollectionArchiveFile(
                path=covered_path,
                content=covered_content,
                sha256=hashlib.sha256(covered_content).hexdigest(),
            ),
            CollectionArchiveFile(
                path=tail_path,
                content=tail_content,
                sha256=hashlib.sha256(tail_content).hexdigest(),
            ),
        ),
        stamper=FixtureProofStamper(),
    )
    package = _FakeArchiveStore().upload_collection_archive_package(
        collection_id=collection_id,
        package=archive_package,
    )
    cache_collection_manifest_artifacts(
        config,
        collection_id=collection_id,
        manifest_bytes=archive_package.manifest_bytes,
        proof_bytes=archive_package.proof_bytes,
    )
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        collection = CollectionRecord(id=collection_id)
        for path, content in (
            (covered_path, covered_content),
            (tail_path, tail_content),
        ):
            collection.files.append(
                CollectionFileRecord(
                    collection_id=collection_id,
                    path=path,
                    bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    hot=True,
                    archived=False,
                )
            )
        session.add(collection)
        session.add(
            CollectionArchiveRecord(
                collection_id=collection_id,
                state="uploaded",
                object_path=package.archive.object_path,
                stored_bytes=package.archive.stored_bytes,
                sha256=package.archive_sha256,
                backend="s3",
                storage_class="DEEP_ARCHIVE",
                manifest_object_path=package.manifest.object_path,
                manifest_sha256=package.manifest_sha256,
                manifest_stored_bytes=package.manifest.stored_bytes,
                ots_object_path=package.proof.object_path,
                ots_sha256=package.proof_sha256,
                ots_stored_bytes=package.proof.stored_bytes,
            )
        )
        session.add(
            FinalizedImageRecord(
                image_id="20260526T011347Z",
                candidate_id="candidate-covered",
                filename="20260526T011347Z.iso",
                bytes=len(covered_content),
                image_root=str(tmp_path / "finalized-image"),
                target_bytes=10_000_000,
                required_copy_count=2,
            )
        )
        session.add(
            FinalizedImageCoveredPathRecord(
                image_id="20260526T011347Z",
                collection_id=collection_id,
                path=covered_path,
            )
        )

    planning = SqlAlchemyPlanningService(
        config,
        hot_store,
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    with session_scope(session_factory) as session:
        plan_files = _load_plan_files(session, config)
        assert len(plan_files) == 1
        assert plan_files[0].path == tail_path
        assert plan_files[0].collection_optional_split_allowed is False

    assert planning.process_due_refresh() == 1

    with session_scope(session_factory) as session:
        candidate = session.scalars(select(PlannedCandidateRecord)).one()
        assert candidate.state == "waiting"
        assert candidate.iso_ready is False
        assert [(cp.collection_id, cp.path) for cp in candidate.covered_paths] == [
            (collection_id, tail_path)
        ]

    plan = planning.get_plan(
        page=1,
        per_page=25,
        sort="fill",
        order="desc",
        q=None,
        collection=None,
        iso_ready=None,
    )
    assert plan["ready"] is False
    assert plan["candidates"] == []
    assert plan["unplanned_bytes"] == len(tail_content)


def test_planner_ignores_fully_finalized_collection(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    config = _config(sqlite_path)
    collection_id = "2026/20260529T000000Z__done"
    relpath = "done.txt"
    content = b"already planned\n"

    archive_package = build_collection_archive_package(
        collection_id=collection_id,
        files=(
            CollectionArchiveFile(
                path=relpath,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            ),
        ),
        stamper=FixtureProofStamper(),
    )
    package = _FakeArchiveStore().upload_collection_archive_package(
        collection_id=collection_id,
        package=archive_package,
    )
    cache_collection_manifest_artifacts(
        config,
        collection_id=collection_id,
        manifest_bytes=archive_package.manifest_bytes,
        proof_bytes=archive_package.proof_bytes,
    )

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        collection = CollectionRecord(id=collection_id)
        collection.files.append(
            CollectionFileRecord(
                collection_id=collection_id,
                path=relpath,
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                hot=True,
                archived=False,
            )
        )
        session.add(collection)
        session.add(
            CollectionArchiveRecord(
                collection_id=collection_id,
                state="uploaded",
                object_path=package.archive.object_path,
                stored_bytes=package.archive.stored_bytes,
                sha256=package.archive_sha256,
                backend="s3",
                storage_class="DEEP_ARCHIVE",
                manifest_object_path=package.manifest.object_path,
                manifest_sha256=package.manifest_sha256,
                manifest_stored_bytes=package.manifest.stored_bytes,
                ots_object_path=package.proof.object_path,
                ots_sha256=package.proof_sha256,
                ots_stored_bytes=package.proof.stored_bytes,
            )
        )
        session.add(
            FinalizedImageRecord(
                image_id="20260529T000000Z",
                candidate_id="candidate-done",
                filename="20260529T000000Z.iso",
                bytes=len(content),
                image_root=str(tmp_path / "finalized-image"),
                target_bytes=10_000_000,
                required_copy_count=2,
            )
        )
        session.add(
            FinalizedImageCoveredPathRecord(
                image_id="20260529T000000Z",
                collection_id=collection_id,
                path=relpath,
            )
        )

    with session_scope(session_factory) as session:
        caplog.set_level(logging.INFO, logger="riverhog_core.services.planning")
        assert _load_plan_files(session, config) == []

    assert "planner refresh loaded plan files" not in caplog.text


def test_new_collection_uploads_are_blocked_over_unburned_limit(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    hot_store = _FakeHotStore()
    upload_store = _FakeUploadStore()
    service = SqlAlchemyCollectionService(
        _config(sqlite_path, unburned_collection_bytes_limit=10),
        hot_store,
        upload_store,
    )

    first = b"12345678"
    service.create_or_resume_upload(
        upload_slug="first",
        files=[
            {
                "path": "a.txt",
                "bytes": len(first),
                "sha256": hashlib.sha256(first).hexdigest(),
            }
        ],
    )

    second = b"123"
    with pytest.raises(Conflict, match="unburned collection limit exceeded"):
        service.create_or_resume_upload(
            upload_slug="second",
            files=[
                {
                    "path": "b.txt",
                    "bytes": len(second),
                    "sha256": hashlib.sha256(second).hexdigest(),
                }
            ],
        )


def test_new_collection_upload_limit_uses_fast_committed_byte_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        collection = CollectionRecord(id="2026/20260101T000000Z__docs")
        collection.files.append(
            CollectionFileRecord(
                collection_id=collection.id,
                path="a.txt",
                bytes=8,
                sha256="0" * 64,
                hot=True,
                archived=False,
            )
        )
        session.add(collection)

    def fail_summary(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unburned admission check must not build collection summaries")

    monkeypatch.setattr(collections_service, "_summary_from_records", fail_summary)
    monkeypatch.setattr(collections_service, "_collection_image_coverage", fail_summary)

    service = SqlAlchemyCollectionService(
        _config(sqlite_path, unburned_collection_bytes_limit=7),
        _FakeHotStore(),
        _FakeUploadStore(),
    )

    with pytest.raises(Conflict, match="unburned collection limit exceeded"):
        service.create_or_resume_upload_session(
            upload_slug="camera",
            upload_timestamp="20260102T000000Z",
        )


def test_unburned_collection_bytes_excludes_protected_files(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        collection = CollectionRecord(id="2026/20260101T000000Z__docs")
        collection.files.append(
            CollectionFileRecord(
                collection_id=collection.id,
                path="protected.txt",
                bytes=8,
                sha256="0" * 64,
                hot=True,
                archived=False,
            )
        )
        collection.files.append(
            CollectionFileRecord(
                collection_id=collection.id,
                path="loose.txt",
                bytes=5,
                sha256="1" * 64,
                hot=True,
                archived=False,
            )
        )
        session.add(collection)
        session.add(
            FinalizedImageRecord(
                image_id="20260103T000000Z",
                candidate_id="candidate",
                filename="disc.iso",
                bytes=8,
                image_root="/tmp/disc",
                target_bytes=8,
                required_copy_count=1,
            )
        )
        session.add(
            FinalizedImageCoveredPathRecord(
                image_id="20260103T000000Z",
                collection_id=collection.id,
                path="protected.txt",
            )
        )
        session.add(
            ImageCopyRecord(
                image_id="20260103T000000Z",
                copy_id="copy-1",
                label_text="disc",
                location=None,
                created_at="2026-01-03T00:00:00Z",
                state="registered",
                verification_state="pending",
            )
        )

    with session_scope(session_factory) as session:
        assert collections_service._unburned_collection_bytes(session) == 5


def test_existing_collection_upload_can_resume_when_over_unburned_limit(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    hot_store = _FakeHotStore()
    upload_store = _FakeUploadStore()
    initial = SqlAlchemyCollectionService(
        _config(sqlite_path, unburned_collection_bytes_limit=10),
        hot_store,
        upload_store,
    )

    content = b"12345678"
    files = [
        {
            "path": "a.txt",
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    ]
    initial_payload = initial.create_or_resume_upload(upload_slug="first", files=files)

    stricter = SqlAlchemyCollectionService(
        _config(sqlite_path, unburned_collection_bytes_limit=7),
        hot_store,
        upload_store,
    )

    resumed = stricter.create_or_resume_upload(upload_slug="first", files=files)

    assert resumed["collection_id"] == initial_payload["collection_id"]


def test_collection_summary_does_not_count_finalized_image_parts_as_glacier_recovery(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))

    hot_store = _FakeHotStore()
    upload_store = _FakeUploadStore()
    service = SqlAlchemyCollectionService(_config(sqlite_path), hot_store, upload_store)

    image_root = tmp_path / "image-root"
    image_root.mkdir(parents=True, exist_ok=True)
    _seed_docs_collection_with_finalized_image(sqlite_path, image_root)

    summary = service.get("docs")

    assert summary.recovery.verified_physical.state.value == "none"
    assert summary.recovery.glacier.state.value == "none"
