from __future__ import annotations

from typing import cast

from lifecycle_events import EventPage, cloud_event
from riverhog_protocol.errors import NotFound
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
                    manifest_sha256=str(index) * 64,
                    content_etag="b" * 64,
                ),
            ),
        )
    )


class _State:
    def __init__(self, records: tuple[WorkRecord, ...]) -> None:
        self.records = tuple(sorted(records, key=lambda item: item.work_id))
        self.cursors: dict[str, tuple[str, int]] = {}
        self.list_calls: list[dict[str, object]] = []

    def list_work(self, **kwargs: object) -> dict[str, object]:
        self.list_calls.append(kwargs)
        page = int(cast(int, kwargs["page"]))
        per_page = int(cast(int, kwargs["per_page"]))
        start = (page - 1) * per_page
        selected = self.records[start : start + per_page]
        total = len(self.records)
        return {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page,
            "work": [item.model_dump(mode="json", exclude_none=True) for item in selected],
        }

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


def test_controller_owns_branch_and_join_coordination_ticks() -> None:
    assert "coordinating" in _phases_for_role("controller")
    assert "coordinating" not in _phases_for_role("worker")


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


def test_scheduler_uses_bounded_pages_and_rotates_past_a_permanent_noop() -> None:
    records = tuple(WorkRecord(work=_identity(index)) for index in range(1, 4))
    ordered = tuple(sorted(records, key=lambda item: item.work_id))
    state = _State(records)
    coordinator = _Coordinator(records, noops=frozenset({ordered[0].work_id}))
    scheduler = Stove0Scheduler(
        riverhog=cast(object, None),  # type: ignore[arg-type]
        catalog=cast(object, None),  # type: ignore[arg-type]
        planner=cast(object, None),  # type: ignore[arg-type]
        coordinator=coordinator,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
    )

    first = scheduler.advance(role="controller", limit=1)
    second = scheduler.advance(role="controller", limit=1)

    assert first["progressed"] == []
    assert second["progressed"] == [ordered[1].work_id]
    assert [call["per_page"] for call in state.list_calls] == [1, 1]
    assert all(call["all_items"] is False for call in state.list_calls)
    assert state.cursors["stove0-work-scan/controller/v1"][0] == "3"


def test_scheduler_visits_more_than_one_page_without_unbounded_listing() -> None:
    records = tuple(WorkRecord(work=_identity(index)) for index in range(1, 6))
    state = _State(records)
    coordinator = _Coordinator(records)
    scheduler = Stove0Scheduler(
        riverhog=cast(object, None),  # type: ignore[arg-type]
        catalog=cast(object, None),  # type: ignore[arg-type]
        planner=cast(object, None),  # type: ignore[arg-type]
        coordinator=coordinator,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
    )

    results = [scheduler.advance(role="controller", limit=2) for _ in range(3)]

    assert [result["page"] for result in results] == [1, 2, 3]
    assert {item for result in results for item in result["progressed"]} == {
        record.work_id for record in records
    }
    assert all(len(result["progressed"]) <= 2 for result in results)


class _LifecycleRiverhog:
    def __init__(self, events: EventPage) -> None:
        self.events = events
        self.collection_reads: list[int] = []

    def list_lifecycle_events(self, *, after: str, limit: int) -> EventPage:
        assert after == "0"
        assert limit == 100
        return self.events

    def get_collection(self, collection_id: int) -> dict[str, object]:
        self.collection_reads.append(collection_id)
        raise NotFound("collection is absent")


def test_lifecycle_cursor_advances_past_deleted_malformed_and_unrelated_events() -> None:
    events = EventPage(
        events=[
            cloud_event(
                event_id="deleted",
                source="urn:riverhog:riverhog",
                type="io.riverhog.riverhog.collection.finalized",
                subject="7",
            ),
            cloud_event(
                event_id="malformed",
                source="urn:riverhog:riverhog",
                type="io.riverhog.riverhog.collection.tags_changed",
                subject="not-a-collection",
            ),
            cloud_event(
                event_id="later-valid",
                source="urn:riverhog:riverhog",
                type="io.riverhog.riverhog.collection.tags_changed",
                subject="8",
            ),
            cloud_event(
                event_id="unrelated",
                source="urn:riverhog:riverhog",
                type="io.riverhog.riverhog.retrieval.ready",
                subject="job-1",
            ),
        ],
        next_cursor="4",
        has_more=False,
    )
    riverhog = _LifecycleRiverhog(events)
    state = _State(())
    scheduler = Stove0Scheduler(
        riverhog=riverhog,  # type: ignore[arg-type]
        catalog=cast(object, None),  # type: ignore[arg-type]
        planner=cast(object, None),  # type: ignore[arg-type]
        coordinator=cast(object, None),  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
    )

    result = scheduler.ingest_events()

    assert result["events"] == 4
    assert result["work_ids"] == []
    assert result["failures"] == [
        {
            "event_id": "malformed",
            "error": "ValueError: Riverhog event collection identity is invalid",
        }
    ]
    assert riverhog.collection_reads == [7, 8]
    assert state.cursors["riverhog-lifecycle/v1"] == ("4", 1)
