from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace

from riverhog_age import (
    AEAD_TAG_SIZE,
    CHUNK_SIZE,
    ResumableAgeScryptSession,
    S3PartPlan,
    UploadState,
)
from riverhog_protocol.pack_ingress import canonical_json_bytes
from riverhog_protocol.paths import normalize_relpath

from riverhog_core.archive_formats import PACK_VOLUME_STORAGE_FORMAT
from riverhog_core.domain.archive import (
    PackUploadUnitPlan,
    PackVolumePlan,
    SealedPackVolume,
    StoredPartReceipt,
)
from riverhog_core.pack_volume import iter_render_pack_upload_unit_payload
from riverhog_core.ports.archive_ingress_store import (
    ArchiveMultipartObjectStore,
    CompletedObjectReceipt,
    MultipartPartReceipt,
    MultipartUpload,
    PackUploadCheckpointStore,
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
    DEFAULT_S3_PART_CONCURRENCY,
    DEFAULT_SOURCE_READ_CHUNK_BYTES,
    DEFAULT_UPLOAD_MAX_INFLIGHT_BYTES,
    DEFAULT_UPLOAD_PREPARE_CONCURRENCY,
    ArchiveTransferResources,
    TransferConcurrencyGate,
    TransferTiming,
    WeightedByteSemaphore,
)

PACK_UPLOAD_CHECKPOINT_SCHEMA = "pack-upload-checkpoint/v1"
PACK_VOLUME_CONTENT_TYPE = "application/vnd.riverhog.pack+age"
_PACK_VOLUME_ID_RE = re.compile(r"pack-[0-9]{12}")
TransferTimingObserver = Callable[[TransferTiming], None]
PackPayloadFactory = Callable[[int], Iterable[bytes]]


@dataclass(frozen=True, slots=True)
class CompletedPackObject:
    version_id: str | None
    etag: str | None
    bytes: int
    completed_at: str


@dataclass(frozen=True, slots=True)
class PackUploadCheckpoint:
    collection_id: int
    volume_id: str
    object_path: str
    relative_path: str
    plan_sha256: str
    plaintext_bytes: int
    upload_id: str
    age_state_json: str
    next_unit: int
    parts: tuple[StoredPartReceipt, ...]
    completed: CompletedPackObject | None = None

    def to_json(self) -> str:
        ordered_parts = tuple(sorted(self.parts, key=lambda current: current.number))
        payload: dict[str, object] = {
            "schema": PACK_UPLOAD_CHECKPOINT_SCHEMA,
            "collection_id": self.collection_id,
            "volume_id": self.volume_id,
            "object_path": self.object_path,
            "relative_path": self.relative_path,
            "plan_sha256": self.plan_sha256,
            "plaintext_bytes": self.plaintext_bytes,
            "upload_id": self.upload_id,
            "age_state": json.loads(self.age_state_json),
            "next_unit": self.next_unit,
            "parts": [_part_payload(current) for current in ordered_parts],
            "completed": (
                {
                    "version_id": self.completed.version_id,
                    "etag": self.completed.etag,
                    "bytes": self.completed.bytes,
                    "completed_at": self.completed.completed_at,
                }
                if self.completed is not None
                else None
            ),
        }
        return canonical_json_bytes(payload).decode("utf-8")

    @classmethod
    def from_json(cls, content: str) -> PackUploadCheckpoint:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("pack upload checkpoint is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("schema") != PACK_UPLOAD_CHECKPOINT_SCHEMA:
            raise ValueError("pack upload checkpoint schema mismatch")
        expected_fields = {
            "schema",
            "collection_id",
            "volume_id",
            "object_path",
            "relative_path",
            "plan_sha256",
            "plaintext_bytes",
            "upload_id",
            "age_state",
            "next_unit",
            "parts",
            "completed",
        }
        if set(payload) != expected_fields:
            raise ValueError("pack upload checkpoint fields are invalid")
        age_state = payload.get("age_state")
        raw_parts = payload.get("parts")
        if not isinstance(age_state, dict) or not isinstance(raw_parts, list):
            raise ValueError("pack upload checkpoint structure is invalid")
        age_state_json = canonical_json_bytes(age_state).decode("utf-8")
        age_upload_state = UploadState.from_json_bytes(age_state_json)
        parts = _parts_from_payload(raw_parts)
        next_unit = _canonical_nonnegative_int(payload.get("next_unit"), label="next unit")
        if next_unit != _first_missing_unit(parts):
            raise ValueError("pack upload checkpoint next unit does not match its parts")
        plaintext_bytes = _canonical_nonnegative_int(
            payload.get("plaintext_bytes"), label="plaintext bytes"
        )
        completed = _completed_from_payload(payload.get("completed"))
        if completed is not None:
            _require_complete_plaintext_coverage(parts, plaintext_bytes=plaintext_bytes)
            if completed.bytes != sum(current.stored_bytes for current in parts):
                raise ValueError("completed pack upload checkpoint stored byte count mismatch")
        if age_upload_state.plaintext_size != plaintext_bytes:
            raise ValueError("pack upload age state plaintext size mismatch")
        volume_id = str(payload.get("volume_id", ""))
        object_path = str(payload.get("object_path", ""))
        upload_id = str(payload.get("upload_id", ""))
        if _PACK_VOLUME_ID_RE.fullmatch(volume_id) is None:
            raise ValueError("pack upload checkpoint volume id is invalid")
        if not object_path or not upload_id:
            raise ValueError("pack upload checkpoint storage identity is invalid")
        return cls(
            collection_id=_canonical_positive_int(
                payload.get("collection_id"), label="collection id"
            ),
            volume_id=volume_id,
            object_path=object_path,
            relative_path=normalize_relpath(str(payload.get("relative_path", ""))),
            plan_sha256=_required_sha256(payload.get("plan_sha256"), label="plan"),
            plaintext_bytes=plaintext_bytes,
            upload_id=upload_id,
            age_state_json=age_state_json,
            next_unit=next_unit,
            parts=parts,
            completed=completed,
        )


@dataclass(frozen=True, slots=True)
class _UploadedPackPart:
    receipt: StoredPartReceipt
    queue_wait_seconds: float
    source_seconds: float
    crypto_seconds: float
    remote_seconds: float
    elapsed_seconds: float


class PackVolumeUploader:
    """Encrypt deterministic pack units directly into one final multipart object.

    Units may be prepared and uploaded out of order. Checkpoint writes remain serialized by
    the coordinator, and an unrecorded successful UploadPart is safely overwritten on retry.
    Remote ListParts is used once when an existing checkpoint is opened and once by the
    object-store completion guard, rather than once per part.
    """

    def __init__(
        self,
        *,
        object_store: ArchiveMultipartObjectStore,
        checkpoint_store: PackUploadCheckpointStore,
        passphrase: str,
        scrypt_log_n: int,
        part_concurrency: int = DEFAULT_S3_PART_CONCURRENCY,
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
        if part_concurrency < 1:
            raise ValueError("pack part concurrency must be positive")
        if source_read_chunk_bytes < 64 * 1024:
            raise ValueError("source read chunk must be at least 64 KiB")
        self._object_store = object_store
        self._checkpoint_store = checkpoint_store
        self._passphrase = passphrase
        self._scrypt_log_n = scrypt_log_n
        self._part_concurrency = part_concurrency
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
            else request_gate or TransferConcurrencyGate(part_concurrency)
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
        plan: PackVolumePlan,
        object_path: str,
        relative_path: str,
    ) -> PackUploadCheckpoint:
        opened_started = time.perf_counter()
        if collection_id < 1:
            raise ValueError("collection id must be positive")
        normalized_relative_path = normalize_relpath(relative_path)
        expected_relative_path = f"volumes/{plan.volume_id}.tar.age"
        if normalized_relative_path != expected_relative_path:
            raise ValueError("pack volume relative path is not canonical")
        if not object_path.endswith(f"/{normalized_relative_path}"):
            raise ValueError("pack volume object path does not contain its relative path")
        existing_json = self._checkpoint_store.load_pack_upload_checkpoint(
            collection_id=collection_id,
            volume_id=plan.volume_id,
        )
        if existing_json is not None:
            checkpoint = PackUploadCheckpoint.from_json(existing_json)
            self._validate_checkpoint(
                collection_id=collection_id,
                plan=plan,
                checkpoint=checkpoint,
                object_path=object_path,
                relative_path=normalized_relative_path,
            )
            if checkpoint.completed is None:
                completed = self._object_store.head_completed_object(
                    object_path=object_path,
                    expected_metadata=_object_metadata(plan, checkpoint.age_state_json),
                )
                if completed is not None:
                    return self._mark_completed(checkpoint, completed)
                self._reconcile_recorded_parts(checkpoint)
            return checkpoint

        completed = self._object_store.head_completed_object(
            object_path=object_path,
            expected_metadata=_object_metadata(plan),
        )
        if completed is not None:
            raise RuntimeError(
                "completed pack object exists without a checkpoint; retain the upload receipt "
                "transaction before discarding checkpoint state"
            )
        with self._derivation_gate.reserve() as derivation_wait_seconds:
            crypto_started = time.perf_counter()
            session = ResumableAgeScryptSession.create(
                self._passphrase,
                log_n=self._scrypt_log_n,
                plaintext_size=plan.plaintext_bytes,
            )
            crypto_seconds = time.perf_counter() - crypto_started
        age_state_json = (
            session.export_state(plaintext_size=plan.plaintext_bytes)
            .to_json_bytes()
            .decode("utf-8")
        )
        self._session_cache.remember(age_state_json, session)
        remote_started = time.perf_counter()
        upload = self._object_store.create_multipart_upload(
            object_path=object_path,
            content_type=PACK_VOLUME_CONTENT_TYPE,
            metadata=_object_metadata(plan, age_state_json),
        )
        remote_seconds = time.perf_counter() - remote_started
        checkpoint = PackUploadCheckpoint(
            collection_id=collection_id,
            volume_id=plan.volume_id,
            object_path=object_path,
            relative_path=normalized_relative_path,
            plan_sha256=plan.plan_sha256,
            plaintext_bytes=plan.plaintext_bytes,
            upload_id=upload.upload_id,
            age_state_json=age_state_json,
            next_unit=0,
            parts=(),
        )
        checkpoint_started = time.perf_counter()
        checkpoint = self._save(checkpoint)
        checkpoint_seconds = time.perf_counter() - checkpoint_started
        self._observe_timing(
            TransferTiming(
                operation="pack_upload_open",
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

    def upload_next_unit(
        self,
        *,
        plan: PackVolumePlan,
        checkpoint: PackUploadCheckpoint,
        payload_chunks: Iterable[bytes],
    ) -> PackUploadCheckpoint:
        if checkpoint.completed is not None:
            return checkpoint
        if checkpoint.next_unit >= len(plan.units):
            return self._complete(plan=plan, checkpoint=checkpoint)
        return self.upload_unit(
            plan=plan,
            checkpoint=checkpoint,
            unit_number=checkpoint.next_unit,
            payload_chunks=payload_chunks,
        )

    def upload_unit(
        self,
        *,
        plan: PackVolumePlan,
        checkpoint: PackUploadCheckpoint,
        unit_number: int,
        payload_chunks: Iterable[bytes],
    ) -> PackUploadCheckpoint:
        self._validate_checkpoint(
            collection_id=checkpoint.collection_id,
            plan=plan,
            checkpoint=checkpoint,
            object_path=checkpoint.object_path,
            relative_path=checkpoint.relative_path,
        )
        if checkpoint.completed is not None:
            return checkpoint
        if unit_number < 0 or unit_number >= len(plan.units):
            raise ValueError("pack upload unit number is outside the plan")
        if _part_by_number(checkpoint.parts, unit_number + 1) is not None:
            return checkpoint
        uploaded = self._prepare_and_upload_unit(
            plan=plan,
            checkpoint=checkpoint,
            unit_number=unit_number,
            payload_chunks=payload_chunks,
        )
        checkpoint, checkpoint_seconds = self._record_part(checkpoint, uploaded.receipt)
        self._observe(uploaded, checkpoint_seconds=checkpoint_seconds)
        if len(checkpoint.parts) == len(plan.units):
            return self._complete(plan=plan, checkpoint=checkpoint)
        return checkpoint

    def upload_units_concurrently(
        self,
        *,
        plan: PackVolumePlan,
        checkpoint: PackUploadCheckpoint,
        payload_factory: PackPayloadFactory,
        concurrency: int | None = None,
    ) -> PackUploadCheckpoint:
        """Upload all missing units with bounded parallel preparation and S3 requests.

        At most ``concurrency`` source iterators and ciphertext part bodies exist at once.
        Checkpoint merges happen on the calling thread after each completed worker.
        """

        self._validate_checkpoint(
            collection_id=checkpoint.collection_id,
            plan=plan,
            checkpoint=checkpoint,
            object_path=checkpoint.object_path,
            relative_path=checkpoint.relative_path,
        )
        if checkpoint.completed is not None:
            return checkpoint
        completed_numbers = {current.number for current in checkpoint.parts}
        missing = iter(unit.unit for unit in plan.units if unit.unit + 1 not in completed_numbers)
        workers = concurrency or self._part_concurrency
        if workers < 1:
            raise ValueError("pack part concurrency must be positive")
        current = checkpoint
        pending: dict[Future[_UploadedPackPart], int] = {}
        exhausted = False

        def fill(executor: ThreadPoolExecutor) -> None:
            nonlocal exhausted
            while not exhausted and len(pending) < workers:
                try:
                    unit_number = next(missing)
                except StopIteration:
                    exhausted = True
                    return
                payload = payload_factory(unit_number)
                pending[
                    executor.submit(
                        self._prepare_and_upload_unit,
                        plan=plan,
                        checkpoint=checkpoint,
                        unit_number=unit_number,
                        payload_chunks=payload,
                    )
                ] = unit_number

        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="riverhog-pack-part",
        ) as executor:
            fill(executor)
            try:
                while pending:
                    done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                    for future in done:
                        pending.pop(future)
                        uploaded = future.result()
                        current, checkpoint_seconds = self._record_part(
                            current,
                            uploaded.receipt,
                        )
                        self._observe(uploaded, checkpoint_seconds=checkpoint_seconds)
                    fill(executor)
            except BaseException:
                for future in pending:
                    future.cancel()
                raise
        return self._complete(plan=plan, checkpoint=current)

    def sealed_receipt(
        self,
        *,
        plan: PackVolumePlan,
        checkpoint: PackUploadCheckpoint,
    ) -> SealedPackVolume:
        self._validate_checkpoint(
            collection_id=checkpoint.collection_id,
            plan=plan,
            checkpoint=checkpoint,
            object_path=checkpoint.object_path,
            relative_path=checkpoint.relative_path,
        )
        completed = checkpoint.completed
        if completed is None:
            raise RuntimeError("pack volume is not sealed")
        return SealedPackVolume(
            volume_id=plan.volume_id,
            sequence=plan.sequence,
            relative_path=checkpoint.relative_path,
            files=len(plan.members),
            source_bytes=sum(current.bytes for current in plan.members),
            plaintext_bytes=plan.plaintext_bytes,
            age_state_json=checkpoint.age_state_json,
            index_sha256=plan.index_sha256,
            plan_sha256=plan.plan_sha256,
            parts=tuple(sorted(checkpoint.parts, key=lambda current: current.number)),
            version_id=completed.version_id,
            completed_at=completed.completed_at,
        )

    def abort(self, checkpoint: PackUploadCheckpoint) -> None:
        if checkpoint.completed is not None:
            raise ValueError("cannot abort a completed pack object")
        self._object_store.abort_multipart_upload(
            upload=MultipartUpload(
                object_path=checkpoint.object_path,
                upload_id=checkpoint.upload_id,
            )
        )
        self._checkpoint_store.delete_pack_upload_checkpoint(
            collection_id=checkpoint.collection_id,
            volume_id=checkpoint.volume_id,
        )

    def _prepare_and_upload_unit(
        self,
        *,
        plan: PackVolumePlan,
        checkpoint: PackUploadCheckpoint,
        unit_number: int,
        payload_chunks: Iterable[bytes],
    ) -> _UploadedPackPart:
        started = time.perf_counter()
        unit = plan.units[unit_number]
        session = self._session_cache.get(checkpoint.age_state_json)
        age_plan = _age_part_plan(
            session=session,
            total_plaintext_bytes=plan.plaintext_bytes,
            unit=unit,
        )
        working_bytes = (
            age_plan.ciphertext_len
            + min(self._source_read_chunk_bytes, max(1, age_plan.plaintext_len))
            + CHUNK_SIZE
        )
        byte_reserved = False
        with self._prepare_gate.reserve() as prepare_wait_seconds:
            byte_wait_seconds = self._byte_budget.acquire(working_bytes)
            byte_reserved = True
            try:
                prepared = prepare_age_part(
                    session=session,
                    plan=age_plan,
                    total_plaintext_bytes=plan.plaintext_bytes,
                    plaintext_chunks=iter_render_pack_upload_unit_payload(
                        plan,
                        unit_number,
                        iter_rechunk(
                            payload_chunks,
                            chunk_bytes=self._source_read_chunk_bytes,
                        ),
                    ),
                )
            except BaseException:
                self._byte_budget.release(working_bytes)
                byte_reserved = False
                raise
        try:
            with self._request_gate.reserve() as request_wait_seconds:
                remote_started = time.perf_counter()
                remote_part = self._object_store.upload_part(
                    upload=MultipartUpload(
                        object_path=checkpoint.object_path,
                        upload_id=checkpoint.upload_id,
                    ),
                    number=age_plan.part_number,
                    content=prepared.ciphertext,
                )
                remote_seconds = time.perf_counter() - remote_started
        finally:
            if byte_reserved:
                self._byte_budget.release(working_bytes)
        queue_wait_seconds = prepare_wait_seconds + byte_wait_seconds + request_wait_seconds
        if remote_part.number != age_plan.part_number:
            raise RuntimeError("multipart store returned an inconsistent part number")
        if remote_part.bytes != prepared.stored_bytes:
            raise RuntimeError("multipart store returned an inconsistent part size")
        return _UploadedPackPart(
            receipt=_stored_part_receipt(unit, prepared, remote_part),
            queue_wait_seconds=queue_wait_seconds,
            source_seconds=prepared.source_seconds,
            crypto_seconds=prepared.crypto_seconds,
            remote_seconds=remote_seconds,
            elapsed_seconds=time.perf_counter() - started,
        )

    def _record_part(
        self,
        checkpoint: PackUploadCheckpoint,
        part: StoredPartReceipt,
    ) -> tuple[PackUploadCheckpoint, float]:
        existing = _part_by_number(checkpoint.parts, part.number)
        if existing is not None and existing != part:
            raise RuntimeError("pack upload part receipt changed for an immutable part number")
        parts = checkpoint.parts if existing is not None else (*checkpoint.parts, part)
        ordered = tuple(sorted(parts, key=lambda current: current.number))
        updated = PackUploadCheckpoint(
            collection_id=checkpoint.collection_id,
            volume_id=checkpoint.volume_id,
            object_path=checkpoint.object_path,
            relative_path=checkpoint.relative_path,
            plan_sha256=checkpoint.plan_sha256,
            plaintext_bytes=checkpoint.plaintext_bytes,
            upload_id=checkpoint.upload_id,
            age_state_json=checkpoint.age_state_json,
            next_unit=_first_missing_unit(ordered),
            parts=ordered,
            completed=checkpoint.completed,
        )
        started = time.perf_counter()
        persisted = self._save(updated)
        return persisted, time.perf_counter() - started

    def _complete(
        self,
        *,
        plan: PackVolumePlan,
        checkpoint: PackUploadCheckpoint,
    ) -> PackUploadCheckpoint:
        if checkpoint.completed is not None:
            return checkpoint
        expected_numbers = tuple(range(1, len(plan.units) + 1))
        parts = tuple(sorted(checkpoint.parts, key=lambda current: current.number))
        if tuple(current.number for current in parts) != expected_numbers:
            raise RuntimeError("cannot complete a pack with pending upload units")
        upload = MultipartUpload(checkpoint.object_path, checkpoint.upload_id)
        completed = self._object_store.complete_multipart_upload(
            upload=upload,
            parts=tuple(
                MultipartPartReceipt(current.number, current.etag, current.stored_bytes)
                for current in parts
            ),
            expected_bytes=sum(current.stored_bytes for current in parts),
            expected_metadata=_object_metadata(plan, checkpoint.age_state_json),
        )
        return self._mark_completed(checkpoint, completed)

    def _mark_completed(
        self,
        checkpoint: PackUploadCheckpoint,
        completed: CompletedObjectReceipt,
    ) -> PackUploadCheckpoint:
        expected_bytes = sum(current.stored_bytes for current in checkpoint.parts)
        if completed.object_path != checkpoint.object_path or completed.bytes != expected_bytes:
            raise RuntimeError("completed pack object does not match its checkpoint")
        sealed = PackUploadCheckpoint(
            collection_id=checkpoint.collection_id,
            volume_id=checkpoint.volume_id,
            object_path=checkpoint.object_path,
            relative_path=checkpoint.relative_path,
            plan_sha256=checkpoint.plan_sha256,
            plaintext_bytes=checkpoint.plaintext_bytes,
            upload_id=checkpoint.upload_id,
            age_state_json=checkpoint.age_state_json,
            next_unit=checkpoint.next_unit,
            parts=tuple(sorted(checkpoint.parts, key=lambda current: current.number)),
            completed=CompletedPackObject(
                version_id=completed.version_id,
                etag=completed.etag,
                bytes=completed.bytes,
                completed_at=completed.completed_at,
            ),
        )
        return self._save(sealed)

    def _validate_checkpoint(
        self,
        *,
        collection_id: int,
        plan: PackVolumePlan,
        checkpoint: PackUploadCheckpoint,
        object_path: str,
        relative_path: str,
    ) -> None:
        if (
            checkpoint.collection_id != collection_id
            or checkpoint.volume_id != plan.volume_id
            or checkpoint.plan_sha256 != plan.plan_sha256
            or checkpoint.plaintext_bytes != plan.plaintext_bytes
            or checkpoint.object_path != object_path
            or checkpoint.relative_path != normalize_relpath(relative_path)
        ):
            raise ValueError("pack upload checkpoint does not match the requested plan")
        state = UploadState.from_json_bytes(checkpoint.age_state_json)
        if state.plaintext_size != plan.plaintext_bytes:
            raise ValueError("pack upload checkpoint age state does not match the plan")
        units = {current.unit + 1: current for current in plan.units}
        for part in checkpoint.parts:
            unit = units.get(part.number)
            if unit is None or (
                part.plaintext_start != unit.plaintext_start
                or part.plaintext_bytes != unit.plaintext_bytes
            ):
                raise ValueError("pack upload checkpoint parts do not match the plan")
        if checkpoint.next_unit != _first_missing_unit(checkpoint.parts):
            raise ValueError("pack upload checkpoint next unit is invalid")
        if checkpoint.completed is not None and len(checkpoint.parts) != len(plan.units):
            raise ValueError("completed pack upload checkpoint has pending units")

    def _reconcile_recorded_parts(self, checkpoint: PackUploadCheckpoint) -> None:
        remote = {
            current.number: current
            for current in self._object_store.list_parts(
                upload=MultipartUpload(checkpoint.object_path, checkpoint.upload_id)
            )
        }
        for current in checkpoint.parts:
            found = remote.get(current.number)
            if found is None or found.etag != current.etag or found.bytes != current.stored_bytes:
                raise RuntimeError("multipart store no longer contains a recorded pack part")

    def _observe(self, uploaded: _UploadedPackPart, *, checkpoint_seconds: float) -> None:
        if self._timing_observer is None:
            return
        self._observe_timing(
            TransferTiming(
                operation="pack_upload_part",
                identity=f"{uploaded.receipt.number}",
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

    def _save(self, checkpoint: PackUploadCheckpoint) -> PackUploadCheckpoint:
        persisted = self._checkpoint_store.merge_pack_upload_checkpoint(
            collection_id=checkpoint.collection_id,
            volume_id=checkpoint.volume_id,
            checkpoint_json=checkpoint.to_json(),
        )
        return PackUploadCheckpoint.from_json(persisted)


def merge_pack_upload_checkpoints(
    current: PackUploadCheckpoint,
    candidate: PackUploadCheckpoint,
) -> PackUploadCheckpoint:
    """Merge a stale concurrent part acknowledgement without losing durable receipts."""

    static_fields = (
        "collection_id",
        "volume_id",
        "object_path",
        "relative_path",
        "plan_sha256",
        "plaintext_bytes",
        "upload_id",
        "age_state_json",
    )
    if any(getattr(current, name) != getattr(candidate, name) for name in static_fields):
        raise ValueError("pack upload checkpoints do not describe the same multipart object")
    parts = {part.number: part for part in current.parts}
    for part in candidate.parts:
        existing = parts.get(part.number)
        if existing is not None and existing != part:
            raise RuntimeError("pack upload checkpoint contains conflicting part receipts")
        parts[part.number] = part
    ordered = tuple(sorted(parts.values(), key=lambda part: part.number))
    completed = current.completed or candidate.completed
    if current.completed is not None and candidate.completed is not None:
        if current.completed != candidate.completed:
            raise RuntimeError("pack upload checkpoint completion receipt changed")
    merged = replace(
        current,
        next_unit=_first_missing_unit(ordered),
        parts=ordered,
        completed=completed,
    )
    if completed is not None:
        _require_complete_plaintext_coverage(
            ordered,
            plaintext_bytes=merged.plaintext_bytes,
        )
        if completed.bytes != sum(part.stored_bytes for part in ordered):
            raise RuntimeError("completed pack checkpoint does not match its merged parts")
    return merged


def _stored_part_receipt(
    unit: PackUploadUnitPlan,
    prepared: PreparedAgePart,
    remote: MultipartPartReceipt,
) -> StoredPartReceipt:
    return StoredPartReceipt(
        number=prepared.part_number,
        plaintext_start=unit.plaintext_start,
        plaintext_bytes=prepared.plaintext_bytes,
        plaintext_sha256=prepared.plaintext_sha256,
        stored_bytes=prepared.stored_bytes,
        stored_sha256=prepared.stored_sha256,
        etag=remote.etag,
    )


def _age_part_plan(
    *,
    session: ResumableAgeScryptSession,
    total_plaintext_bytes: int,
    unit: PackUploadUnitPlan,
) -> S3PartPlan:
    start = int(unit.plaintext_start)
    end = int(unit.plaintext_end)
    if start < 0 or end <= start or end > total_plaintext_bytes:
        raise ValueError("pack unit plaintext range is invalid")
    final = end == total_plaintext_bytes
    if start % CHUNK_SIZE or (not final and end % CHUNK_SIZE):
        raise ValueError("non-final pack unit boundaries must align to age chunks")
    first_chunk = start // CHUNK_SIZE
    chunk_count = (end - start + CHUNK_SIZE - 1) // CHUNK_SIZE
    includes_prefix = first_chunk == 0
    prefix_bytes = len(session.age_prefix)
    ciphertext_start = (
        0 if includes_prefix else prefix_bytes + first_chunk * (CHUNK_SIZE + AEAD_TAG_SIZE)
    )
    ciphertext_bytes = (prefix_bytes if includes_prefix else 0) + (end - start)
    ciphertext_bytes += chunk_count * AEAD_TAG_SIZE
    return S3PartPlan(
        part_number=int(unit.unit) + 1,
        first_chunk=first_chunk,
        chunk_count=chunk_count,
        includes_age_prefix=includes_prefix,
        plaintext_start=start,
        plaintext_end=end,
        ciphertext_start=ciphertext_start,
        ciphertext_end=ciphertext_start + ciphertext_bytes,
    )


def _object_metadata(
    plan: PackVolumePlan,
    age_state_json: str | None = None,
) -> dict[str, str]:
    metadata = {
        "riverhog-format": PACK_VOLUME_STORAGE_FORMAT,
        "riverhog-plan-sha256": plan.plan_sha256,
        "riverhog-plaintext-bytes": str(plan.plaintext_bytes),
        "riverhog-index-sha256": plan.index_sha256,
    }
    if age_state_json is not None:
        state = UploadState.from_json_bytes(age_state_json)
        metadata["riverhog-age-state-sha256"] = hashlib.sha256(state.to_json_bytes()).hexdigest()
    return metadata


def _parts_from_payload(raw_parts: list[object]) -> tuple[StoredPartReceipt, ...]:
    parts: list[StoredPartReceipt] = []
    previous_number = 0
    previous_end = 0
    for raw in raw_parts:
        if not isinstance(raw, dict):
            raise ValueError("pack upload checkpoint part must be a mapping")
        part = StoredPartReceipt(
            number=_canonical_nonnegative_int(raw.get("number"), label="part number"),
            plaintext_start=_canonical_nonnegative_int(
                raw.get("plaintext_start"), label="part plaintext start"
            ),
            plaintext_bytes=_canonical_nonnegative_int(
                raw.get("plaintext_bytes"), label="part plaintext bytes"
            ),
            plaintext_sha256=_required_sha256(raw.get("plaintext_sha256"), label="part"),
            stored_bytes=_canonical_nonnegative_int(
                raw.get("stored_bytes"), label="part stored bytes"
            ),
            stored_sha256=_required_sha256(raw.get("stored_sha256"), label="stored part"),
            etag=str(raw.get("etag", "")),
        )
        if (
            part.number <= previous_number
            or part.number > 10_000
            or part.plaintext_start < previous_end
            or part.plaintext_bytes <= 0
            or part.stored_bytes <= 0
            or not part.etag
        ):
            raise ValueError("pack upload checkpoint part order is invalid")
        previous_number = part.number
        previous_end = part.plaintext_start + part.plaintext_bytes
        parts.append(part)
    return tuple(parts)


def _require_complete_plaintext_coverage(
    parts: tuple[StoredPartReceipt, ...],
    *,
    plaintext_bytes: int,
) -> None:
    expected_start = 0
    for expected_number, part in enumerate(parts, start=1):
        if part.number != expected_number or part.plaintext_start != expected_start:
            raise ValueError("completed pack upload parts are not contiguous")
        expected_start += part.plaintext_bytes
    if expected_start != plaintext_bytes:
        raise ValueError("completed pack upload does not cover its plaintext")


def _first_missing_unit(parts: tuple[StoredPartReceipt, ...]) -> int:
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
        "etag": current.etag,
    }


def _completed_from_payload(value: object) -> CompletedPackObject | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("pack upload checkpoint completion is invalid")
    byte_count = _canonical_nonnegative_int(value.get("bytes"), label="completed bytes")
    completed_at = str(value.get("completed_at", ""))
    if byte_count <= 0 or not completed_at:
        raise ValueError("pack upload checkpoint completion receipt is invalid")
    version_id = value.get("version_id")
    etag = value.get("etag")
    return CompletedPackObject(
        version_id=str(version_id) if version_id is not None else None,
        etag=str(etag) if etag is not None else None,
        bytes=byte_count,
        completed_at=completed_at,
    )


def _required_sha256(value: object, *, label: str) -> str:
    candidate = str(value or "")
    if len(candidate) != 64 or any(ch not in "0123456789abcdef" for ch in candidate):
        raise ValueError(f"{label} sha256 is invalid")
    return candidate


def _canonical_positive_int(value: object, *, label: str) -> int:
    parsed = _canonical_nonnegative_int(value, label=label)
    if parsed < 1:
        raise ValueError(f"{label} must be positive")
    return parsed


def _canonical_nonnegative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative integer")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a non-negative integer") from exc
    if parsed < 0 or str(parsed) != str(value):
        raise ValueError(f"{label} must be a canonical non-negative integer")
    return parsed
