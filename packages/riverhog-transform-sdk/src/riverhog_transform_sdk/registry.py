"""Thread-safe process-local claimed-collection runtime registration."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol


class RefreshableClaimedCollectionRuntime(Protocol):
    def refresh_capability(self, capability_token: str) -> None: ...

    def close(self, *, raise_errors: bool = True) -> None: ...


class ClaimedCollectionRuntimeRegistry:
    """Route capability refreshes to one executing claimed-collection runtime.

    The registry contains bearer material only in memory. A refresh arriving just
    before target startup is retained until the runtime binds; a refresh arriving
    during execution atomically swaps the runtime's API delegate.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runtimes: dict[str, RefreshableClaimedCollectionRuntime] = {}
        self._pending_tokens: dict[str, str] = {}

    @contextmanager
    def bind(
        self,
        job_id: str,
        runtime: RefreshableClaimedCollectionRuntime,
    ) -> Iterator[RefreshableClaimedCollectionRuntime]:
        normalized = _job_id(job_id)
        with self._lock:
            if normalized in self._runtimes:
                raise RuntimeError(f"claimed collection runtime is already active: {normalized}")
            self._runtimes[normalized] = runtime
            pending = self._pending_tokens.pop(normalized, None)
            try:
                if pending is not None:
                    runtime.refresh_capability(pending)
            except Exception:
                self._runtimes.pop(normalized, None)
                raise
        try:
            yield runtime
        finally:
            with self._lock:
                current = self._runtimes.get(normalized)
                if current is runtime:
                    self._runtimes.pop(normalized, None)

    def refresh(self, job_id: str, capability_token: str) -> None:
        normalized = _job_id(job_id)
        token = capability_token.strip()
        if not token:
            raise ValueError("claimed collection capability token must be nonempty")
        with self._lock:
            runtime = self._runtimes.get(normalized)
            if runtime is None:
                self._pending_tokens[normalized] = token
                return
        runtime.refresh_capability(token)

    def discard(self, job_id: str) -> None:
        normalized = _job_id(job_id)
        with self._lock:
            self._pending_tokens.pop(normalized, None)
            runtime = self._runtimes.pop(normalized, None)
        if runtime is not None:
            runtime.close()


def _job_id(value: str) -> str:
    normalized = value.strip()
    if not normalized or normalized != value:
        raise ValueError("claimed collection runtime job id must be canonical")
    return normalized


__all__ = ["ClaimedCollectionRuntimeRegistry", "RefreshableClaimedCollectionRuntime"]
