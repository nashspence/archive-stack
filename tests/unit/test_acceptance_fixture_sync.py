from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path
from typing import Protocol

from arc_api.deps import ServiceContainer
from arc_core.services.contracts import (
    CollectionService,
    CopyService,
    FetchService,
    FileService,
    GlacierReportingService,
    GlacierUploadService,
    PinService,
    PlanningService,
    RecoverySessionService,
    SearchService,
)
from tests.fixtures.acceptance import (
    AcceptanceCollectionService,
    AcceptanceCopyService,
    AcceptanceFetchService,
    AcceptanceFileService,
    AcceptanceGlacierReportingService,
    AcceptanceGlacierUploadService,
    AcceptancePinService,
    AcceptancePlanningService,
    AcceptanceRecoverySessionService,
    AcceptanceSearchService,
)

SERVICE_SYNC_CONTRACTS = {
    "collections": (AcceptanceCollectionService, CollectionService),
    "search": (AcceptanceSearchService, SearchService),
    "planning": (AcceptancePlanningService, PlanningService),
    "glacier_uploads": (AcceptanceGlacierUploadService, GlacierUploadService),
    "glacier_reporting": (AcceptanceGlacierReportingService, GlacierReportingService),
    "recovery_sessions": (AcceptanceRecoverySessionService, RecoverySessionService),
    "copies": (AcceptanceCopyService, CopyService),
    "pins": (AcceptancePinService, PinService),
    "fetches": (AcceptanceFetchService, FetchService),
    "files": (AcceptanceFileService, FileService),
}

REPO_ROOT = Path(__file__).resolve().parents[2]
ACCEPTANCE_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "acceptance.py"

LOCKED_HELPER_STATE_ACCESS = {
    "seed_finalized_image": "mutates finalized image and Glacier upload state directly",
    "fail_glacier_upload": "mutates Glacier upload failure fixtures directly",
    "delete_hot_backing_file": "mutates hot backing visibility directly",
    "has_committed_collection_file": "reads committed collection file state directly",
    "collection_source_root": "reads registered collection source paths directly",
    "inspect_downloaded_iso": "reads finalized image metadata before filesystem inspection",
    "expire_collection_upload": "mutates collection upload expiry timestamps directly",
    "expire_fetch_upload": "mutates fetch upload expiry timestamps directly",
    "wait_for_collection_upload_cleanup": "polls collection upload cleanup state directly",
    "wait_for_fetch_upload_cleanup": "polls fetch upload cleanup state directly",
    "upload_collection_source": "reads collection source root directly before HTTP upload flow",
    "seed_photos_hot": "checks collection seed state before HTTP upload flow",
    "seed_nested_photos_hot": "checks collection seed state before HTTP upload flow",
    "seed_parent_photos_hot": "checks collection seed state before HTTP upload flow",
    "seed_docs_hot": "checks collection seed state before HTTP upload flow",
    "seed_docs_archive": "checks and adjusts docs archive state around HTTP setup",
    "seed_docs_archive_with_split_invoice": (
        "checks and adjusts split archive state around HTTP setup"
    ),
    "constrain_collection_to_paths": "mutates collection file projection state directly",
    "constrain_collection_to_finalized_image_coverage": "reads finalized image coverage directly",
    "recovery_upload_absent": "reads fetch existence directly",
    "list_read_only_browsing_paths": "reads hot file projection state directly",
    "bucket_contains_object": "reads fixture-backed bucket object state directly",
    "bucket_contains_prefix": "reads fixture-backed bucket prefix state directly",
    "uploaded_entry_content": "reads uploaded fetch entry content directly",
    "configure_arc_disc_fixture": "reads fetch entry content directly before fixture file writes",
}

DELEGATED_HELPER_STATE_ACCESS = {
    "list_webhook_deliveries": "delegates to locked AcceptanceState webhook listing",
    "list_webhook_attempts": "delegates to locked AcceptanceState webhook listing",
    "configure_webhook_failure": "delegates to locked AcceptanceState webhook behavior mutation",
    "seed_collection_source": "delegates collection source registration to locked AcceptanceState",
    "seed_image_fixtures": "delegates candidate image registration to locked AcceptanceState",
}

LIFECYCLE_HELPER_STATE_ACCESS = {
    "restart": "rebuilds service handles around the same state while restarting the test server",
    "reset": "swaps the shared fixture state between scenarios before yielding the fixture",
}

EXTERNAL_CALLS_FORBIDDEN_UNDER_STATE_LOCK = {
    "request",
    "run_arc",
    "run_arc_disc",
}


class _AcceptanceSystemStateVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.state_methods: set[str] = set()
        self.locked_methods: set[str] = set()
        self.external_calls_under_lock: list[str] = []
        self._method_stack: list[str] = []
        self._lock_depth = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            node.attr == "state"
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and self._method_stack
        ):
            self.state_methods.add(self._method_stack[-1])
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        is_state_lock = any(self._is_state_lock(item.context_expr) for item in node.items)
        if is_state_lock and self._method_stack:
            self.locked_methods.add(self._method_stack[-1])
        self._lock_depth += int(is_state_lock)
        self.generic_visit(node)
        self._lock_depth -= int(is_state_lock)

    def visit_Call(self, node: ast.Call) -> None:
        if self._lock_depth > 0 and self._method_stack and self._is_forbidden_external_call(node):
            self.external_calls_under_lock.append(
                f"{self._method_stack[-1]}:{node.lineno}:{ast.unparse(node.func)}"
            )
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._method_stack.append(node.name)
        self.generic_visit(node)
        self._method_stack.pop()

    @staticmethod
    def _is_state_lock(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "lock"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "state"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
        )

    @staticmethod
    def _is_forbidden_external_call(node: ast.Call) -> bool:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
        ):
            return func.attr in EXTERNAL_CALLS_FORBIDDEN_UNDER_STATE_LOCK
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        )


def _contract_method_names(contract: type[Protocol]) -> list[str]:
    return [
        name
        for name, value in vars(contract).items()
        if not name.startswith("_") and callable(value)
    ]


def _acceptance_system_state_visitor() -> _AcceptanceSystemStateVisitor:
    tree = ast.parse(
        ACCEPTANCE_FIXTURE.read_text(encoding="utf-8"),
        filename=str(ACCEPTANCE_FIXTURE),
    )
    visitor = _AcceptanceSystemStateVisitor()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AcceptanceSystem":
            visitor.visit(node)
            return visitor
    raise AssertionError("AcceptanceSystem class not found")


def test_acceptance_fixture_sync_guard_covers_every_service_container_field() -> None:
    assert set(SERVICE_SYNC_CONTRACTS) == {field.name for field in fields(ServiceContainer)}


def test_acceptance_fixture_service_contract_methods_are_state_locked() -> None:
    missing: list[str] = []
    for service_name, (implementation, contract) in SERVICE_SYNC_CONTRACTS.items():
        for method_name in _contract_method_names(contract):
            method = getattr(implementation, method_name)
            if not getattr(method, "__acceptance_state_locked__", False):
                missing.append(f"{service_name}.{method_name}")

    assert missing == []


def test_acceptance_system_state_helpers_are_explicitly_classified() -> None:
    visitor = _acceptance_system_state_visitor()
    classified = (
        set(LOCKED_HELPER_STATE_ACCESS)
        | set(DELEGATED_HELPER_STATE_ACCESS)
        | set(LIFECYCLE_HELPER_STATE_ACCESS)
    )

    assert visitor.state_methods == classified


def test_acceptance_system_direct_state_helpers_take_narrow_locks() -> None:
    visitor = _acceptance_system_state_visitor()

    assert set(LOCKED_HELPER_STATE_ACCESS).issubset(visitor.locked_methods)
    assert set(DELEGATED_HELPER_STATE_ACCESS).isdisjoint(visitor.locked_methods)
    assert visitor.external_calls_under_lock == []
