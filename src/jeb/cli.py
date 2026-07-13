from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Mapping
from typing import Any

from jeb.collector import (
    BATCH_LIST_SORT_FIELDS,
    Collector,
    UnrecoverableJebError,
    config_from_env,
    format_progress_bytes,
)
from jeb.health import JebHealthState, start_health_server

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


def emit(payload: object, *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    print(str(payload))


def preview(value: object, *, limit: int = 160) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def sequence(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def batch_page_header(payload: Mapping[str, Any]) -> str:
    header = (
        f"batches page {payload.get('page', 1)}/{payload.get('pages', 0)}  "
        f"per_page={payload.get('per_page', 25)}  "
        f"total={payload.get('total', 0)}  "
        f"sort={payload.get('sort', 'updated_at')}  "
        f"order={payload.get('order', 'desc')}  "
        f"terminal={payload.get('terminal', 'active')}"
    )
    if payload.get("query"):
        header += f"  query={payload.get('query')}"
    filters = mapping(payload.get("filters"))
    active_filters = [
        f"{key}={value}" for key, value in filters.items() if value not in (None, [], "")
    ]
    if active_filters:
        header += "  " + "  ".join(active_filters)
    return header


def account_text(batch: Mapping[str, Any]) -> str:
    accounts = [str(item) for item in sequence(batch.get("accounts"))]
    return ",".join(accounts) if accounts else "-"


def format_batch_line(batch: Mapping[str, Any]) -> str:
    parts = [
        str(batch.get("attempt_id") or batch.get("id") or "unknown"),
        f"account={account_text(batch)}",
        f"collection={batch.get('collection_id') or '-'}",
        f"state={batch.get('state') or 'unknown'}",
        f"files={batch.get('file_count', 0)}",
        f"bytes={format_progress_bytes(int(batch.get('total_bytes') or 0))}",
        f"cleanup={batch.get('cleanup') or '-'}",
    ]
    if batch.get("job_id"):
        parts.append(f"job={batch.get('job_id')}")
    if batch.get("updated_at"):
        parts.append(f"updated={batch.get('updated_at')}")
    if batch.get("last_error"):
        parts.append(f"error={preview(batch.get('last_error'))}")
    return "  ".join(parts)


def format_batches(payload: Mapping[str, Any]) -> str:
    lines = [batch_page_header(payload)]
    batches = [batch for batch in sequence(payload.get("batches")) if isinstance(batch, Mapping)]
    if not batches:
        lines.append("- none")
        return "\n".join(lines)
    lines.extend(f"- {format_batch_line(batch)}" for batch in batches)
    return "\n".join(lines)


def summarize_state_counts(counts: Mapping[str, Any]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{state}={counts[state]}" for state in sorted(counts))


def format_source_line(source: Mapping[str, Any]) -> str:
    enabled = "enabled" if source.get("enabled") else "disabled"
    path_state = "present" if source.get("path_exists") else "missing"
    parts = [
        str(source.get("id") or "unknown"),
        enabled,
        f"path={path_state}",
        "routing_preflight=failed"
        if source.get("routing_preflight_failed")
        else "routing_preflight=ok",
    ]
    if "eligible_files" in source:
        parts.append(f"eligible={source.get('eligible_files')} files")
        parts.append(f"bytes={format_progress_bytes(int(source.get('eligible_bytes') or 0))}")
    if source.get("eligible_error"):
        parts.append(f"eligible_error={preview(source.get('eligible_error'))}")
    collections = [str(item) for item in sequence(source.get("collections"))]
    if collections:
        parts.append(f"collections={','.join(collections)}")
    return "  ".join(parts)


def format_status(payload: Mapping[str, Any]) -> str:
    sources = [source for source in sequence(payload.get("sources")) if isinstance(source, Mapping)]
    collections = [
        collection
        for collection in sequence(payload.get("collections"))
        if isinstance(collection, Mapping)
    ]
    batch_counts = mapping(payload.get("batches"))
    preflight = mapping(payload.get("routing_preflight_failures"))
    lines = [
        "jeb status",
        (
            f"sources: {sum(1 for source in sources if source.get('enabled'))}/"
            f"{len(sources)} enabled"
        ),
        (
            f"collections: {sum(1 for item in collections if item.get('enabled'))}/"
            f"{len(collections)} enabled"
        ),
        (
            f"batches: total={batch_counts.get('total', 0)}  "
            f"active={batch_counts.get('active', 0)}  "
            f"terminal={batch_counts.get('terminal', 0)}"
        ),
        f"states: {summarize_state_counts(mapping(batch_counts.get('states')))}",
        f"routing preflight failures: {preflight.get('total', 0)}",
        "sources:",
    ]
    if sources:
        lines.extend(f"- {format_source_line(source)}" for source in sources)
    else:
        lines.append("- none")

    active_attempts = mapping(payload.get("active_attempts"))
    lines.append("active batches:")
    active_batches = [
        batch for batch in sequence(active_attempts.get("batches")) if isinstance(batch, Mapping)
    ]
    if active_batches:
        lines.extend(f"- {format_batch_line(batch)}" for batch in active_batches)
        total_active_listed = int(active_attempts.get("total") or 0)
        if total_active_listed > len(active_batches):
            lines.append(f"- ... {total_active_listed - len(active_batches)} more")
    else:
        lines.append("- none")

    recent_failures = mapping(payload.get("recent_failures"))
    failure_batches = [
        batch for batch in sequence(recent_failures.get("batches")) if isinstance(batch, Mapping)
    ]
    lines.append("recent failures:")
    if failure_batches:
        lines.extend(f"- {format_batch_line(batch)}" for batch in failure_batches)
    else:
        lines.append("- none")

    failures = [
        failure
        for failure in sequence(preflight.get("failures"))
        if isinstance(failure, Mapping)
    ]
    if failures:
        lines.append("routing preflight:")
        lines.extend(
            (
                f"- {failure.get('source_id')}  "
                f"kind={failure.get('failure_kind')}  "
                f"unmatched={failure.get('unmatched_count')}/{failure.get('file_count')}  "
                f"updated={failure.get('updated_at')}  "
                f"message={preview(failure.get('message'))}"
            )
            for failure in failures
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.getenv("JEB_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        prog="jeb",
        description="Weekly collector and automated uploader.",
        epilog=(
            "commands:\n"
            "  run           run continuously and process eligible batches\n"
            "  once          discover and process one scheduler pass\n"
            "  archive-now   archive one account immediately\n"
            "  status        show read-only collector status\n"
            "  batches       list batch attempts\n"
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
    status = sub.add_parser("status", help="show read-only collector status")
    status.add_argument("--json", action="store_true", help="Emit JSON.")
    status.add_argument(
        "--no-backlog",
        action="store_true",
        help="Skip source directory eligible-file scans.",
    )
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
    sub.add_parser(
        "check-config",
        help="validate env configuration and initialize state",
    )
    args = parser.parse_args(argv)
    command = args.command or "run"

    collector = Collector(config_from_env())
    if command == "check-config":
        collector.init_db()
        print(f"ok: {len(collector.config.sources)} sources")
        return 0
    if command == "once":
        collector.run_once()
        return 0
    if command == "archive-now":
        collector.init_db()
        try:
            batch_id = collector.archive_now(
                source_id=args.account,
                process=not args.no_process,
            )
        except UnrecoverableJebError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if batch_id is None:
            print(f"no eligible files for account {args.account}")
            return 1
        print(f"archive attempt started for account {args.account}: {batch_id}")
        return 0
    if command == "status":
        collector.init_db()
        payload = collector.status_summary(include_backlog=not args.no_backlog)
        emit(payload if args.json else format_status(payload), json_mode=args.json)
        return 0
    if command == "batches":
        collector.init_db()
        payload = collector.list_batches(
            page=args.page,
            per_page=args.per_page,
            sort=args.sort,
            order=args.order,
            query=args.query,
            terminal=args.terminal,
            state=args.state,
            account=args.account,
            collection=args.collection,
            target=args.target,
        )
        emit(payload if args.json else format_batches(payload), json_mode=args.json)
        return 0
    collector.init_db()
    start_health_server(
        DEFAULT_HEALTH_HOST,
        health_port(),
        JebHealthState(source_count=len(collector.config.sources)),
    )
    collector.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
