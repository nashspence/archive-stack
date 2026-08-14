"""Canonical Jeb source identity contract."""

from __future__ import annotations

import re

SOURCE_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
SOURCE_ID = re.compile(SOURCE_ID_PATTERN, re.ASCII)


class SourceIdError(ValueError):
    """Raised when a Jeb source identity is not canonical."""


def source_id(value: str) -> str:
    """Return an exact canonical Jeb source identity."""

    if not isinstance(value, str) or SOURCE_ID.fullmatch(value) is None:
        raise SourceIdError(
            "source must be a 1-63 character lowercase ASCII slug containing only "
            "letters, digits, and interior hyphens"
        )
    return value


__all__ = ["SOURCE_ID_PATTERN", "SourceIdError", "source_id"]
