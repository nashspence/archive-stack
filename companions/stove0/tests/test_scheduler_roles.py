from __future__ import annotations

from typing import cast

from stove0_core import ClaimBinding, Stove0Scheduler, WorkRecord
from stove0_core.scheduler import _phases_for_role
from stove0_protocol import CollectionRootRef, RecipeRef, WorkIdentity, WorkPayload


def _identity(index: int) -> WorkIdentity:
    return WorkIdentity.seal(
        WorkPayload(
            recipe=RecipeRef(id="fixture/v1", revision=1, sha256="a" * 64),
            inputs=(
                CollectionRootRef(
                    collection_id=index,
                    archive_root_sha256=f"{index:064x}",
                    content_identity="b" * 64,
                ),
            ),
        )
    )


class _State:
    def __init__(self, records: tuple[WorkRecord, ...]) -> None:
        self.records = tuple(sorted(records, key=lambda item: item.work_id))
        self.cursors: dict[str, tuple[str, int]] = {}
        self.scan_calls: list[dict[str, object]] = []
        self.prune_calls: list[str] = []

    def scan_work(self, **kwargs: object) -> tuple[list[WorkRecord], str]:
        self.scan_calls.append(kwargs)
        phases = cast(tuple[str, ...], kwargs["phases"])
        after = cast(str, kwargs["after_work_id"])
        limit = cast(int, kwargs["limit"])
        eligible = [item for item in self.records if item.phase in phases and item.work_id > after]
        if not eligible and after:
            eligible = [item for item in self.records if item.phase in phases]
        selected = eligible[:limit]
        return selected, (selected[-1].work_id if selected else "")

    def prune_operational_state(self, *, cutoff: str) -> dict[str, int]:
        self.prune_calls.append(cutoff)
        return {"work": 0, "events": 0}

    def load_cursor(self, stream: str) -> tuple[str, int] | None:
        return self.cursors.get(stream)

    def compare_and_swap_cursor(
        self,
        stream: str,
        *,
        expected_revision: int | None,
        cursor: str,
    ) -> tuple[str, int]:
        current = self.cursors.get(stream)
        current_revision = None if current is None else current[1]
        assert current_revision == expected_revision
        revision = 1 if expected_revision is None else expected_revision + 1
        self.cursors[stream] = (cursor, revision)
        return cursor, revision


class _Coordinator:
    def __init__(
        self,
        records: tuple[WorkRecord, ...],
        *,
        noops: frozenset[str] = frozenset(),
    ) -> None:
        self.records = {item.work_id: item for item in records}
        self.noops = noops
        self.steps: list[str] = []

    def step(self, work_id: str) -> WorkRecord:
        self.steps.append(work_id)
        record = self.records[work_id]
        if work_id in self.noops:
            return record
        return record.model_copy(update={"revision": record.revision + 1})


def test_controller_and_worker_advance_disjoint_phases_of_one_work_authority() -> None:
    controller_record = WorkRecord(work=_identity(1))
    worker_record = WorkRecord(
        work=_identity(2),
        phase="executing",
        claim=ClaimBinding(claim_id="claim-2", fence=1),
    )
    records = (controller_record, worker_record)
    coordinator = _Coordinator(records)
    scheduler = Stove0Scheduler(
        coordinator=coordinator,  # type: ignore[arg-type]
        state=_State(records),  # type: ignore[arg-type]
    )

    controller = scheduler.advance(role="controller")
    assert controller["progressed"] == [controller_record.work_id]
    assert coordinator.steps == [controller_record.work_id]

    coordinator.steps.clear()
    worker = scheduler.advance(role="worker")
    assert worker["progressed"] == [worker_record.work_id]
    assert coordinator.steps == [worker_record.work_id]


def test_controller_owns_branch_and_join_coordination_ticks() -> None:
    assert "coordinating" in _phases_for_role("controller")
    assert "coordinating" not in _phases_for_role("worker")


def test_worker_tick_advances_work_without_controller_pruning() -> None:
    worker_record = WorkRecord(
        work=_identity(2),
        phase="executing",
        claim=ClaimBinding(claim_id="claim-2", fence=1),
    )
    coordinator = _Coordinator((worker_record,))
    scheduler = Stove0Scheduler(
        coordinator=coordinator,  # type: ignore[arg-type]
        state=_State((worker_record,)),  # type: ignore[arg-type]
    )

    result = scheduler.run_once(role="worker")

    assert result["pruning"] is None
    assert cast(_State, scheduler.state).prune_calls == []
    assert coordinator.steps == [worker_record.work_id]


def test_scheduler_uses_bounded_keyset_scans_and_rotates_past_a_permanent_noop() -> None:
    records = tuple(WorkRecord(work=_identity(index)) for index in range(1, 4))
    ordered = tuple(sorted(records, key=lambda item: item.work_id))
    state = _State(records)
    coordinator = _Coordinator(records, noops=frozenset({ordered[0].work_id}))
    scheduler = Stove0Scheduler(
        coordinator=coordinator,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
    )

    first = scheduler.advance(role="controller", limit=1)
    second = scheduler.advance(role="controller", limit=1)

    assert first["progressed"] == []
    assert second["progressed"] == [ordered[1].work_id]
    assert [call["limit"] for call in state.scan_calls] == [1, 1]
    assert all(
        call["phases"] == tuple(sorted(_phases_for_role("controller"))) for call in state.scan_calls
    )
    assert state.cursors["stove0-work-scan/controller/v1"][0] == ordered[1].work_id


def test_scheduler_visits_the_runnable_keyset_without_scanning_terminal_history() -> None:
    records = tuple(WorkRecord(work=_identity(index)) for index in range(1, 6))
    state = _State(records)
    coordinator = _Coordinator(records)
    scheduler = Stove0Scheduler(
        coordinator=coordinator,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
    )

    results = [scheduler.advance(role="controller", limit=2) for _ in range(3)]

    assert {item for result in results for item in result["progressed"]} == {
        record.work_id for record in records
    }
    assert all(len(result["progressed"]) <= 2 for result in results)


def test_scheduler_reaches_runnable_work_with_large_terminal_history_in_one_scan() -> None:
    terminal = tuple(WorkRecord(work=_identity(index), phase="canceled") for index in range(1, 251))
    runnable = WorkRecord(work=_identity(251))
    state = _State((*terminal, runnable))
    coordinator = _Coordinator((runnable,))
    scheduler = Stove0Scheduler(
        coordinator=coordinator,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
    )

    result = scheduler.advance(role="controller", limit=1)

    assert result["progressed"] == [runnable.work_id]
    assert state.scan_calls[0]["limit"] == 1


def test_controller_prunes_expired_operational_state_on_a_bounded_interval() -> None:
    state = _State(())
    scheduler = Stove0Scheduler(
        coordinator=_Coordinator(()),  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        operational_state_retention_seconds=86400,
    )

    first = scheduler.run_once(role="controller")
    second = scheduler.run_once(role="controller")

    assert first["pruning"] == {"work": 0, "events": 0}
    assert second["pruning"] is None
    assert len(state.prune_calls) == 1
    assert state.prune_calls[0].endswith("Z")
