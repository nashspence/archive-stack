"""Port for Munchy's optional GPU manager and execution target."""

from __future__ import annotations

from typing import Any, Protocol


class GpuPlatform(Protocol):
    def manager_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def target_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
