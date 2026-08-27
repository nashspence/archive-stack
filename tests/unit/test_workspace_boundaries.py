from __future__ import annotations

import ast
import re
import shlex
import sys
import tomllib
from pathlib import Path

import yaml

from tests.workspace import workspace_pyprojects

REPO = Path(__file__).resolve().parents[2]

IMPLEMENTATION_OWNERS = {
    "riverhog-server": (REPO / "riverhog/server/src", {"riverhog_api", "riverhog_core"}),
    "riverhog-client": (REPO / "riverhog/client/src", {"riverhog_cli"}),
    "riverhog-recover": (REPO / "riverhog/recovery/src", {"riverhog_recover"}),
    "riverhog-ftp-adapter": (REPO / "reference/riverhog/ingress/ftp/src", {"riverhog_ftp_adapter"}),
    "riverhog-storage-adapter-aws": (
        REPO / "reference/riverhog/storage/aws/src",
        {"riverhog_storage_adapter_aws"},
    ),
    "riverhog-storage-adapter-backblaze": (
        REPO / "reference/riverhog/storage/backblaze/src",
        {"riverhog_storage_adapter_backblaze"},
    ),
    "riverhog-provenance-linux-observer": (
        REPO / "reference/riverhog/provenance/linux/src",
        {"riverhog_provenance_linux_observer"},
    ),
    "riverhog-provenance-macos-observer": (
        REPO / "reference/riverhog/provenance/macos/src",
        {"riverhog_provenance_macos_observer"},
    ),
    "riverhog-provenance-windows-observer": (
        REPO / "reference/riverhog/provenance/windows/src",
        {"riverhog_provenance_windows_observer"},
    ),
    "stove0-server": (
        REPO / "companions/stove0/server/src",
        {"stove0_api", "stove0_core"},
    ),
    "stove0-client": (REPO / "companions/stove0/client/src", {"stove0_cli"}),
    "stove0-exiftool-observer": (
        REPO / "reference/stove0/observers/exiftool/src",
        {"stove0_exiftool_observer"},
    ),
    "stove0-ffprobe-sampling-observer": (
        REPO / "reference/stove0/observers/ffprobe-sampling/src",
        {"stove0_ffprobe_sampling_observer"},
    ),
    "stove0-nvenc-av1-opus-target": (
        REPO / "reference/stove0/targets/nvenc-av1-opus/target/src",
        {"stove0_nvenc_av1_opus_target"},
    ),
    "stove0-nvenc-av1-opus-review-sampler": (
        REPO / "reference/stove0/targets/nvenc-av1-opus/review-sampler/src",
        {"stove0_nvenc_av1_opus_review_sampler"},
    ),
    "stove0-opus-review-sampler": (
        REPO / "reference/stove0/targets/opus/review-sampler/src",
        {"stove0_opus_review_sampler"},
    ),
    "stove0-opus-target": (
        REPO / "reference/stove0/targets/opus/target/src",
        {"stove0_opus_target"},
    ),
    "stove0-review-target": (
        REPO / "reference/stove0/targets/review/target/src",
        {"stove0_review_target"},
    ),
    "mango-fish": (REPO / "utilities/mango-fish/src", {"mango_fish"}),
    "gogurt": (REPO / "utilities/gogurt/src", {"gogurt"}),
    "gogurt-linux": (REPO / "reference/gogurt/linux/src", {"gogurt_linux"}),
    "gogurt-macos": (REPO / "reference/gogurt/macos/src", {"gogurt_macos"}),
    "gogurt-windows": (REPO / "reference/gogurt/windows/src", {"gogurt_windows"}),
}
ALL_IMPLEMENTATION_MODULES = set().union(
    *(modules for _, modules in IMPLEMENTATION_OWNERS.values())
)
CORE_ROOTS = {
    "riverhog_core": REPO / "riverhog/server/src/riverhog_core",
    "stove0_core": REPO / "companions/stove0/server/src/stove0_core",
}
RIVERHOG_COLLECTION_WORKFLOW_SURFACE = (
    REPO / "packages/riverhog-protocol/src/riverhog_protocol/collection_workflows.py",
    REPO / "packages/riverhog-api-client/src/riverhog_api_client/workflows.py",
    REPO / "riverhog/server/src/riverhog_api/routers/workflows.py",
    REPO / "riverhog/server/src/riverhog_api/schemas/workflows.py",
    REPO / "riverhog/server/src/riverhog_core/catalog_workflow_models.py",
    REPO / "riverhog/server/src/riverhog_core/services/collection_workflows.py",
    REPO / "riverhog/server/src/riverhog_core/state_migrations/versions/v1_0004.py",
    REPO / "riverhog/server/src/riverhog_core/state_migrations/versions/v1_0005.py",
    REPO / "riverhog/server/src/riverhog_core/state_migrations/versions/v1_0006.py",
    REPO / "riverhog/server/src/riverhog_core/state_migrations/versions/v1_0007.py",
)
EXTERNAL_DISTRIBUTION_MODULES = {
    "alembic": {"alembic"},
    "argon2-cffi": {"argon2"},
    "boto3": {"boto3"},
    "botocore": {"botocore"},
    "cryptography": {"cryptography"},
    "fastapi": {"fastapi"},
    "httpx": {"httpx"},
    "jsonschema": {"jsonschema"},
    "opentimestamps-client": set(),
    "psycopg": set(),
    "pydantic": {"pydantic"},
    "pyyaml": {"yaml"},
    "rfc8785": {"rfc8785"},
    "rich": {"rich"},
    "sqlalchemy": {"sqlalchemy"},
    "starlette": {"starlette"},
    "typer": {"typer"},
    "uvicorn": {"uvicorn"},
}
RUNTIME_ONLY_DEPENDENCIES = {
    "riverhog-server": {"opentimestamps-client", "psycopg"},
    "stove0-server": {"psycopg"},
}


def normalize_distribution_name(name: str) -> str:
    return name.replace("_", "-").lower()


def workspace_project_graph() -> tuple[dict[str, Path], dict[str, set[str]]]:
    projects: dict[str, Path] = {}
    declared_dependencies: dict[str, set[str]] = {}
    configs: dict[str, dict[str, object]] = {}
    for path in workspace_pyprojects(REPO):
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        name = normalize_distribution_name(str(config["project"]["name"]))
        projects[name] = path.parent
        configs[name] = config

    for name, config in configs.items():
        project = config["project"]
        assert isinstance(project, dict)
        dependencies = {
            normalize_distribution_name(re.split(r"[<>=!~;\[]", str(raw), maxsplit=1)[0])
            for raw in project.get("dependencies", [])
        }
        declared_dependencies[name] = dependencies & projects.keys()
    return projects, declared_dependencies


def declared_project_dependencies(config: dict[str, object]) -> set[str]:
    project = config["project"]
    assert isinstance(project, dict)
    return {
        normalize_distribution_name(re.split(r"[<>=!~;\[]", str(raw), maxsplit=1)[0])
        for raw in project.get("dependencies", [])
    }


def dependency_closure(root: str, graph: dict[str, set[str]]) -> set[str]:
    pending = list(graph[root])
    resolved: set[str] = set()
    while pending:
        dependency = pending.pop()
        if dependency in resolved:
            continue
        resolved.add(dependency)
        pending.extend(graph[dependency] - resolved)
    return resolved


def imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.partition(".")[0])
    return roots


def source_module_roots(source: Path) -> set[str]:
    packages = {
        path.name for path in source.iterdir() if path.is_dir() and (path / "__init__.py").is_file()
    }
    modules = {path.stem for path in source.glob("*.py") if path.name != "__init__.py"}
    return packages | modules


def python_module(path: Path, source: Path, package: str) -> str:
    relative = path.relative_to(source).with_suffix("")
    parts = relative.parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join((package, *parts))


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    return imported


def internal_module_graph(source: Path, package: str) -> dict[str, set[str]]:
    modules = {python_module(path, source, package): path for path in source.rglob("*.py")}
    graph: dict[str, set[str]] = {module: set() for module in modules}
    for module, path in modules.items():
        for imported in imported_modules(path):
            candidate = imported
            while candidate.startswith(f"{package}."):
                if candidate in modules and candidate != module:
                    graph[module].add(candidate)
                    break
                candidate = candidate.rpartition(".")[0]
    return graph


def dependency_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    visited: set[str] = set()
    active: list[str] = []

    def visit(module: str) -> list[str] | None:
        if module in active:
            start = active.index(module)
            return [*active[start:], module]
        if module in visited:
            return None
        active.append(module)
        for dependency in sorted(graph[module]):
            if cycle := visit(dependency):
                return cycle
        active.pop()
        visited.add(module)
        return None

    for module in sorted(graph):
        if cycle := visit(module):
            return cycle
    return None


def compose_interpolation_default(value: str) -> str:
    match = re.fullmatch(r"\$\{[A-Z0-9_]+:-(.+)\}", value)
    return match.group(1) if match else value


def test_implementation_projects_do_not_cross_owner_boundaries() -> None:
    violations: list[str] = []
    for owner, (source, owned_modules) in IMPLEMENTATION_OWNERS.items():
        foreign_modules = ALL_IMPLEMENTATION_MODULES - owned_modules
        for path in source.rglob("*.py"):
            crossed = sorted(imported_roots(path) & foreign_modules)
            if crossed:
                violations.append(f"{owner}: {path.relative_to(REPO)} imports {', '.join(crossed)}")
    assert not violations, "\n".join(violations)


def test_every_implementation_project_and_module_has_exactly_one_owner() -> None:
    projects: dict[str, Path] = {}
    for pyproject in workspace_pyprojects(REPO):
        if pyproject.relative_to(REPO).parts[0] == "packages":
            continue
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        projects[normalize_distribution_name(str(config["project"]["name"]))] = (
            pyproject.parent / "src"
        )

    assert set(IMPLEMENTATION_OWNERS) == set(projects)
    for owner, (source, owned_modules) in IMPLEMENTATION_OWNERS.items():
        assert source == projects[owner]
        assert owned_modules == source_module_roots(source)


def test_shared_packages_do_not_import_implementation_projects() -> None:
    violations = [
        f"{path.relative_to(REPO)} imports {', '.join(crossed)}"
        for path in (REPO / "packages").rglob("*.py")
        if (crossed := sorted(imported_roots(path) & ALL_IMPLEMENTATION_MODULES))
    ]
    assert not violations, "\n".join(violations)


def test_riverhog_collection_workflows_use_application_agnostic_outcomes() -> None:
    surface = {
        path.relative_to(REPO): path.read_text(encoding="utf-8")
        for path in RIVERHOG_COLLECTION_WORKFLOW_SURFACE
    }
    combined = "\n".join(surface.values())

    assert "CollectionProcessingOutcomeIdentity" in combined
    assert '"collection_processing_outcomes"' in combined
    assert '"/collection-processing-claims/{claim_id}/outcomes/settle"' in combined

    forbidden = ("stove0", "branch_set", "join_plan", "coordination", "dependency_id")
    violations = [
        f"{path}: {term}"
        for path, text in surface.items()
        for term in forbidden
        if term in text.lower()
    ]
    assert not violations, "\n".join(violations)


def test_riverhog_production_surfaces_are_stove0_agnostic() -> None:
    roots = (
        REPO / "riverhog/server/src",
        REPO / "riverhog/client/src",
        REPO / "riverhog/recovery/src",
        REPO / "reference/riverhog/ingress/ftp/src",
    )
    paths = [path for root in roots for path in root.rglob("*.py")]
    paths.extend(
        path
        for package in (REPO / "packages").glob("riverhog-*")
        for path in (package / "src").rglob("*.py")
    )
    violations = [
        str(path.relative_to(REPO))
        for path in paths
        if "stove0" in path.read_text(encoding="utf-8").casefold()
    ]

    assert not violations, "\n".join(violations)


def test_projects_declare_their_exact_direct_runtime_dependencies() -> None:
    configs: dict[str, tuple[Path, dict[str, object]]] = {}
    distribution_modules: dict[str, set[str]] = dict(EXTERNAL_DISTRIBUTION_MODULES)
    for pyproject in workspace_pyprojects(REPO):
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        distribution = normalize_distribution_name(str(config["project"]["name"]))
        configs[distribution] = (pyproject, config)
        distribution_modules[distribution] = source_module_roots(pyproject.parent / "src")

    known_roots = {
        root: distribution for distribution, roots in distribution_modules.items() for root in roots
    }
    violations: list[str] = []
    for distribution, (pyproject, config) in configs.items():
        dependencies = declared_project_dependencies(config)
        imported = set().union(
            *(imported_roots(path) for path in (pyproject.parent / "src").rglob("*.py"))
        )
        runtime_only = RUNTIME_ONLY_DEPENDENCIES.get(distribution, set())

        for dependency in sorted(dependencies):
            roots = distribution_modules.get(dependency)
            if roots is None:
                violations.append(f"{distribution}: unknown dependency mapping for {dependency}")
            elif dependency not in runtime_only and not (roots & imported):
                violations.append(f"{distribution}: unused direct dependency {dependency}")

        local_roots = distribution_modules[distribution]
        for root in sorted(imported - local_roots - sys.stdlib_module_names):
            required = known_roots.get(root)
            if required is not None and required not in dependencies:
                violations.append(f"{distribution}: imports {root} without declaring {required}")

    assert not violations, "\n".join(violations)


def test_portable_products_do_not_select_provider_implementations() -> None:
    gogurt = tomllib.loads((REPO / "utilities/gogurt/pyproject.toml").read_text(encoding="utf-8"))
    assert declared_project_dependencies(gogurt).isdisjoint(
        {
            "gogurt-linux",
            "gogurt-macos",
            "gogurt-path-volume-support",
            "gogurt-windows",
        }
    )
    gogurt_sources = tuple((REPO / "utilities/gogurt/src").rglob("*.py"))
    gogurt_imports = set().union(*(imported_roots(path) for path in gogurt_sources))
    assert gogurt_imports.isdisjoint(
        {
            "gogurt_linux",
            "gogurt_macos",
            "gogurt_path_volume_support",
            "gogurt_windows",
        }
    )
    assert all("sys.platform" not in path.read_text(encoding="utf-8") for path in gogurt_sources)
    client = tomllib.loads((REPO / "riverhog/client/pyproject.toml").read_text(encoding="utf-8"))
    client_dependencies = declared_project_dependencies(client)
    assert client_dependencies.isdisjoint(
        {
            "riverhog-provenance-linux-observer",
            "riverhog-provenance-macos-observer",
            "riverhog-provenance-windows-observer",
        }
    )


def test_portable_core_listener_runtime_and_platform_dependency_direction_is_exact() -> None:
    gogurt_native_roots = {"gogurt_linux", "gogurt_macos", "gogurt_windows"}
    gogurt_core_imports = set().union(
        *(imported_roots(path) for path in (REPO / "packages/gogurt-core/src").rglob("*.py"))
    )
    assert gogurt_core_imports.isdisjoint(
        gogurt_native_roots | {"gogurt_listener_runtime", "gogurt_path_volume_support"}
    )

    path_volume_config = tomllib.loads(
        (REPO / "packages/gogurt-path-volume-support/pyproject.toml").read_text(encoding="utf-8")
    )
    assert declared_project_dependencies(path_volume_config) == {
        "config-validation",
        "gogurt-core",
    }

    listener_runtime_config = tomllib.loads(
        (REPO / "packages/gogurt-listener-runtime/pyproject.toml").read_text(encoding="utf-8")
    )
    assert declared_project_dependencies(listener_runtime_config) == {
        "config-validation",
        "gogurt-core",
    }
    listener_runtime_imports = set().union(
        *(
            imported_roots(path)
            for path in (REPO / "packages/gogurt-listener-runtime/src").rglob("*.py")
        )
    )
    assert "gogurt_core" in listener_runtime_imports
    assert listener_runtime_imports.isdisjoint(
        gogurt_native_roots | {"gogurt", "gogurt_path_volume_support"}
    )

    generic_marker_sources = (
        REPO / "packages/gogurt-core/src/gogurt_core/mounts.py",
        REPO / "packages/gogurt-core/src/gogurt_core/core.py",
        REPO / "packages/gogurt-listener-runtime/src/gogurt_listener_runtime/listener.py",
        REPO / "utilities/gogurt/src/gogurt/cli.py",
        REPO / "utilities/gogurt/src/gogurt/providers.py",
    )
    for source in generic_marker_sources:
        text = source.read_text(encoding="utf-8")
        assert "marker_name" not in text
        assert '".gogurt"' not in text

    path_support = (
        REPO / "packages/gogurt-path-volume-support/src/gogurt_path_volume_support/__init__.py"
    ).read_text(encoding="utf-8")
    assert 'PATH_MARKER_NAME = ".gogurt"' in path_support
    assert 'f"{document.route}\\n".encode()' in path_support

    platform_contracts = {
        "linux": "riverhog-provenance-linux-contracts",
        "macos": "riverhog-provenance-macos-contracts",
        "windows": "riverhog-provenance-windows-contracts",
    }
    for platform, contract in platform_contracts.items():
        observer_config = tomllib.loads(
            (REPO / f"reference/riverhog/provenance/{platform}/pyproject.toml").read_text(
                encoding="utf-8"
            )
        )
        assert declared_project_dependencies(observer_config) == {
            "riverhog-provenance",
            contract,
        }
        contract_config = tomllib.loads(
            (REPO / f"packages/{contract}/pyproject.toml").read_text(encoding="utf-8")
        )
        assert declared_project_dependencies(contract_config) == {"riverhog-provenance-contracts"}

        gogurt_config = tomllib.loads(
            (REPO / f"reference/gogurt/{platform}/pyproject.toml").read_text(encoding="utf-8")
        )
        assert declared_project_dependencies(gogurt_config) == {
            "gogurt-core",
            "gogurt-listener-runtime",
            "gogurt-path-volume-support",
        }

    provenance_config = tomllib.loads(
        (REPO / "packages/riverhog-provenance/pyproject.toml").read_text(encoding="utf-8")
    )
    assert declared_project_dependencies(provenance_config).isdisjoint(
        set(platform_contracts.values())
    )
    provenance_imports = set().union(
        *(
            imported_roots(path)
            for path in (REPO / "packages/riverhog-provenance/src").rglob("*.py")
        )
    )
    assert provenance_imports.isdisjoint(
        {f"riverhog_provenance_{platform}_observer" for platform in platform_contracts}
    )


def test_core_dependency_graphs_are_acyclic() -> None:
    for package, source in CORE_ROOTS.items():
        cycle = dependency_cycle(internal_module_graph(source, package))
        assert cycle is None, " -> ".join(cycle or ())


def test_core_domain_and_ports_are_dependency_roots() -> None:
    for package, source in CORE_ROOTS.items():
        for path in (source / "domain").rglob("*.py"):
            internal = {
                imported
                for imported in imported_modules(path)
                if imported == package or imported.startswith(f"{package}.")
            }
            assert all(imported.startswith(f"{package}.domain") for imported in internal)
        for path in (source / "ports").rglob("*.py"):
            internal = {
                imported
                for imported in imported_modules(path)
                if imported == package or imported.startswith(f"{package}.")
            }
            assert all(
                imported.startswith((f"{package}.domain", f"{package}.ports"))
                for imported in internal
            )


def test_images_copy_only_their_owned_implementation_project() -> None:
    dockerfiles = {
        REPO / "riverhog/server/Dockerfile": "riverhog/server",
        REPO / "reference/riverhog/ingress/ftp/Dockerfile": (
            "reference/riverhog/ingress/ftp",
            "reference/riverhog/provenance/linux",
        ),
        REPO / "reference/riverhog/storage/aws/Dockerfile": "reference/riverhog/storage/aws",
        REPO / "reference/riverhog/storage/backblaze/Dockerfile": (
            "reference/riverhog/storage/backblaze"
        ),
        REPO / "companions/stove0/server/Dockerfile": "companions/stove0/server",
        REPO / "reference/stove0/observers/exiftool/Dockerfile": (
            "reference/stove0/observers/exiftool"
        ),
        REPO / "reference/stove0/observers/ffprobe-sampling/Dockerfile": (
            "reference/stove0/observers/ffprobe-sampling"
        ),
        REPO / "reference/stove0/targets/nvenc-av1-opus/Dockerfile": (
            "reference/stove0/targets/nvenc-av1-opus/target",
            "reference/stove0/targets/nvenc-av1-opus/review-sampler",
            "reference/stove0/targets/nvenc-av1-opus/verify-ffmpeg",
        ),
        REPO / "reference/stove0/targets/opus/Dockerfile": (
            "reference/stove0/targets/opus/target",
            "reference/stove0/targets/opus/review-sampler",
        ),
        REPO / "reference/stove0/targets/review/Dockerfile": (
            "reference/stove0/targets/review/target"
        ),
        REPO / "utilities/mango-fish/Dockerfile": "utilities/mango-fish",
    }
    implementation_prefix = re.compile(r"^(?:companions|reference|riverhog|utilities)/")
    for dockerfile, expected in dockerfiles.items():
        copied = {
            source
            for source in re.findall(r"^COPY ([^\s]+)", dockerfile.read_text(), re.MULTILINE)
            if implementation_prefix.match(source)
        }
        assert copied
        allowed = (expected,) if isinstance(expected, str) else expected
        assert all(
            any(source == root or source.startswith(f"{root}/") for root in allowed)
            for source in copied
        )


def test_stove0_server_has_only_protocol_and_caller_side_extension_dependencies() -> None:
    _projects, graph = workspace_project_graph()
    closure = dependency_closure("stove0-server", graph)
    assert {"stove0-observer-client", "stove0-target-client"} <= closure
    assert not closure & {
        "riverhog-transform-sdk",
        "stove0-media-archive-target-contracts",
        "stove0-media-archive-target-support",
        "stove0-media-metadata-observer-contracts",
        "stove0-media-sampling-observer-contracts",
        "stove0-observer-support",
        "stove0-review-planning",
        "stove0-review-target-contracts",
        "stove0-review-sampler-support",
        "stove0-target-support",
    }


def test_maintained_observer_distributions_do_not_pull_target_authority() -> None:
    _projects, graph = workspace_project_graph()
    expected = {
        "stove0-exiftool-observer": "stove0-media-metadata-observer-contracts",
        "stove0-ffprobe-sampling-observer": "stove0-media-sampling-observer-contracts",
    }
    forbidden = {
        "stove0-media-archive-target-contracts",
        "stove0-media-archive-target-support",
        "stove0-review-planning",
        "stove0-review-target-contracts",
        "stove0-target-client",
        "stove0-target-protocol",
        "stove0-target-support",
    }
    for distribution, observer_contract in expected.items():
        closure = dependency_closure(distribution, graph)
        assert observer_contract in closure
        assert not closure & forbidden


def test_media_archive_target_contracts_do_not_pull_observer_authority() -> None:
    _projects, graph = workspace_project_graph()
    closure = dependency_closure("stove0-media-archive-target-contracts", graph)
    assert "stove0-media-metadata-observer-contracts" not in closure


def test_semantic_contract_distributions_do_not_pull_runtime_support() -> None:
    _projects, graph = workspace_project_graph()
    for distribution in (
        "stove0-media-metadata-observer-contracts",
        "stove0-media-sampling-observer-contracts",
        "stove0-media-archive-target-contracts",
        "stove0-review-target-contracts",
        "stove0-media-archive-target-support",
        "stove0-review-planning",
    ):
        closure = dependency_closure(distribution, graph)
        assert not closure & {
            "stove0-observer-client",
            "stove0-observer-support",
            "stove0-target-client",
            "stove0-target-support",
        }


def test_paired_target_and_review_sampler_distributions_do_not_import_each_other() -> None:
    pairs = (
        ("stove0_opus_target", "stove0_opus_review_sampler"),
        ("stove0_nvenc_av1_opus_target", "stove0_nvenc_av1_opus_review_sampler"),
    )
    for target, sampler in pairs:
        target_source = next(
            path for path, modules in IMPLEMENTATION_OWNERS.values() if target in modules
        )
        sampler_source = next(
            path for path, modules in IMPLEMENTATION_OWNERS.values() if sampler in modules
        )
        assert sampler not in {
            root for path in target_source.rglob("*.py") for root in imported_roots(path)
        }
        assert target not in {
            root for path in sampler_source.rglob("*.py") for root in imported_roots(path)
        }


def test_locally_built_compose_services_use_development_image_tags() -> None:
    built_services: list[str] = []
    for compose_file in REPO.rglob("compose.yaml"):
        compose = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
        for service_name, service in compose["services"].items():
            if "build" not in service:
                continue
            label = f"{compose_file.relative_to(REPO)}:{service_name}"
            built_services.append(label)
            image = service.get("image")
            assert isinstance(image, str), f"{label} has no explicit image"
            assert compose_interpolation_default(image).endswith(":dev"), label

    assert built_services


def test_compose_timezone_defaults_are_configurable_utc() -> None:
    configured_services: list[str] = []
    for compose_file in REPO.rglob("compose.yaml"):
        compose = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
        for service_name, service in compose["services"].items():
            environment = service.get("environment", {})
            if "TZ" not in environment:
                continue
            label = f"{compose_file.relative_to(REPO)}:{service_name}"
            configured_services.append(label)
            assert environment["TZ"] == "${TZ:-UTC}", label

    assert configured_services


def test_images_copy_their_complete_internal_dependency_closure() -> None:
    images = {
        REPO / "riverhog/server/Dockerfile": "riverhog-server",
        REPO / "reference/riverhog/ingress/ftp/Dockerfile": (
            "riverhog-ftp-adapter",
            "riverhog-provenance-linux-observer",
        ),
        REPO / "reference/riverhog/storage/aws/Dockerfile": "riverhog-storage-adapter-aws",
        REPO / "reference/riverhog/storage/backblaze/Dockerfile": (
            "riverhog-storage-adapter-backblaze"
        ),
        REPO / "companions/stove0/server/Dockerfile": (
            "stove0-server",
            "stove0-media-metadata-observer-contracts",
            "stove0-media-sampling-observer-contracts",
        ),
        REPO / "reference/stove0/observers/exiftool/Dockerfile": ("stove0-exiftool-observer"),
        REPO / "reference/stove0/observers/ffprobe-sampling/Dockerfile": (
            "stove0-ffprobe-sampling-observer"
        ),
        REPO / "reference/stove0/targets/nvenc-av1-opus/Dockerfile": (
            "stove0-nvenc-av1-opus-target",
            "stove0-nvenc-av1-opus-review-sampler",
        ),
        REPO / "reference/stove0/targets/opus/Dockerfile": (
            "stove0-opus-target",
            "stove0-opus-review-sampler",
        ),
        REPO / "reference/stove0/targets/review/Dockerfile": "stove0-review-target",
        REPO / "utilities/mango-fish/Dockerfile": "mango-fish",
    }
    projects, graph = workspace_project_graph()

    for dockerfile, distributions in images.items():
        copied_sources: set[str] = set()
        for raw_line in dockerfile.read_text(encoding="utf-8").splitlines():
            if not raw_line.startswith("COPY "):
                continue
            tokens = shlex.split(raw_line)
            if tokens[1].startswith("--from="):
                continue
            copied_sources.update(tokens[1:-1])

        roots = (distributions,) if isinstance(distributions, str) else distributions
        closure = set(roots)
        for distribution in roots:
            closure.update(dependency_closure(distribution, graph))
        expected = {str(projects[dependency].relative_to(REPO)) for dependency in closure}
        expected = {path for path in expected if path.startswith("packages/")}
        copied_packages = {source for source in copied_sources if source.startswith("packages/")}
        missing = expected - copied_packages
        extra = copied_packages - expected
        assert not missing, f"{dockerfile.relative_to(REPO)} omits {sorted(missing)}"
        assert not extra, f"{dockerfile.relative_to(REPO)} unnecessarily copies {sorted(extra)}"
