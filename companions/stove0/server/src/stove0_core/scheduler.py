"""Riverhog-event wakeups and fair stove0 work advancement."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from riverhog_api_client import ApiClient
from riverhog_protocol.errors import NotFound
from stove0_protocol import CollectionRootRef

from stove0_core.coordinator import Stove0Coordinator
from stove0_core.persistence import SqlAlchemyStateStore
from stove0_core.recipes import RecipeCatalog, RecipePlanner
from stove0_core.work_state import WorkRecord

_COLLECTION_WAKE_TYPES = frozenset(
    {
        "io.riverhog.riverhog.collection.finalized",
        "io.riverhog.riverhog.collection.tags_changed",
    }
)
_TERMINAL_PHASES = frozenset({"complete", "inapplicable", "failed", "canceled"})
SchedulerRole = Literal["controller", "worker", "combined"]
_CONTROLLER_PHASES = frozenset(
    {
        "eligible",
        "claimed",
        "planning",
        "coordinating",
        "verifying",
        "settled",
        "retirement_pending",
        "abandon_pending",
    }
)
_WORKER_PHASES = frozenset(
    {
        "observing",
        "target_preflight",
        "queued",
        "executing",
        "output_finalizing",
    }
)


class Stove0Scheduler:
    """Treat lifecycle events as wakeups and catalog state as current truth."""

    def __init__(
        self,
        *,
        riverhog: ApiClient,
        catalog: RecipeCatalog,
        planner: RecipePlanner,
        coordinator: Stove0Coordinator,
        state: SqlAlchemyStateStore,
    ) -> None:
        self.riverhog = riverhog
        self.catalog = catalog
        self.planner = planner
        self.coordinator = coordinator
        self.state = state

    def ingest_events(self, *, limit: int = 100) -> dict[str, object]:
        saved = self.state.load_cursor("riverhog-lifecycle/v1")
        cursor, revision = saved if saved is not None else ("0", None)
        page = self.riverhog.list_lifecycle_events(after=cursor, limit=limit)
        page.require_progress_after(cursor)
        created: list[str] = []
        for event in page.events:
            if event.type not in _COLLECTION_WAKE_TYPES:
                continue
            collection_id = _collection_id(event.data, event.subject)
            created.extend(self.reconcile_collection(collection_id))
        if page.next_cursor != cursor:
            self.state.compare_and_swap_cursor(
                "riverhog-lifecycle/v1",
                expected_revision=revision,
                cursor=page.next_cursor,
            )
        return {
            "events": len(page.events),
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
            "work_ids": sorted(set(created)),
        }

    def reconcile_collection(self, collection_id: int) -> list[str]:
        current = self.riverhog.get_collection(collection_id)
        root = CollectionRootRef(
            collection_id=collection_id,
            manifest_sha256=str(current.get("manifest_sha256") or ""),
            content_etag=str(current.get("content_etag") or ""),
        )
        tags = tuple(str(item) for item in current.get("tags", []))
        derived = self._is_derived(collection_id)
        created: list[str] = []
        for recipe in self.catalog.matching(tags):
            if derived and not recipe.allow_derived_inputs:
                continue
            identity = self.planner.create_work(recipe.id, (root,), revision=recipe.revision)
            record = self.coordinator.create_or_resume(identity)
            created.append(record.work_id)
        return created

    def advance(
        self,
        *,
        role: SchedulerRole = "combined",
        limit: int = 25,
    ) -> dict[str, object]:
        phases = _phases_for_role(role)
        page = self.state.list_work(
            all_items=True,
            sort="updated_at",
            order="asc",
        )
        raw_records = page.get("work")
        if not isinstance(raw_records, list):
            raise RuntimeError("stove0 work store returned an invalid page")
        records = [WorkRecord.model_validate(item) for item in raw_records]
        progressed: list[str] = []
        failures: list[dict[str, str]] = []
        for record in records:
            if len(progressed) >= limit:
                break
            if record.phase in _TERMINAL_PHASES or record.phase not in phases:
                continue
            try:
                self.coordinator.step(record.work_id)
                progressed.append(record.work_id)
            except Exception as exc:
                # A failed item never prevents later work from advancing. The
                # original record remains retryable and exact; operator-visible
                # diagnostics identify the failed step without inventing payload
                # custody or silently changing the state machine.
                failures.append(
                    {
                        "work_id": record.work_id,
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                    }
                )
        return {"role": role, "progressed": progressed, "failures": failures}

    def run_once(
        self,
        *,
        role: SchedulerRole = "combined",
        event_limit: int = 100,
        work_limit: int = 25,
    ) -> dict[str, object]:
        events = (
            self.ingest_events(limit=event_limit)
            if role in {"controller", "combined"}
            else {"events": 0, "next_cursor": None, "has_more": False, "work_ids": []}
        )
        work = self.advance(role=role, limit=work_limit)
        return {"events": events, "work": work}

    def _is_derived(self, collection_id: int) -> bool:
        try:
            self.riverhog.get_collection_derivation(collection_id)
        except NotFound:
            return False
        return True


def _collection_id(data: Mapping[str, object], subject: str | None) -> int:
    raw = data.get("collection_id", subject)
    if isinstance(raw, bool):
        raise ValueError("Riverhog event collection identity is invalid")
    try:
        value = int(str(raw))
    except ValueError as exc:
        raise ValueError("Riverhog event collection identity is invalid") from exc
    if value < 1:
        raise ValueError("Riverhog event collection identity is invalid")
    return value


def scheduler_role(value: str) -> SchedulerRole:
    normalized = value.strip().casefold()
    if normalized not in {"controller", "worker", "combined"}:
        raise ValueError("stove0 scheduler role must be controller, worker, or combined")
    return cast(SchedulerRole, normalized)


def _phases_for_role(role: SchedulerRole) -> frozenset[str]:
    if role == "controller":
        return _CONTROLLER_PHASES
    if role == "worker":
        return _WORKER_PHASES
    if role == "combined":
        return _CONTROLLER_PHASES | _WORKER_PHASES
    raise ValueError(f"unsupported stove0 scheduler role: {role}")


__all__ = ["SchedulerRole", "Stove0Scheduler", "scheduler_role"]
