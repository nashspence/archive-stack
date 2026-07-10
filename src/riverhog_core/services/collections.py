from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypedDict

from sqlalchemy import Integer, and_, case, cast, func, select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql.elements import ColumnElement

from riverhog_core.archive_compliance import (
    DEFAULT_REQUIRED_PHYSICAL_COPIES,
    collection_protection_state,
    copy_counts_as_verified,
    copy_counts_toward_protection,
    image_protection_state,
    normalize_copy_state,
    normalize_glacier_state,
    normalize_required_copy_count,
    normalize_verification_state,
    registered_copy_shortfall,
)
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionImageOperatorSummaryRecord,
    CollectionOperatorSummaryRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    ImageCopyRecord,
    ImageOperatorSummaryRecord,
)
from riverhog_core.domain.enums import GlacierState, ProtectionState, RecoveryCoverageState
from riverhog_core.domain.errors import BadRequest, Conflict, HashMismatch, NotFound
from riverhog_core.domain.models import (
    CollectionCoverageImage,
    CollectionListPage,
    CollectionManifestStatus,
    CollectionRecoverySummary,
    CollectionSummary,
    CopySummary,
    GlacierArchiveStatus,
    RecoveryCoverage,
)
from riverhog_core.domain.types import CollectionId, CopyId, ImageId, Sha256Hex
from riverhog_core.fs_paths import (
    PathNormalizationError,
    find_collection_id_conflict,
    normalize_collection_id,
    normalize_relpath,
    normalize_upload_slug,
    normalize_upload_timestamp,
)
from riverhog_core.ports.hot_store import HotStore
from riverhog_core.ports.upload_store import UploadStore
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.notification_routing import (
    collection_notify_json as _collection_notify_json,
)
from riverhog_core.services.notification_routing import (
    decode_collection_notify_json,
    post_collection_operator_webhook,
)
from riverhog_core.services.resumable_uploads import (
    UploadLifecycleState,
    create_or_resume_upload_state,
    expire_upload_state,
    sync_upload_state,
    upload_expiry_timestamp,
    upload_state_name,
)
from riverhog_core.webhooks import post_webhook

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LOG = logging.getLogger(__name__)
_COLLECTION_SORT_FIELDS = {
    "id",
    "bytes",
    "files",
    "hot_bytes",
    "archived_bytes",
    "pending_bytes",
    "protected_bytes",
}


@dataclass(frozen=True, slots=True)
class _RecoveryParts:
    part_count: int
    present_parts: frozenset[int]


@dataclass(slots=True)
class _CollectionListStats:
    files: int = 0
    bytes: int = 0
    hot_bytes: int = 0
    archived_bytes: int = 0
    protected_bytes: int = 0
    physical_bytes: int = 0
    has_registered_image: bool = False


@dataclass(frozen=True, slots=True)
class _CollectionFileSummaryRow:
    collection_id: str
    path: str
    bytes: int
    hot: bool
    archived: bool


class _UploadManifestEntry(TypedDict):
    path: str
    bytes: int
    sha256: str


class _StoredManifestEntry(Protocol):
    path: str
    bytes: int
    sha256: str


class _CollectionFileLike(Protocol):
    @property
    def collection_id(self) -> str: ...

    @property
    def path(self) -> str: ...

    @property
    def bytes(self) -> int: ...

    @property
    def hot(self) -> bool: ...

    @property
    def archived(self) -> bool: ...


class SqlAlchemyCollectionService:
    def __init__(
        self,
        config: RuntimeConfig,
        hot_store: HotStore,
        upload_store: UploadStore,
    ) -> None:
        self._config = config
        self._hot_store = hot_store
        self._upload_store = upload_store
        self._upload_ttl = config.incomplete_upload_ttl
        self._upload_session_idle_ttl = config.upload_session_idle_ttl
        self._session_factory = make_session_factory(config.database_url)

    def create_or_resume_upload(
        self,
        *,
        upload_slug: str,
        files: Sequence[dict[str, object]],
        ingest_source: str | None = None,
        upload_timestamp: str | None = None,
        notify: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        normalized_slug = _normalize_upload_slug_or_raise(upload_slug)
        normalized_notify_json = _collection_notify_json(notify)
        normalized_upload_timestamp = (
            _normalize_upload_timestamp_or_raise(upload_timestamp)
            if upload_timestamp is not None
            else None
        )
        requested_collection_id = (
            _collection_id_for_upload_timestamp(normalized_slug, normalized_upload_timestamp)
            if normalized_upload_timestamp is not None
            else None
        )
        normalized_files = _normalize_upload_files(files)
        manifest_fingerprint = _collection_upload_manifest_fingerprint(normalized_files)

        with session_scope(self._session_factory) as session:
            collection = _find_matching_collection(
                session,
                upload_slug=normalized_slug,
                manifest_fingerprint=manifest_fingerprint,
            )
            if collection is not None:
                _ensure_requested_collection_id_matches(
                    collection.id,
                    requested_collection_id,
                )
                return _finalized_collection_upload_payload(session, collection)

            upload = _find_matching_upload(
                session,
                upload_slug=normalized_slug,
                manifest_fingerprint=manifest_fingerprint,
                upload_store=self._upload_store,
            )
            if upload is not None:
                _ensure_requested_collection_id_matches(
                    upload.collection_id,
                    requested_collection_id,
                )
                _validate_existing_upload_manifest(upload, normalized_files)

            if upload is None:
                normalized_collection_id = requested_collection_id or _mint_collection_id(
                    session,
                    normalized_slug,
                )
                _ensure_collection_id_unused(session, normalized_collection_id)
                _ensure_collection_upload_conflict_free(session, normalized_collection_id)
                _ensure_unburned_collection_limit_allows(
                    session,
                    incoming_bytes=sum(item["bytes"] for item in normalized_files),
                    limit_bytes=self._config.unburned_collection_bytes_limit,
                )
                upload = CollectionUploadRecord(
                    collection_id=normalized_collection_id,
                    ingest_source=ingest_source,
                    notify_json=normalized_notify_json,
                    state="uploading",
                    opened_at=_utc_now(),
                    last_activity_at=_utc_now(),
                )
                session.add(upload)
                for index, item in enumerate(normalized_files, start=1):
                    upload.files.append(
                        CollectionUploadFileRecord(
                            collection_id=normalized_collection_id,
                            path=item["path"],
                            file_order=index,
                            bytes=item["bytes"],
                            sha256=item["sha256"],
                            uploaded_bytes=0,
                            upload_expires_at=None,
                            tus_url=None,
                        )
                    )
            else:
                upload.ingest_source = ingest_source
                if normalized_notify_json is not None:
                    upload.notify_json = normalized_notify_json
                _touch_collection_upload(upload)
                normalized_collection_id = upload.collection_id

            if upload.state != "open" and _collection_upload_is_complete(upload.files):
                if upload.state == "failed":
                    upload.state = "archiving"
                    upload.archive_next_attempt_at = _utc_now()
                _ensure_collection_upload_archiving(upload)
                return _collection_upload_payload(
                    collection_id=normalized_collection_id,
                    ingest_source=upload.ingest_source,
                    files=upload.files,
                    state=_collection_upload_session_state(upload),
                    collection=None,
                    upload=upload,
                )

            return _collection_upload_payload(
                collection_id=normalized_collection_id,
                ingest_source=upload.ingest_source,
                files=upload.files,
                state="uploading",
                collection=None,
                upload=upload,
            )

    def create_or_resume_upload_session(
        self,
        *,
        upload_slug: str,
        ingest_source: str | None = None,
        upload_timestamp: str | None = None,
        notify: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        normalized_slug = _normalize_upload_slug_or_raise(upload_slug)
        normalized_notify_json = _collection_notify_json(notify)
        normalized_upload_timestamp = (
            _normalize_upload_timestamp_or_raise(upload_timestamp)
            if upload_timestamp is not None
            else None
        )
        requested_collection_id = (
            _collection_id_for_upload_timestamp(normalized_slug, normalized_upload_timestamp)
            if normalized_upload_timestamp is not None
            else None
        )

        with session_scope(self._session_factory) as session:
            if requested_collection_id is not None:
                collection = session.get(CollectionRecord, requested_collection_id)
                if collection is not None:
                    return _finalized_collection_upload_payload(session, collection)
                upload = session.get(CollectionUploadRecord, requested_collection_id)
            else:
                upload = _find_open_upload_session(session, upload_slug=normalized_slug)

            if upload is not None:
                upload = _sync_and_expire_collection_upload(
                    session,
                    upload,
                    upload_store=self._upload_store,
                    session_idle_ttl=self._upload_session_idle_ttl,
                )
            if upload is not None:
                if upload.state in {"canceled", "expired"} and requested_collection_id is None:
                    upload = None
            if upload is not None:
                if upload.state == "open":
                    upload.ingest_source = ingest_source
                    if normalized_notify_json is not None:
                        upload.notify_json = normalized_notify_json
                    _touch_collection_upload(upload)
                    return _collection_upload_payload(
                        collection_id=upload.collection_id,
                        ingest_source=upload.ingest_source,
                        files=upload.files,
                        state="open",
                        collection=None,
                        upload=upload,
                    )
                if upload.state in {"canceled", "expired"}:
                    raise Conflict(
                        f"collection upload session is {upload.state}: {upload.collection_id}"
                    )
                return _collection_upload_payload(
                    collection_id=upload.collection_id,
                    ingest_source=upload.ingest_source,
                    files=upload.files,
                    state=_collection_upload_session_state(upload),
                    collection=None,
                    upload=upload,
                )

            normalized_collection_id = requested_collection_id or _mint_collection_id(
                session,
                normalized_slug,
            )
            _ensure_collection_id_unused(session, normalized_collection_id)
            _ensure_collection_upload_conflict_free(session, normalized_collection_id)
            _ensure_unburned_collection_limit_allows(
                session,
                incoming_bytes=0,
                limit_bytes=self._config.unburned_collection_bytes_limit,
            )
            now = _utc_now()
            upload = CollectionUploadRecord(
                collection_id=normalized_collection_id,
                ingest_source=ingest_source,
                notify_json=normalized_notify_json,
                state="open",
                opened_at=now,
                last_activity_at=now,
            )
            session.add(upload)
            return _collection_upload_payload(
                collection_id=upload.collection_id,
                ingest_source=upload.ingest_source,
                files=upload.files,
                state="open",
                collection=None,
                upload=upload,
            )

    def register_upload_session_file(
        self,
        collection_id: str,
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
            return _collection_upload_file_registration_payload(upload, file_record)

    def create_or_resume_registered_file_upload(
        self,
        collection_id: str,
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
            target_path = _collection_upload_target_path(
                normalized_collection_id,
                str(normalized_file["path"]),
            )
            _expire_collection_upload_file(file_record, self._upload_store)
            updated, tus_url = create_or_resume_upload_state(
                current=_upload_lifecycle_state(file_record),
                target_path=target_path,
                length=file_record.bytes,
                upload_store=self._upload_store,
                ttl=self._upload_ttl,
            )
            _apply_upload_lifecycle_state(file_record, updated)

            return {
                **_collection_upload_file_registration_payload(upload, file_record),
                **_collection_file_upload_payload(file_record, tus_url=tus_url),
            }

    def sync_finished_upload_target(self, target_path: str) -> dict[str, object] | None:
        parsed = _collection_upload_target_parts(target_path)
        if parsed is None:
            return None
        normalized_collection_id, normalized_path = parsed
        staged_webhook_details: dict[str, object] | None = None
        staged_notify: dict[str, object] | None = None

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None or upload.state in {"canceled", "expired"}:
                return None
            file_record = session.get(
                CollectionUploadFileRecord,
                (normalized_collection_id, normalized_path),
            )
            if file_record is None:
                return None

            file_record.uploaded_bytes = file_record.bytes
            file_record.upload_expires_at = None
            if upload.state != "open" and _collection_upload_is_complete_for_session(
                session,
                normalized_collection_id,
            ):
                was_archiving = upload.state == "archiving"
                _ensure_collection_upload_archiving(upload)
                if not was_archiving:
                    staged_webhook_details = _collection_upload_webhook_details(upload)
                    staged_notify = _decode_collection_notify_json(upload.notify_json)
            payload = _collection_upload_file_registration_payload(upload, file_record)
        if staged_webhook_details is not None:
            _post_collection_operator_webhook(
                self._config,
                event="collections.upload_staged",
                collection_id=normalized_collection_id,
                details=staged_webhook_details,
                notify=staged_notify,
            )
        return payload

    def _register_upload_session_file_record(
        self,
        session: Session,
        *,
        normalized_collection_id: str,
        normalized_file: _UploadManifestEntry,
    ) -> tuple[CollectionUploadRecord, CollectionUploadFileRecord]:
        upload = session.get(CollectionUploadRecord, normalized_collection_id)
        if upload is None:
            raise NotFound(f"collection upload session not found: {normalized_collection_id}")
        _expire_open_collection_upload_if_idle(
            upload,
            upload_store=self._upload_store,
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

        _ensure_active_upload_limit_allows(
            session,
            incoming_bytes=int(normalized_file["bytes"]),
            limit_bytes=self._config.unburned_collection_bytes_limit,
        )
        file_order = (
            session.scalar(
                select(func.max(CollectionUploadFileRecord.file_order)).where(
                    CollectionUploadFileRecord.collection_id == normalized_collection_id
                )
            )
            or 0
        ) + 1
        file_record = CollectionUploadFileRecord(
            collection_id=normalized_collection_id,
            path=normalized_file["path"],
            file_order=file_order,
            bytes=normalized_file["bytes"],
            sha256=normalized_file["sha256"],
            uploaded_bytes=0,
            upload_expires_at=None,
            tus_url=None,
        )
        session.add(file_record)
        return upload, file_record

    def complete_upload_session(self, collection_id: str) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)
        staged_webhook_details: dict[str, object] | None = None
        staged_notify: dict[str, object] | None = None

        with session_scope(self._session_factory) as session:
            collection = session.get(CollectionRecord, normalized_collection_id)
            if collection is not None:
                return _finalized_collection_upload_payload(session, collection)

            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None:
                raise NotFound(f"collection upload session not found: {normalized_collection_id}")
            upload = _sync_and_expire_collection_upload(
                session,
                upload,
                upload_store=self._upload_store,
                session_idle_ttl=self._upload_session_idle_ttl,
                force_offset_sync=True,
            )
            if upload is None:
                raise NotFound(f"collection upload session not found: {normalized_collection_id}")
            if upload.state in {"canceled", "expired"}:
                raise Conflict(
                    f"collection upload session is {upload.state}: {normalized_collection_id}"
                )
            if upload.state in {"archiving", "failed"}:
                return _collection_upload_payload(
                    collection_id=upload.collection_id,
                    ingest_source=upload.ingest_source,
                    files=upload.files,
                    state=_collection_upload_session_state(upload),
                    collection=None,
                    upload=upload,
                )
            if upload.state != "open":
                raise Conflict(f"collection upload session is not open: {normalized_collection_id}")
            if not upload.files:
                raise Conflict("collection upload session cannot complete without files")
            if not _collection_upload_is_complete_for_session(session, normalized_collection_id):
                raise Conflict("collection upload session still has missing file bytes")

            _ensure_collection_upload_archiving(upload)
            now = _utc_now()
            upload.closed_at = now
            upload.last_activity_at = now
            staged_webhook_details = _collection_upload_webhook_details(upload)
            staged_notify = _decode_collection_notify_json(upload.notify_json)
            payload = _collection_upload_payload(
                collection_id=upload.collection_id,
                ingest_source=upload.ingest_source,
                files=upload.files,
                state=_collection_upload_session_state(upload),
                collection=None,
                upload=upload,
            )
        if staged_webhook_details is not None:
            _post_collection_operator_webhook(
                self._config,
                event="collections.upload_staged",
                collection_id=normalized_collection_id,
                details=staged_webhook_details,
                notify=staged_notify,
            )
        return payload

    def cancel_upload_session(self, collection_id: str) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)

        with session_scope(self._session_factory) as session:
            if session.get(CollectionRecord, normalized_collection_id) is not None:
                raise Conflict(f"collection is already finalized: {normalized_collection_id}")
            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None:
                raise NotFound(f"collection upload session not found: {normalized_collection_id}")
            upload = _sync_and_expire_collection_upload(
                session,
                upload,
                upload_store=self._upload_store,
                session_idle_ttl=self._upload_session_idle_ttl,
            )
            if upload is None:
                raise NotFound(f"collection upload session not found: {normalized_collection_id}")
            if upload.state in {"canceled", "expired"}:
                payload = _collection_upload_payload(
                    collection_id=upload.collection_id,
                    ingest_source=upload.ingest_source,
                    files=upload.files,
                    state=str(upload.state),
                    collection=None,
                    upload=upload,
                )
                session.delete(upload)
                return payload
            if upload.state != "open":
                raise Conflict(
                    "collection upload session cannot be canceled after completion handoff"
                )

            _forget_collection_upload(upload, self._upload_store)
            upload.files.clear()
            now = _utc_now()
            upload.state = "canceled"
            upload.closed_at = now
            upload.last_activity_at = now
            payload = _collection_upload_payload(
                collection_id=upload.collection_id,
                ingest_source=upload.ingest_source,
                files=upload.files,
                state="canceled",
                collection=None,
                upload=upload,
            )
            session.delete(upload)
            return payload

    def get_upload(self, collection_id: str) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None:
                raise NotFound(f"collection upload not found: {normalized_collection_id}")

            upload = _sync_and_expire_collection_upload(
                session,
                upload,
                upload_store=self._upload_store,
                session_idle_ttl=self._upload_session_idle_ttl,
            )
            if upload is None:
                raise NotFound(f"collection upload not found: {normalized_collection_id}")

            if upload.state == "open":
                return _collection_upload_payload(
                    collection_id=normalized_collection_id,
                    ingest_source=upload.ingest_source,
                    files=upload.files,
                    state="open",
                    collection=None,
                    upload=upload,
                )
            if upload.state in {"canceled", "expired"}:
                return _collection_upload_payload(
                    collection_id=normalized_collection_id,
                    ingest_source=upload.ingest_source,
                    files=upload.files,
                    state=str(upload.state),
                    collection=None,
                    upload=upload,
                )
            if _collection_upload_is_complete(upload.files):
                _ensure_collection_upload_archiving(upload)
                return _collection_upload_payload(
                    collection_id=normalized_collection_id,
                    ingest_source=upload.ingest_source,
                    files=upload.files,
                    state=_collection_upload_session_state(upload),
                    collection=None,
                    upload=upload,
                )

            return _collection_upload_payload(
                collection_id=normalized_collection_id,
                ingest_source=upload.ingest_source,
                files=upload.files,
                state="uploading",
                collection=None,
                upload=upload,
            )

    def create_or_resume_file_upload(self, collection_id: str, path: str) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)
        normalized_path = _normalize_relpath_or_raise(path)

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None:
                raise NotFound(f"collection upload not found: {normalized_collection_id}")
            _expire_open_collection_upload_if_idle(
                upload,
                upload_store=self._upload_store,
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
            target_path = _collection_upload_target_path(normalized_collection_id, normalized_path)
            _expire_collection_upload_file(file_record, self._upload_store)
            updated, tus_url = create_or_resume_upload_state(
                current=_upload_lifecycle_state(file_record),
                target_path=target_path,
                length=file_record.bytes,
                upload_store=self._upload_store,
                ttl=self._upload_ttl,
            )
            _apply_upload_lifecycle_state(file_record, updated)

            return _collection_file_upload_payload(file_record, tus_url=tus_url)

    def append_upload_chunk(
        self,
        collection_id: str,
        path: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)
        normalized_path = _normalize_relpath_or_raise(path)
        staged_webhook_details: dict[str, object] | None = None
        staged_notify: dict[str, object] | None = None

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None:
                raise NotFound(f"collection upload not found: {normalized_collection_id}")
            _expire_open_collection_upload_if_idle(
                upload,
                upload_store=self._upload_store,
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
            if offset != file_record.uploaded_bytes:
                raise Conflict(
                    f"collection upload offset for {normalized_path} is "
                    f"{offset}, expected {file_record.uploaded_bytes}"
                )

            next_offset, _ = self._upload_store.append_upload_chunk(
                file_record.tus_url,
                offset=offset,
                checksum=checksum,
                content=content,
            )
            file_record.uploaded_bytes = next_offset

            if next_offset >= file_record.bytes:
                file_record.upload_expires_at = None
                if offset == 0 and len(content) == file_record.bytes:
                    content_digest = _sha256_hex(content)
                else:
                    target_path = _collection_upload_target_path(
                        normalized_collection_id,
                        normalized_path,
                    )
                    content_digest = _sha256_hex_chunks(self._upload_store.iter_target(target_path))
                if content_digest != file_record.sha256:
                    raise HashMismatch("sha256 did not match expected file hash")
            else:
                file_record.upload_expires_at = upload_expiry_timestamp(self._upload_ttl)

            if upload.state != "open" and _collection_upload_is_complete_for_session(
                session, normalized_collection_id
            ):
                was_archiving = upload.state == "archiving"
                _ensure_collection_upload_archiving(upload)
                if not was_archiving:
                    staged_webhook_details = _collection_upload_webhook_details(upload)
                    staged_notify = _decode_collection_notify_json(upload.notify_json)

            response: dict[str, object] = {
                "offset": file_record.uploaded_bytes,
                "length": file_record.bytes,
                "expires_at": file_record.upload_expires_at,
            }
        if staged_webhook_details is not None:
            _post_collection_operator_webhook(
                self._config,
                event="collections.upload_staged",
                collection_id=normalized_collection_id,
                details=staged_webhook_details,
                notify=staged_notify,
            )
        return response

    def get_file_upload(self, collection_id: str, path: str) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)
        normalized_path = _normalize_relpath_or_raise(path)

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None:
                raise NotFound(f"collection upload not found: {normalized_collection_id}")

            upload = _sync_and_expire_collection_upload(
                session,
                upload,
                upload_store=self._upload_store,
                session_idle_ttl=self._upload_session_idle_ttl,
            )
            if upload is None:
                raise NotFound(f"collection upload not found: {normalized_collection_id}")

            file_record = _get_upload_file(upload.files, normalized_path)
            if file_record.tus_url is None:
                raise NotFound(f"collection upload file is not resumable: {normalized_path}")
            return _collection_file_upload_payload(file_record, tus_url=file_record.tus_url)

    def cancel_file_upload(self, collection_id: str, path: str) -> None:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)
        normalized_path = _normalize_relpath_or_raise(path)

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None:
                raise NotFound(f"collection upload not found: {normalized_collection_id}")

            upload = _sync_and_expire_collection_upload(
                session,
                upload,
                upload_store=self._upload_store,
                session_idle_ttl=self._upload_session_idle_ttl,
            )
            if upload is None:
                raise NotFound(f"collection upload not found: {normalized_collection_id}")

            file_record = _get_upload_file(upload.files, normalized_path)
            if file_record.tus_url is None:
                raise NotFound(f"collection upload file is not resumable: {normalized_path}")

            self._upload_store.cancel_upload(file_record.tus_url)
            self._upload_store.delete_target(
                _collection_upload_target_path(normalized_collection_id, normalized_path)
            )
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
            uploads = session.scalars(
                select(CollectionUploadRecord).options(selectinload(CollectionUploadRecord.files))
            ).all()
            for upload in uploads:
                _sync_and_expire_collection_upload(
                    session,
                    upload,
                    upload_store=self._upload_store,
                    session_idle_ttl=self._upload_session_idle_ttl,
                )

    def get(
        self,
        collection_id: str,
        *,
        coverage_path_limit: int = 100,
    ) -> CollectionSummary:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)
        if coverage_path_limit < 0:
            raise BadRequest("coverage_path_limit must be at least 0")

        with session_scope(self._session_factory) as session:
            row = session.get(CollectionOperatorSummaryRecord, normalized_collection_id)
            if row is None:
                collection = session.get(CollectionRecord, normalized_collection_id)
                if collection is None:
                    raise NotFound(f"collection not found: {normalized_collection_id}")
                raise Conflict(
                    f"collection operator summary projection is missing: {normalized_collection_id}"
                )
            return replace(
                _summary_from_operator_projection(row),
                image_coverage=_bounded_collection_image_coverage(
                    session,
                    normalized_collection_id,
                    path_limit=coverage_path_limit,
                ),
            )

    def list(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        protection_state: str | None,
        sort: str = "id",
        order: str = "asc",
    ) -> CollectionListPage:
        if page < 1:
            raise BadRequest("page must be at least 1")
        if per_page < 1:
            raise BadRequest("per_page must be at least 1")
        if sort not in _COLLECTION_SORT_FIELDS:
            raise BadRequest(f"sort must be one of {', '.join(sorted(_COLLECTION_SORT_FIELDS))}")
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")
        if protection_state is not None and protection_state not in {
            "unprotected",
            "partially_protected",
            "protected",
        }:
            raise BadRequest(f"unsupported protection_state filter: {protection_state}")

        needle = q.casefold() if q else None
        with session_scope(self._session_factory) as session:
            filters: list[ColumnElement[bool]] = []
            if needle is not None:
                filters.append(
                    func.lower(CollectionOperatorSummaryRecord.collection_id).like(f"%{needle}%")
                )
            if protection_state is not None:
                filters.append(CollectionOperatorSummaryRecord.protection_state == protection_state)

            total = int(
                session.scalar(
                    select(func.count())
                    .select_from(CollectionOperatorSummaryRecord)
                    .where(*filters)
                )
                or 0
            )
            pages = (total + per_page - 1) // per_page if total else 0
            start = (page - 1) * per_page
            sort_column = (
                CollectionOperatorSummaryRecord.collection_id
                if sort == "id"
                else getattr(CollectionOperatorSummaryRecord, sort)
            )
            order_by = sort_column.desc() if order == "desc" else sort_column.asc()
            rows = session.scalars(
                select(CollectionOperatorSummaryRecord)
                .where(*filters)
                .order_by(order_by, CollectionOperatorSummaryRecord.collection_id.asc())
                .offset(start)
                .limit(per_page)
            ).all()

            return CollectionListPage(
                page=page,
                per_page=per_page,
                total=total,
                pages=pages,
                collections=[_summary_from_operator_projection(row) for row in rows],
            )


def _summary_from_operator_projection(row: CollectionOperatorSummaryRecord) -> CollectionSummary:
    bytes_total = int(row.bytes or 0)
    archived_bytes = int(row.archived_bytes or 0)
    protected_bytes = int(row.protected_bytes or 0)
    physical_bytes = int(row.physical_bytes or 0)
    archive_state = normalize_glacier_state(row.archive_state)
    glacier_bytes = bytes_total if archive_state is GlacierState.UPLOADED else 0
    physical_state = _recovery_coverage_state(
        covered_bytes=physical_bytes,
        total_bytes=bytes_total,
    )
    glacier_state = _recovery_coverage_state(
        covered_bytes=glacier_bytes,
        total_bytes=bytes_total,
    )
    available: list[str] = []
    if physical_state is RecoveryCoverageState.FULL:
        available.append("verified_physical")
    if glacier_state is RecoveryCoverageState.FULL:
        available.append("glacier")

    collection_manifest = None
    if row.has_archive:
        ots_state = "uploaded" if row.ots_object_path else "pending"
        if row.archive_state == "failed":
            ots_state = "failed"
        collection_manifest = CollectionManifestStatus(
            object_path=row.manifest_object_path,
            sha256=row.manifest_sha256,
            ots_object_path=row.ots_object_path,
            ots_state=ots_state,
            ots_sha256=row.ots_sha256,
        )

    return CollectionSummary(
        id=CollectionId(row.collection_id),
        files=int(row.files or 0),
        bytes=bytes_total,
        hot_bytes=int(row.hot_bytes or 0),
        archived_bytes=archived_bytes,
        protection_state=ProtectionState(row.protection_state or "unprotected"),
        protected_bytes=protected_bytes,
        recovery=CollectionRecoverySummary(
            verified_physical=RecoveryCoverage(
                state=physical_state,
                bytes=physical_bytes,
            ),
            glacier=RecoveryCoverage(
                state=glacier_state,
                bytes=glacier_bytes,
            ),
            available=tuple(available),
        ),
        image_coverage=[],
        glacier=GlacierArchiveStatus(
            state=archive_state,
            object_path=row.archive_object_path,
            stored_bytes=row.archive_stored_bytes,
            backend=row.archive_backend,
            storage_class=row.archive_storage_class,
            last_uploaded_at=row.archive_last_uploaded_at,
            last_verified_at=row.archive_last_verified_at,
            failure=row.archive_failure,
        ),
        collection_manifest=collection_manifest,
        archive_format=row.archive_format,
        compression=row.compression,
    )


def _bounded_collection_image_coverage(
    session: Session,
    collection_id: str,
    *,
    path_limit: int,
) -> list[CollectionCoverageImage]:
    rows = session.execute(
        select(CollectionImageOperatorSummaryRecord, ImageOperatorSummaryRecord)
        .join(
            ImageOperatorSummaryRecord,
            ImageOperatorSummaryRecord.image_id == CollectionImageOperatorSummaryRecord.image_id,
        )
        .where(CollectionImageOperatorSummaryRecord.collection_id == collection_id)
        .order_by(CollectionImageOperatorSummaryRecord.image_id.asc())
    ).all()
    image_ids = [image_row.image_id for _, image_row in rows]
    if not image_ids:
        return []

    paths_by_image: dict[str, list[str]] = {image_id: [] for image_id in image_ids}
    if path_limit > 0:
        for image_id in image_ids:
            path_rows = session.scalars(
                select(FinalizedImageCoveredPathRecord.path)
                .where(FinalizedImageCoveredPathRecord.image_id == image_id)
                .where(FinalizedImageCoveredPathRecord.collection_id == collection_id)
                .order_by(FinalizedImageCoveredPathRecord.path.asc())
                .limit(path_limit)
            ).all()
            paths_by_image[image_id] = list(path_rows)

    copy_rows = session.execute(
        select(
            ImageCopyRecord.image_id,
            ImageCopyRecord.copy_id,
            ImageCopyRecord.label_text,
            ImageCopyRecord.location,
            ImageCopyRecord.created_at,
            ImageCopyRecord.state,
            ImageCopyRecord.verification_state,
        )
        .where(ImageCopyRecord.image_id.in_(image_ids))
        .order_by(ImageCopyRecord.image_id.asc(), ImageCopyRecord.copy_id.asc())
    ).all()
    copies_by_image: dict[str, list[CopySummary]] = {image_id: [] for image_id in image_ids}
    for copy in copy_rows:
        copies_by_image.setdefault(copy.image_id, []).append(
            CopySummary(
                id=CopyId(copy.copy_id),
                volume_id=copy.image_id,
                label_text=copy.label_text or copy.copy_id,
                location=copy.location,
                created_at=copy.created_at,
                state=normalize_copy_state(copy.state),
                verification_state=normalize_verification_state(copy.verification_state),
            )
        )

    return [
        CollectionCoverageImage(
            id=ImageId(image_row.image_id),
            filename=image_row.filename,
            protection_state=ProtectionState(image_row.physical_protection_state or "unprotected"),
            physical_copies_required=int(image_row.physical_copies_required or 0),
            physical_copies_registered=int(image_row.physical_copies_registered or 0),
            physical_copies_verified=int(image_row.physical_copies_verified or 0),
            physical_copies_missing=int(image_row.physical_copies_missing or 0),
            covered_paths=paths_by_image.get(image_row.image_id, []),
            copies=copies_by_image.get(image_row.image_id, []),
            covered_paths_total=int(collection_image_row.covered_paths_total or 0),
        )
        for collection_image_row, image_row in rows
    ]


def _collection_list_summaries(
    session: Session,
    collection_ids: Sequence[str],
) -> list[CollectionSummary]:
    if not collection_ids:
        return []

    stats_by_collection = {
        collection_id: _CollectionListStats() for collection_id in collection_ids
    }

    file_stat_rows = session.execute(
        select(
            CollectionFileRecord.collection_id,
            func.count(CollectionFileRecord.path).label("files"),
            func.coalesce(func.sum(CollectionFileRecord.bytes), 0).label("bytes"),
            func.coalesce(
                func.sum(
                    case(
                        (CollectionFileRecord.hot.is_(True), CollectionFileRecord.bytes),
                        else_=0,
                    )
                ),
                0,
            ).label("hot_bytes"),
            func.coalesce(
                func.sum(
                    case(
                        (CollectionFileRecord.archived.is_(True), CollectionFileRecord.bytes),
                        else_=0,
                    )
                ),
                0,
            ).label("archived_bytes"),
        )
        .where(CollectionFileRecord.collection_id.in_(collection_ids))
        .group_by(CollectionFileRecord.collection_id)
    ).all()
    for row in file_stat_rows:
        stats = stats_by_collection[row.collection_id]
        stats.files = int(row.files or 0)
        stats.bytes = int(row.bytes or 0)
        stats.hot_bytes = int(row.hot_bytes or 0)
        stats.archived_bytes = int(row.archived_bytes or 0)

    archive_rows = session.scalars(
        select(CollectionArchiveRecord).where(
            CollectionArchiveRecord.collection_id.in_(collection_ids)
        )
    ).all()
    archives_by_collection = {archive.collection_id: archive for archive in archive_rows}

    registered_copy_count = func.coalesce(
        func.sum(
            case(
                (ImageCopyRecord.state.in_(("registered", "verified")), 1),
                (ImageCopyRecord.state.is_(None), 1),
                else_=0,
            )
        ),
        0,
    )
    registered_counts = (
        select(
            ImageCopyRecord.image_id.label("image_id"),
            registered_copy_count.label("registered_count"),
        )
        .group_by(ImageCopyRecord.image_id)
        .subquery()
    )
    required_copy_count = case(
        (
            FinalizedImageRecord.required_copy_count.is_(None),
            DEFAULT_REQUIRED_PHYSICAL_COPIES,
        ),
        (
            FinalizedImageRecord.required_copy_count <= 0,
            DEFAULT_REQUIRED_PHYSICAL_COPIES,
        ),
        else_=FinalizedImageRecord.required_copy_count,
    )
    image_states = (
        select(
            FinalizedImageRecord.image_id.label("image_id"),
            case(
                (
                    func.coalesce(registered_counts.c.registered_count, 0) >= required_copy_count,
                    1,
                ),
                else_=0,
            ).label("is_protected"),
            case(
                (
                    func.coalesce(registered_counts.c.registered_count, 0) > 0,
                    1,
                ),
                else_=0,
            ).label("has_registered"),
        )
        .outerjoin(
            registered_counts,
            registered_counts.c.image_id == FinalizedImageRecord.image_id,
        )
        .subquery()
    )

    path_protection = (
        select(
            FinalizedImageCoveredPathRecord.collection_id,
            FinalizedImageCoveredPathRecord.path,
            func.min(image_states.c.is_protected).label("all_protected"),
            func.max(image_states.c.has_registered).label("has_registered"),
        )
        .join(
            image_states,
            image_states.c.image_id == FinalizedImageCoveredPathRecord.image_id,
        )
        .where(FinalizedImageCoveredPathRecord.collection_id.in_(collection_ids))
        .group_by(
            FinalizedImageCoveredPathRecord.collection_id,
            FinalizedImageCoveredPathRecord.path,
        )
        .subquery()
    )
    protection_rows = session.execute(
        select(
            CollectionFileRecord.collection_id,
            func.coalesce(
                func.sum(
                    case(
                        (
                            path_protection.c.all_protected == 1,
                            CollectionFileRecord.bytes,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("protected_bytes"),
            func.coalesce(func.max(path_protection.c.has_registered), 0).label(
                "has_registered_image"
            ),
        )
        .outerjoin(
            path_protection,
            and_(
                path_protection.c.collection_id == CollectionFileRecord.collection_id,
                path_protection.c.path == CollectionFileRecord.path,
            ),
        )
        .where(CollectionFileRecord.collection_id.in_(collection_ids))
        .group_by(CollectionFileRecord.collection_id)
    ).all()
    for row in protection_rows:
        stats = stats_by_collection[row.collection_id]
        stats.protected_bytes = int(row.protected_bytes or 0)
        stats.has_registered_image = bool(row.has_registered_image)

    path_part_stats = (
        select(
            FinalizedImageCoveragePartRecord.collection_id,
            FinalizedImageCoveragePartRecord.path,
            func.max(
                case(
                    (
                        and_(
                            FinalizedImageCoveragePartRecord.part_count == 1,
                            FinalizedImageCoveragePartRecord.part_index == 0,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("has_single_full_part"),
            func.min(FinalizedImageCoveragePartRecord.part_count).label("min_part_count"),
            func.max(FinalizedImageCoveragePartRecord.part_count).label("max_part_count"),
            func.count(func.distinct(FinalizedImageCoveragePartRecord.part_index)).label(
                "present_parts"
            ),
        )
        .join(
            image_states,
            image_states.c.image_id == FinalizedImageCoveragePartRecord.image_id,
        )
        .where(FinalizedImageCoveragePartRecord.collection_id.in_(collection_ids))
        .where(image_states.c.has_registered == 1)
        .group_by(
            FinalizedImageCoveragePartRecord.collection_id,
            FinalizedImageCoveragePartRecord.path,
        )
        .subquery()
    )
    partial_physical_bytes_expr = cast(
        (CollectionFileRecord.bytes * path_part_stats.c.present_parts)
        / path_part_stats.c.min_part_count,
        Integer,
    )
    bounded_partial_physical_bytes_expr = case(
        (partial_physical_bytes_expr < 1, 1),
        else_=partial_physical_bytes_expr,
    )
    bounded_partial_physical_bytes_expr = case(
        (
            bounded_partial_physical_bytes_expr > CollectionFileRecord.bytes,
            CollectionFileRecord.bytes,
        ),
        else_=bounded_partial_physical_bytes_expr,
    )
    physical_bytes_expr = case(
        (
            path_part_stats.c.has_single_full_part == 1,
            CollectionFileRecord.bytes,
        ),
        (
            and_(
                path_part_stats.c.min_part_count == path_part_stats.c.max_part_count,
                path_part_stats.c.present_parts > 0,
            ),
            bounded_partial_physical_bytes_expr,
        ),
        else_=0,
    )
    physical_rows = session.execute(
        select(
            CollectionFileRecord.collection_id,
            func.coalesce(func.sum(physical_bytes_expr), 0).label("physical_bytes"),
        )
        .outerjoin(
            path_part_stats,
            and_(
                path_part_stats.c.collection_id == CollectionFileRecord.collection_id,
                path_part_stats.c.path == CollectionFileRecord.path,
            ),
        )
        .where(CollectionFileRecord.collection_id.in_(collection_ids))
        .group_by(CollectionFileRecord.collection_id)
    ).all()
    for row in physical_rows:
        stats_by_collection[row.collection_id].physical_bytes = int(row.physical_bytes or 0)

    summaries: list[CollectionSummary] = []
    for collection_id in collection_ids:
        stats = stats_by_collection[collection_id]
        bytes_total = stats.bytes
        archived_bytes = stats.archived_bytes
        protected_bytes = stats.protected_bytes
        physical_bytes = stats.physical_bytes

        archive = archives_by_collection.get(collection_id)
        archive_uploaded = (
            archive is not None and normalize_glacier_state(archive.state) == GlacierState.UPLOADED
        )
        glacier_bytes = bytes_total if archive_uploaded else 0
        physical_state = _recovery_coverage_state(
            covered_bytes=physical_bytes,
            total_bytes=bytes_total,
        )
        glacier_state = _recovery_coverage_state(
            covered_bytes=glacier_bytes,
            total_bytes=bytes_total,
        )
        available: list[str] = []
        if physical_state is RecoveryCoverageState.FULL:
            available.append("verified_physical")
        if glacier_state is RecoveryCoverageState.FULL:
            available.append("glacier")

        summaries.append(
            CollectionSummary(
                id=CollectionId(collection_id),
                files=stats.files,
                bytes=bytes_total,
                hot_bytes=stats.hot_bytes,
                archived_bytes=archived_bytes,
                protection_state=collection_protection_state(
                    bytes_total=bytes_total,
                    protected_bytes=protected_bytes,
                    archived_bytes=archived_bytes,
                    image_states=(
                        (ProtectionState.PARTIALLY_PROTECTED,) if stats.has_registered_image else ()
                    ),
                ),
                protected_bytes=protected_bytes,
                recovery=CollectionRecoverySummary(
                    verified_physical=RecoveryCoverage(
                        state=physical_state,
                        bytes=physical_bytes,
                    ),
                    glacier=RecoveryCoverage(
                        state=glacier_state,
                        bytes=glacier_bytes,
                    ),
                    available=tuple(available),
                ),
                glacier=_collection_glacier_status(archive),
                collection_manifest=_collection_manifest_status(archive),
                archive_format=archive.archive_format if archive is not None else None,
                compression=archive.compression if archive is not None else None,
            )
        )
    return summaries


def _collection_file_summary_rows(
    session: Session,
    collection_ids: Sequence[str],
) -> list[_CollectionFileSummaryRow]:
    if not collection_ids:
        return []
    rows = session.execute(
        select(
            CollectionFileRecord.collection_id,
            CollectionFileRecord.path,
            CollectionFileRecord.bytes,
            CollectionFileRecord.hot,
            CollectionFileRecord.archived,
        )
        .where(CollectionFileRecord.collection_id.in_(collection_ids))
        .order_by(CollectionFileRecord.collection_id.asc(), CollectionFileRecord.path.asc())
    ).all()
    return [
        _CollectionFileSummaryRow(
            collection_id=row.collection_id,
            path=row.path,
            bytes=row.bytes,
            hot=row.hot,
            archived=row.archived,
        )
        for row in rows
    ]


def _finalized_image_protection_states(session: Session) -> dict[str, ProtectionState]:
    image_rows = session.execute(
        select(FinalizedImageRecord.image_id, FinalizedImageRecord.required_copy_count)
    ).all()
    registered_counts: dict[str, int] = {row.image_id: 0 for row in image_rows}
    copy_rows = session.execute(select(ImageCopyRecord.image_id, ImageCopyRecord.state)).all()
    for copy in copy_rows:
        if copy_counts_toward_protection(copy.state):
            registered_counts[copy.image_id] = registered_counts.get(copy.image_id, 0) + 1
    return {
        row.image_id: image_protection_state(
            required_copy_count=normalize_required_copy_count(row.required_copy_count),
            registered_copy_count=registered_counts.get(row.image_id, 0),
        )
        for row in image_rows
    }


def _finalized_image_registered_counts(session: Session) -> dict[str, int]:
    image_ids = session.scalars(select(FinalizedImageRecord.image_id)).all()
    registered_counts: dict[str, int] = {image_id: 0 for image_id in image_ids}
    copy_rows = session.execute(select(ImageCopyRecord.image_id, ImageCopyRecord.state)).all()
    for copy in copy_rows:
        if copy_counts_toward_protection(copy.state):
            registered_counts[copy.image_id] = registered_counts.get(copy.image_id, 0) + 1
    return registered_counts


def _path_recoverable_bytes_from_registered_images(
    total_bytes: int,
    path: str,
    *,
    image_ids: set[str],
    recovery_parts_by_image_path: dict[tuple[str, str], _RecoveryParts],
    registered_counts: dict[str, int],
) -> int:
    expected_part_count: int | None = None
    present_parts: set[int] = set()
    for image_id in image_ids:
        if registered_counts.get(image_id, 0) <= 0:
            continue
        recovery_parts = recovery_parts_by_image_path.get((image_id, path))
        if recovery_parts is None:
            continue
        if recovery_parts.part_count == 1 and recovery_parts.present_parts == frozenset({0}):
            return total_bytes
        if expected_part_count is None:
            expected_part_count = recovery_parts.part_count
        elif expected_part_count != recovery_parts.part_count:
            return 0
        present_parts.update(recovery_parts.present_parts)

    if expected_part_count is None or not present_parts:
        return 0
    return min(
        total_bytes,
        max(1, (total_bytes * len(present_parts)) // expected_part_count),
    )


def _normalize_collection_id_or_raise(raw: str) -> str:
    try:
        return normalize_collection_id(raw)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc


def _normalize_upload_slug_or_raise(raw: str) -> str:
    try:
        return normalize_upload_slug(raw)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc


def _normalize_upload_timestamp_or_raise(raw: str) -> str:
    try:
        return normalize_upload_timestamp(raw)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc


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


def _decode_collection_notify_json(raw: str | None) -> dict[str, object] | None:
    return decode_collection_notify_json(raw, log=_LOG)


def _collection_upload_manifest_fingerprint(
    files: Sequence[_UploadManifestEntry | _StoredManifestEntry],
) -> str:
    payload = [_manifest_entry_payload(file_record) for file_record in files]
    content = json.dumps(
        sorted(payload, key=lambda item: item["path"]),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _manifest_entry_payload(
    file_record: _UploadManifestEntry | _StoredManifestEntry,
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


def _collection_id_upload_slug(collection_id: str) -> str | None:
    leaf = collection_id.rsplit("/", 1)[-1]
    if "__" not in leaf:
        return None
    return leaf.split("__", 1)[1]


def _collection_id_for_upload_timestamp(upload_slug: str, upload_timestamp: str) -> str:
    return f"{upload_timestamp[:4]}/{upload_timestamp}__{upload_slug}"


def _ensure_requested_collection_id_matches(
    existing_collection_id: str,
    requested_collection_id: str | None,
) -> None:
    if requested_collection_id is None or existing_collection_id == requested_collection_id:
        return
    raise Conflict(
        "collection upload already exists for this slug and manifest with a different "
        f"timestamp: {existing_collection_id}"
    )


def _find_matching_collection(
    session: Session,
    *,
    upload_slug: str,
    manifest_fingerprint: str,
) -> CollectionRecord | None:
    collections = session.scalars(
        select(CollectionRecord)
        .options(selectinload(CollectionRecord.files))
        .options(selectinload(CollectionRecord.archive))
        .order_by(CollectionRecord.id.asc())
    ).all()
    for collection in collections:
        if _collection_id_upload_slug(collection.id) != upload_slug:
            continue
        if _collection_upload_manifest_fingerprint(collection.files) == manifest_fingerprint:
            return collection
    return None


def _find_matching_upload(
    session: Session,
    *,
    upload_slug: str,
    manifest_fingerprint: str,
    upload_store: UploadStore,
) -> CollectionUploadRecord | None:
    uploads = session.scalars(
        select(CollectionUploadRecord)
        .options(selectinload(CollectionUploadRecord.files))
        .order_by(CollectionUploadRecord.collection_id.asc())
    ).all()
    for upload in uploads:
        if upload.state in {"open", "canceled", "expired"}:
            continue
        if _collection_id_upload_slug(upload.collection_id) != upload_slug:
            continue
        if _collection_upload_manifest_fingerprint(upload.files) != manifest_fingerprint:
            continue
        return _sync_and_expire_collection_upload(
            session,
            upload,
            upload_store=upload_store,
        )
    return None


def _find_open_upload_session(
    session: Session,
    *,
    upload_slug: str,
) -> CollectionUploadRecord | None:
    uploads = session.scalars(
        select(CollectionUploadRecord)
        .options(selectinload(CollectionUploadRecord.files))
        .where(CollectionUploadRecord.state == "open")
        .order_by(CollectionUploadRecord.collection_id.asc())
    ).all()
    for upload in uploads:
        if _collection_id_upload_slug(upload.collection_id) == upload_slug:
            return upload
    return None


def _mint_collection_id(session: Session, upload_slug: str) -> str:
    current = _utc_now_dt().replace(microsecond=0)
    while True:
        stamp = current.strftime("%Y%m%dT%H%M%SZ")
        collection_id = f"{current:%Y}/{stamp}__{upload_slug}"
        if (
            session.get(CollectionRecord, collection_id) is None
            and session.get(CollectionUploadRecord, collection_id) is None
        ):
            return collection_id
        current += timedelta(seconds=1)


def _ensure_collection_id_unused(session: Session, collection_id: str) -> None:
    if session.get(CollectionRecord, collection_id) is not None:
        raise Conflict(f"collection already exists: {collection_id}")
    if session.get(CollectionUploadRecord, collection_id) is not None:
        raise Conflict(f"collection upload already exists with different manifest: {collection_id}")


def _ensure_collection_upload_conflict_free(session: Session, collection_id: str) -> None:
    committed_ids = session.scalars(select(CollectionRecord.id)).all()
    in_progress_ids = session.scalars(select(CollectionUploadRecord.collection_id)).all()
    conflict = find_collection_id_conflict(
        [
            *committed_ids,
            *(current for current in in_progress_ids if current != collection_id),
        ],
        collection_id,
    )
    if conflict is not None:
        raise Conflict(f"collection id conflicts with existing collection: {conflict}")


def _ensure_unburned_collection_limit_allows(
    session: Session,
    *,
    incoming_bytes: int,
    limit_bytes: int,
) -> None:
    if limit_bytes <= 0:
        return

    current_bytes = _unburned_collection_bytes(session)
    projected_bytes = current_bytes + incoming_bytes
    if projected_bytes <= limit_bytes:
        return

    raise Conflict(
        "unburned collection limit exceeded: "
        f"{projected_bytes} bytes would exceed the configured {limit_bytes} byte limit; "
        "finalize, burn, and register enough physical image copies before uploading "
        "new collections"
    )


def _ensure_active_upload_limit_allows(
    session: Session,
    *,
    incoming_bytes: int,
    limit_bytes: int,
) -> None:
    if limit_bytes <= 0:
        return

    current_bytes = _active_collection_upload_bytes(session)
    projected_bytes = current_bytes + incoming_bytes
    if projected_bytes <= limit_bytes:
        return

    raise Conflict(
        "unburned collection limit exceeded: "
        f"{projected_bytes} bytes would exceed the configured {limit_bytes} byte limit; "
        "finalize, burn, and register enough physical image copies before uploading "
        "new collections"
    )


def _active_collection_upload_bytes(session: Session) -> int:
    value = session.scalar(
        select(func.coalesce(func.sum(CollectionUploadFileRecord.bytes), 0))
        .join(
            CollectionUploadRecord,
            CollectionUploadRecord.collection_id == CollectionUploadFileRecord.collection_id,
        )
        .where(CollectionUploadRecord.state != "finalized")
    )
    return int(value or 0)


def _unburned_collection_bytes(session: Session) -> int:
    upload_bytes = _active_collection_upload_bytes(session)
    committed_unprotected_bytes = _committed_unprotected_collection_bytes(session)
    return upload_bytes + committed_unprotected_bytes


def _committed_unprotected_collection_bytes(session: Session) -> int:
    file_rows = session.execute(
        select(
            CollectionFileRecord.collection_id,
            CollectionFileRecord.path,
            CollectionFileRecord.bytes,
        )
    ).all()
    if not file_rows:
        return 0

    coverage_rows = session.execute(
        select(
            FinalizedImageCoveredPathRecord.collection_id,
            FinalizedImageCoveredPathRecord.path,
            FinalizedImageCoveredPathRecord.image_id,
        )
    ).all()
    if not coverage_rows:
        return int(sum(row.bytes for row in file_rows))

    image_ids = sorted({row.image_id for row in coverage_rows})
    image_rows = session.execute(
        select(
            FinalizedImageRecord.image_id,
            FinalizedImageRecord.required_copy_count,
        ).where(FinalizedImageRecord.image_id.in_(image_ids))
    ).all()
    copy_rows = session.execute(
        select(
            ImageCopyRecord.image_id,
            ImageCopyRecord.state,
        ).where(ImageCopyRecord.image_id.in_(image_ids))
    ).all()

    registered_copy_counts: dict[str, int] = defaultdict(int)
    for copy_row in copy_rows:
        if copy_counts_toward_protection(copy_row.state):
            registered_copy_counts[copy_row.image_id] += 1

    image_states = {
        image_row.image_id: image_protection_state(
            required_copy_count=normalize_required_copy_count(image_row.required_copy_count),
            registered_copy_count=registered_copy_counts.get(image_row.image_id, 0),
        )
        for image_row in image_rows
    }
    coverage_by_file: dict[tuple[str, str], set[str]] = defaultdict(set)
    for coverage_row in coverage_rows:
        coverage_by_file[(coverage_row.collection_id, coverage_row.path)].add(coverage_row.image_id)

    unprotected_bytes = 0
    for file_row in file_rows:
        covered_image_ids = coverage_by_file.get((file_row.collection_id, file_row.path), set())
        if covered_image_ids and all(image_id in image_states for image_id in covered_image_ids):
            if all(image_states[image_id].value == "protected" for image_id in covered_image_ids):
                continue
        unprotected_bytes += file_row.bytes
    return int(unprotected_bytes)


def _validate_existing_upload_manifest(
    upload: CollectionUploadRecord, expected_files: Sequence[_UploadManifestEntry]
) -> None:
    current_files = [
        {
            "path": file_record.path,
            "bytes": file_record.bytes,
            "sha256": file_record.sha256,
        }
        for file_record in sorted(upload.files, key=lambda current: current.file_order)
    ]
    if current_files != list(expected_files):
        raise Conflict(f"collection upload manifest does not match: {upload.collection_id}")


def _get_upload_file(
    file_records: Iterable[CollectionUploadFileRecord], path: str
) -> CollectionUploadFileRecord:
    for file_record in file_records:
        if file_record.path == path:
            return file_record
    raise NotFound(f"collection upload file not found: {path}")


def _get_upload_file_record(
    session: Session,
    collection_id: str,
    path: str,
) -> CollectionUploadFileRecord:
    file_record = session.get(CollectionUploadFileRecord, (collection_id, path))
    if file_record is None:
        raise NotFound(f"collection upload file not found: {path}")
    return file_record


def _upload_lifecycle_state(file_record: CollectionUploadFileRecord) -> UploadLifecycleState:
    return UploadLifecycleState(
        tus_url=file_record.tus_url,
        uploaded_bytes=file_record.uploaded_bytes,
        upload_expires_at=file_record.upload_expires_at,
    )


def _apply_upload_lifecycle_state(
    file_record: CollectionUploadFileRecord, state: UploadLifecycleState
) -> None:
    file_record.tus_url = state.tus_url
    file_record.uploaded_bytes = state.uploaded_bytes
    file_record.upload_expires_at = state.upload_expires_at


def _collection_upload_target_path(collection_id: str, path: str) -> str:
    return f"/.riverhog/uploads/collections/{collection_id}/{path}"


def _collection_upload_target_parts(target_path: str) -> tuple[str, str] | None:
    normalized = target_path.lstrip("/")
    prefix = ".riverhog/uploads/collections/"
    if not normalized.startswith(prefix):
        return None
    rest = normalized.removeprefix(prefix)
    parts = rest.split("/", 2)
    if len(parts) != 3:
        return None
    collection_id = _normalize_collection_id_or_raise(f"{parts[0]}/{parts[1]}")
    relpath = _normalize_relpath_or_raise(parts[2])
    expected = _collection_upload_target_path(collection_id, relpath).lstrip("/")
    if normalized != expected:
        raise BadRequest("collection upload target path is not normalized")
    return collection_id, relpath


def _collection_file_upload_payload(
    file_record: CollectionUploadFileRecord,
    *,
    tus_url: str,
) -> dict[str, object]:
    return {
        "path": file_record.path,
        "protocol": "tus",
        "upload_url": tus_url,
        "offset": file_record.uploaded_bytes,
        "length": file_record.bytes,
        "checksum_algorithm": "sha256",
        "expires_at": file_record.upload_expires_at,
    }


def _sync_collection_upload_files(
    file_records: Sequence[CollectionUploadFileRecord],
    upload_store: UploadStore,
    *,
    force: bool = False,
) -> None:
    for file_record in file_records:
        updated = sync_upload_state(
            current=_upload_lifecycle_state(file_record),
            target_path=_collection_upload_target_path(file_record.collection_id, file_record.path),
            length=file_record.bytes,
            upload_store=upload_store,
            force=force,
        )
        _apply_upload_lifecycle_state(file_record, updated)


def _expire_collection_upload_files(
    file_records: Sequence[CollectionUploadFileRecord],
    upload_store: UploadStore,
) -> bool:
    expired_any = False
    for file_record in file_records:
        updated, expired = expire_upload_state(
            current=_upload_lifecycle_state(file_record),
            target_path=_collection_upload_target_path(file_record.collection_id, file_record.path),
            upload_store=upload_store,
        )
        _apply_upload_lifecycle_state(file_record, updated)
        expired_any = expired_any or expired
    return expired_any


def _expire_collection_upload_file(
    file_record: CollectionUploadFileRecord,
    upload_store: UploadStore,
) -> None:
    target_path = _collection_upload_target_path(file_record.collection_id, file_record.path)
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
            now = _utc_now()
            upload.state = "expired"
            upload.closed_at = now
            upload.last_activity_at = now
        return upload
    if expired_any and _collection_upload_has_no_live_file_state(upload.files):
        _forget_collection_upload(upload, upload_store)
        session.delete(upload)
        return None
    return upload


def _expire_open_collection_upload_if_idle(
    upload: CollectionUploadRecord,
    *,
    upload_store: UploadStore,
    session_idle_ttl: timedelta | None = None,
) -> None:
    if not _collection_upload_session_is_idle_expired(upload, ttl=session_idle_ttl):
        return
    _forget_collection_upload(upload, upload_store)
    upload.files.clear()
    now = _utc_now()
    upload.state = "expired"
    upload.closed_at = now
    upload.last_activity_at = now


def _touch_collection_upload(upload: CollectionUploadRecord) -> None:
    now = _utc_now()
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
    activity_at = _parse_utc_timestamp(upload.last_activity_at or upload.opened_at)
    if activity_at is None:
        return False
    return _utc_now_dt() >= activity_at + ttl


def _collection_upload_has_no_live_file_state(
    file_records: Sequence[CollectionUploadFileRecord],
) -> bool:
    return all(
        upload_state_name(uploaded_bytes=file_record.uploaded_bytes, length=file_record.bytes)
        == "pending"
        and file_record.tus_url is None
        and file_record.upload_expires_at is None
        for file_record in file_records
    )


def _forget_collection_upload(
    upload: CollectionUploadRecord,
    upload_store: UploadStore,
) -> None:
    for file_record in upload.files:
        if file_record.tus_url is not None:
            upload_store.cancel_upload(file_record.tus_url)
        upload_store.delete_target(
            _collection_upload_target_path(upload.collection_id, file_record.path)
        )


def _collection_upload_is_complete(file_records: Sequence[CollectionUploadFileRecord]) -> bool:
    return bool(file_records) and all(
        upload_state_name(uploaded_bytes=file_record.uploaded_bytes, length=file_record.bytes)
        == "uploaded"
        for file_record in file_records
    )


def _collection_upload_is_complete_for_session(session: Session, collection_id: str) -> bool:
    session.flush()
    remaining = session.scalar(
        select(func.count())
        .select_from(CollectionUploadFileRecord)
        .where(CollectionUploadFileRecord.collection_id == collection_id)
        .where(CollectionUploadFileRecord.uploaded_bytes < CollectionUploadFileRecord.bytes)
    )
    return remaining == 0


def _collection_upload_session_state(upload: CollectionUploadRecord) -> str:
    if upload.state in {"open", "canceled", "expired"}:
        return str(upload.state)
    if not _collection_upload_is_complete(upload.files):
        return "uploading"
    if upload.state == "failed":
        return "failed"
    return "archiving"


def _collection_upload_webhook_details(upload: CollectionUploadRecord) -> dict[str, object]:
    files = upload.files
    bytes_total = sum(file_record.bytes for file_record in files)
    uploaded_bytes = sum(file_record.uploaded_bytes for file_record in files)
    return {
        "state": _collection_upload_session_state(upload),
        "files_total": len(files),
        "files_uploaded": sum(
            file_record.uploaded_bytes >= file_record.bytes for file_record in files
        ),
        "bytes_total": bytes_total,
        "uploaded_bytes": uploaded_bytes,
    }


def _post_collection_operator_webhook(
    config: RuntimeConfig,
    *,
    event: str,
    collection_id: str,
    details: dict[str, object] | None = None,
    notify: Mapping[str, object] | None = None,
) -> None:
    post_collection_operator_webhook(
        config=config,
        event=event,
        collection_id=collection_id,
        details=details,
        notify=notify,
        post=post_webhook,
        log=_LOG,
    )


def _ensure_collection_upload_archiving(upload: CollectionUploadRecord) -> None:
    if upload.state not in {"archiving", "failed"}:
        upload.state = "archiving"
    if upload.archive_next_attempt_at is None and upload.state == "archiving":
        upload.archive_next_attempt_at = _utc_now()


def _finalize_collection_upload(
    session: Session,
    upload: CollectionUploadRecord,
    *,
    hot_store: HotStore,
    upload_store: UploadStore,
) -> CollectionSummary:
    collection = CollectionRecord(
        id=upload.collection_id,
        ingest_source=upload.ingest_source,
        notify_json=upload.notify_json,
    )
    session.add(collection)

    for file_record in sorted(upload.files, key=lambda current: current.file_order):
        target_path = _collection_upload_target_path(upload.collection_id, file_record.path)
        content_digest = _promote_upload_target_to_hot_store(
            hot_store,
            upload_store,
            collection_id=upload.collection_id,
            path=file_record.path,
            target_path=target_path,
            content_length=file_record.bytes,
        )
        if content_digest != file_record.sha256:
            hot_store.delete_collection_file(upload.collection_id, file_record.path)
            raise Conflict(
                "uploaded collection file sha256 did not match "
                f"expected digest for {upload.collection_id}/{file_record.path}"
            )
        upload_store.delete_target(target_path)
        collection.files.append(
            CollectionFileRecord(
                collection_id=upload.collection_id,
                path=file_record.path,
                bytes=file_record.bytes,
                sha256=content_digest,
                hot=True,
                archived=False,
            )
        )

    session.flush()
    session.refresh(collection)
    summary = _summary_from_records(upload.collection_id, collection.files)
    session.delete(upload)
    return summary


def _finalized_collection_upload_payload(
    session: Session,
    collection: CollectionRecord,
) -> dict[str, object]:
    (
        image_coverage,
        covered_paths,
        recovery_parts_by_image_path,
    ) = _collection_image_coverage(session, collection.id)
    summary = _summary_from_records(
        collection.id,
        collection.files,
        archive=collection.archive,
        image_coverage=image_coverage,
        covered_paths=covered_paths,
        recovery_parts_by_image_path=recovery_parts_by_image_path,
    )
    files = sorted(collection.files, key=lambda current: current.path)
    bytes_total = sum(file_record.bytes for file_record in files)
    archive = collection.archive
    return {
        "collection_id": collection.id,
        "ingest_source": collection.ingest_source,
        "state": "finalized",
        "files_total": len(files),
        "files_pending": 0,
        "files_partial": 0,
        "files_uploaded": len(files),
        "hot_promoted_files": len(files),
        "bytes_total": bytes_total,
        "uploaded_bytes": bytes_total,
        "hot_promoted_bytes": bytes_total,
        "missing_bytes": 0,
        "upload_state_expires_at": None,
        "latest_failure": None,
        "archive_phase": "completed",
        "archive_phase_updated_at": archive.last_verified_at if archive is not None else None,
        "archive_object_path": archive.object_path if archive is not None else None,
        "archive_uploaded_bytes": archive.stored_bytes if archive is not None else None,
        "archive_total_bytes": archive.stored_bytes if archive is not None else None,
        "archive_uploaded_parts": None,
        "archive_total_parts": None,
        "notify": _decode_collection_notify_json(collection.notify_json),
        "files": [
            {
                "path": file_record.path,
                "bytes": file_record.bytes,
                "sha256": file_record.sha256,
                "upload_state": "uploaded",
                "uploaded_bytes": file_record.bytes,
                "upload_state_expires_at": None,
            }
            for file_record in files
        ],
        "collection": _collection_summary_payload(summary),
    }


def _collection_upload_payload(
    *,
    collection_id: str,
    ingest_source: str | None,
    files: Sequence[CollectionUploadFileRecord],
    state: str,
    collection: CollectionSummary | None,
    upload: CollectionUploadRecord | None = None,
) -> dict[str, object]:
    upload_record = upload or (files[0].upload if files else None)
    files_total = len(files)
    files_pending = sum(
        1
        for file_record in files
        if upload_state_name(uploaded_bytes=file_record.uploaded_bytes, length=file_record.bytes)
        == "pending"
    )
    files_partial = sum(
        1
        for file_record in files
        if upload_state_name(uploaded_bytes=file_record.uploaded_bytes, length=file_record.bytes)
        == "partial"
    )
    files_uploaded = sum(
        1
        for file_record in files
        if upload_state_name(uploaded_bytes=file_record.uploaded_bytes, length=file_record.bytes)
        == "uploaded"
    )
    hot_promoted_files = sum(1 for file_record in files if file_record.hot_promoted_at is not None)
    uploaded_bytes = sum(file_record.uploaded_bytes for file_record in files)
    hot_promoted_bytes = sum(
        file_record.bytes for file_record in files if file_record.hot_promoted_at is not None
    )
    bytes_total = sum(file_record.bytes for file_record in files)
    expiries = [
        file_record.upload_expires_at
        for file_record in files
        if file_record.upload_expires_at is not None
    ]
    return {
        "collection_id": collection_id,
        "ingest_source": ingest_source,
        "state": state,
        "files_total": files_total,
        "files_pending": files_pending,
        "files_partial": files_partial,
        "files_uploaded": files_uploaded,
        "hot_promoted_files": hot_promoted_files,
        "bytes_total": bytes_total,
        "uploaded_bytes": uploaded_bytes,
        "hot_promoted_bytes": hot_promoted_bytes,
        "missing_bytes": max(bytes_total - uploaded_bytes, 0),
        "upload_state_expires_at": max(expiries) if expiries else None,
        "latest_failure": getattr(upload_record, "archive_failure", None),
        "archive_phase": getattr(upload_record, "archive_phase", None),
        "archive_phase_updated_at": getattr(upload_record, "archive_phase_updated_at", None),
        "archive_object_path": getattr(upload_record, "archive_object_path", None),
        "archive_uploaded_bytes": getattr(upload_record, "archive_multipart_uploaded_bytes", None),
        "archive_total_bytes": getattr(upload_record, "archive_multipart_content_length", None),
        "archive_uploaded_parts": getattr(upload_record, "archive_multipart_uploaded_parts", None),
        "archive_total_parts": getattr(upload_record, "archive_multipart_total_parts", None),
        "notify": _decode_collection_notify_json(
            getattr(upload_record, "notify_json", None),
        ),
        "files": [
            _collection_upload_file_payload(file_record)
            for file_record in sorted(files, key=lambda current: current.file_order)
        ],
        "collection": _collection_summary_payload(collection) if collection is not None else None,
    }


def _collection_upload_file_registration_payload(
    upload: CollectionUploadRecord,
    file_record: CollectionUploadFileRecord,
) -> dict[str, object]:
    return {
        "collection_id": upload.collection_id,
        "ingest_source": upload.ingest_source,
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
            uploaded_bytes=file_record.uploaded_bytes,
            length=file_record.bytes,
        ),
        "uploaded_bytes": file_record.uploaded_bytes,
        "upload_state_expires_at": file_record.upload_expires_at,
    }


def _collection_summary_payload(summary: CollectionSummary) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "files": summary.files,
        "bytes": summary.bytes,
        "hot_bytes": summary.hot_bytes,
        "archived_bytes": summary.archived_bytes,
        "pending_bytes": summary.pending_bytes,
        "glacier": {
            "state": summary.glacier.state.value,
            "object_path": summary.glacier.object_path,
            "stored_bytes": summary.glacier.stored_bytes,
            "backend": summary.glacier.backend,
            "storage_class": summary.glacier.storage_class,
            "last_uploaded_at": summary.glacier.last_uploaded_at,
            "last_verified_at": summary.glacier.last_verified_at,
            "failure": summary.glacier.failure,
        },
        "collection_manifest": _collection_manifest_payload(summary.collection_manifest),
        "archive_format": summary.archive_format,
        "compression": summary.compression,
        "protection_state": summary.protection_state.value,
        "protected_bytes": summary.protected_bytes,
        "image_coverage": [
            {
                "id": str(image.id),
                "filename": image.filename,
                "protection_state": image.protection_state.value,
                "physical_copies_required": image.physical_copies_required,
                "physical_copies_registered": image.physical_copies_registered,
                "physical_copies_verified": image.physical_copies_verified,
                "physical_copies_missing": image.physical_copies_missing,
                "covered_paths": list(image.covered_paths),
                "copies": [
                    {
                        "id": str(copy.id),
                        "volume_id": copy.volume_id,
                        "label_text": copy.label_text,
                        "location": copy.location,
                        "created_at": copy.created_at,
                        "state": copy.state.value,
                        "verification_state": copy.verification_state.value,
                        "history": [
                            {
                                "at": entry.at,
                                "event": entry.event,
                                "state": entry.state.value,
                                "verification_state": entry.verification_state.value,
                                "location": entry.location,
                            }
                            for entry in copy.history
                        ],
                    }
                    for copy in image.copies
                ],
            }
            for image in summary.image_coverage
        ],
    }


def _collection_manifest_payload(
    summary: CollectionManifestStatus | None,
) -> dict[str, object] | None:
    if summary is None:
        return None
    return {
        "object_path": summary.object_path,
        "sha256": summary.sha256,
        "ots_object_path": summary.ots_object_path,
        "ots_state": summary.ots_state,
    }


def _utc_now() -> str:
    from datetime import UTC, datetime  # noqa: PLC0415

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_hex(content: bytes) -> Sha256Hex:
    return Sha256Hex(hashlib.sha256(content).hexdigest())


def _sha256_hex_chunks(chunks: Iterable[bytes]) -> Sha256Hex:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return Sha256Hex(digest.hexdigest())


def _promote_upload_target_to_hot_store(
    hot_store: HotStore,
    upload_store: UploadStore,
    *,
    collection_id: str,
    path: str,
    target_path: str,
    content_length: int,
) -> Sha256Hex:
    digest = hashlib.sha256()

    def digesting_chunks() -> Iterable[bytes]:
        for chunk in upload_store.iter_target(target_path):
            digest.update(chunk)
            yield chunk

    hot_store.put_collection_file_stream(
        collection_id,
        path,
        digesting_chunks(),
        content_length=content_length,
    )
    return Sha256Hex(digest.hexdigest())


def _utc_now_dt() -> datetime:
    return datetime.now(UTC)


def _parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _summary_from_records(
    collection_id: str,
    file_records: Sequence[_CollectionFileLike],
    *,
    archive: CollectionArchiveRecord | None = None,
    image_coverage: Sequence[CollectionCoverageImage] = (),
    covered_paths: dict[str, set[str]] | None = None,
    recovery_parts_by_image_path: dict[tuple[str, str], _RecoveryParts] | None = None,
) -> CollectionSummary:
    bytes_total = sum(record.bytes for record in file_records)
    archived_bytes = sum(record.bytes for record in file_records if record.archived)
    protected_bytes = _protected_bytes(
        file_records,
        image_coverage=image_coverage,
        covered_paths=covered_paths or {},
    )
    recovery = _collection_recovery_summary(
        file_records,
        archive=archive,
        image_coverage=image_coverage,
        covered_paths=covered_paths or {},
        recovery_parts_by_image_path=recovery_parts_by_image_path or {},
    )
    protection_state = collection_protection_state(
        bytes_total=bytes_total,
        protected_bytes=protected_bytes,
        archived_bytes=archived_bytes,
        image_states=(image.protection_state for image in image_coverage),
    )
    return CollectionSummary(
        id=CollectionId(collection_id),
        files=len(file_records),
        bytes=bytes_total,
        hot_bytes=sum(record.bytes for record in file_records if record.hot),
        archived_bytes=archived_bytes,
        protection_state=protection_state,
        protected_bytes=protected_bytes,
        recovery=recovery,
        image_coverage=list(image_coverage),
        glacier=_collection_glacier_status(archive),
        collection_manifest=_collection_manifest_status(archive),
        archive_format=archive.archive_format if archive is not None else None,
        compression=archive.compression if archive is not None else None,
    )


def _collection_glacier_status(archive: CollectionArchiveRecord | None) -> GlacierArchiveStatus:
    if archive is None:
        return GlacierArchiveStatus()
    return GlacierArchiveStatus(
        state=normalize_glacier_state(archive.state),
        object_path=archive.object_path,
        stored_bytes=archive.stored_bytes,
        backend=archive.backend,
        storage_class=archive.storage_class,
        last_uploaded_at=archive.last_uploaded_at,
        last_verified_at=archive.last_verified_at,
        failure=archive.failure,
    )


def _collection_manifest_status(
    archive: CollectionArchiveRecord | None,
) -> CollectionManifestStatus | None:
    if archive is None:
        return None
    ots_state = "uploaded" if archive.ots_object_path else "pending"
    if archive.state == "failed":
        ots_state = "failed"
    return CollectionManifestStatus(
        object_path=archive.manifest_object_path,
        sha256=archive.manifest_sha256,
        ots_object_path=archive.ots_object_path,
        ots_state=ots_state,
        ots_sha256=archive.ots_sha256,
    )


def _collection_image_coverage(
    session: Session,
    collection_id: str,
) -> tuple[
    list[CollectionCoverageImage],
    dict[str, set[str]],
    dict[tuple[str, str], _RecoveryParts],
]:
    image_rows = session.execute(
        select(
            FinalizedImageRecord.image_id,
            FinalizedImageRecord.filename,
            FinalizedImageRecord.required_copy_count,
        )
        .join(FinalizedImageCoveredPathRecord)
        .where(FinalizedImageCoveredPathRecord.collection_id == collection_id)
        .distinct()
    ).all()
    image_ids = sorted(row.image_id for row in image_rows)
    if not image_ids:
        return [], {}, {}

    image_metadata = {row.image_id: (row.filename, row.required_copy_count) for row in image_rows}

    covered_path_rows = session.execute(
        select(
            FinalizedImageCoveredPathRecord.image_id,
            FinalizedImageCoveredPathRecord.path,
        )
        .where(FinalizedImageCoveredPathRecord.collection_id == collection_id)
        .order_by(
            FinalizedImageCoveredPathRecord.image_id.asc(),
            FinalizedImageCoveredPathRecord.path.asc(),
        )
    ).all()
    coverage_part_rows = session.execute(
        select(
            FinalizedImageCoveragePartRecord.image_id,
            FinalizedImageCoveragePartRecord.path,
            FinalizedImageCoveragePartRecord.part_index,
            FinalizedImageCoveragePartRecord.part_count,
        )
        .where(FinalizedImageCoveragePartRecord.collection_id == collection_id)
        .order_by(
            FinalizedImageCoveragePartRecord.image_id.asc(),
            FinalizedImageCoveragePartRecord.path.asc(),
            FinalizedImageCoveragePartRecord.part_index.asc(),
        )
    ).all()
    copy_rows = session.execute(
        select(
            ImageCopyRecord.image_id,
            ImageCopyRecord.copy_id,
            ImageCopyRecord.label_text,
            ImageCopyRecord.location,
            ImageCopyRecord.created_at,
            ImageCopyRecord.state,
            ImageCopyRecord.verification_state,
        )
        .where(ImageCopyRecord.image_id.in_(image_ids))
        .order_by(ImageCopyRecord.image_id.asc(), ImageCopyRecord.copy_id.asc())
    ).all()

    copies_by_image: dict[str, list[CopySummary]] = {}
    registered_copy_counts: dict[str, int] = {}
    verified_copy_counts: dict[str, int] = {}
    for copy in copy_rows:
        copies_by_image.setdefault(copy.image_id, []).append(
            CopySummary(
                id=CopyId(copy.copy_id),
                volume_id=copy.image_id,
                label_text=copy.label_text or copy.copy_id,
                location=copy.location,
                created_at=copy.created_at,
                state=normalize_copy_state(copy.state),
                verification_state=normalize_verification_state(copy.verification_state),
            )
        )
        if copy_counts_toward_protection(copy.state):
            registered_copy_counts[copy.image_id] = registered_copy_counts.get(copy.image_id, 0) + 1
        if copy_counts_as_verified(
            state=copy.state,
            verification_state=copy.verification_state,
        ):
            verified_copy_counts[copy.image_id] = verified_copy_counts.get(copy.image_id, 0) + 1

    image_paths_by_image: dict[str, set[str]] = {image_id: set() for image_id in image_ids}
    covered_paths: dict[str, set[str]] = {}
    for covered_path in covered_path_rows:
        covered_paths.setdefault(covered_path.path, set()).add(covered_path.image_id)
        image_paths_by_image.setdefault(covered_path.image_id, set()).add(covered_path.path)

    recovery_parts_by_image_path: dict[tuple[str, str], _RecoveryParts] = {}
    for part in coverage_part_rows:
        key = (part.image_id, part.path)
        current = recovery_parts_by_image_path.get(key)
        present_parts = frozenset({part.part_index})
        if current is None:
            recovery_parts_by_image_path[key] = _RecoveryParts(
                part_count=part.part_count,
                present_parts=present_parts,
            )
            continue
        recovery_parts_by_image_path[key] = _RecoveryParts(
            part_count=current.part_count,
            present_parts=current.present_parts | present_parts,
        )

    image_coverage: list[CollectionCoverageImage] = []
    for image_id in image_ids:
        filename, required_copy_count_value = image_metadata[image_id]
        required_copy_count = normalize_required_copy_count(required_copy_count_value)
        registered_copy_count = registered_copy_counts.get(image_id, 0)
        verified_copy_count = verified_copy_counts.get(image_id, 0)
        image_coverage.append(
            CollectionCoverageImage(
                id=ImageId(image_id),
                filename=filename,
                protection_state=image_protection_state(
                    required_copy_count=required_copy_count,
                    registered_copy_count=registered_copy_count,
                ),
                physical_copies_required=required_copy_count,
                physical_copies_registered=registered_copy_count,
                physical_copies_verified=verified_copy_count,
                physical_copies_missing=registered_copy_shortfall(
                    required_copy_count=required_copy_count,
                    registered_copy_count=registered_copy_count,
                ),
                covered_paths=sorted(image_paths_by_image.get(image_id, set())),
                copies=copies_by_image.get(image_id, []),
            )
        )

    return image_coverage, covered_paths, recovery_parts_by_image_path


def _protected_bytes(
    file_records: Sequence[_CollectionFileLike],
    *,
    image_coverage: Sequence[CollectionCoverageImage],
    covered_paths: dict[str, set[str]],
) -> int:
    if not image_coverage or not covered_paths:
        return 0
    image_states = {str(image.id): image.protection_state for image in image_coverage}
    protected = 0
    for record in file_records:
        image_ids = covered_paths.get(record.path, set())
        if image_ids and all(image_states.get(image_id) is not None for image_id in image_ids):
            if all(image_states[image_id].value == "protected" for image_id in image_ids):
                protected += record.bytes
    return protected


def _collection_recovery_summary(
    file_records: Sequence[_CollectionFileLike],
    *,
    archive: CollectionArchiveRecord | None,
    image_coverage: Sequence[CollectionCoverageImage],
    covered_paths: dict[str, set[str]],
    recovery_parts_by_image_path: dict[tuple[str, str], _RecoveryParts],
) -> CollectionRecoverySummary:
    if not file_records:
        return CollectionRecoverySummary(
            verified_physical=RecoveryCoverage(
                state=RecoveryCoverageState.NONE,
                bytes=0,
            ),
            glacier=RecoveryCoverage(
                state=RecoveryCoverageState.NONE,
                bytes=0,
            ),
            available=(),
        )

    image_by_id = {str(image.id): image for image in image_coverage}
    verified_physical_bytes = 0
    total_bytes = sum(record.bytes for record in file_records)
    archive_uploaded = (
        archive is not None and normalize_glacier_state(archive.state) == GlacierState.UPLOADED
    )
    glacier_bytes = total_bytes if archive_uploaded else 0

    for record in file_records:
        image_ids = covered_paths.get(record.path, set())
        physical_bytes = _path_recoverable_bytes(
            record.bytes,
            record.path,
            image_ids=image_ids,
            recovery_parts_by_image_path=recovery_parts_by_image_path,
            image_available=lambda image: image.physical_copies_registered > 0,
            image_by_id=image_by_id,
        )
        verified_physical_bytes += physical_bytes

    verified_physical_state = _recovery_coverage_state(
        covered_bytes=verified_physical_bytes,
        total_bytes=total_bytes,
    )
    glacier_state = _recovery_coverage_state(
        covered_bytes=glacier_bytes,
        total_bytes=total_bytes,
    )
    available: list[str] = []
    if verified_physical_state is RecoveryCoverageState.FULL:
        available.append("verified_physical")
    if glacier_state is RecoveryCoverageState.FULL:
        available.append("glacier")
    return CollectionRecoverySummary(
        verified_physical=RecoveryCoverage(
            state=verified_physical_state,
            bytes=verified_physical_bytes,
        ),
        glacier=RecoveryCoverage(
            state=glacier_state,
            bytes=glacier_bytes,
        ),
        available=tuple(available),
    )


def _path_is_recoverable(
    path: str,
    *,
    image_ids: set[str],
    recovery_parts_by_image_path: dict[tuple[str, str], _RecoveryParts],
    image_available: Callable[[CollectionCoverageImage], bool],
    image_by_id: dict[str, CollectionCoverageImage],
) -> bool:
    if not image_ids:
        return False

    expected_part_count: int | None = None
    present_parts: set[int] = set()
    for image_id in image_ids:
        image = image_by_id.get(image_id)
        if image is None or not image_available(image):
            continue
        recovery_parts = recovery_parts_by_image_path.get((image_id, path))
        if recovery_parts is None:
            continue
        if recovery_parts.part_count == 1 and recovery_parts.present_parts == frozenset({0}):
            return True
        if expected_part_count is None:
            expected_part_count = recovery_parts.part_count
        elif expected_part_count != recovery_parts.part_count:
            return False
        present_parts.update(recovery_parts.present_parts)
    return expected_part_count is not None and len(present_parts) == expected_part_count


def _path_recoverable_bytes(
    total_bytes: int,
    path: str,
    *,
    image_ids: set[str],
    recovery_parts_by_image_path: dict[tuple[str, str], _RecoveryParts],
    image_available: Callable[[CollectionCoverageImage], bool],
    image_by_id: dict[str, CollectionCoverageImage],
) -> int:
    if _path_is_recoverable(
        path,
        image_ids=image_ids,
        recovery_parts_by_image_path=recovery_parts_by_image_path,
        image_available=image_available,
        image_by_id=image_by_id,
    ):
        return total_bytes

    expected_part_count: int | None = None
    present_parts: set[int] = set()
    for image_id in image_ids:
        image = image_by_id.get(image_id)
        if image is None or not image_available(image):
            continue
        recovery_parts = recovery_parts_by_image_path.get((image_id, path))
        if recovery_parts is None:
            continue
        if expected_part_count is None:
            expected_part_count = recovery_parts.part_count
        elif expected_part_count != recovery_parts.part_count:
            return 0
        present_parts.update(recovery_parts.present_parts)

    if expected_part_count is None or not present_parts:
        return 0
    return min(
        total_bytes,
        max(1, (total_bytes * len(present_parts)) // expected_part_count),
    )


def _recovery_coverage_state(
    *,
    covered_bytes: int,
    total_bytes: int,
) -> RecoveryCoverageState:
    if total_bytes <= 0 or covered_bytes <= 0:
        return RecoveryCoverageState.NONE
    if covered_bytes >= total_bytes:
        return RecoveryCoverageState.FULL
    return RecoveryCoverageState.PARTIAL
