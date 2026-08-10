from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"
QUALIFICATION_WORKFLOW = REPO_ROOT / ".github/workflows/release-qualification.yml"
MISE_LOCK = REPO_ROOT / "mise.lock"


def test_ci_uses_thin_repository_and_image_build_adapters() -> None:
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)

    assert workflow["on"] == {
        "pull_request": "",
        "push": {"branches": ["main", "release/v1"]},
        "workflow_dispatch": "",
        "workflow_call": {
            "inputs": {
                "ref": {
                    "description": "Exact commit to check out and validate.",
                    "required": "false",
                    "type": "string",
                }
            }
        },
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "true"

    assert set(workflow["jobs"]) == {
        "repository",
        "provenance-observers",
        "images",
    }
    job = workflow["jobs"]["repository"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["strategy"]["fail-fast"] == "false"
    matrix = job["strategy"]["matrix"]["include"]
    assert [entry["target"] for entry in matrix] == [
        "lint",
        "compile",
        "unit",
        "spec",
        "c2sp-vectors",
        "postgres-concurrency",
        "compose-smoke",
        "dist-smoke",
    ]
    assert [entry["target"] for entry in matrix if entry.get("docker") == "true"] == [
        "postgres-concurrency",
        "compose-smoke",
    ]

    steps = job["steps"]
    assert [step["uses"].split("@", 1)[0] for step in steps if "uses" in step] == [
        "actions/checkout",
        "jdx/mise-action",
        "docker/setup-docker-action",
        "docker/setup-compose-action",
    ]
    action_steps = [
        step
        for workflow_job in workflow["jobs"].values()
        for step in workflow_job["steps"]
        if "uses" in step
    ]
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"]) for step in action_steps)
    checkout_steps = [
        step
        for workflow_job in workflow["jobs"].values()
        for step in workflow_job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert all(
        step["with"]
        == {
            "persist-credentials": "false",
            "ref": "${{ inputs.ref || github.sha }}",
        }
        for step in checkout_steps
    )
    assert steps[2]["if"] == "matrix.docker"
    assert steps[2]["with"]["version"] == "v29.3.1"
    assert json.loads(steps[2]["with"]["daemon-config"]) == {
        "features": {"containerd-snapshotter": True}
    }
    assert steps[3]["if"] == "matrix.docker"
    assert steps[3]["with"] == {"version": "v5.1.1"}
    assert [step["run"] for step in steps if "run" in step] == ['make "$CI_TARGET"']
    assert steps[-1]["env"] == {"CI_TARGET": "${{ matrix.target }}"}

    observers = workflow["jobs"]["provenance-observers"]
    assert observers["strategy"] == {
        "fail-fast": "false",
        "matrix": {
            "os": ["ubuntu-24.04", "macos-15", "windows-2025"],
        },
    }
    assert observers["runs-on"] == "${{ matrix.os }}"
    assert [step["uses"].split("@", 1)[0] for step in observers["steps"] if "uses" in step] == [
        "actions/checkout",
        "jdx/mise-action",
    ]
    assert observers["steps"][0]["with"]["persist-credentials"] == "false"
    assert observers["steps"][1]["with"] == {"install_args": "python uv"}
    assert [step["run"] for step in observers["steps"] if "run" in step] == [
        "mise x -- uv run --locked --all-packages --group dev "
        "python -m pytest -q "
        "packages/riverhog-provenance/tests/test_platform_live.py"
    ]
    assert "secrets." not in text


def test_release_qualification_reuses_ci_and_publishes_only_sha_bound_summaries() -> None:
    text = QUALIFICATION_WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)

    assert workflow["on"]["schedule"] == [{"cron": "17 7 1,15 * *"}]
    assert workflow["permissions"] == {
        "actions": "read",
        "checks": "read",
        "contents": "read",
        "deployments": "read",
    }
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    assert set(workflow["jobs"]) == {"resolve", "ci", "release-audit"}
    assert workflow["jobs"]["ci"] == {
        "name": "required checks",
        "needs": "resolve",
        "uses": "./.github/workflows/ci.yml",
        "with": {"ref": "${{ needs.resolve.outputs.sha }}"},
        "permissions": {"contents": "read"},
    }
    audit = workflow["jobs"]["release-audit"]
    assert "environment" not in audit
    assert audit["needs"] == ["resolve", "ci"]
    assert audit["env"]["SOURCE_SHA"] == "${{ needs.resolve.outputs.sha }}"
    assert audit["env"]["SOURCE_REF"] == "${{ needs.resolve.outputs.ref }}"
    assert audit["env"]["RIVERHOG_RELEASE_GHA_CACHE"] == "true"
    resolve_source = next(
        step
        for step in workflow["jobs"]["resolve"]["steps"]
        if step["name"] == "Resolve the selected ref once"
    )
    assert resolve_source["env"]["WORKFLOW_REF"] == "${{ github.ref }}"
    assert '[[ "$WORKFLOW_REF" != refs/heads/main ]]' in resolve_source["run"]
    audit_checkout = next(
        step for step in audit["steps"] if step["name"] == "Check out workflow authority"
    )
    assert audit_checkout["with"] == {
        "fetch-depth": "0",
        "persist-credentials": "false",
    }
    exact_checkout = next(
        step for step in audit["steps"] if step["name"] == "Check out verified exact source"
    )
    assert exact_checkout["run"] == (
        'git fetch --force --no-tags origin "$SOURCE_SHA"\n'
        'git checkout --detach "$SOURCE_SHA"\n'
        'test "$(git rev-parse --verify HEAD)" = "$SOURCE_SHA"\n'
    )
    assert all(
        re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
        for step in workflow["jobs"]["resolve"]["steps"] + audit["steps"]
        if "uses" in step
    )
    upload = next(step for step in audit["steps"] if step["name"] == "Upload SHA-bound summaries")
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"]["path"] == "release-qualification/*.json"
    assert "published == false" in text
    assert "riverhog-release-qualification/v1" in text
    assert "Analyze (actions)" in text and "Analyze (python)" in text
    assert "release/v1" in text
    assert "v1\\.[0-9]+\\.[0-9]+" in text


def test_release_required_check_names_are_derived_from_stable_job_names() -> None:
    workflow = yaml.load(CI_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    release = tomllib.loads((REPO_ROOT / "release.toml").read_text(encoding="utf-8"))
    repository = workflow["jobs"]["repository"]
    images = workflow["jobs"]["images"]
    observers = workflow["jobs"]["provenance-observers"]
    actual = {
        *(
            repository["name"].replace("${{ matrix.target }}", entry["target"])
            for entry in repository["strategy"]["matrix"]["include"]
        ),
        *(
            images["name"].replace("${{ matrix.target }}", target)
            for target in images["strategy"]["matrix"]["target"]
        ),
        *(
            observers["name"].replace("${{ matrix.os }}", os_name)
            for os_name in observers["strategy"]["matrix"]["os"]
        ),
        "Analyze (actions)",
        "Analyze (python)",
    }

    assert release["governance"]["required_checks"] == sorted(actual)


def test_provenance_observer_toolchain_is_locked_for_every_matrix_os() -> None:
    lock = tomllib.loads(MISE_LOCK.read_text(encoding="utf-8"))

    for tool in ("python", "uv"):
        entries = lock["tools"][tool]
        assert len(entries) == 1
        platforms = {
            key.removeprefix("platforms."): value
            for key, value in entries[0].items()
            if key.startswith("platforms.")
        }
        assert set(platforms) == {"linux-x64", "macos-arm64", "windows-x64"}
        windows = platforms["windows-x64"]
        assert windows["url"].startswith("https://github.com/")
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", windows["checksum"])
