"""Connected deployment configuration shared by maintained target services."""

from __future__ import annotations

import os
from collections.abc import Mapping

from stove0_target_support.persistent import DEFAULT_TERMINAL_STATE_RETENTION_SECONDS

TARGET_TERMINAL_STATE_RETENTION_ENV = "STOVE0_TARGET_TERMINAL_STATE_RETENTION_SECONDS"


def terminal_state_retention_seconds(
    environ: Mapping[str, str] | None = None,
) -> int:
    values = os.environ if environ is None else environ
    raw = values.get(
        TARGET_TERMINAL_STATE_RETENTION_ENV,
        str(DEFAULT_TERMINAL_STATE_RETENTION_SECONDS),
    )
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{TARGET_TERMINAL_STATE_RETENTION_ENV} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{TARGET_TERMINAL_STATE_RETENTION_ENV} must be positive")
    return value


__all__ = [
    "TARGET_TERMINAL_STATE_RETENTION_ENV",
    "terminal_state_retention_seconds",
]
