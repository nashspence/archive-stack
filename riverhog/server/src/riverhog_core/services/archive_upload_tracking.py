from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from riverhog_core.catalog_db import session_scope
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
    ArchiveMultipartUploadTracker,
)
from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt

ArchiveUploadRecordLoader = Callable[[Session, int, str], Any | None]
ArchiveUploadProgressCallback = Callable[[Session, int], None]


class SqlAlchemyArchiveMultipartUploadTracker(ArchiveMultipartUploadTracker):
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        load_record: ArchiveUploadRecordLoader,
        record_progress: ArchiveUploadProgressCallback | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._load_record = load_record
        self._record_progress = record_progress

    def load_multipart_upload(
        self,
        *,
        collection_id: int,
        object_id: str,
        object_path: str,
        part_size: int,
        content_length: int,
        sha256: str,
    ) -> ArchiveMultipartUploadState | None:
        with session_scope(self._session_factory) as session:
            record = self._load_record(session, collection_id, object_id)
            if record is None or not record.multipart_upload_id:
                return None
            if record.object_path and record.object_path != object_path:
                return None
            if int(record.multipart_part_size or 0) != part_size:
                return None
            if int(record.multipart_content_length or 0) != content_length:
                return None
            if record.sha256 != sha256:
                return None
            return ArchiveMultipartUploadState(
                object_id=object_id,
                upload_id=record.multipart_upload_id,
                object_path=object_path,
                part_size=part_size,
                content_length=content_length,
                sha256=sha256,
                total_parts=record.total_parts,
                encryption_state_json=record.encryption_state_json,
                parts=_multipart_parts_from_json(record.multipart_parts_json),
            )

    def save_multipart_upload(
        self,
        *,
        collection_id: int,
        state: ArchiveMultipartUploadState,
    ) -> None:
        with session_scope(self._session_factory) as session:
            record = self._load_record(session, collection_id, state.object_id)
            if record is None:
                raise RuntimeError("archive object upload state was not planned")
            record.object_path = state.object_path
            record.multipart_upload_id = state.upload_id
            record.multipart_part_size = state.part_size
            record.multipart_content_length = state.content_length
            record.encryption_state_json = state.encryption_state_json
            record.multipart_parts_json = _multipart_parts_to_json(())
            record.uploaded_bytes = 0
            record.uploaded_parts = 0
            record.total_parts = state.total_parts or max(
                1,
                (state.content_length + state.part_size - 1) // state.part_size,
            )
            self._progress(session, collection_id)

    def record_multipart_upload_progress(
        self,
        *,
        collection_id: int,
        state: ArchiveMultipartUploadState,
        part: ArchiveMultipartUploadedPart,
        uploaded_bytes: int,
        uploaded_parts: int,
        total_parts: int,
    ) -> None:
        with session_scope(self._session_factory) as session:
            record = self._load_record(session, collection_id, state.object_id)
            if record is None or record.multipart_upload_id != state.upload_id:
                return
            parts = _multipart_parts_from_json(record.multipart_parts_json)
            parts_by_number = {current.part_number: current for current in parts}
            parts_by_number[part.part_number] = part
            record.multipart_parts_json = _multipart_parts_to_json(
                tuple(parts_by_number[number] for number in sorted(parts_by_number))
            )
            record.uploaded_bytes = uploaded_bytes
            record.uploaded_parts = uploaded_parts
            record.total_parts = total_parts
            self._progress(session, collection_id)

    def clear_multipart_upload(
        self,
        *,
        collection_id: int,
        object_id: str,
        upload_id: str,
    ) -> None:
        with session_scope(self._session_factory) as session:
            record = self._load_record(session, collection_id, object_id)
            if record is None or record.multipart_upload_id != upload_id:
                return
            record.multipart_upload_id = None
            record.multipart_part_size = None
            record.multipart_parts_json = None
            record.encryption_state_json = None

    def load_ingestion_cache(
        self,
        *,
        collection_id: int,
        object_id: str,
    ) -> RetrievalCacheReceipt | None:
        with session_scope(self._session_factory) as session:
            record = self._load_record(session, collection_id, object_id)
            if record is None or not record.cache_object_path:
                return None
            if (
                record.cache_stored_bytes is None
                or record.cache_stored_sha256 is None
                or record.cache_cached_at is None
                or record.cache_verified_at is None
            ):
                return None
            return RetrievalCacheReceipt(
                object_path=record.cache_object_path,
                version_id=record.cache_version_id,
                stored_bytes=record.cache_stored_bytes,
                stored_sha256=record.cache_stored_sha256,
                cached_at=record.cache_cached_at,
                verified_at=record.cache_verified_at,
            )

    def save_ingestion_cache(
        self,
        *,
        collection_id: int,
        object_id: str,
        receipt: RetrievalCacheReceipt,
    ) -> None:
        with session_scope(self._session_factory) as session:
            record = self._load_record(session, collection_id, object_id)
            if record is None:
                raise RuntimeError("archive object upload state was not planned")
            record.cache_object_path = receipt.object_path
            record.cache_version_id = receipt.version_id
            record.cache_stored_bytes = receipt.stored_bytes
            record.cache_stored_sha256 = receipt.stored_sha256
            record.cache_cached_at = receipt.cached_at
            record.cache_verified_at = receipt.verified_at

    def _progress(self, session: Session, collection_id: int) -> None:
        if self._record_progress is not None:
            self._record_progress(session, collection_id)


def _multipart_parts_from_json(raw: str | None) -> tuple[ArchiveMultipartUploadedPart, ...]:
    if not raw:
        return ()
    payload = json.loads(raw)
    if not isinstance(payload, list):
        return ()
    parts: list[ArchiveMultipartUploadedPart] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        parts.append(
            ArchiveMultipartUploadedPart(
                part_number=int(item["part_number"]),
                etag=str(item["etag"]),
                size=int(item["size"]),
            )
        )
    return tuple(sorted(parts, key=lambda part: part.part_number))


def _multipart_parts_to_json(parts: tuple[ArchiveMultipartUploadedPart, ...]) -> str:
    return json.dumps(
        [
            {"part_number": part.part_number, "etag": part.etag, "size": part.size}
            for part in parts
        ],
        separators=(",", ":"),
    )
