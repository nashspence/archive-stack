from __future__ import annotations

import os
from contextlib import ExitStack
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from riverhog_archive_contracts import ARCHIVE_ENCRYPTION_FORMAT
from riverhog_core.archive_store_registry import ArchiveStoreBinding, ArchiveStoreRegistry
from riverhog_core.catalog_db import (
    SessionFactory,
    dispose_session_factory,
    make_session_factory,
    session_scope,
    validate_db,
)
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionRecord,
    CollectionUploadRecord,
)
from riverhog_core.collection_access import SqlAlchemyCollectionAccessService
from riverhog_core.ports.download_allowance import DownloadAllowance
from riverhog_core.runtime_config import (
    RuntimeConfig,
    StorageAdapterRegistration,
    load_runtime_config,
)
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from riverhog_core.services.archive_copies import SqlAlchemyArchiveCopyService
from riverhog_core.services.archive_copy_retirements import (
    SqlAlchemyArchiveCopyRetirementService,
)
from riverhog_core.services.archive_maintenance import SqlAlchemyArchiveMaintenanceService
from riverhog_core.services.archive_stores import SqlAlchemyArchiveStoreService
from riverhog_core.services.collection_deletions import SqlAlchemyCollectionDeletionService
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_core.services.collection_workflows import SqlAlchemyCollectionWorkflowService
from riverhog_core.services.collections import SqlAlchemyCollectionService
from riverhog_core.services.download_allowances import SqlAlchemyDownloadAllowance
from riverhog_core.services.interfaces import (
    AppKeyService,
    ArchiveCopyRetirementService,
    ArchiveCopyService,
    ArchiveMaintenanceService,
    ArchiveStoreService,
    CollectionDeletionService,
    CollectionService,
    LifecycleEventService,
    ProvenanceService,
    RetrievalService,
    SearchService,
    TagService,
)
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService
from riverhog_core.services.provenance import SqlAlchemyProvenanceService
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_core.services.retrieval_cache import SqlAlchemyRetrievalCache
from riverhog_core.services.search import SqlAlchemySearchService
from riverhog_core.services.tags import SqlAlchemyTagService
from riverhog_core.stores.storage_adapter_archive_objects import (
    StorageAdapterArchiveObjectRangeStore,
    StorageAdapterArchiveResumableObjectStore,
    StorageAdapterImmutableArchiveObjectStore,
)
from riverhog_core.stores.storage_adapter_archive_store import StorageAdapterArchiveStore
from riverhog_core.stores.storage_adapter_retrieval_cache import StorageAdapterRetrievalCache
from riverhog_core.throughput import ArchiveThroughputTuning, ArchiveTransferResources
from riverhog_storage_adapter_protocol import validated_storage_adapter
from riverhog_storage_adapter_support import StorageAdapterClient
from sqlalchemy import select


@dataclass(slots=True)
class ServiceContainer:
    app_keys: AppKeyService
    collection_access: SqlAlchemyCollectionAccessService
    tags: TagService
    collections: CollectionService
    collection_uploads: SqlAlchemyCollectionUploadService
    collection_workflows: SqlAlchemyCollectionWorkflowService
    provenance: ProvenanceService
    collection_deletions: CollectionDeletionService
    search: SearchService
    archive_maintenance: ArchiveMaintenanceService
    archive_copies: ArchiveCopyService
    archive_copy_retirements: ArchiveCopyRetirementService
    archive_stores: ArchiveStoreService
    retrieval: RetrievalService
    lifecycle_events: LifecycleEventService
    download_quotas: DownloadAllowance
    session_factory: SessionFactory
    storage_adapter_clients: tuple[StorageAdapterClient, ...] = ()

    def close(self) -> None:
        dispose_session_factory(self.session_factory)
        for client in self.storage_adapter_clients:
            client.close()


def _archive_store_registry(
    config: RuntimeConfig,
    *,
    adapters: dict[str, StorageAdapterClient],
    download_allowance: DownloadAllowance,
) -> ArchiveStoreRegistry:
    validated_adapters = {
        name: validated_storage_adapter(adapter) for name, adapter in adapters.items()
    }
    return ArchiveStoreRegistry(
        {
            name: ArchiveStoreBinding(
                store=StorageAdapterArchiveStore(
                    config,
                    name=name,
                    adapter=validated_adapters[name],
                    download_allowance=download_allowance,
                ),
                resumable_objects=StorageAdapterArchiveResumableObjectStore(
                    validated_adapters[name]
                ),
                immutable_objects=StorageAdapterImmutableArchiveObjectStore(
                    validated_adapters[name]
                ),
                object_ranges=StorageAdapterArchiveObjectRangeStore(validated_adapters[name]),
            )
            for name in config.archive_stores
        }
    )


def _adapter_client(registration: StorageAdapterRegistration) -> StorageAdapterClient:
    return StorageAdapterClient.from_token_file(
        registration.base_url,
        token_file=registration.token_file,
        allow_insecure_http=registration.allow_insecure_http,
        timeout=registration.timeout_seconds,
        maximum_connections=registration.maximum_connections,
    )


def _build_default_container(
    config: RuntimeConfig,
    *,
    session_factory: SessionFactory,
    startup_cleanup: ExitStack,
) -> ServiceContainer:
    _require_archive_encryption_bindings(config, session_factory=session_factory)
    throughput_tuning = ArchiveThroughputTuning.from_env(os.environ)
    transfer_resources = ArchiveTransferResources.from_tuning(throughput_tuning)
    adapters: dict[str, StorageAdapterClient] = {}
    for name, store in config.archive_stores.items():
        client = _adapter_client(store)
        startup_cleanup.callback(client.close)
        adapters[name] = client
    for client in adapters.values():
        client.check_readiness()
    cache_clients: dict[str, StorageAdapterClient] = {}
    cache_stores: dict[str, StorageAdapterRetrievalCache] = {}
    for name, registration in config.retrieval_cache_stores.items():
        client = _adapter_client(registration.adapter)
        startup_cleanup.callback(client.close)
        client.check_readiness()
        cache_clients[name] = client
        cache_stores[name] = StorageAdapterRetrievalCache(
            name,
            client,
            write_segment_bytes=config.retrieval_cache_write_segment_bytes,
            throughput_tuning=throughput_tuning,
            transfer_resources=transfer_resources,
        )
    retrieval_cache = (
        SqlAlchemyRetrievalCache(
            cache_stores,
            config.retrieval_cache_stores,
            session_factory=session_factory,
        )
        if cache_stores
        else None
    )
    restore_required = [
        name
        for name, client in adapters.items()
        if client.descriptor().read_mode == "restore_required"
    ]
    if restore_required and retrieval_cache is None:
        raise ValueError(
            "restore-required archive adapters require a retrieval cache adapter: "
            + ", ".join(restore_required)
        )
    download_allowance = SqlAlchemyDownloadAllowance(
        config,
        session_factory=session_factory,
    )
    archive_stores = _archive_store_registry(
        config,
        adapters=adapters,
        download_allowance=download_allowance,
    )
    return ServiceContainer(
        app_keys=SqlAlchemyAppKeyService(config, session_factory=session_factory),
        collection_access=SqlAlchemyCollectionAccessService(
            config,
            session_factory=session_factory,
        ),
        tags=SqlAlchemyTagService(config, session_factory=session_factory),
        collections=SqlAlchemyCollectionService(config, session_factory=session_factory),
        collection_uploads=SqlAlchemyCollectionUploadService(
            config,
            archive_stores,
            retrieval_cache=retrieval_cache,
            session_factory=session_factory,
            throughput_tuning=throughput_tuning,
            transfer_resources=transfer_resources,
        ),
        collection_workflows=SqlAlchemyCollectionWorkflowService(
            config, session_factory=session_factory
        ),
        provenance=SqlAlchemyProvenanceService(config, session_factory=session_factory),
        collection_deletions=SqlAlchemyCollectionDeletionService(
            config,
            archive_stores,
            retrieval_cache=retrieval_cache,
            session_factory=session_factory,
        ),
        search=SqlAlchemySearchService(config, session_factory=session_factory),
        archive_maintenance=SqlAlchemyArchiveMaintenanceService(
            config,
            archive_stores,
            session_factory=session_factory,
        ),
        archive_copies=SqlAlchemyArchiveCopyService(
            config,
            archive_stores,
            retrieval_cache=retrieval_cache,
            session_factory=session_factory,
            throughput_tuning=throughput_tuning,
            transfer_resources=transfer_resources,
        ),
        archive_copy_retirements=SqlAlchemyArchiveCopyRetirementService(
            config,
            archive_stores,
            session_factory=session_factory,
        ),
        archive_stores=SqlAlchemyArchiveStoreService(
            config,
            archive_stores,
            download_allowance=download_allowance,
            session_factory=session_factory,
        ),
        retrieval=SqlAlchemyRetrievalService(
            config,
            archive_stores,
            retrieval_cache,
            download_allowance=download_allowance,
            session_factory=session_factory,
            throughput_tuning=throughput_tuning,
            transfer_resources=transfer_resources,
        ),
        lifecycle_events=SqlAlchemyLifecycleEventService(
            config,
            session_factory=session_factory,
        ),
        download_quotas=download_allowance,
        session_factory=session_factory,
        storage_adapter_clients=tuple([*adapters.values(), *cache_clients.values()]),
    )


def _require_archive_encryption_bindings(
    config: RuntimeConfig,
    *,
    session_factory: SessionFactory,
) -> None:
    with session_scope(session_factory) as session:
        bindings = set(
            session.query(CollectionRecord.encryption_format, CollectionRecord.passphrase_id)
        ) | set(
            session.query(
                CollectionUploadRecord.encryption_format,
                CollectionUploadRecord.passphrase_id,
            )
        )
        descriptor_exists = (
            select(CollectionArchiveObjectRecord.object_id)
            .where(
                CollectionArchiveObjectRecord.collection_id
                == CollectionArchiveCopyRecord.collection_id,
                CollectionArchiveObjectRecord.store == CollectionArchiveCopyRecord.store,
                CollectionArchiveObjectRecord.kind == "recovery-descriptor",
            )
            .exists()
        )
        missing_descriptor = session.execute(
            select(
                CollectionArchiveCopyRecord.collection_id,
                CollectionArchiveCopyRecord.store,
            )
            .where(
                CollectionArchiveCopyRecord.state == "uploaded",
                ~descriptor_exists,
            )
            .limit(1)
        ).first()
    for encryption_format, passphrase_id in bindings:
        if encryption_format != ARCHIVE_ENCRYPTION_FORMAT:
            raise ValueError(
                f"unsupported persisted archive encryption format: {encryption_format}"
            )
        config.archive_passphrase_for(passphrase_id)
    if missing_descriptor is not None:
        collection_id, store = missing_descriptor
        raise ValueError(
            "uploaded archive copy has no recovery descriptor: "
            f"collection={collection_id} store={store}"
        )


@lru_cache(maxsize=1)
def default_container() -> ServiceContainer:
    config = load_runtime_config()
    validate_db(config.database_url)
    session_factory = make_session_factory(config.database_url)
    startup_cleanup = ExitStack()
    startup_cleanup.callback(dispose_session_factory, session_factory)
    try:
        container = _build_default_container(
            config,
            session_factory=session_factory,
            startup_cleanup=startup_cleanup,
        )
    except BaseException:
        startup_cleanup.close()
        raise
    startup_cleanup.pop_all()
    return container


def get_container() -> ServiceContainer:
    return default_container()


ContainerDep = Annotated[ServiceContainer, Depends(get_container)]
