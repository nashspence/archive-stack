from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_security_policy_routes_private_reports_through_github() -> None:
    policy = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "GitHub's private vulnerability reporting" in policy
    assert "[Report a vulnerability]" in policy
    assert "https://github.com/nashspence/riverhog/security/advisories/new" in policy
