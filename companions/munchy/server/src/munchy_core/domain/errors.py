from __future__ import annotations

from typing import Any


class ServiceError(RuntimeError):
    def __init__(self, *, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class InsufficientStorage(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        label: str,
        required_bytes: int,
        free_bytes: int,
        reserved_bytes: int = 0,
    ) -> None:
        super().__init__(message)
        self.label = label
        self.required_bytes = required_bytes
        self.free_bytes = free_bytes
        self.reserved_bytes = reserved_bytes


class EncodingFailed(RuntimeError):
    pass


class RoutingFailed(RuntimeError):
    pass


class HandoffFailed(RuntimeError):
    pass


class JobCanceled(RuntimeError):
    pass
