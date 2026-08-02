"""Munchy's application-owned durable SQLite state."""

from state_schema import StateStatus

from munchy_core.persistence.schema import state_schema, upgrade_state, validate_state

__all__ = ["initialize_persistence", "state_schema", "validate_persistence"]


def initialize_persistence() -> StateStatus:
    """Explicitly create or forward-migrate Munchy state."""

    return upgrade_state()


def validate_persistence() -> StateStatus:
    """Validate Munchy state without applying schema changes."""

    return validate_state()
