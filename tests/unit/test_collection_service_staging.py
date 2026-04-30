from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from pathlib import Path

from arc_core.catalog_models import (
    CollectionFileRecord,
    CollectionRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
)
from arc_core.ports.archive_store import ArchiveUploadReceipt, CollectionArchiveUploadReceipt
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.collections import SqlAlchemyCollectionService
from arc_core.services.glacier_uploads import SqlAlchemyGlacierUploadService
from arc_core.sqlite_db import initialize_db, make_session_factory, session_scope
from tests.fixtures.data import DOCS_FILES


class _FakeHotStore:
    def __init__(self) -> None:
        self._files: dict[tuple[str, str], bytes] = {}

    def put_collection_file(self, collection_id: str, path: str, content: bytes) -> None:
        self._files[(collection_id, path)] = content

    def get_collection_file(self, collection_id: str, path: str) -> bytes:
        return self._files[(collection_id, path)]

    def has_collection_file(self, collection_id: str, path: str) -> bool:
        return (collection_id, path) in self._files

    def delete_collection_file(self, collection_id: str, path: str) -> None:
        self._files.pop((collection_id, path), None)

    def list_collection_files(self, collection_id: str) -> list[tuple[str, int]]:
        return [
            (path, len(content))
            for (stored_collection_id, path), content in sorted(self._files.items())
            if stored_collection_id == collection_id
        ]


class _FakeUploadStore:
    def __init__(self) -> None:
        self._target_by_url: dict[str, str] = {}
        self._content_by_target: dict[str, bytes] = {}
        self.deleted_targets: list[str] = []

    def create_upload(self, target_path: str, length: int) -> str:
        tus_url = f"/uploads/{len(self._target_by_url) + 1}"
        self._target_by_url[tus_url] = target_path
        self._content_by_target.setdefault(target_path, b"")
        return tus_url

    def get_offset(self, tus_url: str) -> int:
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


class _FakeArchiveStore:
    def upload_collection_archive_package(self, *, collection_id, package):
        return CollectionArchiveUploadReceipt(
            archive=ArchiveUploadReceipt(
                object_path=f"glacier/collections/{collection_id}/archive.tar",
                stored_bytes=len(package.archive_bytes),
                backend="s3",
                storage_class="DEEP_ARCHIVE",
                uploaded_at="2026-04-20T04:00:00Z",
                verified_at="2026-04-20T04:00:01Z",
            ),
            manifest=ArchiveUploadReceipt(
                object_path=f"glacier/collections/{collection_id}/manifest.yml",
                stored_bytes=len(package.manifest_bytes),
                backend="s3",
                storage_class="DEEP_ARCHIVE",
                uploaded_at="2026-04-20T04:00:00Z",
                verified_at="2026-04-20T04:00:01Z",
            ),
            proof=ArchiveUploadReceipt(
                object_path=f"glacier/collections/{collection_id}/manifest.yml.ots",
                stored_bytes=len(package.proof_bytes),
                backend="s3",
                storage_class="DEEP_ARCHIVE",
                uploaded_at="2026-04-20T04:00:00Z",
                verified_at="2026-04-20T04:00:01Z",
            ),
            archive_sha256=package.archive_sha256,
            manifest_sha256=package.manifest_sha256,
            proof_sha256=package.proof_sha256,
            archive_format=package.archive_format,
            compression=package.compression,
        )


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
        sqlite_path=sqlite_path,
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
    upload_store = _FakeUploadStore()
    service = SqlAlchemyCollectionService(_config(sqlite_path), hot_store, upload_store)

    content = b"hello world\n"
    sha256 = hashlib.sha256(content).hexdigest()
    collection_id = "photos-2024"
    relpath = "albums/day-01.txt"

    service.create_or_resume_upload(
        collection_id=collection_id,
        files=[{"path": relpath, "bytes": len(content), "sha256": sha256}],
        ingest_source="/tmp/source",
    )
    session = service.create_or_resume_file_upload(collection_id, relpath)
    service.append_upload_chunk(
        collection_id,
        relpath,
        offset=int(session["offset"]),
        checksum="sha256 " + base64.b64encode(hashlib.sha256(content[:5]).digest()).decode("ascii"),
        content=content[:5],
    )

    assert not hot_store.has_collection_file(collection_id, relpath)


def test_completed_collection_upload_promotes_from_staging_and_cleans_up(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

    hot_store = _FakeHotStore()
    upload_store = _FakeUploadStore()
    service = SqlAlchemyCollectionService(_config(sqlite_path), hot_store, upload_store)

    content = b"hello world\n"
    sha256 = hashlib.sha256(content).hexdigest()
    collection_id = "photos-2024"
    relpath = "albums/day-01.txt"
    staging_target = f"/.arc/uploads/collections/{collection_id}/{relpath}"

    service.create_or_resume_upload(
        collection_id=collection_id,
        files=[{"path": relpath, "bytes": len(content), "sha256": sha256}],
        ingest_source="/tmp/source",
    )
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
    )
    assert upload_service.process_due_uploads() == 1

    assert hot_store.get_collection_file(collection_id, relpath) == content
    assert staging_target in upload_store.deleted_targets


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
