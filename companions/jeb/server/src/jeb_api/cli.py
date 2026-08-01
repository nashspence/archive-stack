from __future__ import annotations

import argparse
import logging
import os
import sys

from jeb_cli_support.listing import add_list_output_arguments, add_list_query_arguments
from jeb_cli_support.output import (
    archive_plan_exit_code,
    format_archive_plan,
    format_attempt,
    format_attempts,
    format_config_check,
    format_operation,
    format_status,
)
from jeb_core.domain.models import UnrecoverableJebError
from jeb_protocol import ATTEMPT_LIST_SORT_FIELDS
from riverhog_cli_support.output import emit, format_list_ids

from jeb_api.app import JebServiceState, start_jeb_service_server
from jeb_api.composition import config_from_env, create_services

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
        description="Service-local Jeb source and delivery server.",
        epilog=(
            "commands:\n"
            "  run           run continuously and process eligible batches\n"
            "  once          discover and process one scheduler pass\n"
            "  archive-now   archive one source immediately\n"
            "  status        show read-only service status\n"
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
    archive_now.add_argument("--source", required=True, help="Source id to archive.")
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
    status = sub.add_parser("status", help="show read-only service status")
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
        query_help=("Search attempt, batch, job, source, target, state, run id, or error."),
    )
    attempt_list.add_argument(
        "--resolution",
        choices=("unresolved", "resolved", "all"),
        default="unresolved",
        help="Show unresolved, resolved, or all attempts.",
    )
    attempt_list.add_argument("--state", help="Filter by attempt state.")
    attempt_list.add_argument("--source", help="Filter by source id.")
    attempt_list.add_argument("--target", help="Filter by target name.")
    add_list_output_arguments(attempt_list, noun="attempt")
    attempt_show = attempt_sub.add_parser("show", help="show one processing attempt")
    attempt_show.add_argument("attempt")
    attempt_show.add_argument("--json", action="store_true", help="Emit JSON.")
    attempt_cancel = attempt_sub.add_parser("cancel", help="cancel one unresolved attempt")
    attempt_cancel.add_argument("attempt")
    attempt_cancel.add_argument("--json", action="store_true", help="Emit JSON.")
    sub.add_parser(
        "check-config",
        help="validate env configuration and initialize state",
    )
    args = parser.parse_args(argv)
    command = args.command or "run"

    services = create_services(config_from_env())
    if command == "check-config":
        services.runtime.initialize()
        sources = services.source_registry.list()
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
        services.runtime.run_once()
        emit(
            format_operation(
                {"status": "completed", "operation": {"operation": "once"}},
                title="jeb scheduler pass",
            ),
            json_mode=False,
        )
        return 0
    if command == "archive-now":
        services.runtime.initialize()
        if args.dry_run:
            try:
                payload = services.attempts.archive_plan(
                    source_id=args.source,
                    process=not args.no_process,
                )
            except UnrecoverableJebError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            emit(format_archive_plan(payload), json_mode=False)
            return archive_plan_exit_code(payload)
        try:
            attempt_id = services.attempts.archive_now(
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
        attempt = services.store.load_attempt(attempt_id)
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
        services.runtime.initialize()
        payload = services.runtime.status_summary(include_backlog=not args.no_backlog)
        emit(payload if args.json else format_status(payload), json_mode=args.json)
        return 0
    if command == "attempt":
        services.runtime.initialize()
        if args.attempt_command == "show":
            payload = services.store.get_attempt(args.attempt)
            emit(payload if args.json else format_attempt(payload), json_mode=args.json)
            return 0
        if args.attempt_command == "cancel":
            try:
                payload = services.attempts.cancel_attempt(args.attempt)
            except (KeyError, UnrecoverableJebError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            emit(payload if args.json else format_attempt(payload), json_mode=args.json)
            return 0
        payload = services.store.list_attempts(
            page=args.page,
            per_page=args.per_page,
            sort=args.sort,
            order=args.order,
            query=args.query,
            resolution=args.resolution,
            state=args.state,
            source=args.source,
            target=args.target,
            all_items=args.all,
        )
        if args.ids:
            emit(format_list_ids(payload, "attempts", id_key="attempt_id"), json_mode=False)
            return 0
        emit(payload if args.json else format_attempts(payload), json_mode=args.json)
        return 0
    services.runtime.initialize()
    start_jeb_service_server(
        DEFAULT_HEALTH_HOST,
        health_port(),
        JebServiceState(services=services),
    )
    services.runtime.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
