from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

from jeb.collector import ATTEMPT_LIST_SORT_FIELDS
from jeb.service_cli import (
    MAX_PER_PAGE,
    per_page_value,
    positive_int,
)
from munchy.job_authoring import load_munchy_job_config, munchy_job_defaults_from_config
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
        source=args.source,
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
        source=args.source,
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


def load_object(path: str, *, label: str) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"could not read {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON/YAML object")
    return payload


def load_target_policy(path: str, *, target: str) -> dict[str, Any]:
    payload = load_object(path, label="source policy")
    if target == "munchy" and payload.get("kind") == "munchy.job":
        return munchy_job_defaults_from_config(load_munchy_job_config(Path(path)))
    return payload


def source_credential(args: argparse.Namespace) -> str | None:
    if not args.credential_stdin:
        return None
    credential = sys.stdin.readline().rstrip("\r\n")
    if not credential:
        raise ValueError("credential stdin was empty")
    return credential


def cmd_source_list(args: argparse.Namespace) -> int:
    payload = client().list_jeb_sources()
    if args.json:
        emit(payload, json_mode=True)
        return 0
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        print("No Jeb sources enrolled.")
        return 0
    for source in sources:
        if not isinstance(source, dict):
            continue
        adapters = ",".join(str(item) for item in source.get("adapters", []))
        state = "enabled" if source.get("enabled") else "disabled"
        print(f"{source.get('id')}\t{state}\t{adapters}")
    return 0


def cmd_source_show(args: argparse.Namespace) -> int:
    payload = client().get_jeb_source(args.source)
    emit(payload, json_mode=True)
    return 0


def cmd_source_add(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {
        "id": args.source,
        "adapters": args.adapter,
        "policy": load_target_policy(args.policy, target=args.target),
        "enabled": not args.disabled,
        "stable_seconds": args.stable_seconds,
        "collection_slug": args.collection_slug or args.source,
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
    result = client().add_jeb_source(payload)
    emit(result, json_mode=True)
    return 0


def cmd_source_set(args: argparse.Namespace) -> int:
    changes = (
        load_object(args.changes, label="source changes") if args.changes is not None else {}
    )
    if args.policy is not None:
        changes["policy"] = load_target_policy(args.policy, target=args.target)
    payload = client().update_jeb_source(args.source, changes)
    emit(payload, json_mode=True)
    return 0


def cmd_source_enabled(args: argparse.Namespace) -> int:
    payload = client().set_jeb_source_enabled(args.source, enabled=args.enabled)
    emit(payload, json_mode=True)
    return 0


def cmd_source_credential(args: argparse.Namespace) -> int:
    payload = client().rotate_jeb_source_credential(
        args.source,
        credential=source_credential(args),
    )
    emit(payload, json_mode=True)
    return 0


def format_source_removal_plan(payload: dict[str, Any]) -> str:
    lines = [
        f"Jeb source removal plan: {payload.get('source', 'unknown')}",
        f"mode: {'purge' if payload.get('purge') else 'remove'}",
        f"Jeb-managed files: {payload.get('managed_file_count', 0)}",
        f"Jeb-managed bytes: {payload.get('managed_bytes', 0)}",
        f"active delivery attempts: {len(payload.get('active_attempts') or [])}",
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
        payload = client().remove_jeb_source(args.source, challenge=args.confirm)
        emit(payload, json_mode=args.json)
        return 0
    plan = client().plan_jeb_source_removal(args.source, purge=args.purge)
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
    result = client().remove_jeb_source(args.source, challenge=challenge)
    emit(result, json_mode=args.json)
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
            "  archive-now   archive one source immediately\n"
            "  source        manage enrolled sources"
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
    attempts.add_argument("--source", help="Filter by source slug.")
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

    archive_now = sub.add_parser("archive-now", help="archive one source immediately")
    archive_now.add_argument("--source", required=True, help="Source slug to archive.")
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
    source_list.add_argument("--json", action="store_true", help="Emit JSON.")
    source_list.set_defaults(func=cmd_source_list)
    source_show = source_sub.add_parser("show", help="show one source")
    source_show.add_argument("source")
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
    source_add.add_argument("--policy", required=True, help="Target policy JSON/YAML file.")
    source_add.add_argument(
        "--credential-stdin",
        action="store_true",
        help="Read the ingress credential from one line of stdin.",
    )
    source_add.add_argument("--disabled", action="store_true")
    source_add.add_argument("--stable-seconds", type=int, default=600)
    source_add.add_argument("--include-extension", action="append")
    source_add.add_argument("--collection-slug")
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
    source_add.set_defaults(func=cmd_source_add)
    source_set = source_sub.add_parser("set", help="apply source settings from JSON/YAML")
    source_set.add_argument("source")
    source_set_input = source_set.add_mutually_exclusive_group(required=True)
    source_set_input.add_argument("--changes", help="Source changes JSON/YAML file.")
    source_set_input.add_argument("--policy", help="Replacement target policy JSON/YAML file.")
    source_set.add_argument("--target", default="munchy")
    source_set.set_defaults(func=cmd_source_set)
    for action, enabled in (("enable", True), ("disable", False)):
        source_enabled = source_sub.add_parser(action, help=f"{action} a source")
        source_enabled.add_argument("source")
        source_enabled.set_defaults(func=cmd_source_enabled, enabled=enabled)
    credential = source_sub.add_parser("credential", help="rotate a source credential")
    credential.add_argument("source")
    credential.add_argument(
        "--credential-stdin",
        action="store_true",
        help="Read the replacement credential from one line of stdin; otherwise generate one.",
    )
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
    if isinstance(exc, httpx.TransportError):
        return f"transport error: {exc}"
    return str(exc) or type(exc).__name__


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (RiverhogError, httpx.TransportError, ValueError) as exc:
        print(f"jeb: {_error_message(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
