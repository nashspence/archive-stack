from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/github_governance.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("riverhog_github_governance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _release_ruleset(required_checks: list[str]) -> dict[str, object]:
    return {
        "name": "Protect release/v1",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"exclude": [], "include": ["refs/heads/release/v1"]}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "required_linear_history"},
            {
                "type": "pull_request",
                "parameters": {
                    "allowed_merge_methods": ["squash", "rebase"],
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_approving_review_count": 0,
                    "required_reviewers": [],
                    "required_review_thread_resolution": False,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": context, "integration_id": 15368} for context in required_checks
                    ],
                    "strict_required_status_checks_policy": True,
                    "do_not_enforce_on_create": True,
                },
            },
        ],
    }


def _environment(*, reviewer_required: bool) -> dict[str, object]:
    protection_rules: list[dict[str, object]] = [{"type": "branch_policy"}]
    if reviewer_required:
        protection_rules.append(
            {
                "type": "required_reviewers",
                "prevent_self_review": False,
                "reviewers": [
                    {
                        "type": "User",
                        "reviewer": {"login": "nashspence"},
                    }
                ],
            }
        )
    return {
        "can_admins_bypass": False,
        "deployment_branch_policy": {
            "custom_branch_policies": True,
            "protected_branches": False,
        },
        "protection_rules": protection_rules,
    }


def test_release_ruleset_accepts_the_exact_declared_check_contract() -> None:
    module = load_script()
    required_checks = ["Analyze (actions)", "make unit"]

    module._check_release_ruleset(_release_ruleset(required_checks), required_checks, 15368)


def test_release_ruleset_rejects_check_name_drift() -> None:
    module = load_script()
    ruleset = _release_ruleset(["make unit"])

    with pytest.raises(module.GovernanceError, match="required checks differ"):
        module._check_release_ruleset(ruleset, ["make lint", "make unit"], 15368)


def test_main_policy_preserves_authorized_direct_delivery() -> None:
    module = load_script()
    ruleset: dict[str, Any] = {
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"exclude": [], "include": ["refs/heads/main"]}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "required_linear_history"},
        ],
    }

    module._check_main_ruleset(ruleset)

    ruleset["rules"].append({"type": "required_status_checks"})
    with pytest.raises(module.GovernanceError, match="direct-delivery contract"):
        module._check_main_ruleset(ruleset)


def test_v1_tag_policy_is_immutable() -> None:
    module = load_script()
    ruleset = {
        "target": "tag",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"exclude": [], "include": ["refs/tags/v1.*"]}},
        "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}],
    }

    module._check_tag_ruleset(ruleset)


@pytest.mark.parametrize("reviewer_required", [False, True])
def test_environment_policy_supports_gated_and_unattended_jobs(
    reviewer_required: bool,
) -> None:
    module = load_script()

    def gh(endpoint: str) -> dict[str, object]:
        if endpoint.endswith("/deployment-branch-policies"):
            return {"branch_policies": [{"name": "main", "type": "branch"}]}
        return _environment(reviewer_required=reviewer_required)

    module._gh = gh
    module._check_environment(
        "nashspence/riverhog",
        "provider-qualification",
        "nashspence",
        {("main", "branch")},
        reviewer_required=reviewer_required,
    )


def test_unattended_environment_rejects_an_unexpected_reviewer_gate() -> None:
    module = load_script()
    module._gh = lambda _endpoint: _environment(reviewer_required=True)

    with pytest.raises(module.GovernanceError, match="protection rules differ"):
        module._check_environment(
            "nashspence/riverhog",
            "provider-qualification",
            "nashspence",
            {("main", "branch")},
            reviewer_required=False,
        )
