from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
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
from riverhog_core.services.archive_stores import SqlAlchemyArchiveStoreService
from riverhog_core.services.archive_uploads import SqlAlchemyArchiveUploadService
from riverhog_core.services.collection_deletions import SqlAlchemyCollectionDeletionService
from riverhog_core.services.collections import SqlAlchemyCollectionService
from riverhog_core.services.download_allowances import SqlAlchemyDownloadAllowance
from riverhog_core.services.interfaces import (
    AppKeyService,
    ArchiveAttestationService,
    ArchiveCopyRetirementService,
    ArchiveCopyService,
    ArchiveStoreService,
    ArchiveUploadService,
    CollectionDeletionService,
    CollectionService,
    LifecycleEventService,
    ProofMaturationService,
    RetrievalService,
    SearchService,
    TagService,
)
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService
from riverhog_core.services.proof_maturations import SqlAlchemyProofMaturationService
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_core.services.search import SqlAlchemySearchService
from riverhog_core.services.tags import SqlAlchemyTagService
from riverhog_core.stores.s3_archive_store import S3ArchiveStore
from riverhog_core.stores.s3_retrieval_cache import S3RetrievalCache
from riverhog_core.stores.s3_support import ensure_bucket_exists
from riverhog_core.stores.tusd_upload_store import TusdUploadStore


@dataclass(slots=True)
class ServiceContainer:
    app_keys: AppKeyService
    collection_access: SqlAlchemyCollectionAccessService
    tags: TagService
    collections: CollectionService
    collection_deletions: CollectionDeletionService
    search: SearchService
    archive_uploads: ArchiveUploadService
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
    upload_store = TusdUploadStore(config)
    proof_stamper = CommandProofStamper(config.ots_stamp_command)
    proof_verifier = CommandProofVerifier(config.ots_verify_command)
    proof_upgrader = CommandProofUpgrader(config.ots_upgrade_command)
    return ServiceContainer(
        app_keys=SqlAlchemyAppKeyService(config),
        collection_access=SqlAlchemyCollectionAccessService(config),
        tags=SqlAlchemyTagService(config),
        collections=SqlAlchemyCollectionService(config, upload_store),
        collection_deletions=SqlAlchemyCollectionDeletionService(
            config,
            archive_stores,
            upload_store,
            retrieval_cache,
        ),
        search=SqlAlchemySearchService(config),
        archive_uploads=SqlAlchemyArchiveUploadService(
            config,
            archive_stores,
            upload_store,
            proof_stamper=proof_stamper,
        ),
        archive_copies=SqlAlchemyArchiveCopyService(
            config,
            archive_stores,
            proof_verifier=proof_verifier,
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
            retrieval_cache,
            download_allowance=download_allowance,
            proof_verifier=proof_verifier,
        ),
        lifecycle_events=SqlAlchemyLifecycleEventService(config),
        download_quotas=download_allowance,
    )


def get_container() -> ServiceContainer:
    return default_container()


ContainerDep = Annotated[ServiceContainer, Depends(get_container)]
