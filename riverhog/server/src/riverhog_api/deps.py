from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from riverhog_core.archive_ingress_registry import ArchiveIngressStore, ArchiveIngressStoreRegistry
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import validate_db
from riverhog_core.collection_access import SqlAlchemyCollectionAccessService
from riverhog_core.ports.download_allowance import DownloadAllowance
from riverhog_core.proofs import CommandProofStamper, CommandProofUpgrader, CommandProofVerifier
from riverhog_core.runtime_config import load_runtime_config
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from riverhog_core.services.archive_attestations import SqlAlchemyArchiveAttestationService
from riverhog_core.services.archive_copies import SqlAlchemyArchiveCopyService
from riverhog_core.services.archive_copy_retirements import (
    SqlAlchemyArchiveCopyRetirementService,
)
from riverhog_core.services.archive_maintenance import SqlAlchemyArchiveMaintenanceService
from riverhog_core.services.archive_stores import SqlAlchemyArchiveStoreService
from riverhog_core.services.collection_deletions import SqlAlchemyCollectionDeletionService
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_core.services.collections import SqlAlchemyCollectionService
from riverhog_core.services.download_allowances import SqlAlchemyDownloadAllowance
from riverhog_core.services.interfaces import (
    AppKeyService,
    ArchiveAttestationService,
    ArchiveCopyRetirementService,
    ArchiveCopyService,
    ArchiveMaintenanceService,
    ArchiveStoreService,
    CollectionDeletionService,
    CollectionService,
    LifecycleEventService,
    ProofMaturationService,
    ProvenanceService,
    RetrievalService,
    SearchService,
    TagService,
)
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService
from riverhog_core.services.proof_maturations import SqlAlchemyProofMaturationService
from riverhog_core.services.provenance import SqlAlchemyProvenanceService
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_core.services.search import SqlAlchemySearchService
from riverhog_core.services.tags import SqlAlchemyTagService
from riverhog_core.stores.s3_archive_ingress_store import S3ArchiveMultipartObjectStore
from riverhog_core.stores.s3_archive_manifest_store import S3ImmutableArchiveObjectStore
from riverhog_core.stores.s3_archive_range_store import S3ArchiveObjectRangeStore
from riverhog_core.stores.s3_archive_store import S3ArchiveStore
from riverhog_core.stores.s3_retrieval_cache import S3RetrievalCache
from riverhog_core.stores.s3_support import ensure_bucket_exists


@dataclass(slots=True)
class ServiceContainer:
    app_keys: AppKeyService
    collection_access: SqlAlchemyCollectionAccessService
    tags: TagService
    collections: CollectionService
    collection_uploads: SqlAlchemyCollectionUploadService
    provenance: ProvenanceService
    collection_deletions: CollectionDeletionService
    search: SearchService
    archive_maintenance: ArchiveMaintenanceService
    archive_copies: ArchiveCopyService
    proof_maturations: ProofMaturationService
    archive_attestations: ArchiveAttestationService
    archive_copy_retirements: ArchiveCopyRetirementService
    archive_stores: ArchiveStoreService
    retrieval: RetrievalService
    lifecycle_events: LifecycleEventService
    download_quotas: DownloadAllowance


@lru_cache(maxsize=1)
def default_container() -> ServiceContainer:
    config = load_runtime_config()
    validate_db(config.database_url)
    ensure_bucket_exists(config)
    retrieval_cache = S3RetrievalCache(config) if config.retrieval_cache is not None else None
    download_allowance = SqlAlchemyDownloadAllowance(config)
    archive_stores = ArchiveStoreRegistry(
        {
            name: S3ArchiveStore(
                config,
                store,
                retrieval_cache=retrieval_cache,
                download_allowance=download_allowance,
            )
            for name, store in config.archive_stores.items()
        }
    )
    archive_ingress_stores = ArchiveIngressStoreRegistry(
        {
            name: ArchiveIngressStore(
                multipart=S3ArchiveMultipartObjectStore(config, store),
                root=S3ImmutableArchiveObjectStore(config, store),
                ranges=S3ArchiveObjectRangeStore(config, store),
            )
            for name, store in config.archive_stores.items()
        }
    )
    proof_stamper = CommandProofStamper(config.ots_stamp_command)
    proof_verifier = CommandProofVerifier(config.ots_verify_command)
    proof_upgrader = CommandProofUpgrader(config.ots_upgrade_command)
    return ServiceContainer(
        app_keys=SqlAlchemyAppKeyService(config),
        collection_access=SqlAlchemyCollectionAccessService(config),
        tags=SqlAlchemyTagService(config),
        collections=SqlAlchemyCollectionService(config),
        collection_uploads=SqlAlchemyCollectionUploadService(
            config,
            archive_stores,
            archive_ingress_stores,
            proof_stamper=proof_stamper,
        ),
        provenance=SqlAlchemyProvenanceService(config),
        collection_deletions=SqlAlchemyCollectionDeletionService(
            config,
            archive_stores,
            retrieval_cache,
        ),
        search=SqlAlchemySearchService(config),
        archive_maintenance=SqlAlchemyArchiveMaintenanceService(
            config,
            archive_stores,
        ),
        archive_copies=SqlAlchemyArchiveCopyService(
            config,
            archive_stores,
            archive_ingress_stores,
        ),
        proof_maturations=SqlAlchemyProofMaturationService(
            config,
            archive_stores,
            proof_upgrader=proof_upgrader,
            proof_verifier=proof_verifier,
        ),
        archive_attestations=SqlAlchemyArchiveAttestationService(
            config,
            archive_stores,
            proof_stamper=proof_stamper,
            proof_upgrader=proof_upgrader,
            proof_verifier=proof_verifier,
        ),
        archive_copy_retirements=SqlAlchemyArchiveCopyRetirementService(
            config,
            archive_stores,
        ),
        archive_stores=SqlAlchemyArchiveStoreService(
            config,
            download_allowance=download_allowance,
        ),
        retrieval=SqlAlchemyRetrievalService(
            config,
            archive_stores,
            archive_ingress_stores,
            retrieval_cache,
            download_allowance=download_allowance,
        ),
        lifecycle_events=SqlAlchemyLifecycleEventService(config),
        download_quotas=download_allowance,
    )


def get_container() -> ServiceContainer:
    return default_container()


ContainerDep = Annotated[ServiceContainer, Depends(get_container)]
