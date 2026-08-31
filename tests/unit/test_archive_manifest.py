from __future__ import annotations

import hashlib
import json

import pytest
from riverhog_archive_contracts import CollectionArchiveManifest, format_archive_sequence
from riverhog_core.archive_manifest import (
    build_collection_archive_authority,
    build_collection_archive_manifest,
)
from riverhog_core.domain.archive import (
    ArchiveFile,
    SealedPackVolume,
    SealedRawVolume,
    StoredArchivePart,
    VerifiedRawFile,
)
from riverhog_core.pack_volume import iter_render_pack_upload_unit, plan_pack_volume
from riverhog_core.raw_verification import raw_file_ordered_volume_commitment

from tests.fixtures.archive import age_state_json


def _file(path: str, content: bytes) -> ArchiveFile:
    return ArchiveFile(path=path, bytes=len(content), sha256=hashlib.sha256(content).hexdigest())


def _segment_id(sequence: int) -> str:
    return f"segment-{format_archive_sequence(sequence)}"


def _pack_receipt(plan, contents: dict[str, bytes]) -> SealedPackVolume:
    parts = []
    for unit in plan.units:
        plaintext = b"".join(
            iter_render_pack_upload_unit(plan, unit.unit, lambda path: (contents[path],))
        )
        parts.append(
            StoredArchivePart(
                number=unit.unit + 1,
                plaintext_start=unit.plaintext_start,
                plaintext_bytes=len(plaintext),
                plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
                stored_bytes=len(plaintext) + 100,
                stored_sha256=hashlib.sha256(b"stored" + plaintext).hexdigest(),
            )
        )
    return SealedPackVolume(
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
        revision="v1",
        completed_at="2026-08-03T00:00:00Z",
    )


def test_manifest_is_small_immutable_volume_index_not_a_file_listing() -> None:
    contents = {"a.txt": b"alpha", "b.txt": b"beta"}
    files = [_file(path, content) for path, content in contents.items()]
    plan = plan_pack_volume(files, sequence=0)
    manifest, volumes = build_collection_archive_authority(
        archive_generation="a" * 64,
        files=files,
        packs=((plan, _pack_receipt(plan, contents)),),
    )
    payload = json.loads(manifest)

    assert payload["schema"] == "collection-archive-manifest/v1"
    assert set(payload["volume_sequence"]) == {"sha256"}
    assert "files" not in payload or isinstance(payload.get("files"), int)
    assert {"a.txt", "b.txt"}.isdisjoint(volumes[0].to_mapping()["volume"])
    assert CollectionArchiveManifest.from_json_bytes(manifest).to_mapping() == payload


def test_v1_construction_vector_fixes_pack_manifest_and_root_identities() -> None:
    contents = {"a.txt": b"alpha", "b.txt": b"beta"}
    files = [_file(path, content) for path, content in contents.items()]
    plan = plan_pack_volume(files, sequence=0)
    plaintext = b"".join(
        chunk
        for unit in plan.units
        for chunk in iter_render_pack_upload_unit(
            plan,
            unit.unit,
            lambda path: (contents[path],),
        )
    )
    manifest = build_collection_archive_manifest(
        archive_generation="a" * 64,
        files=files,
        packs=((plan, _pack_receipt(plan, contents)),),
    )

    assert plan.plan_sha256 == "6037c2daf2fe30c5edc5250e678057363d8ea95b952b2cc13644136d1b9fe678"
    assert hashlib.sha256(plaintext).hexdigest() == (
        "e96fa53e1e9c7787171d51af47b7859e72278fe070628b5370513a4629b113bb"
    )
    assert hashlib.sha256(manifest).hexdigest() == (
        "e0b38cd631b675c5b1d8e0ec6d312e8c27de9f6cb4c05e0345de1aa0b2c3634a"
    )


def test_manifest_validates_raw_segment_coverage_without_repeating_pack_files() -> None:
    small_content = b"small"
    large_content = b"abcdefgh"
    small = _file("small.txt", small_content)
    large = _file("large.bin", large_content)
    plan = plan_pack_volume((small,), sequence=0)
    pack_receipt = _pack_receipt(plan, {small.path: small_content})
    first = large_content[:4]
    second = large_content[4:]
    raw = (
        SealedRawVolume(
            volume_id=_segment_id(1),
            sequence=1,
            relative_path=f"volumes/{_segment_id(1)}.bin.age",
            source_path=large.path,
            file_offset=0,
            plaintext_bytes=len(first),
            age_state_json=age_state_json(len(first)),
            file_bytes=large.bytes,
            file_sha256=large.sha256,
            parts=(
                StoredArchivePart(
                    number=1,
                    plaintext_start=0,
                    plaintext_bytes=len(first),
                    plaintext_sha256=hashlib.sha256(first).hexdigest(),
                    stored_bytes=10,
                    stored_sha256=hashlib.sha256(b"first").hexdigest(),
                ),
            ),
            revision=None,
            completed_at="2026-08-03T00:00:00Z",
        ),
        SealedRawVolume(
            volume_id=_segment_id(2),
            sequence=2,
            relative_path=f"volumes/{_segment_id(2)}.bin.age",
            source_path=large.path,
            file_offset=4,
            plaintext_bytes=len(second),
            age_state_json=age_state_json(len(second)),
            file_bytes=large.bytes,
            file_sha256=large.sha256,
            parts=(
                StoredArchivePart(
                    number=1,
                    plaintext_start=0,
                    plaintext_bytes=len(second),
                    plaintext_sha256=hashlib.sha256(second).hexdigest(),
                    stored_bytes=11,
                    stored_sha256=hashlib.sha256(b"second").hexdigest(),
                ),
            ),
            revision=None,
            completed_at="2026-08-03T00:00:00Z",
        ),
    )

    manifest, volumes = build_collection_archive_authority(
        archive_generation="a" * 64,
        files=(small, large),
        packs=((plan, pack_receipt),),
        raw_volumes=raw,
        verified_raw_files=(
            VerifiedRawFile(
                path=large.path,
                bytes=large.bytes,
                sha256=large.sha256,
                ordered_volume_sha256=raw_file_ordered_volume_commitment(file=large, volumes=raw),
                verified_at="2026-08-03T00:00:00Z",
            ),
        ),
    )
    payload = CollectionArchiveManifest.from_json_bytes(manifest).to_mapping()

    assert [row.volume.kind for row in volumes] == ["pack", "segment", "segment"]
    assert payload["tree"]["files"] == 2
    assert payload["tree"]["bytes"] == len(small_content) + len(large_content)


def test_manifest_rejects_gapped_raw_segments() -> None:
    content = b"abcdefgh"
    file = _file("large.bin", content)
    raw = SealedRawVolume(
        volume_id=_segment_id(0),
        sequence=0,
        relative_path=f"volumes/{_segment_id(0)}.bin.age",
        source_path=file.path,
        file_offset=2,
        plaintext_bytes=6,
        age_state_json=age_state_json(6),
        file_bytes=file.bytes,
        file_sha256=file.sha256,
        parts=(
            StoredArchivePart(
                number=1,
                plaintext_start=0,
                plaintext_bytes=6,
                plaintext_sha256=hashlib.sha256(content[2:]).hexdigest(),
                stored_bytes=7,
                stored_sha256=hashlib.sha256(b"stored").hexdigest(),
            ),
        ),
        revision=None,
        completed_at="2026-08-03T00:00:00Z",
    )

    with pytest.raises(ValueError, match="(contiguous|do not form)"):
        build_collection_archive_manifest(
            archive_generation="a" * 64,
            files=(file,),
            packs=(),
            raw_volumes=(raw,),
            verified_raw_files=(
                VerifiedRawFile(
                    path=file.path,
                    bytes=file.bytes,
                    sha256=file.sha256,
                    ordered_volume_sha256=raw_file_ordered_volume_commitment(
                        file=file, volumes=(raw,)
                    ),
                    verified_at="2026-08-03T00:00:00Z",
                ),
            ),
        )


def test_manifest_is_canonical_json() -> None:
    content = b"schema"
    file = _file("schema.txt", content)
    plan = plan_pack_volume((file,), sequence=0)
    payload = json.loads(
        build_collection_archive_manifest(
            archive_generation="a" * 64,
            files=(file,),
            packs=((plan, _pack_receipt(plan, {file.path: content})),),
        )
    )
    assert payload["schema"] == "collection-archive-manifest/v1"
