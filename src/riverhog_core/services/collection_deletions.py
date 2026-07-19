from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from riverhog_core.archive_custody import ARCHIVE_CUSTODY_WARNING
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyJobRecord,
    ArchiveCopyRetirementRecord,
    CatalogEventRecord,
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionDeletionRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    RetrievalCacheObjectRecord,
    RetrievalJobFileRecord,
    RetrievalJobRecord,
)
from riverhog_core.domain.errors import BadRequest, Conflict, InvalidState, NotFound
from riverhog_core.ports.retrieval_cache import RetrievalCache
from riverhog_core.ports.upload_store import UploadStore
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_catalog import publish_archive_catalog
from riverhog_core.services.archive_records import archive_copy_identity, archive_copy_is_complete
from riverhog_core.services.collections import (
    _collection_upload_target_path,
    _normalize_collection_id_or_raise,
)
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService
from riverhog_core.timestamps import format_utc_timestamp, utc_now

_PLAN_TTL = timedelta(minutes=15)
_CHALLENGE_RE = re.compile(r"^delete-(\d+)-([0-9a-f]{64})$")
_ACTIVE_RETRIEVAL_STATES = {"requested", "ready"}


class SqlAlchemyCollectionDeletionService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        upload_store: UploadStore,
        retrieval_cache: RetrievalCache | None,
    ) -> None:
        self._archive_stores = archive_stores
        self._upload_store = upload_store
        self._retrieval_cache = retrieval_cache
        self._session_factory = make_session_factory(config.database_url)
        self._lifecycle_events = SqlAlchemyLifecycleEventService(config)

    def plan(self, collection_id: str) -> dict[str, object]:
        normalized_id = _normalize_collection_id_or_raise(collection_id)
        with session_scope(self._session_factory) as session:
            active = session.get(CollectionDeletionRecord, normalized_id)
            if active is not None:
                return cast(dict[str, object], json.loads(active.plan_json))
            expires = (utc_now() + _PLAN_TTL).replace(microsecond=0)
            plan = _build_plan(session, collection_id=normalized_id, expires_at=expires)
            plan["challenge"] = None if plan["blockers"] else _plan_challenge(plan, expires)
            return plan

    def delete(self, collection_id: str, *, challenge: str) -> dict[str, object]:
        normalized_id = _normalize_collection_id_or_raise(collection_id)
        supplied_challenge = challenge.strip()
        if not supplied_challenge:
            raise BadRequest("collection deletion challenge is required")

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
                if _CHALLENGE_RE.fullmatch(supplied_challenge) is None:
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
                expires = _challenge_expiry(supplied_challenge)
                if utc_now() > expires:
                    raise Conflict("collection deletion plan has expired; request a new plan")
                plan = _build_plan(session, collection_id=normalized_id, expires_at=expires)
                if not secrets.compare_digest(_plan_challenge(plan, expires), supplied_challenge):
                    raise Conflict("collection deletion plan changed; request a new plan")
                blockers = cast(list[str], plan["blockers"])
                if blockers:
                    raise Conflict("collection deletion is blocked: " + "; ".join(blockers))
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

        self._delete_upload_remnants(normalized_id)
        self._delete_cached_objects(normalized_id)
        self._delete_archive_objects(plan)
        for store_name, archive_store in self._archive_stores.items():
            publish_archive_catalog(
                store_name=store_name,
                archive_store=archive_store,
                session_factory=self._session_factory,
                excluded_collection_ids={normalized_id},
            )
        return self._finish(normalized_id, supplied_challenge, plan)

    def _delete_upload_remnants(self, collection_id: str) -> None:
        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .options(selectinload(CollectionUploadRecord.files))
                .where(CollectionUploadRecord.collection_id == collection_id)
            )
            if upload is None:
                return
            if upload.state not in {"canceled", "expired"}:
                raise Conflict(f"collection upload is active: {upload.state or 'unknown'}")
            for file in upload.files:
                if file.tus_url:
                    self._upload_store.cancel_upload(file.tus_url)
                self._upload_store.delete_target(
                    _collection_upload_target_path(collection_id, file.path)
                )

    def _delete_cached_objects(self, collection_id: str) -> None:
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
        collection_id = str(plan["collection_id"])
        stores = {
            str(item["store"]) for item in cast(list[dict[str, object]], plan["archive_objects"])
        }
        for store_name in sorted(stores):
            with session_scope(self._session_factory) as session:
                copy = session.get(CollectionArchiveCopyRecord, (collection_id, store_name))
                if copy is None or not archive_copy_is_complete(copy):
                    raise Conflict("collection archive changed during deletion")
                objects = archive_copy_identity(copy).objects
            self._archive_stores.require(store_name).delete_collection_archive(
                collection_id=collection_id,
                objects=objects,
            )

    def _finish(
        self,
        collection_id: str,
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
            job_ids = list(
                session.scalars(
                    select(RetrievalJobFileRecord.job_id).where(
                        RetrievalJobFileRecord.collection_id == collection_id
                    )
                )
            )
            if job_ids:
                session.execute(
                    delete(RetrievalJobRecord).where(RetrievalJobRecord.id.in_(job_ids))
                )
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is not None:
                session.delete(upload)
            collection = session.get(CollectionRecord, collection_id)
            if collection is not None:
                self._lifecycle_events.emit_collection(
                    type="collection.deleted",
                    collection_id=collection_id,
                    details={
                        "files": int(plan["file_count"]),
                        "bytes": int(plan["bytes"]),
                        "remote_storage_bytes": int(plan["remote_storage_bytes"]),
                    },
                    terminal=True,
                    session=session,
                )
                session.delete(collection)
            session.add(
                CatalogEventRecord(
                    change="deleted",
                    collection_id=collection_id,
                    occurred_at=format_utc_timestamp(utc_now()),
                    manifest_etag=str(plan["manifest_etag"]),
                )
            )
            session.delete(active)
        return _deletion_result(plan, status="deleted")


def _build_plan(
    session: Session,
    *,
    collection_id: str,
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
    files = list(
        session.scalars(
            select(CollectionFileRecord)
            .where(CollectionFileRecord.collection_id == collection_id)
            .order_by(CollectionFileRecord.path)
        )
    )
    file_count, file_bytes = session.execute(
        select(
            func.count(CollectionFileRecord.path),
            func.coalesce(func.sum(CollectionFileRecord.bytes), 0),
        ).where(CollectionFileRecord.collection_id == collection_id)
    ).one()
    remote_storage_bytes = int(
        session.scalar(
            select(func.coalesce(func.sum(CollectionArchiveObjectRecord.stored_bytes), 0)).where(
                CollectionArchiveObjectRecord.collection_id == collection_id
            )
        )
        or 0
    )
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
    archive_objects = [
        {
            "store": archive.store,
            "kind": current.kind,
            "object_path": current.object_path,
            "stored_bytes": current.stored_bytes,
        }
        for archive in archives
        for current in sorted(archive.objects, key=lambda item: item.object_order)
    ]
    return {
        "status": "blocked" if blockers else "ready",
        "collection_id": collection_id,
        "warning": ARCHIVE_CUSTODY_WARNING,
        "expires_at": format_utc_timestamp(expires_at),
        "files": [{"path": file.path, "bytes": file.bytes} for file in files],
        "file_count": int(file_count),
        "bytes": int(file_bytes),
        "archive_objects": archive_objects,
        "remote_storage_bytes": remote_storage_bytes,
        "upload_files": [
            {"path": row.path, "bytes": row.bytes}
            for row in session.scalars(
                select(CollectionUploadFileRecord)
                .where(CollectionUploadFileRecord.collection_id == collection_id)
                .order_by(CollectionUploadFileRecord.file_order)
            )
        ],
        "manifest_etag": collection.manifest_etag,
        "metadata_rows": {
            "collections": 1,
            "collection_files": int(file_count),
            "collection_archive_copies": len(archives),
            "collection_uploads": int(upload is not None),
            "collection_upload_files": upload_file_count,
        },
        "blockers": blockers,
        "billing_note": (
            "Measured remote bytes are catalog values. Provider retention, object versions, "
            "minimum-storage duration, and billing timing can affect realized savings."
        ),
    }


def _active_blockers(session: Session, collection_id: str) -> list[str]:
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
        )
    )
    blockers.extend(f"retrieval job is active: {job_id}" for job_id in retrieval_jobs)
    copy_jobs = list(
        session.execute(
            select(ArchiveCopyJobRecord.source_store, ArchiveCopyJobRecord.destination_store)
            .where(ArchiveCopyJobRecord.collection_id == collection_id)
            .order_by(ArchiveCopyJobRecord.destination_store)
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
        )
    )
    blockers.extend(f"archive copy retirement is active: {store}" for store in retirements)
    return blockers


def _plan_challenge(plan: dict[str, object], expires_at: datetime) -> str:
    payload = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"delete-{int(expires_at.timestamp())}-{hashlib.sha256(payload).hexdigest()}"


def _challenge_expiry(challenge: str) -> datetime:
    match = _CHALLENGE_RE.fullmatch(challenge)
    if match is None:
        raise BadRequest("invalid collection deletion challenge")
    return datetime.fromtimestamp(int(match.group(1)), tz=UTC)


def _deletion_result(plan: dict[str, object], *, status: str) -> dict[str, object]:
    return {
        "status": status,
        "collection_id": plan["collection_id"],
        "files": plan["file_count"],
        "bytes": plan["bytes"],
        "remote_storage_bytes": plan["remote_storage_bytes"],
    }
