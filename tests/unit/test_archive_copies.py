from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyJobRecord,
    CollectionArchiveCopyRecord,
    CollectionFileRecord,
    CollectionRecord,
)
from riverhog_core.collection_archives import (
    CollectionArchiveFile,
    CollectionArchivePackage,
    build_collection_archive_package,
)
from riverhog_core.ports.archive_store import (
    ArchiveReadStatus,
    ArchiveStore,
    ArchiveUploadReceipt,
    CollectionArchivePackageIdentity,
    CollectionArchiveUploadReceipt,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_copies import SqlAlchemyArchiveCopyService
from tests.fixtures.crypto import FixtureProofStamper, FixtureProofVerifier
from tests.unit.db_helpers import sqlite_url

_COLLECTION_ID = "2026/20260102T030405Z__docs"
_CONTENT = b"archive copy service\n"


class SourceArchiveStore:
    def __init__(self, *, ready: bool = True) -> None:
        self.package = build_collection_archive_package(
            collection_id=_COLLECTION_ID,
            files=(
                CollectionArchiveFile(
                    path="document.txt",
                    content=_CONTENT,
                    sha256=hashlib.sha256(_CONTENT).hexdigest(),
                ),
            ),
            stamper=FixtureProofStamper(),
        )
        self.ready = ready
        self.prepared = 0
        self.cleaned = 0

    def prepare_collection_archive_read(self, **_: object) -> ArchiveReadStatus:
        self.prepared += 1
        return ArchiveReadStatus(state="ready" if self.ready else "requested")

    def get_collection_archive_read_status(self, **_: object) -> ArchiveReadStatus:
        return ArchiveReadStatus(state="ready" if self.ready else "requested")

    def verify_collection_archive_package(
        self,
        *,
        collection_id: str,
        package: CollectionArchivePackageIdentity,
    ) -> None:
        assert collection_id == _COLLECTION_ID
        assert package.archive.sha256 == self.package.archive_sha256

    def iter_collection_archive(self, **_: object) -> Iterator[bytes]:
        yield from self.package.iter_archive()

    def read_collection_manifest(self, **_: object) -> bytes:
        return self.package.manifest_bytes

    def read_collection_manifest_proof(self, **_: object) -> bytes:
        return self.package.proof_bytes

    def cleanup_collection_archive_read(self, **_: object) -> None:
        self.cleaned += 1


class DestinationArchiveStore:
    def __init__(self) -> None:
        self.archive_bytes: bytes | None = None
        self.received_prefix: str | None = None
        self.verified = 0
        self.catalog_entries: list[dict[str, object]] = []

    def new_collection_archive_storage_prefix(self) -> str:
        return "archives/b2/copy-job"

    def upload_collection_archive_package(
        self,
        *,
        collection_id: str,
        package: CollectionArchivePackage,
        archive_storage_prefix: str | None = None,
        **_: object,
    ) -> CollectionArchiveUploadReceipt:
        assert collection_id == _COLLECTION_ID
        self.received_prefix = archive_storage_prefix
        self.archive_bytes = package.archive_bytes
        uploaded_at = "2026-07-15T00:00:00Z"

        def receipt(path: str, size: int) -> ArchiveUploadReceipt:
            return ArchiveUploadReceipt(
                object_path=path,
                stored_bytes=size,
                backend="b2",
                storage_class="STANDARD",
                uploaded_at=uploaded_at,
                verified_at=uploaded_at,
            )

        return CollectionArchiveUploadReceipt(
            archive=receipt("archives/b2/archive.tar.age", len(self.archive_bytes)),
            manifest=receipt("archives/b2/manifest.yml.age", len(package.manifest_bytes)),
            proof=receipt("archives/b2/manifest.yml.ots.age", len(package.proof_bytes)),
            archive_sha256=package.archive_sha256,
            manifest_sha256=package.manifest_sha256,
            proof_sha256=package.proof_sha256,
            archive_format=package.archive_format,
            compression=package.compression,
        )

    def verify_collection_archive_package(self, **_: object) -> None:
        self.verified += 1

    def publish_restore_catalog(
        self,
        *,
        entries: Sequence[dict[str, object]],
        generated_at: str,
    ) -> None:
        assert generated_at.endswith("Z")
        self.catalog_entries = list(entries)


def _seed(path: Path, source: SourceArchiveStore) -> RuntimeConfig:
    database_url = sqlite_url(path)
    initialize_db(database_url)
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        session.add(CollectionRecord(id=_COLLECTION_ID))
        session.add(
            CollectionFileRecord(
                collection_id=_COLLECTION_ID,
                path="document.txt",
                bytes=len(_CONTENT),
                sha256=hashlib.sha256(_CONTENT).hexdigest(),
                hot=False,
            )
        )
        session.add(
            CollectionArchiveCopyRecord(
                collection_id=_COLLECTION_ID,
                store="deep",
                state="uploaded",
                object_path="archives/deep/archive.tar.age",
                stored_bytes=source.package.archive_size,
                sha256=source.package.archive_sha256,
                manifest_object_path="archives/deep/manifest.yml.age",
                manifest_stored_bytes=len(source.package.manifest_bytes),
                manifest_sha256=source.package.manifest_sha256,
                ots_object_path="archives/deep/manifest.yml.ots.age",
                ots_stored_bytes=len(source.package.proof_bytes),
                ots_sha256=source.package.proof_sha256,
                archive_format=source.package.archive_format,
                compression=source.package.compression,
                last_verified_at="2026-07-15T00:00:00Z",
            )
        )
    config = RuntimeConfig(database_url=database_url)
    return replace(
        config,
        archive_stores={
            "deep": config.archive_store("deep"),
            "b2": replace(
                config.archive_store("deep"),
                name="b2",
                backend="b2",
                storage_class="STANDARD",
                read_mode="auto",
            ),
        },
    )


def _service(
    config: RuntimeConfig,
    source: SourceArchiveStore,
    destination: DestinationArchiveStore,
) -> SqlAlchemyArchiveCopyService:
    return SqlAlchemyArchiveCopyService(
        config,
        ArchiveStoreRegistry(
            {
                "deep": cast(ArchiveStore, source),
                "b2": cast(ArchiveStore, destination),
            },
            default_store="deep",
        ),
        proof_verifier=FixtureProofVerifier(),
    )


def test_archive_copy_streams_between_stores_without_materializing_hot_files(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    source = SourceArchiveStore()
    destination = DestinationArchiveStore()
    config = _seed(path, source)
    service = _service(config, source, destination)

    requested = service.create_or_resume(
        _COLLECTION_ID,
        source_store="deep",
        destination_store="b2",
    )
    processed = service.process_due(limit=1)

    assert requested["state"] == "requested"
    assert processed == 1
    assert destination.archive_bytes == source.package.archive_bytes
    assert destination.received_prefix == "archives/b2/copy-job"
    assert destination.verified == 1
    assert source.cleaned == 1
    assert [entry["collection_id"] for entry in destination.catalog_entries] == [_COLLECTION_ID]
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        copy = session.get(CollectionArchiveCopyRecord, (_COLLECTION_ID, "b2"))
        assert copy is not None
        assert copy.state == "uploaded"
        assert copy.backend == "b2"
        assert session.get(ArchiveCopyJobRecord, (_COLLECTION_ID, "b2")) is None


def test_archive_copy_waits_for_source_readiness(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    source = SourceArchiveStore(ready=False)
    destination = DestinationArchiveStore()
    config = _seed(path, source)
    service = _service(config, source, destination)

    service.create_or_resume(_COLLECTION_ID, destination_store="b2")
    assert service.process_due(limit=1) == 1

    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        job = session.get(ArchiveCopyJobRecord, (_COLLECTION_ID, "b2"))
        assert job is not None
        assert job.state == "waiting"
    assert destination.archive_bytes is None
    assert source.cleaned == 0


def test_archive_copy_requeues_interrupted_transfer_on_startup(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    source = SourceArchiveStore()
    destination = DestinationArchiveStore()
    config = _seed(path, source)
    service = _service(config, source, destination)
    service.create_or_resume(_COLLECTION_ID, destination_store="b2")
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        job = session.get(ArchiveCopyJobRecord, (_COLLECTION_ID, "b2"))
        assert job is not None
        job.state = "copying"
        job.next_attempt_at = None

    assert service.requeue_interrupted_copies_for_startup() == 1
    assert service.process_due(limit=1) == 1

    with session_scope(factory) as session:
        copy = session.get(CollectionArchiveCopyRecord, (_COLLECTION_ID, "b2"))
        assert copy is not None
        assert copy.state == "uploaded"
