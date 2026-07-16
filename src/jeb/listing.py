from __future__ import annotations

import argparse
from collections.abc import Collection

MAX_LIST_PAGE_SIZE = 100


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def page_size(value: str) -> int:
    parsed = positive_int(value)
    if parsed > MAX_LIST_PAGE_SIZE:
        raise argparse.ArgumentTypeError(f"must be <= {MAX_LIST_PAGE_SIZE}")
    return parsed


def add_list_query_arguments(
    parser: argparse.ArgumentParser,
    *,
    sort_fields: Collection[str],
    default_sort: str,
    default_order: str,
    query_help: str,
) -> None:
    parser.add_argument("--page", type=positive_int, default=1, help="Page number.")
    parser.add_argument(
        "--per-page",
        type=page_size,
        default=25,
        help=f"Rows per page, up to {MAX_LIST_PAGE_SIZE}.",
    )
    parser.add_argument(
        "--sort",
        choices=sorted(sort_fields),
        default=default_sort,
        help="Sort field.",
    )
    parser.add_argument(
        "--order",
        choices=("asc", "desc"),
        default=default_order,
        help="Sort order.",
    )
    parser.add_argument("--query", "--search", "-q", help=query_help)


def add_list_output_arguments(
    parser: argparse.ArgumentParser,
    *,
    noun: str,
) -> None:
    parser.add_argument(
        "--all",
        action="store_true",
        help=f"Return every matching {noun}.",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--ids",
        action="store_true",
        help=f"Emit one {noun} id per line.",
    )
    output.add_argument("--json", action="store_true", help="Emit JSON.")
