from __future__ import annotations

from typing import cast

from stove0_core import ClaimBinding, Stove0Scheduler, WorkRecord
from stove0_protocol import CollectionRootRef, RecipeRef, WorkIdentity, WorkPayload


def _identity(index: int) -> WorkIdentity:
    return WorkIdentity.seal(
        WorkPayload(
            recipe=RecipeRef(id="fixture/v1", revision=1, sha256="a" * 64),
            inputs=(
                CollectionRootRef(
                    collection_id=index,
                    manifest_sha256=str(index) * 64,
                    content_etag="b" * 64,
                ),
            ),
        )
    )


class _State:
    def __init__(self, records: tuple[WorkRecord, ...]) -> None:
        self.records = records

    def list_work(self, **_kwargs: object) -> dict[str, object]:
        return {"work": [item.model_dump(mode="json", exclude_none=True) for item in self.records]}


class _Coordinator:
    def __init__(self, records: tuple[WorkRecord, ...]) -> None:
        self.records = {item.work_id: item for item in records}
        self.steps: list[str] = []

    def step(self, work_id: str) -> WorkRecord:
        self.steps.append(work_id)
        return self.records[work_id]


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
        riverhog=cast(object, None),  # type: ignore[arg-type]
        catalog=cast(object, None),  # type: ignore[arg-type]
        planner=cast(object, None),  # type: ignore[arg-type]
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


def test_worker_tick_never_consumes_the_controller_event_cursor() -> None:
    worker_record = WorkRecord(
        work=_identity(2),
        phase="executing",
        claim=ClaimBinding(claim_id="claim-2", fence=1),
    )
    coordinator = _Coordinator((worker_record,))
    scheduler = Stove0Scheduler(
        riverhog=cast(object, None),  # type: ignore[arg-type]
        catalog=cast(object, None),  # type: ignore[arg-type]
        planner=cast(object, None),  # type: ignore[arg-type]
        coordinator=coordinator,  # type: ignore[arg-type]
        state=_State((worker_record,)),  # type: ignore[arg-type]
    )

    result = scheduler.run_once(role="worker")

    assert result["events"] == {
        "events": 0,
        "next_cursor": None,
        "has_more": False,
        "work_ids": [],
    }
    assert coordinator.steps == [worker_record.work_id]
