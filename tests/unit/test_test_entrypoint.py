from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_ENTRYPOINT = REPO_ROOT / "test"


def _install_fake_docker(tmp_path: Path) -> Path:
    docker_bin = tmp_path / "bin"
    docker_bin.mkdir()
    log_path = tmp_path / "docker.log"
    docker = docker_bin / "docker"
    docker.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f"printf '%s|%s\\n' \"${{COMPOSE_PROJECT_NAME:-}}\" \"$*\" >> {log_path}",
            ]
        )
        + "\n"
    )
    docker.chmod(0o755)
    return log_path


def _run_test_entrypoint(
    tmp_path: Path, *args: str, extra_env: dict[str, str] | None = None
) -> list[str]:
    log_path = _install_fake_docker(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path / 'bin'}:{env['PATH']}"
    if extra_env:
        env.update(extra_env)

    completed = subprocess.run(
        ["bash", str(TEST_ENTRYPOINT), *args],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    return log_path.read_text().splitlines()


def test_spec_lane_uses_isolated_compose_project_name(tmp_path: Path) -> None:
    log_lines = _run_test_entrypoint(tmp_path, "spec", "-k", "glacier")

    project_names = {line.split("|", 1)[0] for line in log_lines}
    assert len(project_names) == 1

    project_name = next(iter(project_names))
    assert re.fullmatch(r"archive-stack-test-[a-z0-9]+(?:-[a-z0-9]+)*-\d+", project_name)

    assert any(" build test" in line for line in log_lines)
    assert any(" run --rm" in line for line in log_lines)
    assert any(" down --remove-orphans" in line for line in log_lines)
    assert not any(" down --volumes --remove-orphans" in line for line in log_lines)


def test_test_compose_project_name_override_is_respected(tmp_path: Path) -> None:
    log_lines = _run_test_entrypoint(
        tmp_path,
        "spec",
        extra_env={"TEST_COMPOSE_PROJECT_NAME": "archive-stack-shared"},
    )

    project_names = {line.split("|", 1)[0] for line in log_lines}
    assert project_names == {"archive-stack-shared"}
