"""Riverhog-event wakeups and fair stove0 work advancement."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from datetime import timedelta
from typing import Literal, cast

from riverhog_api_client import ApiClient
from riverhog_protocol.collection_workflows import CollectionDerivation
from riverhog_protocol.errors import NotFound
from stove0_protocol import CollectionRootRef, RecipeRef
from time_formats import format_utc_timestamp, utc_now

from stove0_core.coordinator import Stove0Coordinator
from stove0_core.persistence import SqlAlchemyStateStore
from stove0_core.recipes import RecipeCatalog, RecipePlanner
from stove0_core.work_state import ConcurrentWorkUpdate

_COLLECTION_WAKE_TYPES = frozenset(
    {
        "io.riverhog.riverhog.collection.finalized",
        "io.riverhog.riverhog.collection.tags_changed",
    }
)
_TERMINAL_PHASES = frozenset({"complete", "inapplicable", "failed", "canceled"})
_PRUNE_INTERVAL_SECONDS = 60 * 60
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
        operational_state_retention_seconds: int = 30 * 24 * 60 * 60,
    ) -> None:
        if operational_state_retention_seconds < 1:
            raise ValueError("stove0 operational-state retention must be positive")
        self.riverhog = riverhog
        self.catalog = catalog
        self.planner = planner
        self.coordinator = coordinator
        self.state = state
        self.operational_state_retention_seconds = operational_state_retention_seconds
        self._prune_lock = threading.Lock()
        self._next_prune = 0.0

    def ingest_events(self, *, limit: int = 100) -> dict[str, object]:
        saved = self.state.load_cursor("riverhog-lifecycle/v1")
        cursor, revision = saved if saved is not None else ("0", None)
        page = self.riverhog.list_lifecycle_events(after=cursor, limit=limit)
        page.require_progress_after(cursor)
        created: list[str] = []
        failures: list[dict[str, str]] = []
        for event in page.events:
            if event.type not in _COLLECTION_WAKE_TYPES:
                continue
            try:
                collection_id = _collection_id(event.data, event.subject)
            except ValueError as exc:
                failures.append(
                    {
                        "event_id": event.id,
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                    }
                )
                continue
            created.extend(self.reconcile_collection(collection_id))
        if page.next_cursor != cursor:
            self._advance_cursor(
                "riverhog-lifecycle/v1",
                expected_revision=revision,
                cursor=page.next_cursor,
                require_exact=True,
            )
        return {
            "events": len(page.events),
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
            "work_ids": sorted(set(created)),
            "failures": failures,
        }

    def reconcile_collection(self, collection_id: int) -> list[str]:
        try:
            current = self.riverhog.get_collection(collection_id)
        except NotFound:
            return []
        root = CollectionRootRef(
            collection_id=collection_id,
            manifest_sha256=str(current.get("manifest_sha256") or ""),
            content_etag=str(current.get("content_etag") or ""),
        )
        tags = tuple(str(item) for item in current.get("tags", []))
        derivation = self._derivation(collection_id)
        created: list[str] = []
        for recipe in self.catalog.matching(tags):
            if derivation is not None:
                if not recipe.allow_derived_inputs:
                    continue
                if self._recipe_in_ancestry(
                    collection_id,
                    recipe.ref,
                    initial=derivation,
                ):
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
        if limit < 1 or limit > 100:
            raise ValueError("stove0 scheduler work limit must be between 1 and 100")
        phases = _phases_for_role(role)
        stream = f"stove0-work-scan/{role}/v1"
        saved = self.state.load_cursor(stream)
        cursor, revision = saved if saved is not None else ("", None)
        records, next_cursor = self.state.scan_work(
            phases=tuple(sorted(phases)),
            after_work_id=cursor,
            limit=limit,
        )
        progressed: list[str] = []
        failures: list[dict[str, str]] = []
        for record in records:
            if record.phase in _TERMINAL_PHASES or record.phase not in phases:
                continue
            try:
                updated = self.coordinator.step(record.work_id)
                if updated.revision > record.revision:
                    progressed.append(record.work_id)
            except ConcurrentWorkUpdate as exc:
                current = self.state.load(record.work_id)
                if current is not None and current.revision > record.revision:
                    # Another scheduler owns the same compare-and-swap
                    # transition. That is convergence, not a work failure.
                    continue
                failures.append(
                    {
                        "work_id": record.work_id,
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                    }
                )
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
        self._advance_cursor(
            stream,
            expected_revision=revision,
            cursor=next_cursor,
            require_exact=False,
        )
        return {
            "role": role,
            "cursor": cursor,
            "next_cursor": next_cursor,
            "progressed": progressed,
            "failures": failures,
        }

    def run_once(
        self,
        *,
        role: SchedulerRole = "combined",
        event_limit: int = 100,
        work_limit: int = 25,
    ) -> dict[str, object]:
        pruning = self._prune_operational_state() if role in {"controller", "combined"} else None
        events = (
            self.ingest_events(limit=event_limit)
            if role in {"controller", "combined"}
            else {"events": 0, "next_cursor": None, "has_more": False, "work_ids": []}
        )
        work = self.advance(role=role, limit=work_limit)
        return {"pruning": pruning, "events": events, "work": work}

    def _prune_operational_state(self) -> dict[str, int] | None:
        observed = time.monotonic()
        with self._prune_lock:
            if observed < self._next_prune:
                return None
            cutoff = format_utc_timestamp(
                utc_now() - timedelta(seconds=self.operational_state_retention_seconds)
            )
            result = self.state.prune_operational_state(cutoff=cutoff)
            self._next_prune = observed + _PRUNE_INTERVAL_SECONDS
            return result

    def _derivation(self, collection_id: int) -> CollectionDerivation | None:
        try:
            payload = self.riverhog.get_collection_derivation(collection_id)
        except NotFound:
            return None
        derivation = payload.get("derivation")
        if not isinstance(derivation, Mapping):
            raise RuntimeError("Riverhog returned invalid collection derivation evidence")
        try:
            return CollectionDerivation.from_mapping(derivation)
        except ValueError as exc:
            raise RuntimeError("Riverhog returned invalid collection derivation evidence") from exc

    def _recipe_in_ancestry(
        self,
        collection_id: int,
        recipe: RecipeRef,
        *,
        initial: CollectionDerivation,
    ) -> bool:
        pending = [collection_id]
        visited: set[int] = set()
        cached: dict[int, CollectionDerivation | None] = {collection_id: initial}
        expected = recipe.to_identity()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            derivation = cached.get(current)
            if current not in cached:
                derivation = self._derivation(current)
                cached[current] = derivation
            if derivation is None:
                continue
            if derivation.recipe == expected:
                return True
            for item in derivation.inputs:
                input_id = item.collection_id
                if input_id not in visited:
                    pending.append(input_id)
        return False

    def _advance_cursor(
        self,
        stream: str,
        *,
        expected_revision: int | None,
        cursor: str,
        require_exact: bool,
    ) -> None:
        try:
            self.state.compare_and_swap_cursor(
                stream,
                expected_revision=expected_revision,
                cursor=cursor,
            )
        except ConcurrentWorkUpdate:
            current = self.state.load_cursor(stream)
            if current is not None and current[0] == cursor:
                return
            if require_exact:
                raise


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
