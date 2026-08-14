from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import fields
from pathlib import Path

from riverhog_core.collection_plan import CollectionVolumePolicy
from riverhog_core.pack_retrieval import PackRangeRetrievalPolicy
from riverhog_core.runtime_config import ArchiveStoreConfig, RetrievalCacheConfig, RuntimeConfig
from riverhog_core.throughput import ArchiveThroughputTuning, S3TransportTuning

REPO_ROOT = Path(__file__).parents[2]
JEB_SOURCE = REPO_ROOT / "companions" / "jeb" / "server" / "src"
MUNCHY_SOURCE = REPO_ROOT / "companions" / "munchy" / "server"
PRODUCTION_ROOTS = (
    REPO_ROOT / "packages",
    REPO_ROOT / "riverhog",
    REPO_ROOT / "companions",
    REPO_ROOT / "utilities",
    REPO_ROOT / "scripts",
)
_SETTING_NAME = re.compile(r"^(?:RIVERHOG|JEB|MUNCHY|GOGURT|MANGO|VCRUNCH)_[A-Z0-9_]+$")
_SETTING_CLASSIFICATIONS = (
    "credential",
    "identity",
    "runtime",
    "build-only",
    "workflow-only",
    "test-only",
)


def _test_paths() -> tuple[Path, ...]:
    return tuple(
        path
        for path in REPO_ROOT.rglob("*.py")
        if ".venv" not in path.parts
        and ("tests" in path.parts or path.name.startswith("test_"))
        and path != Path(__file__)
    )


def _trees(root: Path) -> dict[Path, ast.Module]:
    return {path: ast.parse(path.read_text()) for path in root.rglob("*.py")}


def _loaded_attributes(trees: dict[Path, ast.Module], *, excluding: set[Path]) -> set[str]:
    return {
        node.attr
        for path, tree in trees.items()
        if path not in excluding
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
    }


def _loaded_tokens(trees: dict[Path, ast.Module]) -> set[str]:
    return {
        node.attr
        for tree in trees.values()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
    } | {
        node.id
        for tree in trees.values()
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _munchy_environment_fields(trees: dict[Path, ast.Module]) -> set[str]:
    return {
        target.id
        for tree in trees.values()
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and node.value is not None
        and any(
            isinstance(current, ast.Call)
            and isinstance(current.func, ast.Attribute)
            and current.func.attr in {"get", "getenv"}
            for current in ast.walk(node.value)
        )
        for target in (node.targets if isinstance(node, ast.Assign) else (node.target,))
        if isinstance(target, ast.Name) and target.id.isupper()
    }


def _test_tokens() -> set[str]:
    return {
        token
        for path in _test_paths()
        for token in (
            node.id for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.Name)
        )
    } | {
        node.attr
        for path in _test_paths()
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute)
    }


def _test_string_literals() -> set[str]:
    return {
        node.value
        for path in _test_paths()
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _called_environment_settings() -> set[str]:
    settings: set[str] = set()
    for root in PRODUCTION_ROOTS:
        for path in root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call):
                    values = (*node.args, *(keyword.value for keyword in node.keywords))
                    settings.update(
                        value.value
                        for value in values
                        if isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                        and _SETTING_NAME.fullmatch(value.value)
                    )
                if (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.ctx, ast.Load)
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)
                    and _SETTING_NAME.fullmatch(node.slice.value)
                ):
                    settings.add(node.slice.value)
    return settings


def _classification(name: str) -> str:
    if name == "RIVERHOG_RELEASE_GHA_CACHE":
        return "build-only"
    normalized = name.casefold()
    parts = set(normalized.split("_"))
    if (
        name == "retrieval_cache"
        or parts & {"token", "secret", "password", "credential", "passphrase"}
        or normalized
        in {
            "access_key_id",
            "cloudfront_private_key_path",
            "cloudfront_public_key_id",
        }
        or normalized.endswith(("_secret_key_file", "_public_key_file"))
    ):
        return "credential"
    if normalized in {
        "archive_stores",
        "archive_write_store",
        "database_url",
        "event_source",
        "ftp_projection",
        "gpu_target",
        "backend",
        "bucket",
        "name",
        "prefix",
        "region",
        "source",
        "state_db",
        "targets",
        "url",
    } or normalized.endswith(("_path", "_dir", "_url", "_source", "_projection")):
        return "identity"
    return "runtime"


def test_jeb_configuration_model_fields_have_production_consumers() -> None:
    trees = _trees(JEB_SOURCE)
    models_path = JEB_SOURCE / "jeb_core" / "domain" / "models.py"
    parser_path = JEB_SOURCE / "jeb_core" / "runtime" / "config.py"
    config_classes = {
        "JebConfig",
        "JebIngressConfig",
        "LifecycleEventSettings",
        "ServiceSettings",
        "TargetConfig",
    }
    fields = {
        node.target.id
        for current in ast.walk(trees[models_path])
        if isinstance(current, ast.ClassDef) and current.name in config_classes
        for node in current.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }

    assert fields - _loaded_attributes(trees, excluding={models_path, parser_path}) == set()
    assert fields - _test_tokens() == set()


def test_direct_environment_settings_have_explicit_test_witnesses() -> None:
    settings = _called_environment_settings()

    assert len(settings) == 205
    assert settings - _test_string_literals() == set()
    assert Counter(
        (name.split("_", 1)[0].casefold(), _classification(name)) for name in settings
    ) == Counter(
        {
            ("riverhog", "credential"): 9,
            ("riverhog", "identity"): 6,
            ("riverhog", "runtime"): 72,
            ("riverhog", "build-only"): 1,
            ("jeb", "credential"): 4,
            ("jeb", "identity"): 12,
            ("jeb", "runtime"): 23,
            ("munchy", "credential"): 3,
            ("munchy", "identity"): 11,
            ("munchy", "runtime"): 62,
            ("mango", "runtime"): 1,
            ("vcrunch", "runtime"): 1,
        }
    )


def test_munchy_environment_backed_constants_have_runtime_consumers() -> None:
    trees = _trees(MUNCHY_SOURCE)
    assignments = _munchy_environment_fields(trees)

    assert assignments
    assert assignments - _loaded_tokens(trees) == set()
    assert assignments - _test_tokens() == set()


def test_parser_owned_settings_have_an_explicit_stable_classification() -> None:
    jeb_tree = ast.parse((JEB_SOURCE / "jeb_core" / "domain" / "models.py").read_text())
    jeb_classes = {
        "JebConfig",
        "JebIngressConfig",
        "LifecycleEventSettings",
        "ServiceSettings",
        "TargetConfig",
    }
    jeb_fields = {
        node.target.id
        for current in ast.walk(jeb_tree)
        if isinstance(current, ast.ClassDef) and current.name in jeb_classes
        for node in current.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    munchy_fields = _munchy_environment_fields(_trees(MUNCHY_SOURCE))
    components = {
        "riverhog": [
            field.name
            for model in (
                RuntimeConfig,
                ArchiveStoreConfig,
                RetrievalCacheConfig,
                CollectionVolumePolicy,
                PackRangeRetrievalPolicy,
                S3TransportTuning,
                ArchiveThroughputTuning,
            )
            for field in fields(model)
        ],
        "jeb": sorted(jeb_fields),
        "munchy": sorted(munchy_fields),
    }
    counts = {
        component: {
            classification: Counter(_classification(name) for name in names)[classification]
            for classification in _SETTING_CLASSIFICATIONS
        }
        for component, names in components.items()
    }

    assert counts == {
        "riverhog": {
            "credential": 13,
            "identity": 16,
            "runtime": 55,
            "build-only": 0,
            "workflow-only": 0,
            "test-only": 0,
        },
        "jeb": {
            "credential": 1,
            "identity": 12,
            "runtime": 19,
            "build-only": 0,
            "workflow-only": 0,
            "test-only": 0,
        },
        "munchy": {
            "credential": 3,
            "identity": 11,
            "runtime": 52,
            "build-only": 0,
            "workflow-only": 0,
            "test-only": 0,
        },
    }
