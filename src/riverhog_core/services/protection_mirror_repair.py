from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from riverhog_core.catalog_db import session_scope
from riverhog_core.catalog_models import CollectionFileRecord, CollectionProtectionMirrorRecord
from riverhog_core.collection_archives import (
    CollectionArchiveExpectedFile,
    iter_verified_collection_archive_file_chunks,
)
from riverhog_core.fs_paths import normalize_relpath
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
    ArchiveMultipartUploadTracker,
)
from riverhog_core.ports.hot_store import HotStore
from riverhog_core.ports.protection_mirror import ProtectionMirrorStore

_LOG = logging.getLogger(__name__)


class _ResumableHotStore(Protocol):
    def put_collection_file_stream_resumable(
        self,
        collection_id: str,
        path: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
        sha256: str | None = None,
        multipart_tracker: ArchiveMultipartUploadTracker | None = None,
    ) -> None: ...


class _AbortableHotMultipartStore(Protocol):
    def abort_collection_file_multipart_upload(
        self,
        collection_id: str,
        path: str,
        upload_id: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ProtectionMirrorRepairResult:
    collection_id: str
    checked_files: int
    restored_files: int
    restored_bytes: int
    missing_or_mismatched_paths: tuple[str, ...]

    @property
    def repaired(self) -> bool:
        return self.restored_files > 0


def repair_collection_hot_files_from_protection_mirror(
    *,
    session_factory: sessionmaker[Session],
    hot_store: HotStore,
    protection_mirror_store: ProtectionMirrorStore,
    collection_id: str,
    trigger_paths: set[str] | None = None,
) -> ProtectionMirrorRepairResult:
    expected_files = _load_expected_collection_files(
        session_factory=session_factory,
        collection_id=collection_id,
    )
    if not expected_files:
        return ProtectionMirrorRepairResult(
            collection_id=collection_id,
            checked_files=0,
            restored_files=0,
            restored_bytes=0,
            missing_or_mismatched_paths=(),
        )
    paths_to_check = (
        {normalize_relpath(path) for path in trigger_paths}
        if trigger_paths is not None
        else {file.path for file in expected_files}
    )
    expected_by_path = {file.path: file for file in expected_files}
    missing_or_mismatched: list[str] = []
    for path in sorted(paths_to_check):
        expected = expected_by_path.get(path)
        if expected is None:
            continue
        if _hot_file_matches(
            hot_store,
            collection_id=collection_id,
            path=path,
            expected_bytes=expected.bytes,
            expected_sha256=expected.sha256,
        ):
            _clear_verified_hot_file_multipart_state(
                session_factory=session_factory,
                hot_store=hot_store,
                collection_id=collection_id,
                path=path,
            )
            continue
        missing_or_mismatched.append(path)
    if not missing_or_mismatched:
        return ProtectionMirrorRepairResult(
            collection_id=collection_id,
            checked_files=len(paths_to_check),
            restored_files=0,
            restored_bytes=0,
            missing_or_mismatched_paths=(),
        )

    _verify_mirror_receipt(
        session_factory=session_factory,
        protection_mirror_store=protection_mirror_store,
        collection_id=collection_id,
    )
    _LOG.info(
        "restoring collection hot files from protection mirror: collection=%s "
        "files=%s trigger_missing=%s",
        collection_id,
        len(expected_files),
        len(missing_or_mismatched),
    )
    restored_files = 0
    restored_bytes = 0
    paths_to_restore = set(missing_or_mismatched)
    for path, chunks, content_length in iter_verified_collection_archive_file_chunks(
        protection_mirror_store.iter_collection_archive(collection_id),
        files=expected_files,
        selected_paths=paths_to_restore,
    ):
        expected = expected_by_path[path]
        _put_hot_file_stream_resumable(
            session_factory=session_factory,
            hot_store=hot_store,
            collection_id=collection_id,
            path=path,
            chunks=chunks,
            content_length=content_length,
            sha256=expected.sha256,
        )
        if not _hot_file_matches(
            hot_store,
            collection_id=collection_id,
            path=path,
            expected_bytes=expected.bytes,
            expected_sha256=expected.sha256,
        ):
            hot_store.delete_collection_file(collection_id, path)
            raise ValueError(f"protection mirror restore sha256 mismatch: {collection_id}/{path}")
        _mark_collection_file_hot(
            session_factory=session_factory,
            collection_id=collection_id,
            path=path,
        )
        restored_files += 1
        restored_bytes += content_length
        if restored_files == len(expected_files) or restored_files % 100 == 0:
            _LOG.info(
                "protection mirror restore progress: collection=%s files=%s/%s bytes=%s",
                collection_id,
                restored_files,
                len(expected_files),
                restored_bytes,
            )

    return ProtectionMirrorRepairResult(
        collection_id=collection_id,
        checked_files=len(paths_to_check),
        restored_files=restored_files,
        restored_bytes=restored_bytes,
        missing_or_mismatched_paths=tuple(missing_or_mismatched),
    )


def _load_expected_collection_files(
    *,
    session_factory: sessionmaker[Session],
    collection_id: str,
) -> list[CollectionArchiveExpectedFile]:
    with session_scope(session_factory) as session:
        rows = session.scalars(
            select(CollectionFileRecord)
            .where(CollectionFileRecord.collection_id == collection_id)
            .order_by(CollectionFileRecord.path.asc())
        ).all()
        return [
            CollectionArchiveExpectedFile(
                path=row.path,
                bytes=row.bytes,
                sha256=row.sha256,
            )
            for row in rows
        ]


def _verify_mirror_receipt(
    *,
    session_factory: sessionmaker[Session],
    protection_mirror_store: ProtectionMirrorStore,
    collection_id: str,
) -> None:
    with session_scope(session_factory) as session:
        mirror = session.get(CollectionProtectionMirrorRecord, collection_id)
        if mirror is None or mirror.state not in {"complete", "repairing", "repair_wait"}:
            raise RuntimeError(f"protection mirror is not complete: {collection_id}")
        expected_bytes = mirror.archive_bytes
        expected_sha256 = mirror.archive_sha256
    stat = protection_mirror_store.stat_collection_archive(collection_id)
    if stat is None:
        raise RuntimeError(f"protection mirror archive is missing: {collection_id}")
    if expected_bytes is not None and stat.bytes != expected_bytes:
        raise RuntimeError(f"protection mirror archive byte count mismatch: {collection_id}")
    if expected_sha256 is not None and stat.sha256 is not None and stat.sha256 != expected_sha256:
        raise RuntimeError(f"protection mirror archive sha256 mismatch: {collection_id}")


def _put_hot_file_stream_resumable(
    *,
    session_factory: sessionmaker[Session],
    hot_store: HotStore,
    collection_id: str,
    path: str,
    chunks: Iterable[bytes],
    content_length: int,
    sha256: str,
) -> None:
    resumable = getattr(hot_store, "put_collection_file_stream_resumable", None)
    if callable(resumable):
        cast(_ResumableHotStore, hot_store).put_collection_file_stream_resumable(
            collection_id,
            path,
            chunks,
            content_length=content_length,
            sha256=sha256,
            multipart_tracker=_CollectionFileHotMultipartUploadTracker(
                session_factory,
                path=path,
            ),
        )
        return
    hot_store.put_collection_file_stream(
        collection_id,
        path,
        chunks,
        content_length=content_length,
        sha256=sha256,
    )


def _mark_collection_file_hot(
    *,
    session_factory: sessionmaker[Session],
    collection_id: str,
    path: str,
) -> None:
    with session_scope(session_factory) as session:
        record = session.get(CollectionFileRecord, (collection_id, path))
        if record is None:
            return
        record.hot = True


def _clear_verified_hot_file_multipart_state(
    *,
    session_factory: sessionmaker[Session],
    hot_store: HotStore,
    collection_id: str,
    path: str,
) -> None:
    with session_scope(session_factory) as session:
        record = session.get(CollectionFileRecord, (collection_id, path))
        upload_id = record.hot_multipart_upload_id if record is not None else None
    if not upload_id:
        return
    abort_upload = getattr(hot_store, "abort_collection_file_multipart_upload", None)
    if callable(abort_upload):
        cast(_AbortableHotMultipartStore, hot_store).abort_collection_file_multipart_upload(
            collection_id,
            path,
            upload_id,
        )
    _CollectionFileHotMultipartUploadTracker(session_factory, path=path).clear_multipart_upload(
        collection_id=collection_id,
        upload_id=upload_id,
    )
    _LOG.info(
        "cleared stale hot-store multipart restore state for %s/%s: upload_id=%s",
        collection_id,
        path,
        upload_id,
    )


def _hot_file_matches(
    hot_store: HotStore,
    *,
    collection_id: str,
    path: str,
    expected_bytes: int,
    expected_sha256: str,
) -> bool:
    stat = hot_store.stat_collection_file(collection_id, path)
    if stat is None:
        return False
    if stat.bytes != expected_bytes:
        return False
    if stat.sha256 is not None:
        return stat.sha256 == expected_sha256

    digest = hashlib.sha256()
    byte_count = 0
    for chunk in hot_store.iter_collection_file(collection_id, path):
        digest.update(chunk)
        byte_count += len(chunk)
    return byte_count == expected_bytes and digest.hexdigest() == expected_sha256


class _CollectionFileHotMultipartUploadTracker(ArchiveMultipartUploadTracker):
    def __init__(self, session_factory: sessionmaker[Session], *, path: str) -> None:
        self._session_factory = session_factory
        self._path = path

    def load_multipart_upload(
        self,
        *,
        collection_id: str,
        object_path: str,
        part_size: int,
        content_length: int,
        sha256: str,
    ) -> ArchiveMultipartUploadState | None:
        with session_scope(self._session_factory) as session:
            file_record = session.get(CollectionFileRecord, (collection_id, self._path))
            if file_record is None:
                return None
            if not file_record.hot_multipart_upload_id:
                return None
            if int(file_record.hot_multipart_part_size or 0) != part_size:
                return None
            if int(file_record.bytes) != content_length:
                return None
            if file_record.sha256 != sha256:
                return None
            return ArchiveMultipartUploadState(
                upload_id=file_record.hot_multipart_upload_id,
                object_path=object_path,
                part_size=part_size,
                content_length=content_length,
                sha256=sha256,
                parts=_multipart_parts_from_json(file_record.hot_multipart_parts_json),
            )

    def save_multipart_upload(
        self,
        *,
        collection_id: str,
        state: ArchiveMultipartUploadState,
    ) -> None:
        with session_scope(self._session_factory) as session:
            file_record = session.get(CollectionFileRecord, (collection_id, self._path))
            if file_record is None:
                return
            file_record.hot_multipart_upload_id = state.upload_id
            file_record.hot_multipart_part_size = state.part_size
            file_record.hot_multipart_parts_json = _multipart_parts_to_json(())
            file_record.hot_multipart_uploaded_bytes = 0
            file_record.hot_multipart_uploaded_parts = 0
            file_record.hot_multipart_total_parts = max(
                1,
                (state.content_length + state.part_size - 1) // state.part_size,
            )

    def record_multipart_upload_progress(
        self,
        *,
        collection_id: str,
        state: ArchiveMultipartUploadState,
        part: ArchiveMultipartUploadedPart,
        uploaded_bytes: int,
        uploaded_parts: int,
        total_parts: int,
    ) -> None:
        with session_scope(self._session_factory) as session:
            file_record = session.get(CollectionFileRecord, (collection_id, self._path))
            if file_record is None:
                return
            if file_record.hot_multipart_upload_id != state.upload_id:
                return
            parts = _multipart_parts_from_json(file_record.hot_multipart_parts_json)
            parts_by_number = {current.part_number: current for current in parts}
            parts_by_number[part.part_number] = part
            file_record.hot_multipart_parts_json = _multipart_parts_to_json(
                tuple(parts_by_number[number] for number in sorted(parts_by_number))
            )
            file_record.hot_multipart_uploaded_bytes = uploaded_bytes
            file_record.hot_multipart_uploaded_parts = uploaded_parts
            file_record.hot_multipart_total_parts = total_parts

    def clear_multipart_upload(
        self,
        *,
        collection_id: str,
        upload_id: str,
    ) -> None:
        with session_scope(self._session_factory) as session:
            file_record = session.get(CollectionFileRecord, (collection_id, self._path))
            if file_record is None:
                return
            if file_record.hot_multipart_upload_id != upload_id:
                return
            file_record.hot_multipart_upload_id = None
            file_record.hot_multipart_part_size = None
            file_record.hot_multipart_parts_json = None
            file_record.hot_multipart_uploaded_bytes = None
            file_record.hot_multipart_uploaded_parts = None
            file_record.hot_multipart_total_parts = None


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
        [{"part_number": part.part_number, "etag": part.etag, "size": part.size} for part in parts],
        separators=(",", ":"),
    )
