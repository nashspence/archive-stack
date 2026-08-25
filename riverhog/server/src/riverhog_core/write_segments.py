from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from riverhog_core.ports.archive_objects import ResumableWriteConstraints


@dataclass(frozen=True, slots=True)
class WriteSegmentPlan:
    """An operational byte range over an already-authoritative opaque object."""

    number: int
    archive_part_number: int
    archive_part_offset: int
    stored_bytes: int


def plan_write_segments(
    archive_part_bytes: Sequence[int],
    constraints: ResumableWriteConstraints,
) -> tuple[WriteSegmentPlan, ...]:
    """Map immutable archive parts to adapter-compatible write segments.

    The mapping never changes archive bytes or boundaries. It only subdivides an
    archive part when the adapter cannot accept that part as one write segment.
    """

    if not archive_part_bytes or any(size < 1 for size in archive_part_bytes):
        raise ValueError("archive parts must contain positive byte counts")
    minimum = constraints.minimum_nonfinal_segment_bytes
    maximum = constraints.maximum_segment_bytes
    if minimum < 1 or (maximum is not None and maximum < minimum):
        raise ValueError("adapter write constraints are invalid")

    plans: list[WriteSegmentPlan] = []
    for archive_index, total in enumerate(archive_part_bytes, start=1):
        object_final = archive_index == len(archive_part_bytes)
        sizes = _segment_sizes(
            total,
            minimum=minimum,
            maximum=maximum,
            object_final=object_final,
        )
        offset = 0
        for size in sizes:
            plans.append(
                WriteSegmentPlan(
                    number=len(plans) + 1,
                    archive_part_number=archive_index,
                    archive_part_offset=offset,
                    stored_bytes=size,
                )
            )
            offset += size

    if (
        constraints.maximum_segment_count is not None
        and len(plans) > constraints.maximum_segment_count
    ):
        raise ValueError("authoritative object exceeds the adapter write-segment count")
    return tuple(plans)


def _segment_sizes(
    total: int,
    *,
    minimum: int,
    maximum: int | None,
    object_final: bool,
) -> tuple[int, ...]:
    if maximum is None or total <= maximum:
        if not object_final and total < minimum:
            raise ValueError(
                "authoritative object cannot satisfy adapter write-segment constraints"
            )
        return (total,)

    count = (total + maximum - 1) // maximum
    final_minimum = 1 if object_final else minimum
    required = (count - 1) * minimum + final_minimum
    if total < required:
        raise ValueError("authoritative object cannot satisfy adapter write-segment constraints")
    sizes = [minimum] * (count - 1) + [final_minimum]
    remaining = total - required
    for index, size in enumerate(sizes):
        accepted = min(maximum - size, remaining)
        sizes[index] += accepted
        remaining -= accepted
    if remaining:
        raise ValueError("authoritative object cannot satisfy adapter write-segment constraints")
    return tuple(sizes)


__all__ = ["WriteSegmentPlan", "plan_write_segments"]
