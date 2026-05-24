from __future__ import annotations

import os
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock


@dataclass(slots=True)
class _EventStats:
    count: int = 0
    total: float = 0.0
    max: float = 0.0

    def add(self, seconds: float) -> None:
        self.count += 1
        self.total += seconds
        self.max = max(self.max, seconds)


class _TimingProfile:
    def __init__(self) -> None:
        self._lock = Lock()
        self.enabled = os.getenv("RIVERHOG_TEST_PROFILE") == "1"
        self._events: dict[str, _EventStats] = defaultdict(_EventStats)
        self._scenario_phases: dict[str, dict[str, float]] = defaultdict(
            lambda: {"setup": 0.0, "call": 0.0, "teardown": 0.0}
        )

    def record_event(self, label: str, seconds: float) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._events[label].add(seconds)

    def record_test_phase(self, nodeid: str, when: str, seconds: float) -> None:
        if not self.enabled or when not in {"setup", "call", "teardown"}:
            return
        with self._lock:
            self._scenario_phases[nodeid][when] += seconds

    def render(self) -> str:
        if not self.enabled:
            return ""

        lines: list[str] = []
        if self._events:
            lines.append("Fixture and helper hotspots:")
            for label, stats in sorted(
                self._events.items(),
                key=lambda item: item[1].total,
                reverse=True,
            )[:20]:
                lines.append(
                    f"  {stats.total:7.2f}s total  {stats.count:4d}x  "
                    f"{stats.max:6.2f}s max  {label}"
                )

        if self._scenario_phases:
            if lines:
                lines.append("")
            lines.append("Slowest scenarios:")
            scenario_rows = []
            for nodeid, phases in self._scenario_phases.items():
                total = phases["setup"] + phases["call"] + phases["teardown"]
                scenario_rows.append((total, phases, nodeid))
            for total, phases, nodeid in sorted(scenario_rows, reverse=True)[:20]:
                lines.append(
                    "  "
                    f"{total:7.2f}s total  "
                    f"setup {phases['setup']:6.2f}s  "
                    f"call {phases['call']:6.2f}s  "
                    f"teardown {phases['teardown']:6.2f}s  "
                    f"{nodeid}"
                )

        return "\n".join(lines)


PROFILE = _TimingProfile()


@contextmanager
def time_block(label: str) -> Iterator[None]:
    if not PROFILE.enabled:
        yield
        return

    started_at = time.perf_counter()
    try:
        yield
    finally:
        PROFILE.record_event(label, time.perf_counter() - started_at)
