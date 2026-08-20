from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import fields
from pathlib import Path

from riverhog_adapters.config import AdapterConfig, SourceConfig
from riverhog_core.collection_plan import CollectionVolumePolicy
from riverhog_core.pack_retrieval import PackRangeRetrievalPolicy
from riverhog_core.runtime_config import ArchiveStoreConfig, RetrievalCacheConfig, RuntimeConfig
from riverhog_core.throughput import ArchiveThroughputTuning, S3TransportTuning
from stove0_core import EndpointRegistration, Stove0RuntimeConfig

REPO_ROOT = Path(__file__).parents[2]
STOVE0_SOURCE = REPO_ROOT / "companions" / "stove0" / "server" / "src"
ADAPTER_SOURCE = REPO_ROOT / "riverhog" / "adapters" / "src"
PRODUCTION_ROOTS = (
    REPO_ROOT / "packages",
    REPO_ROOT / "riverhog",
    REPO_ROOT / "companions",
    REPO_ROOT / "utilities",
    REPO_ROOT / "scripts",
)
_SETTING_NAME = re.compile(r"^(?:RIVERHOG|STOVE0|GOGURT|MANGO|VCRUNCH)_[A-Z0-9_]+$")
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
    } or normalized.endswith(
        ("_path", "_dir", "_url", "_source", "_projection", "_registry", "_revision")
    ):
        return "identity"
    return "runtime"


def test_stove0_and_adapter_configuration_fields_have_consumers_and_witnesses() -> None:
    components = {
        "stove0": (
            _trees(STOVE0_SOURCE),
            set(Stove0RuntimeConfig.__dataclass_fields__)
            | set(EndpointRegistration.__dataclass_fields__),
        ),
        "riverhog-adapters": (
            _trees(ADAPTER_SOURCE),
            set(AdapterConfig.model_fields) | set(SourceConfig.model_fields),
        ),
    }

    for component, (trees, names) in components.items():
        assert names - _loaded_attributes(trees, excluding=set()) == set(), component
        assert names - _test_tokens() == set(), component


def test_direct_environment_settings_have_explicit_test_witnesses() -> None:
    settings = _called_environment_settings()

    assert settings
    assert settings - _test_string_literals() == set()
    classifications = Counter(_classification(name) for name in settings)
    assert set(classifications) <= set(_SETTING_CLASSIFICATIONS)
    assert classifications["credential"] > 0
    assert classifications["identity"] > 0
    assert classifications["runtime"] > 0


def test_parser_owned_settings_have_an_explicit_stable_classification() -> None:
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
        "stove0": [
            *Stove0RuntimeConfig.__dataclass_fields__,
            *EndpointRegistration.__dataclass_fields__,
        ],
        "riverhog-adapters": [*AdapterConfig.model_fields, *SourceConfig.model_fields],
    }
    counts = {
        component: {
            classification: Counter(_classification(name) for name in names)[classification]
            for classification in _SETTING_CLASSIFICATIONS
        }
        for component, names in components.items()
    }

    assert set(counts) == {"riverhog", "stove0", "riverhog-adapters"}
    assert all(sum(component.values()) > 0 for component in counts.values())
    assert all(set(component) == set(_SETTING_CLASSIFICATIONS) for component in counts.values())
