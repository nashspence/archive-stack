from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml
from jeb_api_client import JebApiClient, JebApiError
from jeb_cli_support.listing import add_list_output_arguments, add_list_query_arguments
from jeb_cli_support.output import (
    archive_plan_exit_code,
    format_archive_plan,
    format_attempt,
    format_attempt_transition,
    format_attempts,
    format_config_check,
    format_operation,
    format_operation_detail,
    format_operations,
    format_source,
    format_source_removal,
    format_source_result,
    format_sources,
    format_status,
)
from jeb_protocol import ATTEMPT_LIST_SORT_FIELDS, SOURCE_LIST_SORT_FIELDS, attempt_succeeded
from riverhog_cli_support.output import (
    emit,
    error_document,
    format_lifecycle_events,
    format_list_ids,
)

_API_CLIENT: JebApiClient | None = None


def client() -> JebApiClient:
    global _API_CLIENT
    if _API_CLIENT is None:
        _API_CLIENT = JebApiClient()
    return _API_CLIENT


def _close_client() -> None:
    global _API_CLIENT
    if _API_CLIENT is not None:
        _API_CLIENT.close()
        _API_CLIENT = None


def cmd_status(args: argparse.Namespace) -> int:
    payload = client().get_status(include_backlog=not args.no_backlog)
    emit(payload if args.json else format_status(payload), json_mode=args.json)
    return 0


def cmd_event_list(args: argparse.Namespace) -> int:
    page = client().list_lifecycle_events(after=args.after, limit=args.limit)
    payload = page.model_dump(mode="json")
    emit(payload if args.json else format_lifecycle_events(payload), json_mode=args.json)
    return 0


def event_limit(value: str) -> int:
    limit = int(value)
    if not 1 <= limit <= 100:
        raise argparse.ArgumentTypeError("event limit must be between 1 and 100")
    return limit


def cmd_attempt_list(args: argparse.Namespace) -> int:
    payload = client().list_attempts(
        page=args.page,
        per_page=args.per_page,
        sort=args.sort,
        order=args.order,
        resolution=args.resolution,
        state=args.state,
        source=args.source,
        target=args.target,
        query=args.query,
        all_items=args.all,
    )
    if args.ids:
        emit(format_list_ids(payload, "attempts", id_key="attempt_id"), json_mode=False)
        return 0
    emit(payload if args.json else format_attempts(payload), json_mode=args.json)
    return 0


def cmd_attempt_show(args: argparse.Namespace) -> int:
    payload = client().get_attempt(args.attempt)
    emit(payload if args.json else format_attempt(payload), json_mode=args.json)
    return 0


def cmd_attempt_watch(args: argparse.Namespace) -> int:
    def report_update(payload: dict[str, Any]) -> None:
        print(format_attempt_transition(payload), file=sys.stderr)

    on_update = None if args.json else report_update
    payload = client().wait_for_attempt(
        args.attempt,
        interval=args.interval,
        on_update=on_update,
    )
    emit(payload if args.json else format_attempt(payload), json_mode=args.json)
    return 0 if attempt_succeeded(payload) else 1


def cmd_attempt_cancel(args: argparse.Namespace) -> int:
    payload = client().cancel_attempt(args.attempt)
    emit(payload if args.json else format_attempt(payload), json_mode=args.json)
    return 0


def cmd_check_config(args: argparse.Namespace) -> int:
    payload = client().check_config()
    emit(payload if args.json else format_config_check(payload), json_mode=args.json)
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    payload = client().run_once()
    if args.json:
        emit(payload, json_mode=True)
        return 0
    emit(format_operation(payload, title="jeb scheduler pass"), json_mode=False)
    return 0


def cmd_operation_list(args: argparse.Namespace) -> int:
    payload = client().list_operations(
        page=args.page,
        per_page=args.per_page,
        sort=args.sort,
        order=args.order,
        state=args.state,
        query=args.query,
        all_items=args.all,
    )
    if args.ids:
        emit(format_list_ids(payload, "operations"), json_mode=False)
        return 0
    emit(payload if args.json else format_operations(payload), json_mode=args.json)
    return 0


def cmd_operation_show(args: argparse.Namespace) -> int:
    payload = client().get_operation(args.operation)
    emit(payload if args.json else format_operation_detail(payload), json_mode=args.json)
    return 0


def cmd_operation_watch(args: argparse.Namespace) -> int:
    payload = client().wait_for_operation(args.operation, interval=args.interval)
    emit(payload if args.json else format_operation_detail(payload), json_mode=args.json)
    return 0 if payload.get("state") == "succeeded" else 1


def cmd_archive_now(args: argparse.Namespace) -> int:
    payload = client().archive_source_now(
        source=args.source,
        process=not args.no_process,
        dry_run=args.dry_run,
    )
    if args.json:
        emit(payload, json_mode=True)
    elif args.dry_run:
        emit(format_archive_plan(payload), json_mode=False)
    else:
        if payload.get("status") == "no_eligible_files":
            emit(format_operation(payload, title="jeb archive"), json_mode=False)
            return 1
        emit(format_operation(payload, title="jeb archive"), json_mode=False)
    if args.dry_run:
        return archive_plan_exit_code(payload)
    return 0


def load_object(path: str, *, label: str) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"could not read {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON/YAML object")
    return payload


def source_credential(args: argparse.Namespace) -> str | None:
    if not args.credential_stdin:
        return None
    credential = sys.stdin.readline().rstrip("\r\n")
    if not credential:
        raise ValueError("credential stdin was empty")
    return credential


def parse_target_config(items: list[str] | None) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for item in items or []:
        name, separator, raw_value = item.partition("=")
        name = name.strip()
        if not separator or not name:
            raise ValueError("target config values must use NAME=JSON")
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        config[name] = value
    return config


def cmd_source_list(args: argparse.Namespace) -> int:
    payload = client().list_sources(
        page=args.page,
        per_page=args.per_page,
        sort=args.sort,
        order=args.order,
        query=args.query,
        enabled=args.enabled,
        adapter=args.adapter,
        target=args.target,
        all_items=args.all,
    )
    if args.ids:
        emit(format_list_ids(payload, "sources"), json_mode=False)
        return 0
    emit(payload if args.json else format_sources(payload), json_mode=args.json)
    return 0


def cmd_source_show(args: argparse.Namespace) -> int:
    payload = client().get_source(args.source)
    emit(payload if args.json else format_source(payload), json_mode=args.json)
    return 0


def cmd_source_add(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {
        "id": args.source,
        "adapters": args.adapter,
        "target_config": parse_target_config(args.target_config),
        "enabled": not args.disabled,
        "stable_seconds": args.stable_seconds,
        "target": args.target,
        "threshold_bytes": args.threshold_bytes,
        "cleanup": args.cleanup,
        "cadence": args.cadence,
        "weekday": args.weekday,
        "hour": args.hour,
        "minute": args.minute,
    }
    if args.include_extension:
        payload["include_extensions"] = args.include_extension
    credential = source_credential(args)
    if credential is not None:
        payload["credential"] = credential
    result = client().create_source(payload)
    emit(result if args.json else format_source_result(result), json_mode=args.json)
    return 0


def cmd_source_set(args: argparse.Namespace) -> int:
    changes = load_object(args.changes, label="source changes") if args.changes is not None else {}
    if args.target_config is not None:
        changes["target_config"] = parse_target_config(args.target_config)
    payload = client().update_source(args.source, changes)
    emit(payload if args.json else format_source(payload), json_mode=args.json)
    return 0


def cmd_source_enabled(args: argparse.Namespace) -> int:
    operation = client().enable_source if args.enabled else client().disable_source
    payload = operation(args.source)
    emit(payload if args.json else format_source(payload), json_mode=args.json)
    return 0


def cmd_source_credential(args: argparse.Namespace) -> int:
    payload = client().rotate_source_credential(
        args.source,
        credential=source_credential(args),
    )
    emit(payload if args.json else format_source_result(payload), json_mode=args.json)
    return 0


def format_source_removal_plan(payload: dict[str, Any]) -> str:
    lines = [
        f"Jeb source removal plan: {payload.get('source', 'unknown')}",
        f"mode: {'purge' if payload.get('purge') else 'remove'}",
        f"Jeb-managed files: {payload.get('managed_file_count', 0)}",
        f"Jeb-managed bytes: {payload.get('managed_bytes', 0)}",
        f"unresolved delivery attempts: {len(payload.get('unresolved_attempts') or [])}",
    ]
    if payload.get("warning"):
        lines.extend(("", str(payload["warning"])))
    blockers = payload.get("blockers") or []
    if blockers:
        lines.extend(("", "Blocked:"))
        lines.extend(f"- {item}" for item in blockers)
    if payload.get("expires_at"):
        lines.append(f"plan expires: {payload['expires_at']}")
    if payload.get("challenge"):
        lines.append(f"confirmation challenge: {payload['challenge']}")
    return "\n".join(lines)


def cmd_source_remove(args: argparse.Namespace) -> int:
    if args.dry_run and args.confirm:
        raise ValueError("--dry-run and --confirm cannot be used together")
    if args.confirm:
        payload = client().remove_source(args.source, challenge=args.confirm)
        emit(payload if args.json else format_source_removal(payload), json_mode=args.json)
        return 0
    plan = client().plan_source_removal(args.source, purge=args.purge)
    if args.json:
        emit(plan, json_mode=True)
    else:
        print(format_source_removal_plan(plan))
    if plan.get("blockers"):
        return 1
    if args.dry_run:
        return 0
    challenge = plan.get("challenge")
    if not isinstance(challenge, str) or not challenge:
        raise ValueError("server did not return a source removal challenge")
    if not sys.stdin.isatty():
        raise ValueError("interactive confirmation requires a terminal; use --confirm")
    entered = input(f"Type the source id {args.source!r} to confirm: ")
    if entered != args.source:
        print("Source removal canceled.", file=sys.stderr)
        return 1
    result = client().remove_source(args.source, challenge=challenge)
    emit(result if args.json else format_source_removal(result), json_mode=args.json)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jeb",
        description="Remote Jeb operator CLI.",
        epilog=(
            "commands:\n"
            "  status        show read-only service status\n"
            "  event         inspect lifecycle events\n"
            "  attempt       inspect processing attempts\n"
            "  operation     inspect service operations\n"
            "  check-config  validate deployed Jeb configuration\n"
            "  once          request one scheduler pass\n"
            "  archive-now   archive one source immediately\n"
            "  source        manage enrolled sources"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=importlib.metadata.version("jeb-client"),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="show read-only service status")
    status.add_argument("--json", action="store_true", help="Emit JSON.")
    status.add_argument(
        "--no-backlog",
        action="store_true",
        help="Skip source directory eligible-file scans on the Jeb service.",
    )
    status.set_defaults(func=cmd_status)

    event = sub.add_parser("event", help="inspect lifecycle events")
    event_sub = event.add_subparsers(dest="event_command", required=True)
    event_list = event_sub.add_parser("list", help="list application-visible lifecycle events")
    event_list.add_argument("--after", help="Return events after this cursor.")
    event_list.add_argument("--limit", type=event_limit, default=100)
    event_list.add_argument("--json", action="store_true", help="Emit JSON.")
    event_list.set_defaults(func=cmd_event_list)

    attempt = sub.add_parser("attempt", help="inspect processing attempts")
    attempt_sub = attempt.add_subparsers(dest="attempt_command", required=True)
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
    attempt_list.set_defaults(func=cmd_attempt_list)
    attempt_show = attempt_sub.add_parser("show", help="show one processing attempt")
    attempt_show.add_argument("attempt")
    attempt_show.add_argument("--json", action="store_true", help="Emit JSON.")
    attempt_show.set_defaults(func=cmd_attempt_show)
    attempt_watch = attempt_sub.add_parser("watch", help="watch one processing attempt")
    attempt_watch.add_argument("attempt")
    attempt_watch.add_argument("--interval", type=float, default=10.0, help="Polling seconds.")
    attempt_watch.add_argument("--json", action="store_true", help="Emit final JSON.")
    attempt_watch.set_defaults(func=cmd_attempt_watch)
    attempt_cancel = attempt_sub.add_parser("cancel", help="cancel one unresolved attempt")
    attempt_cancel.add_argument("attempt")
    attempt_cancel.add_argument("--json", action="store_true", help="Emit JSON.")
    attempt_cancel.set_defaults(func=cmd_attempt_cancel)

    operation = sub.add_parser("operation", help="inspect service operations")
    operation_sub = operation.add_subparsers(dest="operation_command", required=True)
    operation_list = operation_sub.add_parser("list", help="list service operations")
    add_list_query_arguments(
        operation_list,
        sort_fields={"id", "operation", "state", "started_at", "completed_at"},
        default_sort="started_at",
        default_order="desc",
        query_help="Search operation fields.",
    )
    operation_list.add_argument("--state", help="Filter by operation state.")
    add_list_output_arguments(operation_list, noun="operation")
    operation_list.set_defaults(func=cmd_operation_list)
    operation_show = operation_sub.add_parser("show", help="show one service operation")
    operation_show.add_argument("operation")
    operation_show.add_argument("--json", action="store_true", help="Emit JSON.")
    operation_show.set_defaults(func=cmd_operation_show)
    operation_watch = operation_sub.add_parser("watch", help="watch one service operation")
    operation_watch.add_argument("operation")
    operation_watch.add_argument("--interval", type=float, default=1.0)
    operation_watch.add_argument("--json", action="store_true", help="Emit final JSON.")
    operation_watch.set_defaults(func=cmd_operation_watch)

    check_config = sub.add_parser("check-config", help="validate deployed Jeb configuration")
    check_config.add_argument("--json", action="store_true", help="Emit JSON.")
    check_config.set_defaults(func=cmd_check_config)

    once = sub.add_parser("once", help="request one scheduler pass")
    once.add_argument("--json", action="store_true", help="Emit JSON.")
    once.set_defaults(func=cmd_once)

    archive_now = sub.add_parser("archive-now", help="archive one source immediately")
    archive_now.add_argument("--source", required=True, help="Source id to archive.")
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

    source = sub.add_parser("source", help="manage enrolled sources")
    source_sub = source.add_subparsers(dest="source_command", required=True)
    source_list = source_sub.add_parser("list", help="list sources")
    add_list_query_arguments(
        source_list,
        sort_fields=SOURCE_LIST_SORT_FIELDS,
        default_sort="id",
        default_order="asc",
        query_help="Search source, target config, cadence, or adapter.",
    )
    source_state = source_list.add_mutually_exclusive_group()
    source_state.add_argument(
        "--enabled",
        dest="enabled",
        action="store_const",
        const=True,
        default=None,
        help="Show enabled sources.",
    )
    source_state.add_argument(
        "--disabled",
        dest="enabled",
        action="store_const",
        const=False,
        help="Show disabled sources.",
    )
    source_list.add_argument(
        "--adapter",
        choices=("ftp", "tus"),
        help="Filter by enabled ingress adapter.",
    )
    source_list.add_argument("--target", help="Filter by target name.")
    add_list_output_arguments(source_list, noun="source")
    source_list.set_defaults(func=cmd_source_list)
    source_show = source_sub.add_parser("show", help="show one source")
    source_show.add_argument("source")
    source_show.add_argument("--json", action="store_true", help="Emit JSON.")
    source_show.set_defaults(func=cmd_source_show)
    source_add = source_sub.add_parser("add", help="enroll a source")
    source_add.add_argument("source")
    source_add.add_argument(
        "--adapter",
        action="append",
        choices=("ftp", "tus"),
        required=True,
        help="Enable an ingress adapter; repeat for more than one.",
    )
    source_add.add_argument(
        "--target-config",
        action="append",
        required=True,
        metavar="NAME=JSON",
        help="Adapter-specific target setting; repeat for more than one.",
    )
    source_add.add_argument(
        "--credential-stdin",
        action="store_true",
        help="Read the ingress credential from one line of stdin.",
    )
    source_add.add_argument("--disabled", action="store_true")
    source_add.add_argument("--stable-seconds", type=int, default=600)
    source_add.add_argument("--include-extension", action="append")
    source_add.add_argument("--target", default="munchy")
    source_add.add_argument("--threshold-bytes", type=int, default=0)
    source_add.add_argument(
        "--cleanup",
        choices=("never", "after_target_success"),
        default="after_target_success",
    )
    source_add.add_argument(
        "--cadence",
        choices=("weekly", "monthly", "seasonal", "manual"),
        default="weekly",
    )
    source_add.add_argument("--weekday", type=int, default=0)
    source_add.add_argument("--hour", type=int, default=3)
    source_add.add_argument("--minute", type=int, default=0)
    source_add.add_argument("--json", action="store_true", help="Emit JSON.")
    source_add.set_defaults(func=cmd_source_add)
    source_set = source_sub.add_parser("set", help="apply source settings from JSON/YAML")
    source_set.add_argument("source")
    source_set_input = source_set.add_mutually_exclusive_group(required=True)
    source_set_input.add_argument("--changes", help="Source changes JSON/YAML file.")
    source_set_input.add_argument(
        "--target-config",
        action="append",
        metavar="NAME=JSON",
        help="Replace adapter-specific target settings; repeat for more than one.",
    )
    source_set.add_argument("--json", action="store_true", help="Emit JSON.")
    source_set.set_defaults(func=cmd_source_set)
    for action, enabled in (("enable", True), ("disable", False)):
        source_enabled = source_sub.add_parser(action, help=f"{action} a source")
        source_enabled.add_argument("source")
        source_enabled.add_argument("--json", action="store_true", help="Emit JSON.")
        source_enabled.set_defaults(func=cmd_source_enabled, enabled=enabled)
    credential = source_sub.add_parser("credential", help="rotate a source credential")
    credential.add_argument("source")
    credential.add_argument(
        "--credential-stdin",
        action="store_true",
        help="Read the replacement credential from one line of stdin; otherwise generate one.",
    )
    credential.add_argument("--json", action="store_true", help="Emit JSON.")
    credential.set_defaults(func=cmd_source_credential)
    remove = source_sub.add_parser("remove", help="remove a source enrollment")
    remove.add_argument("source")
    remove.add_argument(
        "--purge",
        action="store_true",
        help="Include Jeb-managed upload, landing, staged, and delivery state.",
    )
    remove.add_argument("--dry-run", action="store_true", help="Show the removal plan only.")
    remove.add_argument("--confirm", help="Short-lived challenge from a prior plan.")
    remove.add_argument("--json", action="store_true", help="Emit JSON.")
    remove.set_defaults(func=cmd_source_remove)
    return parser


def _error_message(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, JebApiError):
        return exc.code
    if isinstance(exc, httpx.TransportError):
        return "transport_error"
    if isinstance(exc, ValueError):
        return "bad_request"
    return "error"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (JebApiError, httpx.TransportError, ValueError) as exc:
        if bool(getattr(args, "json", False)):
            emit(
                error_document(
                    code=_error_code(exc),
                    message=_error_message(exc),
                    details=exc.details if isinstance(exc, JebApiError) else None,
                ),
                json_mode=True,
            )
            return 1
        prefix = "transport error: " if isinstance(exc, httpx.TransportError) else ""
        print(f"jeb: {prefix}{_error_message(exc)}", file=sys.stderr)
        return 1
    finally:
        _close_client()


if __name__ == "__main__":
    raise SystemExit(main())
