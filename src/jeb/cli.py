from __future__ import annotations

import argparse
import json
import sys

import httpx

from jeb.collector import BATCH_LIST_SORT_FIELDS
from jeb.service_cli import (
    MAX_PER_PAGE,
    format_batches,
    format_status,
    per_page_value,
    positive_int,
)
from riverhog_cli.client import ApiClient
from riverhog_core.domain.errors import RiverhogError


def client() -> ApiClient:
    return ApiClient()


def emit(payload: object, *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    print(str(payload))


def cmd_status(args: argparse.Namespace) -> int:
    payload = client().get_jeb_status(include_backlog=not args.no_backlog)
    emit(payload if args.json else format_status(payload), json_mode=args.json)
    return 0


def cmd_batches(args: argparse.Namespace) -> int:
    payload = client().list_jeb_batches(
        page=args.page,
        per_page=args.per_page,
        sort=args.sort,
        order=args.order,
        terminal=args.terminal,
        state=args.state,
        account=args.account,
        collection=args.collection,
        target=args.target,
        query=args.query,
    )
    emit(payload if args.json else format_batches(payload), json_mode=args.json)
    return 0


def cmd_check_config(args: argparse.Namespace) -> int:
    payload = client().check_jeb_config()
    if args.json:
        emit(payload, json_mode=True)
        return 0
    print(f"ok: {payload.get('source_count', 0)} sources")
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    payload = client().run_jeb_once()
    if args.json:
        emit(payload, json_mode=True)
        return 0
    print("ok: scheduler pass completed")
    return 0


def cmd_archive_now(args: argparse.Namespace) -> int:
    payload = client().archive_jeb_now(account=args.account, process=not args.no_process)
    if args.json:
        emit(payload, json_mode=True)
        return 0
    if payload.get("status") == "no_eligible_files":
        print(f"no eligible files for account {args.account}")
        return 1
    print(f"archive attempt started for account {args.account}: {payload.get('batch_id')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jeb",
        description="Remote Jeb operator CLI.",
        epilog=(
            "commands:\n"
            "  status        show read-only collector status\n"
            "  batches       list batch attempts\n"
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

    batches = sub.add_parser("batches", help="list batch attempts")
    batches.add_argument("--page", type=positive_int, default=1, help="Page number.")
    batches.add_argument(
        "--per-page",
        type=per_page_value,
        default=25,
        help=f"Rows per page, up to {MAX_PER_PAGE}.",
    )
    batches.add_argument(
        "--sort",
        choices=sorted(BATCH_LIST_SORT_FIELDS),
        default="updated_at",
        help="Sort field.",
    )
    batches.add_argument(
        "--order",
        choices=("asc", "desc"),
        default="desc",
        help="Sort order.",
    )
    batches.add_argument(
        "--terminal",
        choices=("active", "terminal", "all"),
        default="active",
        help="Show active, terminal, or all attempts.",
    )
    batches.add_argument("--state", help="Filter by batch attempt state.")
    batches.add_argument("--account", help="Filter by account/source slug.")
    batches.add_argument("--collection", help="Filter by collection id.")
    batches.add_argument("--target", help="Filter by target name.")
    batches.add_argument(
        "--query",
        "-q",
        help="Search attempt, batch, job, collection, target, state, timestamp, or error.",
    )
    batches.add_argument("--json", action="store_true", help="Emit JSON.")
    batches.set_defaults(func=cmd_batches)

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
