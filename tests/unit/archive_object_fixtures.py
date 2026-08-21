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
from riverhog_core.archive_manifest import build_collection_archive_manifest
from riverhog_core.archive_store_registry import ArchiveStoreBinding
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
    SealedProvenanceObject,
    StoredPartReceipt,
)
from riverhog_core.pack_volume import iter_render_pack_upload_unit, plan_pack_volume
from riverhog_core.ports.archive_objects import (
    ArchiveObjectIdentityConflict,
    CompletedObjectReceipt,
    ImmutableObjectReceipt,
    MultipartPartReceipt,
    MultipartUpload,
)
from riverhog_core.ports.archive_store import (
    ArchiveArtifactRead,
    ArchiveObjectIdentity,
    ArchiveObjectUploadReceipt,
    ArchiveReadStatus,
    ArchiveStore,
    CollectionArchiveIdentity,
    CollectionArchiveUploadReceipt,
    MutableManifestReceipt,
    StorageExecutionEvidence,
)
from riverhog_core.ports.download_allowance import DownloadAttribution
from riverhog_core.runtime_config import (
    DEV_ARCHIVE_PASSPHRASE,
    RuntimeConfig,
)
from riverhog_provenance import (
    FileProvenanceBinding,
    ProvenanceArchive,
    build_provenance_archive,
    create_observation_journal,
    validate_journal,
)
from sqlalchemy.orm import Session

from tests.unit.db_helpers import sqlite_url

COLLECTION_ID = 1
UPLOADED_AT = "2026-07-15T00:00:00.000000Z"
FIXTURE_PROFILE_ID = "riverhog.memory-test/v1"
FIXTURE_PROFILE_SHA256 = "1" * 64
FIXTURE_RUNTIME_SHA256 = "2" * 64


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
    provenance: ProvenanceArchive | None = None


def make_archive(
    files: dict[str, bytes],
    *,
    collection_id: int = COLLECTION_ID,
    provenance: ProvenanceArchive | None = None,
) -> FixtureArchive:
    archive_files = tuple(
        ArchiveFile(path=path, bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
        for path, content in sorted(files.items())
    )
    plan = plan_pack_volume(archive_files, sequence=0)
    plaintext = b"".join(iter_render_pack_upload_unit(plan, 0, lambda path: (files[path],)))
    age_session = ResumableAgeScryptSession.create(
        DEV_ARCHIVE_PASSPHRASE,
        log_n=1,
        plaintext_size=len(plaintext),
    )
    pack_ciphertext = age_session.encrypt_plaintext(plaintext)
    part = StoredPartReceipt(
        number=1,
        plaintext_start=0,
        plaintext_bytes=len(plaintext),
        plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
        stored_bytes=len(pack_ciphertext),
        stored_sha256=hashlib.sha256(pack_ciphertext).hexdigest(),
        part_token="fixture-pack-part",
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
        revision="fixture-pack-revision",
        completed_at=UPLOADED_AT,
    )
    sealed_provenance: list[SealedProvenanceObject] = []
    provenance_ciphertexts: dict[str, bytes] = {}
    if provenance is not None:
        for object_id, kind, relative_path, content in (
            *(
                (
                    bundle.bundle_id,
                    "provenance-bundle",
                    bundle.relative_path,
                    bundle.content,
                )
                for bundle in provenance.bundles
            ),
            (
                "provenance-index",
                "provenance-index",
                "provenance/index.json.age",
                provenance.index_bytes,
            ),
        ):
            provenance_ciphertext = encrypt_age_scrypt(content, DEV_ARCHIVE_PASSPHRASE, log_n=1)
            provenance_ciphertexts[relative_path] = provenance_ciphertext
            sealed_provenance.append(
                SealedProvenanceObject(
                    object_id=object_id,
                    kind=kind,
                    relative_path=relative_path,
                    plaintext_bytes=len(content),
                    plaintext_sha256=hashlib.sha256(content).hexdigest(),
                    stored_bytes=len(provenance_ciphertext),
                    stored_sha256=hashlib.sha256(provenance_ciphertext).hexdigest(),
                    revision=f"fixture-{object_id}-revision",
                    completed_at=UPLOADED_AT,
                )
            )
    manifest = build_collection_archive_manifest(
        files=archive_files,
        packs=((plan, sealed),),
        provenance_identity=provenance.identity if provenance is not None else None,
        provenance_objects=sealed_provenance,
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
                "part_token": part.part_token,
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
            f"volumes/{plan.volume_id}.tar.age": pack_ciphertext,
            **provenance_ciphertexts,
            "manifest.json.age": manifest_ciphertext,
            "manifest.json.ots.age": proof_ciphertext,
        },
        pack_age_state_json=state_json,
        pack_parts_json=parts_json,
        pack_plan_sha256=plan.plan_sha256,
        pack_index_sha256=plan.index_sha256,
        provenance=provenance,
    )


def make_captured_provenance_archive(
    files: dict[str, bytes],
    root: Path,
    *,
    collection_id: int = COLLECTION_ID,
) -> FixtureArchive:
    bindings: list[FileProvenanceBinding] = []
    journals: dict[str, bytes] = {}
    for relative_path, content in sorted(files.items()):
        payload = root / relative_path
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(content)
        journal = create_observation_journal(
            payload,
            relative_path=relative_path,
            host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
            agent_name="riverhog-archive-fixture",
            agent_version="1.0.0",
        )
        summary = validate_journal(journal)
        bindings.append(
            FileProvenanceBinding(
                path=relative_path,
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                status="captured",
                journal_id=summary.journal_id,
                current_state_id=summary.current_state_id,
            )
        )
        journals[summary.journal_id] = journal
    return make_archive(
        files,
        collection_id=collection_id,
        provenance=build_provenance_archive(bindings=bindings, journals=journals),
    )


def archive_receipt(
    archive: FixtureArchive,
    *,
    storage_adapter: str = "archive",
    prefix: str = "archives/opaque-docs",
) -> CollectionArchiveUploadReceipt:
    evidence = _storage_evidence(storage_adapter)
    rows: list[ArchiveObjectUploadReceipt] = [
        _upload_receipt(
            object_id=archive.pack_plan.volume_id,
            kind="pack",
            object_path=f"{prefix}/volumes/{archive.pack_plan.volume_id}.tar.age",
            plaintext_bytes=len(archive.pack_plaintext),
            stored_bytes=len(
                archive.stored_objects[f"volumes/{archive.pack_plan.volume_id}.tar.age"]
            ),
            sha256=hashlib.sha256(archive.pack_plaintext).hexdigest(),
            stored_sha256=hashlib.sha256(
                archive.stored_objects[f"volumes/{archive.pack_plan.volume_id}.tar.age"]
            ).hexdigest(),
            revision=f"fixture-{archive.pack_plan.volume_id}-revision",
            evidence=evidence,
            uploaded_at=UPLOADED_AT,
            verified_at=UPLOADED_AT,
        )
    ]
    if archive.provenance is not None:
        for object_id, kind, relative_path, plaintext in (
            *(
                (
                    bundle.bundle_id,
                    "provenance-bundle",
                    bundle.relative_path,
                    bundle.content,
                )
                for bundle in archive.provenance.bundles
            ),
            (
                "provenance-index",
                "provenance-index",
                "provenance/index.json.age",
                archive.provenance.index_bytes,
            ),
        ):
            stored = archive.stored_objects[relative_path]
            rows.append(
                _upload_receipt(
                    object_id=object_id,
                    kind=kind,
                    object_path=f"{prefix}/{relative_path}",
                    plaintext_bytes=len(plaintext),
                    stored_bytes=len(stored),
                    sha256=hashlib.sha256(plaintext).hexdigest(),
                    stored_sha256=hashlib.sha256(stored).hexdigest(),
                    revision=f"fixture-{object_id}-revision",
                    evidence=evidence,
                    uploaded_at=UPLOADED_AT,
                    verified_at=UPLOADED_AT,
                )
            )
    rows.extend(
        (
            _upload_receipt(
                object_id="manifest",
                kind="manifest",
                object_path=f"{prefix}/manifest.json.age",
                plaintext_bytes=len(archive.manifest_bytes),
                stored_bytes=len(archive.stored_objects["manifest.json.age"]),
                sha256=archive.manifest_sha256,
                stored_sha256=hashlib.sha256(
                    archive.stored_objects["manifest.json.age"]
                ).hexdigest(),
                revision="fixture-manifest-revision",
                evidence=evidence,
                uploaded_at=UPLOADED_AT,
                verified_at=UPLOADED_AT,
            ),
            _upload_receipt(
                object_id="proof",
                kind="proof",
                object_path=f"{prefix}/manifest.json.ots.age",
                plaintext_bytes=len(archive.proof_bytes),
                stored_bytes=len(archive.stored_objects["manifest.json.ots.age"]),
                sha256=archive.proof_sha256,
                stored_sha256=hashlib.sha256(
                    archive.stored_objects["manifest.json.ots.age"]
                ).hexdigest(),
                revision="fixture-proof-revision",
                evidence=evidence,
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
    storage_adapter: str = "archive",
) -> CollectionArchiveCopyRecord:
    copy = CollectionArchiveCopyRecord(collection_id=archive.collection_id, store=store)
    session.add(copy)
    session.flush()
    prefix = f"archives/{store}/opaque-docs"
    receipt = archive_receipt(
        archive,
        storage_adapter=storage_adapter,
        prefix=prefix,
    )
    evidence = _storage_evidence(storage_adapter)
    copy.state = "uploaded"
    copy.archive_storage_prefix = prefix
    for field_name, value in _evidence_fields(evidence).items():
        setattr(copy, field_name, value)
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
        sha256=pack_receipt.sha256,
        stored_sha256=pack_receipt.stored_sha256,
        revision="fixture-pack-revision",
        age_state_json=archive.pack_age_state_json,
        part_receipts_json=archive.pack_parts_json,
        plan_sha256=archive.pack_plan_sha256,
        index_sha256=archive.pack_index_sha256,
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
    provenance_artifacts = (
        [(bundle.bundle_id, "provenance-bundle") for bundle in archive.provenance.bundles]
        + [("provenance-index", "provenance-index")]
        if archive.provenance is not None
        else []
    )
    artifacts = [*provenance_artifacts, ("manifest", "manifest"), ("proof", "proof")]
    for order, (object_id, kind) in enumerate(artifacts, start=1):
        object_receipt = receipt.require_object(object_id)
        copy.objects.append(
            CollectionArchiveObjectRecord(
                collection_id=archive.collection_id,
                store=store,
                object_id=object_id,
                object_order=order,
                kind=kind,
                object_path=object_receipt.object_path,
                plaintext_bytes=object_receipt.plaintext_bytes,
                stored_bytes=object_receipt.stored_bytes,
                sha256=object_receipt.sha256,
                stored_sha256=object_receipt.stored_sha256,
                revision=f"fixture-{object_id}-revision",
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
    storage_adapter: str = "archive",
    archive: FixtureArchive | None = None,
) -> tuple[RuntimeConfig, FixtureArchive]:
    database_url = sqlite_url(path)
    initialize_db(database_url)
    current = archive or make_archive(files)
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        file_rows = [(file.path, file.bytes, file.sha256) for file in current.files]
        content_etag = collection_content_etag(file_rows)
        provenance_mode = _provenance_mode(current.provenance)
        provenance_etag = current.provenance.identity if current.provenance is not None else None
        _manifest, record_etag = collection_record_manifest(
            collection_id=current.collection_id,
            content_etag=content_etag,
            provenance_mode=provenance_mode,
            provenance_etag=provenance_etag,
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
            provenance_mode=provenance_mode,
            provenance_etag=provenance_etag,
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
            storage_adapter=storage_adapter,
        )
    config = RuntimeConfig(database_url=database_url)
    if store == "archive":
        return config, current
    configured_store = replace(
        config.archive_store("archive"),
        name=store,
        storage_adapter=storage_adapter,
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


def _provenance_mode(provenance: ProvenanceArchive | None) -> str:
    if provenance is None:
        return "omitted"
    index = json.loads(provenance.index_bytes)
    return (
        "mixed" if any(current["status"] == "omitted" for current in index["files"]) else "captured"
    )


def _archive_relative_path(object_path: str) -> str:
    for prefix in ("volumes/", "provenance/"):
        if prefix in object_path:
            return object_path[object_path.index(prefix) :]
    return object_path.rsplit("/", 1)[-1]


class MemoryArchiveStore:
    def __init__(
        self,
        archive: FixtureArchive | None = None,
        *,
        storage_adapter: str = "archive",
        ready: bool = True,
        read_mode: str = "immediate",
    ) -> None:
        self.archive = archive
        self.storage_adapter = storage_adapter
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

    def storage_execution_evidence(self) -> StorageExecutionEvidence:
        return _storage_evidence(self.storage_adapter, read_mode=self._read_mode)

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
            transfer_id: current
            for transfer_id, current in self._uploads.items()
            if not current[0].startswith(f"{archive_storage_prefix}/")
        }

    def new_collection_archive_storage_prefix(self) -> str:
        return f"archives/{self.storage_adapter}/new-copy"

    def create_multipart_upload(
        self,
        *,
        object_path: str,
        content_type: str,
        metadata: dict[str, str],
        expected_bytes: int,
    ) -> MultipartUpload:
        _ = (content_type, expected_bytes)
        transfer_id = f"upload-{self._next_upload}"
        self._next_upload += 1
        self._uploads[transfer_id] = (object_path, dict(metadata), {})
        return MultipartUpload(object_path=object_path, transfer_id=transfer_id)

    def upload_part(
        self,
        *,
        upload: MultipartUpload,
        number: int,
        content: bytes,
    ) -> MultipartPartReceipt:
        object_path, metadata, parts = self._uploads[upload.transfer_id]
        assert object_path == upload.object_path
        parts[number] = content
        self._uploads[upload.transfer_id] = (object_path, metadata, parts)
        digest = hashlib.sha256(content).hexdigest()
        return MultipartPartReceipt(
            number=number,
            part_token=digest,
            stored_bytes=len(content),
            stored_sha256=digest,
        )

    def list_parts(self, *, upload: MultipartUpload) -> tuple[MultipartPartReceipt, ...]:
        _path, _metadata, parts = self._uploads[upload.transfer_id]
        return tuple(
            MultipartPartReceipt(
                number=number,
                part_token=hashlib.sha256(content).hexdigest(),
                stored_bytes=len(content),
                stored_sha256=hashlib.sha256(content).hexdigest(),
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
        object_path, metadata, uploaded_parts = self._uploads.pop(upload.transfer_id)
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
        self._uploads.pop(upload.transfer_id, None)

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
            revision=self._version(content),
            stored_bytes=len(content),
            stored_sha256=hashlib.sha256(content).hexdigest(),
            completed_at=UPLOADED_AT,
        )

    def iter_object_range(
        self,
        *,
        object_path: str,
        revision: str,
        offset: int,
        size: int,
    ) -> Iterator[bytes]:
        _ = revision
        content = self.objects.get(object_path)
        if content is None:
            if self.archive is None:
                raise KeyError(object_path)
            relative_path = _archive_relative_path(object_path)
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
        prior_revision: str | None = None,
    ) -> MutableManifestReceipt:
        _ = prior_revision
        self.published_metadata.append((collection_id, archive_storage_prefix, manifest))
        object_path = f"{archive_storage_prefix}/metadata.json.age"
        return MutableManifestReceipt(
            object_path=object_path,
            revision=f"v{len(self.published_metadata)}",
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
        receipt = _upload_receipt(
            object_id=object.object_id,
            kind=object.kind,
            object_path=object.object_path,
            plaintext_bytes=len(content),
            stored_bytes=len(stored),
            sha256=hashlib.sha256(content).hexdigest(),
            stored_sha256=hashlib.sha256(stored).hexdigest(),
            revision=object.revision,
            evidence=self.storage_execution_evidence(),
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
        return _upload_receipt(
            object_id="proof",
            kind="proof",
            object_path=object.object_path,
            plaintext_bytes=len(proof_bytes),
            stored_bytes=len(ciphertext),
            sha256=hashlib.sha256(proof_bytes).hexdigest(),
            stored_sha256=hashlib.sha256(ciphertext).hexdigest(),
            revision=self._version(ciphertext),
            evidence=self.storage_execution_evidence(),
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
                    storage_adapter=self.storage_adapter,
                    revision=self._version(content),
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
                storage_adapter=self.storage_adapter,
                revision=object.revision,
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
            storage_adapter=self.storage_adapter,
            revision=self._version(proof_bytes),
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
        relative_path = _archive_relative_path(object.object_path)
        return self.archive.stored_objects[relative_path]

    @staticmethod
    def _version(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()[:16]

    @classmethod
    def _completed_receipt(cls, object_path: str, content: bytes) -> CompletedObjectReceipt:
        return CompletedObjectReceipt(
            object_path=object_path,
            revision=cls._version(content),
            stored_bytes=len(content),
            stored_sha256=hashlib.sha256(content).hexdigest(),
            completed_at=UPLOADED_AT,
        )


def archive_store_binding(store: MemoryArchiveStore) -> ArchiveStoreBinding:
    return ArchiveStoreBinding(
        store=cast(ArchiveStore, store),
        multipart_objects=store,
        immutable_objects=store,
        object_ranges=store,
    )


def _plaintext_receipt(
    *,
    object_id: str,
    object_path: str,
    content: bytes,
    storage_adapter: str,
    revision: str,
) -> ArchiveObjectUploadReceipt:
    digest = hashlib.sha256(content).hexdigest()
    return _upload_receipt(
        object_id=object_id,
        kind=object_id,
        object_path=object_path,
        plaintext_bytes=len(content),
        stored_bytes=len(content),
        sha256=digest,
        stored_sha256=digest,
        revision=revision,
        evidence=_storage_evidence(storage_adapter),
        uploaded_at=UPLOADED_AT,
        verified_at=UPLOADED_AT,
    )


def _storage_evidence(
    storage_adapter: str,
    *,
    read_mode: str = "immediate",
) -> StorageExecutionEvidence:
    return StorageExecutionEvidence(
        storage_adapter=storage_adapter,
        storage_profile_id=FIXTURE_PROFILE_ID,
        storage_profile_contract_sha256=FIXTURE_PROFILE_SHA256,
        egress_accounting_id="riverhog-memory-test",
        read_mode=read_mode,
        adapter_implementation_id="riverhog.memory-test-storage-adapter/v1",
        adapter_implementation_version="1.0.0",
        adapter_source_revision="fixture",
        adapter_runtime_descriptor_sha256=FIXTURE_RUNTIME_SHA256,
    )


def _upload_receipt(
    *,
    object_id: str,
    kind: str,
    object_path: str,
    plaintext_bytes: int,
    stored_bytes: int,
    sha256: str | None,
    stored_sha256: str | None,
    revision: str,
    evidence: StorageExecutionEvidence,
    uploaded_at: str,
    verified_at: str | None,
) -> ArchiveObjectUploadReceipt:
    return ArchiveObjectUploadReceipt(
        object_id=object_id,
        kind=kind,
        object_path=object_path,
        plaintext_bytes=plaintext_bytes,
        stored_bytes=stored_bytes,
        sha256=sha256,
        stored_sha256=stored_sha256,
        revision=revision,
        storage_adapter=evidence.storage_adapter,
        storage_profile_id=evidence.storage_profile_id,
        storage_profile_contract_sha256=evidence.storage_profile_contract_sha256,
        egress_accounting_id=evidence.egress_accounting_id,
        adapter_implementation_id=evidence.adapter_implementation_id,
        adapter_implementation_version=evidence.adapter_implementation_version,
        adapter_source_revision=evidence.adapter_source_revision,
        adapter_runtime_descriptor_sha256=evidence.adapter_runtime_descriptor_sha256,
        read_mode=evidence.read_mode,
        uploaded_at=uploaded_at,
        verified_at=verified_at,
    )


def _evidence_fields(evidence: StorageExecutionEvidence) -> dict[str, str]:
    return {
        "storage_adapter": evidence.storage_adapter,
        "storage_profile_id": evidence.storage_profile_id,
        "storage_profile_contract_sha256": evidence.storage_profile_contract_sha256,
        "egress_accounting_id": evidence.egress_accounting_id,
        "read_mode": evidence.read_mode,
        "adapter_implementation_id": evidence.adapter_implementation_id,
        "adapter_implementation_version": evidence.adapter_implementation_version,
        "adapter_source_revision": evidence.adapter_source_revision,
        "adapter_runtime_descriptor_sha256": evidence.adapter_runtime_descriptor_sha256,
    }
