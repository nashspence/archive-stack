from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
COMPOSE_FILE = REPO_ROOT / "compose.yml"


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
                    f'printf \'%s|%s\\n\' "${{COMPOSE_PROJECT_NAME:-}}" "$*" >> {log_path}',
                    'if [[ "$1" == "image" && "$2" == "inspect" ]]; then',
                    '  [[ "${FAKE_DOCKER_HAVE_IMAGES:-0}" == "1" ]] && '
                    "printf 'fake-image-id\\n' && exit 0",
                    "  exit 1",
                    "fi",
                    (
                        'if [[ "$*" == *'
                        '" exec -T garage /garage -c /etc/garage.toml node id"* ]]; then'
                    ),
                    "  printf 'fake-node@garage\\n'",
                    "fi",
                ]
            )
            + "\n"
        )
    elif name == "pgrep":
        command.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    f'printf \'%s|%s\\n\' "${{COMPOSE_PROJECT_NAME:-}}" "$*" >> {log_path}',
                    "exit 1",
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
                    f'printf \'%s|%s\\n\' "${{COMPOSE_PROJECT_NAME:-}}" "$*" >> {log_path}',
                ]
            )
            + "\n"
        )
    command.chmod(0o755)
    return log_path


def _run_make(
    tmp_path: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
    with_mise: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    docker_log_path = _install_fake_command(tmp_path, "docker", "docker.log")
    uv_log_path = (
        _install_fake_command(tmp_path, "mise", "uv.log") if with_mise else tmp_path / "uv.log"
    )
    _install_fake_command(tmp_path, "pgrep", "pgrep.log")
    env = os.environ.copy()
    env["PATH"] = (
        f"{tmp_path / 'bin'}:/usr/bin:/bin" if not with_mise else f"{tmp_path / 'bin'}:{env['PATH']}"
    )
    env.pop("args", None)
    env.pop("FILES", None)
    env.pop("MAKEFLAGS", None)
    env.pop("MFLAGS", None)
    env.pop("SPEC_TESTS", None)
    env.pop("TESTS", None)
    env.pop("MISE_BIN", None)
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


def test_checked_in_compose_uses_supported_tusd_filesystem_storage_flag() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")

    assert '- "-upload-dir"' in compose_text
    assert '- "-dir"' not in compose_text


@pytest.mark.parametrize(
    ("target", "extra_args", "expected_command"),
    [
        ("ruff", (), "python -m ruff check ."),
        ("ruff-fix", ("FILES=src/jeb",), "python -m ruff check --fix src/jeb"),
        ("format", ("FILES=src/jeb",), "python -m ruff format src/jeb"),
        ("unit", ("args=-k entrypoint",), "python -m pytest -q tests/unit -k entrypoint"),
        (
            "unit",
            ("TESTS=tests/unit/test_jeb_health.py",),
            "python -m pytest -q tests/unit/test_jeb_health.py",
        ),
        (
            "spec",
            ("args=-k glacier",),
            "python -m pytest -q tests/harness/test_spec_harness.py -k glacier",
        ),
        (
            "spec",
            ("SPEC_TESTS=tests/harness/test_spec_harness.py", "args=-k garage"),
            "python -m pytest -q tests/harness/test_spec_harness.py -k garage",
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
    assert (
        "run --python 3.11 --isolated --with-requirements "
        f"{REPO_ROOT / 'requirements-test.txt'} --with-editable .[db] "
    ) in uv_log_lines[0]
    assert expected_command in uv_log_lines[0]


def test_fix_runs_ruff_fix_then_format(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(
        tmp_path,
        "fix",
        "FILES=src/jeb",
    )

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(docker_log_path) == []
    uv_log_lines = _read_log_lines(uv_log_path)
    assert len(uv_log_lines) == 2
    assert "python -m ruff check --fix src/jeb" in uv_log_lines[0]
    assert "python -m ruff format src/jeb" in uv_log_lines[1]


def test_local_targets_fail_clearly_when_mise_is_missing(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(
        tmp_path,
        "unit",
        extra_env={"MISE_BIN": str(tmp_path / "missing-mise")},
        with_mise=False,
    )

    assert completed.returncode != 0
    assert _read_log_lines(docker_log_path) == []
    assert _read_log_lines(uv_log_path) == []
    assert "Riverhog Makefile targets require mise on PATH" in completed.stderr
    assert "MISE_BIN=/abs/path/to/mise" in completed.stderr


def test_mypy_target_covers_source_and_service_apps(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(tmp_path, "mypy", "args=--strict")

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(docker_log_path) == []
    uv_log_lines = _read_log_lines(uv_log_path)
    assert len(uv_log_lines) == 3
    assert (
        "python -m mypy src --show-error-codes --hide-error-context "
        "--no-error-summary --no-color-output --strict"
    ) in uv_log_lines[0]
    assert (
        "python -m mypy services/munchy-av1-nvenc/app/main.py "
        "--show-error-codes --hide-error-context --no-error-summary --no-color-output --strict"
    ) in uv_log_lines[1]
    assert (
        "python -m mypy services/munchy-runner/app/main.py "
        "--show-error-codes --hide-error-context --no-error-summary --no-color-output --strict"
    ) in uv_log_lines[2]


def test_lint_runs_ruff_then_mypy(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(tmp_path, "lint")

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(docker_log_path) == []
    uv_log_lines = _read_log_lines(uv_log_path)
    assert len(uv_log_lines) == 4
    assert "python -m ruff check ." in uv_log_lines[0]
    assert "python -m mypy src --show-error-codes" in uv_log_lines[1]
    assert "python -m mypy services/munchy-av1-nvenc/app/main.py" in uv_log_lines[2]
    assert "python -m mypy services/munchy-runner/app/main.py" in uv_log_lines[3]


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


def test_dockerfiles_keep_dependency_layers_independent_of_docs_and_tests() -> None:
    app_dockerfile = (REPO_ROOT / "Dockerfile.app").read_text()
    test_dockerfile = (REPO_ROOT / "Dockerfile.test").read_text()
    dockerignore = (REPO_ROOT / ".dockerignore").read_text().splitlines()

    assert "COPY . ." not in app_dockerfile
    assert "COPY . ." not in test_dockerfile
    assert "COPY README.md" not in app_dockerfile
    assert "COPY README.md" not in test_dockerfile
    assert "pip install --no-cache-dir -e" not in app_dockerfile
    assert "pip install --no-cache-dir -e" not in test_dockerfile
    assert app_dockerfile.index("COPY requirements-runtime.txt ./") < app_dockerfile.index(
        "COPY src ./src"
    )
    assert app_dockerfile.index(
        "pip install --no-cache-dir --require-hashes -r requirements-runtime.txt"
    ) < app_dockerfile.index("COPY src ./src")
    assert test_dockerfile.index("COPY requirements-test.txt ./") < test_dockerfile.index(
        "COPY src ./src"
    )
    assert test_dockerfile.index(
        "pip install --no-cache-dir --require-hashes -r requirements-test.txt"
    ) < test_dockerfile.index("COPY src ./src")
    assert test_dockerfile.index("COPY pyproject.toml ./") < test_dockerfile.index(
        "COPY tests ./tests"
    )
    assert "COPY services/munchy-av1-nvenc/app ./services/munchy-av1-nvenc/app" in test_dockerfile
    assert "COPY services/munchy-runner/app ./services/munchy-runner/app" in test_dockerfile
    assert "COPY tests ./tests" in test_dockerfile
    assert "COPY contracts ./contracts" in test_dockerfile
    assert "docs/" in dockerignore


def test_unit_lane_owns_service_unit_tests() -> None:
    assert not list((REPO_ROOT / "services").glob("*/tests/test_*.py"))
    assert (REPO_ROOT / "tests" / "unit" / "test_munchy_av1_container_profiles.py").is_file()
    assert (REPO_ROOT / "tests" / "unit" / "test_munchy_av1_qcut_planner.py").is_file()
    assert (REPO_ROOT / "tests" / "unit" / "test_munchy_av1_source_artifacts.py").is_file()
    assert (REPO_ROOT / "tests" / "unit" / "test_munchy_runner_contract.py").is_file()


def test_repo_wide_lint_targets_cover_source_and_service_apps() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "python -m ruff check $(FILES)" in makefile
    assert "python -m ruff check --fix $(FILES)" in makefile
    assert "python -m ruff format $(FILES)" in makefile
    assert "python -m mypy src" in makefile
    assert "python -m mypy services/munchy-av1-nvenc/app/main.py" in makefile
    assert "python -m mypy services/munchy-runner/app/main.py" in makefile
    assert "strict = true" in pyproject


def test_deployed_service_dockerfiles_use_locked_service_dependencies() -> None:
    service_dockerfiles = [
        REPO_ROOT / "services" / "jeb" / "Dockerfile",
        REPO_ROOT / "services" / "munchy-av1-nvenc" / "Dockerfile",
        REPO_ROOT / "services" / "munchy-runner" / "Dockerfile",
    ]

    for path in service_dockerfiles:
        dockerfile = path.read_text(encoding="utf-8")
        assert "COPY requirements-service.txt" in dockerfile
        assert "--require-hashes -r" in dockerfile
        assert "PYTHONPATH=/riverhog/src" in dockerfile
        assert "pip install /riverhog" not in dockerfile
        assert "pip install -r requirements.txt" not in dockerfile
        assert dockerfile.index("COPY requirements-service.txt") < dockerfile.index("COPY src")
        assert dockerfile.index("--require-hashes -r") < dockerfile.index("COPY src")


def test_munchy_runner_image_includes_source_artifact_runtime_tools() -> None:
    dockerfile = (REPO_ROOT / "services" / "munchy-runner" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "ffmpeg" in dockerfile
    assert "libimage-exiftool-perl" in dockerfile
    assert "rclone" in dockerfile
    assert "zstd" in dockerfile


def test_locked_dependency_files_cover_runtime_and_test_db_extras() -> None:
    runtime_requirements = (REPO_ROOT / "requirements-runtime.txt").read_text()
    test_requirements = (REPO_ROOT / "requirements-test.txt").read_text()
    service_requirements = (REPO_ROOT / "requirements-service.txt").read_text()

    assert "--extra db" in runtime_requirements.splitlines()[1]
    assert "--extra db" in test_requirements.splitlines()[1]
    assert "--extra service" in service_requirements.splitlines()[1]
    assert "-c requirements-runtime.txt" in service_requirements.splitlines()[1]
    for package in ("boto3", "fastapi", "psycopg", "psycopg-binary", "sqlalchemy", "uvicorn"):
        assert f"{package}==" in runtime_requirements
        assert f"{package}==" in test_requirements
    for package in ("fastapi", "httptools", "uvicorn", "uvloop", "watchfiles", "websockets"):
        assert f"{package}==" in service_requirements
    assert "--hash=sha256:" in runtime_requirements
    assert "--hash=sha256:" in service_requirements
    assert "--hash=sha256:" in test_requirements


def test_test_aggregate_runs_lint_then_unit(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(tmp_path, "test")

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(docker_log_path) == []

    uv_log_lines = _read_log_lines(uv_log_path)
    assert len(uv_log_lines) == 5
    assert "python -m ruff check ." in uv_log_lines[0]
    assert "python -m mypy src --show-error-codes" in uv_log_lines[1]
    assert "python -m mypy services/munchy-av1-nvenc/app/main.py" in uv_log_lines[2]
    assert "python -m mypy services/munchy-runner/app/main.py" in uv_log_lines[3]
    assert "python -m pytest -q tests/unit" in uv_log_lines[4]


def test_down_target_uses_compose_down_with_volumes(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(tmp_path, "down")

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(uv_log_path) == []
    docker_log = "\n".join(_read_log_lines(docker_log_path))
    assert " down --volumes --remove-orphans" in docker_log


def test_stop_spec_is_available_when_no_spec_lane_is_running(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(tmp_path, "stop-spec")

    assert completed.returncode == 0, completed.stderr
    assert "No in-flight spec harness process found." in completed.stdout
    assert _read_log_lines(docker_log_path) == []
    assert _read_log_lines(uv_log_path) == []


def test_help_describes_make_targets(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(tmp_path, "help")

    assert completed.returncode == 0, completed.stderr
    assert "make bootstrap-garage" in completed.stdout
    assert "make build-app" in completed.stdout
    assert "make build-test" in completed.stdout
    assert "make fix" in completed.stdout
    assert "make format" in completed.stdout
    assert "make ruff-fix" in completed.stdout
    assert "make stop-spec" in completed.stdout
    assert "make test" in completed.stdout
    assert "args='...'" in completed.stdout
    assert "FILES='...'" in completed.stdout
    assert "TESTS='...'" in completed.stdout
    assert "fast" not in completed.stdout
    assert _read_log_lines(docker_log_path) == []
    assert _read_log_lines(uv_log_path) == []
