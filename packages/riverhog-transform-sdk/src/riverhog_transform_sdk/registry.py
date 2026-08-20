"""Thread-safe process-local transform runtime registration and token refresh."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from riverhog_transform_sdk.runtime import CollectionTransformRuntime


class TransformRuntimeRegistry:
    """Route controller capability refreshes to an executing target runtime.

    The registry contains bearer material only in memory. A refresh arriving just
    before target startup is retained until the runtime binds; a refresh arriving
    during execution atomically swaps the runtime's API delegate.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runtimes: dict[str, CollectionTransformRuntime] = {}
        self._pending_tokens: dict[str, str] = {}

    @contextmanager
    def bind(
        self,
        job_id: str,
        runtime: CollectionTransformRuntime,
    ) -> Iterator[CollectionTransformRuntime]:
        normalized = _job_id(job_id)
        with self._lock:
            if normalized in self._runtimes:
                raise RuntimeError(f"transform runtime is already active: {normalized}")
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
            raise ValueError("transform capability token must be nonempty")
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
        raise ValueError("transform runtime job id must be canonical")
    return normalized


__all__ = ["TransformRuntimeRegistry"]
