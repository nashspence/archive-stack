from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jeb.collector import (
    Collector,
    load_config,
    safe_capture_signature_for_file,
    stable_json,
)

DEFAULT_CONFIG = os.getenv("JEB_CONFIG", "/config/jeb.toml")


def config_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="Path to the Jeb TOML configuration.",
    )
    return parent


def row_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    for key in ("signature_json", "example_paths_json"):
        value = payload.get(key)
        if isinstance(value, str):
            payload[key.removesuffix("_json")] = json.loads(value)
            del payload[key]
    return payload


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def format_mtime_ns(value: object) -> str:
    try:
        seconds = int(str(value)) / 1_000_000_000
    except (TypeError, ValueError):
        return "-"
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def iter_probe_paths(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        expanded = path.expanduser()
        if expanded.is_dir():
            for child in sorted(expanded.rglob("*")):
                if child.is_file():
                    yield child
        elif expanded.is_file():
            yield expanded


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
            "  check-config  validate configuration and initialize state\n"
            "  signatures    inspect held capture signatures"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parent = config_parent()
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", parents=[parent], help="run continuously and process eligible batches")
    sub.add_parser("once", parents=[parent], help="discover and process one scheduler pass")
    sub.add_parser(
        "check-config",
        parents=[parent],
        help="validate configuration and initialize state",
    )
    signatures = sub.add_parser(
        "signatures",
        parents=[parent],
        help="inspect held capture signatures",
    )
    signatures_sub = signatures.add_subparsers(dest="signatures_command", required=True)
    signatures_list = signatures_sub.add_parser(
        "list",
        parents=[parent],
        help="list held capture signatures",
    )
    signatures_list.add_argument("--source", help="Limit results to one source id.")
    signatures_list.add_argument(
        "--state",
        choices=["held", "resolved", "all"],
        default="held",
        help="Held signature state to list.",
    )
    signatures_list.add_argument("--json", action="store_true", help="Emit JSON.")
    signatures_show = signatures_sub.add_parser(
        "show",
        parents=[parent],
        help="show one held capture signature",
    )
    signatures_show.add_argument("signature_id")
    signatures_show.add_argument("--source", help="Disambiguate by source id.")
    signatures_show.add_argument("--json", action="store_true", help="Emit JSON.")
    signatures_probe = signatures_sub.add_parser(
        "probe",
        parents=[parent],
        help="probe files and print their capture signatures",
    )
    signatures_probe.add_argument("paths", nargs="+", type=Path)
    signatures_probe.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args(argv)
    command = args.command or "run"

    if command == "signatures" and args.signatures_command == "probe":
        probe_rows = []
        for path in iter_probe_paths(args.paths):
            signature = safe_capture_signature_for_file(path)
            probe_rows.append(
                {
                    "path": str(path),
                    "signature_id": signature.id,
                    "signature": dict(signature.data),
                }
            )
        if args.json:
            print_json(probe_rows)
        else:
            for probe_row in probe_rows:
                print(
                    f"{probe_row['signature_id']}\t{probe_row['path']}\t"
                    f"{stable_json(probe_row['signature'])}"
                )
        return 0

    collector = Collector(load_config(Path(args.config)))
    if command == "check-config":
        collector.init_db()
        print(f"ok: {len(collector.config.sources)} sources")
        return 0
    if command == "once":
        collector.run_once()
        return 0
    if command == "signatures":
        collector.init_db()
        if args.signatures_command == "list":
            signature_rows = collector.held_signatures(source_id=args.source, state=args.state)
            if args.json:
                print_json([row_payload(held_row) for held_row in signature_rows])
            else:
                for held_row in signature_rows:
                    print(
                        "\t".join(
                            [
                                str(held_row["signature_id"]),
                                str(held_row["state"]),
                                str(held_row["source_id"]),
                                str(held_row["reason"]),
                                str(held_row["file_count"]),
                                str(held_row["total_bytes"]),
                                format_mtime_ns(held_row["oldest_mtime_ns"]),
                                str(json.loads(str(held_row["example_paths_json"]))[:3]),
                            ]
                        )
                    )
            return 0
        if args.signatures_command == "show":
            signature_row = collector.held_signature(args.signature_id, source_id=args.source)
            payload = row_payload(signature_row)
            if args.json:
                print_json(payload)
            else:
                for key, value in payload.items():
                    print(f"{key}: {value}")
            return 0
    collector.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
