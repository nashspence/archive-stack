from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from riverhog_core.catalog_db import initialize_db
from riverhog_core.proofs import CommandProofStamper, CommandProofVerifier
from riverhog_core.recovery_payloads import CommandAgeBatchpassRecoveryPayloadCodec
from riverhog_core.runtime_config import load_runtime_config
from riverhog_core.services.archive_reporting import SqlAlchemyArchiveReportingService
from riverhog_core.services.archive_restores import SqlAlchemyArchiveRestoreService
from riverhog_core.services.archive_uploads import SqlAlchemyArchiveUploadService
from riverhog_core.services.collections import SqlAlchemyCollectionService
from riverhog_core.services.contracts import (
    ArchiveReportingService,
    ArchiveRestoreService,
    ArchiveUploadService,
    CollectionService,
    DiscService,
    FetchService,
    FileService,
    PlanningService,
    SearchService,
)
from riverhog_core.services.discs import SqlAlchemyDiscService
from riverhog_core.services.fetches import SqlAlchemyFetchService
from riverhog_core.services.files import SqlAlchemyFileService
from riverhog_core.services.planning import SqlAlchemyPlanningService
from riverhog_core.services.search import SqlAlchemySearchService
from riverhog_core.stores.s3_archive_store import S3ArchiveStore
from riverhog_core.stores.s3_hot_store import S3HotStore
from riverhog_core.stores.s3_support import ensure_bucket_exists
from riverhog_core.stores.tusd_upload_store import TusdUploadStore


@dataclass(slots=True)
class ServiceContainer:
    collections: CollectionService
    search: SearchService
    planning: PlanningService
    archive_uploads: ArchiveUploadService
    archive_reporting: ArchiveReportingService
    archive_restores: ArchiveRestoreService
    discs: DiscService
    fetches: FetchService
    files: FileService


@lru_cache(maxsize=1)
def default_container() -> ServiceContainer:
    config = load_runtime_config()
    initialize_db(config.database_url)
    ensure_bucket_exists(config)
    hot_store = S3HotStore(config)
    archive_store = S3ArchiveStore(config)
    upload_store = TusdUploadStore(config)
    proof_stamper = CommandProofStamper(config.ots_stamp_command)
    proof_verifier = CommandProofVerifier(config.ots_verify_command)
    recovery_payload_codec = CommandAgeBatchpassRecoveryPayloadCodec(
        command=config.recovery_payload_command,
        passphrase=config.recovery_payload_passphrase,
        work_factor=config.recovery_payload_work_factor,
        max_work_factor=config.recovery_payload_max_work_factor,
    )
    return ServiceContainer(
        collections=SqlAlchemyCollectionService(config, hot_store, upload_store),
        search=SqlAlchemySearchService(config),
        planning=SqlAlchemyPlanningService(
            config,
            hot_store,
            archive_store,
            recovery_payload_codec,
        ),
        archive_uploads=SqlAlchemyArchiveUploadService(
            config,
            archive_store,
            hot_store,
            upload_store,
            proof_stamper=proof_stamper,
            recovery_payload_codec=recovery_payload_codec,
        ),
        archive_reporting=SqlAlchemyArchiveReportingService(config),
        archive_restores=SqlAlchemyArchiveRestoreService(
            config,
            archive_store,
            hot_store,
            proof_verifier=proof_verifier,
            recovery_payload_codec=recovery_payload_codec,
        ),
        discs=SqlAlchemyDiscService(config, hot_store, recovery_payload_codec),
        fetches=SqlAlchemyFetchService(config, hot_store, upload_store, recovery_payload_codec),
        files=SqlAlchemyFileService(config, hot_store),
    )


def get_container() -> ServiceContainer:
    return default_container()


ContainerDep = Annotated[ServiceContainer, Depends(get_container)]
