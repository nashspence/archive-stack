from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/check_dependency_readiness.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_dependency_readiness", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dependency_readiness_accepts_an_exact_graph_without_alerts() -> None:
    module = load_script()
    lock = module.load_lock(REPO_ROOT / "uv.lock")
    packages = [
        {
            "externalRefs": [
                {
                    "referenceLocator": f"pkg:pypi/{name}@{version}",
                }
            ]
        }
        for name, version in module.registry_versions(lock)
    ]
    packages.extend(
        [
            {"externalRefs": [{"referenceLocator": "pkg:pypi/example-without-an-exact-version"}]},
            {"externalRefs": [{"referenceLocator": "pkg:docker/example/image@1.0.0"}]},
        ]
    )

    assert module.readiness_errors(lock, {"sbom": {"packages": packages}}, []) == []


def test_dependency_readiness_identifies_current_release_blockers() -> None:
    module = load_script()
    lock = module.load_lock(REPO_ROOT / "uv.lock")
    versions = module.registry_versions(lock)
    cryptography_version = next(version for name, version in versions if name == "cryptography")
    packages = [
        {
            "externalRefs": [
                {
                    "referenceLocator": f"pkg:pypi/{name}@{version}",
                }
            ]
        }
        for name, version in versions
        if name != "cryptography"
    ]
    packages.append({"externalRefs": [{"referenceLocator": "pkg:pypi/cryptography@46.0.7"}]})
    alerts = [{"dependency": {"package": {"name": "cryptography"}}}]

    errors = module.readiness_errors(lock, {"sbom": {"packages": packages}}, alerts)

    assert errors == [
        f"dependency graph is missing locked versions: cryptography=={cryptography_version}",
        "dependency graph contains obsolete locked versions: cryptography==46.0.7",
        "Dependabot has 1 open alerts for: cryptography",
    ]
