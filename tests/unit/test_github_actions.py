from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"


def test_ci_uses_thin_repository_and_image_build_adapters() -> None:
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)

    assert workflow["on"] == {
        "pull_request": "",
        "push": {"branches": ["main"]},
        "workflow_dispatch": "",
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
    assert steps[0]["with"]["persist-credentials"] == "false"
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
    assert [step["run"] for step in observers["steps"] if "run" in step] == [
        "mise x -- uv run --locked --all-packages --group dev "
        "python -m pytest -q "
        "packages/riverhog-provenance/tests/test_platform_live.py"
    ]
    assert "secrets." not in text
