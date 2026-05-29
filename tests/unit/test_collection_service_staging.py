from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from riverhog_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionProtectionMirrorRecord,
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
    CollectionArchivePackage,
    build_collection_archive_package,
)
from riverhog_core.domain.errors import Conflict
from riverhog_core.finalized_image_coverage import (
    read_finalized_image_collection_artifacts,
    read_finalized_image_coverage_parts,
)
from riverhog_core.planner.manifest import MANIFEST_FILENAME
from riverhog_core.ports.archive_store import ArchiveUploadReceipt, CollectionArchiveUploadReceipt
from riverhog_core.ports.hot_store import HotFileStat
from riverhog_core.ports.protection_mirror import ProtectionMirrorArchiveStat
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collections import SqlAlchemyCollectionService
from riverhog_core.services.glacier_uploads import SqlAlchemyGlacierUploadService
from riverhog_core.services.planning import (
    SqlAlchemyPlanningService,
    _load_plan_files,
    cache_collection_manifest_artifacts,
    refresh_provisional_plan,
)
from riverhog_core.sqlite_db import initialize_db, make_session_factory, session_scope
from tests.fixtures.crypto import FixtureProofStamper, FixtureRecoveryPayloadCodec
from tests.fixtures.data import DOCS_FILES


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

    def iter_target(self, target_path: str) -> Iterator[bytes]:
        yield self.read_target(target_path)

    def delete_target(self, target_path: str) -> None:
        self.deleted_targets.append(target_path)
        self._content_by_target.pop(target_path, None)

    def cancel_upload(self, tus_url: str) -> None:
        self._target_by_url.pop(tus_url, None)


class _StreamingOnlyUploadStore(_FakeUploadStore):
    def read_target(self, target_path: str) -> bytes:
        raise AssertionError(f"read_target should not be used for upload promotion: {target_path}")

    def iter_target(self, target_path: str) -> Iterator[bytes]:
        content = self._content_by_target[target_path]
        midpoint = len(content) // 2
        yield content[:midpoint]
        yield content[midpoint:]


class _FakeArchiveStore:
    def upload_collection_archive_package(self, *, collection_id, package, multipart_tracker=None):
        _ = multipart_tracker
        object_path = f"glacier/collections/{collection_id}/archive.tar"
        manifest_object_path = f"glacier/collections/{collection_id}/manifest.yml"
        proof_object_path = f"glacier/collections/{collection_id}/manifest.yml.ots"
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


class _FakeProtectionMirrorStore:
    def __init__(self) -> None:
        self.archives: dict[str, bytes] = {}
        self.uploads = 0
        self.uploaded_collections: list[str] = []
        self.deleted: list[str] = []

    def object_path(self, collection_id: str) -> str:
        return f"mirror/collections/{collection_id}/archive.tar"

    def put_collection_archive_stream_resumable(
        self,
        collection_id: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
        sha256: str,
        multipart_tracker=None,
    ) -> None:
        _ = multipart_tracker
        content = b"".join(chunks)
        assert len(content) == content_length
        assert hashlib.sha256(content).hexdigest() == sha256
        self.archives[collection_id] = content
        self.uploaded_collections.append(collection_id)
        self.uploads += 1

    def iter_collection_archive(
        self,
        collection_id: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        content = self.archives[collection_id]
        yield content[offset:] if size is None else content[offset : offset + size]

    def stat_collection_archive(self, collection_id: str) -> ProtectionMirrorArchiveStat | None:
        content = self.archives.get(collection_id)
        if content is None:
            return None
        return ProtectionMirrorArchiveStat(
            bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def delete_collection(self, collection_id: str) -> None:
        self.deleted.append(collection_id)
        self.archives.pop(collection_id, None)


class _FailingProtectionMirrorStore(_FakeProtectionMirrorStore):
    def put_collection_archive_stream_resumable(
        self,
        collection_id: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
        sha256: str,
        multipart_tracker=None,
    ) -> None:
        _ = collection_id, chunks, content_length, sha256, multipart_tracker
        raise RuntimeError("mirror bucket unavailable")


class _CountingArchiveStore(_FakeArchiveStore):
    def __init__(self) -> None:
        self.uploads = 0

    def upload_collection_archive_package(self, *, collection_id, package, multipart_tracker=None):
        self.uploads += 1
        return super().upload_collection_archive_package(
            collection_id=collection_id,
            package=package,
            multipart_tracker=multipart_tracker,
        )


class _RecordingArchiveStore(_FakeArchiveStore):
    def __init__(self) -> None:
        self.collection_ids: list[str] = []

    def upload_collection_archive_package(self, *, collection_id, package, multipart_tracker=None):
        self.collection_ids.append(collection_id)
        return super().upload_collection_archive_package(
            collection_id=collection_id,
            package=package,
            multipart_tracker=multipart_tracker,
        )


class _AlwaysFailingArchiveStore(_FakeArchiveStore):
    def __init__(self) -> None:
        self.uploads = 0

    def upload_collection_archive_package(self, *, collection_id, package, multipart_tracker=None):
        _ = collection_id, package, multipart_tracker
        self.uploads += 1
        raise RuntimeError("archive bucket unavailable")


class _FailOnceArchiveStore(_FakeArchiveStore):
    def __init__(self) -> None:
        self.uploads = 0

    def upload_collection_archive_package(self, *, collection_id, package, multipart_tracker=None):
        self.uploads += 1
        if self.uploads == 1:
            raise RuntimeError("archive bucket unavailable")
        return super().upload_collection_archive_package(
            collection_id=collection_id,
            package=package,
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
        sqlite_path=sqlite_path,
        **overrides,
    )


def _seed_docs_collection_with_finalized_image(sqlite_path: Path, image_root: Path) -> None:
    session_factory = make_session_factory(str(sqlite_path))
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
    initialize_db(str(sqlite_path))

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


def test_file_upload_resume_does_not_sync_unrelated_upload_files(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

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
    initialize_db(str(sqlite_path))

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
    initialize_db(str(sqlite_path))

    config = _config(sqlite_path)
    upload_store = _FakeUploadStore()
    session_factory = make_session_factory(str(sqlite_path))
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
            checksum="sha256 "
            + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
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
                    "archive-upload-1" if collection_id == resumed_id else None
                ),
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

    dashboard = SqlAlchemyCollectionService(
        config,
        _FakeHotStore(),
        upload_store,
    ).list_dashboard_collections(q="resume")
    assert dashboard["collections"] == []
    assert dashboard["active_uploads"][0]["collection_id"] == resumed_id
    assert dashboard["active_uploads"][0]["state"] == "archiving"
    assert dashboard["active_uploads"][0]["archive_phase"] == "uploading"

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


def test_completed_collection_upload_writes_protection_mirror_before_planning(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

    config = _config(
        sqlite_path,
        protection_mirror_enabled=True,
        protection_mirror_s3_endpoint_url="https://s3.example.invalid",
        protection_mirror_s3_bucket="mirror",
        protection_mirror_s3_access_key_id="mirror-key",
        protection_mirror_s3_secret_access_key="mirror-secret",
    )
    hot_store = _FakeHotStore()
    upload_store = _StreamingOnlyUploadStore()
    mirror_store = _FakeProtectionMirrorStore()
    service = SqlAlchemyCollectionService(config, hot_store, upload_store)

    content = b"hello mirrored world\n"
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
    )
    collection_id = str(payload["collection_id"])
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
        mirror_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )
    assert upload_service.process_due_uploads() == 1

    assert mirror_store.uploads == 1
    assert collection_id in mirror_store.archives
    session_factory = make_session_factory(str(sqlite_path))
    with session_scope(session_factory) as session:
        mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
        assert mirror is not None
        assert mirror.state == "complete"
        assert mirror.archive_bytes == len(mirror_store.archives[collection_id])
        assert session.get(CollectionRecord, collection_id) is not None


def test_protection_mirror_backfills_underprotected_and_cleans_after_verified_copies(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

    config = _config(
        sqlite_path,
        protection_mirror_enabled=True,
        protection_mirror_s3_endpoint_url="https://s3.example.invalid",
        protection_mirror_s3_bucket="mirror",
        protection_mirror_s3_access_key_id="mirror-key",
        protection_mirror_s3_secret_access_key="mirror-secret",
    )
    hot_store = _FakeHotStore()
    upload_store = _FakeUploadStore()
    mirror_store = _FakeProtectionMirrorStore()
    collection_id = "20250712T213200Z__photos"
    relpath = "albums/day-01.txt"
    content = b"existing collection bytes\n"
    sha256 = hashlib.sha256(content).hexdigest()
    hot_store.put_collection_file(collection_id, relpath, content)
    package = build_collection_archive_package(
        collection_id=collection_id,
        files=[CollectionArchiveFile(path=relpath, content=content, sha256=sha256)],
        stamper=FixtureProofStamper(),
    )

    session_factory = make_session_factory(str(sqlite_path))
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
                object_path=f"glacier/collections/{collection_id}/archive.tar",
                stored_bytes=package.archive_size,
                sha256=package.archive_sha256,
            )
        )

    upload_service = SqlAlchemyGlacierUploadService(
        config,
        _FakeArchiveStore(),
        hot_store,
        upload_store,
        mirror_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )
    assert upload_service.process_due_uploads() == 1
    assert mirror_store.archives[collection_id] == package.archive_bytes
    with session_scope(session_factory) as session:
        mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
        assert mirror is not None
        assert mirror.state == "complete"

        session.add(
            FinalizedImageRecord(
                image_id="20260528T210000Z",
                candidate_id="candidate-1",
                filename="20260528T210000Z.iso",
                bytes=10_000,
                image_root=str(tmp_path / "image-root"),
                target_bytes=50_000_000_000,
                required_copy_count=2,
            )
        )
        session.add(
            FinalizedImageCoveredPathRecord(
                image_id="20260528T210000Z",
                collection_id=collection_id,
                path=relpath,
            )
        )
        for copy_id in ("20260528T210000Z-1", "20260528T210000Z-2"):
            session.add(
                ImageCopyRecord(
                    image_id="20260528T210000Z",
                    copy_id=copy_id,
                    label_text=copy_id,
                    location="test shelf",
                    created_at="2026-05-28T21:00:00Z",
                    state="registered",
                    verification_state="verified",
                )
            )

    assert upload_service.process_due_uploads() == 1
    assert collection_id in mirror_store.deleted
    assert collection_id not in mirror_store.archives
    with session_scope(session_factory) as session:
        mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
        assert mirror is not None
        assert mirror.state == "deleted"

    collection_service = SqlAlchemyCollectionService(config, hot_store, upload_store)
    dashboard = collection_service.list_dashboard_collections(q="photos")
    dashboard_collection = dashboard["collections"][0]
    assert dashboard_collection["protection_state"] == "fully_protected"
    assert dashboard_collection["protection_mirror"] == {
        "enabled": True,
        "required": False,
        "state": "not_required",
        "bytes": 0,
        "failure": None,
    }


def test_protection_mirror_hot_repair_rewrites_only_missing_or_mismatched_files(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

    config = _config(
        sqlite_path,
        protection_mirror_enabled=True,
        protection_mirror_s3_endpoint_url="https://s3.example.invalid",
        protection_mirror_s3_bucket="mirror",
        protection_mirror_s3_access_key_id="mirror-key",
        protection_mirror_s3_secret_access_key="mirror-secret",
    )
    hot_store = _FakeHotStore()
    mirror_store = _FakeProtectionMirrorStore()
    collection_id = "20250712T213200Z__photos"
    files = [
        ("albums/day-01.txt", b"existing collection bytes\n"),
        ("albums/day-02.txt", b"second file bytes\n"),
        ("albums/day-03.txt", b"already valid bytes\n"),
    ]
    archive_files = []
    for relpath, content in files:
        sha256 = hashlib.sha256(content).hexdigest()
        hot_store.put_collection_file(collection_id, relpath, content)
        archive_files.append(CollectionArchiveFile(path=relpath, content=content, sha256=sha256))
    package = build_collection_archive_package(
        collection_id=collection_id,
        files=archive_files,
        stamper=FixtureProofStamper(),
    )
    mirror_store.archives[collection_id] = package.archive_bytes
    hot_store.put_paths.clear()
    hot_store.delete_collection_file(collection_id, "albums/day-01.txt")
    hot_store.put_collection_file(collection_id, "albums/day-02.txt", b"wrong bytes\n")
    hot_store.put_paths.clear()

    session_factory = make_session_factory(str(sqlite_path))
    with session_scope(session_factory) as session:
        collection = CollectionRecord(id=collection_id)
        for relpath, content in files:
            collection.files.append(
                CollectionFileRecord(
                    collection_id=collection_id,
                    path=relpath,
                    bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    hot=True,
                    archived=False,
                    hot_multipart_upload_id=(
                        "stale-upload" if relpath == "albums/day-03.txt" else None
                    ),
                )
            )
        session.add(collection)
        session.add(
            CollectionArchiveRecord(
                collection_id=collection_id,
                state="uploaded",
                object_path=f"glacier/collections/{collection_id}/archive.tar",
                stored_bytes=package.archive_size,
                sha256=package.archive_sha256,
            )
        )
        session.add(
            CollectionProtectionMirrorRecord(
                collection_id=collection_id,
                state="complete",
                object_path=mirror_store.object_path(collection_id),
                archive_bytes=package.archive_size,
                archive_sha256=package.archive_sha256,
            )
        )

    upload_service = SqlAlchemyGlacierUploadService(
        config,
        _FakeArchiveStore(),
        hot_store,
        _FakeUploadStore(),
        mirror_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert upload_service.repair_missing_hot_files_from_protection_mirror(
        limit=10,
        force=True,
    ) == 1
    for relpath, content in files:
        assert hot_store.get_collection_file(collection_id, relpath) == content
    assert hot_store.put_paths == [
        (collection_id, "albums/day-01.txt"),
        (collection_id, "albums/day-02.txt"),
    ]
    with session_scope(session_factory) as session:
        mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
        assert mirror is not None
        assert mirror.state == "complete"
        assert mirror.next_attempt_at is not None
        valid_file = session.get(CollectionFileRecord, (collection_id, "albums/day-03.txt"))
        assert valid_file is not None
        assert valid_file.hot_multipart_upload_id is None


def test_protection_mirror_backfill_failure_notifies_once_per_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

    webhook_payloads: list[dict[str, object]] = []
    monkeypatch.setattr(
        "riverhog_core.services.glacier_uploads.post_webhook",
        lambda *, config, payload: webhook_payloads.append(payload),
    )
    config = _config(
        sqlite_path,
        operator_webhook_url="http://example.invalid/webhook",
        protection_mirror_enabled=True,
        protection_mirror_s3_endpoint_url="https://s3.example.invalid",
        protection_mirror_s3_bucket="mirror",
        protection_mirror_s3_access_key_id="mirror-key",
        protection_mirror_s3_secret_access_key="mirror-secret",
    )
    hot_store = _FakeHotStore()
    upload_store = _FakeUploadStore()
    mirror_store = _FailingProtectionMirrorStore()
    collection_id = "20250712T213200Z__photos"
    relpath = "albums/day-01.txt"
    content = b"existing collection bytes\n"
    sha256 = hashlib.sha256(content).hexdigest()
    hot_store.put_collection_file(collection_id, relpath, content)
    package = build_collection_archive_package(
        collection_id=collection_id,
        files=[CollectionArchiveFile(path=relpath, content=content, sha256=sha256)],
        stamper=FixtureProofStamper(),
    )

    session_factory = make_session_factory(str(sqlite_path))
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
                object_path=f"glacier/collections/{collection_id}/archive.tar",
                stored_bytes=package.archive_size,
                sha256=package.archive_sha256,
            )
        )

    upload_service = SqlAlchemyGlacierUploadService(
        config,
        _FakeArchiveStore(),
        hot_store,
        upload_store,
        mirror_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert upload_service.process_due_uploads() == 1
    assert [payload["event"] for payload in webhook_payloads] == [
        "collections.protection_mirror_retrying"
    ]
    assert webhook_payloads[0]["collection_id"] == collection_id
    assert "mirror bucket unavailable" in str(webhook_payloads[0]["error"])
    with session_scope(session_factory) as session:
        mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
        assert mirror is not None
        mirror.next_attempt_at = "2026-01-01T00:00:00Z"

    assert upload_service.process_due_uploads() == 1
    assert [payload["event"] for payload in webhook_payloads] == [
        "collections.protection_mirror_retrying"
    ]


def test_protection_mirror_resumes_mirroring_before_new_pending_work(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

    config = _config(
        sqlite_path,
        protection_mirror_enabled=True,
        protection_mirror_s3_endpoint_url="https://s3.example.invalid",
        protection_mirror_s3_bucket="mirror",
        protection_mirror_s3_access_key_id="mirror-key",
        protection_mirror_s3_secret_access_key="mirror-secret",
    )
    hot_store = _FakeHotStore()
    mirror_store = _FakeProtectionMirrorStore()
    session_factory = make_session_factory(str(sqlite_path))

    packages: dict[str, CollectionArchivePackage] = {}
    for index, collection_id in enumerate(
        [
            "20250712T213200Z__resume-first",
            "20250712T213201Z__pending-second",
        ],
        start=1,
    ):
        relpath = "albums/day-01.txt"
        content = f"collection {index}\n".encode()
        sha256 = hashlib.sha256(content).hexdigest()
        hot_store.put_collection_file(collection_id, relpath, content)
        package = build_collection_archive_package(
            collection_id=collection_id,
            files=[CollectionArchiveFile(path=relpath, content=content, sha256=sha256)],
            stamper=FixtureProofStamper(),
        )
        packages[collection_id] = package
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
                    object_path=f"glacier/collections/{collection_id}/archive.tar",
                    stored_bytes=package.archive_size,
                    sha256=package.archive_sha256,
                )
            )

    with session_scope(session_factory) as session:
        resume_package = packages["20250712T213200Z__resume-first"]
        session.add(
            CollectionProtectionMirrorRecord(
                collection_id="20250712T213200Z__resume-first",
                state="mirroring",
                object_path=mirror_store.object_path("20250712T213200Z__resume-first"),
                archive_bytes=resume_package.archive_size,
                archive_sha256=resume_package.archive_sha256,
                next_attempt_at="2026-01-01T00:00:02Z",
            )
        )
        session.add(
            CollectionProtectionMirrorRecord(
                collection_id="20250712T213201Z__pending-second",
                state="pending",
                next_attempt_at="2026-01-01T00:00:01Z",
            )
        )

    upload_service = SqlAlchemyGlacierUploadService(
        config,
        _FakeArchiveStore(),
        hot_store,
        _FakeUploadStore(),
        mirror_store,
        proof_stamper=FixtureProofStamper(),
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert upload_service.process_due_uploads() == 1
    assert mirror_store.uploaded_collections == ["20250712T213200Z__resume-first"]
    with session_scope(session_factory) as session:
        resumed = session.get(
            CollectionProtectionMirrorRecord,
            "20250712T213200Z__resume-first",
        )
        pending = session.get(
            CollectionProtectionMirrorRecord,
            "20250712T213201Z__pending-second",
        )
        assert resumed is not None
        assert resumed.state == "complete"
        assert pending is not None
        assert pending.state == "pending"


def test_planner_hydrates_missing_hot_files_from_protection_mirror(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

    config = _config(
        sqlite_path,
        planner_image_root=tmp_path / "images",
        planner_disc_target_bytes=1_000_000,
        planner_min_fill_bytes=1,
        protection_mirror_enabled=True,
        protection_mirror_s3_endpoint_url="https://s3.example.invalid",
        protection_mirror_s3_bucket="mirror",
        protection_mirror_s3_access_key_id="mirror-key",
        protection_mirror_s3_secret_access_key="mirror-secret",
    )
    hot_store = _FakeHotStore()
    mirror_store = _FakeProtectionMirrorStore()
    collection_id = "20250712T213200Z__photos"
    relpath = "albums/day-01.txt"
    content = b"recover me from mirror\n"
    sha256 = hashlib.sha256(content).hexdigest()
    package = build_collection_archive_package(
        collection_id=collection_id,
        files=[CollectionArchiveFile(path=relpath, content=content, sha256=sha256)],
        stamper=FixtureProofStamper(),
    )
    mirror_store.archives[collection_id] = package.archive_bytes
    cache_collection_manifest_artifacts(
        config,
        collection_id=collection_id,
        manifest_bytes=package.manifest_bytes,
        proof_bytes=package.proof_bytes,
    )

    session_factory = make_session_factory(str(sqlite_path))
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
                object_path=f"glacier/collections/{collection_id}/archive.tar",
                stored_bytes=package.archive_size,
                sha256=package.archive_sha256,
            )
        )
        session.add(
            CollectionProtectionMirrorRecord(
                collection_id=collection_id,
                state="complete",
                object_path=mirror_store.object_path(collection_id),
                archive_bytes=package.archive_size,
                archive_sha256=package.archive_sha256,
            )
        )

    assert not hot_store.has_collection_file(collection_id, relpath)
    refresh_provisional_plan(
        config=config,
        hot_store=hot_store,
        protection_mirror_store=mirror_store,
        recovery_payload_codec=FixtureRecoveryPayloadCodec(),
    )

    assert hot_store.get_collection_file(collection_id, relpath) == content
    with session_scope(session_factory) as session:
        candidates = session.scalars(select(PlannedCandidateRecord)).all()
        assert len(candidates) == 1
        assert candidates[0].state == "ready"


def test_planner_worker_restores_missing_artifact_cache_from_archive_store(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

    config = _config(
        sqlite_path,
        planner_image_root=tmp_path / "images",
        planner_disc_target_bytes=1_000_000,
        planner_min_fill_bytes=1,
        protection_mirror_enabled=True,
        protection_mirror_s3_endpoint_url="https://s3.example.invalid",
        protection_mirror_s3_bucket="mirror",
        protection_mirror_s3_access_key_id="mirror-key",
        protection_mirror_s3_secret_access_key="mirror-secret",
    )
    hot_store = _FakeHotStore()
    archive_store = _ReadableArchiveStore()
    mirror_store = _FakeProtectionMirrorStore()
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
    manifest_object_path = f"glacier/collections/{collection_id}/manifest.yml"
    proof_object_path = f"glacier/collections/{collection_id}/manifest.yml.ots"
    archive_store.store_collection_artifacts(
        manifest_object_path=manifest_object_path,
        manifest_bytes=package.manifest_bytes,
        proof_object_path=proof_object_path,
        proof_bytes=package.proof_bytes,
    )

    session_factory = make_session_factory(str(sqlite_path))
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
                object_path=f"glacier/collections/{collection_id}/archive.tar",
                stored_bytes=package.archive_size,
                sha256=package.archive_sha256,
                manifest_object_path=manifest_object_path,
                manifest_sha256=package.manifest_sha256,
                ots_object_path=proof_object_path,
                ots_sha256=package.proof_sha256,
            )
        )
        session.add(
            CollectionProtectionMirrorRecord(
                collection_id=collection_id,
                state="complete",
                object_path=mirror_store.object_path(collection_id),
                archive_bytes=package.archive_size,
                archive_sha256=package.archive_sha256,
            )
        )

    planning = SqlAlchemyPlanningService(
        config,
        hot_store,
        archive_store,
        mirror_store,
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
    initialize_db(str(sqlite_path))

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
    assert upload_store.deleted_targets == []
    assert hot_store.get_collection_file(collection_id, "albums/day-01.txt") == files[0][1]
    assert archive_store.uploads == 1
    session_factory = make_session_factory(str(sqlite_path))
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


def test_packaged_archive_artifacts_are_reused_after_upload_failure(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

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

    session_factory = make_session_factory(str(sqlite_path))
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


def test_archive_failures_retry_indefinitely_with_throttled_operator_notifications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

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

    session_factory = make_session_factory(str(sqlite_path))
    with session_scope(session_factory) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        assert upload.state == "archiving"
        assert upload.archive_phase == "retry_wait"
        assert upload.archive_failure == "archive bucket unavailable"
        assert upload.archive_last_failure_notification_at == "2026-04-21T04:00:01Z"
        assert session.get(CollectionRecord, collection_id) is None


def test_successful_archive_emits_only_finalized_operator_webhook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

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
    assert webhook_payloads[0]["archive_object_path"].endswith("/archive.tar")
    assert webhook_payloads[0]["files_uploaded"] == 1


def test_finalized_collection_upload_is_idempotent_for_same_slug_and_manifest(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

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
    initialize_db(str(sqlite_path))

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
    initialize_db(str(sqlite_path))

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
    initialize_db(str(sqlite_path))

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
    initialize_db(str(sqlite_path))

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

    session_factory = make_session_factory(str(sqlite_path))
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
    initialize_db(str(sqlite_path))

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
    with session_scope(make_session_factory(str(sqlite_path))) as session:
        candidate = session.scalars(select(PlannedCandidateRecord)).one()
        assert candidate.ready_notification_sent_at is not None
        assert candidate.ready_notification_next_attempt_at is not None
        assert candidate.ready_notification_count == 1


def test_provisional_disc_plan_materialization_resumes_partial_candidate(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))
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
    session_factory = make_session_factory(str(sqlite_path))
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
    initialize_db(str(sqlite_path))
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
    session_factory = make_session_factory(str(sqlite_path))
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


def test_saturated_underfilled_candidate_is_iso_ready(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))
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
    session_factory = make_session_factory(str(sqlite_path))
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
        candidate_id = candidate.candidate_id
        assert candidate.state == "ready"
        assert candidate.bytes < candidate.min_fill_bytes
        assert candidate.iso_ready is True
        assert [(cp.collection_id, cp.path) for cp in candidate.covered_paths] == [
            (collection_id, path)
        ]
        assert Path(candidate.image_root).exists()

    finalized = planning.finalize_image(candidate_id)
    assert finalized["id"]


def test_planner_continues_after_partially_finalized_collection(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))
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
    session_factory = make_session_factory(str(sqlite_path))
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


def test_planner_ignores_fully_finalized_collection_without_mirror(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))
    config = _config(
        sqlite_path,
        protection_mirror_enabled=True,
        protection_mirror_s3_endpoint_url="https://s3.example.invalid",
        protection_mirror_s3_bucket="mirror",
        protection_mirror_s3_access_key_id="mirror-key",
        protection_mirror_s3_secret_access_key="mirror-secret",
    )
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

    session_factory = make_session_factory(str(sqlite_path))
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

    assert "protection mirror is not complete" not in caplog.text


def test_new_collection_uploads_are_blocked_over_unburned_limit(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

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


def test_existing_collection_upload_can_resume_when_over_unburned_limit(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

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
    initialize_db(str(sqlite_path))

    hot_store = _FakeHotStore()
    upload_store = _FakeUploadStore()
    service = SqlAlchemyCollectionService(_config(sqlite_path), hot_store, upload_store)

    image_root = tmp_path / "image-root"
    image_root.mkdir(parents=True, exist_ok=True)
    _seed_docs_collection_with_finalized_image(sqlite_path, image_root)

    summary = service.get("docs")

    assert summary.recovery.verified_physical.state.value == "none"
    assert summary.recovery.glacier.state.value == "none"
