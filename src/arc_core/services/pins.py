from __future__ import annotations

from arc_core.domain.errors import NotYetImplemented


class StubPinService:
    def pin(self, raw_target: str) -> object:
        raise NotYetImplemented("StubPinService is not implemented yet")

    def release(self, raw_target: str) -> object:
        raise NotYetImplemented("StubPinService is not implemented yet")

    def list_pins(self) -> list[object]:
        raise NotYetImplemented("StubPinService is not implemented yet")
