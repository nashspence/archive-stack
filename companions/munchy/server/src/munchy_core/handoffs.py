from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol


class HandoffAdapter(Protocol):
    name: str
    enabled: bool
    supports_eager: bool
    eager_interval_seconds: float

    def start(self) -> None: ...

    def stop(self) -> None: ...

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

    def expected_primary_files_total(
        self,
        input_upload: dict[str, Any],
        groups: dict[str, dict[str, Any]],
        routing: Mapping[str, Any] | None,
    ) -> int | None: ...

    def handed_off_paths(self, job: dict[str, Any]) -> set[str]: ...

    def artifact_record(self, job: dict[str, Any], path: str) -> dict[str, Any] | None: ...

    def artifact_complete(self, record: dict[str, Any]) -> bool: ...
