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
    CollectionTagRecord,
    TagRecord,
)
from riverhog_core.collection_metadata import (
    collection_content_etag,
    collection_record_manifest,
)
from riverhog_core.ports.archive_store import (
    ArchiveArtifactRead,
    ArchiveMultipartUploadTracker,
    ArchiveObjectIdentity,
    ArchiveObjectUploadReceipt,
    ArchiveReadStatus,
    CollectionArchiveIdentity,
    CollectionArchiveUploadReceipt,
    MutableManifestReceipt,
)
from riverhog_core.ports.download_allowance import DownloadAttribution
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_records import apply_archive_receipt

from tests.fixtures.crypto import FixtureProofStamper
from tests.unit.db_helpers import sqlite_url

COLLECTION_ID = 1
UPLOADED_AT = "2026-07-15T00:00:00.000000Z"


def make_archive(
    files: dict[str, bytes],
    *,
    collection_id: int = COLLECTION_ID,
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
            stored_sha256=hashlib.sha256(b"".join(current.iter_plaintext())).hexdigest(),
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
                stored_sha256=hashlib.sha256(archive.manifest_bytes).hexdigest(),
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
                stored_sha256=hashlib.sha256(archive.proof_bytes).hexdigest(),
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
        files = [(file.path, file.bytes, file.sha256) for file in current.files]
        content_etag = collection_content_etag(files)
        _manifest, record_etag = collection_record_manifest(
            collection_id=current.collection_id,
            content_etag=content_etag,
            metadata_revision=1,
            tags=("docs",),
            files=files,
        )
        session.add(
            TagRecord(
                id="docs",
                created_by_app="fixture",
                created_at=UPLOADED_AT,
            )
        )
        collection = CollectionRecord(
            id=current.collection_id,
            creation_idempotency_key="fixture-docs",
            content_etag=content_etag,
            record_etag=record_etag,
            metadata_revision=1,
            metadata_updated_at=UPLOADED_AT,
            created_by_app="fixture",
            created_at=UPLOADED_AT,
        )
        session.add(collection)
        session.add(
            CollectionTagRecord(
                collection_id=current.collection_id,
                tag_id="docs",
                assigned_by_app="fixture",
                assigned_at=UPLOADED_AT,
            )
        )
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
    if store == "archive":
        return config, current
    configured_store = replace(
        config.archive_store("archive"),
        name=store,
        backend=backend,
        storage_class=storage_class,
    )
    return (
        replace(
            config,
            archive_stores={store: configured_store},
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
        self.discarded_uploads: list[str] = []
        self.published_metadata: list[tuple[int, str, bytes]] = []
        self.replaced_proofs: list[bytes] = []
        self.attestation_artifacts: dict[str, bytes] = {}
        self.multipart_tracker: ArchiveMultipartUploadTracker | None = None

    def read_mode(self) -> str:
        return self._read_mode

    def abort_incomplete_multipart_uploads(self, **_: object) -> int:
        return 0

    def discard_collection_archive_upload(self, *, archive_storage_prefix: str) -> None:
        self.discarded_uploads.append(archive_storage_prefix)

    def new_collection_archive_storage_prefix(self) -> str:
        return f"archives/{self.backend}/new-copy"

    def max_plaintext_object_bytes(self) -> int:
        return 32 * 1024 * 1024

    def upload_collection_archive(
        self,
        *,
        collection_id: int,
        archive: CollectionArchive,
        archive_storage_prefix: str | None = None,
        multipart_tracker: ArchiveMultipartUploadTracker | None = None,
    ) -> CollectionArchiveUploadReceipt:
        self.multipart_tracker = multipart_tracker
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
        collection_id: int,
        archive: CollectionArchiveIdentity,
    ) -> None:
        assert collection_id == COLLECTION_ID
        self.verified.append(tuple(current.object_id for current in archive.objects))

    def delete_collection_archive(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> None:
        assert collection_id == COLLECTION_ID
        self.deleted.append(tuple(current.object_id for current in objects))

    def publish_collection_metadata(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        manifest: bytes,
    ) -> MutableManifestReceipt:
        self.published_metadata.append((collection_id, archive_storage_prefix, manifest))
        object_path = f"{archive_storage_prefix}/metadata.yml.age"
        return MutableManifestReceipt(
            object_path=object_path,
            version_id=f"v{len(self.published_metadata)}",
            stored_bytes=len(manifest),
            stored_sha256=hashlib.sha256(manifest).hexdigest(),
            published_at=UPLOADED_AT,
        )

    def read_archive_artifact(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
    ) -> ArchiveArtifactRead:
        assert collection_id == COLLECTION_ID
        assert self.archive is not None
        prefix = object.object_path.rsplit("/", 1)[0]
        receipt = archive_receipt(
            self.archive,
            backend=self.backend,
            storage_class=self.storage_class,
            prefix=prefix,
        ).require_object(object.object_id)
        content = (
            self.archive.manifest_bytes
            if object.object_id == "manifest"
            else self.archive.proof_bytes
        )
        return ArchiveArtifactRead(receipt=receipt, content=content)

    def replace_archive_proof(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        proof_bytes: bytes,
    ) -> ArchiveObjectUploadReceipt:
        assert collection_id == COLLECTION_ID
        assert object.object_id == "proof"
        assert self.archive is not None
        self.replaced_proofs.append(proof_bytes)
        self.archive = replace(
            self.archive,
            proof_bytes=proof_bytes,
            proof_sha256=hashlib.sha256(proof_bytes).hexdigest(),
        )
        return archive_receipt(
            self.archive,
            backend=self.backend,
            storage_class=self.storage_class,
            prefix=object.object_path.rsplit("/", 1)[0],
        ).require_object("proof")

    def stored_archive_object_sha256(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
    ) -> str:
        return hashlib.sha256(
            b"".join(
                self.iter_stored_archive_object(
                    collection_id=collection_id,
                    object=object,
                )
            )
        ).hexdigest()

    def publish_archive_attestation(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        checksums: bytes,
        signature: bytes,
        proof: bytes,
    ) -> CollectionArchiveUploadReceipt:
        assert collection_id == COLLECTION_ID
        content_by_id = {
            "checksums": checksums,
            "signature": signature,
            "signature-proof": proof,
        }
        self.attestation_artifacts.update(content_by_id)
        filenames = {
            "checksums": "SHA256SUMS",
            "signature": "SHA256SUMS.minisig",
            "signature-proof": "SHA256SUMS.minisig.ots",
        }
        return CollectionArchiveUploadReceipt(
            objects=tuple(
                _plaintext_receipt(
                    object_id=object_id,
                    object_path=f"{archive_storage_prefix}/{filenames[object_id]}",
                    content=content,
                    backend=self.backend,
                )
                for object_id, content in content_by_id.items()
            )
        )

    def read_archive_attestation_artifact(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
    ) -> ArchiveArtifactRead:
        assert collection_id == COLLECTION_ID
        content = self.attestation_artifacts[object.object_id]
        return ArchiveArtifactRead(
            receipt=_plaintext_receipt(
                object_id=object.object_id,
                object_path=object.object_path,
                content=content,
                backend=self.backend,
            ),
            content=content,
        )

    def replace_archive_attestation_proof(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        proof_bytes: bytes,
    ) -> ArchiveObjectUploadReceipt:
        assert collection_id == COLLECTION_ID
        assert object.object_id == "signature-proof"
        self.attestation_artifacts[object.object_id] = proof_bytes
        return _plaintext_receipt(
            object_id=object.object_id,
            object_path=object.object_path,
            content=proof_bytes,
            backend=self.backend,
        )

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
        collection_id: int,
        object: ArchiveObjectIdentity,
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]:
        _ = attribution
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
        collection_id: int,
        object: ArchiveObjectIdentity,
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]:
        yield from self.iter_archive_object(
            collection_id=collection_id,
            object=object,
            attribution=attribution,
        )

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


def _plaintext_receipt(
    *,
    object_id: str,
    object_path: str,
    content: bytes,
    backend: str,
) -> ArchiveObjectUploadReceipt:
    digest = hashlib.sha256(content).hexdigest()
    return ArchiveObjectUploadReceipt(
        object_id=object_id,
        kind=object_id,
        object_path=object_path,
        plaintext_bytes=len(content),
        stored_bytes=len(content),
        sha256=digest,
        stored_sha256=digest,
        backend=backend,
        storage_class="STANDARD",
        uploaded_at=UPLOADED_AT,
        verified_at=UPLOADED_AT,
    )
