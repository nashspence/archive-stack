from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from riverhog_core.domain.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.ports.archive_objects import (
    ArchiveResumableObjectStore,
    CompletedObjectReceipt,
    ResumableWriteConstraints,
    WriteSegmentReceipt,
    WriteSession,
)
from riverhog_core.ports.retrieval_cache import RetrievalCache, RetrievalCacheAdmission
from riverhog_core.retrieval_cache_receipts import (
    parse_retrieval_cache_receipt,
    retrieval_cache_receipt_payload,
)

_WRITE_SCHEMA = "archive-cache-mirror-write/v1"
_LOG = logging.getLogger(__name__)


class MirroredArchiveResumableObjectStore:
    """Mirror encrypted archive bytes into optional, pre-admitted cache placement."""

    def __init__(
        self,
        *,
        archive: ArchiveResumableObjectStore,
        cache: RetrievalCache,
        source_store: str,
        collection_id: int,
        object_id: str,
        owner: str,
    ) -> None:
        self._archive = archive
        self._cache = cache
        self._source_store = source_store
        self._collection_id = collection_id
        self._object_id = object_id
        self._owner = owner

    def write_constraints(self) -> ResumableWriteConstraints:
        # Archive layout and segmentation remain archive-contract authority. A
        # cache candidate either accepts that exact object or declines admission.
        return self._archive.write_constraints()

    def begin_write(
        self,
        *,
        object_path: str,
        expected_bytes: int,
        content_type: str,
        metadata: dict[str, str],
    ) -> WriteSession:
        admission = self._cache.admit(
            owner=self._owner,
            source_store=self._source_store,
            collection_id=self._collection_id,
            object_id=self._object_id,
            expected_bytes=expected_bytes,
        )
        try:
            archive_session = self._archive.begin_write(
                object_path=object_path,
                expected_bytes=expected_bytes,
                content_type=content_type,
                metadata=metadata,
            )
        except BaseException:
            self._release_optional_cache()
            raise
        if admission is None:
            self._release_optional_cache()
        return WriteSession(
            object_path=object_path,
            write_token=_encode_write_token(
                archive_session=archive_session,
                admission=admission,
                content_type=content_type,
                metadata=metadata,
            ),
            expected_bytes=expected_bytes,
        )

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        content: bytes,
    ) -> WriteSegmentReceipt:
        archive_session, admission, _content_type, _metadata = _decode_write_token(session)
        cache_session = _cache_session(admission)
        if (
            admission is None
            or cache_session is None
            or not self._cache.is_current(admission=admission)
        ):
            return self._archive.write_segment(
                session=archive_session,
                number=number,
                content=content,
            )
        cache_objects = self._cache.resumable_object_store(admission=admission)
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
                cache_objects.write_segment,
                session=cache_session,
                number=number,
                content=content,
            )
            archive_error: BaseException | None = None
            try:
                archive_receipt = archive_future.result()
            except BaseException as exc:
                archive_error = exc
                archive_receipt = None
            try:
                cache_receipt = cache_future.result()
            except Exception:
                _LOG.warning(
                    "optional retrieval-cache segment mirror failed; archive transfer continues",
                    exc_info=True,
                )
                self._release_optional_cache()
                if archive_error is not None:
                    raise archive_error from None
                assert archive_receipt is not None
                return archive_receipt
        if archive_error is not None:
            raise archive_error
        assert archive_receipt is not None
        if (
            archive_receipt.number != cache_receipt.number
            or archive_receipt.bytes != cache_receipt.bytes
        ):
            self._release_optional_cache()
            raise RuntimeError("archive and retrieval cache segment receipts disagree")
        return archive_receipt

    def list_segments(self, *, session: WriteSession) -> tuple[WriteSegmentReceipt, ...]:
        archive_session, admission, _content_type, _metadata = _decode_write_token(session)
        archive_segments = self._archive.list_segments(session=archive_session)
        cache_session = _cache_session(admission)
        if (
            admission is None
            or cache_session is None
            or not self._cache.is_current(admission=admission)
        ):
            return archive_segments
        try:
            cache_segments = {
                current.number: current
                for current in self._cache.resumable_object_store(
                    admission=admission
                ).list_segments(session=cache_session)
            }
        except Exception:
            _LOG.warning(
                "optional retrieval-cache reconciliation failed; archive transfer continues",
                exc_info=True,
            )
            self._release_optional_cache()
            return archive_segments
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
        expected_content_type: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt:
        archive_session, admission, content_type, _metadata = _decode_write_token(session)
        if expected_bytes != session.expected_bytes:
            raise ValueError("archive-cache mirror byte identity changed")
        if content_type != expected_content_type:
            raise ValueError("archive-cache mirror content type changed")
        cache_receipt = admission.completed if admission is not None else None
        cache_session = _cache_session(admission)
        if (
            admission is not None
            and cache_receipt is None
            and cache_session is not None
            and self._cache.is_current(admission=admission)
        ):
            try:
                cache_objects = self._cache.resumable_object_store(admission=admission)
                cache_completed = cache_objects.find_completed_write(
                    object_path=cache_session.object_path,
                    expected_bytes=expected_bytes,
                    expected_content_type="application/octet-stream",
                    expected_metadata={},
                )
                if cache_completed is None:
                    cache_segments = cache_objects.list_segments(session=cache_session)
                    _require_matching_segments(segments, cache_segments)
                    cache_completed = cache_objects.complete_write(
                        session=cache_session,
                        segments=cache_segments,
                        expected_bytes=expected_bytes,
                        expected_content_type="application/octet-stream",
                        expected_metadata={},
                    )
                cache_receipt = cache_completed.retrieval_cache or RetrievalCacheReceipt(
                    cache_store=admission.cache_store,
                    object_path=cache_completed.object_path,
                    revision=cache_completed.revision,
                    stored_bytes=cache_completed.bytes,
                    stored_sha256=None,
                    cached_at=cache_completed.completed_at,
                    verified_at=cache_completed.completed_at,
                )
            except Exception:
                _LOG.warning(
                    "optional retrieval-cache completion failed; archive completion continues",
                    exc_info=True,
                )
                self._release_optional_cache()
        archive_completed = self._archive.complete_write(
            session=archive_session,
            segments=segments,
            expected_bytes=expected_bytes,
            expected_content_type=expected_content_type,
            expected_metadata=expected_metadata,
        )
        if admission is not None and cache_receipt is None:
            self._release_optional_cache()
        return replace(archive_completed, retrieval_cache=cache_receipt)

    def find_completed_write(
        self,
        *,
        object_path: str,
        expected_bytes: int,
        expected_content_type: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None:
        archive_completed = self._archive.find_completed_write(
            object_path=object_path,
            expected_bytes=expected_bytes,
            expected_content_type=expected_content_type,
            expected_metadata=expected_metadata,
        )
        if archive_completed is None:
            return None
        admission = self._cache.admit(
            owner=self._owner,
            source_store=self._source_store,
            collection_id=self._collection_id,
            object_id=self._object_id,
            expected_bytes=expected_bytes,
        )
        if admission is None:
            self._cache.release(owner=self._owner)
            return archive_completed
        cache_receipt = admission.completed
        cache_session = _cache_session(admission)
        if cache_receipt is None and cache_session is not None:
            cache_completed = self._cache.resumable_object_store(
                admission=admission
            ).find_completed_write(
                object_path=cache_session.object_path,
                expected_bytes=expected_bytes,
                expected_content_type="application/octet-stream",
                expected_metadata={},
            )
            if cache_completed is not None:
                cache_receipt = cache_completed.retrieval_cache or RetrievalCacheReceipt(
                    cache_store=admission.cache_store,
                    object_path=cache_completed.object_path,
                    revision=cache_completed.revision,
                    stored_bytes=cache_completed.bytes,
                    stored_sha256=None,
                    cached_at=cache_completed.completed_at,
                    verified_at=cache_completed.completed_at,
                )
        if cache_receipt is None:
            self._release_optional_cache()
            return archive_completed
        return replace(archive_completed, retrieval_cache=cache_receipt)

    def abort_write(self, *, session: WriteSession) -> None:
        archive_session, _admission, _content_type, _metadata = _decode_write_token(session)
        archive_error: BaseException | None = None
        try:
            self._archive.abort_write(session=archive_session)
        except BaseException as exc:
            archive_error = exc
        self._release_optional_cache()
        if archive_error is not None:
            raise archive_error

    def _release_optional_cache(self) -> None:
        try:
            self._cache.release(owner=self._owner)
        except Exception:
            # The claim deletion is committed before provider cleanup. A
            # failed cleanup remains in normalized abandoning state for the
            # bounded sweeper and must never gate archive authority.
            _LOG.warning("optional retrieval-cache cleanup deferred", exc_info=True)


def _encode_write_token(
    *,
    archive_session: WriteSession,
    admission: RetrievalCacheAdmission | None,
    content_type: str,
    metadata: dict[str, str],
) -> str:
    return json.dumps(
        {
            "schema": _WRITE_SCHEMA,
            "content_type": content_type,
            "metadata": dict(sorted(metadata.items())),
            "archive": _session_payload(archive_session),
            "cache": _admission_payload(admission),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_write_token(
    session: WriteSession,
) -> tuple[WriteSession, RetrievalCacheAdmission | None, str, dict[str, str]]:
    try:
        payload = json.loads(session.write_token)
    except json.JSONDecodeError as exc:
        raise ValueError("archive-cache mirror write token is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema") != _WRITE_SCHEMA:
        raise ValueError("archive-cache mirror write-token schema mismatch")
    archive = _session_from_payload(payload.get("archive"), label="archive")
    admission = _admission_from_payload(payload.get("cache"))
    content_type = str(payload.get("content_type") or "").strip()
    if not content_type:
        raise ValueError("archive-cache mirror content type is invalid")
    raw_metadata = payload.get("metadata")
    if not isinstance(raw_metadata, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in raw_metadata.items()
    ):
        raise ValueError("archive-cache mirror required identity assertions is invalid")
    if (
        archive.object_path != session.object_path
        or archive.expected_bytes != session.expected_bytes
    ):
        raise ValueError("archive-cache mirror object identity changed")
    if admission is not None and admission.expected_bytes != session.expected_bytes:
        raise ValueError("archive-cache mirror admission identity changed")
    return archive, admission, content_type, dict(raw_metadata)


def _session_payload(session: WriteSession) -> dict[str, object]:
    return {
        "object_path": session.object_path,
        "write_token": session.write_token,
        "expected_bytes": session.expected_bytes,
    }


def _session_from_payload(value: object, *, label: str) -> WriteSession:
    if not isinstance(value, dict):
        raise ValueError(f"archive-cache mirror {label} write session is invalid")
    object_path = str(value.get("object_path", ""))
    write_token = str(value.get("write_token", ""))
    expected_bytes = value.get("expected_bytes")
    if (
        not object_path
        or not write_token
        or not isinstance(expected_bytes, int)
        or expected_bytes < 1
    ):
        raise ValueError(f"archive-cache mirror {label} write identity is invalid")
    return WriteSession(object_path, write_token, expected_bytes)


def _admission_payload(admission: RetrievalCacheAdmission | None) -> dict[str, object] | None:
    if admission is None:
        return None
    return {
        "owner": admission.owner,
        "cache_store": admission.cache_store,
        "source_store": admission.source_store,
        "collection_id": admission.collection_id,
        "object_id": admission.object_id,
        "object_path": admission.object_path,
        "expected_bytes": admission.expected_bytes,
        "write_token": admission.write_token,
        "admitted_at": admission.admitted_at,
        "completed": retrieval_cache_receipt_payload(admission.completed),
    }


def _admission_from_payload(value: object) -> RetrievalCacheAdmission | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("archive-cache mirror cache admission is invalid")
    try:
        collection_id = int(value["collection_id"])
        expected_bytes = int(value["expected_bytes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("archive-cache mirror cache admission is invalid") from exc
    write_token = value.get("write_token")
    if write_token is not None and not isinstance(write_token, str):
        raise ValueError("archive-cache mirror cache continuation is invalid")
    admission = RetrievalCacheAdmission(
        owner=str(value.get("owner") or ""),
        cache_store=str(value.get("cache_store") or ""),
        source_store=str(value.get("source_store") or ""),
        collection_id=collection_id,
        object_id=str(value.get("object_id") or ""),
        object_path=str(value.get("object_path") or ""),
        expected_bytes=expected_bytes,
        write_token=write_token,
        admitted_at=str(value.get("admitted_at") or ""),
        completed=parse_retrieval_cache_receipt(value.get("completed")),
    )
    if (
        not admission.owner
        or not admission.cache_store
        or not admission.source_store
        or not admission.object_id
        or not admission.object_path
        or not admission.admitted_at
        or admission.collection_id < 1
        or admission.expected_bytes < 1
    ):
        raise ValueError("archive-cache mirror cache admission is invalid")
    if admission.write_token is None and admission.completed is None:
        raise ValueError("archive-cache mirror cache admission has no result or continuation")
    return admission


def _cache_session(admission: RetrievalCacheAdmission | None) -> WriteSession | None:
    if admission is None or admission.write_token is None:
        return None
    return WriteSession(
        admission.object_path,
        admission.write_token,
        admission.expected_bytes,
    )


def _require_matching_segments(
    archive: tuple[WriteSegmentReceipt, ...],
    cache: tuple[WriteSegmentReceipt, ...],
) -> None:
    archive_shape = tuple((current.number, current.bytes) for current in archive)
    cache_shape = tuple((current.number, current.bytes) for current in cache)
    if archive_shape != cache_shape:
        raise RuntimeError("retrieval cache write segments do not match the archive write")


__all__ = ["MirroredArchiveResumableObjectStore"]
