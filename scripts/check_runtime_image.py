#!/usr/bin/env python3
"""Verify one built runtime image from executable repository metadata."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
FORBIDDEN_RUNTIME_COMMANDS = ("cc", "gcc", "git", "make", "mise", "uv")


def _run(*command: str) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _bake_target(target: str) -> tuple[str, Path]:
    graph = json.loads(
        _run(
            "docker",
            "buildx",
            "bake",
            "--file",
            str(REPO / "docker-bake.hcl"),
            "--print",
            target,
        )
    )
    item: dict[str, Any] = graph["target"][target]
    tags = item["tags"]
    if len(tags) != 1:
        raise SystemExit(f"{target}: expected exactly one local image tag")
    return str(tags[0]), REPO / str(item["dockerfile"])


def _requested_distributions(dockerfile: Path) -> set[str]:
    text = dockerfile.read_text(encoding="utf-8")
    requested = {
        _canonical_name(name) for name in re.findall(r"--package\s+([A-Za-z0-9._-]+)", text)
    }
    if not requested:
        raise SystemExit(f"{dockerfile.relative_to(REPO)}: no runtime package selection")
    return requested


def _workspace_distributions() -> set[str]:
    distributions: set[str] = set()
    for path in REPO.rglob("pyproject.toml"):
        if {".venv", "build", "dist"}.intersection(path.relative_to(REPO).parts):
            continue
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        project = document.get("project")
        if project is not None:
            distributions.add(_canonical_name(str(project["name"])))
    return distributions


def _installed_distributions(tag: str) -> dict[str, dict[str, Any]]:
    code = (
        "import importlib.metadata as m,json;"
        "print(json.dumps([{'name':d.metadata['Name'],'requires':d.requires or [],"
        "'scripts':sorted(e.name for e in d.entry_points if e.group=='console_scripts')}"
        " for d in m.distributions()]))"
    )
    documents = json.loads(_run("docker", "run", "--rm", "--entrypoint", "python", tag, "-c", code))
    return {_canonical_name(str(document["name"])): document for document in documents}


def _requirement_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    if match is None:
        raise SystemExit(f"invalid installed requirement: {requirement!r}")
    return _canonical_name(match.group())


def _workspace_dependency_closure(
    roots: set[str],
    installed: dict[str, dict[str, Any]],
    workspace: set[str],
) -> set[str]:
    closure: set[str] = set()
    pending = list(roots)
    while pending:
        distribution = pending.pop()
        if distribution in closure:
            continue
        if distribution not in installed:
            raise SystemExit(f"missing requested distribution: {distribution}")
        closure.add(distribution)
        for requirement in installed[distribution]["requires"]:
            dependency = _requirement_name(str(requirement))
            if dependency in workspace and dependency not in closure:
                pending.append(dependency)
    return closure


def _command_status(tag: str, commands: list[str]) -> dict[str, bool]:
    code = (
        "import json,shutil,sys;"
        "print(json.dumps({name:bool(shutil.which(name)) for name in sys.argv[1:]}))"
    )
    return json.loads(
        _run("docker", "run", "--rm", "--entrypoint", "python", tag, "-c", code, *commands)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    args = parser.parse_args()

    tag, dockerfile = _bake_target(args.target)
    config = json.loads(_run("docker", "image", "inspect", tag))[0]["Config"]
    if config["User"] != "65532:65532":
        raise SystemExit(f"{args.target}: final user is {config['User']!r}")

    roots = _requested_distributions(dockerfile)
    workspace = _workspace_distributions()
    installed = _installed_distributions(tag)
    expected = _workspace_dependency_closure(roots, installed, workspace)
    actual = set(installed).intersection(workspace)
    if missing := expected - actual:
        raise SystemExit(f"{args.target}: missing workspace dependencies: {sorted(missing)}")
    if unexpected := actual - expected:
        raise SystemExit(f"{args.target}: unrelated workspace distributions: {sorted(unexpected)}")

    root_scripts = sorted({str(script) for root in roots for script in installed[root]["scripts"]})
    if not root_scripts:
        raise SystemExit(f"{args.target}: requested distributions expose no runtime command")
    statuses = _command_status(tag, [*root_scripts, *FORBIDDEN_RUNTIME_COMMANDS])
    if missing_scripts := [script for script in root_scripts if not statuses[script]]:
        raise SystemExit(f"{args.target}: missing runtime commands: {missing_scripts}")
    if build_tools := [name for name in FORBIDDEN_RUNTIME_COMMANDS if statuses[name]]:
        raise SystemExit(f"{args.target}: build-only commands in runtime image: {build_tools}")

    print(
        json.dumps(
            {
                "image": args.target,
                "requested_distributions": sorted(roots),
                "runtime_commands": root_scripts,
                "status": "ok",
                "workspace_dependency_closure": sorted(expected),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
