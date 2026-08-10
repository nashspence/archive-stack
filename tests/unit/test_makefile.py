from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from riverhog_core.runtime_config import load_runtime_config
from yaml.nodes import MappingNode, Node, SequenceNode

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
COMPOSE_FILE = REPO_ROOT / "riverhog/server/compose.yaml"
SBOM_GENERATOR = (
    "docker.io/docker/buildkit-syft-scanner:stable-1@"
    "sha256:79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68"
)


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
        f"{tmp_path / 'bin'}:/usr/bin:/bin"
        if not with_mise
        else f"{tmp_path / 'bin'}:{env['PATH']}"
    )
    env.pop("args", None)
    env.pop("FILES", None)
    env.pop("MAKEFLAGS", None)
    env.pop("MFLAGS", None)
    env.pop("COMPOSE_ENV_FILE", None)
    env.pop("SPEC_TESTS", None)
    env.pop("POSTGRES_TESTS", None)
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


def _assert_unique_yaml_mapping_keys(node: Node) -> None:
    if isinstance(node, MappingNode):
        keys = [key.value for key, _value in node.value]
        assert len(keys) == len(set(keys))
        for key, value in node.value:
            _assert_unique_yaml_mapping_keys(key)
            _assert_unique_yaml_mapping_keys(value)
    elif isinstance(node, SequenceNode):
        for value in node.value:
            _assert_unique_yaml_mapping_keys(value)


def test_compose_has_unique_keys_and_runtime_owned_environment() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    compose_node = yaml.compose(compose_text)
    assert compose_node is not None
    _assert_unique_yaml_mapping_keys(compose_node)

    compose = yaml.safe_load(compose_text)
    runtime_source = "\n".join(
        path.read_text(encoding="utf-8")
        for package in (
            REPO_ROOT / "riverhog/server/src/riverhog_core",
            REPO_ROOT / "riverhog/server/src/riverhog_api",
        )
        for path in package.rglob("*.py")
    )
    configured_names = {
        name for name in compose["services"]["app"]["environment"] if name.startswith("RIVERHOG_")
    }
    dynamic_archive_store_names = {
        name for name in configured_names if name.startswith("RIVERHOG_ARCHIVE_STORE_")
    }
    assert all(name in runtime_source for name in configured_names - dynamic_archive_store_names)
    assert "RIVERHOG_ARCHIVE_STORE_" in runtime_source
    assert {name.rsplit("_", 1)[-1] for name in dynamic_archive_store_names} <= {
        "URL",
        "REGION",
        "BUCKET",
        "ID",
        "KEY",
        "STYLE",
        "PREFIX",
        "BACKEND",
        "CLASS",
        "PATH",
        "MODE",
        "BYTES",
    }


def test_compose_services_publish_every_static_runtime_setting() -> None:
    runtime_trees = (
        ast.parse(path.read_text(encoding="utf-8"))
        for package in (
            REPO_ROOT / "riverhog/server/src/riverhog_core",
            REPO_ROOT / "riverhog/server/src/riverhog_api",
        )
        for path in package.rglob("*.py")
    )
    runtime_names = {
        node.args[0].value
        for runtime_tree in runtime_trees
        for node in ast.walk(runtime_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"get", "getenv"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value.startswith("RIVERHOG_")
    }
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))

    assert runtime_names
    for service in ("app", "test"):
        assert runtime_names <= set(compose["services"][service]["environment"])


def test_compose_host_interpolation_is_complete_without_an_env_file() -> None:
    expressions = re.findall(
        r"(?<!\$)\$\{([^}]+)\}",
        COMPOSE_FILE.read_text(encoding="utf-8"),
    )

    assert expressions
    assert all("-" in expression for expression in expressions)


def test_compose_policy_defaults_match_runtime_defaults() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    compose_environment: dict[str, str] = {}
    for name, raw_value in compose["services"]["app"]["environment"].items():
        value = str(raw_value)
        match = re.fullmatch(r"\$\{[A-Z0-9_]+:?-([^}]*)\}", value)
        compose_environment[name] = match.group(1) if match else value

    assert compose_environment["RIVERHOG_BOOTSTRAP_TOKEN"] == (
        "riverhog-development-bootstrap-token"
    )

    with patch.dict(os.environ, {}, clear=True):
        runtime_defaults = asdict(load_runtime_config())
    with patch.dict(os.environ, compose_environment, clear=True):
        compose_defaults = asdict(load_runtime_config())

    topology_fields = {
        "database_url",
        "public_base_url",
        "retrieval_cache",
    }
    archive_topology_fields = {
        "access_key_id",
        "bucket",
        "endpoint_url",
        "region",
        "secret_access_key",
        "prefix",
    }
    for defaults in (runtime_defaults, compose_defaults):
        for store in defaults["archive_stores"].values():
            for field in archive_topology_fields:
                store.pop(field)
    for field in topology_fields:
        runtime_defaults.pop(field)
        compose_defaults.pop(field)

    assert compose_defaults == runtime_defaults


def test_compose_services_publish_the_archive_runtime_configuration() -> None:
    required = {
        "RIVERHOG_ARCHIVE_STORES",
        "RIVERHOG_ARCHIVE_WRITE_STORE",
        "RIVERHOG_ARCHIVE_READ_ORDER",
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_ENDPOINT_URL",
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_REGION",
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_BUCKET",
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_ACCESS_KEY_ID",
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_SECRET_ACCESS_KEY",
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_FORCE_PATH_STYLE",
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_PREFIX",
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_BACKEND",
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_STORAGE_CLASS",
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_READ_MODE",
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_CLOUDFRONT_BASE_URL",
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_CLOUDFRONT_PUBLIC_KEY_ID",
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_CLOUDFRONT_PRIVATE_KEY_PATH",
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_MONTHLY_DOWNLOAD_ALLOWANCE_BYTES",
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_DOWNLOAD_SAFETY_BUFFER_BYTES",
        "RIVERHOG_ARCHIVE_MULTIPART_PART_BYTES",
        "RIVERHOG_ARCHIVE_MULTIPART_CONCURRENCY",
        "RIVERHOG_ARCHIVE_MULTIPART_MAX_AGE",
        "RIVERHOG_ARCHIVE_MULTIPART_SWEEP_INTERVAL",
        "RIVERHOG_ARCHIVE_PREPARE_CONCURRENCY",
        "RIVERHOG_ARCHIVE_UPLOAD_REQUEST_CONCURRENCY",
        "RIVERHOG_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASE",
        "RIVERHOG_ARCHIVE_PASSPHRASE",
        "RIVERHOG_ARCHIVE_SCRYPT_WORK_FACTOR",
        "RIVERHOG_ARCHIVE_UPLOAD_SWEEP_INTERVAL",
        "RIVERHOG_BOOTSTRAP_TOKEN",
        "RIVERHOG_S3_MAX_POOL_CONNECTIONS",
        "RIVERHOG_UPLOAD_FILE_CONCURRENCY",
        "RIVERHOG_UPLOAD_FILE_WINDOW",
        "RIVERHOG_UPLOAD_CHUNK_BYTES",
        "RIVERHOG_INGRESS_VOLUME_CONCURRENCY",
        "RIVERHOG_INGRESS_VOLUME_WINDOW",
        "RIVERHOG_INGRESS_MAX_INFLIGHT_BYTES",
        "RIVERHOG_INGRESS_SOURCE_READ_CHUNK_BYTES",
        "RIVERHOG_AGE_SESSION_CACHE_ENTRIES",
        "RIVERHOG_AGE_SESSION_DERIVATION_CONCURRENCY",
        "RIVERHOG_RETRIEVAL_CACHE_ENDPOINT_URL",
        "RIVERHOG_RETRIEVAL_CACHE_BUCKET",
        "RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_LEASE",
        "RIVERHOG_RETRIEVAL_DEFAULT_LEASE",
        "RIVERHOG_RETRIEVAL_MAX_LEASE",
        "RIVERHOG_RETRIEVAL_SWEEP_INTERVAL",
        "RIVERHOG_RETRIEVAL_ESTIMATED_LATENCY",
        "RIVERHOG_RETRIEVAL_TIER",
        "RIVERHOG_DOWNLOAD_FILE_CONCURRENCY",
        "RIVERHOG_DOWNLOAD_FILE_WINDOW",
        "RIVERHOG_DOWNLOAD_CHUNK_BYTES",
        "RIVERHOG_RETRIEVAL_REQUEST_CONCURRENCY",
        "RIVERHOG_RETRIEVAL_MAX_INFLIGHT_BYTES",
        "RIVERHOG_RETRIEVAL_READ_CHUNK_BYTES",
        "RIVERHOG_S3_CONNECT_TIMEOUT_SECONDS",
        "RIVERHOG_S3_READ_TIMEOUT_SECONDS",
        "RIVERHOG_S3_MAX_ATTEMPTS",
        "RIVERHOG_S3_RETRY_MODE",
        "RIVERHOG_S3_TCP_KEEPALIVE",
        "RIVERHOG_EVENT_SOURCE",
        "RIVERHOG_EVENT_CONTEXT_RETENTION",
        "RIVERHOG_OTS_STAMP_COMMAND",
        "RIVERHOG_OTS_UPGRADE_COMMAND",
        "RIVERHOG_OTS_VERIFY_COMMAND",
        "RIVERHOG_ATTESTATION_SECRET_KEY_FILE",
        "RIVERHOG_ATTESTATION_PUBLIC_KEY_FILE",
        "RIVERHOG_PROOF_MATURATION_RETRY_DELAY",
        "RIVERHOG_PROOF_MATURATION_SWEEP_INTERVAL",
    }
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    for service in ("app", "test"):
        assert required <= set(compose["services"][service]["environment"])
        assert (
            compose["services"][service]["environment"]["RIVERHOG_ARCHIVE_STORE_ARCHIVE_PREFIX"]
            == "${RIVERHOG_ARCHIVE_STORE_ARCHIVE_PREFIX-}"
        )
        assert compose["services"][service]["env_file"] == [
            {
                "path": "${RIVERHOG_COMPOSE_ENV_FILE:-../../.env.compose}",
                "required": False,
            }
        ]

    compose_helper = (REPO_ROOT / "scripts" / "_compose_env.sh").read_text(encoding="utf-8")
    assert 'export RIVERHOG_COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE}"' in compose_helper


@pytest.mark.parametrize(
    ("target", "extra_args", "expected_command"),
    [
        ("ruff", (), "python -m ruff check ."),
        (
            "ruff-fix",
            ("FILES=companions/jeb/server",),
            "python -m ruff check --fix companions/jeb/server",
        ),
        ("format", ("FILES=companions/jeb/server",), "python -m ruff format companions/jeb/server"),
        (
            "unit",
            ("args=-k entrypoint",),
            "python -m pytest -q companions packages riverhog tests/unit utilities -k entrypoint",
        ),
        (
            "unit",
            ("TESTS=companions/jeb/tests/test_jeb_health.py",),
            "python -m pytest -q companions/jeb/tests/test_jeb_health.py",
        ),
        (
            "spec",
            ("args=-k archive",),
            "python -m pytest -q tests/harness/test_spec_harness.py -k archive",
        ),
        (
            "spec",
            ("SPEC_TESTS=tests/harness/test_spec_harness.py", "args=-k garage"),
            "python -m pytest -q tests/harness/test_spec_harness.py -k garage",
        ),
        ("release-check", (), "python scripts/release.py check"),
        (
            "release-plan",
            ("RELEASE_VERSION=1.0.0", "args=--format markdown"),
            "python scripts/release.py plan --version 1.0.0 --format markdown",
        ),
        (
            "release-dry-run",
            ("RELEASE_VERSION=1.0.0",),
            "python scripts/release.py dry-run --version 1.0.0",
        ),
        (
            "release-governance-check",
            ("RELEASE_SUMMARY=/tmp/governance.json",),
            "python scripts/github_governance.py check --summary /tmp/governance.json",
        ),
        (
            "release-evidence",
            (
                "RELEASE_VERSION=1.0.0",
                "RELEASE_OUTPUT=/tmp/evidence",
                "RELEASE_SIGNING_KEY=/tmp/release.key",
                "RELEASE_PUBLIC_KEY=/tmp/release.pub",
            ),
            "python scripts/release.py evidence --version 1.0.0 --output /tmp/evidence "
            "--signing-key /tmp/release.key --public-key /tmp/release.pub",
        ),
        (
            "release-verify",
            ("RELEASE_OUTPUT=/tmp/evidence", "RELEASE_PUBLIC_KEY=/tmp/release.pub"),
            "python scripts/release.py verify --directory /tmp/evidence "
            "--public-key /tmp/release.pub",
        ),
        (
            "tus-throughput",
            ("TUS_URL=https://tus.invalid/files/", "args=--size-mib 1"),
            "python scripts/tus_throughput.py https://tus.invalid/files/ --size-mib 1",
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
    assert "run --locked --all-packages --group dev " in uv_log_lines[0]
    assert expected_command in uv_log_lines[0]


def test_fix_runs_ruff_fix_then_format(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(
        tmp_path,
        "fix",
        "FILES=companions/jeb/server",
    )

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(docker_log_path) == []
    uv_log_lines = _read_log_lines(uv_log_path)
    assert len(uv_log_lines) == 2
    assert "python -m ruff check --fix companions/jeb/server" in uv_log_lines[0]
    assert "python -m ruff format companions/jeb/server" in uv_log_lines[1]


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
    assert len(uv_log_lines) == 1
    assert "python -m mypy companions/jeb/client/src companions/jeb/server/src" in uv_log_lines[0]
    assert "companions/munchy/client/src companions/munchy/server/src" in uv_log_lines[0]
    assert "riverhog/client/src riverhog/recovery/src riverhog/server/src" in uv_log_lines[0]
    assert "utilities/gogurt/src utilities/mango-fish/src" in uv_log_lines[0]
    assert "--no-error-summary --no-color-output --strict" in uv_log_lines[0]


def test_lint_runs_license_format_ruff_and_mypy(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(tmp_path, "lint")

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(docker_log_path) == []
    uv_log_lines = _read_log_lines(uv_log_path)
    assert len(uv_log_lines) == 4
    assert "python -m reuse lint" in uv_log_lines[0]
    assert "python -m ruff format --check ." in uv_log_lines[1]
    assert "python -m ruff check ." in uv_log_lines[2]
    assert "python -m mypy companions/jeb/client/src companions/jeb/server/src" in uv_log_lines[3]


def test_build_targets_use_the_canonical_bake_graph(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(tmp_path, "build")

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(uv_log_path) == []
    revision = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    created = subprocess.run(
        ["git", "show", "-s", "--format=%cI", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    epoch = subprocess.run(
        ["git", "show", "-s", "--format=%ct", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    targets = (
        "riverhog",
        "jeb",
        "mango-fish",
        "munchy-server",
        "munchy-av1-nvenc",
        "test",
    )
    assert _read_log_lines(docker_log_path) == [
        "|buildx bake --file docker-bake.hcl --load "
        f"--set {target}.args.SOURCE_REVISION={revision} "
        f"--set {target}.args.BUILD_CREATED={created} "
        f"--set {target}.args.SOURCE_DATE_EPOCH={epoch} "
        f"--set {target}.args.RELEASE_VERSION=development {target}"
        for target in targets
    ]


def test_compose_helpers_need_no_environment_file(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(tmp_path, "down")

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(uv_log_path) == []
    docker_log = "\n".join(_read_log_lines(docker_log_path))
    assert "--env-file" not in docker_log


def test_compose_helpers_honor_an_explicit_environment_file(tmp_path: Path) -> None:
    env_file = tmp_path / "overrides.env"
    env_file.write_text("TEST_COMPOSE_PROJECT_NAME=riverhog-explicit\n")
    completed, docker_log_path, uv_log_path = _run_make(
        tmp_path,
        "down",
        extra_env={"COMPOSE_ENV_FILE": str(env_file)},
    )

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(uv_log_path) == []
    docker_log = "\n".join(_read_log_lines(docker_log_path))
    assert f"--env-file {env_file}" in docker_log
    assert "riverhog-explicit|" in docker_log


def test_compose_helpers_reject_a_missing_explicit_environment_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.env"
    completed, docker_log_path, uv_log_path = _run_make(
        tmp_path,
        "down",
        extra_env={"COMPOSE_ENV_FILE": str(missing)},
    )

    assert completed.returncode == 2
    assert _read_log_lines(docker_log_path) == []
    assert _read_log_lines(uv_log_path) == []
    assert f"Compose environment file does not exist: {missing}" in completed.stderr


def test_dist_builds_a_clean_complete_artifact_set(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(tmp_path, "dist")

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(docker_log_path) == []
    assert _read_log_lines(uv_log_path) == [
        "|x -- uv build --all-packages --clear --no-create-gitignore",
        "|x -- uv run --locked --all-packages --group dev "
        "python scripts/check_distribution_licenses.py dist",
    ]


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


def test_compose_smoke_starts_and_cleans_a_fresh_stack(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(
        tmp_path,
        "compose-smoke",
        extra_env={"FAKE_DOCKER_HAVE_IMAGES": "1"},
    )

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(uv_log_path) == []
    docker_log = "\n".join(_read_log_lines(docker_log_path))
    assert f" build --sbom=generator={SBOM_GENERATOR} test" in docker_log
    assert " up --detach garage" in docker_log
    assert f" build --sbom=generator={SBOM_GENERATOR} app" in docker_log
    assert " up --detach --wait app" in docker_log
    assert " restart app" in docker_log
    assert " exec -T --env RIVERHOG_SMOKE_TOKEN=" in docker_log
    assert " app python -c " in docker_log
    assert " down --volumes --remove-orphans" in docker_log


def test_postgres_concurrency_target_uses_disposable_postgres(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(
        tmp_path,
        "postgres-concurrency",
        extra_env={"FAKE_DOCKER_HAVE_IMAGES": "1"},
    )

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(uv_log_path) == []
    docker_log = "\n".join(_read_log_lines(docker_log_path))
    assert " up --detach --wait postgres" in docker_log
    assert "RIVERHOG_TEST_POSTGRES_URL=postgresql+psycopg://" in docker_log
    assert "tests/integration/test_catalog_schema_postgres.py" in docker_log
    assert "tests/integration/test_collection_deletion_concurrency.py" in docker_log
    assert "tests/integration/test_download_allowance_concurrency.py" in docker_log
    assert " down --volumes --remove-orphans" in docker_log


def test_dockerfiles_keep_dependency_layers_independent_of_docs_and_tests() -> None:
    app_dockerfile = (REPO_ROOT / "riverhog/server/Dockerfile").read_text()
    test_dockerfile = (REPO_ROOT / "tests" / "Dockerfile").read_text()
    dockerignore = (REPO_ROOT / ".dockerignore").read_text().splitlines()

    assert "COPY . ." not in app_dockerfile
    assert "COPY . ." not in test_dockerfile
    assert "COPY README.md" not in app_dockerfile
    assert "COPY README.md" not in test_dockerfile
    assert "pip install" not in app_dockerfile
    assert "pip install" not in test_dockerfile
    assert app_dockerfile.index("COPY pyproject.toml uv.lock ./") < app_dockerfile.index(
        "COPY riverhog/server/src riverhog/server/src"
    )
    assert app_dockerfile.index(
        "COPY riverhog/server/src riverhog/server/src"
    ) < app_dockerfile.index("uv sync --frozen --package riverhog-server --no-dev --no-editable")
    assert test_dockerfile.index("COPY pyproject.toml uv.lock ./") < test_dockerfile.index(
        "COPY companions companions"
    )
    assert "uv sync --frozen --all-packages --group dev --no-editable" in test_dockerfile
    assert "COPY --from=ghcr.io/astral-sh/uv:0.11.24" in test_dockerfile
    assert "UV_PROJECT_ENVIRONMENT=/opt/venv" in test_dockerfile
    assert 'ENTRYPOINT ["python", "-m", "pytest"]' in test_dockerfile
    assert test_dockerfile.index("COPY pyproject.toml uv.lock ./") < test_dockerfile.index(
        "COPY tests tests"
    )
    assert "COPY companions companions" in test_dockerfile
    assert "COPY packages packages" in test_dockerfile
    assert "COPY riverhog riverhog" in test_dockerfile
    assert "COPY utilities utilities" in test_dockerfile
    assert "COPY tests tests" in test_dockerfile
    assert "/docs/" in dockerignore


def test_dockerfile_copy_sources_are_git_owned() -> None:
    dockerfiles = [
        REPO_ROOT / "tests" / "Dockerfile",
        *sorted((REPO_ROOT / "companions").rglob("Dockerfile")),
        *sorted((REPO_ROOT / "riverhog").rglob("Dockerfile")),
        *sorted((REPO_ROOT / "utilities").rglob("Dockerfile")),
    ]

    for dockerfile in dockerfiles:
        for raw_line in dockerfile.read_text(encoding="utf-8").splitlines():
            if not raw_line.lstrip().startswith("COPY "):
                continue
            tokens = shlex.split(raw_line)
            if tokens[1].startswith("--from="):
                continue
            sources = tokens[1:-1]
            for source in sources:
                tracked = subprocess.run(
                    ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", source],
                    cwd=REPO_ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.splitlines()
                assert tracked, f"{dockerfile.relative_to(REPO_ROOT)} copies untracked {source}"


def test_workspace_unit_lane_owns_application_unit_tests() -> None:
    assert (REPO_ROOT / "companions/jeb/tests/test_jeb_health.py").is_file()
    assert (REPO_ROOT / "utilities/mango-fish/tests/test_mango_fish.py").is_file()
    assert (REPO_ROOT / "companions/munchy/tests/test_munchy_server_contract.py").is_file()
    assert (REPO_ROOT / "utilities/gogurt/tests/test_gogurt.py").is_file()


def test_repo_wide_lint_targets_cover_source_and_service_apps() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "python -m ruff check $(FILES)" in makefile
    assert "python -m ruff check --fix $(FILES)" in makefile
    assert "python -m ruff format $(FILES)" in makefile
    assert "python -m ruff format --check $(FILES)" in makefile
    assert "MYPY_SOURCES" in makefile
    assert "riverhog/server/src" in makefile
    assert "packages/tus-transport/src" in makefile
    assert "strict = true" in pyproject


def test_format_check_and_compile_are_non_mutating_repository_targets(tmp_path: Path) -> None:
    format_completed, docker_log_path, uv_log_path = _run_make(tmp_path, "format-check")

    assert format_completed.returncode == 0, format_completed.stderr
    assert _read_log_lines(docker_log_path) == []
    assert _read_log_lines(uv_log_path) == [
        "|x -- uv run --locked --all-packages --group dev python -m ruff format --check ."
    ]
    uv_log_path.unlink()

    compile_completed, docker_log_path, uv_log_path = _run_make(tmp_path, "compile")

    assert compile_completed.returncode == 0, compile_completed.stderr
    assert _read_log_lines(docker_log_path) == []
    assert _read_log_lines(uv_log_path) == [
        "|x -- uv run --locked --all-packages --group dev "
        "python -m compileall -q companions packages riverhog scripts tests utilities"
    ]


def test_deployed_application_dockerfiles_use_locked_workspace_dependencies() -> None:
    service_dockerfiles = [
        REPO_ROOT / "riverhog/server/Dockerfile",
        REPO_ROOT / "companions/jeb/server/Dockerfile",
        REPO_ROOT / "utilities/mango-fish/Dockerfile",
        REPO_ROOT / "companions/munchy/server/Dockerfile",
        REPO_ROOT / "companions/munchy/server/targets/av1-nvenc/Dockerfile",
    ]

    for path in service_dockerfiles:
        dockerfile = path.read_text(encoding="utf-8")
        assert "COPY pyproject.toml uv.lock ./" in dockerfile
        assert "uv sync --frozen --package " in dockerfile
        assert "--no-dev --no-editable" in dockerfile
        assert "pip install" not in dockerfile
        assert "PYTHONPATH=/riverhog/src" not in dockerfile


def test_munchy_av1_ffmpeg_retains_cuda_features_without_nonfree_code() -> None:
    target = REPO_ROOT / "companions/munchy/server/targets/av1-nvenc"
    dockerfile = (target / "Dockerfile").read_text(encoding="utf-8")
    verification = (target / "verify-ffmpeg").read_text(encoding="utf-8")

    assert "--enable-cuda-llvm" in dockerfile
    assert "clang-20" in dockerfile
    assert "--enable-nvenc" in dockerfile
    assert "--enable-nvdec" in dockerfile
    assert "--enable-cuvid" in dockerfile
    assert "--enable-nonfree" not in dockerfile
    assert "--enable-cuda-nvcc" not in dockerfile
    assert dockerfile.count("git -c http.version=HTTP/1.1 fetch --depth 1 origin") == 2
    assert "https://code.ffmpeg.org/FFmpeg/nv-codec-headers.git" in dockerfile
    assert "https://code.ffmpeg.org/FFmpeg/FFmpeg.git" in dockerfile
    for capability in ("av1_nvenc", "libopus", "av1_cuvid", "scale_cuda", "uhq"):
        assert capability in verification
    assert "FFmpeg must not be built with --enable-nonfree" in verification


def test_munchy_server_image_includes_source_artifact_runtime_tools() -> None:
    dockerfile = (REPO_ROOT / "companions/munchy/server" / "Dockerfile").read_text(encoding="utf-8")

    assert "ffmpeg" in dockerfile
    assert "libimage-exiftool-perl" in dockerfile
    assert "rclone" in dockerfile
    assert "zstd" in dockerfile


def test_workspace_lock_and_app_manifests_own_runtime_dependencies() -> None:
    lock = (REPO_ROOT / "uv.lock").read_text()
    riverhog = (REPO_ROOT / "riverhog/server/pyproject.toml").read_text()
    root = (REPO_ROOT / "pyproject.toml").read_text()

    for package in ("boto3", "fastapi", "psycopg", "sqlalchemy", "uvicorn"):
        assert f'name = "{package}"' in lock
    assert '"psycopg[binary]' in riverhog
    for package in ("pytest", "ruff", "mypy"):
        assert f'"{package}' in root


def test_test_aggregate_runs_lint_then_unit(tmp_path: Path) -> None:
    completed, docker_log_path, uv_log_path = _run_make(tmp_path, "test")

    assert completed.returncode == 0, completed.stderr
    assert _read_log_lines(docker_log_path) == []

    uv_log_lines = _read_log_lines(uv_log_path)
    assert len(uv_log_lines) == 5
    assert "python -m reuse lint" in uv_log_lines[0]
    assert "python -m ruff format --check ." in uv_log_lines[1]
    assert "python -m ruff check ." in uv_log_lines[2]
    assert "python -m mypy companions/jeb/client/src companions/jeb/server/src" in uv_log_lines[3]
    assert (
        "python -m pytest -q companions packages riverhog tests/unit utilities" in uv_log_lines[4]
    )


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
    assert "make build-riverhog" in completed.stdout
    assert "make build-jeb" in completed.stdout
    assert "make build-mango-fish" in completed.stdout
    assert "make build-munchy-server" in completed.stdout
    assert "make build-munchy-av1-nvenc" in completed.stdout
    assert "make build-test" in completed.stdout
    assert "make dist-smoke" in completed.stdout
    assert "make fix" in completed.stdout
    assert "make format" in completed.stdout
    assert "make format-check" in completed.stdout
    assert "make compile" in completed.stdout
    assert "make ruff-fix" in completed.stdout
    assert "make stop-spec" in completed.stdout
    assert "make test" in completed.stdout
    assert "make tus-throughput" in completed.stdout
    assert "args='...'" in completed.stdout
    assert "FILES='...'" in completed.stdout
    assert "PYTHON_PATHS='...'" in completed.stdout
    assert "TESTS='...'" in completed.stdout
    assert "TUS_URL=https://..." in completed.stdout
    assert "fast" not in completed.stdout
    assert _read_log_lines(docker_log_path) == []
    assert _read_log_lines(uv_log_path) == []
