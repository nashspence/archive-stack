from __future__ import annotations

import hashlib
import importlib.util
import sys
import tarfile
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_SCRIPT = REPO_ROOT / "scripts/release.py"
INSTALLATION_SCRIPT = REPO_ROOT / "scripts/release_installation.py"


def _load(name: str, path: Path) -> ModuleType:
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _external_lock() -> tuple[str, dict[str, str]]:
    return (
        'lock-version = "1.0"\ncreated-by = "uv"\nrequires-python = ">=3.12"\n',
        {
            "external-fixture": (
                "[[packages]]\n"
                'name = "external-fixture"\n'
                'version = "2.0.0"\n'
                'wheels = [{ url = "https://example.invalid/'
                'external_fixture-2.0.0-py3-none-any.whl", '
                'size = 10, hashes = { sha256 = "' + "f" * 64 + '" } }]\n'
            )
        },
    )


def _wheel_records(projects: list[Any]) -> list[dict[str, object]]:
    records = []
    for project in projects:
        name = str(project.name)
        filename = f"{name.replace('-', '_')}-0.1.0-py3-none-any.whl"
        records.append(
            {
                "kind": "wheel",
                "name": f"python/{filename}",
                "sha256": hashlib.sha256(name.encode()).hexdigest(),
                "size": len(name),
                "distribution": name,
            }
        )
    return records


def test_installation_artifacts_are_derived_and_mutually_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _load("riverhog_release_for_installation", RELEASE_SCRIPT)
    installation = _load("riverhog_release_installation_test", INSTALLATION_SCRIPT)
    projects = release.validate_release_contract(REPO_ROOT)
    monkeypatch.setattr(installation, "_export_external_lock", lambda *_args: _external_lock())

    manifest, records = installation.build_installation_artifacts(
        REPO_ROOT,
        tmp_path,
        projects,
        _wheel_records(projects),
        version="0.1.0",
        source_sha="1" * 40,
        source_epoch=1234567890,
        repository="nashspence/riverhog",
        simple_index_path="artifacts/v{version}/simple/",
    )

    installation.verify_installation_artifacts(tmp_path, manifest)
    assert manifest["toolchain"] == {
        "uv": "0.11.24",
        "python": "3.12.3",
        "python_provider": "uv-managed-cpython",
    }
    assert [item["root"] for item in manifest["components"]] == list(installation.END_USER_ROOTS)
    assert {item["kind"] for item in records} == {
        "install-index",
        "install-lock",
        "install-reference",
    }
    assert len([item for item in records if item["kind"] == "install-lock"]) == 4
    listener = manifest["gogurt_listener"]
    assert listener["contract"]["operations"] == [
        "install",
        "status",
        "start",
        "stop",
        "restart",
        "uninstall",
    ]
    reference = tmp_path / listener["reference_path"]
    assert reference.is_file()
    assert "gogurt listener status --json" in reference.read_text(encoding="utf-8")
    for component in manifest["components"]:
        lock = tomllib.loads((tmp_path / component["lock"]["path"]).read_text(encoding="utf-8"))
        locked_names = {item["name"] for item in lock["packages"]}
        assert locked_names == {item["name"] for item in component["resolved_packages"]}
        assert not any(
            "workspace" in str(item) or "file://" in str(item) for item in lock["packages"]
        )
        for platform in installation.SUPPORTED_PLATFORMS:
            commands = component["commands"][platform]
            assert len(commands) == 5
            assert "3.12.3" in commands[1]
            assert "--no-build" in commands[1]
            assert "pip sync" in commands[2]
            assert "--dry-run --strict --no-build" in commands[2]

    snapshot = tmp_path / manifest["index"]["snapshot_path"]
    with tarfile.open(snapshot, mode="r:gz") as archive:
        files = {member.name for member in archive.getmembers() if member.isfile()}
    assert files == {
        "artifacts/v0.1.0/simple/index.html",
        *{
            f"artifacts/v0.1.0/simple/{name}/index.html"
            for name in manifest["index"]["first_party_projects"]
        },
    }


def test_index_snapshot_is_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = _load("riverhog_release_for_determinism", RELEASE_SCRIPT)
    installation = _load("riverhog_release_installation_determinism", INSTALLATION_SCRIPT)
    projects = release.validate_release_contract(REPO_ROOT)
    monkeypatch.setattr(installation, "_export_external_lock", lambda *_args: _external_lock())
    records = _wheel_records(projects)
    outputs = [tmp_path / "first", tmp_path / "second"]
    manifests = []
    for output in outputs:
        output.mkdir()
        manifest, _subjects = installation.build_installation_artifacts(
            REPO_ROOT,
            output,
            projects,
            records,
            version="0.1.0",
            source_sha="2" * 40,
            source_epoch=1234567890,
            repository="nashspence/riverhog",
            simple_index_path="artifacts/v{version}/simple/",
        )
        manifests.append(manifest)

    assert manifests[0] == manifests[1]
    first = outputs[0] / manifests[0]["index"]["snapshot_path"]
    second = outputs[1] / manifests[1]["index"]["snapshot_path"]
    assert first.read_bytes() == second.read_bytes()
