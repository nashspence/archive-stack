from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from riverhog_protocol.pack_ingress import canonical_json_bytes
from riverhog_protocol.paths import normalize_relpath

from riverhog_core.archive_manifest import (
    build_collection_archive_manifest,
    parse_collection_archive_manifest,
)
from riverhog_core.archive_root import SealedArchiveRoot
from riverhog_core.domain.archive import (
    ArchiveFile,
    PackVolumePlan,
    SealedPackVolume,
    SealedRawVolume,
    StoredPartReceipt,
    VerifiedRawFile,
)


@dataclass(frozen=True, slots=True)
class ArchiveRootProjection:
    collection_id: int
    store: str
    archive_storage_prefix: str
    manifest_object_path: str
    manifest_version_id: str | None
    manifest_stored_bytes: int
    manifest_stored_sha256: str
    manifest_plaintext_sha256: str
    manifest_json: str
    files: int
    bytes: int
    tree_sha256: str
    sealed_at: str


@dataclass(frozen=True, slots=True)
class ArchiveVolumeProjection:
    collection_id: int
    store: str
    volume_id: str
    sequence: int
    kind: str
    relative_path: str
    object_path: str
    version_id: str | None
    plaintext_bytes: int
    stored_bytes: int
    age_state_json: str
    part_receipts_json: str
    index_sha256: str | None
    plan_sha256: str | None
    completed_at: str


@dataclass(frozen=True, slots=True)
class ArchivePackMemberProjection:
    collection_id: int
    store: str
    path: str
    volume_id: str
    unit: int
    member_order: int
    bytes: int
    sha256: str
    header_offset: int
    data_offset: int


@dataclass(frozen=True, slots=True)
class ArchiveSegmentProjection:
    collection_id: int
    store: str
    path: str
    file_offset: int
    volume_id: str
    bytes: int
    file_bytes: int
    file_sha256: str


@dataclass(frozen=True, slots=True)
class ArchiveCatalogProjection:
    root: ArchiveRootProjection
    volumes: tuple[ArchiveVolumeProjection, ...]
    pack_members: tuple[ArchivePackMemberProjection, ...]
    segments: tuple[ArchiveSegmentProjection, ...]


def build_archive_catalog_projection(
    *,
    collection_id: int,
    store: str,
    archive_storage_prefix: str,
    root: SealedArchiveRoot,
    files: Sequence[ArchiveFile],
    packs: Sequence[tuple[PackVolumePlan, SealedPackVolume]],
    raw_volumes: Sequence[SealedRawVolume] = (),
    verified_raw_files: Sequence[VerifiedRawFile] = (),
) -> ArchiveCatalogProjection:
    """Build the exact durable catalog projection for one immutable archive root."""

    if collection_id < 1 or not store:
        raise ValueError("archive catalog collection and store are required")
    prefix = archive_storage_prefix.strip("/")
    if not prefix:
        raise ValueError("archive storage prefix is required")
    expected_manifest = build_collection_archive_manifest(
        files=files,
        packs=packs,
        raw_volumes=raw_volumes,
        verified_raw_files=verified_raw_files,
    )
    if expected_manifest != root.manifest_bytes:
        raise ValueError("sealed root does not match the supplied archive volumes")
    manifest = parse_collection_archive_manifest(root.manifest_bytes)
    tree = manifest["tree"]
    if not isinstance(tree, dict):
        raise RuntimeError("validated archive manifest tree is not a mapping")
    expected_root_path = f"{prefix}/manifest.json.age"
    if (
        root.object_path != expected_root_path
        or root.relative_path != "manifest.json.age"
        or root.tree_sha256 != str(tree["sha256"])
        or root.files != int(tree["files"])
        or root.bytes != int(tree["bytes"])
    ):
        raise ValueError("sealed root receipt identity is inconsistent")

    volumes: list[ArchiveVolumeProjection] = []
    members: list[ArchivePackMemberProjection] = []
    segments: list[ArchiveSegmentProjection] = []
    for plan, pack_receipt in packs:
        relative_path = f"volumes/{pack_receipt.volume_id}.tar.age"
        volumes.append(
            ArchiveVolumeProjection(
                collection_id=collection_id,
                store=store,
                volume_id=pack_receipt.volume_id,
                sequence=pack_receipt.sequence,
                kind="pack",
                relative_path=relative_path,
                object_path=f"{prefix}/{relative_path}",
                version_id=pack_receipt.version_id,
                plaintext_bytes=pack_receipt.plaintext_bytes,
                stored_bytes=pack_receipt.stored_bytes,
                age_state_json=pack_receipt.age_state_json,
                part_receipts_json=_part_receipts_json(pack_receipt.parts),
                index_sha256=pack_receipt.index_sha256,
                plan_sha256=pack_receipt.plan_sha256,
                completed_at=pack_receipt.completed_at,
            )
        )
        for member_order, current in enumerate(plan.members):
            members.append(
                ArchivePackMemberProjection(
                    collection_id=collection_id,
                    store=store,
                    path=current.path,
                    volume_id=pack_receipt.volume_id,
                    unit=current.unit,
                    member_order=member_order,
                    bytes=current.bytes,
                    sha256=current.sha256,
                    header_offset=current.header_offset,
                    data_offset=current.data_offset,
                )
            )
    for raw_receipt in raw_volumes:
        relative_path = f"volumes/{raw_receipt.volume_id}.bin.age"
        volumes.append(
            ArchiveVolumeProjection(
                collection_id=collection_id,
                store=store,
                volume_id=raw_receipt.volume_id,
                sequence=raw_receipt.sequence,
                kind="segment",
                relative_path=relative_path,
                object_path=f"{prefix}/{relative_path}",
                version_id=raw_receipt.version_id,
                plaintext_bytes=raw_receipt.plaintext_bytes,
                stored_bytes=raw_receipt.stored_bytes,
                age_state_json=raw_receipt.age_state_json,
                part_receipts_json=_part_receipts_json(raw_receipt.parts),
                index_sha256=None,
                plan_sha256=None,
                completed_at=raw_receipt.completed_at,
            )
        )
        segments.append(
            ArchiveSegmentProjection(
                collection_id=collection_id,
                store=store,
                path=normalize_relpath(raw_receipt.source_path),
                file_offset=raw_receipt.file_offset,
                volume_id=raw_receipt.volume_id,
                bytes=raw_receipt.plaintext_bytes,
                file_bytes=raw_receipt.file_bytes,
                file_sha256=raw_receipt.file_sha256,
            )
        )
    volumes.sort(key=lambda current: current.sequence)
    if [current.sequence for current in volumes] != list(range(len(volumes))):
        raise ValueError("archive catalog volume sequences are not contiguous")
    manifest_volumes = manifest["volumes"]
    if not isinstance(manifest_volumes, list) or [
        str(current["id"]) for current in manifest_volumes
    ] != [current.volume_id for current in volumes]:
        raise ValueError("archive catalog volumes do not match the immutable root")

    root_projection = ArchiveRootProjection(
        collection_id=collection_id,
        store=store,
        archive_storage_prefix=prefix,
        manifest_object_path=root.object_path,
        manifest_version_id=root.version_id,
        manifest_stored_bytes=root.stored_bytes,
        manifest_stored_sha256=root.stored_sha256,
        manifest_plaintext_sha256=root.plaintext_sha256,
        manifest_json=root.manifest_bytes.decode("utf-8"),
        files=root.files,
        bytes=root.bytes,
        tree_sha256=root.tree_sha256,
        sealed_at=root.completed_at,
    )
    return ArchiveCatalogProjection(
        root=root_projection,
        volumes=tuple(volumes),
        pack_members=tuple(
            sorted(members, key=lambda current: (current.volume_id, current.member_order))
        ),
        segments=tuple(sorted(segments, key=lambda current: (current.path, current.file_offset))),
    )


def _part_receipts_json(parts: Sequence[StoredPartReceipt]) -> str:
    return canonical_json_bytes(
        [
            {
                "number": current.number,
                "plaintext_start": current.plaintext_start,
                "plaintext_bytes": current.plaintext_bytes,
                "plaintext_sha256": current.plaintext_sha256,
                "stored_bytes": current.stored_bytes,
                "stored_sha256": current.stored_sha256,
                "etag": current.etag,
            }
            for current in parts
        ]
    ).decode("utf-8")
