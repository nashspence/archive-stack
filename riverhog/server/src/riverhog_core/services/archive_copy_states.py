from __future__ import annotations

ARCHIVE_COPY_TRANSFER_STATES = frozenset({"requested", "waiting", "checking", "copying"})
ARCHIVE_COPY_BLOCKING_STATES = ARCHIVE_COPY_TRANSFER_STATES | {"canceling"}
ARCHIVE_COPY_STATES = ARCHIVE_COPY_BLOCKING_STATES | {"completed", "failed", "canceled"}

__all__ = [
    "ARCHIVE_COPY_BLOCKING_STATES",
    "ARCHIVE_COPY_STATES",
    "ARCHIVE_COPY_TRANSFER_STATES",
]
