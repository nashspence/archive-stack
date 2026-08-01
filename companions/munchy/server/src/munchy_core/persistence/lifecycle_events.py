from __future__ import annotations

from lifecycle_events import (
    SQLiteEventCursorStore,
    SQLiteLifecycleEventLog,
)

import munchy_core.persistence.sqlite_state as state_store


def lifecycle_event_log() -> SQLiteLifecycleEventLog:
    return SQLiteLifecycleEventLog(state_store.state_db)


def lifecycle_event_cursors() -> SQLiteEventCursorStore:
    return SQLiteEventCursorStore(state_store.state_db)


def initialize_lifecycle_event_store() -> None:
    lifecycle_event_log().initialize()
    lifecycle_event_cursors().initialize()
