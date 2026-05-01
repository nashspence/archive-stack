from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\\\s]+)(?:\s*\\)?$")


@dataclass(frozen=True, slots=True)
class LockedPackage:
    name: str
    version: str
    hashes: tuple[str, ...]


def _normalize_package_name(name: str) -> str:
    return name.replace("_", "-").lower()


def _parse_lockfile(path: Path) -> dict[str, LockedPackage]:
    packages: dict[str, LockedPackage] = {}
    current_name: str | None = None
    current_version: str | None = None
    current_hashes: list[str] = []

    def flush_current() -> None:
        nonlocal current_name, current_version, current_hashes
        if current_name is None or current_version is None:
            return
        packages[current_name] = LockedPackage(
            name=current_name,
            version=current_version,
            hashes=tuple(current_hashes),
        )
        current_name = None
        current_version = None
        current_hashes = []

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        package_match = PACKAGE_RE.match(line)
        if package_match:
            flush_current()
            current_name = _normalize_package_name(package_match.group(1))
            current_version = package_match.group(2)
            continue
        if line.startswith("--hash=sha256:"):
            current_hashes.append(line.removesuffix(" \\"))

    flush_current()
    return packages


def test_runtime_and_test_lockfiles_do_not_drift_for_shared_packages() -> None:
    runtime = _parse_lockfile(REPO_ROOT / "requirements-runtime.txt")
    test = _parse_lockfile(REPO_ROOT / "requirements-test.txt")

    missing_from_test = sorted(set(runtime) - set(test))
    assert not missing_from_test, (
        "Runtime lock packages are missing from requirements-test.txt: "
        f"{', '.join(missing_from_test)}. Regenerate both lockfiles together."
    )

    drift = [
        f"{name}: runtime={runtime[name].version}, test={test[name].version}"
        for name in sorted(set(runtime) & set(test))
        if runtime[name].version != test[name].version
    ]
    assert not drift, (
        "Shared runtime/test lock packages have version drift: "
        f"{'; '.join(drift)}. Regenerate requirements-runtime.txt and "
        "requirements-test.txt together when dependency constraints change."
    )


def test_runtime_and_test_lockfiles_use_hashes_for_every_package() -> None:
    for lockfile in ("requirements-runtime.txt", "requirements-test.txt"):
        packages = _parse_lockfile(REPO_ROOT / lockfile)
        missing_hashes = [package.name for package in packages.values() if not package.hashes]
        assert not missing_hashes, (
            f"{lockfile} has packages without --hash=sha256 entries: "
            f"{', '.join(missing_hashes)}"
        )
