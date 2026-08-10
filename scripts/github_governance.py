#!/usr/bin/env python3
"""Verify the live GitHub controls declared by Riverhog's release contract."""

from __future__ import annotations

import argparse
import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "riverhog-github-governance/v1"
MAIN_RULESET = "Protect main"
RELEASE_RULESET = "Protect release/v1"
TAG_RULESET = "Protect v1 tags"


class GovernanceError(RuntimeError):
    """The live GitHub governance state differs from release.toml."""


def _gh(endpoint: str) -> Any:
    completed = subprocess.run(
        ["gh", "api", endpoint],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _rule(ruleset: dict[str, Any], rule_type: str) -> dict[str, Any]:
    matching = [rule for rule in ruleset.get("rules", []) if rule.get("type") == rule_type]
    if len(matching) != 1:
        raise GovernanceError(f"{ruleset.get('name', 'ruleset')} must contain one {rule_type} rule")
    return matching[0]


def _check_main_ruleset(ruleset: dict[str, Any]) -> None:
    if ruleset.get("target") != "branch" or ruleset.get("enforcement") != "active":
        raise GovernanceError("main ruleset must actively target branches")
    if ruleset.get("bypass_actors"):
        raise GovernanceError("main ruleset must not have bypass actors")
    ref_name = ruleset.get("conditions", {}).get("ref_name", {})
    if ref_name != {"exclude": [], "include": ["refs/heads/main"]}:
        raise GovernanceError("main ruleset must target only main")
    if {rule.get("type") for rule in ruleset.get("rules", [])} != {
        "deletion",
        "non_fast_forward",
        "required_linear_history",
    }:
        raise GovernanceError("main rules differ from the direct-delivery contract")


def _check_release_ruleset(
    ruleset: dict[str, Any], required_checks: list[str], required_check_integration_id: int
) -> None:
    if ruleset.get("target") != "branch" or ruleset.get("enforcement") != "active":
        raise GovernanceError("release/v1 ruleset must actively target branches")
    if ruleset.get("bypass_actors"):
        raise GovernanceError("release/v1 ruleset must not have bypass actors")
    ref_name = ruleset.get("conditions", {}).get("ref_name", {})
    if ref_name != {"exclude": [], "include": ["refs/heads/release/v1"]}:
        raise GovernanceError("release/v1 ruleset must target only release/v1")
    expected_types = {
        "deletion",
        "non_fast_forward",
        "pull_request",
        "required_linear_history",
        "required_status_checks",
    }
    if {rule.get("type") for rule in ruleset.get("rules", [])} != expected_types:
        raise GovernanceError("release/v1 rules differ from the release contract")
    pull_request = _rule(ruleset, "pull_request").get("parameters", {})
    expected_pull_request = {
        "allowed_merge_methods": ["squash", "rebase"],
        "dismiss_stale_reviews_on_push": False,
        "require_code_owner_review": False,
        "require_last_push_approval": False,
        "required_approving_review_count": 0,
        "required_reviewers": [],
        "required_review_thread_resolution": False,
    }
    if pull_request != expected_pull_request:
        raise GovernanceError("release/v1 pull-request policy differs from release.toml")
    statuses = _rule(ruleset, "required_status_checks").get("parameters", {})
    actual_checks = [item.get("context") for item in statuses.get("required_status_checks", [])]
    if actual_checks != required_checks:
        raise GovernanceError("release/v1 required checks differ from release.toml")
    if any(
        item.get("integration_id") != required_check_integration_id
        for item in statuses.get("required_status_checks", [])
    ):
        raise GovernanceError("release/v1 checks must come from the GitHub Actions application")
    if statuses.get("strict_required_status_checks_policy") is not True:
        raise GovernanceError("release/v1 checks must run against the current head")
    if statuses.get("do_not_enforce_on_create") is not True:
        raise GovernanceError("release/v1 creation must remain possible after the branch gate")


def _check_tag_ruleset(ruleset: dict[str, Any]) -> None:
    if ruleset.get("target") != "tag" or ruleset.get("enforcement") != "active":
        raise GovernanceError("v1 tag ruleset must actively target tags")
    if ruleset.get("bypass_actors"):
        raise GovernanceError("v1 tag ruleset must not have bypass actors")
    ref_name = ruleset.get("conditions", {}).get("ref_name", {})
    if ref_name != {"exclude": [], "include": ["refs/tags/v1.*"]}:
        raise GovernanceError("v1 tag ruleset must target only v1 tags")
    if {rule.get("type") for rule in ruleset.get("rules", [])} != {
        "deletion",
        "non_fast_forward",
    }:
        raise GovernanceError("v1 tags must reject deletion and non-fast-forward updates")


def _check_environment(
    repository: str,
    name: str,
    maintainer: str,
    expected_policies: set[tuple[str, str]],
) -> None:
    environment = _gh(f"repos/{repository}/environments/{name}")
    if environment.get("deployment_branch_policy") != {
        "custom_branch_policies": True,
        "protected_branches": False,
    }:
        raise GovernanceError(f"{name} must use explicit deployment branch policies")
    if environment.get("can_admins_bypass") is not False:
        raise GovernanceError(f"{name} must not permit administrator bypass")
    reviewer_rules = [
        rule
        for rule in environment.get("protection_rules", [])
        if rule.get("type") == "required_reviewers"
    ]
    if len(reviewer_rules) != 1:
        raise GovernanceError(f"{name} must require one reviewer rule")
    reviewer_rule = reviewer_rules[0]
    reviewers = reviewer_rule.get("reviewers", [])
    actual_reviewers = {
        reviewer.get("reviewer", {}).get("login")
        for reviewer in reviewers
        if reviewer.get("type") == "User"
    }
    if actual_reviewers != {maintainer} or reviewer_rule.get("prevent_self_review") is not False:
        raise GovernanceError(f"{name} must require explicit maintainer approval")
    policies = _gh(f"repos/{repository}/environments/{name}/deployment-branch-policies")
    actual_policies = {
        (policy.get("name"), policy.get("type")) for policy in policies.get("branch_policies", [])
    }
    if actual_policies != expected_policies:
        raise GovernanceError(f"{name} deployment refs differ from release.toml")


def check() -> dict[str, Any]:
    config = tomllib.loads((ROOT / "release.toml").read_text(encoding="utf-8"))
    governance = config["governance"]
    repository = str(governance["repository"])
    required_checks = list(governance["required_checks"])
    summaries = _gh(f"repos/{repository}/rulesets")
    by_name = {item["name"]: item for item in summaries}
    expected_names = {MAIN_RULESET, RELEASE_RULESET, TAG_RULESET}
    if not expected_names <= set(by_name):
        raise GovernanceError("GitHub lacks a declared main, release/v1, or v1 tag ruleset")
    main_ruleset = _gh(f"repos/{repository}/rulesets/{by_name[MAIN_RULESET]['id']}")
    release_ruleset = _gh(f"repos/{repository}/rulesets/{by_name[RELEASE_RULESET]['id']}")
    tag_ruleset = _gh(f"repos/{repository}/rulesets/{by_name[TAG_RULESET]['id']}")
    _check_main_ruleset(main_ruleset)
    _check_release_ruleset(
        release_ruleset,
        required_checks,
        int(governance["required_check_integration_id"]),
    )
    _check_tag_ruleset(tag_ruleset)

    environments = governance["environments"]
    maintainer = str(governance["maintainer"])
    _check_environment(
        repository,
        str(environments["release"]),
        maintainer,
        {("release/v1", "branch"), ("v1.*", "tag")},
    )
    _check_environment(
        repository,
        str(environments["pages"]),
        maintainer,
        {("release/v1", "branch"), ("gh-pages", "branch")},
    )
    return {
        "schema": SCHEMA,
        "repository": repository,
        "source_sha": _git_sha(),
        "main": "protected-direct-delivery",
        "release_branch": "protected-pull-request-delivery",
        "required_checks": required_checks,
        "tag_policy": "immutable-v1",
        "environments": sorted(environments.values()),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check",))
    parser.add_argument("--summary", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = check()
    except (GovernanceError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"governance error: {exc}") from exc
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.summary is not None:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
