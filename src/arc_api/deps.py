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
from arc_core.stores.seaweedfs_hot_store import SeaweedFSHotStore
from arc_core.stores.seaweedfs_upload_store import SeaweedFSTUSUploadStore


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
    hot_store = SeaweedFSHotStore(config.seaweedfs_filer_url)
    upload_store = SeaweedFSTUSUploadStore(config.seaweedfs_filer_url)
    return ServiceContainer(
        collections=SqlAlchemyCollectionService(config, hot_store),
        search=SqlAlchemySearchService(config),
        planning=SqlAlchemyPlanningService(config),
        copies=SqlAlchemyCopyService(config, hot_store),
        pins=SqlAlchemyPinService(config, hot_store, upload_store),
        fetches=SqlAlchemyFetchService(config, hot_store, upload_store),
        files=SqlAlchemyFileService(config, hot_store),
    )


def get_container() -> ServiceContainer:
    return default_container()


ContainerDep = Annotated[ServiceContainer, Depends(get_container)]
