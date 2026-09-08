"""Own durable mutable collection descriptions and their catalog projections."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Literal

from riverhog_protocol import (
    MAX_COLLECTION_DESCRIPTION_REVISION,
    CollectionDescriptionDocument,
    collection_description_identity,
    validate_collection_description,
)
from riverhog_protocol.errors import Conflict, NotFound, PreconditionFailed, ServiceUnavailable
from riverhog_protocol.paths import text_search_key
from sqlalchemy import select
from sqlalchemy.orm import Session
from time_formats import format_utc_timestamp, utc_now, utc_timestamp_now

from riverhog_core.app_permissions import (
    COLLECTION_DESCRIPTIONS_MANAGE,
    ApplicationPrincipal,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_events import (
    begin_catalog_event,
    publish_catalog_event,
    snapshot_catalog_event_collection_tags,
)
from riverhog_core.catalog_models import (
    ArchiveCopyRetirementRecord,
    CollectionArchiveCopyRecord,
    CollectionDeletionRecord,
    CollectionDescriptionPublicationRecord,
    CollectionRecord,
)
from riverhog_core.collection_access import require_collection_access
from riverhog_core.ports.archive_store import CollectionDescriptionReceipt
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_records import archive_copy_is_complete
from riverhog_core.services.collections import _normalize_collection_id_or_raise

_LOG = logging.getLogger(__name__)

DescriptionPublicationState = Literal["not_required", "current", "reconciling"]


class SqlAlchemyCollectionDescriptionService:
    """Publish one exact mutable sidecar before changing its catalog projection."""

    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._config = config
        self._archive_stores = archive_stores
        self._session_factory = session_factory or make_session_factory(config.database_url)

    def replace(
        self,
        collection_id: int,
        *,
        description: str | None,
        expected_identity: str,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        normalized_id = _normalize_collection_id_or_raise(collection_id)
        normalized_description = (
            validate_collection_description(description) if description is not None else None
        )
        with session_scope(self._session_factory) as session:
            require_collection_access(
                session,
                principal,
                COLLECTION_DESCRIPTIONS_MANAGE,
                normalized_id,
            )
            collection = session.scalar(
                select(CollectionRecord)
                .where(
                    CollectionRecord.id == normalized_id,
                    CollectionRecord.is_published.is_(True),
                )
                .with_for_update()
            )
            if collection is None:  # pragma: no cover - access check owns this result
                raise NotFound(f"collection not found: {normalized_id}")
            if session.get(CollectionDeletionRecord, normalized_id) is not None:
                raise Conflict("collection deletion is active")
            if (
                collection.description_mutation_state == "idle"
                and collection.description == normalized_description
            ):
                return _description_payload(session, collection)
            if collection.description_identity != expected_identity:
                raise PreconditionFailed("collection description identity changed")
            if collection.description_mutation_state != "idle":
                if collection.pending_description != normalized_description:
                    raise Conflict("a different collection description replacement is active")
                if collection.description_mutation_state == "publishing":
                    raise Conflict("collection description replacement is active")
            else:
                revision = collection.description_revision + 1
                if revision > MAX_COLLECTION_DESCRIPTION_REVISION:
                    raise Conflict("collection description revision domain is exhausted")
                if collection.archive_root_sha256 is None:
                    raise Conflict("collection archive authority is unavailable")
                collection.pending_description = normalized_description
                collection.pending_description_revision = revision
                collection.pending_description_identity = collection_description_identity(
                    archive_root_sha256=collection.archive_root_sha256,
                    revision=revision,
                    description=normalized_description,
                )
                collection.description_mutation_state = "pending"
                collection.description_attempt_count = 0
                collection.description_next_attempt_at = utc_timestamp_now()
                collection.description_last_attempt_at = None
                collection.description_failure = None

        try:
            self._publish_mutation(normalized_id)
        except Exception as exc:
            self._record_mutation_failure(normalized_id, exc)
            if isinstance(exc, (Conflict, NotFound, PreconditionFailed, ServiceUnavailable)):
                raise
            raise ServiceUnavailable("collection description publication failed") from exc
        with session_scope(self._session_factory) as session:
            collection = session.get(CollectionRecord, normalized_id)
            if collection is None:
                raise NotFound(f"collection not found: {normalized_id}")
            return _description_payload(session, collection)

    def requeue_interrupted_for_startup(self, *, limit: int = 100) -> int:
        if limit < 1:
            return 0
        now = utc_timestamp_now()
        changed = 0
        with session_scope(self._session_factory) as session:
            collections = list(
                session.scalars(
                    select(CollectionRecord)
                    .where(CollectionRecord.description_mutation_state == "publishing")
                    .order_by(CollectionRecord.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for collection in collections:
                collection.description_mutation_state = "pending"
                collection.description_next_attempt_at = now
                collection.description_failure = "publication interrupted before completion"
            changed += len(collections)
            remaining = limit - changed
            if remaining:
                publications = list(
                    session.scalars(
                        select(CollectionDescriptionPublicationRecord)
                        .where(CollectionDescriptionPublicationRecord.state == "publishing")
                        .order_by(
                            CollectionDescriptionPublicationRecord.collection_id,
                            CollectionDescriptionPublicationRecord.store,
                        )
                        .limit(remaining)
                        .with_for_update(skip_locked=True)
                    )
                )
                for publication in publications:
                    publication.state = "pending"
                    publication.next_attempt_at = now
                    publication.failure = "publication interrupted before completion"
                changed += len(publications)
        return changed

    def process_due(self, *, limit: int = 1) -> int:
        if limit < 1:
            return 0
        processed = 0
        for _ in range(limit):
            collection_id = self._next_due_mutation()
            if collection_id is not None:
                try:
                    self._publish_mutation(collection_id)
                except Exception as exc:  # background operation remains retryable
                    _LOG.exception(
                        "collection description publication failed: collection_id=%s",
                        collection_id,
                    )
                    self._record_mutation_failure(collection_id, exc)
                processed += 1
                continue
            publication = self._claim_due_replica()
            if publication is None:
                break
            try:
                self._publish_replica(*publication)
            except Exception as exc:  # background operation remains retryable
                _LOG.exception(
                    "collection description replica publication failed: collection_id=%s store=%s",
                    publication[0],
                    publication[1],
                )
                self._record_replica_failure(publication[0], publication[1], exc)
            processed += 1
        return processed

    def _next_due_mutation(self) -> int | None:
        now = utc_timestamp_now()
        with session_scope(self._session_factory) as session:
            return session.scalar(
                select(CollectionRecord.id)
                .where(
                    CollectionRecord.description_mutation_state.in_(("pending", "retry_wait")),
                    CollectionRecord.description_next_attempt_at <= now,
                )
                .order_by(CollectionRecord.description_next_attempt_at, CollectionRecord.id)
                .limit(1)
            )

    def _publish_mutation(self, collection_id: int) -> None:
        with session_scope(self._session_factory) as session:
            collection = session.scalar(
                select(CollectionRecord)
                .where(CollectionRecord.id == collection_id)
                .with_for_update(skip_locked=True)
            )
            if collection is None:
                return
            if collection.description_mutation_state not in {"pending", "retry_wait"}:
                if collection.description_mutation_state == "idle":
                    return
                raise ServiceUnavailable("collection description publication is already active")
            if session.get(CollectionDeletionRecord, collection_id) is not None:
                raise Conflict("collection deletion is active")
            if (
                collection.archive_root_sha256 is None
                or collection.pending_description_revision is None
                or collection.pending_description_identity is None
            ):
                raise RuntimeError("pending collection description authority is incomplete")
            candidate = _publication_candidate(session, collection_id)
            if candidate is None:
                raise ServiceUnavailable("no retained archive copy can accept the description")
            store, prefix = candidate
            revision = collection.pending_description_revision
            identity = collection.pending_description_identity
            description = collection.pending_description
            passphrase_id = collection.passphrase_id
            document = CollectionDescriptionDocument(
                archive_root_sha256=collection.archive_root_sha256,
                revision=revision,
                description=description,
                description_identity=identity,
            ).to_json_bytes()
            collection.description_mutation_state = "publishing"
            collection.description_attempt_count += 1
            collection.description_last_attempt_at = utc_timestamp_now()

        receipt = self._archive_stores.require(store).store.publish_collection_description(
            collection_id=collection_id,
            archive_storage_prefix=prefix,
            document=document,
            passphrase_id=passphrase_id,
        )
        now = utc_timestamp_now()
        with session_scope(self._session_factory) as session:
            collection = session.scalar(
                select(CollectionRecord)
                .where(CollectionRecord.id == collection_id)
                .with_for_update()
            )
            if collection is None:
                return
            if (
                collection.description_mutation_state != "publishing"
                or collection.pending_description_revision != revision
                or collection.pending_description_identity != identity
            ):
                raise Conflict("collection description replacement changed during publication")
            collection.description = description
            collection.description_search = text_search_key(description or "")
            collection.description_revision = revision
            collection.description_identity = identity
            collection.description_mutation_state = "idle"
            collection.pending_description = None
            collection.pending_description_revision = None
            collection.pending_description_identity = None
            collection.description_attempt_count = 0
            collection.description_next_attempt_at = None
            collection.description_last_attempt_at = None
            collection.description_failure = None
            _record_published_copy(
                session,
                collection=collection,
                store=store,
                receipt=receipt,
            )
            for copy in _retained_copies(session, collection_id):
                if copy.store != store:
                    _schedule_copy(session, collection=collection, copy=copy, now=now)
            event = begin_catalog_event(
                session,
                change="updated",
                collection_id=collection_id,
                occurred_at=now,
                inventory_identity=collection.inventory_identity,
            )
            snapshot_catalog_event_collection_tags(
                session,
                event=event,
                phase="before",
                collection_id=collection_id,
            )
            snapshot_catalog_event_collection_tags(
                session,
                event=event,
                phase="after",
                collection_id=collection_id,
            )
            publish_catalog_event(session, event=event)

    def _record_mutation_failure(self, collection_id: int, exc: Exception) -> None:
        with session_scope(self._session_factory) as session:
            collection = session.get(CollectionRecord, collection_id)
            if collection is None or collection.description_mutation_state == "idle":
                return
            delay = min(3600, 2 ** min(collection.description_attempt_count, 10))
            collection.description_mutation_state = "retry_wait"
            collection.description_next_attempt_at = format_utc_timestamp(
                utc_now() + timedelta(seconds=delay)
            )
            collection.description_failure = f"{type(exc).__name__}: {exc}"[:1000]

    def _claim_due_replica(self) -> tuple[int, str, int, str] | None:
        now = utc_timestamp_now()
        with session_scope(self._session_factory) as session:
            publication = session.scalar(
                select(CollectionDescriptionPublicationRecord)
                .where(
                    CollectionDescriptionPublicationRecord.state.in_(("pending", "retry_wait")),
                    CollectionDescriptionPublicationRecord.next_attempt_at <= now,
                )
                .order_by(
                    CollectionDescriptionPublicationRecord.next_attempt_at,
                    CollectionDescriptionPublicationRecord.collection_id,
                    CollectionDescriptionPublicationRecord.store,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if publication is None:
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
            return (
                publication.collection_id,
                publication.store,
                publication.desired_revision,
                publication.desired_identity,
            )

    def _publish_replica(
        self,
        collection_id: int,
        store: str,
        revision: int,
        identity: str,
    ) -> None:
        with session_scope(self._session_factory) as session:
            collection = session.get(CollectionRecord, collection_id)
            copy = session.get(CollectionArchiveCopyRecord, (collection_id, store))
            publication = session.get(
                CollectionDescriptionPublicationRecord,
                (collection_id, store),
            )
            if collection is None or copy is None or publication is None:
                return
            if not archive_copy_is_complete(copy) or copy.archive_storage_prefix is None:
                raise Conflict("description destination archive copy is incomplete")
            if (
                publication.state != "publishing"
                or publication.desired_revision != revision
                or publication.desired_identity != identity
                or collection.description_revision != revision
                or collection.description_identity != identity
            ):
                raise Conflict("description replica authority changed before publication")
            if revision == 0:
                receipt = None
            else:
                document = CollectionDescriptionDocument(
                    archive_root_sha256=str(collection.archive_root_sha256),
                    revision=revision,
                    description=collection.description,
                    description_identity=identity,
                ).to_json_bytes()
                prefix = copy.archive_storage_prefix
                passphrase_id = collection.passphrase_id
        if revision > 0:
            receipt = self._archive_stores.require(store).store.publish_collection_description(
                collection_id=collection_id,
                archive_storage_prefix=prefix,
                document=document,
                passphrase_id=passphrase_id,
            )
        with session_scope(self._session_factory) as session:
            publication = session.get(
                CollectionDescriptionPublicationRecord,
                (collection_id, store),
            )
            if publication is None:
                return
            publication.published_revision = revision
            publication.published_identity = identity
            if receipt is not None:
                publication.object_path = receipt.object_path
                publication.provider_revision = receipt.revision
                publication.stored_bytes = receipt.stored_bytes
                publication.stored_sha256 = receipt.stored_sha256
                publication.published_at = receipt.published_at
            publication.failure = None
            publication.attempt_count = 0
            if (
                publication.desired_revision == revision
                and publication.desired_identity == identity
            ):
                publication.state = "published"
                publication.next_attempt_at = None
            else:
                publication.state = "pending"
                publication.next_attempt_at = utc_timestamp_now()

    def _record_replica_failure(self, collection_id: int, store: str, exc: Exception) -> None:
        with session_scope(self._session_factory) as session:
            publication = session.get(
                CollectionDescriptionPublicationRecord,
                (collection_id, store),
            )
            if publication is None:
                return
            delay = min(3600, 2 ** min(publication.attempt_count, 10))
            publication.state = "retry_wait"
            publication.next_attempt_at = format_utc_timestamp(utc_now() + timedelta(seconds=delay))
            publication.failure = f"{type(exc).__name__}: {exc}"[:1000]


def ensure_description_publication_for_copy(
    session: Session,
    *,
    collection: CollectionRecord,
    copy: CollectionArchiveCopyRecord,
    now: str,
) -> CollectionDescriptionPublicationRecord:
    """Create or reconcile the desired description state for one complete copy."""

    publication = session.get(
        CollectionDescriptionPublicationRecord,
        (collection.id, copy.store),
    )
    if publication is None:
        publication = CollectionDescriptionPublicationRecord(
            collection_id=collection.id,
            store=copy.store,
            desired_revision=collection.description_revision,
            desired_identity=collection.description_identity,
            published_revision=0,
            published_identity=collection_description_identity(
                archive_root_sha256=str(collection.archive_root_sha256),
                revision=0,
                description=None,
            ),
            state="published" if collection.description_revision == 0 else "pending",
            next_attempt_at=None if collection.description_revision == 0 else now,
        )
        session.add(publication)
        return publication
    if (
        publication.desired_revision != collection.description_revision
        or publication.desired_identity != collection.description_identity
    ):
        publication.desired_revision = collection.description_revision
        publication.desired_identity = collection.description_identity
        if publication.state != "publishing":
            publication.state = "pending"
            publication.next_attempt_at = now
    return publication


def description_publication_state(
    session: Session,
    collection: CollectionRecord,
) -> DescriptionPublicationState:
    if collection.description_revision == 0:
        return "not_required"
    copies = _retained_copies(session, collection.id)
    if not copies:
        return "reconciling"
    current = 0
    for copy in copies:
        publication = session.get(
            CollectionDescriptionPublicationRecord,
            (collection.id, copy.store),
        )
        if (
            publication is not None
            and publication.state == "published"
            and publication.published_revision == collection.description_revision
            and publication.published_identity == collection.description_identity
        ):
            current += 1
    return "current" if current == len(copies) else "reconciling"


def _publication_candidate(session: Session, collection_id: int) -> tuple[str, str] | None:
    for copy in _retained_copies(session, collection_id):
        publication = session.get(
            CollectionDescriptionPublicationRecord,
            (collection_id, copy.store),
        )
        if publication is None or publication.state != "publishing":
            if copy.archive_storage_prefix is None:  # pragma: no cover - completeness owns this
                continue
            return copy.store, copy.archive_storage_prefix
    return None


def _retained_copies(session: Session, collection_id: int) -> list[CollectionArchiveCopyRecord]:
    return list(
        session.scalars(
            select(CollectionArchiveCopyRecord)
            .where(
                CollectionArchiveCopyRecord.collection_id == collection_id,
                CollectionArchiveCopyRecord.state == "uploaded",
                ~select(ArchiveCopyRetirementRecord.collection_id)
                .where(
                    ArchiveCopyRetirementRecord.collection_id == collection_id,
                    ArchiveCopyRetirementRecord.store == CollectionArchiveCopyRecord.store,
                )
                .exists(),
            )
            .order_by(CollectionArchiveCopyRecord.store)
        )
    )


def _schedule_copy(
    session: Session,
    *,
    collection: CollectionRecord,
    copy: CollectionArchiveCopyRecord,
    now: str,
) -> None:
    ensure_description_publication_for_copy(
        session,
        collection=collection,
        copy=copy,
        now=now,
    )


def _record_published_copy(
    session: Session,
    *,
    collection: CollectionRecord,
    store: str,
    receipt: CollectionDescriptionReceipt,
) -> None:
    publication = session.get(
        CollectionDescriptionPublicationRecord,
        (collection.id, store),
    )
    values = {
        "object_path": receipt.object_path,
        "provider_revision": receipt.revision,
        "stored_bytes": receipt.stored_bytes,
        "stored_sha256": receipt.stored_sha256,
        "published_at": receipt.published_at,
    }
    if publication is None:
        publication = CollectionDescriptionPublicationRecord(
            collection_id=collection.id,
            store=store,
            desired_revision=collection.description_revision,
            desired_identity=collection.description_identity,
            published_revision=collection.description_revision,
            published_identity=collection.description_identity,
            state="published",
            attempt_count=0,
            next_attempt_at=None,
            **values,
        )
        session.add(publication)
        return
    publication.desired_revision = collection.description_revision
    publication.desired_identity = collection.description_identity
    publication.published_revision = collection.description_revision
    publication.published_identity = collection.description_identity
    publication.state = "published"
    publication.attempt_count = 0
    publication.next_attempt_at = None
    publication.last_attempt_at = None
    publication.failure = None
    for key, value in values.items():
        setattr(publication, key, value)


def _description_payload(session: Session, collection: CollectionRecord) -> dict[str, object]:
    return {
        "collection_id": collection.id,
        "description": collection.description,
        "description_revision": collection.description_revision,
        "description_identity": collection.description_identity,
        "description_publication": description_publication_state(session, collection),
    }


__all__ = [
    "DescriptionPublicationState",
    "SqlAlchemyCollectionDescriptionService",
    "description_publication_state",
    "ensure_description_publication_for_copy",
]
