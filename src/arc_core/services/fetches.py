from __future__ import annotations

from arc_core.domain.errors import NotYetImplemented


class StubFetchService:
    def get(self, fetch_id: str) -> object:
        raise NotYetImplemented("StubFetchService is not implemented yet")

    def manifest(self, fetch_id: str) -> object:
        raise NotYetImplemented("StubFetchService is not implemented yet")

    def upload_entry(self, fetch_id: str, entry_id: str, sha256: str, content: bytes) -> object:
        raise NotYetImplemented("StubFetchService is not implemented yet")

    def complete(self, fetch_id: str) -> object:
        raise NotYetImplemented("StubFetchService is not implemented yet")
