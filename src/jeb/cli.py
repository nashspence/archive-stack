from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from jeb.collector import Collector, load_config


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.getenv("JEB_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(prog="jeb")
    parser.add_argument(
        "command",
        choices=["run", "once", "check-config"],
        nargs="?",
        default="run",
    )
    parser.add_argument("--config", default=os.getenv("JEB_CONFIG", "/config/jeb.toml"))
    args = parser.parse_args(argv)

    collector = Collector(load_config(Path(args.config)))
    if args.command == "check-config":
        collector.init_db()
        print(f"ok: {len(collector.config.sources)} sources")
        return 0
    if args.command == "once":
        collector.run_once()
        return 0
    collector.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
