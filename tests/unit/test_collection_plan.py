from __future__ import annotations

import hashlib

from riverhog_age import PORTABLE_MULTIPART_MIN_PART_BYTES
from riverhog_core.collection_plan import (
    CollectionVolumePolicy,
    plan_collection_volumes,
)
from riverhog_core.domain.archive import ArchiveFile


def _file(path: str, byte_count: int, marker: bytes) -> ArchiveFile:
    digest = hashlib.sha256(marker * byte_count).hexdigest()
    return ArchiveFile(path=path, bytes=byte_count, sha256=digest)


def test_collection_planner_assigns_canonical_pack_then_segment_sequences() -> None:
    policy = CollectionVolumePolicy(
        pack_source_bytes=10,
        pack_files=2,
        pack_member_bytes=8,
        pack_part_plaintext_bytes=PORTABLE_MULTIPART_MIN_PART_BYTES,
        raw_volume_plaintext_bytes=PORTABLE_MULTIPART_MIN_PART_BYTES,
        raw_part_plaintext_bytes=PORTABLE_MULTIPART_MIN_PART_BYTES,
    )
    plan = plan_collection_volumes(
        (
            _file("small-b", 4, b"b"),
            _file("large", 2 * PORTABLE_MULTIPART_MIN_PART_BYTES + 2, b"l"),
            _file("small-a", 3, b"a"),
        ),
        policy=policy,
    )

    assert [current.volume_id for current in plan.packs] == ["pack-000000000000"]
    assert [current.sequence for current in plan.raw_volumes] == [1, 2, 3]
    assert [current.file_offset for current in plan.raw_volumes] == [
        0,
        PORTABLE_MULTIPART_MIN_PART_BYTES,
        2 * PORTABLE_MULTIPART_MIN_PART_BYTES,
    ]
    assert plan.volume_count == 4


def test_default_policy_preserves_retrieval_economics_boundary() -> None:
    policy = CollectionVolumePolicy()
    below = _file("below.bin", policy.pack_member_bytes - 1, b"b")
    at = _file("at.bin", policy.pack_member_bytes, b"a")
    plan = plan_collection_volumes((below, at))

    assert policy.pack_member_bytes == 16 * 1024 * 1024
    assert policy.pack_source_bytes == 32 * 1024 * 1024
    assert [current.path for current in plan.packs[0].members] == ["below.bin"]
    assert [current.source_path for current in plan.raw_volumes] == ["at.bin"]


def test_collection_policy_exposes_persisted_layout_knobs() -> None:
    policy = CollectionVolumePolicy.from_env(
        {
            "RIVERHOG_PACK_SOURCE_BYTES": "48MiB",
            "RIVERHOG_PACK_FILES": "12000",
            "RIVERHOG_PACK_MEMBER_BYTES": "12MiB",
            "RIVERHOG_ARCHIVE_MULTIPART_PART_BYTES": "96MiB",
            "RIVERHOG_PACK_PART_PLAINTEXT_BYTES": "64MiB",
            "RIVERHOG_RAW_VOLUME_PLAINTEXT_BYTES": "24GiB",
            "RIVERHOG_RAW_PART_PLAINTEXT_BYTES": "96MiB",
        }
    )

    assert policy.pack_source_bytes == 48 * 1024**2
    assert policy.pack_files == 12_000
    assert policy.pack_member_bytes == 12 * 1024**2
    assert policy.pack_part_plaintext_bytes == 64 * 1024**2
    assert policy.raw_volume_plaintext_bytes == 24 * 1024**3
    assert policy.raw_part_plaintext_bytes == 96 * 1024**2
