from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"


def test_ci_is_a_thin_adapter_over_repository_make_targets() -> None:
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)

    assert set(workflow["on"]) == {"pull_request", "push", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "true"

    assert set(workflow["jobs"]) == {"repository"}
    job = workflow["jobs"]["repository"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["strategy"]["fail-fast"] == "false"
    matrix = job["strategy"]["matrix"]["include"]
    assert [entry["target"] for entry in matrix] == [
        "lint",
        "format-check",
        "compile",
        "unit",
        "spec",
        "c2sp-vectors",
        "postgres-concurrency",
        "compose-smoke",
        "dist-smoke",
        "build",
    ]
    assert [entry["target"] for entry in matrix if entry.get("docker") == "true"] == [
        "postgres-concurrency",
        "compose-smoke",
        "build",
    ]

    steps = job["steps"]
    assert [step["uses"].split("@", 1)[0] for step in steps if "uses" in step] == [
        "actions/checkout",
        "jdx/mise-action",
        "docker/setup-docker-action",
        "docker/setup-compose-action",
    ]
    assert all(
        re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"]) for step in steps if "uses" in step
    )
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
    assert "secrets." not in text
