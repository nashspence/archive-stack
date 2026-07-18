from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db
from riverhog_core.proofs import CommandProofStamper, CommandProofVerifier
from riverhog_core.runtime_config import load_runtime_config
from riverhog_core.services.archive_copies import SqlAlchemyArchiveCopyService
from riverhog_core.services.archive_copy_retirements import (
    SqlAlchemyArchiveCopyRetirementService,
)
from riverhog_core.services.archive_reporting import SqlAlchemyArchiveReportingService
from riverhog_core.services.archive_uploads import SqlAlchemyArchiveUploadService
from riverhog_core.services.collection_deletions import SqlAlchemyCollectionDeletionService
from riverhog_core.services.collections import SqlAlchemyCollectionService
from riverhog_core.services.interfaces import (
    ArchiveCopyRetirementService,
    ArchiveCopyService,
    ArchiveReportingService,
    ArchiveUploadService,
    CollectionDeletionService,
    CollectionService,
    RetrievalService,
    SearchService,
)
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_core.services.search import SqlAlchemySearchService
from riverhog_core.stores.s3_archive_store import S3ArchiveStore
from riverhog_core.stores.s3_retrieval_cache import S3RetrievalCache
from riverhog_core.stores.s3_support import ensure_bucket_exists
from riverhog_core.stores.tusd_upload_store import TusdUploadStore


@dataclass(slots=True)
class ServiceContainer:
    collections: CollectionService
    collection_deletions: CollectionDeletionService
    search: SearchService
    archive_uploads: ArchiveUploadService
    archive_copies: ArchiveCopyService
    archive_copy_retirements: ArchiveCopyRetirementService
    archive_reporting: ArchiveReportingService
    retrieval: RetrievalService


@lru_cache(maxsize=1)
def default_container() -> ServiceContainer:
    config = load_runtime_config()
    initialize_db(config.database_url)
    ensure_bucket_exists(config)
    retrieval_cache = S3RetrievalCache(config) if config.retrieval_cache is not None else None
    archive_stores = ArchiveStoreRegistry(
        {
            name: S3ArchiveStore(config, store, retrieval_cache=retrieval_cache)
            for name, store in config.archive_stores.items()
        }
    )
    upload_store = TusdUploadStore(config)
    proof_stamper = CommandProofStamper(config.ots_stamp_command)
    proof_verifier = CommandProofVerifier(config.ots_verify_command)
    return ServiceContainer(
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
        archive_copy_retirements=SqlAlchemyArchiveCopyRetirementService(
            config,
            archive_stores,
        ),
        archive_reporting=SqlAlchemyArchiveReportingService(config),
        retrieval=SqlAlchemyRetrievalService(
            config,
            archive_stores,
            retrieval_cache,
            proof_verifier=proof_verifier,
        ),
    )


def get_container() -> ServiceContainer:
    return default_container()


ContainerDep = Annotated[ServiceContainer, Depends(get_container)]
