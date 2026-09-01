from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select, update
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyRetirementRecord,
    CollectionArchiveCopyRecord,
    CollectionDeletionRecord,
    CollectionMetadataPublicationRecord,
    CollectionRecord,
    CollectionTagRecord,
)
from riverhog_core.collection_metadata import collection_metadata_manifest
from riverhog_core.runtime_config import RuntimeConfig

_LOG = logging.getLogger(__name__)


class SqlAlchemyArchiveMaintenanceService:
    """Maintain mutable metadata beside already-finalized immutable archives."""

    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._archive_stores = archive_stores
        self._session_factory = session_factory or make_session_factory(config.database_url)

    def requeue_interrupted_metadata_publications_for_startup(self) -> int:
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            result = session.execute(
                update(CollectionMetadataPublicationRecord)
                .where(CollectionMetadataPublicationRecord.state == "publishing")
                .values(
                    state="pending",
                    next_attempt_at=now,
                    failure="publication interrupted before completion",
                )
            )
            return int(getattr(result, "rowcount", 0) or 0)

    def process_due_metadata_publications(self, *, limit: int = 10) -> int:
        if limit < 1:
            return 0
        processed = 0
        for _ in range(limit):
            claimed = self._claim_due_metadata_publication()
            if claimed is None:
                break
            self._publish_collection_metadata(*claimed)
            processed += 1
        return processed

    def _claim_due_metadata_publication(self) -> tuple[int, str, int] | None:
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            candidate = session.execute(
                select(
                    CollectionMetadataPublicationRecord.collection_id,
                    CollectionMetadataPublicationRecord.store,
                )
                .where(
                    CollectionMetadataPublicationRecord.state.in_(("pending", "retry_wait")),
                    CollectionMetadataPublicationRecord.next_attempt_at <= now,
                )
                .order_by(
                    CollectionMetadataPublicationRecord.next_attempt_at,
                    CollectionMetadataPublicationRecord.collection_id,
                    CollectionMetadataPublicationRecord.store,
                )
                .limit(1)
            ).one_or_none()
            if candidate is None:
                return None
            collection = session.scalar(
                select(CollectionRecord)
                .where(CollectionRecord.id == candidate.collection_id)
                .with_for_update(skip_locked=True)
            )
            publication = session.scalar(
                select(CollectionMetadataPublicationRecord)
                .where(
                    CollectionMetadataPublicationRecord.collection_id == candidate.collection_id,
                    CollectionMetadataPublicationRecord.store == candidate.store,
                )
                .with_for_update()
            )
            if collection is None or publication is None:
                return None
            if session.get(CollectionDeletionRecord, publication.collection_id) is not None:
                return None
            if (
                session.get(
                    ArchiveCopyRetirementRecord,
                    (publication.collection_id, publication.store),
                )
                is not None
            ):
                return None
            publication.state = "publishing"
            publication.attempt_count += 1
            publication.last_attempt_at = now
            return publication.collection_id, publication.store, publication.desired_revision

    def _publish_collection_metadata(
        self,
        collection_id: int,
        store: str,
        revision: int,
    ) -> None:
        try:
            with session_scope(self._session_factory) as session:
                collection = session.get(CollectionRecord, collection_id)
                copy = session.get(CollectionArchiveCopyRecord, (collection_id, store))
                publication = session.get(
                    CollectionMetadataPublicationRecord,
                    (collection_id, store),
                )
                if collection is None or copy is None or publication is None:
                    return
                if collection.metadata_revision != revision:
                    publication.state = "pending"
                    publication.next_attempt_at = format_utc_timestamp(utc_now())
                    return
                if copy.archive_storage_prefix is None:
                    raise RuntimeError("collection archive has no storage prefix")
                tags = tuple(
                    session.scalars(
                        select(CollectionTagRecord.tag_id)
                        .where(CollectionTagRecord.collection_id == collection_id)
                        .order_by(CollectionTagRecord.tag_id)
                    )
                )
                manifest = collection_metadata_manifest(
                    collection_id=collection_id,
                    content_identity=collection.content_identity,
                    encryption_format=collection.encryption_format,
                    passphrase_id=collection.passphrase_id,
                    inventory_identity=collection.inventory_identity,
                    metadata_revision=collection.metadata_revision,
                    tags=tags,
                    updated_at=collection.metadata_updated_at,
                )
                prefix = copy.archive_storage_prefix
                passphrase_id = collection.passphrase_id

            receipt = self._archive_stores.require(store).store.publish_collection_metadata(
                collection_id=collection_id,
                archive_storage_prefix=prefix,
                manifest=manifest,
                passphrase_id=passphrase_id,
            )
            with session_scope(self._session_factory) as session:
                publication = session.get(
                    CollectionMetadataPublicationRecord,
                    (collection_id, store),
                )
                if publication is None:
                    return
                publication.published_revision = revision
                publication.object_path = receipt.object_path
                publication.revision = receipt.revision
                publication.stored_bytes = receipt.stored_bytes
                publication.stored_sha256 = receipt.stored_sha256
                publication.published_at = receipt.published_at
                publication.failure = None
                if publication.desired_revision == revision:
                    publication.state = "published"
                else:
                    publication.state = "pending"
                    publication.next_attempt_at = format_utc_timestamp(utc_now())
        except Exception as exc:
            _LOG.exception(
                "collection metadata publication failed: collection_id=%s store=%s",
                collection_id,
                store,
            )
            with session_scope(self._session_factory) as session:
                publication = session.get(
                    CollectionMetadataPublicationRecord,
                    (collection_id, store),
                )
                if publication is None:
                    return
                delay = min(3600, 2 ** min(publication.attempt_count, 10))
                publication.state = "retry_wait"
                publication.next_attempt_at = format_utc_timestamp(
                    utc_now() + timedelta(seconds=delay)
                )
                publication.failure = str(exc)[:1000]
