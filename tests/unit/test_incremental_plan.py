from __future__ import annotations

import hashlib
import json

import pytest
from riverhog_core.collection_plan import CollectionVolumePolicy
from riverhog_core.domain.archive import ArchiveFile
from riverhog_core.incremental_plan import (
    OrderedArchiveFile,
    advance_incremental_volume_plan,
    incremental_volume_planner_checkpoint_bytes,
    new_incremental_volume_planner,
    parse_incremental_volume_planner_checkpoint,
)

TEST_PART_BYTES = 5 * 1024**2


def _ordered(order: int, path: str, byte_count: int) -> OrderedArchiveFile:
    return OrderedArchiveFile(
        order=order,
        file=ArchiveFile(
            path=path,
            bytes=byte_count,
            sha256=hashlib.sha256(f"{path}:{byte_count}".encode()).hexdigest(),
        ),
    )


def _policy() -> CollectionVolumePolicy:
    return CollectionVolumePolicy(
        pack_source_bytes=10,
        pack_files=2,
        pack_member_bytes=8,
        pack_part_plaintext_bytes=5 * 1024 * 1024,
        raw_volume_plaintext_bytes=TEST_PART_BYTES,
        raw_part_plaintext_bytes=TEST_PART_BYTES,
    )


def test_incremental_planner_restart_reproduces_volume_boundaries() -> None:
    first = (_ordered(0, "a.txt", 3),)
    second = (_ordered(1, "b.txt", 4), _ordered(2, "c.txt", 2))

    initial = new_incremental_volume_planner(policy=_policy())
    open_batch = advance_incremental_volume_plan(initial, first)
    assert open_batch.volumes == ()
    restored = parse_incremental_volume_planner_checkpoint(
        incremental_volume_planner_checkpoint_bytes(open_batch.checkpoint)
    )
    resumed = advance_incremental_volume_plan(restored, second, final=True)
    one_shot = advance_incremental_volume_plan(initial, (*first, *second), final=True)

    assert resumed.checkpoint == one_shot.checkpoint
    assert resumed.packs == one_shot.packs
    assert resumed.raw_volumes == one_shot.raw_volumes
    assert [current.sequence for current in resumed.packs] == [0, 1]


def test_large_file_flushes_pending_pack_and_emits_canonical_segments() -> None:
    batch = advance_incremental_volume_plan(
        new_incremental_volume_planner(policy=_policy()),
        (_ordered(0, "small.txt", 3), _ordered(1, "large.bin", 2 * TEST_PART_BYTES + 4)),
        final=True,
    )

    assert [current.sequence for current in batch.packs] == [0]
    assert [current.sequence for current in batch.raw_volumes] == [1, 2, 3]
    assert batch.checkpoint.next_sequence == 4
    assert batch.checkpoint.closed is True
    assert batch.checkpoint.pending_pack_files == ()


def test_incremental_planner_rejects_noncontiguous_registration_order() -> None:
    with pytest.raises(ValueError, match="order"):
        advance_incremental_volume_plan(
            new_incremental_volume_planner(policy=_policy()),
            (_ordered(1, "wrong.txt", 1),),
        )


def test_incremental_checkpoint_has_canonical_identity() -> None:
    checkpoint = advance_incremental_volume_plan(
        new_incremental_volume_planner(policy=_policy()),
        (_ordered(0, "pending.txt", 2),),
    ).checkpoint
    payload = json.loads(incremental_volume_planner_checkpoint_bytes(checkpoint))
    assert payload["schema"] == "incremental-volume-planner-checkpoint/v1"
