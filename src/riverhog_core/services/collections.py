from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from riverhog_core.archive_compliance import (
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
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
)
from riverhog_core.domain.enums import GlacierState, RecoveryCoverageState
from riverhog_core.domain.errors import BadRequest, Conflict, HashMismatch, NotFound
from riverhog_core.domain.models import (
    CollectionArchiveManifestStatus,
    CollectionCoverageImage,
    CollectionListPage,
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
from riverhog_core.services.resumable_uploads import (
    UploadLifecycleState,
    create_or_resume_upload_state,
    expire_upload_state,
    sync_upload_state,
    upload_expiry_timestamp,
    upload_state_name,
)
from riverhog_core.webhooks import (
    WebhookConfig,
    build_collection_lifecycle_payload,
    post_webhook,
    utcnow,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _RecoveryParts:
    part_count: int
    present_parts: frozenset[int]


class _UploadManifestEntry(TypedDict):
    path: str
    bytes: int
    sha256: str


class _StoredManifestEntry(Protocol):
    path: str
    bytes: int
    sha256: str


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
        self._session_factory = make_session_factory(config.database_url)

    def create_or_resume_upload(
        self,
        *,
        upload_slug: str,
        files: Sequence[dict[str, object]],
        ingest_source: str | None = None,
        upload_timestamp: str | None = None,
    ) -> dict[str, object]:
        normalized_slug = _normalize_upload_slug_or_raise(upload_slug)
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
                normalized_collection_id = upload.collection_id

            if _collection_upload_is_complete(upload.files):
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
                )

            return _collection_upload_payload(
                collection_id=normalized_collection_id,
                ingest_source=upload.ingest_source,
                files=upload.files,
                state="uploading",
                collection=None,
            )

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
            )
            if upload is None:
                raise NotFound(f"collection upload not found: {normalized_collection_id}")

            if _collection_upload_is_complete(upload.files):
                _ensure_collection_upload_archiving(upload)
                return _collection_upload_payload(
                    collection_id=normalized_collection_id,
                    ingest_source=upload.ingest_source,
                    files=upload.files,
                    state=_collection_upload_session_state(upload),
                    collection=None,
                )

            return _collection_upload_payload(
                collection_id=normalized_collection_id,
                ingest_source=upload.ingest_source,
                files=upload.files,
                state="uploading",
                collection=None,
            )

    def create_or_resume_file_upload(self, collection_id: str, path: str) -> dict[str, object]:
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
            )
            if upload is None:
                raise NotFound(f"collection upload not found: {normalized_collection_id}")

            file_record = _get_upload_file(upload.files, normalized_path)
            target_path = _collection_upload_target_path(normalized_collection_id, normalized_path)
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

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is None:
                raise NotFound(f"collection upload not found: {normalized_collection_id}")

            file_record = _get_upload_file(upload.files, normalized_path)
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
                target_path = _collection_upload_target_path(
                    normalized_collection_id,
                    normalized_path,
                )
                content_digest = _sha256_hex_chunks(self._upload_store.iter_target(target_path))
                if content_digest != file_record.sha256:
                    raise HashMismatch("sha256 did not match expected file hash")
            else:
                file_record.upload_expires_at = upload_expiry_timestamp(self._upload_ttl)

            if _collection_upload_is_complete(upload.files):
                was_archiving = upload.state == "archiving"
                _ensure_collection_upload_archiving(upload)
                if not was_archiving:
                    staged_webhook_details = _collection_upload_webhook_details(upload)

            response: dict[str, object] = {
                "offset": file_record.uploaded_bytes,
                "length": file_record.bytes,
                "expires_at": file_record.upload_expires_at,
            }
        if staged_webhook_details is not None:
            _post_collection_lifecycle_webhook(
                self._config,
                event="collections.upload_staged",
                collection_id=normalized_collection_id,
                details=staged_webhook_details,
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
                )

    def get(self, collection_id: str) -> CollectionSummary:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)

        with session_scope(self._session_factory) as session:
            collection = session.get(CollectionRecord, normalized_collection_id)
            if collection is None:
                raise NotFound(f"collection not found: {normalized_collection_id}")
            (
                image_coverage,
                covered_paths,
                recovery_parts_by_image_path,
            ) = _collection_image_coverage(session, normalized_collection_id)
            return _summary_from_records(
                normalized_collection_id,
                collection.files,
                archive=collection.archive,
                image_coverage=image_coverage,
                covered_paths=covered_paths,
                recovery_parts_by_image_path=recovery_parts_by_image_path,
            )

    def list(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        protection_state: str | None,
    ) -> CollectionListPage:
        if page < 1:
            raise BadRequest("page must be at least 1")
        if per_page < 1:
            raise BadRequest("per_page must be at least 1")
        if protection_state is not None and protection_state not in {
            "unprotected",
            "partially_protected",
            "protected",
        }:
            raise BadRequest(f"unsupported protection_state filter: {protection_state}")

        needle = q.casefold() if q else None
        with session_scope(self._session_factory) as session:
            collections = session.scalars(
                select(CollectionRecord)
                .options(selectinload(CollectionRecord.files))
                .options(selectinload(CollectionRecord.archive))
                .order_by(CollectionRecord.id.asc())
            ).all()

            summaries: list[CollectionSummary] = []
            for collection in collections:
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
                if needle is not None and needle not in str(summary.id).casefold():
                    continue
                if (
                    protection_state is not None
                    and summary.protection_state.value != protection_state
                ):
                    continue
                summaries.append(summary)

            total = len(summaries)
            pages = (total + per_page - 1) // per_page if total else 0
            start = (page - 1) * per_page
            stop = start + per_page
            return CollectionListPage(
                page=page,
                per_page=per_page,
                total=total,
                pages=pages,
                collections=summaries[start:stop],
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


def _unburned_collection_bytes(session: Session) -> int:
    upload_bytes = 0
    uploads = session.scalars(
        select(CollectionUploadRecord).options(selectinload(CollectionUploadRecord.files))
    ).all()
    for upload in uploads:
        if upload.state == "finalized":
            continue
        upload_bytes += sum(file_record.bytes for file_record in upload.files)

    committed_unprotected_bytes = 0
    collections = session.scalars(
        select(CollectionRecord)
        .options(selectinload(CollectionRecord.files))
        .options(selectinload(CollectionRecord.archive))
        .order_by(CollectionRecord.id.asc())
    ).all()
    for collection in collections:
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
        committed_unprotected_bytes += max(summary.bytes - summary.protected_bytes, 0)

    return upload_bytes + committed_unprotected_bytes


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
) -> None:
    for file_record in file_records:
        updated = sync_upload_state(
            current=_upload_lifecycle_state(file_record),
            target_path=_collection_upload_target_path(file_record.collection_id, file_record.path),
            length=file_record.bytes,
            upload_store=upload_store,
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
) -> CollectionUploadRecord | None:
    _sync_collection_upload_files(upload.files, upload_store)
    expired_any = _expire_collection_upload_files(upload.files, upload_store)
    if expired_any and _collection_upload_has_no_live_file_state(upload.files):
        _forget_collection_upload(upload, upload_store)
        session.delete(upload)
        return None
    return upload


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


def _collection_upload_session_state(upload: CollectionUploadRecord) -> str:
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


def _post_collection_lifecycle_webhook(
    config: RuntimeConfig,
    *,
    event: str,
    collection_id: str,
    details: dict[str, object] | None = None,
) -> None:
    if not config.collection_lifecycle_webhook_url:
        return
    try:
        webhook_config = WebhookConfig(
            url=config.collection_lifecycle_webhook_url,
            base_url=config.public_base_url or "",
            timeout_seconds=config.collection_lifecycle_webhook_timeout.total_seconds(),
        )
        payload = build_collection_lifecycle_payload(
            config=webhook_config,
            event=event,
            collection_id=collection_id,
            delivered_at=utcnow(),
            details=details,
        )
        post_webhook(config=webhook_config, payload=payload)
    except Exception:
        _LOG.warning("failed to deliver %s webhook for %s", event, collection_id, exc_info=True)


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
    collection = CollectionRecord(id=upload.collection_id, ingest_source=upload.ingest_source)
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
) -> dict[str, object]:
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
        "latest_failure": getattr(files[0].upload, "archive_failure", None) if files else None,
        "archive_phase": getattr(files[0].upload, "archive_phase", None) if files else None,
        "archive_phase_updated_at": getattr(files[0].upload, "archive_phase_updated_at", None)
        if files
        else None,
        "archive_object_path": getattr(files[0].upload, "archive_object_path", None)
        if files
        else None,
        "archive_uploaded_bytes": getattr(files[0].upload, "archive_multipart_uploaded_bytes", None)
        if files
        else None,
        "archive_total_bytes": getattr(files[0].upload, "archive_multipart_content_length", None)
        if files
        else None,
        "archive_uploaded_parts": getattr(files[0].upload, "archive_multipart_uploaded_parts", None)
        if files
        else None,
        "archive_total_parts": getattr(files[0].upload, "archive_multipart_total_parts", None)
        if files
        else None,
        "files": [
            {
                "path": file_record.path,
                "bytes": file_record.bytes,
                "sha256": file_record.sha256,
                "upload_state": upload_state_name(
                    uploaded_bytes=file_record.uploaded_bytes, length=file_record.bytes
                ),
                "uploaded_bytes": file_record.uploaded_bytes,
                "upload_state_expires_at": file_record.upload_expires_at,
            }
            for file_record in sorted(files, key=lambda current: current.file_order)
        ],
        "collection": _collection_summary_payload(collection) if collection is not None else None,
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
        "archive_manifest": _archive_manifest_payload(summary.archive_manifest),
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


def _archive_manifest_payload(
    summary: CollectionArchiveManifestStatus | None,
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


def _summary_from_records(
    collection_id: str,
    file_records: Sequence[CollectionFileRecord],
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
    return CollectionSummary(
        id=CollectionId(collection_id),
        files=len(file_records),
        bytes=bytes_total,
        hot_bytes=sum(record.bytes for record in file_records if record.hot),
        archived_bytes=archived_bytes,
        protection_state=collection_protection_state(
            bytes_total=bytes_total,
            protected_bytes=protected_bytes,
            archived_bytes=archived_bytes,
            image_states=(image.protection_state for image in image_coverage),
        ),
        protected_bytes=protected_bytes,
        recovery=recovery,
        image_coverage=list(image_coverage),
        glacier=_collection_glacier_status(archive),
        archive_manifest=_collection_archive_manifest_status(archive),
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


def _collection_archive_manifest_status(
    archive: CollectionArchiveRecord | None,
) -> CollectionArchiveManifestStatus | None:
    if archive is None:
        return None
    ots_state = "uploaded" if archive.ots_object_path else "pending"
    if archive.state == "failed":
        ots_state = "failed"
    return CollectionArchiveManifestStatus(
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
    images = (
        session.scalars(
            select(FinalizedImageRecord)
            .join(FinalizedImageCoveredPathRecord)
            .where(FinalizedImageCoveredPathRecord.collection_id == collection_id)
            .options(
                selectinload(FinalizedImageRecord.coverage_parts),
                selectinload(FinalizedImageRecord.covered_paths),
                selectinload(FinalizedImageRecord.copies),
            )
        )
        .unique()
        .all()
    )

    covered_paths: dict[str, set[str]] = {}
    recovery_parts_by_image_path: dict[tuple[str, str], _RecoveryParts] = {}
    image_coverage: list[CollectionCoverageImage] = []
    for image in sorted(images, key=lambda current: current.image_id):
        image_paths: set[str] = set()
        for covered_path in image.covered_paths:
            if covered_path.collection_id != collection_id:
                continue
            covered_paths.setdefault(covered_path.path, set()).add(image.image_id)
            image_paths.add(covered_path.path)
        for part in image.coverage_parts:
            if part.collection_id != collection_id:
                continue
            key = (image.image_id, part.path)
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

        copies = [
            CopySummary(
                id=CopyId(copy.copy_id),
                volume_id=image.image_id,
                label_text=copy.label_text or copy.copy_id,
                location=copy.location,
                created_at=copy.created_at,
                state=normalize_copy_state(copy.state),
                verification_state=normalize_verification_state(copy.verification_state),
            )
            for copy in sorted(image.copies, key=lambda current: current.copy_id)
        ]
        required_copy_count = normalize_required_copy_count(image.required_copy_count)
        registered_copy_count = sum(
            1 for copy in image.copies if copy_counts_toward_protection(copy.state)
        )
        verified_copy_count = sum(
            1
            for copy in image.copies
            if copy_counts_as_verified(
                state=copy.state,
                verification_state=copy.verification_state,
            )
        )
        image_coverage.append(
            CollectionCoverageImage(
                id=ImageId(image.image_id),
                filename=image.filename,
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
                covered_paths=sorted(image_paths),
                copies=copies,
            )
        )

    return image_coverage, covered_paths, recovery_parts_by_image_path


def _protected_bytes(
    file_records: Sequence[CollectionFileRecord],
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
    file_records: Sequence[CollectionFileRecord],
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
