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
    "munchy-server": (
        REPO / "companions/munchy/server/src",
        {"munchy_api", "munchy_core"},
    ),
    "munchy-av1-nvenc-target": (
        REPO / "companions/munchy/server/targets/av1-nvenc/src",
        {"munchy_av1_nvenc"},
    ),
    "munchy-client": (REPO / "companions/munchy/client/src", {"munchy_cli"}),
    "jeb-server": (REPO / "companions/jeb/server/src", {"jeb_api", "jeb_core"}),
    "jeb-client": (REPO / "companions/jeb/client/src", {"jeb_cli"}),
    "mango-fish": (REPO / "utilities/mango-fish/src", {"mango_fish"}),
    "gogurt": (REPO / "utilities/gogurt/src", {"gogurt"}),
}
ALL_IMPLEMENTATION_MODULES = set().union(
    *(modules for _, modules in IMPLEMENTATION_OWNERS.values())
)
CORE_ROOTS = {
    "riverhog_core": REPO / "riverhog/server/src/riverhog_core",
    "munchy_core": REPO / "companions/munchy/server/src/munchy_core",
    "jeb_core": REPO / "companions/jeb/server/src/jeb_core",
}
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


def test_companion_platform_clients_are_owned_by_contained_adapters() -> None:
    expected = {
        ("riverhog_api_client", REPO / "companions/munchy/server/src"): {
            Path("companions/munchy/server/src/munchy_core/adapters/riverhog.py")
        },
        ("riverhog_protocol", REPO / "companions/munchy/server/src"): {
            Path("companions/munchy/server/src/munchy_core/adapters/riverhog.py")
        },
        ("munchy_api_client", REPO / "companions/jeb/server/src"): {
            Path("companions/jeb/server/src/jeb_core/adapters/munchy.py")
        },
    }
    for (imported, source), expected_paths in expected.items():
        actual = {
            path.relative_to(REPO)
            for path in source.rglob("*.py")
            if imported in imported_roots(path)
        }
        assert actual == expected_paths


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
        REPO / "companions/jeb/server/Dockerfile": "companions/jeb/server",
        REPO / "companions/munchy/server/Dockerfile": "companions/munchy/server",
        REPO / "companions/munchy/server/targets/av1-nvenc/Dockerfile": (
            "companions/munchy/server/targets/av1-nvenc"
        ),
        REPO / "utilities/mango-fish/Dockerfile": "utilities/mango-fish",
    }
    allowed_manifests = {
        REPO / "companions/munchy/server/targets/av1-nvenc/Dockerfile": {
            "companions/munchy/server/pyproject.toml"
        }
    }
    implementation_prefix = re.compile(r"^(?:companions|riverhog|utilities)/")
    for dockerfile, expected in dockerfiles.items():
        copied = {
            source
            for source in re.findall(r"^COPY ([^\s]+)", dockerfile.read_text(), re.MULTILINE)
            if implementation_prefix.match(source)
        }
        assert copied
        assert all(
            source == expected
            or source.startswith(f"{expected}/")
            or source in allowed_manifests.get(dockerfile, set())
            for source in copied
        )


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
        REPO / "companions/jeb/server/Dockerfile": "jeb-server",
        REPO / "companions/munchy/server/Dockerfile": "munchy-server",
        REPO / "companions/munchy/server/targets/av1-nvenc/Dockerfile": ("munchy-av1-nvenc-target"),
        REPO / "utilities/mango-fish/Dockerfile": "mango-fish",
    }
    projects, graph = workspace_project_graph()

    for dockerfile, distribution in images.items():
        copied_sources: set[str] = set()
        for raw_line in dockerfile.read_text(encoding="utf-8").splitlines():
            if not raw_line.startswith("COPY "):
                continue
            tokens = shlex.split(raw_line)
            if tokens[1].startswith("--from="):
                continue
            copied_sources.update(tokens[1:-1])

        expected = {
            str(projects[dependency].relative_to(REPO))
            for dependency in dependency_closure(distribution, graph)
        }
        copied_packages = {source for source in copied_sources if source.startswith("packages/")}
        missing = expected - copied_packages
        extra = copied_packages - expected
        assert not missing, f"{dockerfile.relative_to(REPO)} omits {sorted(missing)}"
        assert not extra, f"{dockerfile.relative_to(REPO)} unnecessarily copies {sorted(extra)}"
