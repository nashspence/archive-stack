from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, TypedDict

from riverhog_age import plaintext_bytes_for_ciphertext_offset
from riverhog_protocol.errors import BadRequest, Conflict, HashMismatch, NotFound
from riverhog_protocol.manifest import collection_content_etag_ordered
from riverhog_protocol.paths import (
    PathNormalizationError,
    normalize_collection_id,
    normalize_relpath,
    normalize_tag,
)
from sqlalchemy import String, and_, case, cast, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql.elements import ColumnElement
from time_formats import format_utc_timestamp, parse_utc_timestamp, utc_now, utc_timestamp_now

from riverhog_core.app_permissions import CATALOG_READ, COLLECTIONS_CREATE, ApplicationPrincipal
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectUploadRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionTagRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    TagRecord,
)
from riverhog_core.collection_access import (
    collection_access_filter,
    require_collection_create_access,
)
from riverhog_core.domain.enums import ArchiveState
from riverhog_core.domain.models import (
    ArchiveCopyStatus,
    CollectionListPage,
    CollectionManifestStatus,
    CollectionSummary,
)
from riverhog_core.domain.types import CollectionId, Sha256Hex
from riverhog_core.ingress_crypto import (
    create_ingress_encryption,
    ingress_encryption_descriptor,
    iter_ingress_plaintext,
)
from riverhog_core.ports.upload_store import UploadStore
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_records import (
    ArchiveCopyAggregate,
    archive_copy_aggregates,
)
from riverhog_core.services.lifecycle_events import (
    SqlAlchemyLifecycleEventService,
    event_context_json,
)
from riverhog_core.services.resumable_uploads import (
    UploadLifecycleState,
    create_or_resume_upload_state,
    expire_upload_state,
    sync_upload_state,
    upload_expiry_timestamp,
    upload_state_name,
)
from riverhog_core.tusd_ids import tusd_upload_id_for_target_path

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LOG = logging.getLogger(__name__)
_UPLOAD_FORGET_WORKERS = 4
_UPLOAD_SYNC_WORKERS = 4
_UPLOAD_EXPIRY_SWEEP_LIMIT = 100
_COLLECTION_SORT_FIELDS = {
    "id",
    "created_at",
    "bytes",
    "files",
}


class _UploadManifestEntry(TypedDict):
    path: str
    bytes: int
    sha256: str


class _CollectionUploadStats(TypedDict):
    files_total: int
    files_pending: int
    files_partial: int
    files_uploaded: int
    bytes_total: int
    uploaded_bytes: int
    missing_bytes: int
    upload_state_expires_at: str | None


class SqlAlchemyCollectionService:
    def __init__(
        self,
        config: RuntimeConfig,
        upload_store: UploadStore,
    ) -> None:
        self._config = config
        self._upload_store = upload_store
        self._upload_file_ttl = config.upload_file_ttl
        self._upload_session_idle_ttl = config.upload_session_idle_ttl
        self._session_factory = make_session_factory(config.database_url)
        self._lifecycle_events = SqlAlchemyLifecycleEventService(config)

    def create_or_resume_upload_session(
        self,
        *,
        idempotency_key: str,
        tags: Sequence[str],
        ingest_source: str | None = None,
        archive_store: str | None = None,
        initiator: ApplicationPrincipal | None = None,
        event_context: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        normalized_idempotency_key = _normalize_idempotency_key(idempotency_key)
        normalized_tags = _normalize_tags(tags)
        normalized_archive_store = self._normalize_archive_store(archive_store)
        initiator_app = initiator.app if initiator is not None else "riverhog"
        initiator_key_id = initiator.key_id if initiator is not None else None
        normalized_event_context_json = event_context_json(event_context)
        require_collection_create_access(initiator, COLLECTIONS_CREATE, normalized_tags)

        with session_scope(self._session_factory) as session:
            _require_tags(session, normalized_tags)
            collection = session.scalar(
                select(CollectionRecord)
                .options(selectinload(CollectionRecord.archive_copies))
                .where(CollectionRecord.creation_idempotency_key == normalized_idempotency_key)
            )
            if collection is not None:
                _ensure_finalized_request_matches(
                    session,
                    collection=collection,
                    tags=normalized_tags,
                    initiator_app=initiator_app,
                )
                return _finalized_collection_upload_payload(
                    session,
                    collection,
                    archive_store=normalized_archive_store,
                )
            upload = session.scalar(
                select(CollectionUploadRecord).where(
                    CollectionUploadRecord.idempotency_key == normalized_idempotency_key
                )
            )

            if upload is not None:
                _ensure_upload_initiator_matches(upload, initiator_app)
                _ensure_event_context_matches(upload, normalized_event_context_json)
                _ensure_upload_tags_match(upload, normalized_tags)
                _ensure_upload_archive_store_matches(upload, normalized_archive_store)
                _expire_open_collection_upload_if_idle(
                    upload,
                    session_idle_ttl=self._upload_session_idle_ttl,
                )
            if upload is not None:
                if upload.state == "open":
                    upload.ingest_source = ingest_source
                    _touch_collection_upload(upload)
                    return _collection_upload_payload(
                        session=session,
                        upload=upload,
                        state="open",
                        collection=None,
                    )
                if upload.state in {"canceled", "expired"}:
                    raise Conflict(
                        f"collection upload session is {upload.state}: {upload.collection_id}"
                    )
                return _collection_upload_payload(
                    session=session,
                    upload=upload,
                    state=None,
                    collection=None,
                )

            now = utc_timestamp_now()
            upload = CollectionUploadRecord(
                idempotency_key=normalized_idempotency_key,
                tags_json=_tags_json(normalized_tags),
                ingest_source=ingest_source,
                initiated_by_app=initiator_app,
                initiated_by_key_id=initiator_key_id,
                event_context_json=normalized_event_context_json,
                archive_store=normalized_archive_store,
                state="open",
                opened_at=now,
                last_activity_at=now,
            )
            session.add(upload)
            session.flush()
            return _collection_upload_payload(
                session=session,
                upload=upload,
                state="open",
                collection=None,
            )

    def _normalize_archive_store(self, value: str | None) -> str:
        store = value or self._config.archive_write_store
        try:
            self._config.archive_store(store)
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        return store

    def register_upload_session_files(
        self,
        collection_id: int,
        files: Sequence[dict[str, object]],
    ) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)
        normalized_files = _normalize_upload_files(files)
        if not normalized_files:
            raise BadRequest("collection upload file batch cannot be empty")
        if len(normalized_files) > 100:
            raise BadRequest("collection upload file batch cannot exceed 100 files")

        with session_scope(self._session_factory) as session:
            upload: CollectionUploadRecord | None = None
            file_records: list[CollectionUploadFileRecord] = []
            for normalized_file in normalized_files:
                upload, file_record = self._register_upload_session_file_record(
                    session,
                    normalized_collection_id=normalized_collection_id,
                    normalized_file=normalized_file,
                )
                file_records.append(file_record)
            assert upload is not None
            _touch_collection_upload(upload)
            return _collection_upload_files_registration_payload(upload, file_records)

    def list_upload_session_files(
        self,
        collection_id: int,
        *,
        page: int,
        per_page: int,
        all_items: bool,
    ) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)
        if page < 1:
            raise BadRequest("page must be at least 1")
        if per_page < 1 or per_page > 100:
            raise BadRequest("per_page must be between 1 and 100")
        with session_scope(self._session_factory) as session:
            if session.get(CollectionUploadRecord, normalized_collection_id) is None:
                raise NotFound(f"collection upload session not found: {normalized_collection_id}")
            total = int(
                session.scalar(
                    select(func.count(CollectionUploadFileRecord.path)).where(
                        CollectionUploadFileRecord.collection_id == normalized_collection_id
                    )
                )
                or 0
            )
            stmt = (
                select(CollectionUploadFileRecord)
                .where(CollectionUploadFileRecord.collection_id == normalized_collection_id)
                .order_by(CollectionUploadFileRecord.path)
            )
            if not all_items:
                stmt = stmt.offset((page - 1) * per_page).limit(per_page)
            rows = session.scalars(stmt).all()
            effective_per_page = total if all_items and total else per_page
            return {
                "page": 1 if all_items else page,
                "per_page": effective_per_page,
                "total": total,
                "pages": 1 if all_items else max(1, (total + per_page - 1) // per_page),
                "files": [_collection_upload_file_payload(row) for row in rows],
            }

    def require_upload_access(
        self,
        collection_id: int,
        principal: ApplicationPrincipal,
    ) -> None:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is not None:
                require_collection_create_access(
                    principal,
                    COLLECTIONS_CREATE,
                    _upload_tags(upload),
                )
                return

            collection = session.get(CollectionRecord, normalized_collection_id)
            if collection is None or collection.created_by_app != principal.app:
                raise NotFound(f"collection upload not found: {normalized_collection_id}")
            require_collection_create_access(
                principal,
                COLLECTIONS_CREATE,
                tuple(
                    session.scalars(
                        select(CollectionTagRecord.tag_id)
                        .where(CollectionTagRecord.collection_id == collection.id)
                        .order_by(CollectionTagRecord.tag_id)
                    ).all()
                ),
            )

    def create_or_resume_registered_file_upload(
        self,
        collection_id: int,
        file: dict[str, object],
    ) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)
        normalized_file = _normalize_upload_files([file])[0]

        with session_scope(self._session_factory) as session:
            upload, file_record = self._register_upload_session_file_record(
                session,
                normalized_collection_id=normalized_collection_id,
                normalized_file=normalized_file,
            )
            target_path = _collection_upload_target_path(file_record)
            _expire_collection_upload_file(file_record, self._upload_store)
            updated, tus_url = create_or_resume_upload_state(
                current=_upload_lifecycle_state(file_record),
                target_path=target_path,
                length=file_record.ingress_bytes,
                upload_store=self._upload_store,
                ttl=self._upload_file_ttl,
            )
            _apply_upload_lifecycle_state(file_record, updated)

            return {
                **_collection_upload_file_registration_payload(upload, file_record),
                **_collection_file_upload_payload(self._config, file_record, tus_url=tus_url),
            }

    def sync_finished_upload_id(self, upload_id: str) -> dict[str, object] | None:
        staged_event_details: dict[str, object] | None = None

        with session_scope(self._session_factory) as session:
            file_record = session.scalar(
                select(CollectionUploadFileRecord).where(
                    CollectionUploadFileRecord.ingress_upload_id == upload_id
                )
            )
            if file_record is None:
                return None
            normalized_collection_id = file_record.collection_id
            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None or upload.state in {"canceled", "expired"}:
                return None
            target_path = _collection_upload_target_path(file_record)
            content_digest = _sha256_hex_chunks(
                iter_ingress_plaintext(
                    self._config,
                    self._upload_store,
                    target_path=target_path,
                    collection_id=normalized_collection_id,
                    path=file_record.path,
                    plaintext_bytes=file_record.bytes,
                    secret_envelope=file_record.ingress_secret_envelope,
                    state_json=file_record.ingress_state_json,
                )
            )
            if content_digest != file_record.sha256:
                self._upload_store.delete_target(target_path)
                file_record.ingress_uploaded_bytes = 0
                file_record.tus_url = None
                raise HashMismatch("sha256 did not match expected file hash")
            file_record.ingress_uploaded_bytes = file_record.ingress_bytes
            file_record.upload_expires_at = None
            if upload.state != "open" and _collection_upload_is_complete_for_session(
                session,
                normalized_collection_id,
            ):
                was_archiving = upload.state == "archiving"
                _ensure_collection_upload_archiving(upload)
                if not was_archiving:
                    staged_event_details = _collection_upload_event_details(session, upload)
            payload = _collection_upload_file_registration_payload(upload, file_record)
        if staged_event_details is not None:
            self._lifecycle_events.emit_collection(
                type="collection.upload_staged",
                collection_id=normalized_collection_id,
                details=staged_event_details,
            )
        return payload

    def collection_id_for_upload_id(self, upload_id: str) -> int | None:
        with session_scope(self._session_factory) as session:
            return session.scalar(
                select(CollectionUploadFileRecord.collection_id).where(
                    CollectionUploadFileRecord.ingress_upload_id == upload_id
                )
            )

    def _register_upload_session_file_record(
        self,
        session: Session,
        *,
        normalized_collection_id: int,
        normalized_file: _UploadManifestEntry,
    ) -> tuple[CollectionUploadRecord, CollectionUploadFileRecord]:
        upload = session.get(CollectionUploadRecord, normalized_collection_id)
        if upload is None:
            raise NotFound(f"collection upload session not found: {normalized_collection_id}")
        _expire_open_collection_upload_if_idle(
            upload,
            session_idle_ttl=self._upload_session_idle_ttl,
        )
        if upload.state != "open":
            raise Conflict(f"collection upload session is not open: {normalized_collection_id}")

        existing = session.get(
            CollectionUploadFileRecord,
            (normalized_collection_id, normalized_file["path"]),
        )
        if existing is not None:
            current = _manifest_entry_payload(existing)
            if current != normalized_file:
                raise Conflict(
                    "collection upload session file already exists with different metadata: "
                    f"{normalized_file['path']}"
                )
            return upload, existing

        file_order = (
            session.scalar(
                select(func.max(CollectionUploadFileRecord.file_order)).where(
                    CollectionUploadFileRecord.collection_id == normalized_collection_id
                )
            )
            or 0
        ) + 1
        file_record = _new_collection_upload_file(
            self._config,
            collection_id=normalized_collection_id,
            path=normalized_file["path"],
            file_order=file_order,
            bytes=normalized_file["bytes"],
            sha256=normalized_file["sha256"],
        )
        session.add(file_record)
        return upload, file_record

    def complete_upload_session(
        self,
        collection_id: int,
        *,
        files_total: int,
        content_etag: str,
    ) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)
        if files_total < 1:
            raise BadRequest("files_total must be at least 1")
        if not _SHA256_RE.fullmatch(content_etag):
            raise BadRequest("content_etag must be lowercase sha256 hex")
        staged_event_details: dict[str, object] | None = None
        missing_file_bytes = False
        payload: dict[str, object] | None = None

        with session_scope(self._session_factory) as session:
            collection = session.get(CollectionRecord, normalized_collection_id)
            if collection is not None:
                finalized_files_total = int(
                    session.scalar(
                        select(func.count(CollectionFileRecord.path)).where(
                            CollectionFileRecord.collection_id == normalized_collection_id
                        )
                    )
                    or 0
                )
                if finalized_files_total != files_total or collection.content_etag != content_etag:
                    raise Conflict(
                        "collection upload completion identity differs from finalized collection"
                    )
                return _finalized_collection_upload_payload(
                    session,
                    collection,
                    archive_store=self._config.archive_write_store,
                )

            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None:
                raise NotFound(f"collection upload session not found: {normalized_collection_id}")
            if upload.state in {"canceled", "expired"}:
                raise Conflict(
                    f"collection upload session is {upload.state}: {normalized_collection_id}"
                )
            stats = _collection_upload_stats(session, normalized_collection_id)
            if stats["files_total"] != files_total:
                raise Conflict(
                    "collection upload registered file count differs from completion request"
                )
            actual_content_etag = collection_content_etag_ordered(
                (
                    row.path,
                    row.bytes,
                    row.sha256,
                )
                for row in session.scalars(
                    select(CollectionUploadFileRecord)
                    .where(CollectionUploadFileRecord.collection_id == normalized_collection_id)
                    .order_by(CollectionUploadFileRecord.path)
                ).yield_per(1000)
            )
            if actual_content_etag != content_etag:
                raise Conflict(
                    "collection upload registered file manifest differs from completion request"
                )
            upload = _sync_and_expire_collection_upload(
                session,
                upload,
                upload_store=self._upload_store,
                session_idle_ttl=self._upload_session_idle_ttl,
                force_offset_sync=True,
            )
            if upload is None:
                raise NotFound(f"collection upload session not found: {normalized_collection_id}")
            if upload.state in {"archiving", "failed"}:
                return _collection_upload_payload(
                    session=session,
                    upload=upload,
                    state=None,
                    collection=None,
                )
            if upload.state != "open":
                raise Conflict(f"collection upload session is not open: {normalized_collection_id}")
            if not _collection_upload_is_complete_for_session(session, normalized_collection_id):
                missing_file_bytes = True
            else:
                _ensure_collection_upload_archiving(upload)
                now = utc_timestamp_now()
                upload.closed_at = now
                upload.last_activity_at = now
                staged_event_details = _collection_upload_event_details(session, upload)
                payload = _collection_upload_payload(
                    session=session,
                    upload=upload,
                    state=None,
                    collection=None,
                )
        if missing_file_bytes:
            raise Conflict("collection upload session still has missing file bytes")
        if staged_event_details is not None:
            self._lifecycle_events.emit_collection(
                type="collection.upload_staged",
                collection_id=normalized_collection_id,
                details=staged_event_details,
            )
        assert payload is not None
        return payload

    def cancel_upload_session(self, collection_id: int) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)

        with session_scope(self._session_factory) as session:
            if session.get(CollectionRecord, normalized_collection_id) is not None:
                raise Conflict(f"collection is already finalized: {normalized_collection_id}")
            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None:
                raise NotFound(f"collection upload session not found: {normalized_collection_id}")
            _expire_open_collection_upload_if_idle(
                upload,
                session_idle_ttl=self._upload_session_idle_ttl,
            )
            if upload.state in {"canceled", "expired"}:
                payload = _collection_upload_payload(
                    session=session,
                    upload=upload,
                    state=str(upload.state),
                    collection=None,
                )
                _forget_collection_upload(upload, self._upload_store)
                session.delete(upload)
                return payload
            if upload.state not in {"open", "uploading", None}:
                raise Conflict(
                    "collection upload session cannot be canceled after completion handoff"
                )

            _forget_collection_upload(upload, self._upload_store)
            upload.files.clear()
            now = utc_timestamp_now()
            upload.state = "canceled"
            upload.closed_at = now
            upload.last_activity_at = now
            payload = _collection_upload_payload(
                session=session,
                upload=upload,
                state="canceled",
                collection=None,
            )
            session.delete(upload)
            return payload

    def get_upload(self, collection_id: int) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None:
                collection = session.scalar(
                    select(CollectionRecord)
                    .options(selectinload(CollectionRecord.archive_copies))
                    .where(CollectionRecord.id == normalized_collection_id)
                )
                if collection is None:
                    raise NotFound(f"collection upload not found: {normalized_collection_id}")
                archive_store = (
                    collection.archive_copies[0].store
                    if collection.archive_copies
                    else self._config.archive_write_store
                )
                return _finalized_collection_upload_payload(
                    session,
                    collection,
                    archive_store=archive_store,
                )

            _expire_open_collection_upload_if_idle(
                upload,
                session_idle_ttl=self._upload_session_idle_ttl,
            )

            if upload.state == "open":
                return _collection_upload_payload(
                    session=session,
                    upload=upload,
                    state="open",
                    collection=None,
                )
            if upload.state in {"canceled", "expired"}:
                return _collection_upload_payload(
                    session=session,
                    upload=upload,
                    state=str(upload.state),
                    collection=None,
                )
            return _collection_upload_payload(
                session=session,
                upload=upload,
                state=None,
                collection=None,
            )

    def create_or_resume_file_upload(self, collection_id: int, path: str) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)
        normalized_path = _normalize_relpath_or_raise(path)

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None:
                raise NotFound(f"collection upload not found: {normalized_collection_id}")
            _expire_open_collection_upload_if_idle(
                upload,
                session_idle_ttl=self._upload_session_idle_ttl,
            )
            if upload.state not in {"open", "uploading", None}:
                raise Conflict(
                    f"collection upload is not accepting file bytes: {normalized_collection_id}"
                )

            file_record = _get_upload_file_record(
                session,
                normalized_collection_id,
                normalized_path,
            )
            target_path = _collection_upload_target_path(file_record)
            _expire_collection_upload_file(file_record, self._upload_store)
            updated, tus_url = create_or_resume_upload_state(
                current=_upload_lifecycle_state(file_record),
                target_path=target_path,
                length=file_record.ingress_bytes,
                upload_store=self._upload_store,
                ttl=self._upload_file_ttl,
            )
            _apply_upload_lifecycle_state(file_record, updated)

            return _collection_file_upload_payload(self._config, file_record, tus_url=tus_url)

    def append_upload_chunk(
        self,
        collection_id: int,
        path: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)
        normalized_path = _normalize_relpath_or_raise(path)
        staged_event_details: dict[str, object] | None = None

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None:
                raise NotFound(f"collection upload not found: {normalized_collection_id}")
            _expire_open_collection_upload_if_idle(
                upload,
                session_idle_ttl=self._upload_session_idle_ttl,
            )
            if upload.state not in {"open", "uploading", None}:
                raise Conflict(
                    f"collection upload is not accepting file bytes: {normalized_collection_id}"
                )

            file_record = _get_upload_file_record(
                session,
                normalized_collection_id,
                normalized_path,
            )
            _expire_collection_upload_file(file_record, self._upload_store)
            if file_record.tus_url is None:
                raise Conflict(f"collection upload file is not resumable: {normalized_path}")
            if offset != file_record.ingress_uploaded_bytes:
                raise Conflict(
                    f"collection upload offset for {normalized_path} is "
                    f"{offset}, expected {file_record.ingress_uploaded_bytes}"
                )

            next_offset, _ = self._upload_store.append_upload_chunk(
                file_record.tus_url,
                offset=offset,
                checksum=checksum,
                content=content,
            )
            file_record.ingress_uploaded_bytes = next_offset

            if next_offset >= file_record.ingress_bytes:
                file_record.upload_expires_at = None
                target_path = _collection_upload_target_path(file_record)
                content_digest = _sha256_hex_chunks(
                    iter_ingress_plaintext(
                        self._config,
                        self._upload_store,
                        target_path=target_path,
                        collection_id=normalized_collection_id,
                        path=normalized_path,
                        plaintext_bytes=file_record.bytes,
                        secret_envelope=file_record.ingress_secret_envelope,
                        state_json=file_record.ingress_state_json,
                    )
                )
                if content_digest != file_record.sha256:
                    raise HashMismatch("sha256 did not match expected file hash")
            else:
                file_record.upload_expires_at = upload_expiry_timestamp(self._upload_file_ttl)

            if upload.state != "open" and _collection_upload_is_complete_for_session(
                session, normalized_collection_id
            ):
                was_archiving = upload.state == "archiving"
                _ensure_collection_upload_archiving(upload)
                if not was_archiving:
                    staged_event_details = _collection_upload_event_details(session, upload)

            response: dict[str, object] = {
                "offset": file_record.ingress_uploaded_bytes,
                "length": file_record.ingress_bytes,
                "expires_at": file_record.upload_expires_at,
            }
        if staged_event_details is not None:
            self._lifecycle_events.emit_collection(
                type="collection.upload_staged",
                collection_id=normalized_collection_id,
                details=staged_event_details,
            )
        return response

    def get_file_upload(self, collection_id: int, path: str) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)
        normalized_path = _normalize_relpath_or_raise(path)

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None:
                raise NotFound(f"collection upload not found: {normalized_collection_id}")

            _expire_open_collection_upload_if_idle(
                upload,
                session_idle_ttl=self._upload_session_idle_ttl,
            )
            if upload.state in {"canceled", "expired"}:
                raise Conflict(f"collection upload is {upload.state}: {normalized_collection_id}")

            file_record = _get_upload_file_record(
                session,
                normalized_collection_id,
                normalized_path,
            )
            updated = sync_upload_state(
                current=_upload_lifecycle_state(file_record),
                target_path=_collection_upload_target_path(file_record),
                length=file_record.ingress_bytes,
                upload_store=self._upload_store,
            )
            _apply_upload_lifecycle_state(file_record, updated)
            _expire_collection_upload_file(file_record, self._upload_store)
            if file_record.tus_url is None:
                raise NotFound(f"collection upload file is not resumable: {normalized_path}")
            return _collection_file_upload_payload(
                self._config,
                file_record,
                tus_url=file_record.tus_url,
            )

    def cancel_file_upload(self, collection_id: int, path: str) -> None:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)
        normalized_path = _normalize_relpath_or_raise(path)

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None:
                raise NotFound(f"collection upload not found: {normalized_collection_id}")

            _expire_open_collection_upload_if_idle(
                upload,
                session_idle_ttl=self._upload_session_idle_ttl,
            )
            if upload.state in {"canceled", "expired"}:
                raise Conflict(f"collection upload is {upload.state}: {normalized_collection_id}")

            file_record = _get_upload_file_record(
                session,
                normalized_collection_id,
                normalized_path,
            )
            if file_record.tus_url is None:
                raise NotFound(f"collection upload file is not resumable: {normalized_path}")

            self._upload_store.cancel_upload(file_record.tus_url)
            self._upload_store.delete_target(_collection_upload_target_path(file_record))
            _apply_upload_lifecycle_state(
                file_record,
                UploadLifecycleState(
                    tus_url=None,
                    uploaded_bytes=0,
                    upload_expires_at=None,
                ),
            )

    def expire_stale_uploads(self) -> None:
        with session_scope(self._session_factory) as session:
            now = utc_now()
            now_text = format_utc_timestamp(now)
            if self._upload_session_idle_ttl is not None:
                cutoff = format_utc_timestamp(now - self._upload_session_idle_ttl)
                idle_uploads = session.scalars(
                    select(CollectionUploadRecord)
                    .where(
                        CollectionUploadRecord.state == "open",
                        func.coalesce(
                            CollectionUploadRecord.last_activity_at,
                            CollectionUploadRecord.opened_at,
                        )
                        <= cutoff,
                    )
                    .order_by(CollectionUploadRecord.collection_id)
                    .limit(_UPLOAD_EXPIRY_SWEEP_LIMIT)
                ).all()
                for upload in idle_uploads:
                    _mark_collection_upload_expired(upload)

            due_files = session.scalars(
                select(CollectionUploadFileRecord)
                .where(
                    CollectionUploadFileRecord.upload_expires_at.is_not(None),
                    CollectionUploadFileRecord.upload_expires_at <= now_text,
                )
                .order_by(
                    CollectionUploadFileRecord.collection_id,
                    CollectionUploadFileRecord.path,
                )
                .limit(_UPLOAD_EXPIRY_SWEEP_LIMIT)
            ).all()
            affected_uploads: set[int] = set()
            for file_record in due_files:
                affected_uploads.add(file_record.collection_id)
                _expire_collection_upload_file(file_record, self._upload_store)

            expired_session_files = session.scalars(
                select(CollectionUploadFileRecord)
                .join(
                    CollectionUploadRecord,
                    CollectionUploadRecord.collection_id
                    == CollectionUploadFileRecord.collection_id,
                )
                .where(CollectionUploadRecord.state == "expired")
                .order_by(
                    CollectionUploadFileRecord.collection_id,
                    CollectionUploadFileRecord.path,
                )
                .limit(_UPLOAD_EXPIRY_SWEEP_LIMIT)
            ).all()
            for file_record in expired_session_files:
                _forget_collection_upload_file(file_record, self._upload_store)
                session.delete(file_record)

            session.flush()
            for collection_id in affected_uploads:
                affected_upload = session.get(CollectionUploadRecord, collection_id)
                if (
                    affected_upload is not None
                    and affected_upload.state != "open"
                    and _collection_upload_has_no_live_file_state(session, collection_id)
                ):
                    session.delete(affected_upload)

    def get(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal | None = None,
    ) -> CollectionSummary:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)

        with session_scope(self._session_factory) as session:
            stmt, _ = _collection_summary_query()
            row = session.execute(
                stmt.where(
                    CollectionRecord.id == normalized_collection_id,
                    collection_access_filter(CollectionRecord.id, principal, CATALOG_READ),
                )
            ).one_or_none()
            if row is None:
                raise NotFound(f"collection not found: {normalized_collection_id}")
            return _collection_summary_from_row(
                row,
                aggregates=archive_copy_aggregates(
                    session,
                    collection_ids=[normalized_collection_id],
                ),
            )

    def list(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        sort: str = "id",
        order: str = "asc",
        all_items: bool = False,
        principal: ApplicationPrincipal | None = None,
    ) -> CollectionListPage:
        if page < 1:
            raise BadRequest("page must be at least 1")
        if per_page < 1:
            raise BadRequest("per_page must be at least 1")
        if sort not in _COLLECTION_SORT_FIELDS:
            raise BadRequest(f"sort must be one of {', '.join(sorted(_COLLECTION_SORT_FIELDS))}")
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")
        needle = q.casefold() if q else None
        with session_scope(self._session_factory) as session:
            filters: list[ColumnElement[bool]] = []
            filters.append(collection_access_filter(CollectionRecord.id, principal, CATALOG_READ))
            if needle is not None:
                filters.append(
                    or_(
                        cast(CollectionRecord.id, String).like(
                            _like_pattern(needle),
                            escape="\\",
                        ),
                        exists(
                            select(1).where(
                                CollectionTagRecord.collection_id == CollectionRecord.id,
                                func.lower(CollectionTagRecord.tag_id).like(
                                    _like_pattern(needle),
                                    escape="\\",
                                ),
                            )
                        ),
                    )
                )
            total = int(
                session.scalar(select(func.count()).select_from(CollectionRecord).where(*filters))
                or 0
            )
            pages = (total + per_page - 1) // per_page if total else 0
            start = (page - 1) * per_page
            collections_stmt, sort_columns = _collection_summary_query()
            sort_column = sort_columns[sort]
            order_by = sort_column.desc() if order == "desc" else sort_column.asc()
            collections_stmt = collections_stmt.where(*filters).order_by(
                order_by,
                CollectionRecord.id.asc(),
            )
            if not all_items:
                collections_stmt = collections_stmt.offset(start).limit(per_page)
            rows = session.execute(collections_stmt).all()
            aggregates = archive_copy_aggregates(
                session,
                collection_ids=[row[0].id for row in rows],
            )

            return CollectionListPage(
                page=1 if all_items else page,
                per_page=total if all_items else per_page,
                total=total,
                pages=(1 if total else 0) if all_items else pages,
                collections=[
                    _collection_summary_from_row(row, aggregates=aggregates) for row in rows
                ],
            )


def _collection_summary_query() -> tuple[Any, dict[str, Any]]:
    file_stats = (
        select(
            CollectionFileRecord.collection_id.label("collection_id"),
            func.count(CollectionFileRecord.path).label("files"),
            func.coalesce(func.sum(CollectionFileRecord.bytes), 0).label("bytes"),
        )
        .group_by(CollectionFileRecord.collection_id)
        .subquery()
    )
    files = func.coalesce(file_stats.c.files, 0)
    bytes_total = func.coalesce(file_stats.c.bytes, 0)
    return (
        select(
            CollectionRecord,
            files.label("files"),
            bytes_total.label("bytes"),
        )
        .options(
            selectinload(CollectionRecord.archive_copies),
            selectinload(CollectionRecord.tags),
        )
        .outerjoin(file_stats, file_stats.c.collection_id == CollectionRecord.id),
        {
            "id": CollectionRecord.id,
            "created_at": CollectionRecord.created_at,
            "bytes": bytes_total,
            "files": files,
        },
    )


def _collection_summary_from_row(
    row: Any,
    *,
    aggregates: dict[tuple[int, str], ArchiveCopyAggregate],
) -> CollectionSummary:
    collection = row[0]
    return CollectionSummary(
        id=CollectionId(collection.id),
        created_at=collection.created_at,
        tags=tuple(sorted(assignment.tag_id for assignment in collection.tags)),
        files=int(row.files),
        bytes=int(row.bytes),
        archive_copies=tuple(
            _collection_archive_status(copy, aggregates=aggregates)
            for copy in sorted(collection.archive_copies, key=lambda item: item.store)
        ),
    )


def _normalize_collection_id_or_raise(raw: str | int) -> int:
    try:
        return normalize_collection_id(raw)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _like_prefix(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def _normalize_tag_or_raise(raw: str) -> str:
    try:
        normalized = normalize_tag(raw)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc
    if raw != normalized:
        raise BadRequest("tag id must be canonical")
    return normalized


def _normalize_tags(tags: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({_normalize_tag_or_raise(str(tag)) for tag in tags}))
    if len(normalized) != len(tags):
        raise BadRequest("collection tags must not contain duplicates")
    return normalized


def _normalize_idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise BadRequest("idempotency_key must not be empty")
    if normalized != value:
        raise BadRequest("idempotency_key must not have surrounding whitespace")
    if len(normalized) > 200:
        raise BadRequest("idempotency_key must not exceed 200 characters")
    return normalized


def _tags_json(tags: Sequence[str]) -> str:
    return json.dumps(list(tags), separators=(",", ":"))


def _upload_tags(upload: CollectionUploadRecord) -> tuple[str, ...]:
    decoded = json.loads(upload.tags_json)
    if not isinstance(decoded, list) or not all(isinstance(tag, str) for tag in decoded):
        raise RuntimeError("collection upload tags are invalid")
    return tuple(decoded)


def _normalize_relpath_or_raise(raw: str) -> str:
    try:
        return normalize_relpath(raw)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc


def _normalize_upload_files(files: Sequence[dict[str, object]]) -> list[_UploadManifestEntry]:
    if not files:
        raise BadRequest("collection upload must include at least one file")

    normalized: list[_UploadManifestEntry] = []
    seen_paths: set[str] = set()
    for item in files:
        path = _normalize_relpath_or_raise(str(item.get("path", "")))
        if path in seen_paths:
            raise BadRequest(f"collection upload listed the same file more than once: {path}")
        seen_paths.add(path)

        bytes_value = item.get("bytes")
        if not isinstance(bytes_value, int) or bytes_value < 0:
            raise BadRequest(f"collection upload file bytes must be a non-negative integer: {path}")

        sha256 = str(item.get("sha256", ""))
        if not _SHA256_RE.fullmatch(sha256):
            raise BadRequest(f"collection upload file sha256 must be lowercase hex: {path}")

        normalized.append({"path": path, "bytes": bytes_value, "sha256": sha256})

    return sorted(normalized, key=lambda current: str(current["path"]))


def _manifest_entry_payload(
    file_record: _UploadManifestEntry | CollectionUploadFileRecord,
) -> _UploadManifestEntry:
    if isinstance(file_record, Mapping):
        return {
            "path": file_record["path"],
            "bytes": file_record["bytes"],
            "sha256": file_record["sha256"],
        }
    return {
        "path": file_record.path,
        "bytes": file_record.bytes,
        "sha256": file_record.sha256,
    }


def _ensure_upload_archive_store_matches(
    upload: CollectionUploadRecord,
    archive_store: str,
) -> None:
    if upload.archive_store == archive_store:
        return
    raise Conflict(
        f"collection upload already exists for a different archive store: {upload.collection_id}"
    )


def _ensure_upload_tags_match(
    upload: CollectionUploadRecord,
    tags: Sequence[str],
) -> None:
    if _upload_tags(upload) != tuple(tags):
        raise Conflict("tags are immutable for an existing collection upload")


def _ensure_finalized_request_matches(
    session: Session,
    *,
    collection: CollectionRecord,
    tags: Sequence[str],
    initiator_app: str,
) -> None:
    if collection.created_by_app != initiator_app:
        raise Conflict("idempotency_key belongs to a different application")
    current_tags = tuple(
        session.scalars(
            select(CollectionTagRecord.tag_id)
            .where(CollectionTagRecord.collection_id == collection.id)
            .order_by(CollectionTagRecord.tag_id)
        ).all()
    )
    if current_tags != tuple(tags):
        raise Conflict("idempotency_key already finalized with different tags")


def _ensure_upload_initiator_matches(upload: CollectionUploadRecord, app: str) -> None:
    if upload.initiated_by_app != app:
        raise Conflict(
            f"collection upload is owned by a different application: {upload.collection_id}"
        )


def _ensure_event_context_matches(
    upload: CollectionUploadRecord,
    requested_context_json: str | None,
) -> None:
    if requested_context_json is not None and requested_context_json != upload.event_context_json:
        raise Conflict("event_context is immutable for an existing collection upload")


def _require_tags(session: Session, tags: Sequence[str]) -> None:
    if not tags:
        return
    existing = set(session.scalars(select(TagRecord.id).where(TagRecord.id.in_(tags))).all())
    missing = sorted(set(tags) - existing)
    if missing:
        raise NotFound(f"tag not found: {missing[0]}")


def _get_upload_file_record(
    session: Session,
    collection_id: int,
    path: str,
) -> CollectionUploadFileRecord:
    file_record = session.get(CollectionUploadFileRecord, (collection_id, path))
    if file_record is None:
        raise NotFound(f"collection upload file not found: {path}")
    return file_record


def _upload_lifecycle_state(file_record: CollectionUploadFileRecord) -> UploadLifecycleState:
    return UploadLifecycleState(
        tus_url=file_record.tus_url,
        uploaded_bytes=file_record.ingress_uploaded_bytes,
        upload_expires_at=file_record.upload_expires_at,
    )


def _apply_upload_lifecycle_state(
    file_record: CollectionUploadFileRecord, state: UploadLifecycleState
) -> None:
    file_record.tus_url = state.tus_url
    file_record.ingress_uploaded_bytes = state.uploaded_bytes
    file_record.upload_expires_at = state.upload_expires_at


def _collection_upload_target_path(file_record: CollectionUploadFileRecord) -> str:
    return _collection_upload_registration_target_path(
        collection_id=file_record.collection_id,
        path=file_record.path,
        secret_envelope=file_record.ingress_secret_envelope,
    )


def _collection_upload_registration_target_path(
    *,
    collection_id: int,
    path: str,
    secret_envelope: str,
) -> str:
    identity = hashlib.sha256(f"{collection_id}\0{path}\0{secret_envelope}".encode()).hexdigest()
    return f"/.riverhog/uploads/objects/{identity}"


def _new_collection_upload_file(
    config: RuntimeConfig,
    *,
    collection_id: int,
    path: str,
    file_order: int,
    bytes: int,
    sha256: str,
) -> CollectionUploadFileRecord:
    encryption = create_ingress_encryption(
        config,
        collection_id=collection_id,
        path=path,
        plaintext_bytes=bytes,
    )
    target_path = _collection_upload_registration_target_path(
        collection_id=collection_id,
        path=path,
        secret_envelope=encryption.secret_envelope,
    )
    return CollectionUploadFileRecord(
        collection_id=collection_id,
        path=path,
        file_order=file_order,
        bytes=bytes,
        sha256=sha256,
        ingress_bytes=encryption.ciphertext_bytes,
        ingress_uploaded_bytes=0,
        ingress_secret_envelope=encryption.secret_envelope,
        ingress_state_json=encryption.state_json,
        ingress_upload_id=tusd_upload_id_for_target_path(target_path),
        upload_expires_at=None,
        tus_url=None,
    )


def _collection_file_upload_payload(
    config: RuntimeConfig,
    file_record: CollectionUploadFileRecord,
    *,
    tus_url: str,
) -> dict[str, object]:
    return {
        "path": file_record.path,
        "protocol": "tus",
        "upload_url": tus_url,
        "offset": file_record.ingress_uploaded_bytes,
        "length": file_record.ingress_bytes,
        "checksum_algorithm": "sha256",
        "expires_at": file_record.upload_expires_at,
        "encryption": ingress_encryption_descriptor(
            config,
            collection_id=file_record.collection_id,
            path=file_record.path,
            plaintext_bytes=file_record.bytes,
            ciphertext_bytes=file_record.ingress_bytes,
            secret_envelope=file_record.ingress_secret_envelope,
            state_json=file_record.ingress_state_json,
        ),
    }


def _sync_collection_upload_files(
    file_records: Sequence[CollectionUploadFileRecord],
    upload_store: UploadStore,
    *,
    force: bool = False,
) -> None:
    records = list(file_records)

    def sync_file(
        task: tuple[UploadLifecycleState, str, int],
    ) -> UploadLifecycleState:
        current, target_path, length = task
        return sync_upload_state(
            current=current,
            target_path=target_path,
            length=length,
            upload_store=upload_store,
            force=force,
        )

    tasks = [
        (
            _upload_lifecycle_state(file_record),
            _collection_upload_target_path(file_record),
            file_record.ingress_bytes,
        )
        for file_record in records
    ]
    if force and len(tasks) > 1:
        with ThreadPoolExecutor(
            max_workers=min(_UPLOAD_SYNC_WORKERS, len(tasks)),
            thread_name_prefix="riverhog-upload-sync",
        ) as executor:
            updated_states = list(executor.map(sync_file, tasks))
    else:
        updated_states = list(map(sync_file, tasks))

    for file_record, updated in zip(records, updated_states, strict=True):
        _apply_upload_lifecycle_state(file_record, updated)


def _expire_collection_upload_files(
    file_records: Sequence[CollectionUploadFileRecord],
    upload_store: UploadStore,
) -> bool:
    expired_any = False
    for file_record in file_records:
        updated, expired = expire_upload_state(
            current=_upload_lifecycle_state(file_record),
            target_path=_collection_upload_target_path(file_record),
            upload_store=upload_store,
        )
        _apply_upload_lifecycle_state(file_record, updated)
        expired_any = expired_any or expired
    return expired_any


def _expire_collection_upload_file(
    file_record: CollectionUploadFileRecord,
    upload_store: UploadStore,
) -> None:
    target_path = _collection_upload_target_path(file_record)
    updated, _ = expire_upload_state(
        current=_upload_lifecycle_state(file_record),
        target_path=target_path,
        upload_store=upload_store,
    )
    _apply_upload_lifecycle_state(file_record, updated)


def _sync_and_expire_collection_upload(
    session: Session,
    upload: CollectionUploadRecord,
    *,
    upload_store: UploadStore,
    session_idle_ttl: timedelta | None = None,
    force_offset_sync: bool = False,
) -> CollectionUploadRecord | None:
    if upload.state in {"canceled", "expired"}:
        return upload
    _sync_collection_upload_files(upload.files, upload_store, force=force_offset_sync)
    expired_any = _expire_collection_upload_files(upload.files, upload_store)
    if upload.state == "open":
        if _collection_upload_session_is_idle_expired(upload, ttl=session_idle_ttl):
            _forget_collection_upload(upload, upload_store)
            upload.files.clear()
            now = utc_timestamp_now()
            upload.state = "expired"
            upload.closed_at = now
            upload.last_activity_at = now
        return upload
    if expired_any and _collection_upload_has_no_live_file_state(
        session,
        upload.collection_id,
    ):
        _forget_collection_upload(upload, upload_store)
        session.delete(upload)
        return None
    return upload


def _expire_open_collection_upload_if_idle(
    upload: CollectionUploadRecord,
    *,
    session_idle_ttl: timedelta | None = None,
) -> None:
    if not _collection_upload_session_is_idle_expired(upload, ttl=session_idle_ttl):
        return
    _mark_collection_upload_expired(upload)


def _mark_collection_upload_expired(upload: CollectionUploadRecord) -> None:
    now = utc_timestamp_now()
    upload.state = "expired"
    upload.closed_at = now
    upload.last_activity_at = now


def _forget_collection_upload_file(
    file_record: CollectionUploadFileRecord,
    upload_store: UploadStore,
) -> None:
    if file_record.tus_url is not None:
        upload_store.cancel_upload(file_record.tus_url)
    upload_store.delete_target(_collection_upload_target_path(file_record))


def _touch_collection_upload(upload: CollectionUploadRecord) -> None:
    now = utc_timestamp_now()
    if upload.opened_at is None:
        upload.opened_at = now
    upload.last_activity_at = now


def _collection_upload_session_is_idle_expired(
    upload: CollectionUploadRecord,
    *,
    ttl: timedelta | None,
) -> bool:
    if upload.state != "open" or ttl is None:
        return False
    activity_at = _safe_parse_utc_timestamp(upload.last_activity_at or upload.opened_at)
    if activity_at is None:
        return False
    return utc_now() >= activity_at + ttl


def _collection_upload_has_no_live_file_state(
    session: Session,
    collection_id: int,
) -> bool:
    session.flush()
    has_live_state = session.scalar(
        select(
            select(CollectionUploadFileRecord.path)
            .where(
                CollectionUploadFileRecord.collection_id == collection_id,
                or_(
                    CollectionUploadFileRecord.ingress_uploaded_bytes > 0,
                    CollectionUploadFileRecord.tus_url.is_not(None),
                    CollectionUploadFileRecord.upload_expires_at.is_not(None),
                ),
            )
            .exists()
        )
    )
    return not bool(has_live_state)


def _forget_collection_upload(
    upload: CollectionUploadRecord,
    upload_store: UploadStore,
) -> None:
    targets = [
        (
            file_record.tus_url,
            _collection_upload_target_path(file_record),
        )
        for file_record in upload.files
    ]

    def forget_target(target: tuple[str | None, str]) -> None:
        tus_url, target_path = target
        if tus_url is not None:
            upload_store.cancel_upload(tus_url)
        upload_store.delete_target(target_path)

    if len(targets) <= 1:
        for target in targets:
            forget_target(target)
        return

    with ThreadPoolExecutor(
        max_workers=min(_UPLOAD_FORGET_WORKERS, len(targets)),
        thread_name_prefix="riverhog-upload-cleanup",
    ) as executor:
        list(executor.map(forget_target, targets))


def _collection_upload_is_complete_for_session(session: Session, collection_id: int) -> bool:
    stats = _collection_upload_stats(session, collection_id)
    return stats["files_total"] > 0 and stats["files_uploaded"] == stats["files_total"]


def _collection_upload_session_state(
    upload: CollectionUploadRecord,
    stats: _CollectionUploadStats,
) -> str:
    if upload.state in {"open", "canceled", "expired"}:
        return str(upload.state)
    if stats["files_total"] == 0 or stats["files_uploaded"] != stats["files_total"]:
        return "uploading"
    if upload.state == "failed":
        return "failed"
    return "archiving"


def _collection_upload_event_details(
    session: Session,
    upload: CollectionUploadRecord,
) -> dict[str, object]:
    stats = _collection_upload_stats(session, upload.collection_id)
    return {
        "state": _collection_upload_session_state(upload, stats),
        "files_total": stats["files_total"],
        "files_uploaded": stats["files_uploaded"],
        "bytes_total": stats["bytes_total"],
        "uploaded_bytes": stats["uploaded_bytes"],
    }


def _collection_upload_stats(
    session: Session,
    collection_id: int,
) -> _CollectionUploadStats:
    session.flush()
    uploaded = and_(
        CollectionUploadFileRecord.ingress_uploaded_bytes
        >= CollectionUploadFileRecord.ingress_bytes,
    )
    partial = and_(CollectionUploadFileRecord.ingress_uploaded_bytes > 0, ~uploaded)
    uploaded_logical_bytes = case((uploaded, CollectionUploadFileRecord.bytes), else_=0)
    row = session.execute(
        select(
            func.count(CollectionUploadFileRecord.path).label("files_total"),
            func.coalesce(
                func.sum(
                    case((CollectionUploadFileRecord.ingress_uploaded_bytes <= 0, 1), else_=0)
                ),
                0,
            ).label("files_pending"),
            func.coalesce(func.sum(case((partial, 1), else_=0)), 0).label("files_partial"),
            func.coalesce(func.sum(case((uploaded, 1), else_=0)), 0).label("files_uploaded"),
            func.coalesce(func.sum(CollectionUploadFileRecord.bytes), 0).label("bytes_total"),
            func.coalesce(func.sum(uploaded_logical_bytes), 0).label("uploaded_bytes"),
            func.coalesce(
                func.sum(CollectionUploadFileRecord.bytes - uploaded_logical_bytes),
                0,
            ).label("missing_bytes"),
            func.max(CollectionUploadFileRecord.upload_expires_at).label("upload_state_expires_at"),
        ).where(CollectionUploadFileRecord.collection_id == collection_id)
    ).one()
    return {
        "files_total": int(row.files_total),
        "files_pending": int(row.files_pending),
        "files_partial": int(row.files_partial),
        "files_uploaded": int(row.files_uploaded),
        "bytes_total": int(row.bytes_total),
        "uploaded_bytes": int(row.uploaded_bytes),
        "missing_bytes": int(row.missing_bytes),
        "upload_state_expires_at": row.upload_state_expires_at,
    }


def _ensure_collection_upload_archiving(upload: CollectionUploadRecord) -> None:
    if upload.state not in {"archiving", "failed"}:
        upload.state = "archiving"
    if upload.archive_next_attempt_at is None and upload.state == "archiving":
        upload.archive_next_attempt_at = utc_timestamp_now()


def _finalized_collection_upload_payload(
    session: Session,
    collection: CollectionRecord,
    *,
    archive_store: str,
) -> dict[str, object]:
    stmt, _ = _collection_summary_query()
    summary = _collection_summary_from_row(
        session.execute(stmt.where(CollectionRecord.id == collection.id)).one(),
        aggregates=archive_copy_aggregates(session, collection_ids=[collection.id]),
    )
    archive = next(
        (copy for copy in collection.archive_copies if copy.store == archive_store),
        collection.archive_copies[0] if collection.archive_copies else None,
    )
    archive_stored_bytes = None
    if archive is not None:
        archive_stored_bytes = archive_copy_aggregates(
            session,
            collection_ids=[collection.id],
        ).get((collection.id, archive.store), (0, 0))[1]
    return {
        "collection_id": collection.id,
        "created_at": collection.created_at,
        "tags": list(summary.tags),
        "ingest_source": collection.ingest_source,
        "archive_store": archive.store if archive is not None else archive_store,
        "state": "finalized",
        "files_total": summary.files,
        "files_pending": 0,
        "files_partial": 0,
        "files_uploaded": summary.files,
        "bytes_total": summary.bytes,
        "uploaded_bytes": summary.bytes,
        "missing_bytes": 0,
        "upload_state_expires_at": None,
        "latest_failure": None,
        "archive_phase": "completed",
        "archive_phase_updated_at": archive.last_verified_at if archive is not None else None,
        "archive_storage_prefix": (archive.archive_storage_prefix if archive is not None else None),
        "archive_uploaded_bytes": archive_stored_bytes,
        "archive_total_bytes": archive_stored_bytes,
        "archive_uploaded_parts": None,
        "archive_total_parts": None,
        "collection": _collection_summary_payload(summary),
    }


def _collection_upload_payload(
    *,
    session: Session,
    upload: CollectionUploadRecord,
    state: str | None,
    collection: CollectionSummary | None,
) -> dict[str, object]:
    stats = _collection_upload_stats(session, upload.collection_id)
    object_progress = session.execute(
        select(
            func.coalesce(func.sum(CollectionArchiveObjectUploadRecord.uploaded_bytes), 0),
            func.coalesce(
                func.sum(CollectionArchiveObjectUploadRecord.multipart_content_length),
                0,
            ),
            func.coalesce(func.sum(CollectionArchiveObjectUploadRecord.uploaded_parts), 0),
            func.coalesce(func.sum(CollectionArchiveObjectUploadRecord.total_parts), 0),
        ).where(CollectionArchiveObjectUploadRecord.collection_id == upload.collection_id)
    ).one()
    return {
        "collection_id": upload.collection_id,
        "created_at": upload.opened_at,
        "tags": list(_upload_tags(upload)),
        "ingest_source": upload.ingest_source,
        "archive_store": upload.archive_store,
        "state": state or _collection_upload_session_state(upload, stats),
        **stats,
        "latest_failure": upload.archive_failure,
        "archive_phase": upload.archive_phase,
        "archive_phase_updated_at": upload.archive_phase_updated_at,
        "archive_storage_prefix": upload.archive_storage_prefix,
        "archive_uploaded_bytes": int(object_progress[0]),
        "archive_total_bytes": int(object_progress[1]),
        "archive_uploaded_parts": int(object_progress[2]),
        "archive_total_parts": int(object_progress[3]),
        "collection": _collection_summary_payload(collection) if collection is not None else None,
    }


def _collection_upload_files_registration_payload(
    upload: CollectionUploadRecord,
    file_records: Sequence[CollectionUploadFileRecord],
) -> dict[str, object]:
    return {
        "collection_id": upload.collection_id,
        "ingest_source": upload.ingest_source,
        "archive_store": upload.archive_store,
        "state": upload.state or "open",
        "files": [_collection_upload_file_payload(file_record) for file_record in file_records],
    }


def _collection_upload_file_registration_payload(
    upload: CollectionUploadRecord,
    file_record: CollectionUploadFileRecord,
) -> dict[str, object]:
    return {
        "collection_id": upload.collection_id,
        "ingest_source": upload.ingest_source,
        "archive_store": upload.archive_store,
        "state": upload.state or "open",
        "file": _collection_upload_file_payload(file_record),
    }


def _collection_upload_file_payload(
    file_record: CollectionUploadFileRecord,
) -> dict[str, object]:
    return {
        "path": file_record.path,
        "bytes": file_record.bytes,
        "sha256": file_record.sha256,
        "upload_state": upload_state_name(
            uploaded_bytes=file_record.ingress_uploaded_bytes,
            length=file_record.ingress_bytes,
        ),
        "uploaded_bytes": plaintext_bytes_for_ciphertext_offset(
            state=file_record.ingress_state_json,
            plaintext_bytes=file_record.bytes,
            ciphertext_bytes=file_record.ingress_bytes,
            ciphertext_offset=file_record.ingress_uploaded_bytes,
        ),
        "upload_state_expires_at": file_record.upload_expires_at,
    }


def _collection_summary_payload(summary: CollectionSummary) -> dict[str, object]:
    return {
        "id": summary.id,
        "created_at": summary.created_at,
        "tags": list(summary.tags),
        "files": summary.files,
        "bytes": summary.bytes,
        "archive_copies": [_archive_copy_payload(copy) for copy in summary.archive_copies],
    }


def _archive_copy_payload(summary: ArchiveCopyStatus) -> dict[str, object]:
    return {
        "store": summary.store,
        "state": summary.state.value,
        "storage_prefix": summary.storage_prefix,
        "object_count": summary.object_count,
        "stored_bytes": summary.stored_bytes,
        "backend": summary.backend,
        "storage_class": summary.storage_class,
        "last_uploaded_at": summary.last_uploaded_at,
        "last_verified_at": summary.last_verified_at,
        "failure": summary.failure,
        "collection_manifest": _collection_manifest_payload(summary.collection_manifest),
    }


def _collection_manifest_payload(
    summary: CollectionManifestStatus | None,
) -> dict[str, object] | None:
    if summary is None:
        return None
    return {
        "object_path": summary.object_path,
        "sha256": summary.sha256,
        "proof_object_path": summary.proof_object_path,
        "proof_state": summary.proof_state,
        "proof_sha256": summary.proof_sha256,
    }


def _sha256_hex(content: bytes) -> Sha256Hex:
    return Sha256Hex(hashlib.sha256(content).hexdigest())


def _sha256_hex_chunks(chunks: Iterable[bytes]) -> Sha256Hex:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return Sha256Hex(digest.hexdigest())


def _safe_parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parse_utc_timestamp(value)
    except ValueError:
        return None


def _collection_archive_status(
    archive: CollectionArchiveCopyRecord,
    *,
    aggregates: dict[tuple[int, str], ArchiveCopyAggregate],
) -> ArchiveCopyStatus:
    object_count, stored_bytes = aggregates.get((archive.collection_id, archive.store), (0, 0))
    return ArchiveCopyStatus(
        store=archive.store,
        state=ArchiveState(archive.state),
        storage_prefix=archive.archive_storage_prefix,
        object_count=object_count,
        stored_bytes=stored_bytes,
        backend=archive.backend,
        storage_class=archive.storage_class,
        last_uploaded_at=archive.last_uploaded_at,
        last_verified_at=archive.last_verified_at,
        failure=archive.failure,
        collection_manifest=_collection_manifest_status(archive),
    )


def _collection_manifest_status(archive: CollectionArchiveCopyRecord) -> CollectionManifestStatus:
    manifest = next(
        (current for current in archive.objects if current.object_id == "manifest"),
        None,
    )
    proof = next(
        (current for current in archive.objects if current.object_id == "proof"),
        None,
    )
    return CollectionManifestStatus(
        object_path=manifest.object_path if manifest else None,
        sha256=manifest.sha256 if manifest else None,
        proof_object_path=proof.object_path if proof else None,
        proof_state=(
            "failed"
            if archive.state == ArchiveState.FAILED.value
            else "uploaded"
            if proof
            else "pending"
        ),
        proof_sha256=proof.sha256 if proof else None,
    )
