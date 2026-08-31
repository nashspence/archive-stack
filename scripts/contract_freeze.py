#!/usr/bin/env python3
"""Generate or verify the canonical Riverhog v1 exposed-contract projection."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import inspect
import json
import re
import sys
import tomllib
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import MISSING, asdict, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

import extent_contract
import gogurt_core
import operation_qualification
import release as release_contract
from gogurt.cli import app as gogurt_app
from gogurt_core import GOGURT_ROUTES_SCHEMA
from mango_fish.relay import MangoFishConfig
from pydantic import BaseModel
from riverhog_cli.main import app as riverhog_app
from riverhog_core.runtime_config import (
    ARCHIVE_STORE_ENVIRONMENT_SETTINGS,
    ARCHIVE_STORE_ENVIRONMENT_TEMPLATE,
)
from riverhog_ftp_adapter.app import build_parser as ftp_adapter_parser
from riverhog_ftp_adapter.config import FtpAdapterConfig
from riverhog_recover.cli import _parser as recovery_parser
from riverhog_storage_adapter_support import storage_adapter_schema_bundle
from stove0_cli.main import app as stove0_app
from stove0_observer_support import observer_schema_bundle
from stove0_recipe_config import RecipeCatalog
from stove0_review_sampler_support import sampler_schema_bundle
from stove0_review_target_support.app import ReviewTargetConfig, SamplerConfig
from stove0_target_support import target_schema_bundle
from typer.main import get_command

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "qualification/contracts/riverhog-v1.json"
TRACE_OUTPUT = ROOT / "qualification/contracts/riverhog-v1-trace.json"
SCHEMA = "riverhog-contract-freeze/v1"
TRACE_SCHEMA = "riverhog-contract-trace/v1"
ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]+$")
CONFIGURATION_ENVIRONMENT_NAME = re.compile(
    r"^(?:GOGURT|MANGO|RIVERHOG|STOVE0|VCRUNCH)_[A-Z0-9_]+$"
)
PROCESS_SCHEMA_BUNDLES: dict[str, Callable[[], dict[str, Any]]] = {
    "riverhog-storage-adapter": storage_adapter_schema_bundle,
    "stove0-observer": observer_schema_bundle,
    "stove0-review-sampler": sampler_schema_bundle,
    "stove0-target": target_schema_bundle,
}


class ContractFreezeError(RuntimeError):
    """The generated contract projection differs from its authority."""


def _json_value(value: object) -> object | None:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            return None
        converted = {str(key): _json_value(item) for key, item in value.items()}
        return converted if all(item is not None for item in converted.values()) else None
    if isinstance(value, (list, tuple)):
        sequence_values = [_json_value(item) for item in value]
        return sequence_values if all(item is not None for item in sequence_values) else None
    if isinstance(value, (set, frozenset)):
        set_values = [_json_value(item) for item in value]
        if any(item is None for item in set_values):
            return None
        return sorted(set_values, key=lambda item: json.dumps(item, sort_keys=True))
    return None


def _signature(value: object) -> str:
    try:
        return str(inspect.signature(cast(Callable[..., Any], value), eval_str=False))
    except (TypeError, ValueError):
        return "unavailable"


def _stable_repr(value: object) -> str:
    return re.sub(r" at 0x[0-9a-fA-F]+", "", repr(value))


def _class_surface(value: type[object]) -> dict[str, object]:
    surface: dict[str, object] = {
        "kind": "class",
        "signature": _signature(value),
    }
    if issubclass(value, Enum):
        surface["members"] = {
            name: _json_value(member.value) for name, member in value.__members__.items()
        }
    if issubclass(value, BaseModel):
        schema = value.model_json_schema(mode="validation")
        surface["schema_sha256"] = hashlib.sha256(
            json.dumps(schema, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
    if is_dataclass(value):
        surface["fields"] = [
            {
                "name": field.name,
                "type": _stable_repr(field.type),
                "default": (
                    _stable_repr(field.default)
                    if field.default is not MISSING
                    else "factory"
                    if field.default_factory is not MISSING
                    else "required"
                ),
            }
            for field in fields(value)
        ]
    members: dict[str, dict[str, str]] = {}
    for name, member in value.__dict__.items():
        if name.startswith("_"):
            continue
        candidate: object = member
        kind = "method"
        if isinstance(member, classmethod):
            candidate = member.__func__
            kind = "classmethod"
        elif isinstance(member, staticmethod):
            candidate = member.__func__
            kind = "staticmethod"
        elif isinstance(member, property):
            candidate = member.fget
            kind = "property"
        if callable(candidate):
            members[name] = {"kind": kind, "signature": _signature(candidate)}
    if members:
        surface["members"] = members
    return surface


def _python_export(value: object) -> dict[str, object]:
    if inspect.isclass(value):
        return _class_surface(value)
    if inspect.isfunction(value) or inspect.isbuiltin(value):
        return {"kind": "function", "signature": _signature(value)}
    if type(value).__name__ == "TypeAliasType":
        return {
            "kind": "type-alias",
            "value": _stable_repr(getattr(value, "__value__", value)),
        }
    serializable = _json_value(value)
    if serializable is not None:
        return {"kind": "constant", "value": serializable}
    return {
        "kind": "object",
        "type": f"{type(value).__module__}.{type(value).__qualname__}",
    }


def _python_surfaces(
    projects: list[release_contract.Project],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for project in projects:
        if project.role != "reusable_library":
            continue
        pyproject = ROOT / project.path / "pyproject.toml"
        package = release_contract._public_python_package(pyproject)
        module = importlib.import_module(package)
        exports = getattr(module, "__all__", None)
        if (
            not isinstance(exports, list)
            or not exports
            or any(
                not isinstance(name, str) or not name or name.startswith("_") for name in exports
            )
            or len(exports) != len(set(exports))
        ):
            raise ContractFreezeError(f"invalid public __all__ for {project.name}")
        missing = sorted(name for name in exports if not hasattr(module, name))
        if missing:
            raise ContractFreezeError(f"missing public exports for {project.name}: {missing}")
        result.append(
            {
                "distribution": project.name,
                "module": package,
                "exports": {
                    name: _python_export(getattr(module, name)) for name in sorted(exports)
                },
            }
        )
    return result


def _project_config(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _component_boundaries(
    projects: list[release_contract.Project],
) -> list[dict[str, object]]:
    names = {project.name for project in projects}
    components: list[dict[str, object]] = []
    for project in projects:
        config = _project_config(ROOT / project.path / "pyproject.toml")
        metadata = config["project"]
        dependencies = sorted(
            release_contract._dependency_name(str(item))
            for item in metadata.get("dependencies", [])
            if release_contract._dependency_name(str(item)) in names
        )
        optional_dependencies = {
            extra: sorted(
                release_contract._dependency_name(str(item))
                for item in values
                if release_contract._dependency_name(str(item)) in names
            )
            for extra, values in sorted(metadata.get("optional-dependencies", {}).items())
        }
        scripts = {name: value for name, value in sorted(metadata.get("scripts", {}).items())}
        components.append(
            {
                "distribution": project.name,
                "path": project.path,
                "role": project.role,
                "dependencies": dependencies,
                "optional_dependencies": optional_dependencies,
                "console_scripts": scripts,
            }
        )
    return components


def _extension_points(
    projects: list[release_contract.Project],
) -> list[dict[str, object]]:
    roles = {project.name: project.role for project in projects}
    paths = {project.name: project.path for project in projects}
    owners: dict[str, tuple[str, str]] = {}
    for surface in _python_surfaces(projects):
        module = importlib.import_module(str(surface["module"]))
        for name in module.__all__:
            value = getattr(module, name)
            if name.endswith("_ENTRY_POINT_GROUP") and isinstance(value, str):
                existing = owners.get(value)
                owner_binding = (str(surface["distribution"]), name)
                if existing is not None and existing != owner_binding:
                    raise ContractFreezeError(
                        f"entry-point group has multiple public owners: {value}"
                    )
                owners[value] = owner_binding

    providers: dict[str, list[dict[str, str]]] = defaultdict(list)
    for project in projects:
        metadata = _project_config(ROOT / project.path / "pyproject.toml")["project"]
        for group, entries in metadata.get("entry-points", {}).items():
            for name, value in entries.items():
                providers[group].append(
                    {
                        "distribution": project.name,
                        "name": name,
                        "value": value,
                    }
                )
    unknown = sorted(set(providers) - set(owners))
    if unknown:
        raise ContractFreezeError(f"extension groups lack a public owner: {unknown}")
    points: list[dict[str, object]] = []
    for group, (owner_distribution, constant) in sorted(owners.items()):
        if roles[owner_distribution] != "reusable_library":
            raise ContractFreezeError(f"extension owner is not reusable: {owner_distribution}")
        group_providers = sorted(
            providers.get(group, []),
            key=lambda item: (item["distribution"], item["name"]),
        )
        invalid = sorted(
            provider["distribution"]
            for provider in group_providers
            if roles[provider["distribution"]] != "reference_component"
        )
        if invalid:
            raise ContractFreezeError(
                f"checked-in extension providers are outside the reference role: {invalid}"
            )
        points.append(
            {
                "group": group,
                "owner": owner_distribution,
                "owner_path": paths[owner_distribution],
                "owner_constant": constant,
                "providers": group_providers,
            }
        )
    return points


def _process_extensions(
    projects: list[release_contract.Project],
) -> list[dict[str, object]]:
    roles = {project.name: project.role for project in projects}
    internal, _direct, _licenses = release_contract._project_dependency_graph(ROOT, projects)
    distributions_by_module: dict[str, str] = {}
    for project in projects:
        config = _project_config(ROOT / project.path / "pyproject.toml")
        packages = (
            config.get("tool", {})
            .get("hatch", {})
            .get("build", {})
            .get("targets", {})
            .get("wheel", {})
            .get("packages", [])
        )
        for package in packages:
            module = Path(str(package)).name
            existing = distributions_by_module.get(module)
            if existing is not None and existing != project.name:
                raise ContractFreezeError(f"Python package has multiple owners: {module}")
            distributions_by_module[module] = project.name

    images_by_distribution: dict[str, set[str]] = defaultdict(set)
    images = _project_config(ROOT / "release.toml")["images"]["runtime"]
    for image, config in images.items():
        for distribution in config["distributions"]:
            images_by_distribution[distribution].add(image)

    result: list[dict[str, object]] = []
    for name, factory in sorted(PROCESS_SCHEMA_BUNDLES.items()):
        module = factory.__module__.split(".", 1)[0]
        binding_support = distributions_by_module.get(module)
        if binding_support is None:
            raise ContractFreezeError(f"process protocol has no distribution owner: {name}")
        contract_owner = f"{binding_support.removesuffix('-support')}-protocol"
        if contract_owner not in internal[binding_support]:
            raise ContractFreezeError(f"process protocol has ambiguous contract ownership: {name}")
        bundle = factory()
        protocols = bundle.get("protocols")
        if not isinstance(protocols, list):
            protocol = bundle.get("protocol")
            protocols = [protocol] if isinstance(protocol, str) else []
        providers = [
            {
                "distribution": distribution,
                "images": sorted(distribution_images),
            }
            for distribution, distribution_images in sorted(images_by_distribution.items())
            if roles[distribution] == "reference_component"
            and binding_support in release_contract._dependency_closure(internal, distribution)
        ]
        if not protocols or not providers:
            raise ContractFreezeError(f"process extension is incomplete: {name}")
        result.append(
            {
                "name": name,
                "binding": "http",
                "contract_owner": contract_owner,
                "contract_owner_role": roles[contract_owner],
                "binding_support": binding_support,
                "binding_support_role": roles[binding_support],
                "schema_bundle_format": bundle["format"],
                "protocols": protocols,
                "providers": providers,
            }
        )
    return result


def _click_type(parameter: Any) -> dict[str, object]:
    type_ = parameter.type
    result: dict[str, object] = {
        "class": f"{type(type_).__module__}.{type(type_).__qualname__}",
        "name": getattr(type_, "name", None),
    }
    choices = getattr(type_, "choices", None)
    if choices is not None:
        result["choices"] = list(choices)
    minimum = _json_value(getattr(type_, "min", None))
    maximum = _json_value(getattr(type_, "max", None))
    if minimum is not None:
        result["minimum"] = minimum
    if maximum is not None:
        result["maximum"] = maximum
    return result


def _click_parameter(parameter: Any) -> dict[str, object]:
    result: dict[str, object] = {
        "kind": type(parameter).__name__,
        "name": parameter.name,
        "required": parameter.required,
        "nargs": parameter.nargs,
        "multiple": parameter.multiple,
        "type": _click_type(parameter),
    }
    if hasattr(parameter, "opts"):
        result["options"] = list(parameter.opts)
    if hasattr(parameter, "secondary_opts"):
        result["secondary_options"] = list(parameter.secondary_opts)
    for attribute in ("is_flag", "count", "envvar"):
        if hasattr(parameter, attribute):
            result[attribute] = _json_value(getattr(parameter, attribute))
    default = _json_value(parameter.default)
    if default is not None:
        result["default"] = default
    return result


def _click_command(command: Any) -> dict[str, object]:
    result: dict[str, object] = {
        "name": command.name,
        "parameters": [_click_parameter(parameter) for parameter in command.params],
    }
    commands = getattr(command, "commands", None)
    if isinstance(commands, Mapping):
        result["commands"] = {
            name: _click_command(child) for name, child in sorted(commands.items())
        }
    return result


def _argparse_action(action: argparse.Action) -> dict[str, object]:
    result: dict[str, object] = {
        "kind": type(action).__name__,
        "dest": action.dest,
        "options": list(action.option_strings),
        "required": action.required,
        "nargs": action.nargs,
    }
    if action.choices is not None:
        result["choices"] = list(action.choices)
    if action.type is not None:
        result["type"] = getattr(action.type, "__qualname__", repr(action.type))
    default = _json_value(action.default)
    if default is not None and action.default is not argparse.SUPPRESS:
        result["default"] = default
    return result


def _argparse_command(parser: argparse.ArgumentParser) -> dict[str, object]:
    actions = [
        action
        for action in parser._actions
        if not isinstance(action, argparse._HelpAction)
        and not isinstance(action, argparse._SubParsersAction)
    ]
    subparsers = next(
        (action for action in parser._actions if isinstance(action, argparse._SubParsersAction)),
        None,
    )
    result: dict[str, object] = {
        "name": parser.prog,
        "parameters": [_argparse_action(action) for action in actions],
    }
    if subparsers is not None:
        result["commands"] = {
            name: _argparse_command(child) for name, child in sorted(subparsers.choices.items())
        }
    return result


def _cli_surfaces() -> dict[str, object]:
    return {
        "gogurt": _click_command(get_command(gogurt_app)),
        "riverhog": _click_command(get_command(riverhog_app)),
        "riverhog-ftp-adapter": _argparse_command(ftp_adapter_parser()),
        "riverhog-recover": _argparse_command(recovery_parser()),
        "stove0": _click_command(get_command(stove0_app)),
    }


def _environment_inventory(
    projects: list[release_contract.Project],
) -> list[dict[str, object]]:
    bindings: dict[str, set[tuple[str, str, str]]] = defaultdict(set)

    def literal_name(node: ast.AST, constants: Mapping[str, str]) -> str | None:
        value: object = (
            node.value
            if isinstance(node, ast.Constant)
            else constants.get(node.id)
            if isinstance(node, ast.Name)
            else None
        )
        return (
            value
            if isinstance(value, str) and CONFIGURATION_ENVIRONMENT_NAME.fullmatch(value)
            else None
        )

    for project in projects:
        source_root = ROOT / project.path / "src"
        for source in sorted(source_root.glob("**/*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            constants = {
                target.id: node.value.value
                for node in tree.body
                if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                for target in node.targets
                if isinstance(target, ast.Name) and ENVIRONMENT_NAME.fullmatch(node.value.value)
            }
            for node in ast.walk(tree):
                candidates: tuple[ast.AST, ...] = ()
                if isinstance(node, ast.Call):
                    candidates = (*node.args, *(keyword.value for keyword in node.keywords))
                elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
                    candidates = (node.slice,)
                for candidate in candidates:
                    name = literal_name(candidate, constants)
                    if name is None:
                        continue
                    bindings[name].add(
                        (
                            project.name,
                            source.relative_to(ROOT).as_posix(),
                            ast.unparse(node),
                        )
                    )
    return [
        {
            "name": name,
            "consumers": sorted({consumer for consumer, _, _ in values}),
            "bindings": [
                {"consumer": consumer, "path": path, "expression": expression}
                for consumer, path, expression in sorted(values)
            ],
        }
        for name, values in sorted(bindings.items())
    ]


def _environment_names(
    projects: list[release_contract.Project],
) -> list[dict[str, object]]:
    return [
        {"name": item["name"], "consumers": item["consumers"]}
        for item in _environment_inventory(projects)
    ]


def _configuration_documents() -> dict[str, object]:
    documents = {
        "gogurt-routes": GOGURT_ROUTES_SCHEMA,
        "mango-fish": MangoFishConfig.model_json_schema(mode="validation"),
        "riverhog-ftp-adapter": FtpAdapterConfig.model_json_schema(mode="validation"),
        "stove0-recipes": RecipeCatalog.model_json_schema(mode="validation"),
        "stove0-review-target": ReviewTargetConfig.model_json_schema(mode="validation"),
        "stove0-review-target-sampler": SamplerConfig.model_json_schema(mode="validation"),
    }
    return dict(sorted(documents.items()))


def _configuration_environment_patterns() -> list[dict[str, object]]:
    return [
        {
            "consumer": "riverhog-server",
            "template": ARCHIVE_STORE_ENVIRONMENT_TEMPLATE,
            "parameters": {
                "store": {
                    "source": "RIVERHOG_ARCHIVE_STORES",
                    "normalization": "uppercase-dashes-to-underscores",
                },
                "setting": list(ARCHIVE_STORE_ENVIRONMENT_SETTINGS),
            },
        }
    ]


def _schema_documents() -> dict[str, object]:
    documents: dict[str, object] = {}
    for path in sorted(ROOT.glob("**/*.schema.json")):
        if ".venv" in path.parts or ".git" in path.parts:
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        authority = document.get("$id")
        if not isinstance(authority, str) or not authority:
            raise ContractFreezeError(f"protocol schema has no stable $id: {path}")
        if authority in documents:
            raise ContractFreezeError(f"protocol schema $id is not unique: {authority}")
        documents[authority] = document
    bundles = {name: factory() for name, factory in PROCESS_SCHEMA_BUNDLES.items()}
    documents.update({f"generated:{name}": document for name, document in sorted(bundles.items())})
    return dict(sorted(documents.items()))


def _state_contract(config: dict[str, Any]) -> dict[str, object]:
    owners: list[dict[str, object]] = []
    for owner in config["state"]["owners"]:
        current = dict(owner)
        current["fixture_sha256s"] = sorted(
            hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in current.pop("fixtures")
        )
        owners.append(current)
    return {"schema": config["state"]["schema"], "owners": owners}


def _openapi_surfaces() -> dict[str, object]:
    return {
        surface.name: surface.app.openapi()
        for surface in operation_qualification.application_surfaces()
    }


def _source_ref(value: object) -> dict[str, str]:
    module: str
    candidate: object
    if inspect.ismodule(value):
        module = str(value.__name__)
        symbol = "<module>"
        candidate = value
    else:
        module = str(getattr(value, "__module__", type(value).__module__))
        symbol = str(getattr(value, "__qualname__", type(value).__qualname__))
        candidate = value if callable(value) else type(value)
    try:
        source = inspect.getsourcefile(candidate)
    except TypeError:
        source = None
    result = {"module": module, "symbol": symbol}
    if source is not None:
        path = Path(source).resolve()
        if path.is_relative_to(ROOT):
            result["path"] = path.relative_to(ROOT).as_posix()
    return result


def _openapi_trace() -> list[dict[str, object]]:
    traced: list[dict[str, object]] = []
    for surface in operation_qualification.application_surfaces():
        routes: list[dict[str, object]] = []
        for route, path in operation_qualification._application_routes(surface.app):
            operation_id = getattr(route, "operation_id", None) or getattr(route, "name", None)
            endpoint = getattr(route, "endpoint", None)
            if not operation_id or endpoint is None:
                continue
            routes.append(
                {
                    "operation_id": operation_id,
                    "path": path,
                    "methods": sorted(getattr(route, "methods", ()) or ()),
                    "source": _source_ref(endpoint),
                }
            )
        traced.append(
            {
                "id": f"openapi:{surface.name}",
                "source": _source_ref(type(surface.app)),
                "routes": sorted(
                    routes,
                    key=lambda item: (str(item["path"]), str(item["operation_id"])),
                ),
            }
        )
    return traced


def _configuration_trace() -> list[dict[str, object]]:
    authorities = {
        "gogurt-routes": gogurt_core,
        "mango-fish": MangoFishConfig,
        "riverhog-ftp-adapter": FtpAdapterConfig,
        "stove0-recipes": RecipeCatalog,
        "stove0-review-target": ReviewTargetConfig,
        "stove0-review-target-sampler": SamplerConfig,
    }
    return [
        {"id": f"configuration:{name}", "source": _source_ref(value)}
        for name, value in sorted(authorities.items())
    ]


def _protocol_trace() -> list[dict[str, object]]:
    traced: list[dict[str, object]] = []
    for path in sorted(ROOT.glob("**/*.schema.json")):
        if ".venv" in path.parts or ".git" in path.parts:
            continue
        relative = path.relative_to(ROOT).as_posix()
        document = json.loads(path.read_text(encoding="utf-8"))
        authority = document.get("$id")
        if not isinstance(authority, str) or not authority:
            raise ContractFreezeError(f"protocol schema has no stable $id: {path}")
        traced.append(
            {
                "id": f"protocol:{authority}",
                "source": {"path": relative},
            }
        )
    traced.extend(
        {
            "id": f"protocol:generated:{name}",
            "source": _source_ref(factory),
        }
        for name, factory in sorted(PROCESS_SCHEMA_BUNDLES.items())
    )
    return traced


def _cli_trace() -> list[dict[str, object]]:
    modules = {
        "gogurt": "gogurt.cli",
        "riverhog": "riverhog_cli.main",
        "riverhog-ftp-adapter": "riverhog_ftp_adapter.app",
        "riverhog-recover": "riverhog_recover.cli",
        "stove0": "stove0_cli.main",
    }
    return [
        {
            "id": f"cli:{name}",
            "source": _source_ref(importlib.import_module(module)),
        }
        for name, module in sorted(modules.items())
    ]


def _python_trace(projection: Mapping[str, object]) -> list[dict[str, object]]:
    external = cast(Mapping[str, object], projection["external_contract"])
    surfaces = cast(list[dict[str, object]], external["python"])
    return [
        {
            "id": f"python:{surface['distribution']}",
            "source": _source_ref(importlib.import_module(str(surface["module"]))),
        }
        for surface in surfaces
    ]


def _state_trace() -> list[dict[str, object]]:
    config = _project_config(ROOT / "release.toml")
    return [
        {
            "id": f"state:{owner['id']}",
            "fixtures": [
                {
                    "path": path,
                    "sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest(),
                }
                for path in owner["fixtures"]
            ],
        }
        for owner in config["state"]["owners"]
    ]


def _environment_trace() -> list[dict[str, object]]:
    projects = release_contract.validate_release_contract(ROOT)
    return [
        {
            "id": f"configuration-environment:{item['name']}",
            "bindings": item["bindings"],
        }
        for item in _environment_inventory(projects)
    ]


def trace_projection(projection: Mapping[str, object]) -> dict[str, object]:
    external = cast(Mapping[str, object], projection["external_contract"])
    extents = cast(Mapping[str, object], external["extents"])
    decisions = cast(list[dict[str, object]], extents["decisions"])
    semantic_payload = json.dumps(projection, separators=(",", ":"), sort_keys=True).encode()
    rendered_payload = (json.dumps(projection, indent=2, sort_keys=True) + "\n").encode()
    sources: list[dict[str, object]] = [
        {"id": "release:release.toml", "source": {"path": "release.toml"}},
        *_openapi_trace(),
        *_cli_trace(),
        *_configuration_trace(),
        *_environment_trace(),
        *_protocol_trace(),
        *_python_trace(projection),
        *_state_trace(),
    ]
    source_ids = [str(source["id"]) for source in sources]
    if len(source_ids) != len(set(source_ids)):
        raise ContractFreezeError("contract trace source identities are not unique")
    source_kinds = dict(
        sorted(Counter(identity.split(":", 1)[0] for identity in source_ids).items())
    )
    return {
        "schema": TRACE_SCHEMA,
        "contract_schema": projection["schema"],
        "contract_canonical_sha256": hashlib.sha256(semantic_payload).hexdigest(),
        "contract_projection_sha256": hashlib.sha256(rendered_payload).hexdigest(),
        "sources": sources,
        "extent_sources": [
            {
                "id": decision["id"],
                "owner": decision["owner"],
                "source_pointer": decision["source_pointer"],
            }
            for decision in decisions
        ],
        "coverage": {
            "source_authorities": len(sources),
            "source_kinds": source_kinds,
            "extent_decisions": len(decisions),
            "extent_source_links": len(decisions),
        },
    }


def contract_projection() -> dict[str, object]:
    projects = release_contract.validate_release_contract(ROOT)
    config = _project_config(ROOT / "release.toml")
    components = _component_boundaries(projects)
    python_surfaces = _python_surfaces(projects)
    external_contract: dict[str, object] = {
        "release": {
            "installation": config["installation"],
            "artifacts": config["artifacts"],
            "compatibility": config["compatibility"],
            "platforms": config["platforms"],
            "tag_template": config["tag_template"],
            "version_policy": config["version_policy"],
        },
        "http_openapi": _openapi_surfaces(),
        "operations": [
            asdict(operation) for operation in operation_qualification.operation_matrix()
        ],
        "cli": _cli_surfaces(),
        "configuration_environment": _environment_names(projects),
        "configuration_environment_patterns": _configuration_environment_patterns(),
        "configuration_documents": _configuration_documents(),
        "protocol_schemas": _schema_documents(),
        "python": python_surfaces,
        "durable_state": _state_contract(config),
    }
    external_contract["extents"] = extent_contract.extent_projection(external_contract)
    return {
        "schema": SCHEMA,
        "series": "v1",
        "boundaries": {
            "reference_policy": config["references"]["policy"],
            "components": components,
            "runtime_images": config["images"],
            "entry_point_extensions": _extension_points(projects),
            "process_extensions": _process_extensions(projects),
        },
        "external_contract": external_contract,
    }


def _render() -> str:
    return json.dumps(contract_projection(), indent=2, sort_keys=True) + "\n"


def _render_trace(projection: Mapping[str, object]) -> str:
    return json.dumps(trace_projection(projection), indent=2, sort_keys=True) + "\n"


def _extent_diff(
    previous: Mapping[str, object] | None,
    current: Mapping[str, object],
) -> dict[str, dict[str, int]]:
    """Summarize the semantic extent diff by its owning contract boundary."""

    def decisions(projection: Mapping[str, object] | None) -> dict[str, dict[str, object]]:
        if projection is None:
            return {}
        try:
            values = cast(
                list[dict[str, object]],
                cast(
                    Mapping[str, object],
                    cast(Mapping[str, object], projection["external_contract"])["extents"],
                )["decisions"],
            )
        except (KeyError, TypeError):
            return {}
        return {str(value["id"]): value for value in values}

    old = decisions(previous)
    new = decisions(current)
    summary: dict[str, Counter[str]] = defaultdict(Counter)
    for identity in sorted(set(old) | set(new)):
        before = old.get(identity)
        after = new.get(identity)
        if before == after:
            continue
        value = after if after is not None else before
        if value is None:
            continue
        change = "added" if before is None else "removed" if after is None else "changed"
        summary[str(value["owner"])][change] += 1
    return {
        owner: {kind: counts.get(kind, 0) for kind in ("added", "changed", "removed")}
        for owner, counts in sorted(summary.items())
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="Verify the checked-in v1 projection.")
    subparsers.add_parser("update", help="Replace the checked-in v1 projection.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        projection = contract_projection()
        rendered = json.dumps(projection, indent=2, sort_keys=True) + "\n"
        trace_rendered = _render_trace(projection)
        extent_diff: dict[str, dict[str, int]] | None = None
        if args.command == "update":
            previous: Mapping[str, object] | None = None
            if OUTPUT.is_file():
                try:
                    loaded = json.loads(OUTPUT.read_text(encoding="utf-8"))
                    previous = loaded if isinstance(loaded, Mapping) else None
                except json.JSONDecodeError:
                    previous = None
            extent_diff = _extent_diff(previous, projection)
            OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT.write_text(rendered, encoding="utf-8")
            TRACE_OUTPUT.write_text(trace_rendered, encoding="utf-8")
        elif (
            not OUTPUT.is_file()
            or OUTPUT.read_text(encoding="utf-8") != rendered
            or not TRACE_OUTPUT.is_file()
            or TRACE_OUTPUT.read_text(encoding="utf-8") != trace_rendered
        ):
            raise ContractFreezeError(
                "the v1 contract or trace projection is stale; "
                "run `make contract-freeze-update` and review the semantic diff"
            )
        print(
            json.dumps(
                {
                    "output": OUTPUT.relative_to(ROOT).as_posix(),
                    "sha256": hashlib.sha256(rendered.encode()).hexdigest(),
                    "trace_output": TRACE_OUTPUT.relative_to(ROOT).as_posix(),
                    "trace_sha256": hashlib.sha256(trace_rendered.encode()).hexdigest(),
                    "status": "updated" if args.command == "update" else "current",
                    **({"extent_diff": extent_diff} if extent_diff is not None else {}),
                },
                sort_keys=True,
            )
        )
        return 0
    except (ContractFreezeError, release_contract.ReleaseError) as exc:
        print(f"contract freeze failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
