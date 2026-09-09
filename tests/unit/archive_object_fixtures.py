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
from riverhog_archive_contracts import (
    ARCHIVE_ENCRYPTION_FORMAT,
    ArchiveRootCiphertextIdentity,
    CollectionArchiveTerminalDocument,
    CollectionArchiveVolumeDocument,
    CollectionEncryptionBinding,
    RecoveryDescriptor,
    format_archive_sequence,
)
from riverhog_core.archive_manifest import (
    build_collection_archive_authority,
    build_collection_archive_terminal_document,
    collection_tree_identity,
)
from riverhog_core.archive_store_registry import ArchiveStoreBinding
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveFileObjectRecord,
    CollectionArchiveObjectRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionTagPublicationRecord,
    CollectionTagRevisionRecord,
)
from riverhog_core.collection_metadata import (
    collection_content_identity,
    collection_inventory_identity,
)
from riverhog_core.domain.archive import (
    ArchiveFile,
    PackVolumePlan,
    SealedPackVolume,
    SealedProvenanceObject,
    StoredArchivePart,
)
from riverhog_core.pack_volume import iter_render_pack_upload_unit, plan_pack_volume
from riverhog_core.ports.archive_objects import (
    ArchiveObjectIdentityConflict,
    CompletedObjectReceipt,
    ImmutableObjectReceipt,
    ResumableWriteConstraints,
    WriteSegmentReceipt,
    WriteSession,
)
from riverhog_core.ports.archive_store import (
    ArchiveArtifactRead,
    ArchiveObjectIdentity,
    ArchiveObjectUploadReceipt,
    ArchiveReadStatus,
    ArchiveStore,
    CollectionArchiveIdentity,
    CollectionDescriptionReceipt,
    CollectionTagObjectReceipt,
)
from riverhog_core.ports.download_allowance import DownloadAttribution
from riverhog_core.runtime_config import (
    DEV_ARCHIVE_PASSPHRASE,
    DEV_ARCHIVE_PASSPHRASE_ID,
    RuntimeConfig,
)
from riverhog_protocol import (
    COLLECTION_DESCRIPTION_RELATIVE_PATH,
    COLLECTION_TAG_HEAD_RELATIVE_PATH,
    CollectionTagHeadDocument,
    collection_tag_node_path,
)
from riverhog_provenance import (
    PROVENANCE_BINDING_SEGMENT_FILES_MAX,
    PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX,
    FileProvenanceBinding,
    ProvenancePayloadIdentity,
    ProvenanceRootDocument,
    ProvenanceTerminalDocument,
    ProvenanceVolumeDocument,
    binding_segment_bytes,
    create_observation_journal,
    format_provenance_sequence,
    parse_binding_segment,
    update_ordered_volume_commitment,
    validate_journal,
)
from riverhog_storage_adapter_protocol import ObjectPlacement
from sqlalchemy.orm import Session

from tests.provenance_observer import native_provenance_observer
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
    archive_root_sha256: str
    stored_objects: dict[str, bytes]
    pack_age_state_json: str
    pack_parts_json: str
    pack_plan_sha256: str
    pack_index_sha256: str
    volume_documents: tuple[CollectionArchiveVolumeDocument, ...]
    archive_terminal: CollectionArchiveTerminalDocument
    provenance: FixtureProvenance | None = None


@dataclass(frozen=True, slots=True)
class FixtureArchiveReceipt:
    objects: tuple[ArchiveObjectUploadReceipt, ...]

    def require_object(self, object_id: str) -> ArchiveObjectUploadReceipt:
        for current in self.objects:
            if current.object_id == object_id:
                return current
        raise KeyError(object_id)


@dataclass(frozen=True, slots=True)
class FixtureProvenanceVolume:
    document: ProvenanceVolumeDocument
    payload: bytes


@dataclass(frozen=True, slots=True)
class FixtureProvenance:
    root: ProvenanceRootDocument
    volumes: tuple[FixtureProvenanceVolume, ...]
    terminal: ProvenanceTerminalDocument

    @property
    def identity(self) -> str:
        return self.root.identity


def _fixture_provenance(
    *,
    archive_generation: str,
    tree_sha256: str,
    bindings: Sequence[FileProvenanceBinding],
    journals: dict[str, bytes],
) -> FixtureProvenance:
    ordered_bindings = sorted(bindings, key=lambda item: item.path.encode("utf-8"))
    volumes: list[FixtureProvenanceVolume] = []
    commitment = hashlib.sha256()

    def append(document: ProvenanceVolumeDocument, payload: bytes) -> None:
        update_ordered_volume_commitment(commitment, document)
        volumes.append(FixtureProvenanceVolume(document=document, payload=payload))

    for first in range(0, len(ordered_bindings), PROVENANCE_BINDING_SEGMENT_FILES_MAX):
        current = ordered_bindings[first : first + PROVENANCE_BINDING_SEGMENT_FILES_MAX]
        rows: list[dict[str, object]] = []
        for binding in current:
            row: dict[str, object] = {
                "path": binding.path,
                "bytes": binding.bytes,
                "sha256": binding.sha256,
                "status": binding.status,
            }
            if binding.status == "captured":
                row.update(
                    {
                        "journal_id": binding.journal_id,
                        "current_state_id": binding.current_state_id,
                    }
                )
            else:
                row["omission_reason"] = binding.omission_reason
            rows.append(row)
        payload = binding_segment_bytes(first_file_order=first, files=rows)
        sequence = len(volumes)
        append(
            ProvenanceVolumeDocument(
                archive_generation=archive_generation,
                archive_tree_sha256=tree_sha256,
                sequence=sequence,
                payload=ProvenancePayloadIdentity(
                    kind="bindings",
                    path=(
                        f"provenance/payloads/volume-{format_provenance_sequence(sequence)}.bin.age"
                    ),
                    bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                ),
                first_file_order=first,
                file_count=len(current),
            ),
            payload,
        )

    for journal_id, content in sorted(journals.items()):
        summary = validate_journal(content)
        if summary.journal_id != journal_id:
            raise AssertionError("fixture provenance journal identity differs")
        for offset in range(0, len(content), PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX):
            payload = content[offset : offset + PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX]
            sequence = len(volumes)
            append(
                ProvenanceVolumeDocument(
                    archive_generation=archive_generation,
                    archive_tree_sha256=tree_sha256,
                    sequence=sequence,
                    payload=ProvenancePayloadIdentity(
                        kind="journal",
                        path=(
                            "provenance/payloads/volume-"
                            f"{format_provenance_sequence(sequence)}.bin.age"
                        ),
                        bytes=len(payload),
                        sha256=hashlib.sha256(payload).hexdigest(),
                    ),
                    journal_id=journal_id,
                    journal_offset=offset,
                    journal_bytes=len(content),
                    journal_sha256=hashlib.sha256(content).hexdigest(),
                ),
                payload,
            )
    terminal = ProvenanceTerminalDocument(
        archive_generation=archive_generation,
        archive_tree_sha256=tree_sha256,
        sequence=len(volumes),
    )
    update_ordered_volume_commitment(commitment, terminal)
    root = ProvenanceRootDocument(
        archive_generation=archive_generation,
        archive_tree_sha256=tree_sha256,
        ordered_volume_sha256=commitment.hexdigest(),
    )
    return FixtureProvenance(root=root, volumes=tuple(volumes), terminal=terminal)


def make_archive(
    files: dict[str, bytes],
    *,
    collection_id: int = COLLECTION_ID,
    provenance_bindings: Sequence[FileProvenanceBinding] = (),
    provenance_journals: dict[str, bytes] | None = None,
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
    part = StoredArchivePart(
        number=1,
        plaintext_start=0,
        plaintext_bytes=len(plaintext),
        plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
        stored_bytes=len(pack_ciphertext),
        stored_sha256=hashlib.sha256(pack_ciphertext).hexdigest(),
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
        revision="fixture-pack-version",
        completed_at=UPLOADED_AT,
    )
    archive_generation = "a" * 64
    tree_sha256 = str(collection_tree_identity(archive_files)["sha256"])
    provenance = (
        _fixture_provenance(
            archive_generation=archive_generation,
            tree_sha256=tree_sha256,
            bindings=provenance_bindings,
            journals=provenance_journals or {},
        )
        if provenance_bindings or provenance_journals
        else None
    )
    sealed_provenance: list[SealedProvenanceObject] = []
    provenance_ciphertexts: dict[str, bytes] = {}
    if provenance is not None:
        artifacts = [
            *(
                (
                    f"provenance-payload-{format_provenance_sequence(volume.document.sequence)}",
                    (
                        "provenance-bindings"
                        if volume.document.payload.kind == "bindings"
                        else "provenance-journal-segment"
                    ),
                    volume.document.payload.path,
                    volume.payload,
                )
                for volume in provenance.volumes
            ),
            *(
                (
                    f"provenance-volume-{format_provenance_sequence(volume.document.sequence)}",
                    "provenance-volume-metadata",
                    volume.document.metadata_path,
                    volume.document.to_json_bytes(),
                )
                for volume in provenance.volumes
            ),
            (
                f"provenance-terminal-{format_provenance_sequence(provenance.terminal.sequence)}",
                "provenance-terminal",
                provenance.terminal.metadata_path,
                provenance.terminal.to_json_bytes(),
            ),
            (
                "provenance-root",
                "provenance-root",
                "provenance/root.json.age",
                provenance.root.to_json_bytes(),
            ),
        ]
        for object_id, kind, relative_path, content in artifacts:
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
                    revision=f"fixture-{object_id}-version",
                    completed_at=UPLOADED_AT,
                )
            )
    manifest, volume_documents = build_collection_archive_authority(
        archive_generation=archive_generation,
        files=archive_files,
        packs=((plan, sealed),),
        provenance_identity=provenance.identity if provenance is not None else None,
        provenance_objects=[item for item in sealed_provenance if item.kind == "provenance-root"],
    )
    main_metadata_ciphertexts: dict[str, bytes] = {}
    for document in volume_documents:
        relative_path = (
            f"metadata/volume-{format_archive_sequence(document.volume.sequence)}.json.age"
        )
        main_metadata_ciphertexts[relative_path] = encrypt_age_scrypt(
            document.to_json_bytes(), DEV_ARCHIVE_PASSPHRASE, log_n=1
        )
    archive_terminal = build_collection_archive_terminal_document(
        archive_generation=archive_generation,
        tree_sha256=tree_sha256,
        sequence=len(volume_documents),
    )
    terminal_path = f"metadata/volume-{format_archive_sequence(archive_terminal.sequence)}.json.age"
    main_metadata_ciphertexts[terminal_path] = encrypt_age_scrypt(
        archive_terminal.to_json_bytes(), DEV_ARCHIVE_PASSPHRASE, log_n=1
    )
    manifest_ciphertext = encrypt_age_scrypt(manifest, DEV_ARCHIVE_PASSPHRASE, log_n=1)
    recovery_descriptor = RecoveryDescriptor(
        encryption=CollectionEncryptionBinding(
            format=ARCHIVE_ENCRYPTION_FORMAT,
            passphrase_id=DEV_ARCHIVE_PASSPHRASE_ID,
        ),
        root=ArchiveRootCiphertextIdentity(
            path="manifest.json.age",
            stored_bytes=len(manifest_ciphertext),
            stored_sha256=hashlib.sha256(manifest_ciphertext).hexdigest(),
        ),
    ).to_json_bytes()
    parts_json = json.dumps(
        [
            {
                "number": part.number,
                "plaintext_start": part.plaintext_start,
                "plaintext_bytes": part.plaintext_bytes,
                "plaintext_sha256": part.plaintext_sha256,
                "stored_bytes": part.stored_bytes,
                "stored_sha256": part.stored_sha256,
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
        archive_root_sha256=hashlib.sha256(manifest).hexdigest(),
        stored_objects={
            f"volumes/{plan.volume_id}.tar.age": pack_ciphertext,
            **main_metadata_ciphertexts,
            **provenance_ciphertexts,
            "manifest.json.age": manifest_ciphertext,
            "recovery.json": recovery_descriptor,
        },
        pack_age_state_json=state_json,
        pack_parts_json=parts_json,
        pack_plan_sha256=plan.plan_sha256,
        pack_index_sha256=plan.index_sha256,
        volume_documents=volume_documents,
        archive_terminal=archive_terminal,
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
            observer=native_provenance_observer(),
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
        provenance_bindings=bindings,
        provenance_journals=journals,
    )


def archive_receipt(
    archive: FixtureArchive,
    *,
    prefix: str = "archives/opaque-docs",
) -> FixtureArchiveReceipt:
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
            revision=f"fixture-{archive.pack_plan.volume_id}-version",
            uploaded_at=UPLOADED_AT,
            verified_at=UPLOADED_AT,
        )
    ]
    for document in archive.volume_documents:
        sequence = document.volume.sequence
        sequence_token = format_archive_sequence(sequence)
        object_id = f"volume-metadata-{sequence_token}"
        relative_path = f"metadata/volume-{sequence_token}.json.age"
        plaintext = document.to_json_bytes()
        stored = archive.stored_objects[relative_path]
        rows.append(
            ArchiveObjectUploadReceipt(
                object_id=object_id,
                kind="volume-metadata",
                object_path=f"{prefix}/{relative_path}",
                plaintext_bytes=len(plaintext),
                stored_bytes=len(stored),
                sha256=hashlib.sha256(plaintext).hexdigest(),
                stored_sha256=hashlib.sha256(stored).hexdigest(),
                revision=f"fixture-{object_id}-version",
                uploaded_at=UPLOADED_AT,
                verified_at=UPLOADED_AT,
            )
        )
    terminal = archive.archive_terminal
    terminal_sequence = format_archive_sequence(terminal.sequence)
    object_id = f"volume-terminal-{terminal_sequence}"
    relative_path = f"metadata/volume-{terminal_sequence}.json.age"
    plaintext = terminal.to_json_bytes()
    stored = archive.stored_objects[relative_path]
    rows.append(
        ArchiveObjectUploadReceipt(
            object_id=object_id,
            kind="volume-terminal",
            object_path=f"{prefix}/{relative_path}",
            plaintext_bytes=len(plaintext),
            stored_bytes=len(stored),
            sha256=hashlib.sha256(plaintext).hexdigest(),
            stored_sha256=hashlib.sha256(stored).hexdigest(),
            revision=f"fixture-{object_id}-version",
            uploaded_at=UPLOADED_AT,
            verified_at=UPLOADED_AT,
        )
    )
    if archive.provenance is not None:
        for object_id, kind, relative_path, plaintext in _fixture_provenance_artifacts(
            archive.provenance
        ):
            stored = archive.stored_objects[relative_path]
            rows.append(
                ArchiveObjectUploadReceipt(
                    object_id=object_id,
                    kind=kind,
                    object_path=f"{prefix}/{relative_path}",
                    plaintext_bytes=len(plaintext),
                    stored_bytes=len(stored),
                    sha256=hashlib.sha256(plaintext).hexdigest(),
                    stored_sha256=hashlib.sha256(stored).hexdigest(),
                    revision=f"fixture-{object_id}-version",
                    uploaded_at=UPLOADED_AT,
                    verified_at=UPLOADED_AT,
                )
            )
    rows.extend(
        (
            ArchiveObjectUploadReceipt(
                object_id="manifest",
                kind="manifest",
                object_path=f"{prefix}/manifest.json.age",
                plaintext_bytes=len(archive.manifest_bytes),
                stored_bytes=len(archive.stored_objects["manifest.json.age"]),
                sha256=archive.archive_root_sha256,
                stored_sha256=hashlib.sha256(
                    archive.stored_objects["manifest.json.age"]
                ).hexdigest(),
                revision="fixture-manifest-version",
                uploaded_at=UPLOADED_AT,
                verified_at=UPLOADED_AT,
            ),
            ArchiveObjectUploadReceipt(
                object_id="recovery-descriptor",
                kind="recovery-descriptor",
                object_path=f"{prefix}/recovery.json",
                plaintext_bytes=len(archive.stored_objects["recovery.json"]),
                stored_bytes=len(archive.stored_objects["recovery.json"]),
                sha256=hashlib.sha256(archive.stored_objects["recovery.json"]).hexdigest(),
                stored_sha256=hashlib.sha256(archive.stored_objects["recovery.json"]).hexdigest(),
                revision="fixture-recovery-descriptor-version",
                uploaded_at=UPLOADED_AT,
                verified_at=UPLOADED_AT,
            ),
        )
    )
    return FixtureArchiveReceipt(objects=tuple(rows))


def add_archive_copy(
    session: Session,
    archive: FixtureArchive,
    *,
    store: str,
) -> CollectionArchiveCopyRecord:
    copy = CollectionArchiveCopyRecord(collection_id=archive.collection_id, store=store)
    session.add(copy)
    session.flush()
    prefix = f"archives/{store}/opaque-docs"
    receipt = archive_receipt(archive, prefix=prefix)
    copy.state = "uploaded"
    copy.archive_storage_prefix = prefix
    copy.last_uploaded_at = UPLOADED_AT
    copy.last_verified_at = UPLOADED_AT
    collection = session.get(CollectionRecord, archive.collection_id)
    assert collection is not None
    session.add(
        CollectionTagPublicationRecord(
            collection_id=archive.collection_id,
            store=store,
            desired_revision=collection.tag_revision,
            desired_tag_set_identity=collection.tag_set_identity,
            desired_head_identity=collection.tag_head_identity,
            published_revision=collection.tag_revision,
            published_tag_set_identity=collection.tag_set_identity,
            published_head_identity=collection.tag_head_identity,
            state="published",
            next_attempt_at=None,
            head_object_path=f"{prefix}/{COLLECTION_TAG_HEAD_RELATIVE_PATH}",
            head_provider_revision="fixture-tag-head-revision",
            head_stored_bytes=128,
            head_stored_sha256="f" * 64,
            published_at=UPLOADED_AT,
        )
    )
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
        revision="fixture-pack-version",
        age_state_json=archive.pack_age_state_json,
        archive_parts_json=archive.pack_parts_json,
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
        [
            (object_id, kind)
            for object_id, kind, _relative_path, _content in _fixture_provenance_artifacts(
                archive.provenance
            )
        ]
        if archive.provenance is not None
        else []
    )
    artifacts = [
        *(
            (
                f"volume-metadata-{format_archive_sequence(document.volume.sequence)}",
                "volume-metadata",
            )
            for document in archive.volume_documents
        ),
        (
            f"volume-terminal-{format_archive_sequence(archive.archive_terminal.sequence)}",
            "volume-terminal",
        ),
        *provenance_artifacts,
        ("manifest", "manifest"),
        ("recovery-descriptor", "recovery-descriptor"),
    ]
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
                revision=f"fixture-{object_id}-version",
                uploaded_at=UPLOADED_AT,
                verified_at=UPLOADED_AT,
            )
        )
    return copy


def _fixture_provenance_artifacts(
    provenance: FixtureProvenance,
) -> list[tuple[str, str, str, bytes]]:
    return [
        *(
            (
                f"provenance-payload-{format_provenance_sequence(volume.document.sequence)}",
                (
                    "provenance-bindings"
                    if volume.document.payload.kind == "bindings"
                    else "provenance-journal-segment"
                ),
                volume.document.payload.path,
                volume.payload,
            )
            for volume in provenance.volumes
        ),
        *(
            (
                f"provenance-volume-{format_provenance_sequence(volume.document.sequence)}",
                "provenance-volume-metadata",
                volume.document.metadata_path,
                volume.document.to_json_bytes(),
            )
            for volume in provenance.volumes
        ),
        (
            f"provenance-terminal-{format_provenance_sequence(provenance.terminal.sequence)}",
            "provenance-terminal",
            provenance.terminal.metadata_path,
            provenance.terminal.to_json_bytes(),
        ),
        (
            "provenance-root",
            "provenance-root",
            "provenance/root.json.age",
            provenance.root.to_json_bytes(),
        ),
    ]


def seed_archive_copy(
    path: Path | None,
    files: dict[str, bytes],
    *,
    store: str = "deep",
    archive: FixtureArchive | None = None,
    database_url: str | None = None,
) -> tuple[RuntimeConfig, FixtureArchive]:
    if database_url is None:
        if path is None:
            raise ValueError("path or database_url is required")
        database_url = sqlite_url(path)
    initialize_db(database_url)
    current = archive or make_archive(files)
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        file_rows = [(file.path, file.bytes, file.sha256) for file in current.files]
        content_identity = collection_content_identity(file_rows)
        provenance_mode = _provenance_mode(current.provenance)
        provenance_identity = (
            current.provenance.identity if current.provenance is not None else None
        )
        _header, inventory_identity = collection_inventory_identity(
            collection_id=current.collection_id,
            content_identity=content_identity,
            encryption_format=ARCHIVE_ENCRYPTION_FORMAT,
            passphrase_id=DEV_ARCHIVE_PASSPHRASE_ID,
            provenance_mode=provenance_mode,
            provenance_identity=provenance_identity,
            files=file_rows,
        )
        collection = CollectionRecord(
            id=current.collection_id,
            creation_idempotency_key="fixture-docs",
            creation_identity_sha256=f"{current.collection_id:064x}",
            creation_custody_mode="producer-retained",
            archive_generation=json.loads(current.manifest_bytes)["archive_generation"],
            content_identity=content_identity,
            encryption_format=ARCHIVE_ENCRYPTION_FORMAT,
            passphrase_id=DEV_ARCHIVE_PASSPHRASE_ID,
            provenance_mode=provenance_mode,
            provenance_identity=provenance_identity,
            inventory_identity=inventory_identity,
            archive_root_sha256=current.archive_root_sha256,
            created_by_app="fixture",
            created_at=UPLOADED_AT,
            file_count=len(file_rows),
            file_bytes=sum(item[1] for item in file_rows),
        )
        empty_tag_head = CollectionTagHeadDocument.seal(
            archive_root_sha256=current.archive_root_sha256,
            revision=1,
            root_sha256=None,
        )
        collection.tag_revision = empty_tag_head.revision
        collection.tag_root_sha256 = empty_tag_head.root_sha256
        collection.tag_set_identity = empty_tag_head.tag_set_identity
        collection.tag_head_identity = empty_tag_head.head_identity
        session.add(collection)
        session.add(
            CollectionTagRevisionRecord(
                collection_id=current.collection_id,
                revision=empty_tag_head.revision,
                root_sha256=empty_tag_head.root_sha256,
                tag_set_identity=empty_tag_head.tag_set_identity,
                head_identity=empty_tag_head.head_identity,
                created_at=UPLOADED_AT,
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
        )
    config = RuntimeConfig(database_url=database_url)
    if store == "archive":
        return config, current
    configured_store = replace(
        config.archive_store("archive"),
        name=store,
        base_url=f"http://127.0.0.1/{store}",
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


def _provenance_mode(provenance: FixtureProvenance | None) -> str:
    if provenance is None:
        return "omitted"
    for volume in provenance.volumes:
        if volume.document.payload.kind != "bindings":
            continue
        _first, bindings = parse_binding_segment(volume.payload)
        if any(current.get("status") == "omitted" for current in bindings):
            return "mixed"
    return "captured"


def _archive_relative_path(object_path: str) -> str:
    for prefix in ("provenance/", "volumes/", "metadata/"):
        if prefix in object_path:
            return object_path[object_path.index(prefix) :]
    return object_path.rsplit("/", 1)[-1]


class MemoryArchiveStore:
    def __init__(
        self,
        archive: FixtureArchive | None = None,
        *,
        new_archive_prefix: str = "archives/memory/new-copy",
        ready: bool = True,
        read_mode: str = "immediate",
    ) -> None:
        self.archive = archive
        self.new_archive_prefix = new_archive_prefix
        self.ready = ready
        self._read_mode = read_mode
        self.prepared: list[tuple[str, ...]] = []
        self.read: list[str] = []
        self.cleaned: list[tuple[str, ...]] = []
        self.verified: list[tuple[str, ...]] = []
        self.deleted: list[tuple[str, ...]] = []
        self.discarded_uploads: list[str] = []
        self.objects: dict[str, bytes] = {}
        self.object_metadata: dict[str, dict[str, str]] = {}
        self.object_content_types: dict[str, str] = {}
        self._writes: dict[str, tuple[str, str, dict[str, str], dict[int, bytes]]] = {}
        self._next_write = 1

    def read_mode(self) -> str:
        return self._read_mode

    def discard_collection_archive_upload(self, *, archive_storage_prefix: str) -> None:
        self.discarded_uploads.append(archive_storage_prefix)
        self.objects = {
            path: content
            for path, content in self.objects.items()
            if not path.startswith(f"{archive_storage_prefix}/")
        }

    def new_collection_archive_storage_prefix(self) -> str:
        return self.new_archive_prefix

    def write_constraints(self) -> ResumableWriteConstraints:
        return ResumableWriteConstraints(
            minimum_nonfinal_segment_bytes=1,
            maximum_segment_bytes=None,
            maximum_segment_count=None,
        )

    def begin_write(
        self,
        *,
        object_path: str,
        expected_bytes: int,
        content_type: str,
        metadata: dict[str, str],
    ) -> WriteSession:
        _ = content_type
        write_token = f"write-{self._next_write}"
        self._next_write += 1
        self._writes[write_token] = (object_path, content_type, dict(metadata), {})
        return WriteSession(
            object_path=object_path,
            write_token=write_token,
            expected_bytes=expected_bytes,
        )

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        content: bytes,
    ) -> WriteSegmentReceipt:
        object_path, content_type, metadata, segments = self._writes[session.write_token]
        assert object_path == session.object_path
        segments[number] = content
        self._writes[session.write_token] = (object_path, content_type, metadata, segments)
        return WriteSegmentReceipt(
            number=number,
            segment_token=hashlib.sha256(content).hexdigest(),
            bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def list_segments(self, *, session: WriteSession) -> tuple[WriteSegmentReceipt, ...]:
        _path, _content_type, _metadata, segments = self._writes[session.write_token]
        return tuple(
            WriteSegmentReceipt(
                number=number,
                segment_token=hashlib.sha256(content).hexdigest(),
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
            for number, content in sorted(segments.items())
        )

    def complete_write(
        self,
        *,
        session: WriteSession,
        segments: tuple[WriteSegmentReceipt, ...],
        expected_bytes: int,
        expected_content_type: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt:
        object_path, content_type, metadata, written_segments = self._writes.pop(
            session.write_token
        )
        assert content_type == expected_content_type
        assert metadata == expected_metadata
        content = b"".join(written_segments[current.number] for current in segments)
        assert len(content) == expected_bytes
        self.objects[object_path] = content
        self.object_content_types[object_path] = content_type
        self.object_metadata[object_path] = metadata
        return self._completed_receipt(object_path, content)

    def find_completed_write(
        self,
        *,
        object_path: str,
        expected_bytes: int,
        expected_content_type: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None:
        content = self.objects.get(object_path)
        if content is None:
            return None
        if len(content) != expected_bytes:
            raise ArchiveObjectIdentityConflict(object_path)
        if self.object_metadata.get(object_path) != expected_metadata:
            raise ArchiveObjectIdentityConflict(object_path)
        if self.object_content_types.get(object_path) != expected_content_type:
            raise ArchiveObjectIdentityConflict(object_path)
        return self._completed_receipt(object_path, content)

    def abort_write(self, *, session: WriteSession) -> None:
        self._writes.pop(session.write_token, None)

    def put_immutable_object(
        self,
        *,
        object_path: str,
        content: bytes,
        content_type: str,
        required_identity_assertions: dict[str, str],
        placement: ObjectPlacement,
    ) -> ImmutableObjectReceipt:
        _ = content_type
        assert placement == "immediate"
        existing = self.objects.get(object_path)
        if existing is not None:
            if self.object_metadata.get(object_path) != required_identity_assertions:
                raise ArchiveObjectIdentityConflict(object_path)
            return self._immutable_receipt(object_path, existing)
        self.objects[object_path] = content
        self.object_content_types[object_path] = content_type
        self.object_metadata[object_path] = dict(required_identity_assertions)
        return ImmutableObjectReceipt(
            object_path=object_path,
            revision=self._version(content),
            entity_token=hashlib.md5(content, usedforsecurity=False).hexdigest(),
            stored_bytes=len(content),
            stored_sha256=hashlib.sha256(content).hexdigest(),
            completed_at=UPLOADED_AT,
        )

    def _immutable_receipt(self, object_path: str, content: bytes) -> ImmutableObjectReceipt:
        return ImmutableObjectReceipt(
            object_path=object_path,
            revision=self._version(content),
            entity_token=hashlib.md5(content, usedforsecurity=False).hexdigest(),
            stored_bytes=len(content),
            stored_sha256=hashlib.sha256(content).hexdigest(),
            completed_at=UPLOADED_AT,
        )

    def iter_object_range(
        self,
        *,
        object_path: str,
        revision: str | None,
        expected_bytes: int,
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
        assert len(content) == expected_bytes
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

    def publish_collection_description(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        document: bytes,
        passphrase_id: str,
        expected_current_stored_sha256: str | None = None,
    ) -> CollectionDescriptionReceipt:
        assert collection_id == COLLECTION_ID
        assert passphrase_id == DEV_ARCHIVE_PASSPHRASE_ID
        object_path = f"{archive_storage_prefix}/{COLLECTION_DESCRIPTION_RELATIVE_PATH}"
        current = self.objects.get(object_path)
        plaintext_sha256 = hashlib.sha256(document).hexdigest()
        if current is not None and self.object_metadata.get(object_path) == {
            "riverhog-plaintext-sha256": plaintext_sha256
        }:
            return CollectionDescriptionReceipt(
                object_path=object_path,
                revision=self._version(current),
                stored_bytes=len(current),
                stored_sha256=hashlib.sha256(current).hexdigest(),
                published_at=UPLOADED_AT,
            )
        if current is not None and expected_current_stored_sha256 is None:
            raise RuntimeError("collection description already exists with another authority")
        if expected_current_stored_sha256 is not None and (
            current is None or hashlib.sha256(current).hexdigest() != expected_current_stored_sha256
        ):
            raise RuntimeError("collection description replacement fence differs")
        ciphertext = encrypt_age_scrypt(document, DEV_ARCHIVE_PASSPHRASE, log_n=1)
        self.objects[object_path] = ciphertext
        self.object_metadata[object_path] = {"riverhog-plaintext-sha256": plaintext_sha256}
        return CollectionDescriptionReceipt(
            object_path=object_path,
            revision=self._version(ciphertext),
            stored_bytes=len(ciphertext),
            stored_sha256=hashlib.sha256(ciphertext).hexdigest(),
            published_at=UPLOADED_AT,
        )

    def delete_collection_description(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
    ) -> None:
        assert collection_id == COLLECTION_ID
        object_path = f"{archive_storage_prefix}/{COLLECTION_DESCRIPTION_RELATIVE_PATH}"
        self.objects.pop(object_path, None)
        self.object_metadata.pop(object_path, None)

    def publish_collection_tag_node(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        digest: str,
        encoded: bytes,
        passphrase_id: str,
    ) -> CollectionTagObjectReceipt:
        object_path = f"{archive_storage_prefix}/{collection_tag_node_path(digest)}"
        return self._publish_collection_tag_object(object_path, encoded)

    def publish_collection_tag_head(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        document: bytes,
        passphrase_id: str,
        expected_current_stored_sha256: str | None = None,
    ) -> CollectionTagObjectReceipt:
        object_path = f"{archive_storage_prefix}/{COLLECTION_TAG_HEAD_RELATIVE_PATH}"
        current = self.objects.get(object_path)
        if current is not None and self.object_metadata.get(object_path) == {
            "riverhog-plaintext-sha256": hashlib.sha256(document).hexdigest()
        }:
            return CollectionTagObjectReceipt(
                object_path=object_path,
                revision=self._version(current),
                stored_bytes=len(current),
                stored_sha256=hashlib.sha256(current).hexdigest(),
                published_at=UPLOADED_AT,
            )
        if current is not None and expected_current_stored_sha256 is None:
            raise RuntimeError("collection tag head already exists with another authority")
        if expected_current_stored_sha256 is not None and (
            current is None or hashlib.sha256(current).hexdigest() != expected_current_stored_sha256
        ):
            raise RuntimeError("collection tag-head replacement fence differs")
        return self._publish_collection_tag_object(object_path, document)

    def _publish_collection_tag_object(
        self,
        object_path: str,
        plaintext: bytes,
    ) -> CollectionTagObjectReceipt:
        ciphertext = encrypt_age_scrypt(plaintext, DEV_ARCHIVE_PASSPHRASE, log_n=1)
        self.objects[object_path] = ciphertext
        self.object_metadata[object_path] = {
            "riverhog-plaintext-sha256": hashlib.sha256(plaintext).hexdigest()
        }
        return CollectionTagObjectReceipt(
            object_path=object_path,
            revision=self._version(ciphertext),
            stored_bytes=len(ciphertext),
            stored_sha256=hashlib.sha256(ciphertext).hexdigest(),
            published_at=UPLOADED_AT,
        )

    def delete_collection_tags(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
    ) -> None:
        prefix = f"{archive_storage_prefix}/tags/"
        for object_path in tuple(self.objects):
            if object_path.startswith(prefix):
                self.objects.pop(object_path, None)
                self.object_metadata.pop(object_path, None)

    def delete_collection_tag_node(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        digest: str,
        expected_current_stored_sha256: str,
        provider_revision: str | None,
    ) -> None:
        _ = collection_id
        object_path = f"{archive_storage_prefix}/{collection_tag_node_path(digest)}"
        current = self.objects.get(object_path)
        if (
            current is not None
            and hashlib.sha256(current).hexdigest() != expected_current_stored_sha256
        ):
            raise RuntimeError("collection tag-node deletion fence differs")
        if current is not None and provider_revision not in {None, self._version(current)}:
            raise RuntimeError("collection tag-node provider revision differs")
        self.objects.pop(object_path, None)
        self.object_metadata.pop(object_path, None)

    def delete_collection_document_revision(
        self,
        *,
        object_path: str,
        provider_revision: str,
        expected_stored_sha256: str,
    ) -> None:
        current = self.objects.get(object_path)
        if current is None:
            return
        if self._version(current) == provider_revision:
            if hashlib.sha256(current).hexdigest() != expected_stored_sha256:
                raise RuntimeError("mutable collection document revision differs")
            raise RuntimeError("refusing to reclaim current mutable collection document")

    def read_archive_artifact(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        passphrase_id: str,
    ) -> ArchiveArtifactRead:
        assert passphrase_id == DEV_ARCHIVE_PASSPHRASE_ID
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
            revision=object.revision,
            uploaded_at=UPLOADED_AT,
            verified_at=UPLOADED_AT,
        )
        return ArchiveArtifactRead(receipt=receipt, content=content)

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
        passphrase_id: str,
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]:
        assert passphrase_id == DEV_ARCHIVE_PASSPHRASE_ID
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
            entity_token=hashlib.md5(content, usedforsecurity=False).hexdigest(),
            bytes=len(content),
            completed_at=UPLOADED_AT,
        )


def archive_store_binding(store: MemoryArchiveStore) -> ArchiveStoreBinding:
    return ArchiveStoreBinding(
        store=cast(ArchiveStore, store),
        resumable_objects=store,
        immutable_objects=store,
        object_ranges=store,
    )
