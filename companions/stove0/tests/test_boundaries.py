from __future__ import annotations

import ast
import json
from pathlib import Path

from stove0_core import WorkRecord

REPO_ROOT = Path(__file__).parents[3]
STOVE0_CORE = REPO_ROOT / "companions" / "stove0" / "server" / "src" / "stove0_core"
STOVE0_SERVER = REPO_ROOT / "companions" / "stove0" / "server" / "src"
PROTOCOL_ROOT = REPO_ROOT / "packages" / "stove0-protocol" / "src"
OBSERVER_PROTOCOL_ROOT = REPO_ROOT / "packages" / "stove0-observer-protocol" / "src"
OBSERVER_ROOT = REPO_ROOT / "packages" / "stove0-observer-support" / "src"
TARGET_PROTOCOL_ROOT = REPO_ROOT / "packages" / "stove0-target-protocol" / "src"
TARGET_ROOT = REPO_ROOT / "packages" / "stove0-target-support" / "src"
REVIEW_CONTRACT_ROOT = REPO_ROOT / "packages" / "stove0-review-contracts" / "src"
MEDIA_ARCHIVE_CONTRACT_ROOT = REPO_ROOT / "packages" / "stove0-media-archive-contracts" / "src"
CALLER_ROOTS = (
    REPO_ROOT / "packages" / "stove0-observer-client" / "src",
    REPO_ROOT / "packages" / "stove0-target-client" / "src",
    REPO_ROOT / "packages" / "stove0-review-sampler-client" / "src",
)
IMPLEMENTATION_ROOT = REPO_ROOT / "extensions" / "stove0"
MAINTAINED_TARGETS = (
    IMPLEMENTATION_ROOT / "opus-target" / "src" / "stove0_opus_target" / "target.py",
    IMPLEMENTATION_ROOT
    / "nvenc-av1-opus-target"
    / "src"
    / "stove0_nvenc_av1_opus_target"
    / "target.py",
    IMPLEMENTATION_ROOT / "review-target" / "src" / "stove0_review_target" / "target.py",
)
EXTENSION_ROOTS = (
    OBSERVER_PROTOCOL_ROOT,
    OBSERVER_ROOT,
    TARGET_PROTOCOL_ROOT,
    TARGET_ROOT,
    REVIEW_CONTRACT_ROOT,
)
FORBIDDEN_CORE_IMPORTS = {
    "PIL",
    "av",
    "cv2",
    "exiftool",
    "ffmpeg",
    "magic",
    "mimetypes",
    "mutagen",
    "numpy",
    "subprocess",
}


def _import_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_stove0_core_has_no_content_parser_probe_or_tool_dependency() -> None:
    imports = {root for path in STOVE0_CORE.rglob("*.py") for root in _import_roots(path)}
    assert not (imports & FORBIDDEN_CORE_IMPORTS)


def test_external_extension_surfaces_do_not_import_product_or_server_internals() -> None:
    forbidden = {
        "riverhog_api",
        "riverhog_core",
        "stove0_core",
    }
    offenders = [
        path
        for root in EXTENSION_ROOTS
        for path in root.rglob("*.py")
        if _import_roots(path) & forbidden
    ]
    assert offenders == []


def test_extension_surfaces_do_not_depend_on_each_other() -> None:
    assert not {
        path
        for path in (*OBSERVER_PROTOCOL_ROOT.rglob("*.py"), *OBSERVER_ROOT.rglob("*.py"))
        if _import_roots(path) & {"stove0_target_protocol", "stove0_target_support"}
    }
    assert not {
        path
        for path in (*TARGET_PROTOCOL_ROOT.rglob("*.py"), *TARGET_ROOT.rglob("*.py"))
        if _import_roots(path) & {"stove0_observer_protocol", "stove0_observer_support"}
    }


def test_semantic_contract_packs_depend_on_protocols_not_runtime_support() -> None:
    review_imports = {
        root for path in REVIEW_CONTRACT_ROOT.rglob("*.py") for root in _import_roots(path)
    }
    media_imports = {
        root for path in MEDIA_ARCHIVE_CONTRACT_ROOT.rglob("*.py") for root in _import_roots(path)
    }
    assert {"stove0_observer_protocol", "stove0_target_protocol"} <= review_imports
    assert "stove0_target_protocol" in media_imports
    for imports in (review_imports, media_imports):
        assert "stove0_observer_support" not in imports
        assert "stove0_target_support" not in imports


def test_protocol_package_is_independent_of_extensions_and_stove0_core() -> None:
    forbidden = {
        "stove0_core",
        "stove0_observer_support",
        "stove0_target_support",
        "stove0_review_contracts",
        "stove0_observer_protocol",
        "stove0_target_protocol",
    }
    imports = {root for path in PROTOCOL_ROOT.rglob("*.py") for root in _import_roots(path)}
    assert not (imports & forbidden)


def test_durable_stove0_work_schema_contains_no_bearer_material() -> None:
    schema = json.dumps(WorkRecord.model_json_schema(), sort_keys=True)
    assert "capability_token" not in schema
    assert "riverhog_base_url" not in schema


def test_stove0_core_does_not_import_maintained_review_semantics() -> None:
    imports = {root for path in STOVE0_CORE.rglob("*.py") for root in _import_roots(path)}
    assert "stove0_review_contracts" not in imports


def test_stove0_server_consumes_extension_boundaries_only_as_protocols_and_callers() -> None:
    imports = {root for path in STOVE0_SERVER.rglob("*.py") for root in _import_roots(path)}
    assert not imports & {
        "riverhog_transform_sdk",
        "stove0_media_archive_contracts",
        "stove0_observer_support",
        "stove0_review_contracts",
        "stove0_review_sampler_support",
        "stove0_target_support",
        "stove0_ffprobe_sampling_observer",
        "stove0_nvenc_av1_opus_review_sampler",
        "stove0_nvenc_av1_opus_target",
        "stove0_opus_review_sampler",
        "stove0_opus_target",
        "stove0_review_target",
    }


def test_caller_packages_do_not_pull_in_author_or_implementation_dependencies() -> None:
    imports = {
        root
        for caller in CALLER_ROOTS
        for path in caller.rglob("*.py")
        for root in _import_roots(path)
    }
    assert not imports & {
        "riverhog_transform_sdk",
        "stove0_core",
        "stove0_media_archive_contracts",
        "stove0_observer_support",
        "stove0_review_contracts",
        "stove0_review_sampler_support",
        "stove0_target_support",
    }


def test_maintained_extensions_do_not_import_product_internals() -> None:
    imports = {root for path in IMPLEMENTATION_ROOT.rglob("*.py") for root in _import_roots(path)}
    assert not imports & {"riverhog_api", "riverhog_core", "stove0_api", "stove0_core"}


def test_maintained_targets_publish_exact_dispositions_through_shared_runtime() -> None:
    for path in MAINTAINED_TARGETS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "publish_success"
        ]
        assert len(calls) == 1, path
        keyword_names = {item.arg for item in calls[0].keywords}
        assert {"artifacts", "dispositions", "execution_sha256", "operation"} <= (keyword_names), (
            path
        )
        assert "stove0_target_support" in _import_roots(path)


def test_review_contract_pack_is_not_a_stove0_product_plugin() -> None:
    forbidden = {
        "stove0_core",
        "riverhog_api",
        "riverhog_core",
    }
    imports = {root for path in REVIEW_CONTRACT_ROOT.rglob("*.py") for root in _import_roots(path)}
    assert not (imports & forbidden)
