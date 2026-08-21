from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any, cast

import release_installation as installation

ROOT = Path(__file__).resolve().parents[1]
RELEASE_CONFIG = "release.toml"
RELEASE_SCHEMA = "riverhog-release/v1"
NOTICE_SCHEMA = "riverhog-artifact-notices/v1"
NOTICE_POLICY = {
    "schema": NOTICE_SCHEMA,
    "directory": "notices",
    "format": "tar.gz",
    "basis": "exact-artifact-contents",
    "required_for": ["wheel", "image"],
}
RELEASE_ROLES = (
    "end_user_artifact",
    "deployed_implementation",
    "reusable_library",
    "internal_build_unit",
    "test_only_artifact",
)
PROJECT_README = {
    "text": "Riverhog v1 component. See the project URL for documentation and releases.",
    "content-type": "text/markdown",
}
PROJECT_PEOPLE = [{"name": "Nash Spence"}]
PROJECT_CLASSIFIERS = [
    "Programming Language :: Python :: 3 :: Only",
    "Programming Language :: Python :: 3.12",
    "Typing :: Typed",
]
PROJECT_URLS = {
    "Documentation": "https://nashspence.github.io/riverhog/v1/",
    "Issues": "https://github.com/nashspence/riverhog/issues",
    "Repository": "https://github.com/nashspence/riverhog",
}
RUNTIME_IMAGE_TARGETS = {
    "riverhog": {
        "distribution": "riverhog-server",
        "repository": "ghcr.io/nashspence/riverhog",
    },
    "riverhog-ftp-adapter": {
        "distribution": "riverhog-ftp-adapter",
        "repository": "ghcr.io/nashspence/riverhog-ftp-adapter",
    },
    "riverhog-storage-adapter-aws": {
        "distribution": "riverhog-storage-adapter-aws",
        "repository": "ghcr.io/nashspence/riverhog-storage-adapter-aws",
    },
    "riverhog-storage-adapter-backblaze": {
        "distribution": "riverhog-storage-adapter-backblaze",
        "repository": "ghcr.io/nashspence/riverhog-storage-adapter-backblaze",
    },
    "mango-fish": {
        "distribution": "mango-fish",
        "repository": "ghcr.io/nashspence/riverhog-mango-fish",
    },
    "stove0": {
        "distribution": "stove0-server",
        "repository": "ghcr.io/nashspence/riverhog-stove0",
    },
    "stove0-ffprobe-sampling-observer": {
        "distribution": "stove0-ffprobe-sampling-observer",
        "repository": "ghcr.io/nashspence/riverhog-stove0-ffprobe-sampling-observer",
    },
    "stove0-nvenc-av1-opus-target": {
        "distribution": "stove0-nvenc-av1-opus-target",
        "repository": "ghcr.io/nashspence/riverhog-stove0-nvenc-av1-opus-target",
    },
    "stove0-opus-target": {
        "distribution": "stove0-opus-target",
        "repository": "ghcr.io/nashspence/riverhog-stove0-opus-target",
    },
    "stove0-review-target": {
        "distribution": "stove0-review-target",
        "repository": "ghcr.io/nashspence/riverhog-stove0-review-target",
    },
}
TEST_IMAGE_TARGETS = {"test": {"local_tag": "riverhog-test:dev"}}
RELEASE_IMAGE_PLATFORMS = ["linux/amd64"]
END_USER_ARTIFACT_PLATFORMS = ["linux-x64", "macos-arm64", "windows-x64"]
SIGNING_POLICY_KEYS = {
    "checksums",
    "signature",
    "key_owner",
    "secret_key",
    "public_key",
    "rotation",
    "compromise",
    "github_oidc",
}
GOVERNANCE_KEYS = {
    "repository",
    "maintainer",
    "workflow_source_branch",
    "required_check_integration_id",
    "required_checks",
    "main",
    "release",
    "tags",
    "authority",
    "environments",
}
GOVERNANCE_SECTION_KEYS = {
    "main": {"delivery", "protection"},
    "release": {
        "delivery",
        "required_approvals",
        "review_policy",
        "contents",
        "selection",
        "provenance",
        "conflicts",
        "return_to_main",
        "support",
    },
    "tags": {
        "release_candidate",
        "final",
        "immutability",
        "github_releases",
        "failed_publication",
    },
    "authority": {
        "branches",
        "tags",
        "github_releases",
        "registries",
        "pages",
        "provider_qualification",
    },
    "environments": {
        "release",
        "pages",
        "provider_qualification_provisioning",
        "provider_qualification_runtime",
    },
}
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
PROJECT_VERSION_RE = re.compile(r'(?m)^version = "(?P<version>[^"]+)"$')


@dataclass(frozen=True, slots=True)
class Project:
    name: str
    path: str
    role: str
    version: str
    description: str


class ReleaseError(RuntimeError):
    """The release contract is incomplete or inconsistent."""


def _run(
    command: list[str],
    *,
    cwd: Path,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = None
    if env is not None:
        environment = os.environ.copy()
        environment.update(env)
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        env=environment,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _git_output(root: Path, *args: str) -> str:
    return _run(["git", *args], cwd=root, capture=True).stdout.strip()


def _normalize_name(value: str) -> str:
    return value.replace("_", "-").lower()


def _version(value: str) -> tuple[int, int, int]:
    if VERSION_RE.fullmatch(value) is None:
        raise ReleaseError(f"release version must be MAJOR.MINOR.PATCH: {value}")
    parsed = tuple(int(part) for part in value.split("."))
    return parsed[0], parsed[1], parsed[2]


def _dependency_range(version: str) -> str:
    major, minor, _ = _version(version)
    if major == 0:
        return f">=0.{minor},<0.{minor + 1}"
    return f">={major}.0,<{major + 1}.0"


def _workspace_pyprojects(root: Path) -> list[Path]:
    workspace = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    result: list[Path] = []
    for pattern in workspace["tool"]["uv"]["workspace"]["members"]:
        for member in root.glob(pattern):
            pyproject = member / "pyproject.toml"
            if not pyproject.is_file():
                raise ReleaseError(f"workspace member has no pyproject.toml: {member}")
            result.append(pyproject)
    return sorted(result)


def _load_config(root: Path) -> dict[str, Any]:
    return tomllib.loads((root / RELEASE_CONFIG).read_text(encoding="utf-8"))


def _project_metadata(path: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        tomllib.loads(path.read_text(encoding="utf-8"))["project"],
    )


def _bake_targets(root: Path) -> set[str]:
    text = (root / "docker-bake.hcl").read_text(encoding="utf-8")
    group = re.search(r'group "default" \{(?P<body>.*?)\n\}', text, re.DOTALL)
    if group is None:
        raise ReleaseError("docker-bake.hcl has no default target group")
    targets = re.search(r"targets\s*=\s*\[(?P<body>.*?)\]", group.group("body"), re.DOTALL)
    if targets is None:
        raise ReleaseError("docker-bake.hcl default group has no targets")
    return set(re.findall(r'"([^"]+)"', targets.group("body")))


def _buildkit_sbom_attestation(root: Path) -> str:
    text = (root / "docker-bake.hcl").read_text(encoding="utf-8")
    common = re.search(r'target "image-common" \{(?P<body>.*?)\n\}', text, re.DOTALL)
    if common is None:
        raise ReleaseError("docker-bake.hcl has no common image contract")
    attest = re.search(r"attest\s*=\s*\[(?P<body>.*?)\]", common.group("body"), re.DOTALL)
    if attest is None:
        raise ReleaseError("release images do not request a BuildKit SBOM attestation")
    values = re.findall(r'"([^"]+)"', attest.group("body"))
    sboms = [value for value in values if value.startswith("type=sbom,generator=")]
    if len(sboms) != 1 or re.search(r"@sha256:[0-9a-f]{64}$", sboms[0]) is None:
        raise ReleaseError("release images require one digest-pinned BuildKit SBOM generator")
    return str(sboms[0])


def _dependency_name(value: str) -> str:
    match = re.match(r"[A-Za-z0-9_.-]+", value)
    if match is None:
        raise ReleaseError(f"dependency has no distribution name: {value}")
    return _normalize_name(match.group())


def _validate_locked_build_inputs(root: Path, uv_lock: dict[str, Any]) -> None:
    mise_lock = tomllib.loads((root / "mise.lock").read_text(encoding="utf-8"))
    tools = mise_lock.get("tools", {})
    expected_provenance = {
        "age": "github-attestations",
        "minisign": "minisign",
        "python": "github-attestations",
        "uv": "github-attestations",
    }
    for name, provenance in expected_provenance.items():
        entries = tools.get(name)
        if not isinstance(entries, list) or len(entries) != 1:
            raise ReleaseError(f"mise.lock must contain one {name} tool")
        artifact = entries[0].get("platforms.linux-x64")
        if not isinstance(artifact, dict) or artifact.get("provenance") != provenance:
            raise ReleaseError(f"mise.lock lacks verified Linux provenance for {name}")
        checksum = artifact.get("checksum")
        if name != "minisign" and (
            not isinstance(checksum, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", checksum) is None
        ):
            raise ReleaseError(f"mise.lock lacks a Linux SHA-256 checksum for {name}")
    exiftool_entries = tools.get("http:exiftool")
    if not isinstance(exiftool_entries, list) or len(exiftool_entries) != 1:
        raise ReleaseError("mise.lock must contain one exiftool input")
    exiftool = exiftool_entries[0].get("platforms.linux-x64")
    if (
        not isinstance(exiftool, dict)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(exiftool.get("checksum", ""))) is None
    ):
        raise ReleaseError("mise.lock lacks a Linux SHA-256 checksum for exiftool")
    for package in uv_lock["package"]:
        if "registry" not in package.get("source", {}):
            continue
        artifacts = list(package.get("wheels", []))
        if package.get("sdist") is not None:
            artifacts.append(package["sdist"])
        if not artifacts or any(
            re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("hash", ""))) is None
            for item in artifacts
        ):
            raise ReleaseError(f"uv.lock lacks SHA-256 artifacts for {package['name']}")


def validate_release_contract(root: Path, *, expected_version: str | None = None) -> list[Project]:
    config = _load_config(root)
    if config.get("schema") != RELEASE_SCHEMA:
        raise ReleaseError("release.toml has another schema")
    if config.get("series") != "v1" or config.get("release_branch") != "release/v1":
        raise ReleaseError("release.toml does not describe the v1 release line")
    if config.get("tag_template") != "v{version}":
        raise ReleaseError("release tags must use v{version}")
    if config.get("version_policy") != "coordinated":
        raise ReleaseError("Riverhog requires one coordinated product version")
    governance = config.get("governance")
    if not isinstance(governance, dict) or set(governance) != GOVERNANCE_KEYS:
        raise ReleaseError("release.toml lacks the complete GitHub governance contract")
    if governance["repository"] != "nashspence/riverhog":
        raise ReleaseError("release governance must target the canonical repository")
    if governance["maintainer"] != "nashspence" or governance["workflow_source_branch"] != "main":
        raise ReleaseError("release governance lacks its maintainer or workflow authority")
    required_checks = governance["required_checks"]
    if (
        not isinstance(required_checks, list)
        or not required_checks
        or any(not isinstance(item, str) or not item.strip() for item in required_checks)
        or len(required_checks) != len(set(required_checks))
    ):
        raise ReleaseError("release governance required checks must be unique names")
    if governance["required_check_integration_id"] != 15368:
        raise ReleaseError("release checks must be bound to the GitHub Actions application")
    for section, expected_keys in GOVERNANCE_SECTION_KEYS.items():
        values = governance.get(section)
        if not isinstance(values, dict) or set(values) != expected_keys:
            raise ReleaseError(f"release governance lacks the complete {section} policy")
        if any(isinstance(value, str) and not value.strip() for value in values.values()):
            raise ReleaseError(f"release governance {section} policy must be visible")
    if governance["release"]["required_approvals"] != 0:
        raise ReleaseError("the single-maintainer release rail must not require self-review")
    if governance["tags"]["release_candidate"] != "v{version}-rc.{candidate}":
        raise ReleaseError("release-candidate tags must use v{version}-rc.{candidate}")
    if governance["tags"]["final"] != config["tag_template"]:
        raise ReleaseError("final tag governance differs from the release tag template")
    if config.get("installation") != installation.INSTALLATION_POLICY:
        raise ReleaseError("release.toml differs from the v1 installation policy")
    artifacts = config.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {
        "python_formats",
        "documentation",
        "source",
        "evidence",
        "notices",
    }:
        raise ReleaseError("release.toml lacks the complete artifact contract")
    if artifacts["python_formats"] != ["wheel", "sdist"]:
        raise ReleaseError("release.toml requires wheel and sdist Python artifacts")
    if artifacts["notices"] != NOTICE_POLICY:
        raise ReleaseError("release.toml differs from the artifact notice policy")
    _buildkit_sbom_attestation(root)

    workspace = {
        path.parent.relative_to(root).as_posix(): path for path in _workspace_pyprojects(root)
    }
    classified: dict[str, str] = {}
    python_config = config.get("python")
    if not isinstance(python_config, dict) or set(python_config) != set(RELEASE_ROLES):
        raise ReleaseError("release.toml must contain every Python release-unit role")
    for role in RELEASE_ROLES:
        values = python_config[role]
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ReleaseError(f"release role {role} must be a path list")
        for path in values:
            if path in classified:
                raise ReleaseError(f"release unit is classified more than once: {path}")
            classified[path] = role
    if set(classified) != set(workspace):
        missing = sorted(set(workspace) - set(classified))
        extra = sorted(set(classified) - set(workspace))
        raise ReleaseError(
            f"release-unit inventory differs from workspace: missing={missing} extra={extra}"
        )

    projects: list[Project] = []
    seen_names: set[str] = set()
    for relative, pyproject in sorted(workspace.items()):
        metadata = _project_metadata(pyproject)
        name = _normalize_name(str(metadata["name"]))
        if name in seen_names:
            raise ReleaseError(f"workspace repeats distribution name: {name}")
        seen_names.add(name)
        version = str(metadata["version"])
        _version(version)
        if expected_version is not None and version != expected_version:
            raise ReleaseError(f"{name} is {version}, expected {expected_version}")
        if metadata.get("readme") != PROJECT_README:
            raise ReleaseError(f"{name} does not carry the common package README")
        if metadata.get("authors") != PROJECT_PEOPLE:
            raise ReleaseError(f"{name} does not carry canonical authorship")
        if metadata.get("maintainers") != PROJECT_PEOPLE:
            raise ReleaseError(f"{name} does not carry canonical maintainership")
        if metadata.get("classifiers") != PROJECT_CLASSIFIERS:
            raise ReleaseError(f"{name} does not carry canonical classifiers")
        if metadata.get("urls") != PROJECT_URLS:
            raise ReleaseError(f"{name} does not carry canonical project URLs")
        projects.append(
            Project(
                name=name,
                path=relative,
                role=classified[relative],
                version=version,
                description=str(metadata["description"]),
            )
        )

    versions = {item.version for item in projects}
    if len(versions) != 1:
        raise ReleaseError(f"coordinated distributions have different versions: {sorted(versions)}")
    current_version = next(iter(versions))
    expected_range = _dependency_range(current_version)
    for project in projects:
        metadata = _project_metadata(root / project.path / "pyproject.toml")
        for dependency in metadata.get("dependencies", []):
            dependency_name = _dependency_name(str(dependency))
            if dependency_name in seen_names and dependency != dependency_name + expected_range:
                raise ReleaseError(
                    f"{project.name} dependency {dependency_name} must use {expected_range}"
                )

    locked = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    _validate_locked_build_inputs(root, locked)
    locked_versions = {
        _normalize_name(str(item["name"])): str(item["version"])
        for item in locked["package"]
        if _normalize_name(str(item["name"])) in seen_names
    }
    if set(locked_versions) != seen_names:
        raise ReleaseError("uv.lock does not contain every release distribution exactly once")
    if any(value != current_version for value in locked_versions.values()):
        raise ReleaseError("uv.lock release-unit versions differ from pyproject metadata")

    platforms_config = config.get("platforms")
    if not isinstance(platforms_config, dict) or set(platforms_config) != {
        "end_user_artifacts",
        "deployed_implementations",
    }:
        raise ReleaseError("release.toml lacks the complete platform support contract")
    if platforms_config["end_user_artifacts"] != END_USER_ARTIFACT_PLATFORMS:
        raise ReleaseError("v1 end-user artifacts must support Linux, macOS, and Windows")
    if platforms_config["deployed_implementations"] != RELEASE_IMAGE_PLATFORMS:
        raise ReleaseError("v1 deployed implementations must target the release image platforms")

    images_config = config.get("images")
    if not isinstance(images_config, dict) or set(images_config) != {
        "platforms",
        "runtime",
        "test_only",
    }:
        raise ReleaseError("release.toml lacks the complete image release contract")
    if images_config["platforms"] != RELEASE_IMAGE_PLATFORMS:
        raise ReleaseError("v1 release images must target the qualified Linux/amd64 platform")
    runtime_images = images_config["runtime"]
    test_images = images_config["test_only"]
    if runtime_images != RUNTIME_IMAGE_TARGETS or test_images != TEST_IMAGE_TARGETS:
        raise ReleaseError("release image inventory differs from the canonical bake graph")
    if set(runtime_images) | set(test_images) != _bake_targets(root):
        raise ReleaseError("release image inventory differs from docker-bake.hcl")
    image_distributions = {
        _normalize_name(str(value.get("distribution", ""))) for value in runtime_images.values()
    }
    if not image_distributions <= seen_names:
        raise ReleaseError("a runtime image refers to an unknown distribution")
    repositories = [str(value.get("repository", "")) for value in runtime_images.values()]
    if len(set(repositories)) != len(repositories) or any(
        not value.startswith("ghcr.io/nashspence/riverhog") for value in repositories
    ):
        raise ReleaseError("runtime image repositories are absent, duplicated, or outside GHCR")
    compatibility = config.get("compatibility")
    if not isinstance(compatibility, dict) or set(compatibility) != {
        "components",
        "http_api",
        "cli",
        "archive",
        "recovery",
        "configuration",
        "persistent_state",
    }:
        raise ReleaseError("release.toml lacks the complete v1 compatibility policy")
    if any(not str(value).strip() for value in compatibility.values()):
        raise ReleaseError("v1 compatibility promises must be visible")
    signing = config.get("signing")
    if not isinstance(signing, dict) or set(signing) != SIGNING_POLICY_KEYS:
        raise ReleaseError("release.toml lacks the complete release-signing policy")
    if signing["checksums"] != "SHA-256" or signing["signature"] != "minisign":
        raise ReleaseError("release evidence requires SHA-256 and minisign")
    if any(not str(value).strip() for value in signing.values()):
        raise ReleaseError("release-signing ownership and response policy must be visible")
    return projects


def apply_release_version(root: Path, version: str) -> list[Project]:
    major, _, _ = _version(version)
    if major != 1:
        raise ReleaseError("release/v1 accepts only 1.x.y versions")
    projects = validate_release_contract(root)
    current_version = projects[0].version
    current_range = _dependency_range(current_version)
    target_range = _dependency_range(version)
    names = {item.name for item in projects}
    for project in projects:
        path = root / project.path / "pyproject.toml"
        text = path.read_text(encoding="utf-8")
        updated, count = PROJECT_VERSION_RE.subn(f'version = "{version}"', text, count=1)
        if count != 1:
            raise ReleaseError(f"cannot update project version in {project.path}")
        for name in names:
            updated = updated.replace(
                f'"{name}{current_range}"',
                f'"{name}{target_range}"',
            )
        path.write_text(updated, encoding="utf-8")
    _apply_release_version_to_lock(
        root,
        names=names,
        current_version=current_version,
        target_version=version,
        current_range=current_range,
        target_range=target_range,
    )
    return projects


def _apply_release_version_to_lock(
    root: Path,
    *,
    names: set[str],
    current_version: str,
    target_version: str,
    current_range: str,
    target_range: str,
) -> None:
    path = root / "uv.lock"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    current_name: str | None = None
    updated_names: set[str] = set()
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        newline = line[len(content) :]
        if content == "[[package]]":
            current_name = None
            continue
        name_match = re.fullmatch(r'name = "([^"]+)"', content)
        if name_match is not None and current_name is None:
            current_name = _normalize_name(name_match.group(1))
            continue
        if current_name not in names:
            continue
        version_match = re.fullmatch(r'version = "([^"]+)"', content)
        if version_match is None:
            continue
        if version_match.group(1) != current_version:
            raise ReleaseError(f"uv.lock has an unexpected {current_name} version")
        lines[index] = f'version = "{target_version}"{newline}'
        updated_names.add(current_name)
    if updated_names != names:
        missing = sorted(names - updated_names)
        extra = sorted(updated_names - names)
        raise ReleaseError(
            "uv.lock release-unit inventory differs during versioning: "
            f"missing={missing} extra={extra}"
        )
    if current_range != target_range:
        for index, line in enumerate(lines):
            if f'specifier = "{current_range}"' not in line:
                continue
            if any(f'name = "{name}"' in line for name in names):
                lines[index] = line.replace(
                    f'specifier = "{current_range}"',
                    f'specifier = "{target_range}"',
                )
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write("".join(lines))


def _ensure_clean(root: Path) -> None:
    status = _git_output(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise ReleaseError("release operations require a clean worktree; Git reported:\n" + status)


def _trusted_config_paths(checkout: Path) -> str:
    existing = os.environ.get("MISE_TRUSTED_CONFIG_PATHS")
    return os.pathsep.join(value for value in (str(checkout), existing) if value)


def _source_sha(root: Path) -> str:
    value = _git_output(root, "rev-parse", "--verify", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ReleaseError("Git did not return a full source SHA")
    return value


def _previous_tag(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return result.stdout.strip() or None


def _release_notes(root: Path, source_sha: str) -> tuple[str | None, list[dict[str, str]]]:
    previous = _previous_tag(root)
    if previous is None:
        return None, []
    revision_range = f"{previous}..{source_sha}"
    output = _git_output(root, "log", "--format=%H%x09%s", revision_range)
    commits = []
    for line in output.splitlines():
        sha, separator, subject = line.partition("\t")
        if separator:
            commits.append({"sha": sha, "subject": subject})
    return previous, commits


def build_release_plan(root: Path, version: str, *, allow_dirty: bool = False) -> dict[str, Any]:
    major, _, _ = _version(version)
    if major != 1:
        raise ReleaseError("release/v1 accepts only 1.x.y versions")
    if not allow_dirty:
        _ensure_clean(root)
    projects = validate_release_contract(root)
    config = _load_config(root)
    source_sha = _source_sha(root)
    previous_tag, commits = _release_notes(root, source_sha)
    python_artifacts = []
    for project in projects:
        artifact_name = project.name.replace("-", "_")
        python_artifacts.append(
            {
                "name": project.name,
                "path": project.path,
                "role": project.role,
                "current_version": project.version,
                "release_version": version,
                "artifacts": [
                    f"dist/{artifact_name}-{version}-py3-none-any.whl",
                    f"dist/{artifact_name}-{version}.tar.gz",
                ],
            }
        )
    images = []
    for target, value in config["images"]["runtime"].items():
        repository = str(value["repository"])
        images.append(
            {
                "target": target,
                "distribution": value["distribution"],
                "repository": repository,
                "platforms": list(config["images"]["platforms"]),
                "tags": [f"{repository}:{version}", f"{repository}:sha-{source_sha}"],
            }
        )
    supporting = {
        "documentation": config["artifacts"]["documentation"].format(version=version),
        "source": config["artifacts"]["source"].format(version=version),
        "installation": {
            "manifest": "install-manifest.json",
            "locks": [f"pylock.{name}.toml" for name in installation.END_USER_ROOTS],
            "index_snapshot": f"riverhog-python-index-v{version}.tar.gz",
            "gogurt_listener_reference": f"gogurt-listener-v{version}.md",
        },
        "notices": dict(config["artifacts"]["notices"]),
        "evidence": list(config["artifacts"]["evidence"]),
    }
    return {
        "schema": RELEASE_SCHEMA,
        "series": config["series"],
        "version": version,
        "tag": config["tag_template"].format(version=version),
        "source_sha": source_sha,
        "release_branch": config["release_branch"],
        "version_policy": config["version_policy"],
        "compatibility": config["compatibility"],
        "python": python_artifacts,
        "images": images,
        "supporting_artifacts": supporting,
        "release_notes": {
            "previous_tag": previous_tag,
            "commits": commits,
        },
    }


def render_release_markdown(plan: dict[str, Any]) -> str:
    lines = [
        f"# Riverhog {plan['tag']}",
        "",
        f"Source: `{plan['source_sha']}`",
        "",
        "## Release units",
        "",
        "| Distribution | Role | Version |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| `{item['name']}` | `{item['role']}` | `{item['release_version']}` |"
        for item in plan["python"]
    )
    lines.extend(["", "## Runtime images", ""])
    lines.extend(f"- `{item['tags'][0]}` and `{item['tags'][1]}`" for item in plan["images"])
    lines.extend(["", "## Changes", ""])
    commits = plan["release_notes"]["commits"]
    lines.extend(f"- {item['subject']} (`{item['sha'][:12]}`)" for item in commits)
    if not commits:
        if plan["release_notes"]["previous_tag"] is None:
            lines.append("- Initial v1 release; there is no previous release tag.")
        else:
            lines.append("- No commits after the previous release tag.")
    return "\n".join(lines) + "\n"


def apply_command(root: Path, version: str, *, allow_dirty: bool) -> None:
    if not allow_dirty:
        _ensure_clean(root)
    apply_release_version(root, version)
    _run(["uv", "lock", "--offline"], cwd=root)
    validate_release_contract(root, expected_version=version)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _wheel_notice_components(
    path: Path,
    *,
    distribution: str,
    version: str,
) -> list[dict[str, Any]]:
    notices: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            relative = PurePosixPath(name)
            parts = tuple(part.lower() for part in relative.parts)
            basename = relative.name.upper()
            in_metadata = any(part.endswith(".dist-info") for part in parts)
            is_notice = in_metadata and (
                "licenses" in parts
                or basename.startswith(("LICENSE", "COPYING", "NOTICE", "COPYRIGHT"))
            )
            if not is_notice or name.endswith("/"):
                continue
            content = archive.read(name)
            if not content:
                raise ReleaseError(f"wheel contains an empty attribution file: {path.name}:{name}")
            notices.append({"source": name, "content": content})
    if not notices:
        raise ReleaseError(f"wheel contains no packaged license or notice text: {path.name}")
    return [
        {
            "kind": "python",
            "name": distribution,
            "version": version,
            "notices": notices,
        }
    ]


def _notice_tar_info(name: str, content: bytes, *, source_epoch: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = source_epoch
    return info


def _write_notice_bundle(
    destination: Path,
    record: dict[str, Any],
    components: list[dict[str, Any]],
    *,
    source_epoch: int,
) -> None:
    if not components:
        raise ReleaseError(f"artifact has no attribution components: {record['name']}")
    files: dict[str, bytes] = {}
    indexed_components: list[dict[str, Any]] = []
    seen_components: set[tuple[str, str, str]] = set()
    for component in sorted(
        components,
        key=lambda item: (str(item["kind"]), str(item["name"]), str(item["version"])),
    ):
        identity = (
            str(component["kind"]),
            str(component["name"]),
            str(component["version"]),
        )
        if identity in seen_components:
            raise ReleaseError(f"artifact notice bundle repeats a component: {identity}")
        seen_components.add(identity)
        raw_notices = cast(list[dict[str, Any]], component.get("notices"))
        if not raw_notices:
            raise ReleaseError(f"artifact component has no attribution text: {identity}")
        notices: list[dict[str, str]] = []
        seen_sources: set[str] = set()
        for notice in sorted(raw_notices, key=lambda item: str(item["source"])):
            source = str(notice["source"])
            content = notice["content"]
            if (
                not source
                or source in seen_sources
                or not isinstance(content, bytes)
                or not content
            ):
                raise ReleaseError(f"artifact component has invalid attribution text: {identity}")
            seen_sources.add(source)
            digest = hashlib.sha256(content).hexdigest()
            bundle_path = f"files/{digest}"
            existing = files.setdefault(bundle_path, content)
            if existing != content:
                raise ReleaseError("SHA-256 collision while assembling artifact notices")
            notices.append({"source": source, "sha256": digest, "file": bundle_path})
        indexed_components.append(
            {
                "kind": identity[0],
                "name": identity[1],
                "version": identity[2],
                "notices": notices,
            }
        )
    index = {
        "schema": NOTICE_SCHEMA,
        "basis": NOTICE_POLICY["basis"],
        "subject": {
            "kind": str(record["kind"]),
            "name": str(record["name"]),
            "sha256": str(record["sha256"]),
        },
        "components": indexed_components,
    }
    index_bytes = (json.dumps(index, indent=2, sort_keys=True) + "\n").encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=source_epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                archive.addfile(
                    _notice_tar_info("NOTICE.json", index_bytes, source_epoch=source_epoch),
                    io.BytesIO(index_bytes),
                )
                for name, content in sorted(files.items()):
                    archive.addfile(
                        _notice_tar_info(name, content, source_epoch=source_epoch),
                        io.BytesIO(content),
                    )


def _write_subject_notices(
    output: Path,
    records: list[dict[str, Any]],
    *,
    source_epoch: int,
) -> None:
    required = set(NOTICE_POLICY["required_for"])
    for record in records:
        kind = str(record["kind"])
        components = record.pop("_notice_components", None)
        if kind not in required:
            if components is not None:
                raise ReleaseError(f"unexpected artifact notice input for {record['name']}")
            continue
        if not isinstance(components, list):
            raise ReleaseError(f"artifact notice input is absent: {record['name']}")
        slug = re.sub(r"[^A-Za-z0-9.-]+", "-", str(record["name"])).strip("-.")
        relative = f"{NOTICE_POLICY['directory']}/{kind}-{slug}.tar.gz"
        _write_notice_bundle(
            output / relative,
            record,
            cast(list[dict[str, Any]], components),
            source_epoch=source_epoch,
        )
        record["notices"] = relative


def _source_time(root: Path, source_sha: str) -> tuple[int, str, str]:
    epoch_text = _git_output(root, "show", "-s", "--format=%ct", source_sha)
    created = _git_output(root, "show", "-s", "--format=%cI", source_sha)
    if not epoch_text.isdigit():
        raise ReleaseError("Git did not return a source commit epoch")
    epoch = int(epoch_text)
    spdx_created = datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return epoch, created, spdx_created


def _write_source_archive(
    checkout: Path,
    destination: Path,
    *,
    version: str,
    source_epoch: int,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"riverhog-{version}"
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=source_epoch) as compressed:
            with tarfile.open(
                fileobj=compressed,
                mode="w",
                format=tarfile.PAX_FORMAT,
            ) as archive:
                root_info = tarfile.TarInfo(prefix)
                root_info.type = tarfile.DIRTYPE
                root_info.mode = 0o755
                root_info.uid = root_info.gid = 0
                root_info.uname = root_info.gname = "root"
                root_info.mtime = source_epoch
                archive.addfile(root_info)
                for path in sorted(checkout.rglob("*"), key=lambda item: item.as_posix()):
                    relative = path.relative_to(checkout)
                    info = archive.gettarinfo(path, arcname=f"{prefix}/{relative.as_posix()}")
                    info.uid = info.gid = 0
                    info.uname = info.gname = "root"
                    info.mtime = source_epoch
                    if info.isdir():
                        info.mode = 0o755
                    elif info.isfile():
                        info.mode = 0o755 if info.mode & 0o111 else 0o644
                    if info.isfile():
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
                    else:
                        archive.addfile(info)


def _distribution_metadata(path: Path) -> tuple[str, str, set[str]]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ReleaseError(f"wheel has no unique METADATA: {path.name}")
            body = archive.read(names[0])
    else:
        with tarfile.open(path, mode="r:gz") as archive:
            members = [
                member
                for member in archive.getmembers()
                if member.isfile() and PurePosixPath(member.name).name == "PKG-INFO"
            ]
            if len(members) != 1:
                raise ReleaseError(f"sdist has no unique PKG-INFO: {path.name}")
            stream = archive.extractfile(members[0])
            if stream is None:
                raise ReleaseError(f"cannot read sdist PKG-INFO: {path.name}")
            body = stream.read()
    metadata = BytesParser(policy=policy.default).parsebytes(body)
    name = str(metadata.get("Name", ""))
    version = str(metadata.get("Version", ""))
    dependencies = {
        _dependency_name(str(value)) for value in (metadata.get_all("Requires-Dist") or [])
    }
    return _normalize_name(name), version, dependencies


def _project_dependency_graph(
    root: Path,
    projects: list[Project],
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, str]]:
    project_names = {project.name for project in projects}
    internal: dict[str, set[str]] = {}
    direct: dict[str, set[str]] = {}
    licenses: dict[str, str] = {}
    for project in projects:
        metadata = _project_metadata(root / project.path / "pyproject.toml")
        dependencies = {_dependency_name(str(value)) for value in metadata.get("dependencies", [])}
        direct[project.name] = dependencies
        internal[project.name] = dependencies & project_names
        licenses[project.name] = str(metadata.get("license", "NOASSERTION"))
    return internal, direct, licenses


def _dependency_closure(graph: dict[str, set[str]], root: str) -> set[str]:
    pending = [root]
    result: set[str] = set()
    while pending:
        name = pending.pop()
        if name in result:
            continue
        result.add(name)
        pending.extend(graph[name] - result)
    return result


def _locked_versions(root: Path) -> dict[str, str]:
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    versions = {
        _normalize_name(str(item["name"])): str(item["version"]) for item in lock["package"]
    }
    if len(versions) != len(lock["package"]):
        raise ReleaseError("uv.lock contains ambiguous package versions")
    return versions


def _validate_distribution_artifacts(
    root: Path,
    projects: list[Project],
    *,
    version: str,
) -> dict[str, tuple[Project, set[str]]]:
    expected: dict[str, Project] = {}
    for project in projects:
        artifact_name = project.name.replace("-", "_")
        expected[f"{artifact_name}-{version}-py3-none-any.whl"] = project
        expected[f"{artifact_name}-{version}.tar.gz"] = project
    dist = root / "dist"
    actual = {path.name for path in dist.iterdir() if path.is_file()}
    if actual != set(expected):
        raise ReleaseError(
            "distribution artifact set differs from the release-unit inventory: "
            f"missing={sorted(set(expected) - actual)} extra={sorted(actual - set(expected))}"
        )
    _internal, direct, _licenses = _project_dependency_graph(root, projects)
    validated: dict[str, tuple[Project, set[str]]] = {}
    for name, project in expected.items():
        artifact_name, artifact_version, dependencies = _distribution_metadata(dist / name)
        if artifact_name != project.name or artifact_version != version:
            raise ReleaseError(f"artifact identity differs from its release unit: {name}")
        if dependencies != direct[project.name]:
            raise ReleaseError(f"artifact dependencies differ from {project.name}: {name}")
        validated[name] = project, dependencies
    return validated


def _spdx_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9.-]+", "-", value).strip("-.")
    return f"SPDXRef-{normalized or 'Artifact'}"


def _spdx_document(
    *,
    name: str,
    version: str,
    digest: str | None,
    license_expression: str,
    purpose: str,
    components: list[dict[str, str]],
    source_sha: str,
    created: str,
    relationship: str,
) -> dict[str, Any]:
    subject_id = "SPDXRef-Subject"
    subject: dict[str, Any] = {
        "SPDXID": subject_id,
        "name": name,
        "versionInfo": version,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": license_expression,
        "licenseDeclared": license_expression,
        "supplier": "Organization: Riverhog",
        "primaryPackagePurpose": purpose,
    }
    if digest is not None:
        subject["checksums"] = [{"algorithm": "SHA256", "checksumValue": digest}]
    packages = [subject]
    relationships: list[dict[str, str]] = []
    for index, component in enumerate(
        sorted(components, key=lambda item: (item["kind"], item["name"], item["version"])),
        start=1,
    ):
        component_id = _spdx_id(f"Component-{index}-{component['kind']}-{component['name']}")
        packages.append(
            {
                "SPDXID": component_id,
                "name": component["name"],
                "versionInfo": component["version"],
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": component.get("license", "NOASSERTION"),
                "licenseDeclared": component.get("license", "NOASSERTION"),
                "supplier": "NOASSERTION",
                "primaryPackagePurpose": (
                    "OPERATING_SYSTEM" if component["kind"] == "deb" else "LIBRARY"
                ),
            }
        )
        relationships.append(
            {
                "spdxElementId": subject_id,
                "relationshipType": relationship,
                "relatedSpdxElement": component_id,
            }
        )
    namespace_name = re.sub(r"[^A-Za-z0-9.-]+", "-", name).strip("-.")
    namespace_digest = digest or source_sha
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{name} SBOM",
        "documentNamespace": (
            "https://github.com/nashspence/riverhog/releases/"
            f"v{version}/sbom/{namespace_name}-{namespace_digest}"
        ),
        "creationInfo": {
            "created": created,
            "creators": ["Tool: Riverhog release.py"],
        },
        "documentDescribes": [subject_id],
        "packages": packages,
        "relationships": relationships,
    }


IMAGE_INVENTORY_PROGRAM = (
    "import importlib.metadata,json;"
    "print(json.dumps(sorted((d.metadata['Name'],d.version) "
    "for d in importlib.metadata.distributions())))"
)

IMAGE_NOTICE_PROGRAM = r"""
import base64
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import subprocess

MAX_NOTICE_BYTES = 8 * 1024 * 1024


def encoded_notice(path):
    content = path.read_bytes()
    if not content or len(content) > MAX_NOTICE_BYTES:
        raise RuntimeError(f"invalid attribution file size: {path}")
    return {
        "source": str(path),
        "content": base64.b64encode(content).decode("ascii"),
    }


python = []
for distribution in importlib.metadata.distributions():
    notices = []
    for item in distribution.files or []:
        relative = PurePosixPath(str(item))
        parts = tuple(part.lower() for part in relative.parts)
        basename = relative.name.upper()
        in_metadata = any(part.endswith(".dist-info") for part in parts)
        is_notice = in_metadata and (
            "licenses" in parts
            or basename.startswith(("LICENSE", "COPYING", "NOTICE", "COPYRIGHT"))
        )
        if not is_notice:
            continue
        path = Path(distribution.locate_file(item))
        if path.is_file():
            notices.append(encoded_notice(path))
    python.append(
        {
            "kind": "python",
            "name": distribution.metadata["Name"],
            "version": distribution.version,
            "notices": sorted(notices, key=lambda item: item["source"]),
        }
    )

query = subprocess.run(
    [
        "dpkg-query",
        "-W",
        "-f=${binary:Package}\t${Version}\t${Architecture}\t${source:Package}\n",
    ],
    check=True,
    text=True,
    stdout=subprocess.PIPE,
).stdout
packages = []
for line in query.splitlines():
    name, version, architecture, source = line.split("\t")
    packages.append(
        {
            "name": name,
            "base": name.partition(":")[0],
            "version": version,
            "architecture": architecture,
            "source": (source or name).partition(":")[0],
        }
    )

copyrights = {}
for package in packages:
    path = Path("/usr/share/doc") / package["base"] / "copyright"
    if path.is_file():
        copyrights[package["name"]] = path

deb = []
missing = []
for package in packages:
    path = copyrights.get(package["name"])
    if path is None:
        path = next(
            (
                copyrights[candidate["name"]]
                for candidate in packages
                if candidate["source"] == package["source"]
                and candidate["name"] in copyrights
            ),
            None,
        )
    if path is None:
        listed = subprocess.run(
            ["dpkg-query", "-L", package["name"]],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.splitlines()
        payload = []
        for value in listed:
            candidate = Path(value)
            if value.startswith(("/usr/share/doc/", "/usr/share/man/", "/usr/share/lintian/")):
                continue
            if candidate.is_file() or candidate.is_symlink():
                payload.append(value)
        if payload:
            missing.append(package["name"])
        continue
    deb.append(
        {
            "kind": "deb",
            "name": f"{package['name']}:{package['architecture']}",
            "version": package["version"],
            "notices": [encoded_notice(path)],
        }
    )

standalone = []
for standalone_root in (
    Path("/usr/share/licenses/riverhog-third-party"),
    Path("/usr/local/share/licenses/riverhog-third-party"),
):
    if standalone_root.is_dir():
        for component in sorted(standalone_root.iterdir()):
            if not component.is_dir():
                raise RuntimeError(f"invalid standalone attribution component: {component}")
            for version in sorted(component.iterdir()):
                if not version.is_dir():
                    raise RuntimeError(f"invalid standalone attribution version: {version}")
                paths = sorted(path for path in version.rglob("*") if path.is_file())
                if not paths:
                    raise RuntimeError(f"standalone component has no attribution text: {version}")
                standalone.append(
                    {
                        "kind": "standalone",
                        "name": component.name,
                        "version": version.name,
                        "notices": [encoded_notice(path) for path in paths],
                    }
                )

print(json.dumps({"python": python, "deb": deb, "standalone": standalone, "missing": missing}))
"""


def _image_notice_components(
    root: Path,
    reference: str,
    *,
    first_party: set[str],
) -> list[dict[str, Any]]:
    raw = _run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--entrypoint",
            "/opt/venv/bin/python",
            reference,
            "-c",
            IMAGE_NOTICE_PROGRAM,
        ],
        cwd=root,
        capture=True,
    ).stdout
    payload = cast(dict[str, Any], json.loads(raw))
    missing = payload.get("missing")
    if not isinstance(missing, list) or any(not isinstance(item, str) for item in missing):
        raise ReleaseError("image attribution inventory returned an invalid missing list")
    if missing:
        raise ReleaseError(
            "image payload packages have no packaged attribution text: "
            + ", ".join(sorted(missing))
        )
    result: list[dict[str, Any]] = []
    for item in [
        *cast(list[dict[str, Any]], payload.get("python")),
        *cast(list[dict[str, Any]], payload.get("deb")),
        *cast(list[dict[str, Any]], payload.get("standalone")),
    ]:
        name = str(item.get("name", ""))
        kind = str(item.get("kind", ""))
        if kind == "python" and _normalize_name(name) in first_party:
            continue
        raw_notices = item.get("notices")
        if not isinstance(raw_notices, list) or not raw_notices:
            raise ReleaseError(f"image component has no packaged attribution text: {kind}:{name}")
        notices = []
        for notice in raw_notices:
            if not isinstance(notice, dict):
                raise ReleaseError("image attribution inventory returned an invalid notice")
            try:
                content = base64.b64decode(str(notice["content"]), validate=True)
            except (KeyError, ValueError) as error:
                raise ReleaseError(
                    "image attribution inventory returned invalid content"
                ) from error
            notices.append({"source": str(notice.get("source", "")), "content": content})
        result.append(
            {
                "kind": kind,
                "name": name,
                "version": str(item.get("version", "")),
                "notices": notices,
            }
        )
    if not result:
        raise ReleaseError("image contains no third-party attribution components")
    return result


def _docker_image_exists(reference: str, *, cwd: Path) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", reference],
        cwd=cwd,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _remove_release_image_tags(tags: list[str], *, cwd: Path) -> None:
    for tag in reversed(tags):
        if not _docker_image_exists(tag, cwd=cwd):
            continue
        subprocess.run(
            ["docker", "image", "rm", tag],
            cwd=cwd,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    remaining = [tag for tag in tags if _docker_image_exists(tag, cwd=cwd)]
    if remaining:
        raise ReleaseError("could not remove temporary release image tags: " + ", ".join(remaining))


def _build_release_images(
    root: Path,
    projects: list[Project],
    *,
    version: str,
    source_sha: str,
    source_epoch: int,
    created: str,
    cleanup_tags: list[str],
) -> list[dict[str, Any]]:
    config = _load_config(root)
    graph, _direct, licenses = _project_dependency_graph(root, projects)
    project_versions = {project.name: project.version for project in projects}
    project_names = set(project_versions)
    bake = (root / "docker-bake.hcl").read_text(encoding="utf-8")
    sbom_attestation = _buildkit_sbom_attestation(root)
    github_cache = os.environ.get("RIVERHOG_RELEASE_GHA_CACHE") == "true"
    records: list[dict[str, Any]] = []
    for target, image in config["images"]["runtime"].items():
        distribution = _normalize_name(str(image["distribution"]))
        local_repository = f"riverhog-release-dry-run-{source_sha[:12]}/{target}"
        local_version_tag = f"{local_repository}:{version}"
        local_sha_tag = f"{local_repository}:sha-{source_sha}"
        if _docker_image_exists(local_version_tag, cwd=root) or _docker_image_exists(
            local_sha_tag, cwd=root
        ):
            raise ReleaseError(f"temporary release image tag already exists: {local_repository}")
        cleanup_tags.extend((local_version_tag, local_sha_tag))
        build_command = [
            "docker",
            "buildx",
            "bake",
            "--file",
            "docker-bake.hcl",
            "--load",
            "--set",
            f"{target}.tags={local_version_tag}",
            "--set",
            f"{target}.args.SOURCE_REVISION={source_sha}",
            "--set",
            f"{target}.args.BUILD_CREATED={created}",
            "--set",
            f"{target}.args.SOURCE_DATE_EPOCH={source_epoch}",
            "--set",
            f"{target}.args.RELEASE_VERSION={version}",
        ]
        if github_cache:
            build_command.extend(
                [
                    "--set",
                    f"{target}.cache-from=type=gha,scope={target}",
                    "--set",
                    f"{target}.cache-to=type=gha,scope={target},mode=min,ignore-error=true",
                ]
            )
        build_command.append(target)
        _run(
            build_command,
            cwd=root,
            env={"SOURCE_DATE_EPOCH": str(source_epoch)},
        )
        _run(["docker", "tag", local_version_tag, local_sha_tag], cwd=root)
        inspect = cast(
            list[dict[str, Any]],
            json.loads(
                _run(
                    ["docker", "image", "inspect", local_version_tag],
                    cwd=root,
                    capture=True,
                ).stdout
            ),
        )
        if len(inspect) != 1:
            raise ReleaseError(f"Docker returned no unique image: {target}")
        image_data = inspect[0]
        second_id = _run(
            ["docker", "image", "inspect", local_sha_tag, "--format", "{{.Id}}"],
            cwd=root,
            capture=True,
        ).stdout.strip()
        digest = str(image_data.get("Id", ""))
        if not digest.startswith("sha256:") or second_id != digest:
            raise ReleaseError(f"semantic and source image tags differ: {target}")
        labels = cast(dict[str, str], image_data.get("Config", {}).get("Labels") or {})
        expected_labels = {
            "org.opencontainers.image.source": "https://github.com/nashspence/riverhog",
            "org.opencontainers.image.revision": source_sha,
            "org.opencontainers.image.version": version,
            "org.opencontainers.image.created": created,
            "org.opencontainers.image.documentation": ("https://nashspence.github.io/riverhog/v1/"),
        }
        if any(labels.get(key) != value for key, value in expected_labels.items()):
            raise ReleaseError(f"release image labels differ from the release plan: {target}")
        if image_data.get("Os") != "linux" or image_data.get("Architecture") != "amd64":
            raise ReleaseError(f"release image has an unqualified platform: {target}")
        notice_components = _image_notice_components(
            root,
            local_version_tag,
            first_party=project_names,
        )
        installed_raw = _run(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--entrypoint",
                "/opt/venv/bin/python",
                local_version_tag,
                "-c",
                IMAGE_INVENTORY_PROGRAM,
            ],
            cwd=root,
            capture=True,
        ).stdout
        installed_pairs = cast(list[list[str]], json.loads(installed_raw))
        installed = {_normalize_name(name): value for name, value in installed_pairs}
        expected_internal = _dependency_closure(graph, distribution)
        installed_internal = set(installed) & project_names
        if installed_internal != expected_internal:
            raise ReleaseError(
                f"image {target} release-unit closure differs: "
                f"missing={sorted(expected_internal - installed_internal)} "
                f"extra={sorted(installed_internal - expected_internal)}"
            )
        if any(installed[name] != project_versions[name] for name in expected_internal):
            raise ReleaseError(f"image {target} contains another release-unit version")
        os_inventory = _run(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--entrypoint",
                "/bin/sh",
                local_version_tag,
                "-c",
                "dpkg-query -W -f='${binary:Package}\\t${Version}\\t${Architecture}\\n'",
            ],
            cwd=root,
            capture=True,
        ).stdout
        components = [
            {
                "kind": "python",
                "name": name,
                "version": package_version,
                "license": licenses.get(name, "NOASSERTION"),
            }
            for name, package_version in installed.items()
        ]
        for line in os_inventory.splitlines():
            name, separator, remainder = line.partition("\t")
            package_version, separator_two, architecture = remainder.partition("\t")
            if not separator or not separator_two:
                raise ReleaseError(f"cannot parse Debian inventory in image {target}")
            components.append(
                {
                    "kind": "deb",
                    "name": f"{name}:{architecture}",
                    "version": package_version,
                    "license": "NOASSERTION",
                }
            )
        repository = str(image["repository"])
        dockerfile_match = re.search(
            rf'target "{re.escape(target)}" \{{.*?dockerfile\s*=\s*"([^"]+)"',
            bake,
            re.DOTALL,
        )
        if dockerfile_match is None:
            raise ReleaseError(f"release image target has no Dockerfile: {target}")
        records.append(
            {
                "kind": "image",
                "name": repository,
                "sha256": digest.removeprefix("sha256:"),
                "size": int(image_data.get("Size", 0)),
                "distribution": distribution,
                "version": version,
                "license": labels.get("org.opencontainers.image.licenses", "NOASSERTION"),
                "platforms": list(config["images"]["platforms"]),
                "tags": [f"{repository}:{version}", f"{repository}:sha-{source_sha}"],
                "dependencies": [
                    {"name": name, "version": project_versions[name]}
                    for name in sorted(expected_internal - {distribution})
                ],
                "dockerfile": dockerfile_match.group(1),
                "buildkit_sbom_attestation": sbom_attestation,
                "_components": components,
                "_notice_components": notice_components,
            }
        )
    return records


def _write_subject_sboms(
    output: Path,
    records: list[dict[str, Any]],
    *,
    source_sha: str,
    created: str,
) -> None:
    for record in records:
        slug = re.sub(r"[^A-Za-z0-9.-]+", "-", str(record["name"])).strip("-.")
        sbom_relative = f"sbom/{record['kind']}-{slug}.spdx.json"
        components = cast(list[dict[str, str]], record.pop("_components", []))
        purpose = {
            "image": "CONTAINER",
            "install-index": "ARCHIVE",
            "install-lock": "OTHER",
            "install-reference": "FILE",
            "source": "SOURCE",
            "wheel": "LIBRARY",
            "sdist": "ARCHIVE",
        }[str(record["kind"])]
        relationship = (
            "CONTAINS" if record["kind"] in {"image", "install-index", "source"} else "DEPENDS_ON"
        )
        _write_json(
            output / sbom_relative,
            _spdx_document(
                name=str(record["name"]),
                version=str(record["version"]),
                digest=str(record["sha256"]),
                license_expression=str(record["license"]),
                purpose=purpose,
                components=components,
                source_sha=source_sha,
                created=created,
                relationship=relationship,
            ),
        )
        record["sbom"] = sbom_relative


def _write_release_spdx(
    output: Path,
    records: list[dict[str, Any]],
    *,
    version: str,
    source_sha: str,
    created: str,
) -> None:
    components = [
        {
            "kind": str(record["kind"]),
            "name": str(record["name"]),
            "version": str(record["version"]),
            "license": str(record["license"]),
        }
        for record in records
    ]
    _write_json(
        output / "release.spdx.json",
        _spdx_document(
            name="Riverhog",
            version=version,
            digest=None,
            license_expression="CAL-1.0 AND Apache-2.0",
            purpose="APPLICATION",
            components=components,
            source_sha=source_sha,
            created=created,
            relationship="CONTAINS",
        ),
    )


def _write_release_provenance(
    root: Path,
    output: Path,
    records: list[dict[str, Any]],
    *,
    version: str,
    source_sha: str,
    created: str,
) -> None:
    common_materials = [
        {
            "uri": f"git+https://github.com/nashspence/riverhog@{source_sha}",
            "digest": {"gitCommit": source_sha},
        },
        {
            "uri": "uv.lock",
            "digest": {"sha256": _sha256_file(root / "uv.lock")},
        },
        {
            "uri": "mise.lock",
            "digest": {"sha256": _sha256_file(root / "mise.lock")},
        },
    ]
    statements = []
    for record in sorted(records, key=lambda item: (str(item["kind"]), str(item["name"]))):
        materials = list(common_materials)
        if record["kind"] == "image":
            dockerfile = root / str(record["dockerfile"])
            materials.append(
                {
                    "uri": str(record["dockerfile"]),
                    "digest": {"sha256": _sha256_file(dockerfile)},
                }
            )
        statements.append(
            {
                "_type": "https://in-toto.io/Statement/v1",
                "subject": [
                    {
                        "name": str(record["name"]),
                        "digest": {"sha256": str(record["sha256"])},
                    }
                ],
                "predicateType": "https://slsa.dev/provenance/v1",
                "predicate": {
                    "buildDefinition": {
                        "buildType": (
                            "https://github.com/nashspence/riverhog/blob/main/scripts/release.py#v1"
                        ),
                        "externalParameters": {
                            "kind": record["kind"],
                            "version": version,
                            "sourceSha": source_sha,
                            "platforms": record.get("platforms", []),
                            "tags": record.get("tags", []),
                        },
                        "internalParameters": {
                            "releaseUnitClosure": record.get("dependencies", [])
                        },
                        "resolvedDependencies": materials,
                    },
                    "runDetails": {
                        "builder": {
                            "id": (
                                "https://github.com/nashspence/riverhog/"
                                "blob/main/scripts/release.py"
                            )
                        },
                        "metadata": {
                            "invocationId": f"riverhog-v{version}-{source_sha}",
                            "startedOn": created,
                            "finishedOn": created,
                        },
                        "byproducts": [
                            {
                                "name": str(record["sbom"]),
                                "digest": {"sha256": _sha256_file(output / str(record["sbom"]))},
                            },
                            *(
                                [
                                    {
                                        "name": str(record["notices"]),
                                        "digest": {
                                            "sha256": _sha256_file(output / str(record["notices"]))
                                        },
                                    }
                                ]
                                if "notices" in record
                                else []
                            ),
                        ],
                    },
                },
            }
        )
    provenance = output / "release.intoto.jsonl"
    provenance.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in statements),
        encoding="utf-8",
    )


def _write_checksums(output: Path) -> None:
    excluded = {"SHA256SUMS", "SHA256SUMS.minisig"}
    files = sorted(
        path
        for path in output.rglob("*")
        if path.is_file() and path.relative_to(output).as_posix() not in excluded
    )
    (output / "SHA256SUMS").write_text(
        "".join(f"{_sha256_file(path)}  {path.relative_to(output).as_posix()}\n" for path in files),
        encoding="utf-8",
    )


def _sign_checksums(
    output: Path,
    *,
    signing_key: Path,
    version: str,
    source_sha: str,
) -> None:
    _run(
        [
            "minisign",
            "-S",
            "-s",
            str(signing_key),
            "-m",
            str(output / "SHA256SUMS"),
            "-x",
            str(output / "SHA256SUMS.minisig"),
            "-t",
            f"Riverhog v{version} checksums",
            "-c",
            f"source {source_sha}",
        ],
        cwd=output,
    )


def _verify_notice_bundle(path: Path, subject: dict[str, Any]) -> int:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            if (
                len(names) != len(set(names))
                or "NOTICE.json" not in names
                or any(
                    not member.isfile()
                    or PurePosixPath(member.name).is_absolute()
                    or ".." in PurePosixPath(member.name).parts
                    for member in members
                )
            ):
                raise ReleaseError(f"artifact notice archive is unsafe: {path.name}")
            contents: dict[str, bytes] = {}
            for member in members:
                stream = archive.extractfile(member)
                if stream is None:
                    raise ReleaseError(f"artifact notice archive cannot be read: {path.name}")
                contents[member.name] = stream.read()
    except (tarfile.TarError, OSError) as error:
        raise ReleaseError(f"artifact notice archive is invalid: {path.name}") from error
    try:
        index = cast(dict[str, Any], json.loads(contents["NOTICE.json"]))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseError(f"artifact notice index is invalid: {path.name}") from error
    if (
        set(index) != {"schema", "basis", "subject", "components"}
        or index["schema"] != NOTICE_SCHEMA
        or index["basis"] != NOTICE_POLICY["basis"]
    ):
        raise ReleaseError(f"artifact notice index uses another schema: {path.name}")
    expected_subject = {
        "kind": str(subject["kind"]),
        "name": str(subject["name"]),
        "sha256": str(subject["sha256"]),
    }
    if index["subject"] != expected_subject:
        raise ReleaseError(f"artifact notice index describes another subject: {path.name}")
    components = index["components"]
    if not isinstance(components, list) or not components:
        raise ReleaseError(f"artifact notice index has no components: {path.name}")
    referenced = {"NOTICE.json"}
    identities: set[tuple[str, str, str]] = set()
    for component in components:
        if not isinstance(component, dict) or set(component) != {
            "kind",
            "name",
            "version",
            "notices",
        }:
            raise ReleaseError(f"artifact notice component is invalid: {path.name}")
        identity = (
            str(component["kind"]),
            str(component["name"]),
            str(component["version"]),
        )
        if not all(identity) or identity in identities:
            raise ReleaseError(f"artifact notice component identity is invalid: {path.name}")
        identities.add(identity)
        notices = component["notices"]
        if not isinstance(notices, list) or not notices:
            raise ReleaseError(f"artifact notice component has no text: {path.name}")
        sources: set[str] = set()
        for notice in notices:
            if not isinstance(notice, dict) or set(notice) != {"source", "sha256", "file"}:
                raise ReleaseError(f"artifact notice reference is invalid: {path.name}")
            source = str(notice["source"])
            digest = str(notice["sha256"])
            name = str(notice["file"])
            if (
                not source
                or source in sources
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or name != f"files/{digest}"
                or name not in contents
                or hashlib.sha256(contents[name]).hexdigest() != digest
            ):
                raise ReleaseError(f"artifact notice text does not verify: {path.name}")
            sources.add(source)
            referenced.add(name)
    if set(contents) != referenced:
        raise ReleaseError(f"artifact notice archive contains unindexed text: {path.name}")
    return len(components)


def verify_release_evidence(
    root: Path,
    output: Path,
    *,
    public_key: Path,
) -> dict[str, Any]:
    config = _load_config(root)
    required = set(config["artifacts"]["evidence"])
    absent = sorted(name for name in required if not (output / name).is_file())
    if absent:
        raise ReleaseError(f"release evidence is absent: {absent}")
    entries: dict[str, str] = {}
    for line in (output / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        relative = PurePosixPath(name)
        if (
            not separator
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or relative.is_absolute()
            or ".." in relative.parts
            or name in entries
        ):
            raise ReleaseError("SHA256SUMS contains an invalid or repeated entry")
        path = output.joinpath(*relative.parts)
        if not path.is_file() or _sha256_file(path) != digest:
            raise ReleaseError(f"release checksum does not verify: {name}")
        entries[name] = digest
    expected_entries = {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file() and path.name not in {"SHA256SUMS", "SHA256SUMS.minisig"}
    }
    if set(entries) != expected_entries:
        raise ReleaseError("SHA256SUMS does not cover every release artifact and evidence file")
    _run(
        [
            "minisign",
            "-V",
            "-p",
            str(public_key),
            "-m",
            str(output / "SHA256SUMS"),
            "-x",
            str(output / "SHA256SUMS.minisig"),
        ],
        cwd=output,
        capture=True,
    )
    manifest = cast(
        dict[str, Any],
        json.loads((output / "release-manifest.json").read_text(encoding="utf-8")),
    )
    if manifest.get("schema") != RELEASE_SCHEMA:
        raise ReleaseError("release manifest uses another schema")
    if manifest.get("notices") != config["artifacts"]["notices"]:
        raise ReleaseError("release manifest differs from the artifact notice policy")
    install_manifest = cast(
        dict[str, Any],
        json.loads((output / "install-manifest.json").read_text(encoding="utf-8")),
    )
    installation.verify_installation_artifacts(output, install_manifest)
    if manifest.get("installation") != {
        "manifest": "install-manifest.json",
        "sha256": _sha256_file(output / "install-manifest.json"),
    }:
        raise ReleaseError("release manifest installation identity differs from its evidence")
    subjects = cast(list[dict[str, Any]], manifest.get("subjects"))
    if not subjects:
        raise ReleaseError("release manifest contains no subjects")
    subject_keys = {(str(item["name"]), str(item["sha256"])) for item in subjects}
    if len(subject_keys) != len(subjects):
        raise ReleaseError("release manifest repeats a subject")
    required_notices = set(config["artifacts"]["notices"]["required_for"])
    notice_components = 0
    sbom_attestation = _buildkit_sbom_attestation(root)
    for subject in subjects:
        sbom_path = output / str(subject["sbom"])
        sbom = cast(dict[str, Any], json.loads(sbom_path.read_text(encoding="utf-8")))
        if sbom.get("spdxVersion") != "SPDX-2.3" or not sbom.get("documentDescribes"):
            raise ReleaseError(f"artifact SBOM is invalid: {subject['sbom']}")
        if subject["kind"] in required_notices:
            notice_relative = str(subject.get("notices", ""))
            expected_prefix = f"{config['artifacts']['notices']['directory']}/"
            if not notice_relative.startswith(expected_prefix) or notice_relative not in entries:
                raise ReleaseError(f"artifact notice bundle is absent: {subject['name']}")
            notice_components += _verify_notice_bundle(output / notice_relative, subject)
        elif "notices" in subject:
            raise ReleaseError(f"artifact has an unexpected notice bundle: {subject['name']}")
        if subject["kind"] == "image":
            if subject.get("buildkit_sbom_attestation") != sbom_attestation:
                raise ReleaseError(
                    f"image lacks its BuildKit SBOM attestation policy: {subject['name']}"
                )
        if subject["kind"] != "image":
            if entries.get(str(subject["name"])) != subject["sha256"]:
                raise ReleaseError(f"manifest digest differs from checksums: {subject['name']}")
    provenance_subjects: set[tuple[str, str]] = set()
    provenance_byproducts: dict[tuple[str, str], set[str]] = {}
    for line in (output / "release.intoto.jsonl").read_text(encoding="utf-8").splitlines():
        statement = cast(dict[str, Any], json.loads(line))
        if statement.get("predicateType") != "https://slsa.dev/provenance/v1":
            raise ReleaseError("release provenance uses another predicate")
        statement_subject = statement["subject"]
        if not isinstance(statement_subject, list) or len(statement_subject) != 1:
            raise ReleaseError("release provenance statement has no unique subject")
        key = (
            str(statement_subject[0]["name"]),
            str(statement_subject[0]["digest"]["sha256"]),
        )
        provenance_subjects.add(key)
        raw_byproducts = statement["predicate"]["runDetails"]["byproducts"]
        if not isinstance(raw_byproducts, list):
            raise ReleaseError("release provenance byproducts are invalid")
        byproducts: set[str] = set()
        for item in raw_byproducts:
            if not isinstance(item, dict):
                raise ReleaseError("release provenance byproduct is invalid")
            name = str(item.get("name", ""))
            digest = str(item.get("digest", {}).get("sha256", ""))
            if not name or name in byproducts or name not in entries or digest != entries[name]:
                raise ReleaseError("release provenance byproduct does not verify")
            byproducts.add(name)
        provenance_byproducts[key] = byproducts
    if provenance_subjects != subject_keys:
        raise ReleaseError("release provenance does not cover every manifest subject")
    for subject in subjects:
        key = (str(subject["name"]), str(subject["sha256"]))
        expected_byproducts = {str(subject["sbom"])}
        if "notices" in subject:
            expected_byproducts.add(str(subject["notices"]))
        if provenance_byproducts.get(key) != expected_byproducts:
            raise ReleaseError(f"release provenance byproducts differ: {subject['name']}")
    release_spdx = cast(
        dict[str, Any],
        json.loads((output / "release.spdx.json").read_text(encoding="utf-8")),
    )
    if release_spdx.get("spdxVersion") != "SPDX-2.3":
        raise ReleaseError("release-wide SBOM is not SPDX 2.3")
    return {
        "subjects": len(subjects),
        "files": len(entries),
        "notice_components": notice_components,
        "signature_verified": True,
    }


def _prepare_file_subjects(
    root: Path,
    output: Path,
    projects: list[Project],
    validated: dict[str, tuple[Project, set[str]]],
    *,
    version: str,
    source_archive: Path,
) -> list[dict[str, Any]]:
    graph, _direct, licenses = _project_dependency_graph(root, projects)
    locked_versions = _locked_versions(root)
    project_versions = {project.name: project.version for project in projects}
    records: list[dict[str, Any]] = []
    python_output = output / "python"
    python_output.mkdir(parents=True, exist_ok=True)
    for name, (project, dependencies) in sorted(validated.items()):
        destination = python_output / name
        shutil.copy2(root / "dist" / name, destination)
        relative = destination.relative_to(output).as_posix()
        components = [
            {
                "kind": "python",
                "name": dependency,
                "version": locked_versions[dependency],
                "license": licenses.get(dependency, "NOASSERTION"),
            }
            for dependency in sorted(dependencies)
        ]
        internal_closure = _dependency_closure(graph, project.name)
        record: dict[str, Any] = {
            "kind": "wheel" if name.endswith(".whl") else "sdist",
            "name": relative,
            "sha256": _sha256_file(destination),
            "size": destination.stat().st_size,
            "distribution": project.name,
            "version": version,
            "license": licenses[project.name],
            "dependencies": [
                {"name": dependency, "version": project_versions[dependency]}
                for dependency in sorted(internal_closure - {project.name})
            ],
            "_components": components,
        }
        if record["kind"] == "wheel":
            record["_notice_components"] = _wheel_notice_components(
                destination,
                distribution=project.name,
                version=version,
            )
        records.append(record)
    source_relative = source_archive.relative_to(output).as_posix()
    records.append(
        {
            "kind": "source",
            "name": source_relative,
            "sha256": _sha256_file(source_archive),
            "size": source_archive.stat().st_size,
            "version": version,
            "license": "CAL-1.0 AND Apache-2.0",
            "dependencies": [
                {"name": project.name, "version": project.version}
                for project in sorted(projects, key=lambda item: item.name)
            ],
            "_components": [
                {
                    "kind": "python",
                    "name": project.name,
                    "version": project.version,
                    "license": licenses[project.name],
                }
                for project in projects
            ],
        }
    )
    return records


def _generate_release_evidence(
    root: Path,
    output: Path,
    records: list[dict[str, Any]],
    *,
    version: str,
    source_sha: str,
    source_epoch: int,
    spdx_created: str,
    install_manifest: dict[str, Any],
    signing_key: Path,
    public_key: Path,
) -> dict[str, Any]:
    shutil.copy2(root / "THIRD_PARTY_NOTICES.md", output / "THIRD_PARTY_NOTICES.md")
    written_install_manifest = cast(
        dict[str, Any],
        json.loads((output / "install-manifest.json").read_text(encoding="utf-8")),
    )
    if written_install_manifest != install_manifest:
        raise ReleaseError("generated install manifest changed before evidence assembly")
    installation.verify_installation_artifacts(output, install_manifest)
    _write_subject_notices(output, records, source_epoch=source_epoch)
    _write_subject_sboms(output, records, source_sha=source_sha, created=spdx_created)
    _write_release_spdx(
        output,
        records,
        version=version,
        source_sha=source_sha,
        created=spdx_created,
    )
    config = _load_config(root)
    manifest = {
        "schema": RELEASE_SCHEMA,
        "version": version,
        "tag": config["tag_template"].format(version=version),
        "source_sha": source_sha,
        "created": spdx_created,
        "platforms": config["images"]["platforms"],
        "notices": config["artifacts"]["notices"],
        "subjects": sorted(records, key=lambda item: (str(item["kind"]), str(item["name"]))),
        "installation": {
            "manifest": "install-manifest.json",
            "sha256": _sha256_file(output / "install-manifest.json"),
        },
        "evidence": config["artifacts"]["evidence"],
        "signing": config["signing"],
        "published": False,
    }
    _write_json(output / "release-manifest.json", manifest)
    _write_release_provenance(
        root,
        output,
        records,
        version=version,
        source_sha=source_sha,
        created=spdx_created,
    )
    _write_checksums(output)
    _sign_checksums(
        output,
        signing_key=signing_key,
        version=version,
        source_sha=source_sha,
    )
    return verify_release_evidence(root, output, public_key=public_key)


def build_release_evidence(
    root: Path,
    version: str,
    output: Path,
    *,
    signing_key: Path,
    public_key: Path,
) -> dict[str, Any]:
    _ensure_clean(root)
    source_sha = _source_sha(root)
    source_epoch, image_created, spdx_created = _source_time(root, source_sha)
    if output.exists() and any(output.iterdir()):
        raise ReleaseError(f"release evidence output is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", source_sha],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    cleanup_tags: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="riverhog-release-build.") as temporary:
            checkout = Path(temporary) / "riverhog"
            checkout.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
                stream.extractall(checkout, filter="data")
            apply_release_version(checkout, version)
            _run(["uv", "lock", "--offline"], cwd=checkout)
            projects = validate_release_contract(checkout, expected_version=version)
            source_name = _load_config(checkout)["artifacts"]["source"].format(version=version)
            source_archive = output / source_name
            _write_source_archive(
                checkout,
                source_archive,
                version=version,
                source_epoch=source_epoch,
            )
            _run(
                ["make", "dist-smoke"],
                cwd=checkout,
                env={
                    "MISE_TRUSTED_CONFIG_PATHS": _trusted_config_paths(checkout),
                    "SOURCE_DATE_EPOCH": str(source_epoch),
                },
            )
            validated = _validate_distribution_artifacts(
                checkout,
                projects,
                version=version,
            )
            records = _prepare_file_subjects(
                checkout,
                output,
                projects,
                validated,
                version=version,
                source_archive=source_archive,
            )
            install_manifest, install_records = installation.build_installation_artifacts(
                checkout,
                output,
                projects,
                records,
                version=version,
                source_sha=source_sha,
                source_epoch=source_epoch,
                repository=str(_load_config(checkout)["governance"]["repository"]),
                simple_index_path=str(_load_config(checkout)["installation"]["simple_index_path"]),
            )
            records.extend(install_records)
            records.extend(
                _build_release_images(
                    checkout,
                    projects,
                    version=version,
                    source_sha=source_sha,
                    source_epoch=source_epoch,
                    created=image_created,
                    cleanup_tags=cleanup_tags,
                )
            )
            verification = _generate_release_evidence(
                checkout,
                output,
                records,
                version=version,
                source_sha=source_sha,
                source_epoch=source_epoch,
                spdx_created=spdx_created,
                install_manifest=install_manifest,
                signing_key=signing_key,
                public_key=public_key,
            )
    finally:
        _remove_release_image_tags(cleanup_tags, cwd=root)
    return {
        "schema": RELEASE_SCHEMA,
        "version": version,
        "tag": f"v{version}",
        "source_sha": source_sha,
        "python_distributions": len(projects),
        "runtime_images": len(RUNTIME_IMAGE_TARGETS),
        "installation_roots": len(install_manifest["components"]),
        "evidence_subjects": verification["subjects"],
        "evidence_files": verification["files"],
        "notice_components": verification["notice_components"],
        "signature_verified": verification["signature_verified"],
        "published": False,
        "validation": [
            "uv lock --offline",
            "make dist-smoke",
            "five independent PEP 751 installation locks",
            "tag-specific Python Simple index snapshot",
            "runtime image release-unit closure",
            "SHA-256 checksums",
            "SPDX 2.3 SBOMs",
            "artifact-specific attribution notice bundles",
            "SLSA provenance",
            "minisign signature",
        ],
    }


def dry_run(root: Path, version: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="riverhog-release-dry-run.") as temporary:
        scratch = Path(temporary)
        keys = scratch / "keys"
        keys.mkdir()
        public_key = keys / "release.pub"
        signing_key = keys / "release.key"
        _run(
            [
                "minisign",
                "-G",
                "-W",
                "-p",
                str(public_key),
                "-s",
                str(signing_key),
            ],
            cwd=keys,
        )
        return build_release_evidence(
            root,
            version,
            scratch / "evidence",
            signing_key=signing_key,
            public_key=public_key,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan, validate, and dry-run the coordinated Riverhog v1 release."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="Validate release inventory, metadata, and versions.")

    plan = subparsers.add_parser("plan", help="Generate SHA-bound release notes and inventory.")
    plan.add_argument("--version", required=True)
    plan.add_argument("--format", choices=("json", "markdown"), default="json")
    plan.add_argument("--allow-dirty", action="store_true")

    apply = subparsers.add_parser("apply", help="Apply one coordinated v1 version and relock.")
    apply.add_argument("--version", required=True)
    apply.add_argument("--allow-dirty", action="store_true")

    dry = subparsers.add_parser(
        "dry-run",
        help="Build and verify exact-SHA release evidence with an ephemeral key; publish nothing.",
    )
    dry.add_argument("--version", required=True)
    dry.add_argument("--summary", type=Path)

    evidence = subparsers.add_parser(
        "evidence",
        help="Build exact-SHA release evidence with an external maintainer key; publish nothing.",
    )
    evidence.add_argument("--version", required=True)
    evidence.add_argument("--output", required=True, type=Path)
    evidence.add_argument("--signing-key", required=True, type=Path)
    evidence.add_argument("--public-key", required=True, type=Path)

    verify = subparsers.add_parser(
        "verify",
        help="Verify checksums, signature, manifest, SBOMs, and provenance for release evidence.",
    )
    verify.add_argument("--directory", required=True, type=Path)
    verify.add_argument("--public-key", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "check":
            projects = validate_release_contract(ROOT)
            payload: dict[str, Any] = {
                "schema": RELEASE_SCHEMA,
                "version": projects[0].version,
                "python_distributions": len(projects),
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
        elif args.command == "plan":
            payload = build_release_plan(
                ROOT,
                args.version,
                allow_dirty=args.allow_dirty,
            )
            if args.format == "markdown":
                print(render_release_markdown(payload), end="")
            else:
                print(json.dumps(payload, indent=2, sort_keys=True))
        elif args.command == "apply":
            apply_command(ROOT, args.version, allow_dirty=args.allow_dirty)
        elif args.command == "dry-run":
            payload = dry_run(ROOT, args.version)
            rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
            if args.summary is not None:
                args.summary.parent.mkdir(parents=True, exist_ok=True)
                args.summary.write_text(rendered, encoding="utf-8")
            print(rendered, end="")
        elif args.command == "evidence":
            payload = build_release_evidence(
                ROOT,
                args.version,
                args.output.resolve(),
                signing_key=args.signing_key.resolve(),
                public_key=args.public_key.resolve(),
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            payload = verify_release_evidence(
                ROOT,
                args.directory.resolve(),
                public_key=args.public_key.resolve(),
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
    except (OSError, ReleaseError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"release error: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
