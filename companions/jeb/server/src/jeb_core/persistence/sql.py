"""Shared SQLite query helpers for Jeb persistence modules."""


def like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
