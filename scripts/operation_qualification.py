#!/usr/bin/env python3
"""Generate and verify the current public operation qualification matrix."""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
import subprocess
import sys
import textwrap
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, TypeGuard, cast

from fastapi import FastAPI
from fastapi.routing import APIRoute
from riverhog_ftp_adapter_api_client import RiverhogFtpAdapterClient
from riverhog_ftp_adapter.app import FtpAdapterComposition
from riverhog_ftp_adapter.app import build_parser as build_adapter_parser
from riverhog_ftp_adapter.app import create_app as create_adapter_app
from riverhog_ftp_adapter.config import FtpAdapterConfig, SourceConfig
from riverhog_api.app import create_app as create_riverhog_app
from riverhog_api_client.client import ApiClient
from riverhog_cli import main as riverhog_cli
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from stove0_api.app import Stove0Composition
from stove0_api.app import create_app as create_stove0_app
from stove0_api_client import Stove0ApiClient
from stove0_cli import main as stove0_cli
from stove0_core import (
    EvaluationService,
    RecipeCatalog,
    SqlAlchemyStateStore,
    Stove0Coordinator,
    Stove0RuntimeConfig,
    Stove0Scheduler,
    Stove0WorkService,
    WorkflowPreviewService,
)

SCHEMA = "riverhog-operation-qualification/v1"
TIMING_SCHEMA = "riverhog-operation-timings/v1"
HTTP_METHODS = frozenset({"delete", "get", "patch", "post", "put"})
SUPPORTED_ROUTE_METHODS = HTTP_METHODS | {"head"}
SOURCE_SHA_PATTERN = "0123456789abcdef"


class _RiverhogContractApi:
    def close(self) -> None:
        pass

    def list_archive_stores(self, *, per_page: int) -> dict[str, object]:
        del per_page
        return {"items": []}


class _AdapterContractService:
    def status(self) -> dict[str, object]:
        return {"format": "riverhog-ftp-adapter-status/v1", "sources": []}

    def run_once(self) -> dict[str, object]:
        return {"format": "riverhog-ftp-adapter-pass/v1", "sources": []}

    def flush(self, source_id: str) -> dict[str, object]:
        return {"format": "riverhog-ftp-adapter-pass/v1", "sources": [source_id]}


def create_stove0_contract_app() -> FastAPI:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    state = SqlAlchemyStateStore("sqlite+pysqlite:///:memory:", engine=engine)
    work = Stove0WorkService(state)
    return create_stove0_app(
        Stove0Composition(
            config=Stove0RuntimeConfig(
                database_url="sqlite+pysqlite:///:memory:",
                api_token="operation-qualification-token",
                riverhog_base_url="https://riverhog.invalid",
                riverhog_token="riverhog-qualification-token",
                riverhog_allow_insecure_http=False,
                recipes_path=Path("recipes.yaml"),
                observers={},
                targets={},
                workspace_assurance="ephemeral",
                claim_lease_seconds=1800,
                capability_ttl_seconds=900,
                scheduler_interval_seconds=5,
            ),
            riverhog_api=cast(ApiClient, _RiverhogContractApi()),
            state=state,
            recipes=RecipeCatalog(operations=(), recipes=()),
            work=work,
            coordinator=cast(Stove0Coordinator, object()),
            preview=cast(WorkflowPreviewService, object()),
            evaluations=EvaluationService(state.evaluation_store(), work=work),
            scheduler=cast(Stove0Scheduler, object()),
        )
    )


def create_adapter_contract_app() -> FastAPI:
    config = FtpAdapterConfig(
        host_id="qualification-host",
        riverhog_base_url="https://riverhog.invalid",
        riverhog_token="riverhog-qualification-token",
        api_token="adapter-qualification-token",
        sources=(
            SourceConfig(
                id="qualification-source",
                root=Path("/tmp/riverhog-operation-qualification"),
                ingest_source="ftp:qualification",
                tags=("qualification",),
                provenance="omit",
                provenance_omission_reason="Synthetic operation contract fixture.",
            ),
        ),
    )
    return create_adapter_app(
        FtpAdapterComposition(
            config,
            cast(Any, _RiverhogContractApi()),
            cast(Any, _AdapterContractService()),
        )
    )


class QualificationError(RuntimeError):
    """An actionable operation-qualification contract failure."""


@dataclass(frozen=True, slots=True)
class ApplicationSurface:
    name: str
    app: FastAPI
    client_types: tuple[type[Any], ...]
    cli_commands: tuple[tuple[str, Callable[..., object], bool], ...]
    supplemental_operations: tuple[SupplementalOperation, ...] = ()


@dataclass(frozen=True, slots=True)
class SupplementalOperation:
    """A public gateway/protocol operation that FastAPI does not own."""

    operation_id: str
    method: str
    path: str
    classification: str
    client_type: type[Any]
    client_method: str
    cli_commands: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Operation:
    application: str
    operation_id: str
    method: str
    path: str
    classification: str
    client: str | None
    cli_commands: tuple[str, ...]
    provider_evidence: str | None


def _callback_has_json(callback: Callable[..., object]) -> bool:
    try:
        return '"--json"' in inspect.getsource(callback)
    except (OSError, TypeError):
        return False


def _callback_has_projection_parity(
    callback: Callable[..., object],
    *,
    visited: set[tuple[str, str]] | None = None,
) -> bool:
    """Require executable machine and human projections from one callback path."""

    current_visited = set() if visited is None else visited
    identity = (
        str(getattr(callback, "__module__", "")),
        str(getattr(callback, "__qualname__", repr(callback))),
    )
    if identity in current_visited:
        return False
    current_visited.add(identity)
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(callback)))
    except (IndentationError, OSError, SyntaxError, TypeError):
        return False
    globals_ = getattr(callback, "__globals__", {})
    human = False
    machine = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        if name == "emit":
            mode = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "json_mode"),
                None,
            )
            if isinstance(mode, ast.Constant) and mode.value is True:
                machine = True
            elif isinstance(mode, ast.Constant) and mode.value is False:
                human = True
            elif mode is not None:
                human = True
                machine = True
        elif name == "dumps":
            machine = True
        elif name in {"echo", "print"}:
            human = True
        if isinstance(node.func, ast.Name):
            target = globals_.get(node.func.id)
            if _project_callable(target) and _callback_has_projection_parity(
                target,
                visited=current_visited,
            ):
                human = True
                machine = True
    return human and machine


def _typer_commands(
    root: Any,
    prefix: tuple[str, ...] = (),
    *,
    inherited_json: bool = False,
) -> Iterator[tuple[str, Any, bool]]:
    callback_info = getattr(root, "registered_callback", None)
    root_callback = getattr(callback_info, "callback", None)
    current_json = inherited_json or (
        root_callback is not None and _callback_has_json(root_callback)
    )
    for command in root.registered_commands:
        callback = command.callback
        if callback is None:
            continue
        name = command.name or callback.__name__.replace("_", "-")
        yield (
            " ".join((*prefix, name)),
            callback,
            (current_json or _callback_has_json(callback))
            and _callback_has_projection_parity(callback),
        )
    for group in root.registered_groups:
        yield from _typer_commands(
            group.typer_instance,
            (*prefix, str(group.name)),
            inherited_json=current_json,
        )


def _argparse_commands(
    parser: argparse.ArgumentParser,
    prefix: tuple[str, ...] = (),
    *,
    inherited_json: bool = False,
) -> Iterator[tuple[str, Any, bool]]:
    current_json = inherited_json or any(
        "--json" in option for current in parser._actions for option in current.option_strings
    )
    subcommands = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]
    for action in subcommands:
        for name, child in action.choices.items():
            nested = tuple(_argparse_commands(child, (*prefix, name), inherited_json=current_json))
            if nested:
                yield from nested
                continue
            callback = child.get_default("func")
            if callback is not None:
                has_json = current_json or any(
                    "--json" in option
                    for current in child._actions
                    for option in current.option_strings
                )
                yield (
                    " ".join((*prefix, name)),
                    callback,
                    has_json and _callback_has_projection_parity(callback),
                )


def application_surfaces() -> tuple[ApplicationSurface, ...]:
    return (
        ApplicationSurface(
            "riverhog",
            create_riverhog_app(),
            (ApiClient,),
            tuple(_typer_commands(riverhog_cli.app)),
        ),
        ApplicationSurface(
            "stove0",
            create_stove0_contract_app(),
            (Stove0ApiClient,),
            tuple(_typer_commands(stove0_cli.app)),
        ),
        ApplicationSurface(
            "riverhog-ftp-adapter",
            create_adapter_contract_app(),
            (RiverhogFtpAdapterClient,),
            tuple(_argparse_commands(build_adapter_parser())),
        ),
    )


def _project_callable(value: object) -> TypeGuard[Callable[..., object]]:
    module = str(getattr(value, "__module__", ""))
    return inspect.isfunction(value) and module.startswith(
        (
            "riverhog_",
            "stove0_",
        )
    )


def _reachable_operations(
    callback: Callable[..., object],
    *,
    operation_ids: frozenset[str],
    client_types: tuple[type[Any], ...],
) -> frozenset[str]:
    """Find client operations reachable from one executable command callback."""

    found: set[str] = set()
    visited: set[tuple[str, str]] = set()

    def visit(current: Callable[..., object]) -> None:
        identity = (
            str(getattr(current, "__module__", "")),
            str(getattr(current, "__qualname__", repr(current))),
        )
        if identity in visited:
            return
        visited.add(identity)
        try:
            source = textwrap.dedent(inspect.getsource(current))
            tree = ast.parse(source)
        except (IndentationError, OSError, SyntaxError, TypeError):
            return
        globals_ = getattr(current, "__globals__", {})
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in operation_ids:
                found.add(node.attr)
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                target = globals_.get(node.func.id)
                if _project_callable(target):
                    visit(target)
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            for client_type in client_types:
                target = getattr(client_type, node.func.attr, None)
                if _project_callable(target):
                    visit(target)

    visit(callback)
    return frozenset(found)


def _declared_interface(operation_id: str, value: object) -> str | None:
    if value is not None and value not in {
        "human-cli+json",
        "client-only-primitive",
        "standard-tool/protocol",
        "service-internal",
    }:
        raise QualificationError(
            f"operation has an invalid interface classification: {operation_id}: {value}"
        )
    return value if isinstance(value, str) else None


def _openapi_operations(app: FastAPI) -> Iterator[tuple[str, str, str, str | None]]:
    documented: set[tuple[str, str]] = set()
    for path, path_item in app.openapi()["paths"].items():
        for method, specification in path_item.items():
            if method not in HTTP_METHODS:
                continue
            documented.add((method.upper(), path))
            operation_id = specification.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                raise QualificationError(f"operation has no operationId: {method.upper()} {path}")
            declared = _declared_interface(
                operation_id,
                specification.get("x-riverhog-interface"),
            )
            yield operation_id, method.upper(), path, declared
    for route, path in _application_routes(app):
        if not isinstance(route, APIRoute) or route.include_in_schema:
            continue
        if not path.startswith(("/v1", "/internal/")):
            continue
        operation_id = route.operation_id or route.name
        if not operation_id:
            raise QualificationError(f"omitted operation has no operationId: {path}")
        declared = _declared_interface(
            operation_id,
            (route.openapi_extra or {}).get("x-riverhog-interface"),
        )
        for method in sorted(route.methods or ()):
            if method.casefold() not in SUPPORTED_ROUTE_METHODS:
                continue
            if (method.upper(), path) in documented:
                continue
            yield operation_id, method.upper(), path, declared


def _application_routes(app: FastAPI) -> Iterator[tuple[object, str]]:
    def walk(routes: Iterable[object], prefix: str) -> Iterator[tuple[object, str]]:
        for route in routes:
            if isinstance(route, APIRoute):
                yield route, f"{prefix}{route.path}"
                continue
            original_router = getattr(route, "original_router", None)
            include_context = getattr(route, "include_context", None)
            if original_router is None or include_context is None:
                continue
            yield from walk(
                getattr(original_router, "routes", ()),
                f"{prefix}{getattr(include_context, 'prefix', '')}",
            )

    yield from walk(app.routes, "")


def _client_owner(
    operation_id: str,
    client_types: tuple[type[Any], ...],
) -> str | None:
    owners = [
        client_type.__name__
        for client_type in client_types
        if operation_id in client_type.__dict__
        and callable(getattr(client_type, operation_id, None))
    ]
    if not owners:
        owners = [
            client_type.__name__
            for client_type in client_types
            if callable(getattr(client_type, operation_id, None))
        ]
    if len(owners) > 1:
        raise QualificationError(
            f"operation has ambiguous official client ownership: {operation_id}: {owners}"
        )
    return owners[0] if owners else None


def _provider_evidence(application: str, operation_id: str) -> str | None:
    if application != "riverhog":
        return None
    prefixes = (
        "acknowledge_retrieval_",
        "archive_copy",
        "cancel_archive_copy",
        "cancel_collection_upload",
        "cancel_retrieval_",
        "complete_collection_upload",
        "create_or_resume_archive_copy",
        "create_or_resume_collection_upload",
        "create_retrieval_",
        "delete_collection",
        "download_retrieval_",
        "get_archive_copy",
        "get_collection_upload",
        "get_retrieval_",
        "list_archive_copy",
        "list_collection_upload",
        "plan_archive_copy",
        "plan_collection_deletion",
        "plan_retrieval",
        "put_collection_upload",
        "register_collection_upload",
        "renew_retrieval_",
        "retrieval_cache_",
        "retire_archive_copy",
        "get_retrieval_cache_",
        "list_retrieval_cache_",
    )
    if operation_id.startswith(prefixes):
        return "provider-qualification:#442"
    return None


def operation_matrix() -> tuple[Operation, ...]:
    matrix: list[Operation] = []
    for surface in application_surfaces():
        openapi = tuple(_openapi_operations(surface.app))
        operation_ids = [item[0] for item in openapi]
        if len(operation_ids) != len(set(operation_ids)):
            raise QualificationError(
                f"{surface.name} OpenAPI operation IDs are not unique: {operation_ids}"
            )
        current_ids = frozenset(operation_ids)
        commands: dict[str, list[str]] = defaultdict(list)
        json_commands: dict[str, bool] = {}
        for command, callback, has_json in surface.cli_commands:
            json_commands[command] = has_json
            for operation_id in _reachable_operations(
                callback,
                operation_ids=current_ids,
                client_types=surface.client_types,
            ):
                commands[operation_id].append(command)

        for operation_id, method, path, declared in openapi:
            owner = _client_owner(operation_id, surface.client_types)
            operation_commands = tuple(sorted(set(commands.get(operation_id, ()))))
            if declared is not None:
                classification = declared
            elif path.startswith("/health/"):
                classification = "standard-tool/protocol"
            elif path.startswith("/internal/"):
                classification = "service-internal"
            elif surface.name == "riverhog" and path.startswith(
                ("/.well-known/resourcesync", "/resourcesync/")
            ):
                classification = "standard-tool/protocol"
            elif path.startswith("/v1") and operation_commands:
                classification = "human-cli+json"
            else:
                raise QualificationError(
                    f"unclassified operation: {surface.name} {method} {path} ({operation_id})"
                )

            if (
                path.startswith("/v1")
                and classification in {"human-cli+json", "client-only-primitive"}
                and owner is None
            ):
                raise QualificationError(
                    f"public operation has no official client: {surface.name} {operation_id}"
                )
            if classification == "client-only-primitive" and owner is None:
                raise QualificationError(f"client-only operation has no client: {operation_id}")
            if classification == "service-internal" and not path.startswith("/internal/"):
                raise QualificationError(
                    f"service-internal operation is not on an internal route: {operation_id}"
                )
            if classification == "human-cli+json" and not operation_commands:
                raise QualificationError(f"CLI operation has no command: {operation_id}")
            missing_json = [
                command for command in operation_commands if not json_commands.get(command, False)
            ]
            if classification == "human-cli+json" and missing_json:
                raise QualificationError(
                    f"CLI operation has commands without --json: {operation_id}: {missing_json}"
                )
            matrix.append(
                Operation(
                    application=surface.name,
                    operation_id=operation_id,
                    method=method,
                    path=path,
                    classification=classification,
                    client=owner,
                    cli_commands=operation_commands,
                    provider_evidence=_provider_evidence(surface.name, operation_id),
                )
            )
        available_commands = {command for command, _callback, _has_json in surface.cli_commands}
        for supplemental in surface.supplemental_operations:
            _declared_interface(supplemental.operation_id, supplemental.classification)
            if supplemental.method.casefold() not in SUPPORTED_ROUTE_METHODS:
                raise QualificationError(
                    f"supplemental operation has unsupported method: {supplemental.operation_id}"
                )
            client_method = getattr(supplemental.client_type, supplemental.client_method, None)
            if not callable(client_method):
                raise QualificationError(
                    "supplemental operation has no executable official-client method: "
                    f"{supplemental.operation_id}: "
                    f"{supplemental.client_type.__name__}.{supplemental.client_method}"
                )
            missing_commands = sorted(set(supplemental.cli_commands) - available_commands)
            if missing_commands:
                raise QualificationError(
                    f"supplemental operation has missing CLI workflows: "
                    f"{supplemental.operation_id}: {missing_commands}"
                )
            matrix.append(
                Operation(
                    application=surface.name,
                    operation_id=supplemental.operation_id,
                    method=supplemental.method,
                    path=supplemental.path,
                    classification=supplemental.classification,
                    client=supplemental.client_type.__name__,
                    cli_commands=supplemental.cli_commands,
                    provider_evidence=None,
                )
            )
    identities = [(item.application, item.operation_id, item.method, item.path) for item in matrix]
    if len(identities) != len(set(identities)):
        raise QualificationError("operation matrix identities are not unique")
    return tuple(sorted(matrix, key=lambda item: (item.application, item.path, item.method)))


def _matrix_sha256(matrix: Sequence[Operation]) -> str:
    payload = json.dumps(
        [asdict(operation) for operation in matrix],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _summary(matrix: Sequence[Operation]) -> dict[str, object]:
    return {
        "operations": len(matrix),
        "applications": dict(sorted(Counter(item.application for item in matrix).items())),
        "classifications": dict(sorted(Counter(item.classification for item in matrix).items())),
        "provider_evidence_operations": sum(item.provider_evidence is not None for item in matrix),
        "matrix_sha256": _matrix_sha256(matrix),
    }


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _source_sha(value: str) -> str:
    normalized = value.strip().casefold()
    if len(normalized) != 40 or any(
        character not in SOURCE_SHA_PATTERN for character in normalized
    ):
        raise QualificationError("source SHA must be an exact lowercase 40-character commit")
    if _git_head() != normalized:
        raise QualificationError("operation evidence source SHA does not match checked-out HEAD")
    return normalized


def _cold_cli_timings(*, trials: int = 3) -> dict[str, object]:
    entrypoints = {
        "riverhog": "from riverhog_cli.main import main; raise SystemExit(main())",
        "stove0": "from stove0_cli.main import main; main()",
        "riverhog-ftp-adapter": ("from riverhog_ftp_adapter.app import main; raise SystemExit(main())"),
    }
    timings: dict[str, object] = {}
    for application, program in entrypoints.items():
        samples: list[float] = []
        output_sha256: str | None = None
        output_bytes: int | None = None
        for _ in range(trials):
            started = time.perf_counter()
            completed = subprocess.run(
                [sys.executable, "-c", program, "--help"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            samples.append((time.perf_counter() - started) * 1000)
            if completed.returncode != 0:
                raise QualificationError(
                    f"{application} cold --help failed with exit {completed.returncode}"
                )
            current_sha256 = hashlib.sha256(completed.stdout).hexdigest()
            if output_sha256 is not None and current_sha256 != output_sha256:
                raise QualificationError(f"{application} cold --help output is not stable")
            output_sha256 = current_sha256
            output_bytes = len(completed.stdout)
        timings[application] = {
            "trials": trials,
            "minimum_ms": round(min(samples), 3),
            "median_ms": round(median(samples), 3),
            "maximum_ms": round(max(samples), 3),
            "output_bytes": output_bytes,
            "output_sha256": output_sha256,
        }
    return timings


def _timing_summary(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise QualificationError(f"{label} timing summary is missing")
    try:
        samples = int(value["samples"])
        minimum = float(value["minimum_ms"])
        middle = float(value["median_ms"])
        maximum = float(value["maximum_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise QualificationError(f"{label} timing summary is invalid") from exc
    if samples < 1 or minimum <= 0 or not minimum <= middle <= maximum:
        raise QualificationError(f"{label} timing summary is invalid")
    return dict(value)


def _load_operation_timings(
    path: Path,
    *,
    source_sha: str,
    matrix: Sequence[Operation],
) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise QualificationError("operation timing evidence is unavailable or invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != TIMING_SCHEMA
        or payload.get("source_sha") != source_sha
        or payload.get("pytest_exit_status") != 0
    ):
        raise QualificationError("operation timing evidence identity or test result is invalid")
    rows = payload.get("operations")
    if not isinstance(rows, list):
        raise QualificationError("operation timing evidence rows are invalid")
    current = {(item.application, item.operation_id): item for item in matrix}
    observed: set[tuple[str, str]] = set()
    client_observed: set[tuple[str, str]] = set()
    validated: list[dict[str, object]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            raise QualificationError("operation timing evidence row is invalid")
        identity = (str(raw.get("application") or ""), str(raw.get("operation_id") or ""))
        if identity not in current or identity in observed:
            raise QualificationError(f"operation timing evidence identity is invalid: {identity}")
        observed.add(identity)
        item: dict[str, object] = {
            "application": identity[0],
            "operation_id": identity[1],
            "server_wall": _timing_summary(
                raw.get("server_wall"),
                label=f"{identity[0]}:{identity[1]} server",
            ),
        }
        if raw.get("client_wall") is not None:
            item["client_wall"] = _timing_summary(
                raw.get("client_wall"),
                label=f"{identity[0]}:{identity[1]} client",
            )
            client_observed.add(identity)
        validated.append(item)
    locally_required = {
        (item.application, item.operation_id) for item in matrix if item.provider_evidence is None
    }
    missing = sorted(locally_required - observed)
    if missing:
        raise QualificationError(f"operations lack positive local timing witnesses: {missing}")
    client_required = {
        (item.application, item.operation_id)
        for item in matrix
        if item.provider_evidence is None
        and item.classification in {"human-cli+json", "client-only-primitive"}
    }
    missing_client = sorted(client_required - client_observed)
    if missing_client:
        raise QualificationError(
            f"official-client operations lack client wall timings: {missing_client}"
        )
    raw_projections = payload.get("cli_projections")
    if not isinstance(raw_projections, list):
        raise QualificationError("CLI projection evidence rows are invalid")
    required_commands = {
        (item.application, command)
        for item in matrix
        if item.classification == "human-cli+json"
        for command in item.cli_commands
    }
    projections: list[dict[str, object]] = []
    projection_identities: set[tuple[str, str]] = set()
    complete_commands: set[tuple[str, str]] = set()
    for raw in raw_projections:
        if not isinstance(raw, dict):
            raise QualificationError("CLI projection evidence row is invalid")
        identity = (str(raw.get("application") or ""), str(raw.get("command") or ""))
        if identity not in required_commands or identity in projection_identities:
            raise QualificationError(f"CLI projection evidence identity is invalid: {identity}")
        projection_identities.add(identity)
        human = raw.get("human_executions")
        machine = raw.get("json_executions")
        if (
            not isinstance(human, int)
            or isinstance(human, bool)
            or human < 0
            or not isinstance(machine, int)
            or isinstance(machine, bool)
            or machine < 0
        ):
            raise QualificationError(f"CLI projection evidence counts are invalid: {identity}")
        if human > 0 and machine > 0:
            complete_commands.add(identity)
        projections.append(
            {
                "application": identity[0],
                "command": identity[1],
                "human_executions": human,
                "json_executions": machine,
            }
        )
    missing_cli_commands = sorted(required_commands - complete_commands)
    if missing_cli_commands:
        raise QualificationError(
            f"CLI commands lack executed human/JSON projection parity: {missing_cli_commands}"
        )
    return {
        "schema": TIMING_SCHEMA,
        "source_sha": source_sha,
        "operations": sorted(
            validated,
            key=lambda item: (str(item["application"]), str(item["operation_id"])),
        ),
        "cli_projections": sorted(
            projections,
            key=lambda item: (str(item["application"]), str(item["command"])),
        ),
        "complete_cli_commands": len(complete_commands),
    }


def evidence(*, source_sha: str, timings: Path) -> dict[str, object]:
    matrix = operation_matrix()
    local_timings = _load_operation_timings(
        timings,
        source_sha=_source_sha(source_sha),
        matrix=matrix,
    )
    return {
        "schema": SCHEMA,
        "source_sha": source_sha,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "summary": _summary(matrix),
        "qualification": {
            "positive_local_lifecycles": {
                "status": "passed",
                "operations": sum(item.provider_evidence is None for item in matrix),
            },
            "cli_human_json_projection": {
                "status": "passed",
                "operations": sum(item.classification == "human-cli+json" for item in matrix),
                "commands": cast(int, local_timings["complete_cli_commands"]),
            },
            "bounded_state_access": {
                "status": "passed",
                "applications": ["riverhog", "riverhog-ftp-adapter", "stove0"],
            },
            "event_cursor_restart_resume": {
                "status": "passed",
                "applications": ["riverhog", "riverhog-ftp-adapter", "stove0"],
            },
            "provider_backed_lifecycles": {
                "status": "linked",
                "operations": sum(item.provider_evidence is not None for item in matrix),
            },
        },
        "performance": {
            "cold_cli_startup": _cold_cli_timings(),
            "local_api": local_timings,
            "interpretation": (
                "Client wall and in-process server/service wall are separate. Provider "
                "asynchronous phase and transfer timings remain in the linked #442 evidence."
            ),
        },
        "provider_evidence": {
            "issue": 442,
            "workflow": "provider-qualification",
            "required_for": sorted(
                f"{item.application}:{item.operation_id}"
                for item in matrix
                if item.provider_evidence is not None
            ),
        },
        "operations": [asdict(operation) for operation in matrix],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="Validate and summarize the current operation matrix.")
    evidence_parser = subparsers.add_parser(
        "evidence",
        help="Write exact-SHA operation matrix evidence.",
    )
    evidence_parser.add_argument("--source-sha", required=True)
    evidence_parser.add_argument("--timings", required=True, type=Path)
    evidence_parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "check":
            print(json.dumps(_summary(operation_matrix()), sort_keys=True))
            return 0
        payload = evidence(source_sha=args.source_sha, timings=args.timings)
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        summary = payload["summary"]
        if not isinstance(summary, dict):
            raise QualificationError("operation evidence summary is invalid")
        print(json.dumps({"output": str(output), **summary}, sort_keys=True))
        return 0
    except (QualificationError, subprocess.CalledProcessError) as exc:
        print(f"operation qualification failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
