from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

from riverhog_core.archive_objects import (
    CollectionArchive,
    CollectionArchiveDataObject,
    CollectionArchiveSourceFile,
    build_collection_archive,
)
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionFileRecord,
    CollectionRecord,
)
from riverhog_core.portable_catalog import portable_collection_manifest
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadTracker,
    ArchiveObjectIdentity,
    ArchiveObjectUploadReceipt,
    ArchiveReadStatus,
    CollectionArchiveIdentity,
    CollectionArchiveUploadReceipt,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_records import apply_archive_receipt
from tests.fixtures.crypto import FixtureProofStamper
from tests.unit.db_helpers import sqlite_url

COLLECTION_ID = "2026/20260102T030405Z__docs"
UPLOADED_AT = "2026-07-15T00:00:00.000000Z"


def make_archive(
    files: dict[str, bytes],
    *,
    collection_id: str = COLLECTION_ID,
) -> CollectionArchive:
    return build_collection_archive(
        collection_id=collection_id,
        files=tuple(
            CollectionArchiveSourceFile(
                path=path,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            )
            for path, content in files.items()
        ),
        max_plaintext_object_bytes=32 * 1024 * 1024,
        stamper=FixtureProofStamper(),
    )


def archive_receipt(
    archive: CollectionArchive,
    *,
    backend: str = "s3",
    storage_class: str = "STANDARD",
    prefix: str = "archives/opaque-docs",
) -> CollectionArchiveUploadReceipt:
    rows = [
        ArchiveObjectUploadReceipt(
            object_id=current.object_id,
            kind=current.kind,
            object_path=f"{prefix}/objects/{current.object_id}.age",
            plaintext_bytes=current.plaintext_bytes,
            stored_bytes=current.plaintext_bytes + 100,
            sha256=current.sha256,
            backend=backend,
            storage_class=storage_class,
            uploaded_at=UPLOADED_AT,
            verified_at=UPLOADED_AT,
        )
        for current in archive.data_objects
    ]
    rows.extend(
        (
            ArchiveObjectUploadReceipt(
                object_id="manifest",
                kind="manifest",
                object_path=f"{prefix}/manifest.yml.age",
                plaintext_bytes=len(archive.manifest_bytes),
                stored_bytes=len(archive.manifest_bytes) + 100,
                sha256=archive.manifest_sha256,
                backend=backend,
                storage_class=storage_class,
                uploaded_at=UPLOADED_AT,
                verified_at=UPLOADED_AT,
            ),
            ArchiveObjectUploadReceipt(
                object_id="proof",
                kind="proof",
                object_path=f"{prefix}/manifest.yml.ots.age",
                plaintext_bytes=len(archive.proof_bytes),
                stored_bytes=len(archive.proof_bytes) + 100,
                sha256=archive.proof_sha256,
                backend=backend,
                storage_class=storage_class,
                uploaded_at=UPLOADED_AT,
                verified_at=UPLOADED_AT,
            ),
        )
    )
    return CollectionArchiveUploadReceipt(objects=tuple(rows))


def seed_archive_copy(
    path: Path,
    files: dict[str, bytes],
    *,
    store: str = "deep",
    backend: str = "s3",
    storage_class: str = "STANDARD",
    archive: CollectionArchive | None = None,
) -> tuple[RuntimeConfig, CollectionArchive]:
    database_url = sqlite_url(path)
    initialize_db(database_url)
    current = archive or make_archive(files)
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        _manifest, manifest_etag = portable_collection_manifest(
            current.collection_id,
            ((file.path, file.bytes, file.sha256) for file in current.files),
        )
        collection = CollectionRecord(id=current.collection_id, manifest_etag=manifest_etag)
        session.add(collection)
        for file in current.files:
            session.add(
                CollectionFileRecord(
                    collection_id=current.collection_id,
                    path=file.path,
                    bytes=file.bytes,
                    sha256=file.sha256,
                )
            )
        copy = CollectionArchiveCopyRecord(collection_id=current.collection_id, store=store)
        session.add(copy)
        session.flush()
        apply_archive_receipt(
            copy,
            archive_receipt(
                current,
                backend=backend,
                storage_class=storage_class,
                prefix=f"archives/{store}/opaque-docs",
            ),
            current,
        )
    config = RuntimeConfig(database_url=database_url)
    if store == "deep":
        return config, current
    return (
        replace(
            config,
            archive_stores={
                store: replace(
                    config.archive_store("deep"),
                    name=store,
                    backend=backend,
                    storage_class=storage_class,
                )
            },
            archive_write_store=store,
            archive_read_order=(store,),
        ),
        current,
    )


class MemoryArchiveStore:
    def __init__(
        self,
        archive: CollectionArchive | None = None,
        *,
        backend: str = "s3",
        storage_class: str = "STANDARD",
        ready: bool = True,
        read_mode: str = "immediate",
    ) -> None:
        self.archive = archive
        self.backend = backend
        self.storage_class = storage_class
        self.ready = ready
        self._read_mode = read_mode
        self.prepared: list[tuple[str, ...]] = []
        self.read: list[str] = []
        self.cleaned: list[tuple[str, ...]] = []
        self.verified: list[tuple[str, ...]] = []
        self.deleted: list[tuple[str, ...]] = []
        self.catalog_entries: list[dict[str, object]] = []

    def read_mode(self) -> str:
        return self._read_mode

    def abort_incomplete_multipart_uploads(self, **_: object) -> int:
        return 0

    def new_collection_archive_storage_prefix(self) -> str:
        return f"archives/{self.backend}/new-copy"

    def max_plaintext_object_bytes(self) -> int:
        return 32 * 1024 * 1024

    def upload_collection_archive(
        self,
        *,
        collection_id: str,
        archive: CollectionArchive,
        archive_storage_prefix: str | None = None,
        multipart_tracker: ArchiveMultipartUploadTracker | None = None,
    ) -> CollectionArchiveUploadReceipt:
        _ = multipart_tracker
        assert collection_id == archive.collection_id
        materialized_objects: list[CollectionArchiveDataObject] = []
        for current in archive.data_objects:
            content = b"".join(current.iter_plaintext())
            materialized_objects.append(
                CollectionArchiveDataObject(
                    object_id=current.object_id,
                    kind=current.kind,
                    plaintext_bytes=current.plaintext_bytes,
                    sha256=current.sha256,
                    placements=current.placements,
                    _chunks=lambda content=content: iter((content,)),
                    _chunks_range=lambda offset, size, content=content: iter(
                        (content[offset : offset + size],)
                    ),
                )
            )
        self.archive = replace(archive, data_objects=tuple(materialized_objects))
        return archive_receipt(
            archive,
            backend=self.backend,
            storage_class=self.storage_class,
            prefix=archive_storage_prefix or self.new_collection_archive_storage_prefix(),
        )

    def verify_collection_archive(
        self,
        *,
        collection_id: str,
        archive: CollectionArchiveIdentity,
    ) -> None:
        assert collection_id == COLLECTION_ID
        self.verified.append(tuple(current.object_id for current in archive.objects))

    def delete_collection_archive(
        self,
        *,
        collection_id: str,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> None:
        assert collection_id == COLLECTION_ID
        self.deleted.append(tuple(current.object_id for current in objects))

    def publish_archive_catalog(
        self,
        *,
        entries: Sequence[dict[str, object]],
        generated_at: str,
    ) -> None:
        assert generated_at.endswith("Z")
        self.catalog_entries = list(entries)

    def prepare_archive_objects_read(
        self,
        *,
        objects: Sequence[ArchiveObjectIdentity],
        **_: object,
    ) -> ArchiveReadStatus:
        self.prepared.append(tuple(current.object_id for current in objects))
        return ArchiveReadStatus(state="ready" if self.ready else "requested")

    def get_archive_objects_read_status(
        self,
        *,
        objects: Sequence[ArchiveObjectIdentity],
        **_: object,
    ) -> ArchiveReadStatus:
        return ArchiveReadStatus(state="ready" if self.ready else "requested")

    def iter_archive_object(
        self,
        *,
        collection_id: str,
        object: ArchiveObjectIdentity,
    ) -> Iterator[bytes]:
        assert collection_id == COLLECTION_ID
        assert self.archive is not None
        self.read.append(object.object_id)
        if object.object_id == "manifest":
            yield self.archive.manifest_bytes
        elif object.object_id == "proof":
            yield self.archive.proof_bytes
        else:
            yield from self.archive.require_object(object.object_id).iter_plaintext()

    def iter_stored_archive_object(
        self,
        *,
        collection_id: str,
        object: ArchiveObjectIdentity,
    ) -> Iterator[bytes]:
        yield from self.iter_archive_object(collection_id=collection_id, object=object)

    def cleanup_archive_objects_read(
        self,
        *,
        objects: Sequence[ArchiveObjectIdentity],
        **_: object,
    ) -> None:
        self.cleaned.append(tuple(current.object_id for current in objects))


def as_archive_store(store: MemoryArchiveStore):
    from riverhog_core.ports.archive_store import ArchiveStore

    return cast(ArchiveStore, store)
