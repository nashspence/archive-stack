from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace

from riverhog_age import CHUNK_SIZE, MultipartPartPlan, ResumableAgeScryptSession, UploadState
from riverhog_protocol.pack_ingress import canonical_json_bytes
from riverhog_protocol.paths import normalize_relpath

from riverhog_core.archive_formats import RAW_VOLUME_STORAGE_FORMAT
from riverhog_core.domain.archive import (
    RawVolumePlan,
    SealedRawVolume,
    StoredPartReceipt,
)
from riverhog_core.ports.archive_objects import (
    ArchiveMultipartObjectStore,
    CompletedObjectReceipt,
    MultipartPartReceipt,
    MultipartUpload,
)
from riverhog_core.ports.archive_upload_checkpoints import RawUploadCheckpointStore
from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.raw_volume import raw_multipart_part_plans
from riverhog_core.retrieval_cache_receipts import (
    parse_retrieval_cache_receipt,
    retrieval_cache_receipt_payload,
)
from riverhog_core.streaming_age import (
    PreparedAgePart,
    ResumableAgeSessionCache,
    iter_rechunk,
    prepare_age_part,
)
from riverhog_core.throughput import (
    DEFAULT_AGE_DERIVATION_CONCURRENCY,
    DEFAULT_AGE_SESSION_CACHE_ENTRIES,
    DEFAULT_SOURCE_READ_CHUNK_BYTES,
    DEFAULT_UPLOAD_MAX_INFLIGHT_BYTES,
    DEFAULT_UPLOAD_PREPARE_CONCURRENCY,
    DEFAULT_UPLOAD_REQUEST_CONCURRENCY,
    ArchiveTransferResources,
    TransferConcurrencyGate,
    TransferTiming,
    WeightedByteSemaphore,
)

RAW_UPLOAD_CHECKPOINT_SCHEMA = "raw-upload-checkpoint/v1"
RAW_VOLUME_CONTENT_TYPE = "application/vnd.riverhog.raw-segment+age"
_SEGMENT_ID_RE = re.compile(r"segment-[0-9]{12}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
TransferTimingObserver = Callable[[TransferTiming], None]


@dataclass(frozen=True, slots=True)
class CompletedRawObject:
    revision: str
    stored_bytes: int
    stored_sha256: str
    completed_at: str
    retrieval_cache: RetrievalCacheReceipt | None = None


@dataclass(frozen=True, slots=True)
class RawUploadCheckpoint:
    collection_id: int
    volume_id: str
    object_path: str
    relative_path: str
    source_path: str
    file_offset: int
    plaintext_bytes: int
    file_bytes: int
    file_sha256: str
    target_part_plaintext_bytes: int
    expected_part_sha256s: tuple[str, ...]
    transfer_id: str
    age_state_json: str
    next_part: int
    parts: tuple[StoredPartReceipt, ...]
    completed: CompletedRawObject | None = None

    def to_json(self) -> str:
        ordered = tuple(sorted(self.parts, key=lambda current: current.number))
        return canonical_json_bytes(
            {
                "schema": RAW_UPLOAD_CHECKPOINT_SCHEMA,
                "collection_id": self.collection_id,
                "volume_id": self.volume_id,
                "object_path": self.object_path,
                "relative_path": self.relative_path,
                "source_path": self.source_path,
                "file_offset": self.file_offset,
                "plaintext_bytes": self.plaintext_bytes,
                "file_bytes": self.file_bytes,
                "file_sha256": self.file_sha256,
                "target_part_plaintext_bytes": self.target_part_plaintext_bytes,
                "expected_part_sha256s": list(self.expected_part_sha256s),
                "transfer_id": self.transfer_id,
                "age_state": json.loads(self.age_state_json),
                "next_part": self.next_part,
                "parts": [_part_payload(current) for current in ordered],
                "completed": (
                    {
                        "revision": self.completed.revision,
                        "stored_bytes": self.completed.stored_bytes,
                        "stored_sha256": self.completed.stored_sha256,
                        "completed_at": self.completed.completed_at,
                        "retrieval_cache": retrieval_cache_receipt_payload(
                            self.completed.retrieval_cache
                        ),
                    }
                    if self.completed is not None
                    else None
                ),
            }
        ).decode("utf-8")

    @classmethod
    def from_json(cls, content: str) -> RawUploadCheckpoint:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("raw upload checkpoint is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("schema") != RAW_UPLOAD_CHECKPOINT_SCHEMA:
            raise ValueError("raw upload checkpoint schema mismatch")
        expected_fields = {
            "schema",
            "collection_id",
            "volume_id",
            "object_path",
            "relative_path",
            "source_path",
            "file_offset",
            "plaintext_bytes",
            "file_bytes",
            "file_sha256",
            "target_part_plaintext_bytes",
            "expected_part_sha256s",
            "transfer_id",
            "age_state",
            "next_part",
            "parts",
            "completed",
        }
        if set(payload) != expected_fields:
            raise ValueError("raw upload checkpoint fields are invalid")
        age_state = payload.get("age_state")
        raw_parts = payload.get("parts")
        raw_expected = payload.get("expected_part_sha256s")
        if (
            not isinstance(age_state, dict)
            or not isinstance(raw_parts, list)
            or not isinstance(raw_expected, list)
        ):
            raise ValueError("raw upload checkpoint structure is invalid")
        age_state_json = canonical_json_bytes(age_state).decode("utf-8")
        state = UploadState.from_json_bytes(age_state_json)
        plaintext_bytes = _uint(payload.get("plaintext_bytes"), "plaintext bytes")
        if state.plaintext_size != plaintext_bytes:
            raise ValueError("raw upload age state plaintext size mismatch")
        expected_part_sha256s = tuple(
            _sha(current, "expected part plaintext") for current in raw_expected
        )
        parts = _parts_from_payload(raw_parts)
        next_part = _uint(payload.get("next_part"), "next part")
        if next_part != _first_missing_part(parts):
            raise ValueError("raw upload next part does not match its receipts")
        completed = _completed_from_payload(payload.get("completed"))
        if completed is not None:
            _require_complete_coverage(parts, plaintext_bytes=plaintext_bytes)
            if completed.stored_bytes != sum(current.stored_bytes for current in parts):
                raise ValueError("completed raw upload stored byte count mismatch")
        collection_id = _uint(payload.get("collection_id"), "collection id")
        if collection_id < 1:
            raise ValueError("collection id must be positive")
        volume_id = str(payload.get("volume_id", ""))
        if _SEGMENT_ID_RE.fullmatch(volume_id) is None:
            raise ValueError("raw upload volume id is invalid")
        file_offset = _uint(payload.get("file_offset"), "file offset")
        file_bytes = _uint(payload.get("file_bytes"), "file bytes")
        if file_offset + plaintext_bytes > file_bytes:
            raise ValueError("raw upload file range is invalid")
        file_sha256 = _sha(payload.get("file_sha256"), "raw upload file")
        object_path = str(payload.get("object_path", ""))
        transfer_id = str(payload.get("transfer_id", ""))
        if not object_path or not transfer_id:
            raise ValueError("raw upload storage identity is invalid")
        target_part_plaintext_bytes = _uint(
            payload.get("target_part_plaintext_bytes"),
            "target part plaintext bytes",
        )
        if target_part_plaintext_bytes < CHUNK_SIZE or target_part_plaintext_bytes % CHUNK_SIZE:
            raise ValueError("raw upload part target must be a positive age-chunk multiple")
        return cls(
            collection_id=collection_id,
            volume_id=volume_id,
            object_path=object_path,
            relative_path=normalize_relpath(str(payload.get("relative_path", ""))),
            source_path=normalize_relpath(str(payload.get("source_path", ""))),
            file_offset=file_offset,
            plaintext_bytes=plaintext_bytes,
            file_bytes=file_bytes,
            file_sha256=file_sha256,
            target_part_plaintext_bytes=target_part_plaintext_bytes,
            expected_part_sha256s=expected_part_sha256s,
            transfer_id=transfer_id,
            age_state_json=age_state_json,
            next_part=next_part,
            parts=parts,
            completed=completed,
        )


@dataclass(frozen=True, slots=True)
class _UploadedRawPart:
    receipt: StoredPartReceipt
    queue_wait_seconds: float
    source_seconds: float
    crypto_seconds: float
    remote_seconds: float
    elapsed_seconds: float


class RawVolumeUploader:
    def __init__(
        self,
        *,
        object_store: ArchiveMultipartObjectStore,
        checkpoint_store: RawUploadCheckpointStore,
        passphrase: str,
        scrypt_log_n: int,
        max_inflight_bytes: int = DEFAULT_UPLOAD_MAX_INFLIGHT_BYTES,
        source_read_chunk_bytes: int = DEFAULT_SOURCE_READ_CHUNK_BYTES,
        prepare_concurrency: int = DEFAULT_UPLOAD_PREPARE_CONCURRENCY,
        byte_budget: WeightedByteSemaphore | None = None,
        prepare_gate: TransferConcurrencyGate | None = None,
        request_gate: TransferConcurrencyGate | None = None,
        resources: ArchiveTransferResources | None = None,
        timing_observer: TransferTimingObserver | None = None,
        session_cache: ResumableAgeSessionCache | None = None,
        session_cache_entries: int = DEFAULT_AGE_SESSION_CACHE_ENTRIES,
        age_derivation_concurrency: int = DEFAULT_AGE_DERIVATION_CONCURRENCY,
        derivation_gate: TransferConcurrencyGate | None = None,
    ) -> None:
        if not passphrase:
            raise ValueError("archive passphrase must not be empty")
        if source_read_chunk_bytes < 64 * 1024:
            raise ValueError("source read chunk must be at least 64 KiB")
        self._object_store = object_store
        self._checkpoint_store = checkpoint_store
        self._passphrase = passphrase
        self._scrypt_log_n = scrypt_log_n
        self._source_read_chunk_bytes = source_read_chunk_bytes
        if resources is not None and (
            byte_budget is not None
            or prepare_gate is not None
            or request_gate is not None
            or derivation_gate is not None
        ):
            raise ValueError(
                "shared transfer resources cannot be combined with explicit upload limits"
            )
        self._byte_budget = (
            resources.upload_bytes
            if resources is not None
            else byte_budget or WeightedByteSemaphore(max_inflight_bytes)
        )
        self._prepare_gate = (
            resources.upload_preparations
            if resources is not None
            else prepare_gate or TransferConcurrencyGate(prepare_concurrency)
        )
        self._request_gate = (
            resources.upload_requests
            if resources is not None
            else request_gate or TransferConcurrencyGate(DEFAULT_UPLOAD_REQUEST_CONCURRENCY)
        )
        self._timing_observer = timing_observer
        self._derivation_gate = (
            resources.age_derivations
            if resources is not None
            else derivation_gate or TransferConcurrencyGate(age_derivation_concurrency)
        )
        self._session_cache = session_cache or ResumableAgeSessionCache(
            passphrase,
            max_entries=session_cache_entries,
            derivation_gate=self._derivation_gate,
        )

    def open(
        self,
        *,
        collection_id: int,
        plan: RawVolumePlan,
        object_path: str,
        relative_path: str,
        target_part_plaintext_bytes: int,
        expected_part_sha256s: Sequence[str] = (),
    ) -> RawUploadCheckpoint:
        opened_started = time.perf_counter()
        if collection_id < 1 or not object_path:
            raise ValueError("raw upload collection and object identities are required")
        normalized_relative_path = normalize_relpath(relative_path)
        expected_relative_path = f"volumes/{plan.volume_id}.bin.age"
        if normalized_relative_path != expected_relative_path:
            raise ValueError("raw volume relative path is not canonical")
        if not object_path.endswith(f"/{normalized_relative_path}"):
            raise ValueError("raw volume object path does not contain its relative path")
        if target_part_plaintext_bytes < CHUNK_SIZE or target_part_plaintext_bytes % CHUNK_SIZE:
            raise ValueError("raw upload part target must be a positive age-chunk multiple")
        expected_digests = tuple(
            _sha(current, "expected part plaintext") for current in expected_part_sha256s
        )
        existing = self._checkpoint_store.load_raw_upload_checkpoint(
            collection_id=collection_id,
            volume_id=plan.volume_id,
        )
        if existing is not None:
            checkpoint = RawUploadCheckpoint.from_json(existing)
            self._validate(plan, checkpoint)
            if (
                checkpoint.collection_id != collection_id
                or checkpoint.object_path != object_path
                or checkpoint.relative_path != normalized_relative_path
                or checkpoint.target_part_plaintext_bytes != target_part_plaintext_bytes
                or checkpoint.expected_part_sha256s != expected_digests
            ):
                raise ValueError("raw upload checkpoint does not match the requested upload")
            if checkpoint.completed is None:
                completed = self._object_store.head_completed_object(
                    object_path=object_path,
                    expected_metadata=_metadata(plan, checkpoint.age_state_json),
                )
                if completed is not None:
                    return self._mark_completed(checkpoint, completed)
                self._reconcile_recorded_parts(checkpoint)
            return checkpoint

        completed = self._object_store.head_completed_object(
            object_path=object_path,
            expected_metadata=_metadata(plan),
        )
        if completed is not None:
            raise RuntimeError("completed raw object exists without its durable checkpoint")
        with self._derivation_gate.reserve() as derivation_wait_seconds:
            crypto_started = time.perf_counter()
            session = ResumableAgeScryptSession.create(
                self._passphrase,
                log_n=self._scrypt_log_n,
                plaintext_size=plan.plaintext_bytes,
            )
            crypto_seconds = time.perf_counter() - crypto_started
        planned_parts = raw_multipart_part_plans(
            plan,
            session,
            target_plaintext_bytes=target_part_plaintext_bytes,
        )
        if expected_digests and len(expected_digests) != len(planned_parts):
            raise ValueError("raw upload expected part digest count does not match the plan")
        age_state_json = (
            session.export_state(plaintext_size=plan.plaintext_bytes)
            .to_json_bytes()
            .decode("utf-8")
        )
        self._session_cache.remember(age_state_json, session)
        remote_started = time.perf_counter()
        upload = self._object_store.create_multipart_upload(
            object_path=object_path,
            content_type=RAW_VOLUME_CONTENT_TYPE,
            metadata=_metadata(plan, age_state_json),
            expected_bytes=sum(current.ciphertext_len for current in planned_parts),
        )
        remote_seconds = time.perf_counter() - remote_started
        checkpoint = RawUploadCheckpoint(
            collection_id=collection_id,
            volume_id=plan.volume_id,
            object_path=object_path,
            relative_path=normalized_relative_path,
            source_path=plan.source_path,
            file_offset=plan.file_offset,
            plaintext_bytes=plan.plaintext_bytes,
            file_bytes=plan.file_bytes,
            file_sha256=plan.file_sha256,
            target_part_plaintext_bytes=target_part_plaintext_bytes,
            expected_part_sha256s=expected_digests,
            transfer_id=upload.transfer_id,
            age_state_json=age_state_json,
            next_part=0,
            parts=(),
        )
        checkpoint_started = time.perf_counter()
        checkpoint = self._save(checkpoint)
        checkpoint_seconds = time.perf_counter() - checkpoint_started
        self._observe_timing(
            TransferTiming(
                operation="raw_upload_open",
                identity=plan.volume_id,
                plaintext_bytes=0,
                stored_bytes=0,
                queue_wait_seconds=derivation_wait_seconds,
                source_seconds=0.0,
                crypto_seconds=crypto_seconds,
                remote_seconds=remote_seconds,
                checkpoint_seconds=checkpoint_seconds,
                elapsed_seconds=time.perf_counter() - opened_started,
            )
        )
        return checkpoint

    def upload_next_part(
        self,
        *,
        plan: RawVolumePlan,
        checkpoint: RawUploadCheckpoint,
        plaintext: bytes | Iterable[bytes],
    ) -> RawUploadCheckpoint:
        if checkpoint.completed is not None:
            return checkpoint
        session = self._session_cache.get(checkpoint.age_state_json)
        plans = raw_multipart_part_plans(
            plan,
            session,
            target_plaintext_bytes=checkpoint.target_part_plaintext_bytes,
        )
        if checkpoint.next_part >= len(plans):
            return self._complete(checkpoint)
        return self.upload_part(
            plan=plan,
            checkpoint=checkpoint,
            part_number=checkpoint.next_part + 1,
            plaintext=plaintext,
        )

    def upload_part(
        self,
        *,
        plan: RawVolumePlan,
        checkpoint: RawUploadCheckpoint,
        part_number: int,
        plaintext: bytes | Iterable[bytes],
    ) -> RawUploadCheckpoint:
        self._validate(plan, checkpoint)
        if checkpoint.completed is not None:
            return checkpoint
        planned = self._planned_parts(plan, checkpoint)
        if part_number < 1 or part_number > len(planned):
            raise ValueError("raw upload part number is outside the plan")
        if _part_by_number(checkpoint.parts, part_number) is not None:
            return checkpoint
        uploaded = self._prepare_and_upload_part(
            plan=plan,
            checkpoint=checkpoint,
            part=planned[part_number - 1],
            plaintext_chunks=_chunks(plaintext),
        )
        checkpoint, checkpoint_seconds = self._record_part(checkpoint, uploaded.receipt)
        self._observe(uploaded, checkpoint_seconds=checkpoint_seconds)
        if len(checkpoint.parts) == len(planned):
            return self._complete(checkpoint)
        return checkpoint

    def sealed_receipt(self, checkpoint: RawUploadCheckpoint) -> SealedRawVolume:
        self._validate(_checkpoint_plan(checkpoint), checkpoint)
        completed = checkpoint.completed
        if completed is None:
            raise RuntimeError("raw volume is not sealed")
        return SealedRawVolume(
            volume_id=checkpoint.volume_id,
            sequence=int(checkpoint.volume_id.removeprefix("segment-")),
            relative_path=checkpoint.relative_path,
            source_path=checkpoint.source_path,
            file_offset=checkpoint.file_offset,
            plaintext_bytes=checkpoint.plaintext_bytes,
            file_bytes=checkpoint.file_bytes,
            file_sha256=checkpoint.file_sha256,
            age_state_json=checkpoint.age_state_json,
            parts=tuple(sorted(checkpoint.parts, key=lambda current: current.number)),
            revision=completed.revision,
            completed_at=completed.completed_at,
            retrieval_cache=completed.retrieval_cache,
        )

    def abort(self, checkpoint: RawUploadCheckpoint) -> None:
        if checkpoint.completed is not None:
            raise ValueError("cannot abort a completed raw object")
        self._object_store.abort_multipart_upload(
            upload=MultipartUpload(checkpoint.object_path, checkpoint.transfer_id)
        )
        self._checkpoint_store.delete_raw_upload_checkpoint(
            collection_id=checkpoint.collection_id,
            volume_id=checkpoint.volume_id,
        )

    def _prepare_and_upload_part(
        self,
        *,
        plan: RawVolumePlan,
        checkpoint: RawUploadCheckpoint,
        part: MultipartPartPlan,
        plaintext_chunks: Iterable[bytes],
    ) -> _UploadedRawPart:
        started = time.perf_counter()
        session = self._session_cache.get(checkpoint.age_state_json)
        working_bytes = (
            part.ciphertext_len
            + min(self._source_read_chunk_bytes, max(1, part.plaintext_len))
            + CHUNK_SIZE
        )
        byte_reserved = False
        with self._prepare_gate.reserve() as prepare_wait_seconds:
            byte_wait_seconds = self._byte_budget.acquire(working_bytes)
            byte_reserved = True
            try:
                prepared = prepare_age_part(
                    session=session,
                    plan=part,
                    total_plaintext_bytes=plan.plaintext_bytes,
                    plaintext_chunks=iter_rechunk(
                        plaintext_chunks,
                        chunk_bytes=self._source_read_chunk_bytes,
                    ),
                )
                expected_digest = _expected_part_digest(
                    checkpoint,
                    part.part_number,
                )
                if expected_digest is not None and prepared.plaintext_sha256 != expected_digest:
                    raise ValueError(
                        "raw upload part does not match its registered digest manifest"
                    )
            except BaseException:
                self._byte_budget.release(working_bytes)
                byte_reserved = False
                raise
        try:
            with self._request_gate.reserve() as request_wait_seconds:
                remote_started = time.perf_counter()
                remote = self._object_store.upload_part(
                    upload=MultipartUpload(checkpoint.object_path, checkpoint.transfer_id),
                    number=part.part_number,
                    content=prepared.ciphertext,
                )
                remote_seconds = time.perf_counter() - remote_started
        finally:
            if byte_reserved:
                self._byte_budget.release(working_bytes)
        queue_wait_seconds = prepare_wait_seconds + byte_wait_seconds + request_wait_seconds
        if remote.number != part.part_number or remote.stored_bytes != prepared.stored_bytes:
            raise RuntimeError("multipart store returned an inconsistent raw part receipt")
        return _UploadedRawPart(
            receipt=_stored_part_receipt(part, prepared, remote),
            queue_wait_seconds=queue_wait_seconds,
            source_seconds=prepared.source_seconds,
            crypto_seconds=prepared.crypto_seconds,
            remote_seconds=remote_seconds,
            elapsed_seconds=time.perf_counter() - started,
        )

    def _record_part(
        self,
        checkpoint: RawUploadCheckpoint,
        part: StoredPartReceipt,
    ) -> tuple[RawUploadCheckpoint, float]:
        existing = _part_by_number(checkpoint.parts, part.number)
        if existing is not None and existing != part:
            raise RuntimeError("raw upload part receipt changed for an immutable part number")
        parts = checkpoint.parts if existing is not None else (*checkpoint.parts, part)
        updated = replace(
            checkpoint,
            next_part=_first_missing_part(parts),
            parts=tuple(sorted(parts, key=lambda current: current.number)),
        )
        started = time.perf_counter()
        persisted = self._save(updated)
        return persisted, time.perf_counter() - started

    def _complete(self, checkpoint: RawUploadCheckpoint) -> RawUploadCheckpoint:
        if checkpoint.completed is not None:
            return checkpoint
        expected_parts = self._planned_parts(_checkpoint_plan(checkpoint), checkpoint)
        parts = tuple(sorted(checkpoint.parts, key=lambda current: current.number))
        if tuple(current.number for current in parts) != tuple(
            current.part_number for current in expected_parts
        ):
            raise RuntimeError("cannot complete a raw volume with pending parts")
        completed = self._object_store.complete_multipart_upload(
            upload=MultipartUpload(checkpoint.object_path, checkpoint.transfer_id),
            parts=tuple(
                MultipartPartReceipt(
                    current.number,
                    current.part_token,
                    current.stored_bytes,
                    current.stored_sha256,
                )
                for current in parts
            ),
            expected_bytes=sum(current.stored_bytes for current in parts),
            expected_metadata=_metadata(
                _checkpoint_plan(checkpoint),
                checkpoint.age_state_json,
            ),
        )
        return self._mark_completed(checkpoint, completed)

    def _mark_completed(
        self,
        checkpoint: RawUploadCheckpoint,
        completed: CompletedObjectReceipt,
    ) -> RawUploadCheckpoint:
        if completed.stored_bytes != sum(current.stored_bytes for current in checkpoint.parts):
            raise RuntimeError("completed raw object byte count mismatch")
        if completed.object_path != checkpoint.object_path:
            raise RuntimeError("completed raw object path mismatch")
        sealed = replace(
            checkpoint,
            parts=tuple(sorted(checkpoint.parts, key=lambda current: current.number)),
            completed=CompletedRawObject(
                revision=completed.revision,
                stored_bytes=completed.stored_bytes,
                stored_sha256=completed.stored_sha256,
                completed_at=completed.completed_at,
                retrieval_cache=completed.retrieval_cache,
            ),
        )
        return self._save(sealed)

    def _planned_parts(
        self,
        plan: RawVolumePlan,
        checkpoint: RawUploadCheckpoint,
    ) -> tuple[MultipartPartPlan, ...]:
        session = self._session_cache.get(checkpoint.age_state_json)
        return raw_multipart_part_plans(
            plan,
            session,
            target_plaintext_bytes=checkpoint.target_part_plaintext_bytes,
        )

    def _validate(self, plan: RawVolumePlan, checkpoint: RawUploadCheckpoint) -> None:
        if (
            checkpoint.volume_id != plan.volume_id
            or checkpoint.source_path != plan.source_path
            or checkpoint.file_offset != plan.file_offset
            or checkpoint.plaintext_bytes != plan.plaintext_bytes
            or checkpoint.file_bytes != plan.file_bytes
            or checkpoint.file_sha256 != plan.file_sha256
        ):
            raise ValueError("raw upload checkpoint does not match its plan")
        state = UploadState.from_json_bytes(checkpoint.age_state_json)
        if state.plaintext_size != plan.plaintext_bytes:
            raise ValueError("raw upload checkpoint age state does not match its plan")
        planned_parts = self._planned_parts(plan, checkpoint)
        if checkpoint.expected_part_sha256s and (
            len(checkpoint.expected_part_sha256s) != len(planned_parts)
        ):
            raise ValueError("raw upload expected part digest count is invalid")
        by_number = {current.part_number: current for current in planned_parts}
        for part in checkpoint.parts:
            planned = by_number.get(part.number)
            if planned is None or (
                part.plaintext_start != planned.plaintext_start
                or part.plaintext_bytes != planned.plaintext_len
            ):
                raise ValueError("raw upload checkpoint part does not match the plan")
            expected_digest = _expected_part_digest(checkpoint, part.number)
            if expected_digest is not None and part.plaintext_sha256 != expected_digest:
                raise ValueError("raw upload checkpoint part digest does not match its manifest")
        if checkpoint.next_part != _first_missing_part(checkpoint.parts):
            raise ValueError("raw upload checkpoint next part is invalid")
        if checkpoint.completed is not None and len(checkpoint.parts) != len(planned_parts):
            raise ValueError("completed raw upload checkpoint has pending parts")

    def _reconcile_recorded_parts(self, checkpoint: RawUploadCheckpoint) -> None:
        remote = {
            current.number: current
            for current in self._object_store.list_parts(
                upload=MultipartUpload(checkpoint.object_path, checkpoint.transfer_id)
            )
        }
        for current in checkpoint.parts:
            found = remote.get(current.number)
            if (
                found is None
                or found.part_token != current.part_token
                or found.stored_bytes != current.stored_bytes
                or found.stored_sha256 != current.stored_sha256
            ):
                raise RuntimeError("multipart store no longer contains a recorded raw part")

    def _observe(self, uploaded: _UploadedRawPart, *, checkpoint_seconds: float) -> None:
        if self._timing_observer is None:
            return
        self._observe_timing(
            TransferTiming(
                operation="raw_upload_part",
                identity=str(uploaded.receipt.number),
                plaintext_bytes=uploaded.receipt.plaintext_bytes,
                stored_bytes=uploaded.receipt.stored_bytes,
                queue_wait_seconds=uploaded.queue_wait_seconds,
                source_seconds=uploaded.source_seconds,
                crypto_seconds=uploaded.crypto_seconds,
                remote_seconds=uploaded.remote_seconds,
                checkpoint_seconds=checkpoint_seconds,
                elapsed_seconds=uploaded.elapsed_seconds + checkpoint_seconds,
            )
        )

    def _observe_timing(self, timing: TransferTiming) -> None:
        if self._timing_observer is not None:
            self._timing_observer(timing)

    def _save(self, checkpoint: RawUploadCheckpoint) -> RawUploadCheckpoint:
        persisted = self._checkpoint_store.merge_raw_upload_checkpoint(
            collection_id=checkpoint.collection_id,
            volume_id=checkpoint.volume_id,
            checkpoint_json=checkpoint.to_json(),
        )
        return RawUploadCheckpoint.from_json(persisted)


def merge_raw_upload_checkpoints(
    current: RawUploadCheckpoint,
    candidate: RawUploadCheckpoint,
) -> RawUploadCheckpoint:
    """Merge concurrent raw-part acknowledgements under a database row lock."""

    static_fields = (
        "collection_id",
        "volume_id",
        "object_path",
        "relative_path",
        "source_path",
        "file_offset",
        "plaintext_bytes",
        "file_bytes",
        "file_sha256",
        "target_part_plaintext_bytes",
        "expected_part_sha256s",
        "transfer_id",
        "age_state_json",
    )
    if any(getattr(current, name) != getattr(candidate, name) for name in static_fields):
        raise ValueError("raw upload checkpoints do not describe the same multipart object")
    parts = {part.number: part for part in current.parts}
    for part in candidate.parts:
        existing = parts.get(part.number)
        if existing is not None and existing != part:
            raise RuntimeError("raw upload checkpoint contains conflicting part receipts")
        parts[part.number] = part
    ordered = tuple(sorted(parts.values(), key=lambda part: part.number))
    completed = current.completed or candidate.completed
    if current.completed is not None and candidate.completed is not None:
        if current.completed != candidate.completed:
            raise RuntimeError("raw upload checkpoint completion receipt changed")
    merged = replace(
        current,
        next_part=_first_missing_part(ordered),
        parts=ordered,
        completed=completed,
    )
    if completed is not None:
        _require_complete_coverage(
            ordered,
            plaintext_bytes=merged.plaintext_bytes,
        )
        if completed.stored_bytes != sum(part.stored_bytes for part in ordered):
            raise RuntimeError("completed raw checkpoint does not match its merged parts")
    return merged


def _metadata(
    plan: RawVolumePlan,
    age_state_json: str | None = None,
) -> dict[str, str]:
    metadata = {
        "riverhog-format": RAW_VOLUME_STORAGE_FORMAT,
        "riverhog-source-path-sha256": hashlib.sha256(plan.source_path.encode("utf-8")).hexdigest(),
        "riverhog-file-offset": str(plan.file_offset),
        "riverhog-plaintext-bytes": str(plan.plaintext_bytes),
        "riverhog-file-sha256": plan.file_sha256,
    }
    if age_state_json is not None:
        state = UploadState.from_json_bytes(age_state_json)
        metadata["riverhog-age-state-sha256"] = hashlib.sha256(state.to_json_bytes()).hexdigest()
    return metadata


def _checkpoint_plan(checkpoint: RawUploadCheckpoint) -> RawVolumePlan:
    return RawVolumePlan(
        volume_id=checkpoint.volume_id,
        sequence=int(checkpoint.volume_id.removeprefix("segment-")),
        source_path=checkpoint.source_path,
        file_offset=checkpoint.file_offset,
        plaintext_bytes=checkpoint.plaintext_bytes,
        file_bytes=checkpoint.file_bytes,
        file_sha256=checkpoint.file_sha256,
    )


def _stored_part_receipt(
    part: MultipartPartPlan,
    prepared: PreparedAgePart,
    remote: MultipartPartReceipt,
) -> StoredPartReceipt:
    return StoredPartReceipt(
        number=part.part_number,
        plaintext_start=part.plaintext_start,
        plaintext_bytes=prepared.plaintext_bytes,
        plaintext_sha256=prepared.plaintext_sha256,
        stored_bytes=prepared.stored_bytes,
        stored_sha256=prepared.stored_sha256,
        part_token=remote.part_token,
    )


def _chunks(value: bytes | Iterable[bytes]) -> Iterable[bytes]:
    if isinstance(value, bytes):
        return (value,)
    return value


def _expected_part_digest(
    checkpoint: RawUploadCheckpoint,
    number: int,
) -> str | None:
    if not checkpoint.expected_part_sha256s:
        return None
    return checkpoint.expected_part_sha256s[number - 1]


def _parts_from_payload(value: list[object]) -> tuple[StoredPartReceipt, ...]:
    parts: list[StoredPartReceipt] = []
    previous_number = 0
    previous_end = 0
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("raw upload part is invalid")
        part = StoredPartReceipt(
            number=_uint(raw.get("number"), "part number"),
            plaintext_start=_uint(raw.get("plaintext_start"), "part plaintext start"),
            plaintext_bytes=_uint(raw.get("plaintext_bytes"), "part plaintext bytes"),
            plaintext_sha256=_sha(raw.get("plaintext_sha256"), "part plaintext"),
            stored_bytes=_uint(raw.get("stored_bytes"), "part stored bytes"),
            stored_sha256=_sha(raw.get("stored_sha256"), "part stored"),
            part_token=str(raw.get("part_token", "")),
        )
        if (
            part.number <= previous_number
            or part.plaintext_start < previous_end
            or part.stored_bytes < 1
            or not part.part_token
        ):
            raise ValueError("raw upload part order is invalid")
        previous_number = part.number
        previous_end = part.plaintext_start + part.plaintext_bytes
        parts.append(part)
    return tuple(parts)


def _require_complete_coverage(
    parts: tuple[StoredPartReceipt, ...],
    *,
    plaintext_bytes: int,
) -> None:
    expected_start = 0
    for expected_number, part in enumerate(parts, start=1):
        if part.number != expected_number or part.plaintext_start != expected_start:
            raise ValueError("completed raw upload parts are not contiguous")
        expected_start += part.plaintext_bytes
    if expected_start != plaintext_bytes:
        raise ValueError("completed raw upload does not cover its plaintext")


def _first_missing_part(parts: tuple[StoredPartReceipt, ...]) -> int:
    numbers = {current.number for current in parts}
    current = 1
    while current in numbers:
        current += 1
    return current - 1


def _part_by_number(
    parts: tuple[StoredPartReceipt, ...],
    number: int,
) -> StoredPartReceipt | None:
    return next((current for current in parts if current.number == number), None)


def _part_payload(current: StoredPartReceipt) -> dict[str, object]:
    return {
        "number": current.number,
        "plaintext_start": current.plaintext_start,
        "plaintext_bytes": current.plaintext_bytes,
        "plaintext_sha256": current.plaintext_sha256,
        "stored_bytes": current.stored_bytes,
        "stored_sha256": current.stored_sha256,
        "part_token": current.part_token,
    }


def _completed_from_payload(value: object) -> CompletedRawObject | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("raw upload completion is invalid")
    byte_count = _uint(value.get("stored_bytes"), "completed stored bytes")
    completed_at = str(value.get("completed_at", ""))
    if byte_count < 1 or not completed_at:
        raise ValueError("raw upload completion receipt is invalid")
    revision = str(value.get("revision", ""))
    stored_sha256 = _sha(value.get("stored_sha256"), "completed stored")
    if not revision:
        raise ValueError("raw upload completion revision is invalid")
    return CompletedRawObject(
        revision=revision,
        stored_bytes=byte_count,
        stored_sha256=stored_sha256,
        completed_at=completed_at,
        retrieval_cache=parse_retrieval_cache_receipt(value.get("retrieval_cache")),
    )


def _sha(value: object, label: str) -> str:
    candidate = str(value or "")
    if _SHA256_RE.fullmatch(candidate) is None:
        raise ValueError(f"{label} sha256 is invalid")
    return candidate


def _uint(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative integer")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a non-negative integer") from exc
    if parsed < 0 or str(parsed) != str(value):
        raise ValueError(f"{label} must be a canonical non-negative integer")
    return parsed
