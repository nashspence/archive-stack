from __future__ import annotations

from dataclasses import dataclass

from arc_core.services.collections import StubCollectionService
from arc_core.services.contracts import CollectionService, CopyService, FetchService, PinService, PlanningService, SearchService
from arc_core.services.copies import StubCopyService
from arc_core.services.fetches import StubFetchService
from arc_core.services.pins import StubPinService
from arc_core.services.planning import StubPlanningService
from arc_core.services.search import StubSearchService


@dataclass(slots=True)
class ServiceContainer:
    collections: CollectionService
    search: SearchService
    planning: PlanningService
    copies: CopyService
    pins: PinService
    fetches: FetchService


def default_container() -> ServiceContainer:
    return ServiceContainer(
        collections=StubCollectionService(),
        search=StubSearchService(),
        planning=StubPlanningService(),
        copies=StubCopyService(),
        pins=StubPinService(),
        fetches=StubFetchService(),
    )


def get_container() -> ServiceContainer:
    return default_container()
