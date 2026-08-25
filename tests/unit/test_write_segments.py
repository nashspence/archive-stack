from __future__ import annotations

import pytest
from riverhog_core.ports.archive_objects import ResumableWriteConstraints
from riverhog_core.write_segments import plan_write_segments


def _render(
    content: bytes,
    archive_parts: tuple[int, ...],
    *,
    constraints: ResumableWriteConstraints,
) -> bytes:
    plans = plan_write_segments(archive_parts, constraints)
    starts: dict[int, int] = {}
    cursor = 0
    for number, size in enumerate(archive_parts, start=1):
        starts[number] = cursor
        cursor += size
    return b"".join(
        content[
            starts[plan.archive_part_number] + plan.archive_part_offset : starts[
                plan.archive_part_number
            ]
            + plan.archive_part_offset
            + plan.stored_bytes
        ]
        for plan in plans
    )


def test_two_adapter_profiles_write_the_exact_same_authoritative_object() -> None:
    archive_parts = (9, 7)
    content = bytes(range(sum(archive_parts)))
    one_to_one = ResumableWriteConstraints(1, None, None)
    subdivided = ResumableWriteConstraints(2, 4, None)

    assert [plan.stored_bytes for plan in plan_write_segments(archive_parts, one_to_one)] == [9, 7]
    assert [plan.stored_bytes for plan in plan_write_segments(archive_parts, subdivided)] == [
        4,
        3,
        2,
        4,
        3,
    ]
    assert _render(content, archive_parts, constraints=one_to_one) == content
    assert _render(content, archive_parts, constraints=subdivided) == content


def test_s3_compatible_profile_does_not_change_v1_archive_parts() -> None:
    mib = 1024 * 1024
    constraints = ResumableWriteConstraints(5 * mib, 5 * 1024**3, 10_000)

    plans = plan_write_segments((64 * mib, 64 * mib, 17), constraints)

    assert [(plan.archive_part_number, plan.stored_bytes) for plan in plans] == [
        (1, 64 * mib),
        (2, 64 * mib),
        (3, 17),
    ]


def test_only_the_final_object_segment_may_be_smaller_than_the_adapter_minimum() -> None:
    constraints = ResumableWriteConstraints(5, 6, None)

    assert [plan.stored_bytes for plan in plan_write_segments((7,), constraints)] == [6, 1]
    with pytest.raises(ValueError, match="cannot satisfy"):
        plan_write_segments((7, 1), constraints)


def test_adapter_segment_count_is_an_operational_failure_not_an_archive_replan() -> None:
    with pytest.raises(ValueError, match="segment count"):
        plan_write_segments(
            (9, 7),
            ResumableWriteConstraints(2, 4, 4),
        )
