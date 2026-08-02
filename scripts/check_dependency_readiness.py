#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import unquote


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def load_lock(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def registry_versions(lock: dict[str, Any]) -> set[tuple[str, str]]:
    versions: set[tuple[str, str]] = set()
    for package in lock["package"]:
        if "registry" not in package.get("source", {}):
            continue
        name = canonical_name(str(package["name"]))
        version = str(package["version"])
        versions.add((name, version))
    return versions


def sbom_registry_versions(sbom_response: dict[str, Any]) -> set[tuple[str, str]]:
    versions: set[tuple[str, str]] = set()
    for package in sbom_response["sbom"]["packages"]:
        for reference in package.get("externalRefs", []):
            locator = str(reference.get("referenceLocator", ""))
            if not locator.startswith("pkg:pypi/"):
                continue
            value = locator.removeprefix("pkg:pypi/").split("?", 1)[0].split("#", 1)[0]
            if "@" not in value:
                continue
            name, version = value.rsplit("@", 1)
            versions.add((canonical_name(unquote(name)), unquote(version)))
    return versions


def readiness_errors(
    lock: dict[str, Any],
    sbom_response: dict[str, Any],
    open_alerts: list[dict[str, Any]],
) -> list[str]:
    locked = registry_versions(lock)
    graphed = sbom_registry_versions(sbom_response)
    locked_names = {name for name, _version in locked}
    missing = sorted(locked - graphed)
    stale = sorted((name, version) for name, version in graphed - locked if name in locked_names)

    errors: list[str] = []
    if missing:
        errors.append(
            "dependency graph is missing locked versions: "
            + ", ".join(f"{name}=={version}" for name, version in missing)
        )
    if stale:
        errors.append(
            "dependency graph contains obsolete locked versions: "
            + ", ".join(f"{name}=={version}" for name, version in stale)
        )
    if open_alerts:
        alerts = sorted(
            {canonical_name(str(alert["dependency"]["package"]["name"])) for alert in open_alerts}
        )
        errors.append(f"Dependabot has {len(open_alerts)} open alerts for: " + ", ".join(alerts))
    return errors


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(3):
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode == 0:
            return result
        if attempt < 2:
            time.sleep(attempt + 1)
    assert result is not None
    raise subprocess.CalledProcessError(
        result.returncode,
        command,
        output=result.stdout,
        stderr=result.stderr,
    )


def _run_json(command: list[str]) -> Any:
    result = _run(command)
    return json.loads(result.stdout)


def _repository() -> str:
    result = _run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
    return result.stdout.strip()


def _open_alerts(repository: str) -> list[dict[str, Any]]:
    result = _run(
        [
            "gh",
            "api",
            "--method",
            "GET",
            "--paginate",
            f"repos/{repository}/dependabot/alerts?state=open&per_page=100",
            "--jq",
            ".[] | @json",
        ]
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify GitHub's exact uv.lock dependency graph and alert state."
    )
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--repository")
    args = parser.parse_args()

    lock = load_lock(args.lock)
    repository = args.repository or _repository()
    sbom = _run_json(["gh", "api", f"repos/{repository}/dependency-graph/sbom"])
    errors = readiness_errors(lock, sbom, _open_alerts(repository))
    if errors:
        for error in errors:
            print(error)
        return 1

    print(
        f"Dependency graph matches all {len(registry_versions(lock))} exact uv.lock "
        "registry resolutions; Dependabot has no open alerts."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
