from __future__ import annotations

from arc_core.domain.errors import NotYetImplemented


class StubFetchService:
    def get(self, fetch_id: str) -> object:
        raise NotYetImplemented("StubFetchService is not implemented yet")

    def manifest(self, fetch_id: str) -> object:
        raise NotYetImplemented("StubFetchService is not implemented yet")

    def create_or_resume_upload(self, fetch_id: str, entry_id: str) -> object:
        raise NotYetImplemented("StubFetchService is not implemented yet")

    def append_upload_chunk(
        self, fetch_id: str, entry_id: str, offset: int, checksum: str, content: bytes
    ) -> object:
        raise NotYetImplemented("StubFetchService is not implemented yet")

    def complete(self, fetch_id: str) -> object:
        raise NotYetImplemented("StubFetchService is not implemented yet")
