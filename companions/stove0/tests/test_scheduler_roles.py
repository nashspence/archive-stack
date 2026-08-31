from __future__ import annotations

from typing import cast

from lifecycle_events import EventPage, cloud_event
from riverhog_protocol.collection_workflow_transport import (
    ArtifactDispositionPageDocument,
)
from riverhog_protocol.collection_workflows import (
    ArtifactDisposition,
    ArtifactDispositionSetIdentity,
    CollectionDerivation,
    OperationIdentity,
    canonical_json_sha256,
)
from riverhog_protocol.errors import NotFound
from riverhog_protocol.lifecycle_events import validate_riverhog_event
from riverhog_protocol.paths import tag_set_identity
from stove0_core import ClaimBinding, RecipeDefinition, Stove0Scheduler, WorkRecord
from stove0_core.recipes import RecipeRoute
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
    assert result["pruning"] is None
    assert cast(_State, scheduler.state).prune_calls == []
    assert coordinator.steps == [worker_record.work_id]


def test_scheduler_uses_bounded_keyset_scans_and_rotates_past_a_permanent_noop() -> None:
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
        riverhog=cast(object, None),  # type: ignore[arg-type]
        catalog=cast(object, None),  # type: ignore[arg-type]
        planner=cast(object, None),  # type: ignore[arg-type]
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
        riverhog=cast(object, None),  # type: ignore[arg-type]
        catalog=cast(object, None),  # type: ignore[arg-type]
        planner=cast(object, None),  # type: ignore[arg-type]
        coordinator=coordinator,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
    )

    result = scheduler.advance(role="controller", limit=1)

    assert result["progressed"] == [runnable.work_id]
    assert state.scan_calls[0]["limit"] == 1


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


def test_controller_prunes_expired_operational_state_on_a_bounded_interval() -> None:
    state = _State(())
    scheduler = Stove0Scheduler(
        riverhog=_LifecycleRiverhog(EventPage(events=[], next_cursor="0", has_more=False)),  # type: ignore[arg-type]
        catalog=cast(object, None),  # type: ignore[arg-type]
        planner=cast(object, None),  # type: ignore[arg-type]
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


def test_lifecycle_cursor_advances_past_deleted_and_unrelated_events() -> None:
    def collection_event(event_id: str, event_type: str, collection_id: int):
        return validate_riverhog_event(
            cloud_event(
                event_id=event_id,
                source="urn:riverhog:riverhog",
                type=event_type,
                subject=str(collection_id),
                data={
                    "collection_id": collection_id,
                    "collection_created_at": "2026-08-01T00:00:00.000000Z",
                    "collection_tag_count": 0,
                    "actor": {"app": "riverhog"},
                    "initiator": {"app": "fixture"},
                    **(
                        {
                            "files_total": 1,
                            "bytes_total": 1,
                            "archive_root_sha256": "a" * 64,
                        }
                        if event_type.endswith("collection.finalized")
                        else {}
                    ),
                },
            )
        )

    events = EventPage(
        events=[
            collection_event("deleted", "io.riverhog.riverhog.collection.finalized", 7),
            collection_event("later-valid", "io.riverhog.riverhog.collection.tags_changed", 8),
            validate_riverhog_event(
                cloud_event(
                    event_id="unrelated",
                    source="urn:riverhog:riverhog",
                    type="io.riverhog.riverhog.retrieval.ready",
                    subject="job-1",
                    data={
                        "retrieval_id": "job-1",
                        "collection_ids": [7],
                        "collection_id": 7,
                        "state": "ready",
                        "expires_at": "2026-08-01T01:00:00.000000Z",
                        "actor": {"app": "riverhog"},
                        "initiator": {"app": "fixture"},
                    },
                )
            ),
        ],
        next_cursor="3",
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

    assert result["events"] == 3
    assert result["work_ids"] == []
    assert result["failures"] == []
    assert riverhog.collection_reads == [7, 8]
    assert state.cursors["riverhog-lifecycle/v1"] == ("3", 1)


def _recipe(label: str, *, allow_derived_inputs: bool = True) -> RecipeDefinition:
    return RecipeDefinition(
        id=f"fixture.{label}/v1",
        revision=1,
        input_tags=("fixture",),
        unmatched_artifact_disposition="retain-in-source",
        allow_derived_inputs=allow_derived_inputs,
        routes=(
            RecipeRoute(
                id="copy",
                operation_id="fixture.copy/v1",
                target_registration_id="fixture",
                output_tags=("fixture-output",),
            ),
        ),
    )


class _LineageCatalog:
    def __init__(self, recipe: RecipeDefinition) -> None:
        self.recipe = recipe

    def matching(self, tags: tuple[str, ...]) -> tuple[RecipeDefinition, ...]:
        assert tags == ("fixture",)
        return (self.recipe,)


class _LineagePlanner:
    def __init__(self, recipe: RecipeDefinition) -> None:
        self.recipe = recipe

    def create_work(
        self,
        recipe_id: str,
        roots: tuple[CollectionRootRef, ...],
        *,
        revision: int,
    ) -> WorkIdentity:
        assert (recipe_id, revision) == (self.recipe.id, self.recipe.revision)
        return WorkIdentity.seal(WorkPayload(recipe=self.recipe.ref, inputs=roots))


class _LineageCoordinator:
    def __init__(self) -> None:
        self.records: dict[str, WorkRecord] = {}

    def create_or_resume(self, identity: WorkIdentity) -> WorkRecord:
        return self.records.setdefault(identity.work_id, WorkRecord(work=identity))


class _LineageRiverhog:
    def __init__(self, derivations: dict[int, dict[str, object]]) -> None:
        self.derivations = derivations
        self.tag_stream_reads: list[int] = []
        self.disposition_inputs = {
            str(derivation["claim"]["id"]): collection_id - 1
            for collection_id, derivation in derivations.items()
        }

    def get_collection(self, collection_id: int) -> dict[str, object]:
        character = f"{collection_id % 16:x}"
        return {
            "id": collection_id,
            "archive_root_sha256": character * 64,
            "content_identity": f"{(collection_id + 1) % 16:x}" * 64,
        }

    def get_collection_tags(
        self, collection_id: int, *, page_size: int, page_token: str | None
    ) -> dict[str, object]:
        assert (page_size, page_token) == (100, None)
        self.tag_stream_reads.append(collection_id)
        return {
            "collection_id": collection_id,
            "metadata_revision": 1,
            "inventory_identity": f"{collection_id % 16:x}" * 64,
            "tag_count": 1,
            "page_size": page_size,
            "next_page_token": None,
            "tags": ["fixture"],
        }

    def get_collection_derivation(self, collection_id: int) -> dict[str, object]:
        try:
            derivation = self.derivations[collection_id]
        except KeyError as exc:
            raise NotFound("raw collection") from exc
        return {"collection_id": collection_id, "derivation": derivation}

    def list_processing_claim_dispositions(
        self,
        claim_id: str,
        *,
        authority_sha256: str,
        start_ordinal: int,
    ) -> ArtifactDispositionPageDocument:
        assert start_ordinal == 0
        input_collection_id = self.disposition_inputs[claim_id]
        character = f"{input_collection_id % 16:x}"
        identity = ArtifactDispositionSetIdentity(
            disposition_count=1,
            output_edge_count=1,
            output_artifact_count=1,
            sha256=canonical_json_sha256({"input_collection_id": input_collection_id}),
        )
        assert authority_sha256 == identity.sha256
        return ArtifactDispositionPageDocument.model_validate(
            {
                "authority": identity.as_dict(),
                "start_ordinal": 0,
                "next_ordinal": None,
                "dispositions": [
                    ArtifactDisposition(
                        input_collection_id=input_collection_id,
                        input_archive_root_sha256=character * 64,
                        input_path="source/input.bin",
                        status="transformed",
                    ).as_dict()
                ],
            }
        )


def _lineage(
    recipe: RecipeRef,
    input_collection_id: int,
) -> dict[str, object]:
    controller_evidence = {"format": "fixture-controller-evidence/v1"}
    disposition_set = ArtifactDispositionSetIdentity(
        disposition_count=1,
        output_edge_count=1,
        output_artifact_count=1,
        sha256=canonical_json_sha256({"input_collection_id": input_collection_id}),
    )
    return CollectionDerivation(
        execution_id="d" * 64,
        claim_id=canonical_json_sha256({"claim_input": input_collection_id}),
        fence=1,
        recipe=recipe.to_identity(),
        operation=OperationIdentity("fixture.copy/v1", "c" * 64),
        input_set_sha256=canonical_json_sha256({"input_collection_id": input_collection_id}),
        artifact_set_sha256=canonical_json_sha256(
            {"input_collection_id": input_collection_id, "path": "source/input.bin"}
        ),
        output_tag_set_sha256=tag_set_identity(("fixture-output",)),
        execution_envelope_sha256="d" * 64,
        execution_sha256="e" * 64,
        controller_evidence=controller_evidence,
        controller_evidence_sha256=canonical_json_sha256(controller_evidence),
        disposition_set=disposition_set,
    ).as_dict()


def _lineage_scheduler(
    recipe: RecipeDefinition,
    derivations: dict[int, dict[str, object]],
) -> tuple[Stove0Scheduler, _LineageCoordinator, _LineageRiverhog]:
    coordinator = _LineageCoordinator()
    riverhog = _LineageRiverhog(derivations)
    return (
        Stove0Scheduler(
            riverhog=riverhog,  # type: ignore[arg-type]
            catalog=_LineageCatalog(recipe),  # type: ignore[arg-type]
            planner=_LineagePlanner(recipe),  # type: ignore[arg-type]
            coordinator=coordinator,  # type: ignore[arg-type]
            state=cast(object, None),  # type: ignore[arg-type]
        ),
        coordinator,
        riverhog,
    )


def test_derived_recipe_rejects_self_and_a_to_b_to_a_ancestry_cycles() -> None:
    recipe_a = _recipe("a")
    recipe_b = _recipe("b")
    derivations = {
        2: _lineage(recipe_a.ref, 1),
        3: _lineage(recipe_b.ref, 2),
    }

    immediate, immediate_coordinator, _ = _lineage_scheduler(recipe_a, derivations)
    ancestry, ancestry_coordinator, _ = _lineage_scheduler(recipe_a, derivations)

    assert immediate.reconcile_collection(2) == []
    assert immediate_coordinator.records == {}
    assert ancestry.reconcile_collection(3) == []
    assert ancestry_coordinator.records == {}


def test_derived_recipe_allows_deep_acyclic_lineage_and_repeated_events_converge() -> None:
    ancestors = [_recipe(f"stage-{index}") for index in range(96)]
    derivations = {
        collection_id: _lineage(ancestors[collection_id - 2].ref, collection_id - 1)
        for collection_id in range(2, 98)
    }
    candidate = _recipe("final")
    scheduler, coordinator, riverhog = _lineage_scheduler(candidate, derivations)

    first = scheduler.reconcile_collection(97)
    repeated = scheduler.reconcile_collection(97)

    assert first == repeated
    assert len(first) == 1
    assert set(coordinator.records) == set(first)
    assert riverhog.tag_stream_reads == [97, 97]
