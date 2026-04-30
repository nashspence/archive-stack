from __future__ import annotations

from dataclasses import fields
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


def _contract_method_names(contract: type[Protocol]) -> list[str]:
    return [
        name
        for name, value in vars(contract).items()
        if not name.startswith("_") and callable(value)
    ]


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
