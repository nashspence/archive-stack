from __future__ import annotations

from arc_core.domain.errors import NotYetImplemented


class StubCopyService:
    def register(self, image_id: str, copy_id: str, location: str) -> object:
        raise NotYetImplemented("StubCopyService is not implemented yet")
