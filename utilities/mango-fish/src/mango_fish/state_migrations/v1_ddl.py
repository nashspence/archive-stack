"""Immutable DDL snapshot for the Mango Fish cursor state v1 baseline."""

# ruff: noqa: E501

# This module is migration authority. Runtime model metadata must never be imported here.

SQLITE_DDL: tuple[str, ...] = (
    """
CREATE TABLE source_cursors (
	source TEXT NOT NULL,
	cursor TEXT NOT NULL,
	PRIMARY KEY (source)
)
    """.strip(),
)
