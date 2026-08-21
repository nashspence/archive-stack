#!/usr/bin/env python3
"""Verify the reviewed role inventory against one already-built final image."""

from __future__ import annotations

import argparse
import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]


def _run(*command: str) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    args = parser.parse_args()
    document = tomllib.loads((REPO / "image-roles.toml").read_text(encoding="utf-8"))
    images = document["images"]
    if args.target not in images:
        raise SystemExit(f"no reviewed image-role inventory for {args.target}")
    item: dict[str, Any] = images[args.target]
    tag = str(item["tag"])
    config = json.loads(_run("docker", "image", "inspect", tag))[0]["Config"]
    if config["User"] != "65532:65532":
        raise SystemExit(f"{args.target}: final user is {config['User']!r}")

    installed = set(
        json.loads(
            _run(
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "python",
                tag,
                "-c",
                "import importlib.metadata as m,json;"
                "print(json.dumps(sorted({d.metadata['Name'].lower() "
                "for d in m.distributions()})))",
            )
        )
    )
    required = set(item["required_distributions"])
    forbidden = set(item["forbidden_distributions"])
    if missing := required - installed:
        raise SystemExit(f"{args.target}: missing distributions: {sorted(missing)}")
    if present := forbidden & installed:
        raise SystemExit(f"{args.target}: forbidden distributions: {sorted(present)}")

    command_names = [*item["required_commands"], *item["forbidden_commands"]]
    status = json.loads(
        _run(
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            tag,
            "-c",
            "import json,shutil,sys;"
            "print(json.dumps({name:bool(shutil.which(name)) for name in sys.argv[1:]}))",
            *command_names,
        )
    )
    missing_commands = [name for name in item["required_commands"] if not status[name]]
    forbidden_commands = [name for name in item["forbidden_commands"] if status[name]]
    if missing_commands:
        raise SystemExit(f"{args.target}: missing commands: {missing_commands}")
    if forbidden_commands:
        raise SystemExit(f"{args.target}: forbidden commands: {forbidden_commands}")
    print(
        json.dumps({"image": args.target, "roles": item["roles"], "status": "ok"}, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
