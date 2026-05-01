from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from arc_core.archive_compliance import (
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
from arc_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
)
from arc_core.domain.enums import GlacierState, RecoveryCoverageState
from arc_core.domain.errors import BadRequest, Conflict, HashMismatch, NotFound
from arc_core.domain.models import (
    CollectionArchiveManifestStatus,
    CollectionCoverageImage,
    CollectionListPage,
    CollectionRecoverySummary,
    CollectionSummary,
    CopySummary,
    GlacierArchiveStatus,
    RecoveryCoverage,
)
from arc_core.domain.types import CollectionId, CopyId, ImageId, Sha256Hex
from arc_core.fs_paths import (
    PathNormalizationError,
    find_collection_id_conflict,
    normalize_collection_id,
    normalize_relpath,
)
from arc_core.ports.hot_store import HotStore
from arc_core.ports.upload_store import UploadStore
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.resumable_uploads import (
    UploadLifecycleState,
    create_or_resume_upload_state,
    expire_upload_state,
    sync_upload_state,
    upload_expiry_timestamp,
    upload_state_name,
)
from arc_core.sqlite_db import make_session_factory, session_scope

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class _RecoveryParts:
    part_count: int
    present_parts: frozenset[int]


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
        self._session_factory = make_session_factory(str(config.sqlite_path))

    def create_or_resume_upload(
        self,
        *,
        collection_id: str,
        files: Sequence[dict[str, object]],
        ingest_source: str | None = None,
    ) -> dict[str, object]:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)
        normalized_files = _normalize_upload_files(files)

        with session_scope(self._session_factory) as session:
            if session.get(CollectionRecord, normalized_collection_id) is not None:
                raise Conflict(f"collection already exists: {normalized_collection_id}")

            upload = session.get(CollectionUploadRecord, normalized_collection_id)
            if upload is not None:
                upload = _sync_and_expire_collection_upload(
                    session,
                    upload,
                    upload_store=self._upload_store,
                )

            if upload is None:
                _ensure_collection_upload_conflict_free(session, normalized_collection_id)
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
                _validate_existing_upload_manifest(upload, normalized_files)
                upload.ingest_source = ingest_source

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
                raise Conflict(f"collection upload file is not resumable: {normalized_path}")

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
                content_digest = _sha256_hex(self._upload_store.read_target(target_path))
                if content_digest != file_record.sha256:
                    raise HashMismatch("sha256 did not match expected file hash")
            else:
                file_record.upload_expires_at = upload_expiry_timestamp(self._upload_ttl)

            if _collection_upload_is_complete(upload.files):
                _ensure_collection_upload_archiving(upload)

            return {
                "offset": file_record.uploaded_bytes,
                "length": file_record.bytes,
                "expires_at": file_record.upload_expires_at,
            }

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


def _normalize_relpath_or_raise(raw: str) -> str:
    try:
        return normalize_relpath(raw)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc


def _normalize_upload_files(files: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    if not files:
        raise BadRequest("collection upload must include at least one file")

    normalized: list[dict[str, object]] = []
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


def _validate_existing_upload_manifest(
    upload: CollectionUploadRecord, expected_files: Sequence[dict[str, object]]
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
    return f"/.arc/uploads/collections/{collection_id}/{path}"


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
        content = upload_store.read_target(target_path)
        digest = _sha256_hex(content)
        if digest != file_record.sha256:
            raise Conflict(
                "uploaded collection file sha256 did not match "
                f"expected digest for {upload.collection_id}/{file_record.path}"
            )
        hot_store.put_collection_file(upload.collection_id, file_record.path, content)
        upload_store.delete_target(target_path)
        collection.files.append(
            CollectionFileRecord(
                collection_id=upload.collection_id,
                path=file_record.path,
                bytes=file_record.bytes,
                sha256=digest,
                hot=True,
                archived=False,
            )
        )

    session.flush()
    session.refresh(collection)
    summary = _summary_from_records(upload.collection_id, collection.files)
    session.delete(upload)
    return summary


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
    uploaded_bytes = sum(file_record.uploaded_bytes for file_record in files)
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
        "bytes_total": bytes_total,
        "uploaded_bytes": uploaded_bytes,
        "missing_bytes": max(bytes_total - uploaded_bytes, 0),
        "upload_state_expires_at": max(expiries) if expiries else None,
        "latest_failure": getattr(files[0].upload, "archive_failure", None) if files else None,
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
