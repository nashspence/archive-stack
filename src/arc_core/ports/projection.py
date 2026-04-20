from __future__ import annotations

from typing import Protocol

from arc_core.domain.models import Target


class ProjectionStore(Protocol):
    def reconcile_from_pins(self) -> None: ...
    def ensure_target_visible(self, target: Target) -> None: ...
