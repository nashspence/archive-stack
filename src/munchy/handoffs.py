from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol


class HandoffAdapter(Protocol):
    name: str
    supports_eager: bool

    def advance(
        self,
        job: dict[str, Any],
        source_dir: Path,
        *,
        final: bool,
        source_label: str,
        context: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None: ...

    def cancel(self, job: dict[str, Any], *, reason: str) -> None: ...

    def refresh(self, job: dict[str, Any]) -> None: ...

    def progress(self, job: dict[str, Any]) -> dict[str, Any] | None: ...

    def safe_to_delete(self, job: dict[str, Any]) -> bool: ...

    def eager_ready(self, job: dict[str, Any]) -> bool: ...

    def wait_until_idle(self, job: dict[str, Any]) -> None: ...

    def can_resume(self, job: dict[str, Any]) -> bool: ...

    def merge_state(
        self,
        current: dict[str, Any],
        incoming: dict[str, Any],
    ) -> dict[str, Any]: ...
