from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/qualify_installation.py"


def load_script() -> ModuleType:
    if str(SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("riverhog_qualify_installation", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_distribution_builds_are_serialized_into_a_clean_output(
    tmp_path: Path,
) -> None:
    module = load_script()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "stale.whl").write_bytes(b"stale")
    commands: list[list[str]] = []

    def record(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == tmp_path
        assert env is None
        assert capture is False
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    module._run = record

    module._build_distributions(
        tmp_path,
        [SimpleNamespace(name="alpha"), SimpleNamespace(name="bravo")],
    )

    assert list(dist.iterdir()) == []
    assert commands == [
        [
            "uv",
            "--no-config",
            "build",
            "--package",
            "alpha",
            "--no-create-gitignore",
        ],
        [
            "uv",
            "--no-config",
            "build",
            "--package",
            "bravo",
            "--no-create-gitignore",
        ],
    ]
