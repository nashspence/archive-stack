from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from jeb.collector import (
    Collector,
    UnrecoverableJebError,
    load_config,
)
from jeb.health import JebHealthState, start_health_server

DEFAULT_CONFIG = os.getenv("JEB_CONFIG", "/config/jeb.toml")
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


def config_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="Path to the Jeb TOML configuration.",
    )
    return parent


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
            "  archive-now   retry one source immediately after route repair\n"
            "  check-config  validate configuration and initialize state"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parent = config_parent()
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", parents=[parent], help="run continuously and process eligible batches")
    sub.add_parser("once", parents=[parent], help="discover and process one scheduler pass")
    archive_now = sub.add_parser(
        "archive-now",
        parents=[parent],
        help="retry one source immediately after route repair",
    )
    archive_now.add_argument("--source", required=True, help="Source id to retry.")
    archive_now.add_argument(
        "--collection",
        help="Collection id when the source belongs to multiple enabled collections.",
    )
    archive_now.add_argument(
        "--no-process",
        action="store_true",
        help="Create an eligible batch but do not process it in this command.",
    )
    sub.add_parser(
        "check-config",
        parents=[parent],
        help="validate configuration and initialize state",
    )
    args = parser.parse_args(argv)
    command = args.command or "run"

    collector = Collector(load_config(Path(args.config)))
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
                source_id=args.source,
                collection_id=args.collection,
                process=not args.no_process,
            )
        except UnrecoverableJebError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if batch_id is None:
            print(f"routing preflight still failed or no eligible files for source {args.source}")
            return 1
        print(f"archive attempt started for source {args.source}: {batch_id}")
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
