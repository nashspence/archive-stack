from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Iterator, Sequence
from datetime import timedelta
from typing import cast

from riverhog_age import iter_decrypt_age_scrypt
from riverhog_protocol.errors import BadRequest, Conflict, InvalidState, NotFound
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from time_formats import format_utc_timestamp, parse_utc_timestamp, utc_now

from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.archive_objects import (
    CollectionArchiveDataObject,
    CollectionArchiveFile,
    iter_verified_file_chunks,
    iter_verified_object_chunks,
    load_collection_archive,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CatalogEventRecord,
    CollectionArchiveCopyRecord,
    CollectionArchiveFileObjectRecord,
    CollectionArchiveObjectRecord,
    CollectionDeletionRecord,
    CollectionFileRecord,
    CollectionRecord,
    RetrievalCacheLeaseRecord,
    RetrievalCacheObjectRecord,
    RetrievalJobFileRecord,
    RetrievalJobObjectRecord,
    RetrievalJobRecord,
)
from riverhog_core.collection_access import collection_access_filter, require_collection_access
from riverhog_core.portable_catalog import portable_collection_manifest
from riverhog_core.ports.archive_store import ArchiveObjectIdentity
from riverhog_core.ports.download_allowance import DownloadAllowance, DownloadAttribution
from riverhog_core.ports.retrieval_cache import RetrievalCache
from riverhog_core.proofs import CommandProofVerifier, ProofVerifier
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_records import archive_copy_is_complete
from riverhog_core.services.lifecycle_events import (
    SqlAlchemyLifecycleEventService,
    event_context_json,
)

_DATA_KINDS = {"pack", "file", "segment"}


class SqlAlchemyRetrievalService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        retrieval_cache: RetrievalCache | None,
        download_allowance: DownloadAllowance | None = None,
        *,
        proof_verifier: ProofVerifier | None = None,
    ) -> None:
        self._config = config
        self._archive_stores = archive_stores
        self._cache = retrieval_cache
        self._download_allowance = download_allowance
        self._proof_verifier = proof_verifier or CommandProofVerifier(config.ots_verify_command)
        self._session_factory = make_session_factory(config.database_url)
        self._lifecycle_events = SqlAlchemyLifecycleEventService(config)

    def collection_manifest(
        self,
        collection_id: str,
        *,
        principal: ApplicationPrincipal | None = None,
    ) -> tuple[dict[str, object], str]:
        require_collection_access(principal, collection_id)
        with session_scope(self._session_factory) as session:
            collection = session.get(CollectionRecord, collection_id)
            if collection is None:
                raise NotFound(f"collection not found: {collection_id}")
            files = list(
                session.scalars(
                    select(CollectionFileRecord)
                    .where(CollectionFileRecord.collection_id == collection_id)
                    .order_by(CollectionFileRecord.path)
                )
            )
            payload, etag = portable_collection_manifest(
                collection_id,
                ((row.path, row.bytes, row.sha256) for row in files),
            )
            if etag != collection.manifest_etag:
                raise InvalidState("portable collection manifest does not match its catalog ETag")
            return payload, etag

    def resource_list(
        self,
        *,
        principal: ApplicationPrincipal | None = None,
    ) -> list[dict[str, str]]:
        with session_scope(self._session_factory) as session:
            rows = session.execute(
                select(CollectionRecord.id, CollectionRecord.manifest_etag)
                .where(collection_access_filter(CollectionRecord.id, principal))
                .order_by(CollectionRecord.id)
            ).all()
            return [{"collection_id": str(row.id), "etag": str(row.manifest_etag)} for row in rows]

    def change_list(
        self,
        *,
        after: int = 0,
        limit: int = 1000,
        principal: ApplicationPrincipal | None = None,
    ) -> dict[str, object]:
        if after < 0:
            raise BadRequest("catalog cursor must be non-negative")
        if limit < 1 or limit > 10_000:
            raise BadRequest("catalog change limit must be between 1 and 10000")
        with session_scope(self._session_factory) as session:
            scanned = (
                select(CatalogEventRecord)
                .where(CatalogEventRecord.sequence > after)
                .order_by(CatalogEventRecord.sequence)
                .limit(limit)
                .subquery()
            )
            cursor = int(
                session.scalar(
                    select(scanned.c.sequence).order_by(scanned.c.sequence.desc()).limit(1)
                )
                or after
            )
            rows = (
                session.execute(
                    select(scanned)
                    .where(collection_access_filter(scanned.c.collection_id, principal))
                    .order_by(scanned.c.sequence)
                )
                .mappings()
                .all()
            )
            return {
                "cursor": cursor,
                "changes": [
                    {
                        "sequence": row["sequence"],
                        "change": row["change"],
                        "collection_id": row["collection_id"],
                        "occurred_at": row["occurred_at"],
                        "etag": row["manifest_etag"],
                    }
                    for row in rows
                ],
            }

    def plan(
        self,
        files: Sequence[tuple[str, str]],
        *,
        lease: timedelta | None = None,
        principal: ApplicationPrincipal | None = None,
    ) -> dict[str, object]:
        normalized = _normalize_file_refs(files)
        for collection_id, _path in normalized:
            require_collection_access(principal, collection_id)
        requested_lease = lease or self._config.retrieval_default_lease
        if requested_lease.total_seconds() <= 0:
            raise BadRequest("retrieval lease must be positive")
        if requested_lease > self._config.retrieval_max_lease:
            raise BadRequest("retrieval lease exceeds the configured maximum")
        with session_scope(self._session_factory) as session:
            payload = self._build_plan(session, normalized, requested_lease)
        canonical = _canonical_json(payload)
        return {
            **payload,
            "etag": hashlib.sha256(canonical).hexdigest(),
        }

    def create(
        self,
        *,
        app: str,
        key_id: str | None = None,
        files: Sequence[tuple[str, str]],
        plan_etag: str,
        lease: timedelta | None = None,
        event_context: dict[str, object] | None = None,
        principal: ApplicationPrincipal | None = None,
    ) -> dict[str, object]:
        normalized = _normalize_file_refs(files)
        if principal is not None:
            app = principal.app
            key_id = principal.key_id
        plan = self.plan(normalized, lease=lease, principal=principal)
        if not plan_etag or plan_etag != plan["etag"]:
            raise Conflict("retrieval plan changed; request a fresh plan")
        job_id = uuid.uuid4().hex
        now = utc_now()
        now_text = format_utc_timestamp(now)
        lease_seconds = int(str(plan["lease_seconds"]))
        planned_files = cast(list[dict[str, object]], plan["files"])
        planned_objects = cast(list[dict[str, object]], plan["objects"])
        requested = any(current["read_mode"] == "restore_required" for current in planned_objects)
        state = "requested" if requested else "ready"
        expires_at = format_utc_timestamp(now + timedelta(seconds=lease_seconds))
        remote_bytes = sum(
            int(str(current["stored_bytes"]))
            for current in planned_objects
            if current["read_mode"] != "cache"
        )
        if key_id is not None and self._download_allowance is not None:
            self._download_allowance.reserve_retrieval(
                key_id=key_id,
                job_id=job_id,
                expected_bytes=remote_bytes,
                expires_at=format_utc_timestamp(
                    now
                    + self._config.retrieval_estimated_latency
                    + self._config.retrieval_max_lease
                ),
            )
        try:
            with session_scope(self._session_factory) as session:
                for collection_id in sorted({collection_id for collection_id, _path in normalized}):
                    collection = session.scalar(
                        select(CollectionRecord)
                        .where(CollectionRecord.id == collection_id)
                        .with_for_update()
                    )
                    if collection is None:
                        raise NotFound(f"collection not found: {collection_id}")
                    if session.get(CollectionDeletionRecord, collection_id) is not None:
                        raise Conflict(f"collection deletion is active: {collection_id}")
                record = RetrievalJobRecord(
                    id=job_id,
                    app=app,
                    initiated_by_key_id=key_id,
                    event_context_json=event_context_json(event_context),
                    state=state,
                    plan_etag=str(plan["etag"]),
                    constraints_json=json.dumps(plan, sort_keys=True, separators=(",", ":")),
                    created_at=now_text,
                    requested_at=now_text if requested else None,
                    ready_at=None if requested else now_text,
                    expires_at=None if requested else expires_at,
                    next_poll_at=now_text if requested else None,
                )
                session.add(record)
                for order, current in enumerate(planned_files):
                    record.files.append(
                        RetrievalJobFileRecord(
                            job_id=job_id,
                            collection_id=str(current["collection_id"]),
                            path=str(current["path"]),
                            file_order=order,
                        )
                    )
                for order, current in enumerate(planned_objects):
                    record.objects.append(
                        RetrievalJobObjectRecord(
                            job_id=job_id,
                            collection_id=str(current["collection_id"]),
                            source_store=str(current["source_store"]),
                            object_id=str(current["object_id"]),
                            object_order=order,
                            read_mode=str(current["read_mode"]),
                        )
                    )
                    if current["read_mode"] == "cache":
                        self._lease_cached_object(
                            session,
                            owner=_job_owner(job_id),
                            source_store=str(current["source_store"]),
                            collection_id=str(current["collection_id"]),
                            object_id=str(current["object_id"]),
                            expires_at=expires_at,
                        )
                self._lifecycle_events.emit_retrieval(
                    type="retrieval.requested",
                    job=record,
                    details={
                        "files": len(planned_files),
                        "objects": len(planned_objects),
                        "restore_required": requested,
                    },
                    session=session,
                )
                if not requested:
                    self._lifecycle_events.emit_retrieval(
                        type="retrieval.ready",
                        job=record,
                        details={"expires_at": expires_at},
                        session=session,
                    )
        except Exception:
            if key_id is not None and self._download_allowance is not None:
                self._download_allowance.release_retrieval(job_id=job_id)
            raise
        if requested:
            self._request_job_objects(job_id)
        return self.get(app=app, key_id=key_id, job_id=job_id)

    def get(self, *, app: str, job_id: str, key_id: str | None = None) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            record = self._require_job(session, app=app, key_id=key_id, job_id=job_id)
            self._expire_job_if_due(session, record)
            return _job_payload(record)

    def acknowledge(self, *, app: str, job_id: str, key_id: str | None = None) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            record = self._require_job(session, app=app, key_id=key_id, job_id=job_id)
            if record.state not in {"ready", "completed"}:
                raise InvalidState("only a ready retrieval job can be acknowledged")
            if record.state != "completed":
                record.state = "completed"
                record.completed_at = format_utc_timestamp(utc_now())
                session.execute(
                    delete(RetrievalCacheLeaseRecord).where(
                        RetrievalCacheLeaseRecord.owner == _job_owner(job_id)
                    )
                )
                self._lifecycle_events.emit_retrieval(
                    type="retrieval.completed",
                    job=record,
                    terminal=True,
                    session=session,
                )
            payload = _job_payload(record)
        if self._download_allowance is not None:
            self._download_allowance.release_retrieval(job_id=job_id)
        return payload

    def cancel(self, *, app: str, job_id: str, key_id: str | None = None) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            record = self._require_job(session, app=app, key_id=key_id, job_id=job_id)
            self._expire_job_if_due(session, record)
            if record.state in {"completed", "expired"}:
                raise InvalidState(f"retrieval job is already {record.state}")
            if record.state != "canceled":
                record.state = "canceled"
                record.canceled_at = format_utc_timestamp(utc_now())
                record.next_poll_at = None
                session.execute(
                    delete(RetrievalCacheLeaseRecord).where(
                        RetrievalCacheLeaseRecord.owner == _job_owner(job_id)
                    )
                )
                self._lifecycle_events.emit_retrieval(
                    type="retrieval.canceled",
                    job=record,
                    terminal=True,
                    session=session,
                )
            payload = _job_payload(record)
        if self._download_allowance is not None:
            self._download_allowance.release_retrieval(job_id=job_id)
        return payload

    def content(
        self,
        *,
        app: str,
        job_id: str,
        collection_id: str,
        path: str,
        key_id: str | None = None,
    ) -> tuple[Iterator[bytes], int, str]:
        with session_scope(self._session_factory) as session:
            job = self._require_job(session, app=app, key_id=key_id, job_id=job_id)
            self._expire_job_if_due(session, job)
            if job.state != "ready":
                raise InvalidState("retrieval job is not ready")
            selected = session.get(RetrievalJobFileRecord, (job_id, collection_id, path))
            if selected is None:
                raise NotFound("file is not part of this retrieval job")
            file_record = session.get(CollectionFileRecord, (collection_id, path))
            if file_record is None:
                raise NotFound("file is no longer present")
            source_store = str(
                session.scalar(
                    select(RetrievalJobObjectRecord.source_store)
                    .where(
                        RetrievalJobObjectRecord.job_id == job_id,
                        RetrievalJobObjectRecord.collection_id == collection_id,
                    )
                    .limit(1)
                )
                or ""
            )
            if not source_store:
                raise InvalidState("retrieval job has no source archive")
            all_files = list(
                session.scalars(
                    select(CollectionFileRecord)
                    .where(CollectionFileRecord.collection_id == collection_id)
                    .order_by(CollectionFileRecord.path)
                )
            )
            object_rows = list(
                session.scalars(
                    select(CollectionArchiveObjectRecord)
                    .where(
                        CollectionArchiveObjectRecord.collection_id == collection_id,
                        CollectionArchiveObjectRecord.store == source_store,
                    )
                    .order_by(CollectionArchiveObjectRecord.object_order)
                )
            )
            cache_rows = {
                row.object_id: row
                for row in session.scalars(
                    select(RetrievalCacheObjectRecord).where(
                        RetrievalCacheObjectRecord.source_store == source_store,
                        RetrievalCacheObjectRecord.collection_id == collection_id,
                    )
                )
            }
            attribution = _download_attribution(job)

        identities = {row.object_id: _object_identity(row) for row in object_rows}

        def read_object(object_id: str) -> Iterable[bytes]:
            identity = identities[object_id]
            return self._read_archive_object(
                source_store=source_store,
                collection_id=collection_id,
                identity=identity,
                cached=cache_rows.get(object_id),
                attribution=attribution,
            )

        manifest_identity = identities.get("manifest")
        proof_identity = identities.get("proof")
        if manifest_identity is None or proof_identity is None:
            raise InvalidState("archive manifest artifacts are missing")
        manifest_bytes = b"".join(read_object("manifest"))
        proof_bytes = b"".join(read_object("proof"))
        archive = load_collection_archive(
            collection_id=collection_id,
            files=[
                CollectionArchiveFile(path=row.path, bytes=row.bytes, sha256=row.sha256)
                for row in all_files
            ],
            manifest_bytes=manifest_bytes,
            proof_bytes=proof_bytes,
            read_object_chunks=read_object,
            verifier=self._proof_verifier,
        )
        chunks, size = iter_verified_file_chunks(archive, path=path, read_object=read_object)
        return chunks, size, file_record.sha256

    def content_metadata(
        self,
        *,
        app: str,
        job_id: str,
        collection_id: str,
        path: str,
        key_id: str | None = None,
    ) -> tuple[int, str]:
        with session_scope(self._session_factory) as session:
            job = self._require_job(session, app=app, key_id=key_id, job_id=job_id)
            self._expire_job_if_due(session, job)
            if job.state != "ready":
                raise InvalidState("retrieval job is not ready")
            if session.get(RetrievalJobFileRecord, (job_id, collection_id, path)) is None:
                raise NotFound("file is not part of this retrieval job")
            file_record = session.get(CollectionFileRecord, (collection_id, path))
            if file_record is None:
                raise NotFound("file is no longer present")
            return file_record.bytes, file_record.sha256

    def object_content(
        self,
        *,
        app: str,
        job_id: str,
        collection_id: str,
        object_id: str,
        key_id: str | None = None,
    ) -> tuple[Iterator[bytes], int, str]:
        with session_scope(self._session_factory) as session:
            object_record, cache_record = self._require_job_data_object(
                session,
                app=app,
                key_id=key_id,
                job_id=job_id,
                collection_id=collection_id,
                object_id=object_id,
            )
            source_store = object_record.store
            identity = _object_identity(object_record)
            job = self._require_job(session, app=app, key_id=key_id, job_id=job_id)
            attribution = _download_attribution(job)
        current = CollectionArchiveDataObject(
            object_id=identity.object_id,
            kind=identity.kind,
            plaintext_bytes=identity.plaintext_bytes,
            sha256=identity.sha256,
            placements=(),
            _chunks=lambda: iter(()),
        )
        chunks = self._read_archive_object(
            source_store=source_store,
            collection_id=collection_id,
            identity=identity,
            cached=cache_record,
            attribution=attribution,
        )
        return (
            iter_verified_object_chunks(current, chunks),
            identity.plaintext_bytes,
            identity.sha256,
        )

    def object_content_metadata(
        self,
        *,
        app: str,
        job_id: str,
        collection_id: str,
        object_id: str,
        key_id: str | None = None,
    ) -> tuple[int, str]:
        with session_scope(self._session_factory) as session:
            object_record, _cache_record = self._require_job_data_object(
                session,
                app=app,
                key_id=key_id,
                job_id=job_id,
                collection_id=collection_id,
                object_id=object_id,
            )
            return object_record.plaintext_bytes, object_record.sha256

    def process_due(self, *, limit: int = 10) -> int:
        if limit < 1:
            return 0
        now_text = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            job_ids = list(
                session.scalars(
                    select(RetrievalJobRecord.id)
                    .where(
                        RetrievalJobRecord.state == "requested",
                        RetrievalJobRecord.next_poll_at <= now_text,
                    )
                    .order_by(RetrievalJobRecord.next_poll_at, RetrievalJobRecord.id)
                    .limit(limit)
                )
            )
        for job_id in job_ids:
            self._process_one(job_id)
        self.sweep()
        return len(job_ids)

    def sweep(self) -> int:
        now_text = format_utc_timestamp(utc_now())
        removed = 0
        with session_scope(self._session_factory) as session:
            for job in session.scalars(
                select(RetrievalJobRecord).where(
                    RetrievalJobRecord.state == "ready",
                    RetrievalJobRecord.expires_at <= now_text,
                )
            ):
                job.state = "expired"
                self._lifecycle_events.emit_retrieval(
                    type="retrieval.expired",
                    job=job,
                    terminal=True,
                    session=session,
                )
                session.execute(
                    delete(RetrievalCacheLeaseRecord).where(
                        RetrievalCacheLeaseRecord.owner == _job_owner(job.id)
                    )
                )
                _release_job_reservation(session, job.id)
            session.execute(
                delete(RetrievalCacheLeaseRecord).where(
                    RetrievalCacheLeaseRecord.expires_at <= now_text
                )
            )
            unleased = list(
                session.scalars(
                    select(RetrievalCacheObjectRecord).where(
                        ~select(RetrievalCacheLeaseRecord.owner)
                        .where(
                            RetrievalCacheLeaseRecord.source_store
                            == RetrievalCacheObjectRecord.source_store,
                            RetrievalCacheLeaseRecord.collection_id
                            == RetrievalCacheObjectRecord.collection_id,
                            RetrievalCacheLeaseRecord.object_id
                            == RetrievalCacheObjectRecord.object_id,
                        )
                        .exists()
                    )
                )
            )
            for cached in unleased:
                if self._cache is None:
                    continue
                self._cache.delete(
                    object_path=cached.object_path,
                    version_id=cached.version_id,
                )
                session.delete(cached)
                removed += 1
        return removed

    def _build_plan(
        self,
        session: Session,
        files: tuple[tuple[str, str], ...],
        lease: timedelta,
    ) -> dict[str, object]:
        files_payload: list[dict[str, object]] = []
        objects_payload: list[dict[str, object]] = []
        object_payloads: dict[tuple[str, str, str], dict[str, object]] = {}
        copy_by_collection: dict[str, CollectionArchiveCopyRecord] = {}
        for collection_id, path in files:
            file_record = session.get(CollectionFileRecord, (collection_id, path))
            if file_record is None:
                raise NotFound(f"file not found: {collection_id}/{path}")
            copy = copy_by_collection.get(collection_id)
            if copy is None:
                copy = self._select_copy(session, collection_id)
                copy_by_collection[collection_id] = copy
            files_payload.append(
                {
                    "collection_id": collection_id,
                    "path": path,
                    "bytes": file_record.bytes,
                    "sha256": file_record.sha256,
                }
            )
            placements = list(
                session.scalars(
                    select(CollectionArchiveFileObjectRecord)
                    .where(
                        CollectionArchiveFileObjectRecord.collection_id == collection_id,
                        CollectionArchiveFileObjectRecord.store == copy.store,
                        CollectionArchiveFileObjectRecord.path == path,
                    )
                    .order_by(CollectionArchiveFileObjectRecord.sequence)
                )
            )
            if not placements:
                raise InvalidState(f"archive placement is missing: {collection_id}/{path}")
            for placement in placements:
                object_id = placement.object_id
                identity = (collection_id, copy.store, str(object_id))
                payload = object_payloads.get(identity)
                if payload is None:
                    payload = self._plan_object_payload(
                        session,
                        collection_id=collection_id,
                        store=copy.store,
                        object_id=str(object_id),
                    )
                    object_payloads[identity] = payload
                    objects_payload.append(payload)
                cast(list[dict[str, object]], payload["placements"]).append(
                    {
                        "path": placement.path,
                        "sequence": placement.sequence,
                        "file_offset": placement.file_offset,
                        "bytes": placement.bytes,
                        "member": placement.member,
                    }
                )
            for object_id in ("manifest", "proof"):
                identity = (collection_id, copy.store, object_id)
                if identity not in object_payloads:
                    payload = self._plan_object_payload(
                        session,
                        collection_id=collection_id,
                        store=copy.store,
                        object_id=object_id,
                    )
                    object_payloads[identity] = payload
                    objects_payload.append(payload)
        return {
            "format": "riverhog-retrieval-plan/v1",
            "lease_seconds": int(lease.total_seconds()),
            "files": files_payload,
            "objects": objects_payload,
        }

    def _plan_object_payload(
        self,
        session: Session,
        *,
        collection_id: str,
        store: str,
        object_id: str,
    ) -> dict[str, object]:
        identity = (collection_id, store, object_id)
        object_record = session.get(CollectionArchiveObjectRecord, identity)
        if object_record is None:
            raise InvalidState("archive object record is missing")
        cached = session.get(RetrievalCacheObjectRecord, (store, collection_id, object_id))
        if cached is not None:
            read_mode = "cache"
        elif object_record.kind not in _DATA_KINDS:
            read_mode = "immediate"
        else:
            read_mode = self._archive_stores.require(store).read_mode()
        return {
            "collection_id": collection_id,
            "source_store": store,
            "object_id": object_record.object_id,
            "kind": object_record.kind,
            "plaintext_bytes": object_record.plaintext_bytes,
            "stored_bytes": object_record.stored_bytes,
            "sha256": object_record.sha256,
            "read_mode": read_mode,
            "placements": [],
        }

    def _select_copy(self, session: Session, collection_id: str) -> CollectionArchiveCopyRecord:
        copies = {
            copy.store: copy
            for copy in session.scalars(
                select(CollectionArchiveCopyRecord).where(
                    CollectionArchiveCopyRecord.collection_id == collection_id
                )
            )
            if archive_copy_is_complete(copy)
        }
        for store in self._config.archive_read_order:
            if store in copies:
                return copies[store]
        raise InvalidState(f"collection has no readable archive copy: {collection_id}")

    def _require_job_data_object(
        self,
        session: Session,
        *,
        app: str,
        job_id: str,
        collection_id: str,
        object_id: str,
        key_id: str | None = None,
    ) -> tuple[CollectionArchiveObjectRecord, RetrievalCacheObjectRecord | None]:
        job = self._require_job(session, app=app, key_id=key_id, job_id=job_id)
        self._expire_job_if_due(session, job)
        if job.state != "ready":
            raise InvalidState("retrieval job is not ready")
        planned = session.scalar(
            select(RetrievalJobObjectRecord).where(
                RetrievalJobObjectRecord.job_id == job_id,
                RetrievalJobObjectRecord.collection_id == collection_id,
                RetrievalJobObjectRecord.object_id == object_id,
            )
        )
        if planned is None:
            raise NotFound("archive object is not part of this retrieval job")
        object_record = session.get(
            CollectionArchiveObjectRecord,
            (collection_id, planned.source_store, object_id),
        )
        if object_record is None or object_record.kind not in _DATA_KINDS:
            raise NotFound("retrieval data object is not present")
        cached = session.get(
            RetrievalCacheObjectRecord,
            (planned.source_store, collection_id, object_id),
        )
        return object_record, cached

    def _read_archive_object(
        self,
        *,
        source_store: str,
        collection_id: str,
        identity: ArchiveObjectIdentity,
        cached: RetrievalCacheObjectRecord | None,
        attribution: DownloadAttribution | None,
    ) -> Iterable[bytes]:
        if cached is not None:
            if self._cache is None:
                raise RuntimeError("retrieval cache is unavailable")
            return iter_decrypt_age_scrypt(
                self._cache.iter_object(
                    object_path=cached.object_path,
                    version_id=cached.version_id,
                    expected_bytes=cached.stored_bytes,
                    expected_sha256=cached.stored_sha256,
                ),
                self._config.archive_passphrase,
            )
        return self._archive_stores.require(source_store).iter_archive_object(
            collection_id=collection_id,
            object=identity,
            attribution=attribution,
        )

    def _request_job_objects(self, job_id: str) -> None:
        with session_scope(self._session_factory) as session:
            job = session.get(RetrievalJobRecord, job_id)
            if job is None or job.state != "requested":
                return
            groups = self._missing_groups(session, job)
            requested_at = job.requested_at or job.created_at
        estimated_ready = format_utc_timestamp(
            parse_utc_timestamp(requested_at) + self._config.retrieval_estimated_latency
        )
        for (store_name, collection_id), objects in groups.items():
            self._archive_stores.require(store_name).prepare_archive_objects_read(
                collection_id=collection_id,
                objects=objects,
                retrieval_tier=self._config.retrieval_tier,
                hold_days=max(1, self._config.retrieval_max_lease.days),
                requested_at=requested_at,
                estimated_ready_at=estimated_ready,
            )

    def _process_one(self, job_id: str) -> None:
        with session_scope(self._session_factory) as session:
            job = session.get(RetrievalJobRecord, job_id)
            if job is None or job.state != "requested":
                return
            groups = self._missing_groups(session, job)
            requested_at = job.requested_at or job.created_at
            plan = json.loads(job.constraints_json)
            attribution = _download_attribution(job)
            lease_seconds = int(plan["lease_seconds"])
            pending_expires_at = format_utc_timestamp(
                utc_now()
                + self._config.retrieval_estimated_latency
                + self._config.retrieval_max_lease
            )
            for current in job.objects:
                if current.read_mode != "restore_required":
                    continue
                if (
                    session.get(
                        RetrievalCacheObjectRecord,
                        (current.source_store, current.collection_id, current.object_id),
                    )
                    is not None
                ):
                    self._lease_cached_object(
                        session,
                        owner=_job_owner(job_id),
                        source_store=current.source_store,
                        collection_id=current.collection_id,
                        object_id=current.object_id,
                        expires_at=pending_expires_at,
                    )
        estimated_ready = format_utc_timestamp(
            parse_utc_timestamp(requested_at) + self._config.retrieval_estimated_latency
        )
        all_ready = True
        try:
            for (store_name, collection_id), objects in groups.items():
                store = self._archive_stores.require(store_name)
                status = store.get_archive_objects_read_status(
                    collection_id=collection_id,
                    objects=objects,
                    requested_at=requested_at,
                    estimated_ready_at=estimated_ready,
                    estimated_expires_at=None,
                )
                if status.state != "ready":
                    all_ready = False
                    continue
                if self._cache is None:
                    raise RuntimeError("retrieval cache is unavailable")
                for object_identity in objects:
                    receipt = self._cache.put(
                        source_store=store_name,
                        collection_id=collection_id,
                        object_id=object_identity.object_id,
                        content=store.iter_stored_archive_object(
                            collection_id=collection_id,
                            object=object_identity,
                            attribution=attribution,
                        ),
                        content_length=object_identity.stored_bytes,
                    )
                    with session_scope(self._session_factory) as session:
                        session.merge(
                            RetrievalCacheObjectRecord(
                                source_store=store_name,
                                collection_id=collection_id,
                                object_id=object_identity.object_id,
                                object_path=receipt.object_path,
                                version_id=receipt.version_id,
                                stored_bytes=receipt.stored_bytes,
                                stored_sha256=receipt.stored_sha256,
                                cached_at=receipt.cached_at,
                                verified_at=receipt.verified_at,
                            )
                        )
                        session.flush()
                        self._lease_cached_object(
                            session,
                            owner=_job_owner(job_id),
                            source_store=store_name,
                            collection_id=collection_id,
                            object_id=object_identity.object_id,
                            expires_at=pending_expires_at,
                        )
            if all_ready:
                now = utc_now()
                expires_at = format_utc_timestamp(now + timedelta(seconds=lease_seconds))
                with session_scope(self._session_factory) as session:
                    job = session.get(RetrievalJobRecord, job_id)
                    if job is None or job.state != "requested":
                        return
                    for current in job.objects:
                        if current.read_mode == "restore_required":
                            self._lease_cached_object(
                                session,
                                owner=_job_owner(job_id),
                                source_store=current.source_store,
                                collection_id=current.collection_id,
                                object_id=current.object_id,
                                expires_at=expires_at,
                            )
                    job.state = "ready"
                    job.ready_at = format_utc_timestamp(now)
                    job.expires_at = expires_at
                    job.next_poll_at = None
                    job.failure = None
                    self._lifecycle_events.emit_retrieval(
                        type="retrieval.ready",
                        job=job,
                        details={"expires_at": expires_at},
                        session=session,
                    )
            else:
                with session_scope(self._session_factory) as session:
                    job = session.get(RetrievalJobRecord, job_id)
                    if job is not None and job.state == "requested":
                        job.next_poll_at = format_utc_timestamp(
                            utc_now() + self._config.retrieval_sweep_interval
                        )
        except Exception as exc:
            with session_scope(self._session_factory) as session:
                job = session.get(RetrievalJobRecord, job_id)
                if job is not None and job.state == "requested":
                    failure = str(exc) or exc.__class__.__name__
                    changed = job.failure != failure
                    job.failure = failure
                    job.next_poll_at = format_utc_timestamp(
                        utc_now() + self._config.retrieval_sweep_interval
                    )
                    if changed:
                        self._lifecycle_events.emit_retrieval(
                            type="retrieval.issue",
                            job=job,
                            details={"error": failure},
                            session=session,
                        )

    def _missing_groups(
        self,
        session: Session,
        job: RetrievalJobRecord,
    ) -> dict[tuple[str, str], list[ArchiveObjectIdentity]]:
        groups: dict[tuple[str, str], list[ArchiveObjectIdentity]] = {}
        for current in job.objects:
            if current.read_mode != "restore_required":
                continue
            cache_key = (current.source_store, current.collection_id, current.object_id)
            if session.get(RetrievalCacheObjectRecord, cache_key) is not None:
                continue
            object_record = session.get(
                CollectionArchiveObjectRecord,
                (current.collection_id, current.source_store, current.object_id),
            )
            if object_record is None:
                raise InvalidState("retrieval archive object is missing")
            groups.setdefault((current.source_store, current.collection_id), []).append(
                _object_identity(object_record)
            )
        return groups

    @staticmethod
    def _lease_cached_object(
        session: Session,
        *,
        owner: str,
        source_store: str,
        collection_id: str,
        object_id: str,
        expires_at: str,
    ) -> None:
        cached = session.get(
            RetrievalCacheObjectRecord,
            (source_store, collection_id, object_id),
        )
        if cached is None:
            raise InvalidState("planned retrieval cache object is missing")
        session.merge(
            RetrievalCacheLeaseRecord(
                owner=owner,
                source_store=source_store,
                collection_id=collection_id,
                object_id=object_id,
                expires_at=expires_at,
            )
        )

    @staticmethod
    def _require_job(
        session: Session,
        *,
        app: str,
        job_id: str,
        key_id: str | None = None,
    ) -> RetrievalJobRecord:
        record = session.get(RetrievalJobRecord, job_id)
        if (
            record is None
            or record.app != app
            or (key_id is not None and record.initiated_by_key_id != key_id)
        ):
            raise NotFound(f"retrieval job not found: {job_id}")
        return record

    def _expire_job_if_due(self, session: Session, job: RetrievalJobRecord) -> None:
        if (
            job.state == "ready"
            and job.expires_at is not None
            and parse_utc_timestamp(job.expires_at) <= utc_now()
        ):
            job.state = "expired"
            self._lifecycle_events.emit_retrieval(
                type="retrieval.expired",
                job=job,
                terminal=True,
                session=session,
            )
            session.execute(
                delete(RetrievalCacheLeaseRecord).where(
                    RetrievalCacheLeaseRecord.owner == _job_owner(job.id)
                )
            )
            _release_job_reservation(session, job.id)


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _normalize_file_refs(files: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    normalized: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for collection_id, path in files:
        current = (str(collection_id).strip(), str(path).strip())
        if not current[0] or not current[1]:
            raise BadRequest("retrieval file references require collection_id and path")
        if current in seen:
            continue
        seen.add(current)
        normalized.append(current)
    if not normalized:
        raise BadRequest("retrieval requires at least one file")
    return tuple(normalized)


def _object_identity(row: CollectionArchiveObjectRecord) -> ArchiveObjectIdentity:
    return ArchiveObjectIdentity(
        object_id=row.object_id,
        kind=row.kind,
        object_path=row.object_path,
        plaintext_bytes=row.plaintext_bytes,
        stored_bytes=row.stored_bytes,
        sha256=row.sha256,
    )


def _job_owner(job_id: str) -> str:
    return f"job:{job_id}"


def _download_attribution(job: RetrievalJobRecord) -> DownloadAttribution | None:
    if job.initiated_by_key_id is None:
        return None
    return DownloadAttribution(key_id=job.initiated_by_key_id, job_id=job.id)


def _release_job_reservation(session: Session, job_id: str) -> None:
    from riverhog_core.catalog_models import KeyDownloadReservationRecord

    session.execute(
        delete(KeyDownloadReservationRecord).where(
            KeyDownloadReservationRecord.job_id == job_id,
            KeyDownloadReservationRecord.kind == "job",
        )
    )


def _job_payload(record: RetrievalJobRecord) -> dict[str, object]:
    plan = json.loads(record.constraints_json)
    return {
        "id": record.id,
        "state": record.state,
        "plan_etag": record.plan_etag,
        "created_at": record.created_at,
        "requested_at": record.requested_at,
        "ready_at": record.ready_at,
        "expires_at": record.expires_at,
        "completed_at": record.completed_at,
        "canceled_at": record.canceled_at,
        "failure": record.failure,
        "files": plan["files"],
        "objects": plan["objects"],
    }
