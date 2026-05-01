from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends

from arc_core.proofs import CommandProofStamper
from arc_core.recovery_payloads import CommandAgeBatchpassRecoveryPayloadCodec
from arc_core.runtime_config import load_runtime_config
from arc_core.services.collections import SqlAlchemyCollectionService
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
from arc_core.services.copies import SqlAlchemyCopyService
from arc_core.services.fetches import SqlAlchemyFetchService
from arc_core.services.files import SqlAlchemyFileService
from arc_core.services.glacier_reporting import SqlAlchemyGlacierReportingService
from arc_core.services.glacier_uploads import SqlAlchemyGlacierUploadService
from arc_core.services.pins import SqlAlchemyPinService
from arc_core.services.planning import SqlAlchemyPlanningService
from arc_core.services.recovery_sessions import SqlAlchemyRecoverySessionService
from arc_core.services.search import SqlAlchemySearchService
from arc_core.sqlite_db import initialize_db
from arc_core.stores.s3_archive_store import S3ArchiveStore
from arc_core.stores.s3_hot_store import S3HotStore
from arc_core.stores.s3_support import ensure_bucket_exists
from arc_core.stores.tusd_upload_store import TusdUploadStore


@dataclass(slots=True)
class ServiceContainer:
    collections: CollectionService
    search: SearchService
    planning: PlanningService
    glacier_uploads: GlacierUploadService
    glacier_reporting: GlacierReportingService
    recovery_sessions: RecoverySessionService
    copies: CopyService
    pins: PinService
    fetches: FetchService
    files: FileService


def default_container() -> ServiceContainer:
    config = load_runtime_config()
    initialize_db(str(config.sqlite_path))
    ensure_bucket_exists(config)
    hot_store = S3HotStore(config)
    archive_store = S3ArchiveStore(config)
    upload_store = TusdUploadStore(config)
    proof_stamper = CommandProofStamper(config.ots_stamp_command)
    recovery_payload_codec = CommandAgeBatchpassRecoveryPayloadCodec(
        command=config.recovery_payload_command,
        passphrase=config.recovery_payload_passphrase,
        work_factor=config.recovery_payload_work_factor,
        max_work_factor=config.recovery_payload_max_work_factor,
    )
    return ServiceContainer(
        collections=SqlAlchemyCollectionService(config, hot_store, upload_store),
        search=SqlAlchemySearchService(config),
        planning=SqlAlchemyPlanningService(config, recovery_payload_codec),
        glacier_uploads=SqlAlchemyGlacierUploadService(
            config,
            archive_store,
            hot_store,
            upload_store,
            proof_stamper=proof_stamper,
        ),
        glacier_reporting=SqlAlchemyGlacierReportingService(config),
        recovery_sessions=SqlAlchemyRecoverySessionService(
            config,
            archive_store,
            hot_store,
            proof_stamper=proof_stamper,
            recovery_payload_codec=recovery_payload_codec,
        ),
        copies=SqlAlchemyCopyService(config, hot_store, recovery_payload_codec),
        pins=SqlAlchemyPinService(config, hot_store, upload_store),
        fetches=SqlAlchemyFetchService(config, hot_store, upload_store, recovery_payload_codec),
        files=SqlAlchemyFileService(config, hot_store),
    )


def get_container() -> ServiceContainer:
    return default_container()


ContainerDep = Annotated[ServiceContainer, Depends(get_container)]
