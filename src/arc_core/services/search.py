from __future__ import annotations

from arc_core.domain.errors import NotYetImplemented


class StubSearchService:
    def search(self, query: str, limit: int) -> list[object]:
        raise NotYetImplemented("StubSearchService is not implemented yet")
