from __future__ import annotations

import argparse
import logging
import os
import sys

from jeb.collector import (
    Collector,
    UnrecoverableJebError,
    config_from_env,
)
from jeb.health import JebHealthState, start_health_server

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
        prog="jeb",
        description="Weekly collector and automated uploader.",
        epilog=(
            "commands:\n"
            "  run           run continuously and process eligible batches\n"
            "  once          discover and process one scheduler pass\n"
            "  archive-now   archive one account immediately\n"
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
