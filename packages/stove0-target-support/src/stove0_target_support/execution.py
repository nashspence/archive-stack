"""Process-local target execution control that never becomes durable authority."""

from __future__ import annotations

import threading

from riverhog_transform_sdk import ClaimedCollectionRuntimeRegistry
from stove0_target_protocol import TargetJobRequest, TargetJobStatus


class TargetExecutionSession:
    """Bind refreshable runtime authority and finalized success to one attempt.

    The session is target-process operational state. It retains no request or
    bearer token itself; capability material remains in the SDK's in-memory
    registry, while only a fully validated successful status may be retained as
    the publication witness for the duration of the attempt.
    """

    def __init__(
        self,
        request: TargetJobRequest,
        attempt: int,
        runtime_registry: ClaimedCollectionRuntimeRegistry,
    ) -> None:
        self.job_id = request.declaration.job_id
        self.request_sha256 = request.request_sha256
        self.plan_sha256 = request.declaration.plan.plan_sha256
        self.attempt = attempt
        self.runtime_registry = runtime_registry
        self._lock = threading.RLock()
        self._completed_status: TargetJobStatus | None = None

    def record_completed(self, status: TargetJobStatus) -> None:
        """Retain one exact, validated successful target result."""

        if (
            status.state != "succeeded"
            or status.job_id != self.job_id
            or status.request_sha256 != self.request_sha256
            or status.plan_sha256 != self.plan_sha256
            or status.attempt != self.attempt
        ):
            raise ValueError("completed target status differs from the active attempt")
        with self._lock:
            if self._completed_status is not None and self._completed_status != status:
                raise RuntimeError("target attempt produced two different terminal outcomes")
            self._completed_status = status

    @property
    def completed_status(self) -> TargetJobStatus | None:
        with self._lock:
            return self._completed_status


__all__ = ["TargetExecutionSession"]
