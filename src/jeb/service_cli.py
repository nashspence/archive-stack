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
from jeb.listing import add_list_output_arguments, add_list_query_arguments
from jeb.output import (
    emit,
    format_archive_plan,
    format_attempts,
    format_config_check,
    format_list_ids,
    format_operation,
    format_status,
)
from jeb.service_api import JebServiceState, start_jeb_service_server

DEFAULT_HEALTH_HOST = os.getenv("JEB_HEALTH_HOST", "0.0.0.0")
DEFAULT_HEALTH_PORT = "8081"


def health_port() -> int:
    value = os.getenv("JEB_HEALTH_PORT", DEFAULT_HEALTH_PORT)
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError(f"JEB_HEALTH_PORT must be an integer, got {value!r}") from exc
    if not 0 < port < 65536:
        raise ValueError(f"JEB_HEALTH_PORT must be between 1 and 65535, got {port}")
    return port


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
            "  archive-now   archive one source immediately\n"
            "  status        show read-only collector status\n"
            "  attempt list  list processing attempts\n"
            "  check-config  validate env configuration and initialize state"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="run continuously and process eligible batches")
    sub.add_parser("once", help="discover and process one scheduler pass")
    archive_now = sub.add_parser(
        "archive-now",
        help="archive one source immediately",
    )
    archive_now.add_argument("--source", required=True, help="Source slug to archive.")
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
    attempt_parser = sub.add_parser("attempt", help="inspect processing attempts")
    attempt_sub = attempt_parser.add_subparsers(dest="attempt_command", required=True)
    attempt_list = attempt_sub.add_parser("list", help="list processing attempts")
    add_list_query_arguments(
        attempt_list,
        sort_fields=ATTEMPT_LIST_SORT_FIELDS,
        default_sort="updated_at",
        default_order="desc",
        query_help=("Search attempt, batch, job, collection, target, state, timestamp, or error."),
    )
    attempt_list.add_argument(
        "--terminal",
        choices=("active", "terminal", "all"),
        default="active",
        help="Show active, terminal, or all attempts.",
    )
    attempt_list.add_argument("--state", help="Filter by attempt state.")
    attempt_list.add_argument("--source", help="Filter by source slug.")
    attempt_list.add_argument("--collection-slug", help="Filter by output collection slug.")
    attempt_list.add_argument("--target", help="Filter by target name.")
    add_list_output_arguments(attempt_list, noun="attempt")
    sub.add_parser(
        "check-config",
        help="validate env configuration and initialize state",
    )
    args = parser.parse_args(argv)
    command = args.command or "run"

    collector = Collector(config_from_env())
    if command == "check-config":
        collector.init_db()
        sources = collector.source_registry.list()
        emit(
            format_config_check(
                {
                    "status": "ok",
                    "source_count": len(sources),
                    "sources": [source.id for source in sources],
                }
            ),
            json_mode=False,
        )
        return 0
    if command == "once":
        collector.run_once()
        emit(
            format_operation(
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
                    source_id=args.source,
                    process=not args.no_process,
                )
            except UnrecoverableJebError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            emit(format_archive_plan(payload), json_mode=False)
            return 0
        try:
            attempt_id = collector.archive_now(
                source_id=args.source,
                process=not args.no_process,
            )
        except UnrecoverableJebError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if attempt_id is None:
            emit(
                format_operation(
                    {"status": "no_eligible_files", "source": args.source},
                    title="jeb archive",
                ),
                json_mode=False,
            )
            return 1
        attempt = collector.load_attempt(attempt_id)
        emit(
            format_operation(
                {
                    "status": "processed" if not args.no_process else "staged",
                    "source": args.source,
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
        emit(payload if args.json else format_status(payload), json_mode=args.json)
        return 0
    if command == "attempt":
        collector.init_db()
        payload = collector.list_attempts(
            page=args.page,
            per_page=args.per_page,
            sort=args.sort,
            order=args.order,
            query=args.query,
            terminal=args.terminal,
            state=args.state,
            source=args.source,
            collection_slug=args.collection_slug,
            target=args.target,
            all_items=args.all,
        )
        if args.ids:
            emit(format_list_ids(payload, "attempts", id_key="attempt_id"), json_mode=False)
            return 0
        emit(payload if args.json else format_attempts(payload), json_mode=args.json)
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
