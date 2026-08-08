from __future__ import annotations

import hashlib
import json
import tarfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import cast

from riverhog_age import ResumableAgeScryptSession, decrypt_age_scrypt, encrypt_age_scrypt
from riverhog_core.archive_ingress_registry import ArchiveIngressStore
from riverhog_core.archive_manifest import build_collection_archive_manifest
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveFileObjectRecord,
    CollectionArchiveObjectRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionTagRecord,
    TagRecord,
)
from riverhog_core.collection_metadata import (
    collection_content_etag,
    collection_record_manifest,
)
from riverhog_core.domain.archive import (
    ArchiveFile,
    PackVolumePlan,
    SealedPackVolume,
    StoredPartReceipt,
)
from riverhog_core.pack_volume import plan_pack_volume, render_pack_upload_unit
from riverhog_core.ports.archive_ingress_store import (
    ArchiveObjectIdentityConflict,
    CompletedObjectReceipt,
    MultipartPartReceipt,
    MultipartUpload,
)
from riverhog_core.ports.archive_manifest_store import ImmutableObjectReceipt
from riverhog_core.ports.archive_store import (
    ArchiveArtifactRead,
    ArchiveObjectIdentity,
    ArchiveObjectUploadReceipt,
    ArchiveReadStatus,
    ArchiveStore,
    CollectionArchiveIdentity,
    CollectionArchiveUploadReceipt,
    MutableManifestReceipt,
)
from riverhog_core.ports.download_allowance import DownloadAttribution
from riverhog_core.runtime_config import DEV_ARCHIVE_PASSPHRASE, RuntimeConfig
from sqlalchemy.orm import Session

from tests.unit.db_helpers import sqlite_url

COLLECTION_ID = 1
UPLOADED_AT = "2026-07-15T00:00:00.000000Z"


@dataclass(frozen=True, slots=True)
class FixtureArchive:
    collection_id: int
    files: tuple[ArchiveFile, ...]
    pack_plan: PackVolumePlan
    pack_plaintext: bytes
    manifest_bytes: bytes
    manifest_sha256: str
    proof_bytes: bytes
    proof_sha256: str
    stored_objects: dict[str, bytes]
    pack_age_state_json: str
    pack_parts_json: str
    pack_plan_sha256: str
    pack_index_sha256: str


def make_archive(
    files: dict[str, bytes],
    *,
    collection_id: int = COLLECTION_ID,
) -> FixtureArchive:
    archive_files = tuple(
        ArchiveFile(path=path, bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
        for path, content in sorted(files.items())
    )
    plan = plan_pack_volume(archive_files, sequence=0)
    plaintext = render_pack_upload_unit(plan, 0, lambda path: (files[path],))
    age_session = ResumableAgeScryptSession.create(
        DEV_ARCHIVE_PASSPHRASE,
        log_n=1,
        plaintext_size=len(plaintext),
    )
    ciphertext = age_session.encrypt_plaintext(plaintext)
    part = StoredPartReceipt(
        number=1,
        plaintext_start=0,
        plaintext_bytes=len(plaintext),
        plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
        stored_bytes=len(ciphertext),
        stored_sha256=hashlib.sha256(ciphertext).hexdigest(),
        etag="fixture-pack-etag",
    )
    state_json = (
        age_session.export_state(plaintext_size=len(plaintext)).to_json_bytes().decode("utf-8")
    )
    sealed = SealedPackVolume(
        volume_id=plan.volume_id,
        sequence=plan.sequence,
        relative_path=f"volumes/{plan.volume_id}.tar.age",
        files=len(plan.members),
        source_bytes=sum(current.bytes for current in plan.members),
        plaintext_bytes=len(plaintext),
        age_state_json=state_json,
        index_sha256=plan.index_sha256,
        plan_sha256=plan.plan_sha256,
        parts=(part,),
        version_id="fixture-pack-version",
        completed_at=UPLOADED_AT,
    )
    manifest = build_collection_archive_manifest(
        files=archive_files,
        packs=((plan, sealed),),
    )
    proof = (
        "OpenTimestamps test proof v1\n"
        f"file: manifest.json\nsha256: {hashlib.sha256(manifest).hexdigest()}\n"
    ).encode()
    manifest_ciphertext = encrypt_age_scrypt(manifest, DEV_ARCHIVE_PASSPHRASE, log_n=1)
    proof_ciphertext = encrypt_age_scrypt(proof, DEV_ARCHIVE_PASSPHRASE, log_n=1)
    parts_json = json.dumps(
        [
            {
                "number": part.number,
                "plaintext_start": part.plaintext_start,
                "plaintext_bytes": part.plaintext_bytes,
                "plaintext_sha256": part.plaintext_sha256,
                "stored_bytes": part.stored_bytes,
                "stored_sha256": part.stored_sha256,
                "etag": part.etag,
            }
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return FixtureArchive(
        collection_id=collection_id,
        files=archive_files,
        pack_plan=plan,
        pack_plaintext=plaintext,
        manifest_bytes=manifest,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        proof_bytes=proof,
        proof_sha256=hashlib.sha256(proof).hexdigest(),
        stored_objects={
            f"volumes/{plan.volume_id}.tar.age": ciphertext,
            "manifest.json.age": manifest_ciphertext,
            "manifest.json.ots.age": proof_ciphertext,
        },
        pack_age_state_json=state_json,
        pack_parts_json=parts_json,
        pack_plan_sha256=plan.plan_sha256,
        pack_index_sha256=plan.index_sha256,
    )


def archive_receipt(
    archive: FixtureArchive,
    *,
    backend: str = "s3",
    storage_class: str = "STANDARD",
    prefix: str = "archives/opaque-docs",
) -> CollectionArchiveUploadReceipt:
    rows: list[ArchiveObjectUploadReceipt] = [
        ArchiveObjectUploadReceipt(
            object_id=archive.pack_plan.volume_id,
            kind="pack",
            object_path=f"{prefix}/volumes/{archive.pack_plan.volume_id}.tar.age",
            plaintext_bytes=len(archive.pack_plaintext),
            stored_bytes=len(
                archive.stored_objects[f"volumes/{archive.pack_plan.volume_id}.tar.age"]
            ),
            sha256=None,
            stored_sha256=None,
            backend=backend,
            storage_class=storage_class,
            uploaded_at=UPLOADED_AT,
            verified_at=UPLOADED_AT,
        )
    ]
    rows.extend(
        (
            ArchiveObjectUploadReceipt(
                object_id="manifest",
                kind="manifest",
                object_path=f"{prefix}/manifest.json.age",
                plaintext_bytes=len(archive.manifest_bytes),
                stored_bytes=len(archive.stored_objects["manifest.json.age"]),
                sha256=archive.manifest_sha256,
                stored_sha256=hashlib.sha256(
                    archive.stored_objects["manifest.json.age"]
                ).hexdigest(),
                backend=backend,
                storage_class=storage_class,
                uploaded_at=UPLOADED_AT,
                verified_at=UPLOADED_AT,
            ),
            ArchiveObjectUploadReceipt(
                object_id="proof",
                kind="proof",
                object_path=f"{prefix}/manifest.json.ots.age",
                plaintext_bytes=len(archive.proof_bytes),
                stored_bytes=len(archive.stored_objects["manifest.json.ots.age"]),
                sha256=archive.proof_sha256,
                stored_sha256=hashlib.sha256(
                    archive.stored_objects["manifest.json.ots.age"]
                ).hexdigest(),
                backend=backend,
                storage_class=storage_class,
                uploaded_at=UPLOADED_AT,
                verified_at=UPLOADED_AT,
            ),
        )
    )
    return CollectionArchiveUploadReceipt(objects=tuple(rows))


def add_archive_copy(
    session: Session,
    archive: FixtureArchive,
    *,
    store: str,
    backend: str,
    storage_class: str,
) -> CollectionArchiveCopyRecord:
    copy = CollectionArchiveCopyRecord(collection_id=archive.collection_id, store=store)
    session.add(copy)
    session.flush()
    prefix = f"archives/{store}/opaque-docs"
    receipt = archive_receipt(
        archive,
        backend=backend,
        storage_class=storage_class,
        prefix=prefix,
    )
    copy.state = "uploaded"
    copy.archive_storage_prefix = prefix
    copy.backend = backend
    copy.storage_class = storage_class
    copy.last_uploaded_at = UPLOADED_AT
    copy.last_verified_at = UPLOADED_AT
    pack_receipt = receipt.require_object(archive.pack_plan.volume_id)
    pack_record = CollectionArchiveObjectRecord(
        collection_id=archive.collection_id,
        store=store,
        object_id=pack_receipt.object_id,
        object_order=0,
        kind="pack",
        object_path=pack_receipt.object_path,
        plaintext_bytes=pack_receipt.plaintext_bytes,
        stored_bytes=pack_receipt.stored_bytes,
        sha256=None,
        stored_sha256=None,
        version_id="fixture-pack-version",
        age_state_json=archive.pack_age_state_json,
        part_receipts_json=archive.pack_parts_json,
        plan_sha256=archive.pack_plan_sha256,
        index_sha256=archive.pack_index_sha256,
        backend=backend,
        storage_class=storage_class,
        uploaded_at=UPLOADED_AT,
        verified_at=UPLOADED_AT,
    )
    for file in archive.files:
        pack_record.placements.append(
            CollectionArchiveFileObjectRecord(
                collection_id=archive.collection_id,
                store=store,
                path=file.path,
                sequence=0,
                object_id=pack_record.object_id,
                file_offset=0,
                object_offset=_pack_member_offset(archive, file.path),
                bytes=file.bytes,
                member=file.path,
            )
        )
    copy.objects.append(pack_record)
    for order, object_id in enumerate(("manifest", "proof"), start=1):
        object_receipt = receipt.require_object(object_id)
        copy.objects.append(
            CollectionArchiveObjectRecord(
                collection_id=archive.collection_id,
                store=store,
                object_id=object_id,
                object_order=order,
                kind=object_id,
                object_path=object_receipt.object_path,
                plaintext_bytes=object_receipt.plaintext_bytes,
                stored_bytes=object_receipt.stored_bytes,
                sha256=object_receipt.sha256,
                stored_sha256=object_receipt.stored_sha256,
                version_id=f"fixture-{object_id}-version",
                backend=backend,
                storage_class=storage_class,
                uploaded_at=UPLOADED_AT,
                verified_at=UPLOADED_AT,
            )
        )
    return copy


def seed_archive_copy(
    path: Path,
    files: dict[str, bytes],
    *,
    store: str = "deep",
    backend: str = "s3",
    storage_class: str = "STANDARD",
    archive: FixtureArchive | None = None,
) -> tuple[RuntimeConfig, FixtureArchive]:
    database_url = sqlite_url(path)
    initialize_db(database_url)
    current = archive or make_archive(files)
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        file_rows = [(file.path, file.bytes, file.sha256) for file in current.files]
        content_etag = collection_content_etag(file_rows)
        _manifest, record_etag = collection_record_manifest(
            collection_id=current.collection_id,
            content_etag=content_etag,
            metadata_revision=1,
            tags=("docs",),
            files=file_rows,
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
        add_archive_copy(
            session,
            current,
            store=store,
            backend=backend,
            storage_class=storage_class,
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


def _pack_member_offset(archive: FixtureArchive, path: str) -> int:
    with tarfile.open(fileobj=BytesIO(archive.pack_plaintext), mode="r:") as pack:
        return pack.getmember(path).offset_data


class MemoryArchiveStore:
    def __init__(
        self,
        archive: FixtureArchive | None = None,
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
        self.objects: dict[str, bytes] = {}
        self.object_metadata: dict[str, dict[str, str]] = {}
        self._uploads: dict[str, tuple[str, dict[str, str], dict[int, bytes]]] = {}
        self._next_upload = 1

    def read_mode(self) -> str:
        return self._read_mode

    def abort_incomplete_multipart_uploads(self, **_: object) -> int:
        return 0

    def discard_collection_archive_upload(self, *, archive_storage_prefix: str) -> None:
        self.discarded_uploads.append(archive_storage_prefix)
        self.objects = {
            path: content
            for path, content in self.objects.items()
            if not path.startswith(f"{archive_storage_prefix}/")
        }
        self._uploads = {
            upload_id: current
            for upload_id, current in self._uploads.items()
            if not current[0].startswith(f"{archive_storage_prefix}/")
        }

    def new_collection_archive_storage_prefix(self) -> str:
        return f"archives/{self.backend}/new-copy"

    def create_multipart_upload(
        self,
        *,
        object_path: str,
        content_type: str,
        metadata: dict[str, str],
    ) -> MultipartUpload:
        _ = content_type
        upload_id = f"upload-{self._next_upload}"
        self._next_upload += 1
        self._uploads[upload_id] = (object_path, dict(metadata), {})
        return MultipartUpload(object_path=object_path, upload_id=upload_id)

    def upload_part(
        self,
        *,
        upload: MultipartUpload,
        number: int,
        content: bytes,
    ) -> MultipartPartReceipt:
        object_path, metadata, parts = self._uploads[upload.upload_id]
        assert object_path == upload.object_path
        parts[number] = content
        self._uploads[upload.upload_id] = (object_path, metadata, parts)
        return MultipartPartReceipt(
            number=number,
            etag=hashlib.sha256(content).hexdigest(),
            bytes=len(content),
        )

    def list_parts(self, *, upload: MultipartUpload) -> tuple[MultipartPartReceipt, ...]:
        _path, _metadata, parts = self._uploads[upload.upload_id]
        return tuple(
            MultipartPartReceipt(
                number=number,
                etag=hashlib.sha256(content).hexdigest(),
                bytes=len(content),
            )
            for number, content in sorted(parts.items())
        )

    def complete_multipart_upload(
        self,
        *,
        upload: MultipartUpload,
        parts: tuple[MultipartPartReceipt, ...],
        expected_bytes: int,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt:
        object_path, metadata, uploaded_parts = self._uploads.pop(upload.upload_id)
        assert metadata == expected_metadata
        content = b"".join(uploaded_parts[current.number] for current in parts)
        assert len(content) == expected_bytes
        self.objects[object_path] = content
        self.object_metadata[object_path] = metadata
        return self._completed_receipt(object_path, content)

    def head_completed_object(
        self,
        *,
        object_path: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None:
        content = self.objects.get(object_path)
        if content is None:
            return None
        if self.object_metadata.get(object_path) != expected_metadata:
            raise ArchiveObjectIdentityConflict(object_path)
        return self._completed_receipt(object_path, content)

    def abort_multipart_upload(self, *, upload: MultipartUpload) -> None:
        self._uploads.pop(upload.upload_id, None)

    def put_immutable_object(
        self,
        *,
        object_path: str,
        content: bytes,
        content_type: str,
        identity_metadata: dict[str, str],
    ) -> ImmutableObjectReceipt:
        _ = content_type
        existing = self.objects.get(object_path)
        if existing is not None and (
            existing != content or self.object_metadata.get(object_path) != identity_metadata
        ):
            raise ArchiveObjectIdentityConflict(object_path)
        self.objects[object_path] = content
        self.object_metadata[object_path] = dict(identity_metadata)
        return ImmutableObjectReceipt(
            object_path=object_path,
            version_id=self._version(content),
            etag=hashlib.md5(content, usedforsecurity=False).hexdigest(),
            stored_bytes=len(content),
            stored_sha256=hashlib.sha256(content).hexdigest(),
            completed_at=UPLOADED_AT,
        )

    def iter_object_range(
        self,
        *,
        object_path: str,
        version_id: str | None,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        _ = version_id
        content = self.objects.get(object_path)
        if content is None:
            if self.archive is None:
                raise KeyError(object_path)
            relative_path = (
                object_path[object_path.index("volumes/") :]
                if "volumes/" in object_path
                else object_path.rsplit("/", 1)[-1]
            )
            content = self.archive.stored_objects[relative_path]
        yield content[offset : offset + size]

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
        for current in objects:
            self.objects.pop(current.object_path, None)

    def publish_collection_metadata(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        manifest: bytes,
    ) -> MutableManifestReceipt:
        self.published_metadata.append((collection_id, archive_storage_prefix, manifest))
        object_path = f"{archive_storage_prefix}/metadata.json.age"
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
        stored = self._stored_object(object)
        content = decrypt_age_scrypt(stored, DEV_ARCHIVE_PASSPHRASE)
        receipt = ArchiveObjectUploadReceipt(
            object_id=object.object_id,
            kind=object.kind,
            object_path=object.object_path,
            plaintext_bytes=len(content),
            stored_bytes=len(stored),
            sha256=hashlib.sha256(content).hexdigest(),
            stored_sha256=hashlib.sha256(stored).hexdigest(),
            backend=self.backend,
            storage_class=self.storage_class,
            uploaded_at=UPLOADED_AT,
            verified_at=UPLOADED_AT,
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
        ciphertext = encrypt_age_scrypt(proof_bytes, DEV_ARCHIVE_PASSPHRASE, log_n=1)
        self.objects[object.object_path] = ciphertext
        self.archive = replace(
            self.archive,
            proof_bytes=proof_bytes,
            proof_sha256=hashlib.sha256(proof_bytes).hexdigest(),
            stored_objects={
                **self.archive.stored_objects,
                "manifest.json.ots.age": ciphertext,
            },
        )
        return ArchiveObjectUploadReceipt(
            object_id="proof",
            kind="proof",
            object_path=object.object_path,
            plaintext_bytes=len(proof_bytes),
            stored_bytes=len(ciphertext),
            sha256=hashlib.sha256(proof_bytes).hexdigest(),
            stored_sha256=hashlib.sha256(ciphertext).hexdigest(),
            backend=self.backend,
            storage_class=self.storage_class,
            uploaded_at=UPLOADED_AT,
            verified_at=UPLOADED_AT,
        )

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
        yield decrypt_age_scrypt(self._stored_object(object), DEV_ARCHIVE_PASSPHRASE)

    def iter_stored_archive_object(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]:
        _ = attribution
        assert collection_id == COLLECTION_ID
        self.read.append(object.object_id)
        yield self._stored_object(object)

    def cleanup_archive_objects_read(
        self,
        *,
        objects: Sequence[ArchiveObjectIdentity],
        **_: object,
    ) -> None:
        self.cleaned.append(tuple(current.object_id for current in objects))

    def _stored_object(self, object: ArchiveObjectIdentity) -> bytes:
        exact = self.objects.get(object.object_path)
        if exact is not None:
            return exact
        if self.archive is None:
            raise KeyError(object.object_path)
        relative_path = (
            object.object_path[object.object_path.index("volumes/") :]
            if "volumes/" in object.object_path
            else object.object_path.rsplit("/", 1)[-1]
        )
        return self.archive.stored_objects[relative_path]

    @staticmethod
    def _version(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()[:16]

    @classmethod
    def _completed_receipt(cls, object_path: str, content: bytes) -> CompletedObjectReceipt:
        return CompletedObjectReceipt(
            object_path=object_path,
            version_id=cls._version(content),
            etag=hashlib.md5(content, usedforsecurity=False).hexdigest(),
            bytes=len(content),
            completed_at=UPLOADED_AT,
        )


def as_archive_store(store: MemoryArchiveStore) -> ArchiveStore:
    return cast(ArchiveStore, store)


def as_ingress_store(store: MemoryArchiveStore) -> ArchiveIngressStore:
    return ArchiveIngressStore(
        multipart=store,
        root=store,
        ranges=store,
    )


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
