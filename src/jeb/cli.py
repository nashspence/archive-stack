from __future__ import annotations

import argparse
import sys

import httpx

from jeb.collector import ATTEMPT_LIST_SORT_FIELDS
from jeb.service_cli import (
    MAX_PER_PAGE,
    per_page_value,
    positive_int,
)
from riverhog_cli.client import ApiClient
from riverhog_cli.output import (
    emit,
    format_jeb_archive_plan,
    format_jeb_attempts,
    format_jeb_config_check,
    format_jeb_operation,
    format_jeb_status,
)
from riverhog_core.domain.errors import RiverhogError


def client() -> ApiClient:
    return ApiClient()


def cmd_status(args: argparse.Namespace) -> int:
    payload = client().get_jeb_status(include_backlog=not args.no_backlog)
    emit(payload if args.json else format_jeb_status(payload), json_mode=args.json)
    return 0


def cmd_attempts(args: argparse.Namespace) -> int:
    payload = client().list_jeb_attempts(
        page=args.page,
        per_page=args.per_page,
        sort=args.sort,
        order=args.order,
        terminal=args.terminal,
        state=args.state,
        account=args.account,
        collection_slug=args.collection_slug,
        target=args.target,
        query=args.query,
    )
    emit(payload if args.json else format_jeb_attempts(payload), json_mode=args.json)
    return 0


def cmd_check_config(args: argparse.Namespace) -> int:
    payload = client().check_jeb_config()
    emit(payload if args.json else format_jeb_config_check(payload), json_mode=args.json)
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    payload = client().run_jeb_once()
    if args.json:
        emit(payload, json_mode=True)
        return 0
    emit(format_jeb_operation(payload, title="jeb scheduler pass"), json_mode=False)
    return 0


def cmd_archive_now(args: argparse.Namespace) -> int:
    payload = client().archive_jeb_now(
        account=args.account,
        process=not args.no_process,
        dry_run=args.dry_run,
    )
    if args.json:
        emit(payload, json_mode=True)
        return 0
    if args.dry_run:
        emit(format_jeb_archive_plan(payload), json_mode=False)
        return 0
    if payload.get("status") == "no_eligible_files":
        emit(format_jeb_operation(payload, title="jeb archive"), json_mode=False)
        return 1
    emit(format_jeb_operation(payload, title="jeb archive"), json_mode=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jeb",
        description="Remote Jeb operator CLI.",
        epilog=(
            "commands:\n"
            "  status        show read-only collector status\n"
            "  attempts      list processing attempts\n"
            "  check-config  validate deployed Jeb configuration\n"
            "  once          request one scheduler pass\n"
            "  archive-now   archive one account immediately"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="show read-only collector status")
    status.add_argument("--json", action="store_true", help="Emit JSON.")
    status.add_argument(
        "--no-backlog",
        action="store_true",
        help="Skip source directory eligible-file scans on the Jeb service.",
    )
    status.set_defaults(func=cmd_status)

    attempts = sub.add_parser("attempts", help="list processing attempts")
    attempts.add_argument("--page", type=positive_int, default=1, help="Page number.")
    attempts.add_argument(
        "--per-page",
        type=per_page_value,
        default=25,
        help=f"Rows per page, up to {MAX_PER_PAGE}.",
    )
    attempts.add_argument(
        "--sort",
        choices=sorted(ATTEMPT_LIST_SORT_FIELDS),
        default="updated_at",
        help="Sort field.",
    )
    attempts.add_argument(
        "--order",
        choices=("asc", "desc"),
        default="desc",
        help="Sort order.",
    )
    attempts.add_argument(
        "--terminal",
        choices=("active", "terminal", "all"),
        default="active",
        help="Show active, terminal, or all attempts.",
    )
    attempts.add_argument("--state", help="Filter by attempt state.")
    attempts.add_argument("--account", help="Filter by account slug.")
    attempts.add_argument("--collection-slug", help="Filter by output collection slug.")
    attempts.add_argument("--target", help="Filter by target name.")
    attempts.add_argument(
        "--query",
        "-q",
        help="Search attempt, batch, job, collection, target, state, timestamp, or error.",
    )
    attempts.add_argument("--json", action="store_true", help="Emit JSON.")
    attempts.set_defaults(func=cmd_attempts)

    check_config = sub.add_parser("check-config", help="validate deployed Jeb configuration")
    check_config.add_argument("--json", action="store_true", help="Emit JSON.")
    check_config.set_defaults(func=cmd_check_config)

    once = sub.add_parser("once", help="request one scheduler pass")
    once.add_argument("--json", action="store_true", help="Emit JSON.")
    once.set_defaults(func=cmd_once)

    archive_now = sub.add_parser("archive-now", help="archive one account immediately")
    archive_now.add_argument("--account", required=True, help="Account slug to archive.")
    archive_now.add_argument(
        "--no-process",
        action="store_true",
        help="Create an eligible batch but do not process it immediately.",
    )
    archive_now.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the archive action without creating a batch or uploading files.",
    )
    archive_now.add_argument("--json", action="store_true", help="Emit JSON.")
    archive_now.set_defaults(func=cmd_archive_now)
    return parser


def _error_message(exc: BaseException) -> str:
    if isinstance(exc, httpx.TransportError):
        return f"transport error: {exc}"
    return str(exc) or type(exc).__name__


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (RiverhogError, httpx.TransportError) as exc:
        print(f"jeb: {_error_message(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
