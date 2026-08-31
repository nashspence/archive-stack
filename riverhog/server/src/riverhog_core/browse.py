"""Bounded keyset traversal helpers for mutable Riverhog projections."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from http_api_contracts.browse import BrowseScalar
from riverhog_protocol.errors import BadRequest
from sqlalchemy import and_, asc, desc, or_
from sqlalchemy.sql.selectable import Select


def validate_page_size(page_size: int) -> None:
    if page_size < 1 or page_size > 100:
        raise BadRequest("page_size must be between 1 and 100")


def keyset_statement(
    statement: Select[Any],
    *,
    columns: Sequence[Any],
    position: Sequence[BrowseScalar] | None,
    order: str,
    page_size: int,
) -> Select[Any]:
    """Apply one stable lexicographic keyset boundary and bounded look-ahead."""

    validate_page_size(page_size)
    if order not in {"asc", "desc"}:
        raise BadRequest("order must be asc or desc")
    if not columns:
        raise ValueError("keyset traversal requires at least one ordering column")
    if position is not None:
        if len(position) != len(columns):
            raise BadRequest("page token position differs from the operation ordering")
        comparisons = []
        for index, (column, value) in enumerate(zip(columns, position, strict=True)):
            preceding = [
                (earlier.is_(None) if earlier_value is None else earlier == earlier_value)
                for earlier, earlier_value in zip(columns[:index], position[:index], strict=True)
            ]
            if value is None:
                if order == "desc":
                    comparisons.append(and_(*preceding, column.is_not(None)))
            else:
                comparison = (
                    or_(column > value, column.is_(None)) if order == "asc" else column < value
                )
                comparisons.append(and_(*preceding, comparison))
        if comparisons:
            statement = statement.where(or_(*comparisons))
    ordering = (
        tuple(asc(column).nulls_last() for column in columns)
        if order == "asc"
        else tuple(desc(column).nulls_first() for column in columns)
    )
    return statement.order_by(*ordering).limit(page_size + 1)


def bounded_page[T](
    rows: Sequence[T],
    *,
    page_size: int,
    position_of: Callable[[T], Sequence[BrowseScalar]],
) -> tuple[list[T], tuple[BrowseScalar, ...] | None]:
    """Discard bounded look-ahead and return the next exact keyset position."""

    validate_page_size(page_size)
    page = list(rows[:page_size])
    next_position = tuple(position_of(page[-1])) if len(rows) > page_size and page else None
    return page, next_position


__all__ = ["bounded_page", "keyset_statement", "validate_page_size"]
