"""Fair advancement of explicitly initiated Stove0 work."""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from typing import Literal, Protocol, cast

from stove0_operator_contracts import AdmissionRun
from time_formats import format_utc_timestamp, utc_now

from stove0_core.coordinator import Stove0Coordinator
from stove0_core.persistence import SqlAlchemyStateStore
from stove0_core.work_state import ConcurrentWorkUpdate

_TERMINAL_PHASES = frozenset({"complete", "inapplicable", "failed", "canceled"})
_PRUNE_INTERVAL_SECONDS = 60 * 60
SchedulerRole = Literal["controller", "worker", "combined"]


class ProductionSealProcessor(Protocol):
    def process_due_production_seals(self, *, limit: int = 1) -> int: ...


class AdmissionProcessor(Protocol):
    def advance(self, *, limit: int = 25) -> AdmissionRun: ...


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
    """Advance work admitted through an explicit or configured preview acceptance."""

    def __init__(
        self,
        *,
        coordinator: Stove0Coordinator,
        state: SqlAlchemyStateStore,
        production_seals: ProductionSealProcessor | None = None,
        admission: AdmissionProcessor | None = None,
        operational_state_retention_seconds: int = 30 * 24 * 60 * 60,
    ) -> None:
        if operational_state_retention_seconds < 1:
            raise ValueError("stove0 operational-state retention must be positive")
        self.coordinator = coordinator
        self.state = state
        self.production_seals = production_seals
        self.admission = admission
        self.operational_state_retention_seconds = operational_state_retention_seconds
        self._prune_lock = threading.Lock()
        self._next_prune = 0.0

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
        work_limit: int = 25,
    ) -> dict[str, object]:
        pruning = self._prune_operational_state() if role in {"controller", "combined"} else None
        if role in {"worker", "combined"} and self.production_seals is not None:
            self.production_seals.process_due_production_seals(limit=work_limit)
        admission = (
            self.admission.advance(limit=work_limit)
            if role in {"controller", "combined"} and self.admission is not None
            else None
        )
        work = self.advance(role=role, limit=work_limit)
        return {
            "pruning": pruning,
            "admission": (None if admission is None else admission.model_dump(mode="python")),
            "work": work,
        }

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
