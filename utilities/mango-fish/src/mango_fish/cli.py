from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import sys
from pathlib import Path

from state_schema import StateSchemaError

from mango_fish.relay import MangoFish, load_config, summarize_config
from mango_fish.schema import state_schema


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mango-fish",
        description="Relay durable CloudEvents logs to webhooks.",
    )
    result.add_argument(
        "--version",
        action="version",
        version=importlib.metadata.version("mango-fish"),
    )
    result.add_argument("--config", type=Path, required=True)
    result.add_argument("--check", action="store_true")
    result.add_argument("--once", action="store_true")
    subparsers = result.add_subparsers(dest="command")
    state = subparsers.add_parser("state", help="inspect or explicitly upgrade cursor state")
    state_subparsers = state.add_subparsers(dest="state_command", required=True)
    for command_name, help_text in (
        ("status", "show the current and required state revisions"),
        ("upgrade", "explicitly upgrade state to the current revision"),
        ("verify", "verify the current revision and exact state schema"),
    ):
        command_parser = state_subparsers.add_parser(command_name, help=help_text)
        command_parser.add_argument("--json", action="store_true", help="Emit JSON.")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)
    if args.command == "state":
        schema = state_schema(config.state_path)
        try:
            if args.state_command == "status":
                status = schema.status()
            elif args.state_command == "upgrade":
                status = schema.upgrade()
            else:
                status = schema.validate()
        except StateSchemaError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        payload = status.as_dict()
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(
                f"mango-fish state: {payload['condition']} "
                f"({payload['current_revision'] or 'none'} -> {payload['head_revision']})"
            )
        return 0
    if args.check:
        print(json.dumps(summarize_config(config), sort_keys=True))
        return 0
    relay = MangoFish(config)
    if args.once:
        relay.run_once()
    else:
        relay.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
