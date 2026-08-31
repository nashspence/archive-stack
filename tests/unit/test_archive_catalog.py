from __future__ import annotations

import hashlib

import pytest
from riverhog_core.archive_catalog import build_archive_catalog_projection
from riverhog_core.archive_manifest import (
    build_collection_archive_authority,
    build_collection_archive_terminal_document,
)
from riverhog_core.archive_root import SealedArchiveRoot, SealedArchiveVolumeMetadata
from riverhog_core.domain.archive import (
    ArchiveFile,
    SealedPackVolume,
    StoredArchivePart,
)
from riverhog_core.pack_volume import iter_render_pack_upload_unit, plan_pack_volume

from tests.fixtures.archive import age_state_json


def _file(path: str, content: bytes) -> ArchiveFile:
    return ArchiveFile(
        path=path,
        bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _fixture() -> tuple[
    tuple[ArchiveFile, ...],
    tuple[object, SealedPackVolume],
    SealedArchiveRoot,
]:
    contents = {"a.txt": b"alpha", "b.txt": b"beta"}
    files = tuple(_file(path, value) for path, value in contents.items())
    plan = plan_pack_volume(files, sequence=0)
    parts = []
    for unit in plan.units:
        plaintext = b"".join(
            iter_render_pack_upload_unit(
                plan,
                unit.unit,
                lambda path: (contents[path],),
            )
        )
        parts.append(
            StoredArchivePart(
                number=unit.unit + 1,
                plaintext_start=unit.plaintext_start,
                plaintext_bytes=len(plaintext),
                plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
                stored_bytes=len(plaintext) + 16,
                stored_sha256=hashlib.sha256(b"stored" + plaintext).hexdigest(),
            )
        )
    receipt = SealedPackVolume(
        volume_id=plan.volume_id,
        sequence=plan.sequence,
        relative_path=f"volumes/{plan.volume_id}.tar.age",
        files=len(plan.members),
        source_bytes=sum(current.bytes for current in plan.members),
        plaintext_bytes=plan.plaintext_bytes,
        age_state_json=age_state_json(plan.plaintext_bytes),
        index_sha256=plan.index_sha256,
        plan_sha256=plan.plan_sha256,
        parts=tuple(parts),
        revision="version-1",
        completed_at="2026-08-03T00:00:00Z",
    )
    manifest, volume_documents = build_collection_archive_authority(
        archive_generation="a" * 64,
        files=files,
        packs=((plan, receipt),),
    )
    volume_metadata = volume_documents[0].to_json_bytes()
    terminal = build_collection_archive_terminal_document(
        archive_generation="a" * 64,
        tree_sha256=str(__import__("json").loads(manifest)["tree"]["sha256"]),
        sequence=1,
    )
    terminal_metadata = terminal.to_json_bytes()
    root = SealedArchiveRoot(
        object_path="archives/example/manifest.json.age",
        relative_path="manifest.json.age",
        revision="root-version",
        plaintext_bytes=len(manifest),
        plaintext_sha256=hashlib.sha256(manifest).hexdigest(),
        stored_bytes=len(manifest) + 100,
        stored_sha256=hashlib.sha256(b"encrypted-root").hexdigest(),
        tree_sha256=str(__import__("json").loads(manifest)["tree"]["sha256"]),
        files=len(files),
        bytes=sum(current.bytes for current in files),
        completed_at="2026-08-03T00:00:01Z",
        manifest_bytes=manifest,
        volume_metadata=(
            SealedArchiveVolumeMetadata(
                sequence=0,
                object_path=("archives/example/metadata/volume-000000000000.json.age"),
                relative_path="metadata/volume-000000000000.json.age",
                revision="metadata-version",
                plaintext_bytes=len(volume_metadata),
                plaintext_sha256=hashlib.sha256(volume_metadata).hexdigest(),
                stored_bytes=len(volume_metadata) + 100,
                stored_sha256=hashlib.sha256(b"encrypted-metadata").hexdigest(),
                completed_at="2026-08-03T00:00:00Z",
            ),
            SealedArchiveVolumeMetadata(
                sequence=1,
                object_path=("archives/example/metadata/volume-000000000001.json.age"),
                relative_path="metadata/volume-000000000001.json.age",
                revision="terminal-version",
                plaintext_bytes=len(terminal_metadata),
                plaintext_sha256=hashlib.sha256(terminal_metadata).hexdigest(),
                stored_bytes=len(terminal_metadata) + 100,
                stored_sha256=hashlib.sha256(b"encrypted-terminal").hexdigest(),
                completed_at="2026-08-03T00:00:00Z",
            ),
        ),
    )
    return files, (plan, receipt), root


def test_catalog_projection_is_exactly_bound_to_root_and_volume_receipts() -> None:
    files, pack, root = _fixture()
    projection = build_archive_catalog_projection(
        collection_id=7,
        store="archive",
        archive_storage_prefix="archives/example",
        root=root,
        files=files,
        packs=(pack,),
    )

    assert projection.root.manifest_plaintext_sha256 == root.plaintext_sha256
    assert [current.volume_id for current in projection.volumes] == [f"pack-{0:064x}"]
    assert [current.path for current in projection.pack_members] == ["a.txt", "b.txt"]
    assert projection.segments == ()
    assert projection.volumes[0].object_path == (f"archives/example/volumes/pack-{0:064x}.tar.age")


def test_catalog_projection_rejects_a_root_for_different_volume_set() -> None:
    files, pack, root = _fixture()
    changed = SealedArchiveRoot(
        object_path=root.object_path,
        relative_path=root.relative_path,
        revision=root.revision,
        plaintext_bytes=root.plaintext_bytes,
        plaintext_sha256=root.plaintext_sha256,
        stored_bytes=root.stored_bytes,
        stored_sha256=root.stored_sha256,
        tree_sha256="0" * 64,
        files=root.files,
        bytes=root.bytes,
        completed_at=root.completed_at,
        manifest_bytes=root.manifest_bytes,
        volume_metadata=root.volume_metadata,
    )

    with pytest.raises(ValueError, match="root receipt"):
        build_archive_catalog_projection(
            collection_id=7,
            store="archive",
            archive_storage_prefix="archives/example",
            root=changed,
            files=files,
            packs=(pack,),
        )
