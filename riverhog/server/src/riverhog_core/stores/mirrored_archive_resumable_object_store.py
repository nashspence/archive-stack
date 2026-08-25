from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from riverhog_core.ports.archive_objects import (
    ArchiveResumableObjectStore,
    CompletedObjectReceipt,
    ResumableWriteConstraints,
    WriteSegmentReceipt,
    WriteSession,
)
from riverhog_core.ports.retrieval_cache import RetrievalCache

_WRITE_SCHEMA = "archive-cache-mirror-write/v1"


class MirroredArchiveResumableObjectStore:
    """Write identical encrypted segments to archive authority and retrieval cache."""

    def __init__(
        self,
        *,
        archive: ArchiveResumableObjectStore,
        cache: RetrievalCache,
        source_store: str,
        collection_id: int,
        object_id: str,
    ) -> None:
        self._archive = archive
        self._cache = cache
        self._cache_objects = cache.resumable_object_store(
            source_store=source_store,
            collection_id=collection_id,
            object_id=object_id,
        )

    def write_constraints(self) -> ResumableWriteConstraints:
        archive = self._archive.write_constraints()
        cache = self._cache_objects.write_constraints()
        maxima = tuple(
            value
            for value in (archive.maximum_segment_bytes, cache.maximum_segment_bytes)
            if value is not None
        )
        counts = tuple(
            value
            for value in (archive.maximum_segment_count, cache.maximum_segment_count)
            if value is not None
        )
        return ResumableWriteConstraints(
            minimum_nonfinal_segment_bytes=max(
                archive.minimum_nonfinal_segment_bytes,
                cache.minimum_nonfinal_segment_bytes,
            ),
            maximum_segment_bytes=min(maxima) if maxima else None,
            maximum_segment_count=min(counts) if counts else None,
        )

    def begin_write(
        self,
        *,
        object_path: str,
        content_type: str,
        metadata: dict[str, str],
    ) -> WriteSession:
        archive_session = self._archive.begin_write(
            object_path=object_path,
            content_type=content_type,
            metadata=metadata,
        )
        try:
            cache_completed = self._cache_objects.find_completed_write(
                object_path=object_path,
                expected_metadata=metadata,
            )
            cache_session = (
                None
                if cache_completed is not None
                else self._cache_objects.begin_write(
                    object_path=object_path,
                    content_type=content_type,
                    metadata=metadata,
                )
            )
        except BaseException:
            self._archive.abort_write(session=archive_session)
            raise
        return WriteSession(
            object_path=object_path,
            write_token=_encode_write_token(
                archive_session=archive_session,
                cache_session=cache_session,
                metadata=metadata,
            ),
        )

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        content: bytes,
    ) -> WriteSegmentReceipt:
        archive_session, cache_session, metadata = _decode_write_token(session)
        cache_completed = self._cache_objects.find_completed_write(
            object_path=session.object_path,
            expected_metadata=metadata,
        )
        if cache_completed is not None:
            return self._archive.write_segment(
                session=archive_session,
                number=number,
                content=content,
            )
        if cache_session is None:
            raise RuntimeError("retrieval cache mirror has no write or completed object")
        with ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="riverhog-archive-cache-mirror",
        ) as executor:
            archive_future = executor.submit(
                self._archive.write_segment,
                session=archive_session,
                number=number,
                content=content,
            )
            cache_future = executor.submit(
                self._cache_objects.write_segment,
                session=cache_session,
                number=number,
                content=content,
            )
            archive_receipt = archive_future.result()
            cache_receipt = cache_future.result()
        if (
            archive_receipt.number != cache_receipt.number
            or archive_receipt.bytes != cache_receipt.bytes
        ):
            raise RuntimeError("archive and retrieval cache segment receipts disagree")
        return archive_receipt

    def list_segments(self, *, session: WriteSession) -> tuple[WriteSegmentReceipt, ...]:
        archive_session, cache_session, metadata = _decode_write_token(session)
        archive_segments = self._archive.list_segments(session=archive_session)
        cache_completed = self._cache_objects.find_completed_write(
            object_path=session.object_path,
            expected_metadata=metadata,
        )
        if cache_completed is not None:
            return archive_segments
        if cache_session is None:
            raise RuntimeError("retrieval cache mirror has no write or completed object")
        cache_segments = {
            current.number: current
            for current in self._cache_objects.list_segments(session=cache_session)
        }
        return tuple(
            current
            for current in archive_segments
            if (mirrored := cache_segments.get(current.number)) is not None
            and mirrored.bytes == current.bytes
        )

    def complete_write(
        self,
        *,
        session: WriteSession,
        segments: tuple[WriteSegmentReceipt, ...],
        expected_bytes: int,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt:
        archive_session, cache_session, _metadata = _decode_write_token(session)
        cache_completed = self._cache_objects.find_completed_write(
            object_path=session.object_path,
            expected_metadata=expected_metadata,
        )
        if cache_completed is None:
            if cache_session is None:
                raise RuntimeError("retrieval cache mirror has no resumable write")
            cache_segments = self._cache_objects.list_segments(session=cache_session)
            _require_matching_segments(segments, cache_segments)
            cache_completed = self._cache_objects.complete_write(
                session=cache_session,
                segments=cache_segments,
                expected_bytes=expected_bytes,
                expected_metadata=expected_metadata,
            )
        if cache_completed.bytes != expected_bytes:
            raise RuntimeError("retrieval cache mirror byte count differs from archive upload")
        cache_receipt = self._cache.verify_resumable_object(
            completed=cache_completed,
            segments=segments,
        )
        archive_completed = self._archive.complete_write(
            session=archive_session,
            segments=segments,
            expected_bytes=expected_bytes,
            expected_metadata=expected_metadata,
        )
        return replace(archive_completed, retrieval_cache=cache_receipt)

    def find_completed_write(
        self,
        *,
        object_path: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None:
        archive_completed = self._archive.find_completed_write(
            object_path=object_path,
            expected_metadata=expected_metadata,
        )
        if archive_completed is None:
            return None
        cache_completed = self._cache_objects.find_completed_write(
            object_path=object_path,
            expected_metadata=expected_metadata,
        )
        if cache_completed is None or cache_completed.bytes != archive_completed.bytes:
            raise RuntimeError("completed archive object is missing its required retrieval cache")
        return replace(
            archive_completed,
            retrieval_cache=self._cache.verify_resumable_object(completed=cache_completed),
        )

    def abort_write(self, *, session: WriteSession) -> None:
        archive_session, cache_session, metadata = _decode_write_token(session)
        archive_completed = self._archive.find_completed_write(
            object_path=session.object_path,
            expected_metadata=metadata,
        )
        cache_completed = self._cache_objects.find_completed_write(
            object_path=session.object_path,
            expected_metadata=metadata,
        )
        archive_error: BaseException | None = None
        try:
            self._archive.abort_write(session=archive_session)
        except BaseException as exc:
            archive_error = exc
        if cache_session is not None and cache_completed is None:
            self._cache_objects.abort_write(session=cache_session)
        if cache_completed is not None and archive_completed is None:
            self._cache.delete(
                object_path=cache_completed.object_path,
                revision=cache_completed.revision,
            )
        if archive_error is not None:
            raise archive_error


def _encode_write_token(
    *,
    archive_session: WriteSession,
    cache_session: WriteSession | None,
    metadata: dict[str, str],
) -> str:
    return json.dumps(
        {
            "schema": _WRITE_SCHEMA,
            "metadata": dict(sorted(metadata.items())),
            "archive": {
                "object_path": archive_session.object_path,
                "write_token": archive_session.write_token,
            },
            "cache": (
                {
                    "object_path": cache_session.object_path,
                    "write_token": cache_session.write_token,
                }
                if cache_session is not None
                else None
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_write_token(
    session: WriteSession,
) -> tuple[WriteSession, WriteSession | None, dict[str, str]]:
    try:
        payload = json.loads(session.write_token)
    except json.JSONDecodeError as exc:
        raise ValueError("archive-cache mirror write token is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema") != _WRITE_SCHEMA:
        raise ValueError("archive-cache mirror write-token schema mismatch")
    archive = _session_from_payload(payload.get("archive"), label="archive")
    cache_payload = payload.get("cache")
    cache = None if cache_payload is None else _session_from_payload(cache_payload, label="cache")
    raw_metadata = payload.get("metadata")
    if not isinstance(raw_metadata, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in raw_metadata.items()
    ):
        raise ValueError("archive-cache mirror identity metadata is invalid")
    if archive.object_path != session.object_path:
        raise ValueError("archive-cache mirror object path changed")
    return archive, cache, dict(raw_metadata)


def _session_from_payload(value: object, *, label: str) -> WriteSession:
    if not isinstance(value, dict):
        raise ValueError(f"archive-cache mirror {label} write session is invalid")
    object_path = str(value.get("object_path", ""))
    write_token = str(value.get("write_token", ""))
    if not object_path or not write_token:
        raise ValueError(f"archive-cache mirror {label} write identity is invalid")
    return WriteSession(object_path, write_token)


def _require_matching_segments(
    archive: tuple[WriteSegmentReceipt, ...],
    cache: tuple[WriteSegmentReceipt, ...],
) -> None:
    archive_shape = tuple((current.number, current.bytes) for current in archive)
    cache_shape = tuple((current.number, current.bytes) for current in cache)
    if archive_shape != cache_shape:
        raise RuntimeError("retrieval cache write segments do not match the archive write")
