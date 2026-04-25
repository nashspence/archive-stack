from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends

from arc_core.runtime_config import load_runtime_config
from arc_core.services.collections import SqlAlchemyCollectionService
from arc_core.services.contracts import (
    CollectionService,
    CopyService,
    FetchService,
    FileService,
    PinService,
    PlanningService,
    SearchService,
)
from arc_core.services.copies import SqlAlchemyCopyService
from arc_core.services.fetches import SqlAlchemyFetchService
from arc_core.services.files import SqlAlchemyFileService
from arc_core.services.pins import SqlAlchemyPinService
from arc_core.services.planning import SqlAlchemyPlanningService
from arc_core.services.search import SqlAlchemySearchService
from arc_core.sqlite_db import initialize_db


@dataclass(slots=True)
class ServiceContainer:
    collections: CollectionService
    search: SearchService
    planning: PlanningService
    copies: CopyService
    pins: PinService
    fetches: FetchService
    files: FileService


def default_container() -> ServiceContainer:
    config = load_runtime_config()
    initialize_db(str(config.sqlite_path))
    return ServiceContainer(
        collections=SqlAlchemyCollectionService(config),
        search=SqlAlchemySearchService(config),
        planning=SqlAlchemyPlanningService(config),
        copies=SqlAlchemyCopyService(config),
        pins=SqlAlchemyPinService(config),
        fetches=SqlAlchemyFetchService(config),
        files=SqlAlchemyFileService(config),
    )


def get_container() -> ServiceContainer:
    return default_container()


ContainerDep = Annotated[ServiceContainer, Depends(get_container)]
