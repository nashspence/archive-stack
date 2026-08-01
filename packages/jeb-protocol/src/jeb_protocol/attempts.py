"""Shared Jeb attempt lifecycle contracts."""

from __future__ import annotations

from collections.abc import Mapping

ATTEMPT_RESOLVED_STATES = frozenset({"target_succeeded", "cleanup_done", "superseded"})
ATTEMPT_SUCCESS_STATES = frozenset({"target_succeeded", "cleanup_done"})
ATTEMPT_WATCH_FAILURE_STATES = frozenset({"failed", "cleanup_failed", "superseded"})
ATTEMPT_WATCH_STOP_STATES = ATTEMPT_SUCCESS_STATES | ATTEMPT_WATCH_FAILURE_STATES


def attempt_state(payload: Mapping[str, object]) -> str:
    return str(payload.get("state") or "")


def attempt_watch_finished(payload: Mapping[str, object]) -> bool:
    return attempt_state(payload) in ATTEMPT_WATCH_STOP_STATES


def attempt_succeeded(payload: Mapping[str, object]) -> bool:
    return attempt_state(payload) in ATTEMPT_SUCCESS_STATES


__all__ = [
    "ATTEMPT_RESOLVED_STATES",
    "ATTEMPT_SUCCESS_STATES",
    "ATTEMPT_WATCH_FAILURE_STATES",
    "ATTEMPT_WATCH_STOP_STATES",
    "attempt_state",
    "attempt_succeeded",
    "attempt_watch_finished",
]
