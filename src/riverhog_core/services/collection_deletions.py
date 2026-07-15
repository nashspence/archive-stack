from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from riverhog_core.archive_custody import ARCHIVE_CUSTODY_WARNING
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyJobRecord,
    ArchiveCopyRetirementRecord,
    ArchiveRestoreCollectionRecord,
    ArchiveRestoreRecord,
    CollectionArchiveCopyRecord,
    CollectionDeletionRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    FetchCollectionRecord,
    FetchRecord,
)
from riverhog_core.domain.enums import ArchiveRestoreState, FetchState
from riverhog_core.domain.errors import BadRequest, Conflict, InvalidState, NotFound
from riverhog_core.ports.hot_store import HotStore
from riverhog_core.ports.upload_store import UploadStore
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_catalog import publish_archive_restore_catalog
from riverhog_core.services.collections import (
    _collection_upload_target_path,
    _normalize_collection_id_or_raise,
)
from riverhog_core.timestamps import format_utc_timestamp, utc_now

_PLAN_TTL = timedelta(minutes=15)
_CHALLENGE_RE = re.compile(r"^delete-(\d+)-([0-9a-f]{64})$")
_ACTIVE_RESTORE_STATES = {
    ArchiveRestoreState.REQUESTED.value,
    ArchiveRestoreState.READY.value,
}
_ACTIVE_FETCH_STATES = {
    FetchState.QUEUED_ARCHIVE.value,
    FetchState.RESTORING_ARCHIVE.value,
}


class SqlAlchemyCollectionDeletionService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        hot_store: HotStore,
        upload_store: UploadStore,
    ) -> None:
        self._archive_stores = archive_stores
        self._hot_store = hot_store
        self._upload_store = upload_store
        self._session_factory = make_session_factory(config.database_url)

    def plan(self, collection_id: str) -> dict[str, object]:
        normalized_id = _normalize_collection_id_or_raise(collection_id)
        with session_scope(self._session_factory) as session:
            active = session.get(CollectionDeletionRecord, normalized_id)
            if active is not None:
                return cast(dict[str, object], json.loads(active.plan_json))
            expires = (utc_now() + _PLAN_TTL).replace(microsecond=0)
            plan = _build_plan(
                session,
                hot_store=self._hot_store,
                collection_id=normalized_id,
                expires_at=expires,
            )
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
                return {
                    "status": "already_absent",
                    "collection_id": normalized_id,
                    "files": 0,
                    "bytes": 0,
                    "remote_storage_bytes": 0,
                }
            else:
                expires = _challenge_expiry(supplied_challenge)
                if utc_now() > expires:
                    raise Conflict("collection deletion plan has expired; request a new plan")
                plan = _build_plan(
                    session,
                    hot_store=self._hot_store,
                    collection_id=normalized_id,
                    expires_at=expires,
                )
                expected_challenge = _plan_challenge(plan, expires)
                if not secrets.compare_digest(expected_challenge, supplied_challenge):
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

        self._delete_hot_objects(plan)
        self._delete_upload_remnants(normalized_id)
        self._delete_archive_package(plan)
        for store_name, archive_store in self._archive_stores.items():
            publish_archive_restore_catalog(
                store_name=store_name,
                archive_store=archive_store,
                session_factory=self._session_factory,
                excluded_collection_ids={normalized_id},
            )
        return self._finish(normalized_id, supplied_challenge, plan)

    def _delete_hot_objects(self, plan: dict[str, object]) -> None:
        collection_id = str(plan["collection_id"])
        for item in cast(list[dict[str, object]], plan["hot_objects"]):
            try:
                self._hot_store.delete_collection_file(collection_id, str(item["path"]))
            except FileNotFoundError:
                pass

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

    def _delete_archive_package(self, plan: dict[str, object]) -> None:
        objects_by_store: dict[str, dict[str, str]] = {}
        for item in cast(list[dict[str, object]], plan["archive_objects"]):
            objects_by_store.setdefault(str(item["store"]), {})[str(item["kind"])] = str(
                item["object_path"]
            )
        for store_name, objects in objects_by_store.items():
            self._archive_stores.require(store_name).delete_collection_archive_package(
                collection_id=str(plan["collection_id"]),
                object_path=objects["archive"],
                manifest_object_path=objects["manifest"],
                proof_object_path=objects["proof"],
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
            newly_active = session.scalars(
                select(ArchiveRestoreRecord.restore_id)
                .join(ArchiveRestoreCollectionRecord)
                .where(
                    ArchiveRestoreCollectionRecord.collection_id == collection_id,
                    ArchiveRestoreRecord.state.in_(_ACTIVE_RESTORE_STATES),
                )
                .order_by(ArchiveRestoreRecord.restore_id)
            ).all()
            if newly_active:
                raise Conflict(
                    "archive restore became active during collection deletion: "
                    + ", ".join(newly_active)
                )
            active_copy_jobs = session.execute(
                select(
                    ArchiveCopyJobRecord.source_store,
                    ArchiveCopyJobRecord.destination_store,
                ).where(ArchiveCopyJobRecord.collection_id == collection_id)
            ).all()
            if active_copy_jobs:
                raise Conflict(
                    "archive copy became active during collection deletion: "
                    + ", ".join(
                        f"{source_store} -> {destination_store}"
                        for source_store, destination_store in active_copy_jobs
                    )
                )
            active_retirements = session.scalars(
                select(ArchiveCopyRetirementRecord.store)
                .where(ArchiveCopyRetirementRecord.collection_id == collection_id)
                .order_by(ArchiveCopyRetirementRecord.store)
            ).all()
            if active_retirements:
                raise Conflict(
                    "archive copy retirement became active during collection deletion: "
                    + ", ".join(active_retirements)
                )
            restores = session.scalars(
                select(ArchiveRestoreRecord)
                .join(ArchiveRestoreCollectionRecord)
                .where(ArchiveRestoreCollectionRecord.collection_id == collection_id)
            ).unique()
            for restore in restores:
                session.delete(restore)
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is not None:
                session.delete(upload)
            collection = session.get(CollectionRecord, collection_id)
            if collection is not None:
                session.delete(collection)
            session.delete(active)
        return _deletion_result(plan, status="deleted")


def _build_plan(
    session: Session,
    *,
    hot_store: HotStore,
    collection_id: str,
    expires_at: datetime,
) -> dict[str, object]:
    collection_exists = session.scalar(
        select(CollectionRecord.id).where(CollectionRecord.id == collection_id)
    )
    if collection_exists is None:
        raise NotFound(f"collection not found: {collection_id}")
    archives = session.scalars(
        select(CollectionArchiveCopyRecord)
        .where(CollectionArchiveCopyRecord.collection_id == collection_id)
        .order_by(CollectionArchiveCopyRecord.store)
    ).all()
    archive_copy_count, remote_storage_bytes = session.execute(
        select(
            func.count(CollectionArchiveCopyRecord.store),
            func.coalesce(
                func.sum(
                    func.coalesce(CollectionArchiveCopyRecord.stored_bytes, 0)
                    + func.coalesce(CollectionArchiveCopyRecord.manifest_stored_bytes, 0)
                    + func.coalesce(CollectionArchiveCopyRecord.ots_stored_bytes, 0)
                ),
                0,
            ),
        ).where(CollectionArchiveCopyRecord.collection_id == collection_id)
    ).one()
    if not archives or any(
        archive.state != "uploaded"
        or archive.last_verified_at is None
        or archive.object_path is None
        or archive.manifest_object_path is None
        or archive.ots_object_path is None
        for archive in archives
    ):
        raise InvalidState(
            f"collection archive copies are not completely uploaded and verified: {collection_id}"
        )

    upload = session.get(CollectionUploadRecord, collection_id)
    restore_query = (
        select(
            ArchiveRestoreRecord.restore_id,
            ArchiveRestoreRecord.state,
        )
        .join(ArchiveRestoreCollectionRecord)
        .where(ArchiveRestoreCollectionRecord.collection_id == collection_id)
    )
    restore_ids = session.scalars(
        select(ArchiveRestoreRecord.restore_id)
        .join(ArchiveRestoreCollectionRecord)
        .where(ArchiveRestoreCollectionRecord.collection_id == collection_id)
        .order_by(ArchiveRestoreRecord.restore_id)
    ).all()
    active_restore_ids = session.scalars(
        select(ArchiveRestoreRecord.restore_id)
        .join(ArchiveRestoreCollectionRecord)
        .where(
            ArchiveRestoreCollectionRecord.collection_id == collection_id,
            ArchiveRestoreRecord.state.in_(_ACTIVE_RESTORE_STATES),
        )
        .order_by(ArchiveRestoreRecord.restore_id)
    ).all()
    restore_count = int(
        session.scalar(select(func.count()).select_from(restore_query.subquery())) or 0
    )
    active_fetch_ids = _active_fetch_ids(session, collection_id)
    archive_copy_jobs = session.execute(
        select(
            ArchiveCopyJobRecord.source_store,
            ArchiveCopyJobRecord.destination_store,
        )
        .where(ArchiveCopyJobRecord.collection_id == collection_id)
        .order_by(ArchiveCopyJobRecord.destination_store)
    ).all()
    archive_copy_retirements = session.scalars(
        select(ArchiveCopyRetirementRecord.store)
        .where(ArchiveCopyRetirementRecord.collection_id == collection_id)
        .order_by(ArchiveCopyRetirementRecord.store)
    ).all()
    blockers: list[str] = []
    if upload is not None and upload.state not in {"canceled", "expired"}:
        blockers.append(f"collection upload is active: {upload.state or 'unknown'}")
    blockers.extend(f"archive restore is active: {restore_id}" for restore_id in active_restore_ids)
    blockers.extend(f"fetch is active: {fetch_id}" for fetch_id in active_fetch_ids)
    blockers.extend(
        f"archive copy is active: {source_store} -> {destination_store}"
        for source_store, destination_store in archive_copy_jobs
    )
    blockers.extend(
        f"archive copy retirement is active: {store}" for store in archive_copy_retirements
    )

    file_rows = session.execute(
        select(
            CollectionFileRecord.path,
            CollectionFileRecord.bytes,
            CollectionFileRecord.hot,
        )
        .where(CollectionFileRecord.collection_id == collection_id)
        .order_by(CollectionFileRecord.path)
    ).all()
    file_count, file_bytes = session.execute(
        select(
            func.count(CollectionFileRecord.path),
            func.coalesce(func.sum(CollectionFileRecord.bytes), 0),
        ).where(CollectionFileRecord.collection_id == collection_id)
    ).one()
    files = [
        {
            "path": file.path,
            "bytes": file.bytes,
            "hot": file.hot,
        }
        for file in file_rows
    ]
    hot_listing = hot_store.list_collection_files(collection_id)
    hot_objects = [{"path": file.path, "bytes": file.bytes} for file in hot_listing.files]
    upload_file_rows = session.execute(
        select(
            CollectionUploadFileRecord.path,
            CollectionUploadFileRecord.bytes,
        )
        .where(CollectionUploadFileRecord.collection_id == collection_id)
        .order_by(
            CollectionUploadFileRecord.file_order,
            CollectionUploadFileRecord.path,
        )
    ).all()
    upload_file_count = int(
        session.scalar(
            select(func.count(CollectionUploadFileRecord.path)).where(
                CollectionUploadFileRecord.collection_id == collection_id
            )
        )
        or 0
    )
    upload_files = [{"path": file.path, "bytes": file.bytes} for file in upload_file_rows]
    archive_objects = [
        {
            "store": archive.store,
            "kind": kind,
            "object_path": object_path,
            "stored_bytes": int(stored_bytes or 0),
        }
        for archive in archives
        for kind, object_path, stored_bytes in (
            ("archive", archive.object_path, archive.stored_bytes),
            ("manifest", archive.manifest_object_path, archive.manifest_stored_bytes),
            ("proof", archive.ots_object_path, archive.ots_stored_bytes),
        )
    ]
    return {
        "status": "blocked" if blockers else "ready",
        "collection_id": collection_id,
        "warning": ARCHIVE_CUSTODY_WARNING,
        "expires_at": format_utc_timestamp(expires_at),
        "files": files,
        "file_count": int(file_count),
        "bytes": int(file_bytes),
        "hot_objects": hot_objects,
        "hot_files": hot_listing.file_count,
        "hot_bytes": hot_listing.total_bytes,
        "archive_objects": archive_objects,
        "remote_storage_bytes": int(remote_storage_bytes),
        "upload_files": upload_files,
        "archive_restores": list(restore_ids),
        "metadata_rows": {
            "collections": 1,
            "collection_files": int(file_count),
            "collection_archive_copies": int(archive_copy_count),
            "archive_copy_jobs": len(archive_copy_jobs),
            "archive_copy_retirements": len(archive_copy_retirements),
            "collection_uploads": int(upload is not None),
            "collection_upload_files": upload_file_count,
            "archive_restores": restore_count,
            "archive_restore_collections": restore_count,
            "encrypted_restore_catalog_entries": int(archive_copy_count),
        },
        "blockers": blockers,
        "billing_note": (
            "Measured remote bytes are catalog values. Provider retention, object versions, "
            "minimum-storage duration, and billing timing can affect realized savings."
        ),
    }


def _active_fetch_ids(session: Session, collection_id: str) -> list[str]:
    return list(
        session.scalars(
            select(FetchRecord.fetch_id)
            .join(FetchCollectionRecord)
            .where(
                FetchRecord.fetch_state.in_(_ACTIVE_FETCH_STATES),
                FetchCollectionRecord.collection_id == collection_id,
            )
            .order_by(FetchRecord.fetch_id)
        ).all()
    )


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
