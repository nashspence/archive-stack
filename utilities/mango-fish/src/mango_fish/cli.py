from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from mango_fish.relay import MangoFish, load_config, summarize_config


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mango-fish",
        description="Relay durable CloudEvents logs to webhooks.",
    )
    result.add_argument("--config", type=Path, required=True)
    result.add_argument("--check", action="store_true")
    result.add_argument("--once", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)
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
