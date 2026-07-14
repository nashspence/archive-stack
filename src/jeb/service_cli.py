from __future__ import annotations

import argparse
import logging
import os
import sys

from jeb.collector import (
    ATTEMPT_LIST_SORT_FIELDS,
    Collector,
    UnrecoverableJebError,
    config_from_env,
)
from jeb.service_api import JebServiceState, start_jeb_service_server
from riverhog_cli.output import (
    emit,
    format_jeb_archive_plan,
    format_jeb_attempts,
    format_jeb_config_check,
    format_jeb_operation,
    format_jeb_status,
)

DEFAULT_HEALTH_HOST = os.getenv("JEB_HEALTH_HOST", "0.0.0.0")
DEFAULT_HEALTH_PORT = "8081"
MAX_PER_PAGE = 500


def health_port() -> int:
    value = os.getenv("JEB_HEALTH_PORT", DEFAULT_HEALTH_PORT)
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError(f"JEB_HEALTH_PORT must be an integer, got {value!r}") from exc
    if not 0 < port < 65536:
        raise ValueError(f"JEB_HEALTH_PORT must be between 1 and 65535, got {port}")
    return port


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def per_page_value(value: str) -> int:
    parsed = positive_int(value)
    if parsed > MAX_PER_PAGE:
        raise argparse.ArgumentTypeError(f"must be <= {MAX_PER_PAGE}")
    return parsed


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.getenv("JEB_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        prog="jeb-service",
        description="Service-local Jeb collector and uploader.",
        epilog=(
            "commands:\n"
            "  run           run continuously and process eligible batches\n"
            "  once          discover and process one scheduler pass\n"
            "  archive-now   archive one account immediately\n"
            "  status        show read-only collector status\n"
            "  attempts      list processing attempts\n"
            "  check-config  validate env configuration and initialize state"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="run continuously and process eligible batches")
    sub.add_parser("once", help="discover and process one scheduler pass")
    archive_now = sub.add_parser(
        "archive-now",
        help="archive one account immediately",
    )
    archive_now.add_argument("--account", required=True, help="Account slug to archive.")
    archive_now.add_argument(
        "--no-process",
        action="store_true",
        help="Create an eligible batch but do not process it in this command.",
    )
    archive_now.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the archive action without creating a batch or uploading files.",
    )
    status = sub.add_parser("status", help="show read-only collector status")
    status.add_argument("--json", action="store_true", help="Emit JSON.")
    status.add_argument(
        "--no-backlog",
        action="store_true",
        help="Skip source directory eligible-file scans.",
    )
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
    sub.add_parser(
        "check-config",
        help="validate env configuration and initialize state",
    )
    args = parser.parse_args(argv)
    command = args.command or "run"

    collector = Collector(config_from_env())
    if command == "check-config":
        collector.init_db()
        emit(
            format_jeb_config_check(
                {
                    "status": "ok",
                    "account_count": len(collector.config.accounts),
                    "accounts": [account.id for account in collector.config.accounts],
                }
            ),
            json_mode=False,
        )
        return 0
    if command == "once":
        collector.run_once()
        emit(
            format_jeb_operation(
                {"status": "completed", "operation": {"operation": "once"}},
                title="jeb scheduler pass",
            ),
            json_mode=False,
        )
        return 0
    if command == "archive-now":
        collector.init_db()
        if args.dry_run:
            try:
                payload = collector.archive_plan(
                    account_id=args.account,
                    process=not args.no_process,
                )
            except UnrecoverableJebError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            emit(format_jeb_archive_plan(payload), json_mode=False)
            return 0
        try:
            attempt_id = collector.archive_now(
                account_id=args.account,
                process=not args.no_process,
            )
        except UnrecoverableJebError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if attempt_id is None:
            emit(
                format_jeb_operation(
                    {"status": "no_eligible_files", "account": args.account},
                    title="jeb archive",
                ),
                json_mode=False,
            )
            return 1
        attempt = collector.load_attempt(attempt_id)
        emit(
            format_jeb_operation(
                {
                    "status": "processed" if not args.no_process else "staged",
                    "account": args.account,
                    "attempt_id": attempt_id,
                    "batch_id": str(attempt["batch_id"]),
                },
                title="jeb archive",
            ),
            json_mode=False,
        )
        return 0
    if command == "status":
        collector.init_db()
        payload = collector.status_summary(include_backlog=not args.no_backlog)
        emit(payload if args.json else format_jeb_status(payload), json_mode=args.json)
        return 0
    if command == "attempts":
        collector.init_db()
        payload = collector.list_attempts(
            page=args.page,
            per_page=args.per_page,
            sort=args.sort,
            order=args.order,
            query=args.query,
            terminal=args.terminal,
            state=args.state,
            account=args.account,
            collection_slug=args.collection_slug,
            target=args.target,
        )
        emit(payload if args.json else format_jeb_attempts(payload), json_mode=args.json)
        return 0
    collector.init_db()
    start_jeb_service_server(
        DEFAULT_HEALTH_HOST,
        health_port(),
        JebServiceState(collector=collector),
    )
    collector.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
