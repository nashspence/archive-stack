from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"


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
                        "if [[ \"${FAKE_CREATE_STATE_ROOT:-0}\" == \"1\" "
                        "&& -n \"${ARC_TEST_HOST_STATE_ROOT:-}\" ]]; then"
                    ),
                    "  mkdir -p \"${ARC_TEST_HOST_STATE_ROOT}\"",
                    "  touch \"${ARC_TEST_HOST_STATE_ROOT}/marker\"",
                    "fi",
                    (
                        "printf '%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\\n' "
                        "\"${COMPOSE_PROJECT_NAME:-}\" "
                        "\"${TEST_COMPOSE_PROJECT_ISOLATED:-}\" "
                        "\"${ARC_ENABLE_TEST_CONTROL:-}\" "
                        "\"$*\" "
                        "\"${ARC_API_PORT:-}\" "
                        "\"${ARC_WEBDAV_PORT:-}\" "
                        "\"${ARC_DB_PATH:-}\" "
                        "\"${ARC_TEST_EXTERNAL_APP_DB_PATH:-}\" "
                        "\"${ARC_TEST_WEBHOOK_CAPTURE_PATH:-}\" "
                        "\"${ARC_TEST_ACCEPTANCE_ROOT:-}\" "
                        "\"${ARC_TEST_HOST_STATE_ROOT:-}\" >> "
                        f"{log_path}"
                    ),
                    (
                        "if [[ \"$*\" == *\" --entrypoint rm \"* "
                        "&& -n \"${ARC_TEST_HOST_STATE_ROOT:-}\" ]]; then"
                    ),
                    "  rm -rf -- \"${ARC_TEST_HOST_STATE_ROOT}\"",
                    "fi",
                    "if [[ \"$1\" == \"image\" && \"$2\" == \"inspect\" ]]; then",
                    "  if [[ \"${FAKE_DOCKER_HAVE_IMAGES:-0}\" == \"1\" ]]; then",
                    "    printf 'fake-image-id\\n'",
                    "    exit 0",
                    "  fi",
                    "  exit 1",
                    "fi",
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
                        "printf '%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\\n' "
                        "\"${COMPOSE_PROJECT_NAME:-}\" "
                        "\"${TEST_COMPOSE_PROJECT_ISOLATED:-}\" "
                        "\"${ARC_ENABLE_TEST_CONTROL:-}\" "
                        "\"$*\" "
                        "\"${ARC_API_PORT:-}\" "
                        "\"${ARC_WEBDAV_PORT:-}\" "
                        "\"${ARC_DB_PATH:-}\" "
                        "\"${ARC_TEST_EXTERNAL_APP_DB_PATH:-}\" "
                        "\"${ARC_TEST_WEBHOOK_CAPTURE_PATH:-}\" "
                        "\"${ARC_TEST_ACCEPTANCE_ROOT:-}\" "
                        "\"${ARC_TEST_HOST_STATE_ROOT:-}\" >> "
                        f"{log_path}"
                    ),
                ]
            )
            + "\n"
        )
    command.chmod(0o755)
    return log_path


def _run_make(
    tmp_path: Path, *args: str, extra_env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    docker_log_path = _install_fake_command(tmp_path, "docker", "docker.log")
    uv_log_path = _install_fake_command(tmp_path, "uv", "uv.log")
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path / 'bin'}:{env['PATH']}"
    if extra_env:
        env.update(extra_env)

    completed = subprocess.run(
        ["make", "-f", str(MAKEFILE), *args],
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


def _split_docker_log_line(line: str) -> tuple[str, ...]:
    return tuple(line.split("|", 10))


def _assert_isolated_prod_runtime(line: str) -> str:
    (
        project,
        isolated,
        _,
        _,
        api_port,
        webdav_port,
        db_path,
        external_db_path,
        webhook_path,
        acceptance_root,
        host_state_root,
    ) = _split_docker_log_line(line)
    assert isolated == "1"
    assert api_port == "0"
    assert webdav_port == "0"
    assert db_path == f"/app/.compose/{project}/state.sqlite3"
    assert external_db_path == db_path
    assert webhook_path == f"/app/.compose/{project}/webhook-captures.jsonl"
    assert acceptance_root == f"/app/.compose/{project}/acceptance"
    assert host_state_root == str(REPO_ROOT / ".compose" / project)
    return project


@pytest.mark.parametrize(
    ("target", "extra_args", "expected_command"),
    [
        ("ruff", (), "python -m ruff check ."),
        (
            "mypy",
            (),
            "python -m mypy src --show-error-codes --hide-error-context "
            "--no-error-summary --no-color-output",
        ),
        ("unit", ("PYTEST_ARGS=-k entrypoint",), "python -m pytest -q tests/unit -k entrypoint"),
        (
            "spec",
            ("PYTEST_ARGS=-k glacier",),
            "python -m pytest -q tests/harness/test_spec_harness.py -k glacier",
        ),
    ],
)
def test_atomic_local_targets_run_in_locked_uv_environment(
    tmp_path: Path, target: str, extra_args: tuple[str, ...], expected_command: str
) -> None:
    completed, docker_log_path, uv_log_path = _run_make(tmp_path, target, *extra_args)

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(docker_log_path) == []

    uv_log_lines = _read_log_lines(uv_log_path)
    assert len(uv_log_lines) == 1
    assert uv_log_lines[0].split("|", 2)[1] == ""
    assert (
        "run --python 3.11 --isolated --with-requirements "
        f"{REPO_ROOT / 'requirements-test.txt'} --with-editable .[db] "
    ) in uv_log_lines[0]
    assert expected_command in uv_log_lines[0]


def test_lint_runs_ruff_then_mypy(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(tmp_path, "lint")

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(docker_log_path) == []
    uv_log_lines = _read_log_lines(uv_log_path)
    assert len(uv_log_lines) == 2
    assert "python -m ruff check ." in uv_log_lines[0]
    assert "python -m mypy src" in uv_log_lines[1]


def test_build_targets_are_atomic(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(tmp_path, "build")

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(uv_log_path) == []
    docker_log = "\n".join(_read_log_lines(docker_log_path))
    assert " build app" in docker_log
    assert " build test" in docker_log


def test_bootstrap_garage_is_available_as_a_standalone_target(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(
        tmp_path, "bootstrap-garage", extra_env={"FAKE_DOCKER_HAVE_IMAGES": "1"}
    )

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(uv_log_path) == []
    docker_log = "\n".join(_read_log_lines(docker_log_path))
    assert " up --detach garage" in docker_log
    assert " exec -T garage /garage -c /etc/garage.toml node id" in docker_log
    assert " run --rm --entrypoint python" in docker_log
    assert "tests/harness/configure_garage.py" in docker_log


def test_prod_builds_images_and_uses_isolated_compose_project_name(
    tmp_path: Path,
) -> None:
    completed, docker_log_path, uv_log_path = _run_make(
        tmp_path,
        "prod",
        "PYTEST_ARGS=-k glacier",
        extra_env={"FAKE_DOCKER_HAVE_IMAGES": "1"},
    )

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(uv_log_path) == []

    log_lines = _read_log_lines(docker_log_path)
    project_names = {line.split("|", 1)[0] for line in log_lines}
    assert len(project_names) == 1
    project_name = next(iter(project_names))
    assert re.fullmatch(r"archive-stack-test-[a-z0-9]+(?:-[a-z0-9]+)*-\d+", project_name)

    docker_log = "\n".join(log_lines)
    assert " build app" in docker_log
    assert " build test" in docker_log
    assert " up --detach garage" in docker_log
    assert " up --detach --wait app" in docker_log
    assert "tests/harness/test_prod_harness.py -k glacier" in docker_log
    assert " down --volumes --remove-orphans" in docker_log
    for line in log_lines:
        _assert_isolated_prod_runtime(line)


def test_prod_removes_generated_bind_mount_state_on_success(tmp_path: Path) -> None:
    state_root = tmp_path / "generated-state"
    completed, docker_log_path, uv_log_path = _run_make(
        tmp_path,
        "prod",
        "PYTEST_ARGS=-k glacier",
        extra_env={
            "FAKE_DOCKER_HAVE_IMAGES": "1",
            "FAKE_CREATE_STATE_ROOT": "1",
            "ARC_TEST_HOST_STATE_ROOT": str(state_root),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(uv_log_path) == []
    assert not state_root.exists()


def test_prod_preserves_explicit_shared_bind_mount_state(tmp_path: Path) -> None:
    shared_project = "archive-stack-shared-unit"
    shared_root = tmp_path / "shared-state"
    shutil.rmtree(shared_root, ignore_errors=True)
    try:
        completed, docker_log_path, uv_log_path = _run_make(
            tmp_path,
            "prod",
            "PYTEST_ARGS=-k glacier",
            extra_env={
                "FAKE_DOCKER_HAVE_IMAGES": "1",
                "FAKE_CREATE_STATE_ROOT": "1",
                "ARC_TEST_HOST_STATE_ROOT": str(shared_root),
                "TEST_COMPOSE_PROJECT_NAME": shared_project,
            },
        )

        assert completed.returncode == 0, completed.stderr
        assert _read_log_lines(uv_log_path) == []
        log_lines = _read_log_lines(docker_log_path)
        assert {line.split("|", 1)[0] for line in log_lines} == {shared_project}
        assert {line.split("|", 2)[1] for line in log_lines} == {"0"}
        assert (shared_root / "marker").exists()
    finally:
        shutil.rmtree(shared_root, ignore_errors=True)


def test_prod_profile_enables_profile_output_and_builds_images(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(
        tmp_path,
        "prod-profile",
        "PYTEST_ARGS=-k glacier",
        extra_env={"FAKE_DOCKER_HAVE_IMAGES": "1"},
    )

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(uv_log_path) == []
    log_lines = _read_log_lines(docker_log_path)
    docker_log = "\n".join(log_lines)
    assert " build app" in docker_log
    assert " build test" in docker_log
    assert " -e ARC_TEST_PROFILE=1 " in docker_log
    assert " --durations=0 --durations-min=0.5 " in docker_log
    for line in log_lines:
        _assert_isolated_prod_runtime(line)


def test_dockerfiles_keep_dependency_layers_independent_of_docs_and_tests() -> None:
    app_dockerfile = (REPO_ROOT / "Dockerfile.app").read_text()
    test_dockerfile = (REPO_ROOT / "Dockerfile.test").read_text()
    dockerignore = (REPO_ROOT / ".dockerignore").read_text().splitlines()

    assert "COPY . ." not in app_dockerfile
    assert "COPY . ." not in test_dockerfile
    assert app_dockerfile.index("RUN mkdir -p src/arc_api") < app_dockerfile.index(
        "COPY src ./src"
    )
    assert test_dockerfile.index("RUN mkdir -p src/arc_api") < test_dockerfile.index(
        "COPY src ./src"
    )
    assert "COPY tests ./tests" in test_dockerfile
    assert "COPY contracts ./contracts" in test_dockerfile
    assert "docs/" in dockerignore


def test_test_aggregate_runs_lint_unit_spec_then_prod(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(
        tmp_path, "test", extra_env={"FAKE_DOCKER_HAVE_IMAGES": "1"}
    )

    assert completed.returncode == 0, completed.stderr

    uv_log_lines = _read_log_lines(uv_log_path)
    assert len(uv_log_lines) == 4
    assert "python -m ruff check ." in uv_log_lines[0]
    assert "python -m mypy src" in uv_log_lines[1]
    assert "python -m pytest -q tests/unit" in uv_log_lines[2]
    assert "python -m pytest -q tests/harness/test_spec_harness.py" in uv_log_lines[3]

    docker_log = "\n".join(_read_log_lines(docker_log_path))
    assert " build app" in docker_log
    assert " build test" in docker_log
    assert "tests/harness/test_prod_harness.py" in docker_log


def test_down_target_uses_compose_down_with_volumes(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(tmp_path, "down")

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(uv_log_path) == []
    docker_log = "\n".join(_read_log_lines(docker_log_path))
    assert " down --volumes --remove-orphans" in docker_log


def test_help_describes_make_targets_and_omits_fast(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(tmp_path, "help")

    assert completed.returncode == 0, completed.stderr
    assert "make bootstrap-garage" in completed.stdout
    assert "make build-app" in completed.stdout
    assert "make build-test" in completed.stdout
    assert "make test" in completed.stdout
    assert "fast" not in completed.stdout
    assert _read_log_lines(docker_log_path) == []
    assert _read_log_lines(uv_log_path) == []
