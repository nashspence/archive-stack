"""Own recoverable collection tags, projections, and copy reconciliation."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from riverhog_protocol import (
    COLLECTION_TAG_PAGE_UTF8_BYTES_MAX,
    MAX_COLLECTION_TAG_REVISION,
    CollectionTagHeadDocument,
    CollectionTagNodeMissing,
    CollectionTagNodeStore,
    CollectionTagSet,
    CollectionTagSetRoot,
    collection_tag_node_digest,
    collection_tag_sha256,
    decode_collection_tag_node,
    validate_collection_tag,
)
from riverhog_protocol.errors import Conflict, NotFound, PreconditionFailed, ServiceUnavailable
from riverhog_protocol.paths import text_search_key
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import Select
from time_formats import format_utc_timestamp, utc_now, utc_timestamp_now

from riverhog_core.app_permissions import (
    CATALOG_READ,
    COLLECTION_TAGS_MANAGE,
    ApplicationPrincipal,
    tag_resource,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.browse import bounded_page, keyset_statement, validate_page_size
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
    CollectionRecord,
    CollectionTagMembershipRecord,
    CollectionTagMutationRecord,
    CollectionTagNodeGcRecord,
    CollectionTagNodeRecord,
    CollectionTagPublicationFrontierRecord,
    CollectionTagPublicationRecord,
    CollectionTagPublishedNodeRecord,
    CollectionTagRecord,
    CollectionTagRevisionRecord,
)
from riverhog_core.collection_access import collection_access_filter, require_collection_access
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_records import archive_copy_is_complete
from riverhog_core.services.collections import _normalize_collection_id_or_raise


def build_collection_tag_set(
    session: Session, tags: Iterable[str]
) -> tuple[CollectionTagSet, set[str]]:
    """Build one canonical set from bounded staging rows inside the caller transaction."""

    store = _DatabaseTagNodeStore(session)
    result = CollectionTagSet(store)
    for tag in sorted(set(tags), key=lambda value: value.encode("utf-8")):
        result = result.insert(tag)
    return result, store.put_digests


def advance_collection_tag_set(
    session: Session,
    *,
    root_sha256: str | None,
    tags: Iterable[str],
) -> CollectionTagSet:
    """Apply one caller-bounded tag batch to an existing authenticated set."""

    store = _DatabaseTagNodeStore(session)
    result = CollectionTagSet(store, CollectionTagSetRoot.seal(root_sha256))
    for tag in tags:
        result = result.insert(tag)
    return result


class _DatabaseTagNodeStore(CollectionTagNodeStore):
    def __init__(self, session: Session) -> None:
        self._session = session
        self._pending: dict[str, bytes] = {}
        self.put_digests: set[str] = set()

    def get(self, digest: str) -> bytes:
        pending = self._pending.get(digest)
        if pending is not None:
            return pending
        record = self._session.get(CollectionTagNodeRecord, digest)
        if record is None:
            raise CollectionTagNodeMissing(digest)
        if collection_tag_node_digest(record.encoded) != digest:
            raise RuntimeError("catalog tag node digest differs")
        return record.encoded

    def put(self, digest: str, encoded: bytes) -> None:
        payload = bytes(encoded)
        if collection_tag_node_digest(payload) != digest:
            raise RuntimeError("catalog tag node digest differs")
        existing = self._pending.get(digest)
        if existing is None:
            record = self._session.get(CollectionTagNodeRecord, digest)
            existing = None if record is None else record.encoded
        if existing is not None and existing != payload:
            raise RuntimeError("immutable catalog tag node differs")
        if existing is None:
            self._session.add(
                CollectionTagNodeRecord(
                    digest=digest,
                    encoded=payload,
                    created_at=utc_timestamp_now(),
                )
            )
            self._pending[digest] = payload
            self.put_digests.add(digest)


class SqlAlchemyCollectionTagService:
    """Maintain one exact copy-on-write tag authority per collection."""

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

    def add(
        self,
        collection_id: int,
        *,
        tag: str,
        operation_id: str,
        expected_revision: int,
        expected_tag_set_identity: str,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        return self._mutate(
            collection_id,
            action="add",
            tag=tag,
            operation_id=operation_id,
            expected_revision=expected_revision,
            expected_tag_set_identity=expected_tag_set_identity,
            principal=principal,
        )

    def remove(
        self,
        collection_id: int,
        *,
        tag: str,
        operation_id: str,
        expected_revision: int,
        expected_tag_set_identity: str,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        return self._mutate(
            collection_id,
            action="remove",
            tag=tag,
            operation_id=operation_id,
            expected_revision=expected_revision,
            expected_tag_set_identity=expected_tag_set_identity,
            principal=principal,
        )

    def _mutate(
        self,
        collection_id: int,
        *,
        action: str,
        tag: str,
        operation_id: str,
        expected_revision: int,
        expected_tag_set_identity: str,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        normalized_id = _normalize_collection_id_or_raise(collection_id)
        canonical_tag = validate_collection_tag(tag)
        normalized_operation = _operation_id(operation_id)
        tag_digest = collection_tag_sha256(canonical_tag)
        if not principal.allows(COLLECTION_TAGS_MANAGE, tag_resource(canonical_tag)):
            raise NotFound(f"collection not found: {normalized_id}")
        with session_scope(self._session_factory) as session:
            prior = session.get(CollectionTagMutationRecord, (normalized_id, normalized_operation))
            if prior is not None:
                _require_same_mutation(
                    prior,
                    action=action,
                    tag=canonical_tag,
                    expected_revision=expected_revision,
                    expected_tag_set_identity=expected_tag_set_identity,
                )
                if prior.state == "succeeded":
                    return _mutation_payload(prior)
            collection = session.scalar(
                select(CollectionRecord)
                .where(
                    CollectionRecord.id == normalized_id,
                    CollectionRecord.is_published.is_(True),
                )
                .with_for_update()
            )
            if collection is None:
                raise NotFound(f"collection not found: {normalized_id}")
            require_collection_access(session, principal, COLLECTION_TAGS_MANAGE, normalized_id)
            if prior is not None:
                pass
            elif collection.tag_mutation_operation_id is not None:
                raise Conflict("a collection tag mutation is active")
            else:
                if session.get(CollectionDeletionRecord, normalized_id) is not None:
                    raise Conflict("collection deletion is active")
                if (
                    collection.tag_revision != expected_revision
                    or collection.tag_set_identity != expected_tag_set_identity
                ):
                    raise PreconditionFailed("collection tag authority changed")
                if collection.archive_root_sha256 is None:
                    raise Conflict("collection archive authority is unavailable")
                store = _DatabaseTagNodeStore(session)
                current = CollectionTagSet(
                    store,
                    CollectionTagSetRoot.seal(collection.tag_root_sha256),
                )
                changed = (
                    not current.contains(canonical_tag)
                    if action == "add"
                    else current.contains(canonical_tag)
                )
                if changed:
                    result = (
                        current.insert(canonical_tag)
                        if action == "add"
                        else current.discard(canonical_tag)
                    )
                    result_revision = expected_revision + 1
                    if result_revision > MAX_COLLECTION_TAG_REVISION:
                        raise Conflict("collection tag revision domain is exhausted")
                else:
                    result = current
                    result_revision = expected_revision
                head = CollectionTagHeadDocument.seal(
                    archive_root_sha256=collection.archive_root_sha256,
                    revision=result_revision,
                    root_sha256=result.root.root_sha256,
                )
                now = utc_timestamp_now()
                prior = CollectionTagMutationRecord(
                    collection_id=normalized_id,
                    operation_id=normalized_operation,
                    action=action,
                    tag=canonical_tag,
                    tag_sha256=tag_digest,
                    expected_revision=expected_revision,
                    expected_tag_set_identity=expected_tag_set_identity,
                    result_revision=result_revision,
                    result_root_sha256=result.root.root_sha256,
                    result_tag_set_identity=result.identity,
                    result_head_identity=head.head_identity,
                    changed=changed,
                    state="pending" if changed else "succeeded",
                    initiated_by_app=principal.app,
                    initiated_by_key_id=principal.key_id,
                    created_at=now,
                    updated_at=now,
                    failure=None,
                )
                session.add(prior)
                if changed:
                    collection.tag_mutation_operation_id = normalized_operation
                    publication = _current_publication_candidate(session, collection=collection)
                    if publication is None:
                        raise ServiceUnavailable(
                            "no current retained archive copy can accept collection tags"
                        )
                    _schedule_publication(
                        session,
                        publication=publication,
                        revision=result_revision,
                        tag_set_identity=result.identity,
                        head_identity=head.head_identity,
                        root_sha256=result.root.root_sha256,
                        already_expanded=store.put_digests,
                    )
                session.flush()
        if prior is None:  # pragma: no cover - established above
            raise RuntimeError("collection tag mutation was not established")
        if prior.changed:
            try:
                self._finish_mutation(normalized_id, normalized_operation)
            except Exception as exc:
                self._record_failure(normalized_id, normalized_operation, exc)
                if isinstance(exc, (Conflict, NotFound, PreconditionFailed, ServiceUnavailable)):
                    raise
                raise ServiceUnavailable("collection tag publication failed") from exc
        with session_scope(self._session_factory) as session:
            completed = session.get(
                CollectionTagMutationRecord, (normalized_id, normalized_operation)
            )
            if completed is None:
                raise RuntimeError("collection tag mutation disappeared")
            return _mutation_payload(completed)

    def _finish_mutation(self, collection_id: int, operation_id: str) -> None:
        for _ in range(128):
            with session_scope(self._session_factory) as session:
                mutation = session.get(CollectionTagMutationRecord, (collection_id, operation_id))
                if mutation is None or mutation.state == "succeeded":
                    return
                publication = session.scalar(
                    select(CollectionTagPublicationRecord).where(
                        CollectionTagPublicationRecord.collection_id == collection_id,
                        CollectionTagPublicationRecord.desired_head_identity
                        == mutation.result_head_identity,
                    )
                )
                if publication is None:
                    raise RuntimeError("collection tag publication is unavailable")
                store_name = publication.store
            self._process_publication_step(collection_id, store_name)
        raise RuntimeError("collection tag mutation exceeded fixed-depth path work")

    def requeue_interrupted_for_startup(self, *, limit: int = 100) -> int:
        now = utc_timestamp_now()
        with session_scope(self._session_factory) as session:
            rows = list(
                session.scalars(
                    select(CollectionTagPublicationRecord)
                    .where(
                        CollectionTagPublicationRecord.state.in_(
                            ("publishing_nodes", "publishing_head")
                        )
                    )
                    .order_by(
                        CollectionTagPublicationRecord.collection_id,
                        CollectionTagPublicationRecord.store,
                    )
                    .limit(max(0, limit))
                    .with_for_update(skip_locked=True)
                )
            )
            for publication_row in rows:
                publication_row.state = "pending"
                publication_row.next_attempt_at = now
            remaining = max(0, limit - len(rows))
            gc_rows = list(
                session.scalars(
                    select(CollectionTagNodeGcRecord)
                    .where(CollectionTagNodeGcRecord.state == "deleting")
                    .order_by(
                        CollectionTagNodeGcRecord.collection_id,
                        CollectionTagNodeGcRecord.store,
                        CollectionTagNodeGcRecord.node_digest,
                    )
                    .limit(remaining)
                    .with_for_update(skip_locked=True)
                )
            )
            for gc_row in gc_rows:
                gc_row.state = "pending"
                gc_row.next_attempt_at = now
            return len(rows) + len(gc_rows)

    def process_due(self, *, limit: int = 1) -> int:
        processed = 0
        for _ in range(max(0, limit)):
            if self._process_gc_step():
                processed += 1
                continue
            with session_scope(self._session_factory) as session:
                now = utc_timestamp_now()
                publication = session.scalar(
                    select(CollectionTagPublicationRecord)
                    .where(
                        CollectionTagPublicationRecord.state.in_(("pending", "retry_wait")),
                        CollectionTagPublicationRecord.next_attempt_at <= now,
                    )
                    .order_by(
                        CollectionTagPublicationRecord.next_attempt_at,
                        CollectionTagPublicationRecord.collection_id,
                        CollectionTagPublicationRecord.store,
                    )
                    .limit(1)
                )
                key = (
                    None if publication is None else (publication.collection_id, publication.store)
                )
            if key is None:
                break
            try:
                self._process_publication_step(*key)
            except Exception as exc:
                self._record_publication_failure(*key, exc)
            processed += 1
        return processed

    def _process_gc_step(self) -> bool:
        with session_scope(self._session_factory) as session:
            now = utc_timestamp_now()
            gc = session.scalar(
                select(CollectionTagNodeGcRecord)
                .where(
                    CollectionTagNodeGcRecord.state.in_(("pending", "retry_wait")),
                    CollectionTagNodeGcRecord.next_attempt_at <= now,
                )
                .order_by(
                    CollectionTagNodeGcRecord.next_attempt_at,
                    CollectionTagNodeGcRecord.collection_id,
                    CollectionTagNodeGcRecord.store,
                    CollectionTagNodeGcRecord.node_digest,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if gc is None:
                published = session.scalar(
                    select(CollectionTagPublishedNodeRecord)
                    .join(
                        CollectionTagPublicationRecord,
                        (
                            CollectionTagPublicationRecord.collection_id
                            == CollectionTagPublishedNodeRecord.collection_id
                        )
                        & (
                            CollectionTagPublicationRecord.store
                            == CollectionTagPublishedNodeRecord.store
                        ),
                    )
                    .where(
                        CollectionTagPublicationRecord.state == "published",
                        CollectionTagPublicationRecord.published_head_identity.is_not(None),
                        CollectionTagPublicationRecord.desired_head_identity
                        == CollectionTagPublicationRecord.published_head_identity,
                        ~exists(
                            select(1).where(
                                CollectionTagPublicationFrontierRecord.collection_id
                                == CollectionTagPublishedNodeRecord.collection_id,
                                CollectionTagPublicationFrontierRecord.store
                                == CollectionTagPublishedNodeRecord.store,
                                CollectionTagPublicationFrontierRecord.head_identity
                                == CollectionTagPublicationRecord.published_head_identity,
                                CollectionTagPublicationFrontierRecord.node_digest
                                == CollectionTagPublishedNodeRecord.node_digest,
                            )
                        ),
                    )
                    .order_by(
                        CollectionTagPublishedNodeRecord.collection_id,
                        CollectionTagPublishedNodeRecord.store,
                        CollectionTagPublishedNodeRecord.node_digest,
                    )
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if published is None:
                    return False
                publication = session.get(
                    CollectionTagPublicationRecord,
                    (published.collection_id, published.store),
                )
                if publication is None or publication.published_head_identity is None:
                    return False
                gc = CollectionTagNodeGcRecord(
                    collection_id=published.collection_id,
                    store=published.store,
                    node_digest=published.node_digest,
                    expected_head_identity=publication.published_head_identity,
                    object_path=published.object_path,
                    provider_revision=published.provider_revision,
                    state="pending",
                    next_attempt_at=now,
                    failure=None,
                )
                session.add(gc)
                session.flush()
            publication = session.get(
                CollectionTagPublicationRecord,
                (gc.collection_id, gc.store),
            )
            still_obsolete = (
                publication is not None
                and publication.state == "published"
                and publication.desired_head_identity == gc.expected_head_identity
                and publication.published_head_identity == gc.expected_head_identity
                and session.scalar(
                    select(
                        exists().where(
                            CollectionTagPublicationFrontierRecord.collection_id
                            == gc.collection_id,
                            CollectionTagPublicationFrontierRecord.store == gc.store,
                            CollectionTagPublicationFrontierRecord.head_identity
                            == gc.expected_head_identity,
                            CollectionTagPublicationFrontierRecord.node_digest == gc.node_digest,
                        )
                    )
                )
                is False
            )
            if not still_obsolete:
                session.delete(gc)
                return True
            copy = session.get(CollectionArchiveCopyRecord, (gc.collection_id, gc.store))
            if copy is None or copy.archive_storage_prefix is None:
                session.delete(gc)
                return True
            gc.state = "deleting"
            collection_id = gc.collection_id
            store_name = gc.store
            digest = gc.node_digest
            prefix = copy.archive_storage_prefix
        try:
            self._archive_stores.require(store_name).store.delete_collection_tag_node(
                collection_id=collection_id,
                archive_storage_prefix=prefix,
                digest=digest,
            )
        except Exception as exc:
            with session_scope(self._session_factory) as session:
                current = session.get(
                    CollectionTagNodeGcRecord, (collection_id, store_name, digest)
                )
                if current is not None:
                    current.state = "retry_wait"
                    current.next_attempt_at = format_utc_timestamp(utc_now() + timedelta(seconds=2))
                    current.failure = f"{type(exc).__name__}: {exc}"[:1000]
            return True
        with session_scope(self._session_factory) as session:
            current = session.get(CollectionTagNodeGcRecord, (collection_id, store_name, digest))
            if current is not None:
                published = session.get(
                    CollectionTagPublishedNodeRecord, (collection_id, store_name, digest)
                )
                if published is not None:
                    session.delete(published)
                session.delete(current)
        return True

    def _process_publication_step(self, collection_id: int, store_name: str) -> None:
        with session_scope(self._session_factory) as session:
            publication = session.scalar(
                select(CollectionTagPublicationRecord)
                .where(
                    CollectionTagPublicationRecord.collection_id == collection_id,
                    CollectionTagPublicationRecord.store == store_name,
                )
                .with_for_update(skip_locked=True)
            )
            if publication is None or publication.state == "published":
                return
            collection = session.get(CollectionRecord, collection_id)
            copy = session.get(CollectionArchiveCopyRecord, (collection_id, store_name))
            if (
                collection is None
                or copy is None
                or copy.archive_storage_prefix is None
                or not archive_copy_is_complete(copy)
            ):
                raise Conflict("collection tag destination archive copy is incomplete")
            if session.get(ArchiveCopyRetirementRecord, (collection_id, store_name)) is not None:
                raise Conflict("collection archive copy retirement is active")
            frontier = session.scalar(
                select(CollectionTagPublicationFrontierRecord)
                .where(
                    CollectionTagPublicationFrontierRecord.collection_id == collection_id,
                    CollectionTagPublicationFrontierRecord.store == store_name,
                    CollectionTagPublicationFrontierRecord.head_identity
                    == publication.desired_head_identity,
                    or_(
                        CollectionTagPublicationFrontierRecord.published.is_(False),
                        CollectionTagPublicationFrontierRecord.expanded.is_(False),
                    ),
                )
                .order_by(CollectionTagPublicationFrontierRecord.node_digest)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            prefix = copy.archive_storage_prefix
            passphrase_id = collection.passphrase_id
            head = CollectionTagHeadDocument.seal(
                archive_root_sha256=str(collection.archive_root_sha256),
                revision=publication.desired_revision,
                root_sha256=_revision_root(
                    session, collection_id, publication.desired_revision, publication
                ),
            )
            if head.head_identity != publication.desired_head_identity:
                raise RuntimeError("collection tag publication authority differs")
            if frontier is not None:
                gc = session.get(
                    CollectionTagNodeGcRecord,
                    (collection_id, store_name, frontier.node_digest),
                )
                if gc is not None and not frontier.published:
                    publication.state = "pending"
                    publication.next_attempt_at = gc.next_attempt_at
                    return
                node = session.get(CollectionTagNodeRecord, frontier.node_digest)
                if node is None:
                    raise RuntimeError("collection tag publication node is unavailable")
                digest = frontier.node_digest
                encoded = node.encoded
                decoded = decode_collection_tag_node(encoded)
                if frontier.published:
                    for child in decoded.children:
                        _add_frontier_node(
                            session,
                            publication=publication,
                            digest=child.digest,
                            expanded=False,
                        )
                    frontier.expanded = True
                    publication.state = "pending"
                    publication.next_attempt_at = utc_timestamp_now()
                    return
                publication.state = "publishing_nodes"
            else:
                digest = None
                encoded = None
                decoded = None
                publication.state = "publishing_head"
        store = self._archive_stores.require(store_name).store
        if digest is not None and encoded is not None and decoded is not None:
            receipt = store.publish_collection_tag_node(
                collection_id=collection_id,
                archive_storage_prefix=prefix,
                digest=digest,
                encoded=encoded,
                passphrase_id=passphrase_id,
            )
            with session_scope(self._session_factory) as session:
                publication = session.get(
                    CollectionTagPublicationRecord, (collection_id, store_name)
                )
                frontier = session.get(
                    CollectionTagPublicationFrontierRecord,
                    (collection_id, store_name, head.head_identity, digest),
                )
                if publication is None or frontier is None:
                    return
                session.merge(
                    CollectionTagPublishedNodeRecord(
                        collection_id=collection_id,
                        store=store_name,
                        node_digest=digest,
                        object_path=receipt.object_path,
                        provider_revision=receipt.revision,
                        stored_bytes=receipt.stored_bytes,
                        stored_sha256=receipt.stored_sha256,
                        published_at=receipt.published_at,
                    )
                )
                if not frontier.expanded:
                    for child in decoded.children:
                        _add_frontier_node(
                            session,
                            publication=publication,
                            digest=child.digest,
                            expanded=False,
                        )
                frontier.expanded = True
                frontier.published = True
                publication.state = "pending"
                publication.next_attempt_at = utc_timestamp_now()
                publication.failure = None
            return

        receipt = store.publish_collection_tag_head(
            collection_id=collection_id,
            archive_storage_prefix=prefix,
            document=head.to_json_bytes(),
            passphrase_id=passphrase_id,
        )
        with session_scope(self._session_factory) as session:
            publication = session.scalar(
                select(CollectionTagPublicationRecord)
                .where(
                    CollectionTagPublicationRecord.collection_id == collection_id,
                    CollectionTagPublicationRecord.store == store_name,
                )
                .with_for_update()
            )
            if publication is None:
                return
            if publication.desired_head_identity != head.head_identity:
                raise Conflict("collection tag publication changed")
            publication.published_revision = head.revision
            publication.published_tag_set_identity = head.tag_set_identity
            publication.published_head_identity = head.head_identity
            publication.state = "published"
            publication.next_attempt_at = None
            publication.failure = None
            publication.head_object_path = receipt.object_path
            publication.head_provider_revision = receipt.revision
            publication.head_stored_bytes = receipt.stored_bytes
            publication.head_stored_sha256 = receipt.stored_sha256
            publication.published_at = receipt.published_at
            mutation = session.scalar(
                select(CollectionTagMutationRecord)
                .where(
                    CollectionTagMutationRecord.collection_id == collection_id,
                    CollectionTagMutationRecord.result_head_identity == head.head_identity,
                    CollectionTagMutationRecord.state != "succeeded",
                )
                .with_for_update()
            )
            if mutation is not None:
                _settle_mutation(
                    session,
                    collection_id=collection_id,
                    mutation=mutation,
                    head=head,
                    publication=publication,
                )

    def _record_failure(self, collection_id: int, operation_id: str, exc: Exception) -> None:
        with session_scope(self._session_factory) as session:
            mutation = session.get(CollectionTagMutationRecord, (collection_id, operation_id))
            if mutation is not None and mutation.state != "succeeded":
                mutation.state = "retry_wait"
                mutation.failure = f"{type(exc).__name__}: {exc}"[:1000]
                mutation.updated_at = utc_timestamp_now()

    def _record_publication_failure(
        self, collection_id: int, store_name: str, exc: Exception
    ) -> None:
        with session_scope(self._session_factory) as session:
            publication = session.get(CollectionTagPublicationRecord, (collection_id, store_name))
            if publication is None or publication.state == "published":
                return
            publication.state = "retry_wait"
            publication.failure = f"{type(exc).__name__}: {exc}"[:1000]
            publication.next_attempt_at = format_utc_timestamp(utc_now() + timedelta(seconds=2))

    def list_collection(
        self,
        collection_id: int,
        *,
        page_size: int,
        position: tuple[str | int | bool | bytes | None, ...] | None,
        expected_revision: int,
        expected_tag_set_identity: str,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        normalized = _normalize_collection_id_or_raise(collection_id)
        validate_page_size(page_size)
        with session_scope(self._session_factory) as session:
            require_collection_access(session, principal, CATALOG_READ, normalized)
            revision = session.get(CollectionTagRevisionRecord, (normalized, expected_revision))
            if revision is None or revision.tag_set_identity != expected_tag_set_identity:
                raise PreconditionFailed("collection tag authority is unavailable")
            if position is not None and (len(position) != 1 or not isinstance(position[0], str)):
                raise ValueError("collection tag page position is invalid")
            tags = CollectionTagSet(
                _DatabaseTagNodeStore(session),
                CollectionTagSetRoot.seal(revision.root_sha256),
            )
            page: list[str] = []
            used = 0
            has_more = False
            start_after: str | None = None if position is None else str(position[0])
            for tag in tags.iter_tags(start_after=start_after):
                encoded_bytes = len(tag.encode("utf-8"))
                if page and (
                    len(page) >= page_size
                    or used + encoded_bytes > COLLECTION_TAG_PAGE_UTF8_BYTES_MAX
                ):
                    has_more = True
                    break
                page.append(tag)
                used += encoded_bytes
            next_position = (page[-1],) if page and has_more else None
            return {
                "collection_id": normalized,
                "revision": revision.revision,
                "tag_set_identity": revision.tag_set_identity,
                "tags": page,
                "_next_position": next_position,
            }

    def list_tags(
        self,
        *,
        page_size: int,
        position: tuple[str | int | bool | bytes | None, ...] | None,
        q: str | None,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        validate_page_size(page_size)
        query = text_search_key(q.strip()) if q is not None and q.strip() else None
        with session_scope(self._session_factory) as session:
            statement, key_columns = _tag_list_statement(query=query, principal=principal)
            statement = keyset_statement(
                statement,
                columns=key_columns,
                position=position,
                order="asc",
                page_size=page_size,
            )
            rows, next_position = bounded_page(
                list(session.execute(statement)),
                page_size=page_size,
                position_of=lambda row: (row.tag,),
            )
            return {
                "query": q,
                "tags": [
                    {"tag": str(row.tag), "collection_count": int(row.visible_count)}
                    for row in rows
                ],
                "_next_position": next_position,
            }

    def contains(
        self,
        collection_id: int,
        *,
        tag: str,
        revision: int,
        tag_set_identity: str,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        normalized = _normalize_collection_id_or_raise(collection_id)
        canonical = validate_collection_tag(tag)
        with session_scope(self._session_factory) as session:
            require_collection_access(session, principal, CATALOG_READ, normalized)
            authority = session.get(CollectionTagRevisionRecord, (normalized, revision))
            if authority is None or authority.tag_set_identity != tag_set_identity:
                raise PreconditionFailed("collection tag authority is unavailable")
            tags = CollectionTagSet(
                _DatabaseTagNodeStore(session),
                CollectionTagSetRoot.seal(authority.root_sha256),
            )
            present = tags.contains(canonical)
            return {
                "collection_id": normalized,
                "revision": authority.revision,
                "tag_set_identity": authority.tag_set_identity,
                "tag": canonical,
                "present": present,
            }


def _tag_list_statement(
    *,
    query: str | None,
    principal: ApplicationPrincipal | None,
) -> tuple[Select[Any], tuple[object, ...]]:
    """Return the bounded global-tag projection and its canonical key."""

    visible_count = func.count(CollectionTagMembershipRecord.collection_id).label("visible_count")
    statement = (
        select(CollectionTagRecord.tag, visible_count)
        .join(
            CollectionTagMembershipRecord,
            CollectionTagMembershipRecord.tag_sha256 == CollectionTagRecord.tag_sha256,
        )
        .where(
            collection_access_filter(
                CollectionTagMembershipRecord.collection_id,
                principal,
                CATALOG_READ,
            )
        )
        .group_by(CollectionTagRecord.tag)
    )
    if query is not None:
        statement = statement.where(
            CollectionTagRecord.search_text.like(f"%{_like_literal(query)}%", escape="\\")
        )
    return statement, (CollectionTagRecord.tag,)


def _settle_mutation(
    session: Session,
    *,
    collection_id: int,
    mutation: CollectionTagMutationRecord,
    head: CollectionTagHeadDocument,
    publication: CollectionTagPublicationRecord,
) -> None:
    collection = session.scalar(
        select(CollectionRecord).where(CollectionRecord.id == collection_id).with_for_update()
    )
    if collection is None:
        return
    if collection.tag_mutation_operation_id != mutation.operation_id:
        raise Conflict("collection tag mutation fence changed")
    event = begin_catalog_event(
        session,
        change="updated",
        collection_id=collection_id,
        occurred_at=utc_timestamp_now(),
        inventory_identity=collection.inventory_identity,
    )
    snapshot_catalog_event_collection_tags(
        session, event=event, phase="before", collection_id=collection_id
    )
    if mutation.action == "add":
        tag_record = session.get(CollectionTagRecord, mutation.tag_sha256)
        if tag_record is None:
            now = utc_timestamp_now()
            tag_record = CollectionTagRecord(
                tag_sha256=mutation.tag_sha256,
                tag=mutation.tag,
                search_text=text_search_key(mutation.tag),
                created_at=now,
                updated_at=now,
                collection_count=0,
            )
            session.add(tag_record)
            session.flush()
        elif tag_record.tag != mutation.tag:
            raise RuntimeError("collection tag SHA-256 collision")
        if session.get(CollectionTagMembershipRecord, (collection_id, mutation.tag_sha256)) is None:
            session.add(
                CollectionTagMembershipRecord(
                    collection_id=collection_id,
                    tag_sha256=mutation.tag_sha256,
                    added_at=utc_timestamp_now(),
                )
            )
            tag_record.collection_count += 1
            tag_record.updated_at = utc_timestamp_now()
    else:
        membership = session.get(
            CollectionTagMembershipRecord, (collection_id, mutation.tag_sha256)
        )
        if membership is not None:
            session.delete(membership)
            tag_record = session.get(CollectionTagRecord, mutation.tag_sha256)
            if tag_record is not None:
                tag_record.collection_count -= 1
                tag_record.updated_at = utc_timestamp_now()
    collection.tag_revision = head.revision
    collection.tag_root_sha256 = head.root_sha256
    collection.tag_set_identity = head.tag_set_identity
    collection.tag_head_identity = head.head_identity
    collection.tag_mutation_operation_id = None
    event.tag_revision = head.revision
    event.tag_set_identity = head.tag_set_identity
    session.add(
        CollectionTagRevisionRecord(
            collection_id=collection_id,
            revision=head.revision,
            root_sha256=head.root_sha256,
            tag_set_identity=head.tag_set_identity,
            head_identity=head.head_identity,
            created_at=utc_timestamp_now(),
        )
    )
    mutation.state = "succeeded"
    mutation.updated_at = utc_timestamp_now()
    mutation.failure = None
    session.flush()
    snapshot_catalog_event_collection_tags(
        session, event=event, phase="after", collection_id=collection_id
    )
    publish_catalog_event(session, event=event)
    for copy in session.scalars(
        select(CollectionArchiveCopyRecord).where(
            CollectionArchiveCopyRecord.collection_id == collection_id,
            CollectionArchiveCopyRecord.store != publication.store,
            CollectionArchiveCopyRecord.state == "uploaded",
        )
    ):
        target = session.get(CollectionTagPublicationRecord, (collection_id, copy.store))
        if target is None:
            target = CollectionTagPublicationRecord(
                collection_id=collection_id,
                store=copy.store,
                desired_revision=head.revision,
                desired_tag_set_identity=head.tag_set_identity,
                desired_head_identity=head.head_identity,
                published_revision=0,
                published_tag_set_identity=CollectionTagSetRoot.seal(None).tag_set_identity,
                published_head_identity=None,
                state="pending",
                next_attempt_at=utc_timestamp_now(),
                failure=None,
            )
            session.add(target)
            session.flush()
        _schedule_publication(
            session,
            publication=target,
            revision=head.revision,
            tag_set_identity=head.tag_set_identity,
            head_identity=head.head_identity,
            root_sha256=head.root_sha256,
            already_expanded=(),
        )


def _schedule_publication(
    session: Session,
    *,
    publication: CollectionTagPublicationRecord,
    revision: int,
    tag_set_identity: str,
    head_identity: str,
    root_sha256: str | None,
    already_expanded: Iterable[str],
) -> None:
    publication.desired_revision = revision
    publication.desired_tag_set_identity = tag_set_identity
    publication.desired_head_identity = head_identity
    publication.state = "pending"
    publication.next_attempt_at = utc_timestamp_now()
    publication.failure = None
    expanded = set(already_expanded)
    if root_sha256 is not None:
        _add_frontier_node(
            session,
            publication=publication,
            digest=root_sha256,
            expanded=root_sha256 in expanded,
        )
    for digest in expanded - ({root_sha256} if root_sha256 is not None else set()):
        _add_frontier_node(
            session,
            publication=publication,
            digest=digest,
            expanded=True,
        )


def _add_frontier_node(
    session: Session,
    *,
    publication: CollectionTagPublicationRecord,
    digest: str,
    expanded: bool,
) -> None:
    key = (
        publication.collection_id,
        publication.store,
        publication.desired_head_identity,
        digest,
    )
    if session.get(CollectionTagPublicationFrontierRecord, key) is None:
        already = session.get(
            CollectionTagPublishedNodeRecord,
            (publication.collection_id, publication.store, digest),
        )
        gc = session.get(
            CollectionTagNodeGcRecord,
            (publication.collection_id, publication.store, digest),
        )
        session.add(
            CollectionTagPublicationFrontierRecord(
                collection_id=publication.collection_id,
                store=publication.store,
                head_identity=publication.desired_head_identity,
                node_digest=digest,
                expanded=expanded,
                published=already is not None and gc is None,
            )
        )


def _current_publication_candidate(
    session: Session, *, collection: CollectionRecord
) -> CollectionTagPublicationRecord | None:
    return session.scalar(
        select(CollectionTagPublicationRecord)
        .join(
            CollectionArchiveCopyRecord,
            (
                CollectionArchiveCopyRecord.collection_id
                == CollectionTagPublicationRecord.collection_id
            )
            & (CollectionArchiveCopyRecord.store == CollectionTagPublicationRecord.store),
        )
        .where(
            CollectionTagPublicationRecord.collection_id == collection.id,
            CollectionTagPublicationRecord.published_revision == collection.tag_revision,
            CollectionTagPublicationRecord.published_tag_set_identity
            == collection.tag_set_identity,
            CollectionTagPublicationRecord.state == "published",
            CollectionArchiveCopyRecord.state == "uploaded",
        )
        .order_by(CollectionTagPublicationRecord.store)
        .limit(1)
        .with_for_update()
    )


def ensure_tag_publication_for_copy(
    session: Session,
    *,
    collection: CollectionRecord,
    store_name: str,
) -> CollectionTagPublicationRecord:
    """Ensure a newly complete archive copy receives the current recoverable tag authority."""

    publication = session.get(CollectionTagPublicationRecord, (collection.id, store_name))
    if publication is None:
        publication = CollectionTagPublicationRecord(
            collection_id=collection.id,
            store=store_name,
            desired_revision=collection.tag_revision,
            desired_tag_set_identity=collection.tag_set_identity,
            desired_head_identity=collection.tag_head_identity,
            published_revision=0,
            published_tag_set_identity=CollectionTagSetRoot.seal(None).tag_set_identity,
            published_head_identity=None,
            state="pending",
            next_attempt_at=utc_timestamp_now(),
            failure=None,
        )
        session.add(publication)
        session.flush()
    _schedule_publication(
        session,
        publication=publication,
        revision=collection.tag_revision,
        tag_set_identity=collection.tag_set_identity,
        head_identity=collection.tag_head_identity,
        root_sha256=collection.tag_root_sha256,
        already_expanded=(),
    )
    return publication


def _revision_root(
    session: Session,
    collection_id: int,
    revision: int,
    publication: CollectionTagPublicationRecord,
) -> str | None:
    mutation = session.scalar(
        select(CollectionTagMutationRecord).where(
            CollectionTagMutationRecord.collection_id == collection_id,
            CollectionTagMutationRecord.result_revision == revision,
            CollectionTagMutationRecord.result_head_identity == publication.desired_head_identity,
        )
    )
    if mutation is not None:
        return mutation.result_root_sha256
    authority = session.get(CollectionTagRevisionRecord, (collection_id, revision))
    if authority is None:
        raise RuntimeError("collection tag revision is unavailable")
    return authority.root_sha256


def _operation_id(value: str) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > 256:
        raise ValueError("collection tag operation id must contain 1 to 256 UTF-8 bytes")
    if value != value.strip() or any(ord(character) < 0x20 for character in value):
        raise ValueError("collection tag operation id is not canonical")
    return value


def _require_same_mutation(
    record: CollectionTagMutationRecord,
    *,
    action: str,
    tag: str,
    expected_revision: int,
    expected_tag_set_identity: str,
) -> None:
    if (
        record.action != action
        or record.tag != tag
        or record.expected_revision != expected_revision
        or record.expected_tag_set_identity != expected_tag_set_identity
    ):
        raise Conflict("collection tag operation id was reused")


def _mutation_payload(record: CollectionTagMutationRecord) -> dict[str, object]:
    return {
        "collection_id": record.collection_id,
        "operation_id": record.operation_id,
        "action": record.action,
        "tag": record.tag,
        "changed": record.changed,
        "revision": record.result_revision,
        "root_sha256": record.result_root_sha256,
        "tag_set_identity": record.result_tag_set_identity,
        "head_identity": record.result_head_identity,
        "state": record.state,
    }


def _like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


__all__ = [
    "SqlAlchemyCollectionTagService",
    "advance_collection_tag_set",
    "build_collection_tag_set",
    "collection_tag_sha256",
    "ensure_tag_publication_for_copy",
]
