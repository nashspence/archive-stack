"""Munchy's durable SQLite-owned state."""

from munchy_core.persistence import lifecycle_events, sqlite_state


def initialize_persistence() -> None:
    sqlite_state.init_state_store()
    lifecycle_events.initialize_lifecycle_event_store()
