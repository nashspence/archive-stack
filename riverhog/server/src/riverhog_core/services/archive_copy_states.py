from __future__ import annotations

from http_api_contracts import closed_literal_values
from riverhog_protocol import ArchiveCopyState

ARCHIVE_COPY_TRANSFER_STATES = frozenset({"requested", "waiting", "checking", "copying"})
ARCHIVE_COPY_BLOCKING_STATES = ARCHIVE_COPY_TRANSFER_STATES | {"canceling"}
ARCHIVE_COPY_STATES = closed_literal_values(ArchiveCopyState)

__all__ = [
    "ARCHIVE_COPY_BLOCKING_STATES",
    "ARCHIVE_COPY_STATES",
    "ARCHIVE_COPY_TRANSFER_STATES",
]
