from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence

from sqlalchemy import select

from arc_core.catalog_models import (
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
)
from arc_core.domain.errors import BadRequest, Conflict, HashMismatch, NotFound
from arc_core.domain.models import CollectionSummary
from arc_core.domain.types import CollectionId, Sha256Hex
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
    upload_state_name,
)
from arc_core.sqlite_db import make_session_factory, session_scope

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class StubCollectionService:
    def create_or_resume_upload(
        self,
        *,
        collection_id: str,
        files: Sequence[dict[str, object]],
        ingest_source: str | None = None,
    ) -> object:
        raise NotImplementedError("StubCollectionService is not implemented yet")

    def get_upload(self, collection_id: str) -> object:
        raise NotImplementedError("StubCollectionService is not implemented yet")

    def create_or_resume_file_upload(self, collection_id: str, path: str) -> object:
        raise NotImplementedError("StubCollectionService is not implemented yet")

    def append_upload_chunk(
        self,
        collection_id: str,
        path: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> object:
        raise NotImplementedError("StubCollectionService is not implemented yet")

    def get(self, collection_id: str) -> object:
        raise NotImplementedError("StubCollectionService is not implemented yet")


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
                summary = _finalize_collection_upload(
                    session,
                    upload,
                    hot_store=self._hot_store,
                )
                return _collection_upload_payload(
                    collection_id=normalized_collection_id,
                    ingest_source=upload.ingest_source,
                    files=upload.files,
                    state="finalized",
                    collection=summary,
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
                summary = _finalize_collection_upload(
                    session,
                    upload,
                    hot_store=self._hot_store,
                )
                return _collection_upload_payload(
                    collection_id=normalized_collection_id,
                    ingest_source=upload.ingest_source,
                    files=upload.files,
                    state="finalized",
                    collection=summary,
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

            return {
                "path": file_record.path,
                "protocol": "tus",
                "upload_url": tus_url,
                "offset": file_record.uploaded_bytes,
                "length": file_record.bytes,
                "checksum_algorithm": "sha256",
                "expires_at": file_record.upload_expires_at,
            }

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

            next_offset, expires_at = self._upload_store.append_upload_chunk(
                file_record.tus_url,
                offset=offset,
                checksum=checksum,
                content=content,
            )
            file_record.uploaded_bytes = next_offset
            file_record.upload_expires_at = expires_at

            if next_offset >= file_record.bytes:
                target_path = _collection_upload_target_path(normalized_collection_id, normalized_path)
                content_digest = _sha256_hex(self._upload_store.read_target(target_path))
                if content_digest != file_record.sha256:
                    raise HashMismatch("sha256 did not match expected file hash")

            if _collection_upload_is_complete(upload.files):
                _finalize_collection_upload(session, upload, hot_store=self._hot_store)

            return {"offset": file_record.uploaded_bytes, "expires_at": file_record.upload_expires_at}

    def get(self, collection_id: str) -> CollectionSummary:
        normalized_collection_id = _normalize_collection_id_or_raise(collection_id)

        with session_scope(self._session_factory) as session:
            collection = session.get(CollectionRecord, normalized_collection_id)
            if collection is None:
                raise NotFound(f"collection not found: {normalized_collection_id}")
            return _summary_from_records(normalized_collection_id, collection.files)


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


def _ensure_collection_upload_conflict_free(session, collection_id: str) -> None:
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
    return f"/collections/{collection_id}/{path}"


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
    session,
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


def _finalize_collection_upload(
    session,
    upload: CollectionUploadRecord,
    *,
    hot_store: HotStore,
) -> CollectionSummary:
    collection = CollectionRecord(id=upload.collection_id, ingest_source=upload.ingest_source)
    session.add(collection)

    for file_record in sorted(upload.files, key=lambda current: current.file_order):
        content = hot_store.get_collection_file(upload.collection_id, file_record.path)
        digest = _sha256_hex(content)
        if digest != file_record.sha256:
            raise Conflict(
                "uploaded collection file sha256 did not match "
                f"expected digest for {upload.collection_id}/{file_record.path}"
            )
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
    }


def _sha256_hex(content: bytes) -> Sha256Hex:
    return Sha256Hex(hashlib.sha256(content).hexdigest())


def _summary_from_records(
    collection_id: str,
    file_records: Sequence[CollectionFileRecord],
) -> CollectionSummary:
    return CollectionSummary(
        id=CollectionId(collection_id),
        files=len(file_records),
        bytes=sum(record.bytes for record in file_records),
        hot_bytes=sum(record.bytes for record in file_records if record.hot),
        archived_bytes=sum(record.bytes for record in file_records if record.archived),
    )
