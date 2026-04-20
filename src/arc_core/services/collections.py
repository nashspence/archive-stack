from __future__ import annotations

from arc_core.domain.errors import NotYetImplemented


class StubCollectionService:
    def close(self, staging_path: str) -> object:
        raise NotYetImplemented("StubCollectionService is not implemented yet")

    def get(self, collection_id: str) -> object:
        raise NotYetImplemented("StubCollectionService is not implemented yet")
