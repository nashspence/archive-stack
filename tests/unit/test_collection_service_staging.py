from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from riverhog_core.catalog_models import (
    CollectionFileRecord,
    CollectionRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    PlannedCandidateRecord,
)
from riverhog_core.domain.errors import Conflict
from riverhog_core.finalized_image_coverage import (
    read_finalized_image_collection_artifacts,
    read_finalized_image_coverage_parts,
)
from riverhog_core.planner.manifest import MANIFEST_FILENAME
from riverhog_core.ports.archive_store import ArchiveUploadReceipt, CollectionArchiveUploadReceipt
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collections import SqlAlchemyCollectionService
from riverhog_core.services.glacier_uploads import SqlAlchemyGlacierUploadService
from riverhog_core.sqlite_db import initialize_db, make_session_factory, session_scope
from tests.fixtures.crypto import FixtureProofStamper, FixtureRecoveryPayloadCodec
from tests.fixtures.data import DOCS_FILES


class _FakeHotStore:
    def __init__(self) -> None:
        self._files: dict[tuple[str, str], bytes] = {}

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
    upload_store = _FakeUploadStore()
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


def test_completed_collection_upload_promotes_from_staging_and_cleans_up(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(str(sqlite_path))

    hot_store = _FakeHotStore()
    upload_store = _FakeUploadStore()
    service = SqlAlchemyCollectionService(_config(sqlite_path), hot_store, upload_store)

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
    assert read_finalized_image_coverage_parts(
        image_root,
        FixtureRecoveryPayloadCodec(),
    )[0].path == relpath
    assert read_finalized_image_collection_artifacts(
        image_root,
        FixtureRecoveryPayloadCodec(),
    )[0].collection_id == collection_id


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
