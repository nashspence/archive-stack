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
    parser = argparse.ArgumentParser(
        prog="jeb",
        description="Weekly collector and automated uploader.",
        epilog=(
            "commands:\n"
            "  run           run continuously and process eligible batches\n"
            "  once          discover and process one scheduler pass\n"
            "  check-config  validate configuration and initialize state"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        choices=["run", "once", "check-config"],
        nargs="?",
        default="run",
        help="Collector action to run; defaults to run.",
    )
    parser.add_argument(
        "--config",
        default=os.getenv("JEB_CONFIG", "/config/jeb.toml"),
        help="Path to the Jeb TOML configuration.",
    )
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
