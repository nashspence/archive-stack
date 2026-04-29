from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_ENTRYPOINT = REPO_ROOT / "test"


def _install_fake_command(tmp_path: Path, name: str, log_name: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log_path = tmp_path / log_name
    command = bin_dir / name
    if name == "docker":
        command.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    (
                        "printf '%s|%s|%s\\n' "
                        "\"${COMPOSE_PROJECT_NAME:-}\" "
                        "\"${ARC_ENABLE_TEST_CONTROL:-}\" "
                        "\"$*\" >> "
                        f"{log_path}"
                    ),
                    (
                        "if [[ \"$*\" == *"
                        "\" exec -T garage /garage -c /etc/garage.toml node id\"* ]]; then"
                    ),
                    "  printf 'fake-node@garage\\n'",
                    "fi",
                ]
            )
            + "\n"
        )
    else:
        command.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    (
                        "printf '%s|%s|%s\\n' "
                        "\"${COMPOSE_PROJECT_NAME:-}\" "
                        "\"${ARC_ENABLE_TEST_CONTROL:-}\" "
                        "\"$*\" >> "
                        f"{log_path}"
                    ),
                ]
            )
            + "\n"
        )
    command.chmod(0o755)
    return log_path


def _run_test_entrypoint(
    tmp_path: Path, *args: str, extra_env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    docker_log_path = _install_fake_command(tmp_path, "docker", "docker.log")
    uv_log_path = _install_fake_command(tmp_path, "uv", "uv.log")
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

    return completed, docker_log_path, uv_log_path


def _read_log_lines(log_path: Path) -> list[str]:
    if not log_path.exists():
        return []
    return log_path.read_text().splitlines()


@pytest.mark.parametrize(
    ("args", "expected_target"),
    [
        (("spec", "-k", "glacier"), "tests/harness/test_spec_harness.py -k glacier"),
        (("unit", "-k", "entrypoint"), "tests/unit -k entrypoint"),
        (
            ("fast", "-k", "entrypoint"),
            "tests/harness/test_spec_harness.py tests/unit -k entrypoint",
        ),
    ],
)
def test_nonproduction_lanes_run_in_local_uv_env(
    tmp_path: Path, args: tuple[str, ...], expected_target: str
) -> None:
    completed, docker_log_path, uv_log_path = _run_test_entrypoint(tmp_path, *args)

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(docker_log_path) == []

    uv_log_lines = _read_log_lines(uv_log_path)
    assert len(uv_log_lines) == 1
    assert all(line.split("|", 2)[1] == "" for line in uv_log_lines)
    assert (
        "run --python 3.11 --isolated --with-requirements "
        f"{REPO_ROOT / 'requirements-test.txt'} --with-editable .[db] "
        "python -m pytest -q "
    ) in uv_log_lines[0]
    assert expected_target in uv_log_lines[0]


def test_prod_lane_uses_isolated_compose_project_name(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_test_entrypoint(
        tmp_path, "prod", "-k", "glacier"
    )

    assert completed.returncode == 0, completed.stderr
    log_lines = _read_log_lines(docker_log_path)

    project_names = {line.split("|", 1)[0] for line in log_lines}
    assert len(project_names) == 1

    project_name = next(iter(project_names))
    assert re.fullmatch(r"archive-stack-test-[a-z0-9]+(?:-[a-z0-9]+)*-\d+", project_name)

    assert any(" build test" in line for line in log_lines)
    assert any(" build app" in line for line in log_lines)
    assert any(" run --rm" in line for line in log_lines)
    assert any(" down --volumes --remove-orphans" in line for line in log_lines)
    assert _read_log_lines(uv_log_path) == []


def test_test_compose_project_name_override_is_respected_for_prod_lane(
    tmp_path: Path,
) -> None:
    completed, docker_log_path, _ = _run_test_entrypoint(
        tmp_path, "prod", extra_env={"TEST_COMPOSE_PROJECT_NAME": "archive-stack-shared"}
    )

    assert completed.returncode == 0, completed.stderr
    log_lines = _read_log_lines(docker_log_path)
    project_names = {line.split("|", 1)[0] for line in log_lines}
    assert project_names == {"archive-stack-shared"}


def test_help_describes_parallel_recommendation_and_serial_wrapper(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_test_entrypoint(tmp_path, "--help")

    assert completed.returncode == 0, completed.stderr
    assert "Recommended full check:" in completed.stdout
    assert (
        "Run `./test lint`, `./test unit`, `./test spec`, and `./test prod`"
        in completed.stdout
    )
    assert "no args     Run the supported serial aggregate flow" in completed.stdout
    assert _read_log_lines(docker_log_path) == []
    assert _read_log_lines(uv_log_path) == []


def test_serial_aggregate_flow_runs_local_nonproduction_lanes_before_prod(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_test_entrypoint(tmp_path)

    assert completed.returncode == 0, completed.stderr

    uv_log = "\n".join(_read_log_lines(uv_log_path))
    assert "|1|" not in uv_log
    assert "python -m ruff check ." in uv_log
    assert (
        "python -m mypy src --show-error-codes --hide-error-context "
        "--no-error-summary --no-color-output"
    ) in uv_log
    assert "python -m pytest -q tests/unit" in uv_log
    assert "python -m pytest -q tests/harness/test_spec_harness.py" in uv_log

    docker_log = "\n".join(_read_log_lines(docker_log_path))
    assert " build test" in docker_log
    assert " build app" in docker_log
    assert "tests/harness/test_prod_harness.py" in docker_log
