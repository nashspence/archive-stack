from __future__ import annotations

import ast
from pathlib import Path

FEATURES_DIR = Path(__file__).resolve().parents[1] / "acceptance" / "features"
PROD_HARNESS = FEATURES_DIR.parents[1] / "harness" / "test_prod_harness.py"
SPEC_HARNESS = FEATURES_DIR.parents[1] / "harness" / "test_spec_harness.py"


def _feature_names_on_disk() -> set[str]:
    return {path.name for path in FEATURES_DIR.glob("*.feature")}


def _scenario_feature_names(test_module: Path) -> set[str]:
    tree = ast.parse(test_module.read_text(encoding="utf-8"), filename=str(test_module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "scenarios":
            continue
        for arg in node.args:
            if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                continue
            names.add(Path(arg.value).name)
    return names


def test_prod_harness_loads_every_feature_file() -> None:
    assert _scenario_feature_names(PROD_HARNESS) == _feature_names_on_disk()


def test_spec_harness_loads_every_feature_file() -> None:
    assert _scenario_feature_names(SPEC_HARNESS) == _feature_names_on_disk()


def test_optical_acceptance_features_do_not_expose_fixture_or_device_internals() -> None:
    optical_features = [
        FEATURES_DIR / "cli.arc_disc.feature",
        FEATURES_DIR / "cli.arc_disc_burn.feature",
        FEATURES_DIR / "cli.arc_disc_recover.feature",
    ]
    forbidden_terms = ("fake", "fixture", "/dev/arc-optical0", "--device")
    violations: list[str] = []
    for path in optical_features:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            lowered = line.lower()
            for term in forbidden_terms:
                if term in lowered:
                    violations.append(f"{path.name}:{line_number}: {term}")
    assert not violations, "Optical acceptance surface exposes internals: " + ", ".join(
        violations
    )
