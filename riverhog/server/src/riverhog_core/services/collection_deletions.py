from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import cast

from riverhog_protocol.errors import BadRequest, Conflict, InvalidState, NotFound
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.archive_safety import ARCHIVE_DATA_LOSS_WARNING
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_events import record_catalog_event
from riverhog_core.catalog_models import (
    ArchiveCopyJobRecord,
    ArchiveCopyRetirementRecord,
    CollectionArchiveAttestationRecord,
    CollectionArchiveCopyRecord,
    CollectionDeletionRecord,
    CollectionFileRecord,
    CollectionMetadataPublicationRecord,
    CollectionProofMaturationRecord,
    CollectionRecord,
    CollectionTagRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    RetrievalCacheObjectRecord,
    RetrievalJobFileRecord,
    RetrievalJobRecord,
)
from riverhog_core.ports.retrieval_cache import RetrievalCache
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_copy_states import ARCHIVE_COPY_BLOCKING_STATES
from riverhog_core.services.archive_records import (
    archive_copy_aggregates,
    archive_copy_is_complete,
    archive_copy_owned_identity,
)
from riverhog_core.services.collections import _normalize_collection_id_or_raise
from riverhog_core.services.lifecycle_events import (
    SqlAlchemyLifecycleEventService,
    event_context_json,
)
from riverhog_core.services.operation_plans import (
    PLAN_TTL,
    challenge_expiry,
    challenge_has_shape,
    plan_challenge,
)

_CHALLENGE_PREFIX = "delete"
_ACTIVE_RETRIEVAL_STATES = {"requested", "ready"}
_EXECUTION_KEY = "_execution"
_BLOCKER_SAMPLE_LIMIT = 10


class SqlAlchemyCollectionDeletionService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        retrieval_cache: RetrievalCache | None,
    ) -> None:
        self._archive_stores = archive_stores
        self._retrieval_cache = retrieval_cache
        self._session_factory = make_session_factory(config.database_url)
        self._lifecycle_events = SqlAlchemyLifecycleEventService(config)

    def plan(self, collection_id: int) -> dict[str, object]:
        normalized_id = _normalize_collection_id_or_raise(collection_id)
        with session_scope(self._session_factory) as session:
            active = session.get(CollectionDeletionRecord, normalized_id)
            if active is not None:
                return _public_plan(cast(dict[str, object], json.loads(active.plan_json)))
            expires = (utc_now() + PLAN_TTL).replace(microsecond=0)
            plan = _build_plan(session, collection_id=normalized_id, expires_at=expires)
            plan["challenge"] = (
                None if plan["blockers"] else plan_challenge(_CHALLENGE_PREFIX, plan, expires)
            )
            return plan

    def delete(
        self,
        collection_id: int,
        *,
        challenge: str,
        initiator: ApplicationPrincipal,
        event_context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        normalized_id = _normalize_collection_id_or_raise(collection_id)
        supplied_challenge = challenge.strip()
        if not supplied_challenge:
            raise BadRequest("collection deletion challenge is required")
        normalized_context_json = event_context_json(event_context)

        with session_scope(self._session_factory) as session:
            collection = session.scalar(
                select(CollectionRecord)
                .where(CollectionRecord.id == normalized_id)
                .with_for_update()
            )
            active = session.get(CollectionDeletionRecord, normalized_id)
            if active is not None:
                if not secrets.compare_digest(active.challenge, supplied_challenge):
                    raise Conflict("collection deletion challenge does not match active deletion")
                plan = cast(dict[str, object], json.loads(active.plan_json))
            elif collection is None:
                if not challenge_has_shape(supplied_challenge, prefix=_CHALLENGE_PREFIX):
                    raise NotFound(f"collection not found: {normalized_id}")
                return _deletion_result(
                    {
                        "collection_id": normalized_id,
                        "file_count": 0,
                        "bytes": 0,
                        "remote_storage_bytes": 0,
                    },
                    status="already_absent",
                )
            else:
                expires = challenge_expiry(
                    supplied_challenge,
                    prefix=_CHALLENGE_PREFIX,
                    operation="collection deletion",
                )
                if utc_now() > expires:
                    raise Conflict("collection deletion plan has expired; request a new plan")
                plan = _build_plan(session, collection_id=normalized_id, expires_at=expires)
                if not secrets.compare_digest(
                    plan_challenge(_CHALLENGE_PREFIX, plan, expires),
                    supplied_challenge,
                ):
                    raise Conflict("collection deletion plan changed; request a new plan")
                blockers = cast(list[str], plan["blockers"])
                if blockers:
                    raise Conflict("collection deletion is blocked: " + "; ".join(blockers))
                plan[_EXECUTION_KEY] = {
                    "app": initiator.app,
                    "key_id": initiator.key_id,
                    "event_context_json": normalized_context_json,
                }
                plan["challenge"] = supplied_challenge
                plan["status"] = "deleting"
                session.add(
                    CollectionDeletionRecord(
                        collection_id=normalized_id,
                        challenge=supplied_challenge,
                        plan_json=json.dumps(plan, sort_keys=True, separators=(",", ":")),
                        started_at=format_utc_timestamp(utc_now()),
                    )
                )

        self._delete_cached_objects(normalized_id)
        self._delete_archive_objects(plan)
        return self._finish(normalized_id, supplied_challenge, plan)

    def _delete_cached_objects(self, collection_id: int) -> None:
        with session_scope(self._session_factory) as session:
            cached = list(
                session.scalars(
                    select(RetrievalCacheObjectRecord).where(
                        RetrievalCacheObjectRecord.collection_id == collection_id
                    )
                )
            )
        if cached and self._retrieval_cache is None:
            raise Conflict("collection retrieval-cache objects cannot be removed")
        for current in cached:
            assert self._retrieval_cache is not None
            self._retrieval_cache.delete(
                object_path=current.object_path,
                version_id=current.version_id,
            )

    def _delete_archive_objects(self, plan: dict[str, object]) -> None:
        collection_id = cast(int, plan["collection_id"])
        stores = {
            str(item["store"]) for item in cast(list[dict[str, object]], plan["archive_copies"])
        }
        for store_name in sorted(stores):
            with session_scope(self._session_factory) as session:
                copy = session.get(CollectionArchiveCopyRecord, (collection_id, store_name))
                if copy is None or not archive_copy_is_complete(copy):
                    raise Conflict("collection archive changed during deletion")
                objects = archive_copy_owned_identity(copy).objects
            self._archive_stores.require(store_name).delete_collection_archive(
                collection_id=collection_id,
                objects=objects,
            )

    def _finish(
        self,
        collection_id: int,
        challenge: str,
        plan: dict[str, object],
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            active = session.get(CollectionDeletionRecord, collection_id)
            if active is None:
                return _deletion_result(plan, status="already_absent")
            if not secrets.compare_digest(active.challenge, challenge):
                raise Conflict("collection deletion challenge does not match active deletion")
            blockers = _active_blockers(session, collection_id)
            if blockers:
                raise Conflict("collection activity began during deletion: " + "; ".join(blockers))
            retrieval_job_ids = select(RetrievalJobFileRecord.job_id).where(
                RetrievalJobFileRecord.collection_id == collection_id
            )
            session.execute(
                delete(RetrievalJobRecord).where(RetrievalJobRecord.id.in_(retrieval_job_ids))
            )
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is not None:
                session.delete(upload)
            collection = session.get(CollectionRecord, collection_id)
            if collection is not None:
                before_tags = tuple(
                    session.scalars(
                        select(CollectionTagRecord.tag_id)
                        .where(CollectionTagRecord.collection_id == collection_id)
                        .order_by(CollectionTagRecord.tag_id)
                    ).all()
                )
                execution = _execution(plan)
                self._lifecycle_events.emit_collection(
                    type="collection.deleted",
                    collection_id=collection_id,
                    details={
                        "files": cast(int, plan["file_count"]),
                        "bytes": cast(int, plan["bytes"]),
                        "remote_storage_bytes": cast(int, plan["remote_storage_bytes"]),
                    },
                    terminal=True,
                    initiator=ApplicationPrincipal(
                        app=str(execution["app"]),
                        key_id=(
                            str(execution["key_id"])
                            if execution.get("key_id") is not None
                            else None
                        ),
                        access=frozenset(),
                    ),
                    event_context_json=(
                        str(execution["event_context_json"])
                        if execution.get("event_context_json") is not None
                        else None
                    ),
                    session=session,
                )
                record_catalog_event(
                    session,
                    change="deleted",
                    collection_id=collection_id,
                    occurred_at=format_utc_timestamp(utc_now()),
                    record_etag=str(plan["record_etag"]),
                    before_tags=before_tags,
                    after_tags=(),
                )
                session.delete(collection)
            session.delete(active)
        return _deletion_result(plan, status="deleted")


def _build_plan(
    session: Session,
    *,
    collection_id: int,
    expires_at: datetime,
) -> dict[str, object]:
    collection = session.get(CollectionRecord, collection_id)
    if collection is None:
        raise NotFound(f"collection not found: {collection_id}")
    archives = list(
        session.scalars(
            select(CollectionArchiveCopyRecord)
            .where(CollectionArchiveCopyRecord.collection_id == collection_id)
            .order_by(CollectionArchiveCopyRecord.store)
        )
    )
    if not archives or any(not archive_copy_is_complete(copy) for copy in archives):
        raise InvalidState(
            f"collection archive copies are not completely uploaded and verified: {collection_id}"
        )
    file_count, file_bytes = session.execute(
        select(
            func.count(CollectionFileRecord.path),
            func.coalesce(func.sum(CollectionFileRecord.bytes), 0),
        ).where(CollectionFileRecord.collection_id == collection_id)
    ).one()
    aggregates = archive_copy_aggregates(session, collection_ids=[collection_id])
    archive_copies: list[dict[str, str | int]] = [
        {
            "store": archive.store,
            "objects": aggregates.get((collection_id, archive.store), (0, 0))[0],
            "stored_bytes": aggregates.get((collection_id, archive.store), (0, 0))[1],
        }
        for archive in archives
    ]
    archive_object_count = sum(int(copy["objects"]) for copy in archive_copies)
    remote_storage_bytes = sum(int(copy["stored_bytes"]) for copy in archive_copies)
    upload = session.get(CollectionUploadRecord, collection_id)
    upload_file_count = int(
        session.scalar(
            select(func.count(CollectionUploadFileRecord.path)).where(
                CollectionUploadFileRecord.collection_id == collection_id
            )
        )
        or 0
    )
    blockers = _active_blockers(session, collection_id)
    if upload is not None and upload.state not in {"canceled", "expired"}:
        blockers.append(f"collection upload is active: {upload.state or 'unknown'}")
    metadata_publication_count = int(
        session.scalar(
            select(func.count())
            .select_from(CollectionMetadataPublicationRecord)
            .where(
                CollectionMetadataPublicationRecord.collection_id == collection_id,
                CollectionMetadataPublicationRecord.object_path.is_not(None),
            )
        )
        or 0
    )
    return {
        "status": "blocked" if blockers else "ready",
        "collection_id": collection_id,
        "warning": ARCHIVE_DATA_LOSS_WARNING,
        "expires_at": format_utc_timestamp(expires_at),
        "file_count": int(file_count),
        "bytes": int(file_bytes),
        "archive_copies": archive_copies,
        "archive_object_count": archive_object_count,
        "remote_storage_bytes": remote_storage_bytes,
        "upload_file_count": upload_file_count,
        "record_etag": collection.record_etag,
        "metadata_rows": {
            "collections": 1,
            "collection_files": int(file_count),
            "collection_archive_copies": len(archives),
            "collection_tags": len(collection.tags),
            "collection_metadata_publications": metadata_publication_count,
            "collection_uploads": int(upload is not None),
            "collection_upload_files": upload_file_count,
        },
        "blockers": blockers,
        "billing_note": (
            "Measured remote bytes are catalog values. Provider retention, object versions, "
            "minimum-storage duration, and billing timing can affect realized savings."
        ),
    }


def _public_plan(plan: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in plan.items() if key != _EXECUTION_KEY}


def _execution(plan: dict[str, object]) -> dict[str, object]:
    execution = plan.get(_EXECUTION_KEY)
    if not isinstance(execution, dict) or not str(execution.get("app") or ""):
        raise Conflict("collection deletion has no authenticated initiator")
    return cast(dict[str, object], execution)


def _active_blockers(session: Session, collection_id: int) -> list[str]:
    blockers: list[str] = []
    retrieval_jobs = list(
        session.scalars(
            select(RetrievalJobRecord.id)
            .join(RetrievalJobFileRecord)
            .where(
                RetrievalJobFileRecord.collection_id == collection_id,
                RetrievalJobRecord.state.in_(_ACTIVE_RETRIEVAL_STATES),
            )
            .order_by(RetrievalJobRecord.id)
            .limit(_BLOCKER_SAMPLE_LIMIT)
        )
    )
    blockers.extend(f"retrieval job is active: {job_id}" for job_id in retrieval_jobs)
    copy_jobs = list(
        session.execute(
            select(ArchiveCopyJobRecord.source_store, ArchiveCopyJobRecord.destination_store)
            .where(
                ArchiveCopyJobRecord.collection_id == collection_id,
                ArchiveCopyJobRecord.state.in_(ARCHIVE_COPY_BLOCKING_STATES),
            )
            .order_by(ArchiveCopyJobRecord.destination_store)
            .limit(_BLOCKER_SAMPLE_LIMIT)
        )
    )
    blockers.extend(
        f"archive copy is active: {source} -> {destination}" for source, destination in copy_jobs
    )
    retirements = list(
        session.scalars(
            select(ArchiveCopyRetirementRecord.store)
            .where(ArchiveCopyRetirementRecord.collection_id == collection_id)
            .order_by(ArchiveCopyRetirementRecord.store)
            .limit(_BLOCKER_SAMPLE_LIMIT)
        )
    )
    blockers.extend(f"archive copy retirement is active: {store}" for store in retirements)
    proof_maturations = list(
        session.scalars(
            select(CollectionProofMaturationRecord.store)
            .where(
                CollectionProofMaturationRecord.collection_id == collection_id,
                CollectionProofMaturationRecord.state == "upgrading",
            )
            .order_by(CollectionProofMaturationRecord.store)
            .limit(_BLOCKER_SAMPLE_LIMIT)
        )
    )
    blockers.extend(f"archive proof maturation is active: {store}" for store in proof_maturations)
    attestations = list(
        session.scalars(
            select(CollectionArchiveAttestationRecord.store)
            .where(
                CollectionArchiveAttestationRecord.collection_id == collection_id,
                CollectionArchiveAttestationRecord.state.in_(("publishing", "upgrading")),
            )
            .order_by(CollectionArchiveAttestationRecord.store)
            .limit(_BLOCKER_SAMPLE_LIMIT)
        )
    )
    blockers.extend(f"archive attestation is active: {store}" for store in attestations)
    metadata_publications = list(
        session.scalars(
            select(CollectionMetadataPublicationRecord.store)
            .where(
                CollectionMetadataPublicationRecord.collection_id == collection_id,
                CollectionMetadataPublicationRecord.state == "publishing",
            )
            .order_by(CollectionMetadataPublicationRecord.store)
            .limit(_BLOCKER_SAMPLE_LIMIT)
        )
    )
    blockers.extend(
        f"collection metadata publication is active: {store}" for store in metadata_publications
    )
    return blockers


def _deletion_result(plan: dict[str, object], *, status: str) -> dict[str, object]:
    return {
        "status": status,
        "collection_id": plan["collection_id"],
        "files": plan["file_count"],
        "bytes": plan["bytes"],
        "remote_storage_bytes": plan["remote_storage_bytes"],
    }
