"""Reusable content-opaque direct-to-final Riverhog collection producer."""

from __future__ import annotations

import builtins
import hashlib
import itertools
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal, cast

from riverhog_protocol import (
    COLLECTION_TAG_REQUEST_MEMBERS_MAX,
    CollectionDescription,
    CollectionTag,
)
from riverhog_protocol.collection_upload_transport import (
    CollectionUploadArtifactCustodyReceiptDocument,
    CollectionUploadRegistrationConstraintsDocument,
    CollectionUploadUnitWorkDocument,
    validate_collection_upload_artifact_custody_receipt,
)
from riverhog_protocol.collection_workflows import (
    DERIVATION_DISPOSITION_EVIDENCE_PREFIX,
    DERIVATION_EVIDENCE_PATH,
    DERIVATION_OUTPUT_EVIDENCE_PREFIX,
    PRODUCER_EVIDENCE_PATH,
    JsonValue,
    ProducerEvidence,
)
from riverhog_protocol.file_identity import ImmutableFileIdentityDocument
from riverhog_protocol.paths import CollectionId, normalize_relpath, validate_collection_id
from riverhog_protocol.storage_names import ArchiveStoreName

from riverhog_api_client.client import ApiClient
from riverhog_api_client.source_hashing import RawSourceHash, hash_raw_source_chunks
from riverhog_api_client.uploads import (
    configured_upload_concurrency,
    configured_upload_window,
    upload_collection_units,
)

ReadProgress = Callable[[str, int, int], None]
RangeReader = Callable[[int, int], bytes]
_STREAM_VERIFY_BLOCK_BYTES = 8 * 1024 * 1024
COLLECTION_UPLOAD_REGISTRATION_BATCH_FILES = 16


@dataclass(frozen=True, slots=True)
class ProducerFile:
    source: Path
    path: str
    provenance: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        supplied = self.source
        if supplied.is_symlink():
            raise ValueError(f"producer source must not be a symlink: {supplied}")
        resolved = supplied.resolve()
        object.__setattr__(self, "source", resolved)
        object.__setattr__(self, "path", normalize_relpath(self.path))
        if not resolved.is_file():
            raise ValueError(f"producer source must be a real regular file: {resolved}")
        if self.provenance is not None:
            object.__setattr__(self, "provenance", dict(self.provenance))


@dataclass(frozen=True, slots=True)
class ProducerStream:
    """Pre-identified range-readable producer input.

    The reader must return exactly ``size`` bytes for every valid range. The
    producer verifies the complete declared SHA-256 before registering the
    artifact with Riverhog and revalidates every block used during upload. A
    target may therefore expose a file, object, or generated random-access source
    without sharing its filesystem with the coordinator.
    """

    path: str
    bytes: int
    sha256: str
    read_range: RangeReader
    provenance: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_relpath(self.path))
        if isinstance(self.bytes, bool) or not isinstance(self.bytes, int) or self.bytes < 0:
            raise ValueError("producer stream byte count must be non-negative")
        digest = self.sha256.casefold()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("producer stream SHA-256 is invalid")
        object.__setattr__(self, "sha256", digest)
        if not callable(self.read_range):
            raise ValueError("producer stream requires a range reader")
        if self.provenance is not None:
            object.__setattr__(self, "provenance", dict(self.provenance))


ProducerInput = ProducerFile | ProducerStream


@dataclass(frozen=True, order=True, slots=True)
class ProducerArtifactIdentity:
    """Exact artifact identity established by the producer's verification pass."""

    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ProducedCollection:
    collection_id: CollectionId
    archive_root_sha256: str
    content_identity: str
    receipt: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProducerArtifactCustody:
    """Exact Riverhog safe-release receipt for a completed producer artifact."""

    artifact: ProducerArtifactIdentity
    receipt: CollectionUploadArtifactCustodyReceiptDocument


@dataclass(frozen=True, slots=True)
class _Source:
    path: str
    bytes: int
    sha256: str
    provenance: dict[str, object]
    content: builtins.bytes | None = None
    raw_parts: dict[str, object] | None = None
    raw_digest_spool: RawSourceHash | None = None
    reader: RangeReader | None = None

    def read_range(self, offset: int, size: int) -> builtins.bytes:
        if offset < 0 or size < 0 or offset + size > self.bytes:
            raise RuntimeError(f"upload unit requested an invalid source range: {self.path}")
        if self.content is not None:
            return self.content[offset : offset + size]
        if self.reader is not None:
            content = self.reader(offset, size)
            if len(content) != size:
                raise RuntimeError(f"producer source returned an incomplete range: {self.path}")
            return content
        raise RuntimeError(f"producer source has no readable content: {self.path}")

    def close(self) -> None:
        if self.raw_digest_spool is not None:
            self.raw_digest_spool.close()
        if isinstance(self.reader, _VerifiedRangeReader):
            self.reader.close()


class CollectionProducer:
    """Stream protocol-complete files into one finalized Riverhog collection.

    The calling adapter or transform target retains its source bytes until this
    method returns a finalized receipt. Reusing the same idempotency key safely
    reconciles lost responses and process restarts. The producer retains only
    the server-declared open pack window; Riverhog owns the complete membership.
    """

    def __init__(
        self,
        api: ApiClient,
        *,
        producer_app: str,
        adapter_id: str,
        adapter_version: str,
        ingest_source: str,
        archive_store: ArchiveStoreName | None = None,
        description: CollectionDescription | None = None,
        tags: Sequence[CollectionTag] = (),
        provenance_mode: Literal["captured", "omitted"] = "omitted",
        provenance_omission_reason: str = (
            "Producer did not receive host provenance; immutable producer evidence records "
            "the source boundary."
        ),
        server_generated_provenance: bool = False,
    ) -> None:
        self.api = api
        self.producer_app = producer_app
        self.adapter_id = adapter_id
        self.adapter_version = adapter_version
        self.ingest_source = ingest_source
        self.archive_store = archive_store
        self.description = description
        self.tags = tuple(tags)
        self.provenance_mode = provenance_mode
        self.server_generated_provenance = server_generated_provenance
        reason = provenance_omission_reason.strip()
        if not reason:
            raise ValueError("producer provenance omission reason must be visible")
        self.provenance_omission_reason = reason

    def publish(
        self,
        files: Iterable[ProducerFile],
        *,
        source_event_id: str,
        source_context: Mapping[str, object] | None = None,
        provenance_journals: Iterable[tuple[str, bytes]] | None = None,
        idempotency_key: str | None = None,
        event_context: Mapping[str, object] | None = None,
        poll_seconds: float = 2.0,
        timeout_seconds: float = 24 * 60 * 60,
        progress: ReadProgress | None = None,
    ) -> ProducedCollection:
        return self.publish_inputs(
            files,
            source_event_id=source_event_id,
            source_context=source_context,
            provenance_journals=provenance_journals,
            idempotency_key=idempotency_key,
            event_context=event_context,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
            progress=progress,
        )

    def publish_inputs(
        self,
        files: Iterable[ProducerInput],
        *,
        source_event_id: str,
        source_context: Mapping[str, object] | None = None,
        provenance_journals: Iterable[tuple[str, bytes]] | None = None,
        idempotency_key: str | None = None,
        event_context: Mapping[str, object] | None = None,
        poll_seconds: float = 2.0,
        timeout_seconds: float = 24 * 60 * 60,
        progress: ReadProgress | None = None,
    ) -> ProducedCollection:
        source_inputs = iter(files)
        try:
            first_input = next(source_inputs)
        except StopIteration as exc:
            raise ValueError("producer collection must contain at least one source file") from exc
        producer = IncrementalCollectionProducer(
            self.api,
            producer_app=self.producer_app,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            ingest_source=self.ingest_source,
            source_event_id=source_event_id,
            source_context=source_context,
            idempotency_key=idempotency_key,
            archive_store=self.archive_store,
            description=self.description,
            tags=self.tags,
            event_context=event_context,
            provenance_mode=(
                "captured" if self.server_generated_provenance else self.provenance_mode
            ),
            server_generated_provenance=self.server_generated_provenance,
            provenance_omission_reason=self.provenance_omission_reason,
            progress=progress,
        )
        try:
            producer.stage_provenance_journals(provenance_journals or ())
            batch: list[ProducerInput] = []
            for item in itertools.chain((first_input,), source_inputs):
                batch.append(item)
                if len(batch) == COLLECTION_UPLOAD_REGISTRATION_BATCH_FILES:
                    producer.append_inputs(batch)
                    batch.clear()
            if batch:
                producer.append_inputs(batch)
            return producer.finish(
                terminal_evidence={},
                poll_seconds=poll_seconds,
                timeout_seconds=timeout_seconds,
            )
        finally:
            producer.stop()


class IncrementalCollectionProducer:
    """Transfer finalized artifacts into one resumable Riverhog construction session.

    The producer retains readable bytes only until Riverhog returns an exact
    artifact custody receipt. Completion remains explicit and is the sole path
    that publishes an immutable collection.
    """

    def __init__(
        self,
        api: ApiClient,
        *,
        producer_app: str,
        adapter_id: str,
        adapter_version: str,
        ingest_source: str,
        source_event_id: str,
        source_context: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
        archive_store: ArchiveStoreName | None = None,
        description: CollectionDescription | None = None,
        tags: Sequence[CollectionTag] = (),
        event_context: Mapping[str, object] | None = None,
        provenance_mode: Literal["captured", "omitted"] = "omitted",
        server_generated_provenance: bool = False,
        provenance_omission_reason: str = (
            "Producer did not receive host provenance; immutable producer evidence records "
            "the source boundary."
        ),
        progress: ReadProgress | None = None,
    ) -> None:
        reason = provenance_omission_reason.strip()
        if not reason:
            raise ValueError("producer provenance omission reason must be visible")
        self.api = api
        self.progress = progress
        self.provenance_omission_reason = reason
        self.provenance_mode = provenance_mode
        self.server_generated_provenance = server_generated_provenance
        if server_generated_provenance and provenance_mode != "captured":
            raise ValueError("server-generated provenance requires captured upload mode")
        evidence = ProducerEvidence(
            producer_app=producer_app,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            source_event_id=source_event_id,
            ingest_source=ingest_source,
            source_context=cast(dict[str, JsonValue], dict(source_context or {})),
        )
        self._producer_evidence = _content_source(
            PRODUCER_EVIDENCE_PATH,
            evidence.to_json_bytes(),
            provenance={} if server_generated_provenance else _omitted(reason),
        )
        self._sources: dict[str, _Source] = {}
        self._closed = False
        self._needs_upload_scan = True
        self._heartbeat_stop = threading.Event()
        self._heartbeat_failure: BaseException | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._finalized: ProducedCollection | None = None
        self.constraints: CollectionUploadRegistrationConstraintsDocument | None
        requested_tags = tuple(tags)
        session = api.create_or_resume_collection_upload_session(
            idempotency_key or evidence.sha256,
            ingest_source=ingest_source,
            description=description,
            tags=requested_tags[:COLLECTION_TAG_REQUEST_MEMBERS_MAX],
            archive_store=archive_store,
            event_context=event_context,
            provenance_mode=provenance_mode,
            provenance_omission_reason=(reason if provenance_mode == "omitted" else None),
            custody_mode="custody-transfer",
        )
        self._heartbeat_interval_seconds = _custody_heartbeat_interval(session)
        self.resumed = bool(session.get("resumed"))
        self.collection_id = validate_collection_id(session.get("collection_id"))
        if str(session.get("state") or "") == "finalized":
            self._finalized = _finalized_receipt(session)
            self._closed = True
            self.constraints = None
            return
        for offset in range(
            COLLECTION_TAG_REQUEST_MEMBERS_MAX,
            len(requested_tags),
            COLLECTION_TAG_REQUEST_MEMBERS_MAX,
        ):
            api.add_collection_upload_session_tags(
                self.collection_id,
                requested_tags[offset : offset + COLLECTION_TAG_REQUEST_MEMBERS_MAX],
            )
        constraints = session.get("registration_constraints")
        if not isinstance(constraints, Mapping):
            raise RuntimeError("Riverhog upload session did not return registration constraints")
        self.constraints = CollectionUploadRegistrationConstraintsDocument.model_validate(
            dict(constraints)
        )
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"riverhog-upload-lease-{self.collection_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def heartbeat(self) -> None:
        if not self._closed:
            self.api.heartbeat_collection_upload_session(self.collection_id)
            self._heartbeat_failure = None

    def stage_provenance_journals(self, journals: Iterable[tuple[str, bytes]]) -> None:
        """Stage a bounded stream of exact provenance journals before sealing."""

        for journal_id, content in journals:
            self._stage_journals({str(journal_id): bytes(content)})

    def stop(self) -> None:
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._heartbeat_thread = None
        for source in self._sources.values():
            source.close()
        self._sources.clear()

    def append_inputs(
        self,
        inputs: Sequence[ProducerInput],
        *,
        provenance_journals: Mapping[str, bytes] | None = None,
        expected_identities: Mapping[str, ProducerArtifactIdentity] | None = None,
    ) -> tuple[ProducerArtifactCustody, ...]:
        if self._closed or self.constraints is None:
            raise RuntimeError("incremental collection producer is already closed")
        self._require_heartbeat()
        if not inputs:
            return ()
        supplied_paths = [item.path for item in inputs]
        if len(supplied_paths) != len(set(supplied_paths)):
            raise ValueError("incremental producer input paths must be unique")
        if any(path.startswith("riverhog/") for path in supplied_paths):
            raise ValueError("producer source files may not use the Riverhog control namespace")
        normalized_journals = {
            str(key): bytes(value) for key, value in (provenance_journals or {}).items()
        }
        expected = {
            normalize_relpath(path): identity
            for path, identity in (expected_identities or {}).items()
        }
        if expected and set(expected) != set(supplied_paths):
            raise ValueError("expected producer identities must match the supplied paths")
        self._stage_journals(normalized_journals)
        candidates: list[_Source] = []
        receipts: list[ProducerArtifactCustody] = []
        for item in inputs:
            expected_identity = expected.get(item.path)
            if self.server_generated_provenance and item.provenance is not None:
                raise ValueError(
                    "server-generated provenance cannot be mixed with producer bindings"
                )
            provenance = (
                {}
                if self.server_generated_provenance
                else _provenance(
                    item.provenance,
                    default=_omitted(self.provenance_omission_reason),
                )
            )
            if (
                self.resumed
                and expected_identity is not None
                and expected_identity.bytes <= self.constraints.pack_member_bytes
            ):
                resumed_source = _Source(
                    path=expected_identity.path,
                    bytes=expected_identity.bytes,
                    sha256=expected_identity.sha256,
                    provenance=provenance,
                )
                resumed_receipts = self._append_sources([resumed_source])
                receipts.extend(resumed_receipts)
                if resumed_receipts:
                    continue
            source = (
                _hash_local_source(
                    item,
                    provenance=provenance,
                    pack_member_bytes=self.constraints.pack_member_bytes,
                    raw_part_bytes=self.constraints.raw_part_plaintext_bytes,
                    progress=self.progress,
                )
                if isinstance(item, ProducerFile)
                else _verify_stream_source(
                    item,
                    provenance=provenance,
                    pack_member_bytes=self.constraints.pack_member_bytes,
                    raw_part_bytes=self.constraints.raw_part_plaintext_bytes,
                    progress=self.progress,
                )
            )
            if (
                expected_identity is not None
                and ProducerArtifactIdentity(
                    source.path,
                    source.bytes,
                    source.sha256,
                )
                != expected_identity
            ):
                raise ValueError(f"producer source differs from its expected identity: {item.path}")
            candidates.append(source)
        receipts.extend(self._append_sources(candidates))
        if self._needs_upload_scan:
            receipts.extend(self._upload_available())
        return tuple(receipts)

    def append_derivation_evidence(
        self, path: str, content: bytes
    ) -> ProducerArtifactCustody | None:
        """Append one bounded Riverhog derivation page after payload custody."""

        normalized = normalize_relpath(path)
        if not normalized.startswith(
            (
                f"{DERIVATION_DISPOSITION_EVIDENCE_PREFIX}/",
                f"{DERIVATION_OUTPUT_EVIDENCE_PREFIX}/",
            )
        ):
            raise ValueError("derivation evidence path is outside its reserved namespace")
        value = bytes(content)
        source = _Source(
            path=normalized,
            bytes=len(value),
            sha256=hashlib.sha256(value).hexdigest(),
            content=value,
            provenance={}
            if self.server_generated_provenance
            else _omitted(self.provenance_omission_reason),
        )
        receipts = self._append_sources([source])
        immediate = next((item for item in receipts if item.artifact.path == normalized), None)
        if immediate is not None:
            return immediate
        if self._needs_upload_scan:
            receipts = self._upload_available()
            return next((item for item in receipts if item.artifact.path == normalized), None)
        return None

    def finish(
        self,
        *,
        terminal_evidence: Mapping[str, bytes],
        provenance_journals: Mapping[str, bytes] | None = None,
        poll_seconds: float = 2.0,
        timeout_seconds: float = 24 * 60 * 60,
    ) -> ProducedCollection:
        if self._finalized is not None:
            return self._finalized
        if self._closed or self.constraints is None:
            raise RuntimeError("incremental collection producer is already closed")
        self._require_heartbeat()
        if terminal_evidence and set(terminal_evidence) != {DERIVATION_EVIDENCE_PATH}:
            raise ValueError("terminal evidence must be the exact derivation document")
        self._stage_journals(
            {str(key): bytes(value) for key, value in (provenance_journals or {}).items()}
        )
        if terminal_evidence:
            derivation = _content_source(
                DERIVATION_EVIDENCE_PATH,
                bytes(terminal_evidence[DERIVATION_EVIDENCE_PATH]),
                provenance=(
                    {}
                    if self.server_generated_provenance
                    else _omitted(self.provenance_omission_reason)
                ),
            )
            self._append_sources([derivation])
        if self._needs_upload_scan:
            self._upload_available()
        receipt = self.api.complete_collection_upload_session(self.collection_id)
        if str(receipt.get("state") or "") != "finalized":
            self._upload_available(reconcile=False)
        deadline = time.monotonic() + timeout_seconds
        while str(receipt.get("state") or "") != "finalized":
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Riverhog collection {self.collection_id} did not finalize before the timeout"
                )
            time.sleep(max(0.05, poll_seconds))
            receipt = self.api.get_collection_upload_session(self.collection_id)
        self._closed = True
        self.stop()
        self._finalized = _finalized_receipt(receipt)
        return self._finalized

    def _heartbeat_loop(self) -> None:
        api = self.api.spawn()
        try:
            retry_seconds = self._heartbeat_interval_seconds
            while not self._heartbeat_stop.wait(retry_seconds):
                try:
                    api.heartbeat_collection_upload_session(self.collection_id)
                    self._heartbeat_failure = None
                    retry_seconds = self._heartbeat_interval_seconds
                except Exception as exc:
                    self._heartbeat_failure = exc
                    retry_seconds = min(
                        10.0,
                        max(0.1, self._heartbeat_interval_seconds / 3),
                    )
        finally:
            close = getattr(api, "close", None)
            if callable(close):
                close()

    def _require_heartbeat(self) -> None:
        if self._heartbeat_failure is not None:
            try:
                self.heartbeat()
            except Exception as exc:
                raise RuntimeError("collection upload custody lease heartbeat failed") from exc

    def _append_sources(self, values: Sequence[_Source]) -> tuple[ProducerArtifactCustody, ...]:
        candidates = list(values)
        if all(source.path != PRODUCER_EVIDENCE_PATH for source in candidates):
            candidates.append(self._producer_evidence)
        if len({source.path for source in candidates}) != len(candidates):
            raise ValueError("incremental producer source paths must be unique")
        for source in candidates:
            existing = self._sources.get(source.path)
            if existing is not None and _registered_identity(existing) != _registered_identity(
                source
            ):
                raise RuntimeError(f"resumed producer artifact identity changed: {source.path}")
            self._sources[source.path] = source
        registration = [_source_registration(source) for source in candidates]
        constraints = self.constraints
        if constraints is None:
            raise RuntimeError("incremental collection producer has no registration constraints")
        receipts: list[ProducerArtifactCustody] = []
        for start in range(0, len(registration), COLLECTION_UPLOAD_REGISTRATION_BATCH_FILES):
            source_batch = candidates[start : start + COLLECTION_UPLOAD_REGISTRATION_BATCH_FILES]
            registered = self.api.register_collection_upload_session_files(
                self.collection_id,
                registration[start : start + COLLECTION_UPLOAD_REGISTRATION_BATCH_FILES],
                registration_constraints=constraints,
            )
            for source in source_batch:
                _register_source_raw_digests(self.api, self.collection_id, source)
            rows = registered.get("files")
            if not isinstance(rows, list):
                raise RuntimeError("Riverhog returned invalid registered files")
            receipts.extend(self._accept_registered_rows(iter(rows), expected=source_batch))
            volumes = registered.get("volumes")
            if isinstance(volumes, list) and volumes:
                self._needs_upload_scan = True
        return tuple(receipts)

    def _upload_available(self, *, reconcile: bool = True) -> tuple[ProducerArtifactCustody, ...]:
        concurrency = configured_upload_concurrency()

        def content_for_unit(unit: CollectionUploadUnitWorkDocument) -> bytes:
            chunks: list[bytes] = []
            for row in unit.sources:
                source = self._sources.get(row.path)
                if source is None:
                    raise RuntimeError(f"uncustodied producer source is unavailable: {row.path}")
                if row.artifact_sha256 != source.sha256:
                    raise RuntimeError(
                        f"Riverhog requested a changed producer artifact: {row.path}"
                    )
                chunks.append(source.read_range(row.offset, row.bytes))
            return b"".join(chunks)

        upload_collection_units(
            self.api,
            self.collection_id,
            content_for_unit=content_for_unit,
            concurrency=concurrency,
            window=configured_upload_window(concurrency=concurrency),
            client_factory=self.api.spawn,
        )
        receipts = self._reconcile_pending_sources() if reconcile else ()
        self._needs_upload_scan = False
        return receipts

    def _reconcile_pending_sources(self) -> tuple[ProducerArtifactCustody, ...]:
        receipts: list[ProducerArtifactCustody] = []
        constraints = self.constraints
        if constraints is None:
            raise RuntimeError("incremental collection producer has no registration constraints")
        pending = list(self._sources.values())
        for start in range(0, len(pending), COLLECTION_UPLOAD_REGISTRATION_BATCH_FILES):
            source_batch = pending[start : start + COLLECTION_UPLOAD_REGISTRATION_BATCH_FILES]
            payload = self.api.register_collection_upload_session_files(
                self.collection_id,
                [_source_registration(source) for source in source_batch],
                registration_constraints=constraints,
            )
            rows = payload.get("files")
            if not isinstance(rows, list):
                raise RuntimeError("Riverhog returned invalid registered files")
            receipts.extend(self._accept_registered_rows(iter(rows), expected=source_batch))
        return tuple(receipts)

    def _accept_registered_rows(
        self,
        rows: Iterator[dict[str, Any]],
        *,
        expected: Sequence[_Source],
    ) -> tuple[ProducerArtifactCustody, ...]:
        expected_by_path = {source.path: source for source in expected}
        receipts: list[ProducerArtifactCustody] = []
        for row in rows:
            provenance = row.get("provenance")
            if provenance is None and self.server_generated_provenance:
                normalized_provenance: dict[str, Any] = {}
            elif isinstance(provenance, Mapping):
                normalized_provenance = dict(provenance)
            else:
                raise RuntimeError("Riverhog upload file has no provenance binding")
            source = _Source(
                path=str(row.get("path") or ""),
                bytes=int(row.get("bytes") or 0),
                sha256=str(row.get("sha256") or ""),
                provenance=normalized_provenance,
            )
            expected_source = expected_by_path.pop(source.path, None)
            if expected_source is None:
                raise RuntimeError(f"Riverhog returned an unexpected artifact: {source.path}")
            if _registered_identity(expected_source) != _registered_identity(source):
                raise RuntimeError(f"Riverhog changed a registered artifact: {source.path}")
            receipt_value = row.get("custody_receipt")
            if receipt_value is not None:
                receipt = CollectionUploadArtifactCustodyReceiptDocument.model_validate(
                    receipt_value
                )
                validate_collection_upload_artifact_custody_receipt(
                    self.collection_id,
                    ImmutableFileIdentityDocument(
                        path=source.path,
                        bytes=source.bytes,
                        sha256=source.sha256,
                    ),
                    receipt,
                )
                receipts.append(
                    ProducerArtifactCustody(
                        artifact=ProducerArtifactIdentity(source.path, source.bytes, source.sha256),
                        receipt=receipt,
                    )
                )
                owned = self._sources.pop(source.path, None)
                if owned is not None:
                    owned.close()
        if expected_by_path:
            raise RuntimeError("Riverhog omitted requested registered artifacts")
        return tuple(receipts)

    def _stage_journals(self, values: Mapping[str, bytes]) -> None:
        for journal_id, content in sorted(values.items()):
            self.api.upload_collection_upload_session_provenance_journal(
                self.collection_id,
                journal_id,
                content=(content,),
                byte_count=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )


def _hash_local_source(
    item: ProducerFile,
    *,
    provenance: dict[str, object],
    pack_member_bytes: int,
    raw_part_bytes: int,
    progress: ReadProgress | None,
) -> _Source:
    observed = item.source.stat()
    expected = observed.st_size
    offset = 0
    block_sha256s = _DigestSpool()

    def read_local(start: int, size: int) -> bytes:
        before = item.source.stat()
        _require_same_file(before, observed, path=item.path)
        with item.source.open("rb") as stream:
            stream.seek(start)
            content = stream.read(size)
        after = item.source.stat()
        _require_same_file(after, observed, path=item.path)
        if len(content) != size:
            raise RuntimeError(f"producer source returned an incomplete range: {item.path}")
        return content

    def chunks() -> Iterator[bytes]:
        nonlocal offset
        while offset < expected:
            size = min(_STREAM_VERIFY_BLOCK_BYTES, expected - offset)
            chunk = read_local(offset, size)
            block_sha256s.append(hashlib.sha256(chunk).digest())
            offset += size
            if progress is not None:
                progress(item.path, offset, expected)
            yield chunk

    if expected >= pack_member_bytes:
        manifest = hash_raw_source_chunks(
            path=item.path,
            chunks=chunks(),
            expected_bytes=expected,
            part_plaintext_bytes=raw_part_bytes,
        )
        sha256 = manifest.summary.sha256
        raw_parts: dict[str, object] | None = {
            "part_plaintext_bytes": manifest.summary.part_plaintext_bytes,
            "part_count": manifest.summary.part_count,
            "ordered_sha256": manifest.summary.ordered_part_sha256,
        }
        raw_digest_spool: RawSourceHash | None = manifest
    else:
        digest = hashlib.sha256()
        for chunk in chunks():
            digest.update(chunk)
        sha256 = digest.hexdigest()
        raw_parts = None
        raw_digest_spool = None
    _require_same_file(item.source.stat(), observed, path=item.path)
    return _Source(
        path=item.path,
        bytes=expected,
        sha256=sha256,
        provenance=provenance,
        raw_parts=raw_parts,
        raw_digest_spool=raw_digest_spool,
        reader=_VerifiedRangeReader(
            source=read_local,
            path=item.path,
            bytes=expected,
            block_sha256s=block_sha256s,
        ),
    )


def _require_same_file(current: object, expected: object, *, path: str) -> None:
    for attribute in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
        if getattr(current, attribute) != getattr(expected, attribute):
            raise RuntimeError(f"producer source changed during upload verification: {path}")


def _verify_stream_source(
    item: ProducerStream,
    *,
    provenance: dict[str, object],
    pack_member_bytes: int,
    raw_part_bytes: int,
    progress: ReadProgress | None,
) -> _Source:
    offset = 0
    block_sha256s = _DigestSpool()

    def chunks() -> Iterator[bytes]:
        nonlocal offset
        while offset < item.bytes:
            size = min(_STREAM_VERIFY_BLOCK_BYTES, item.bytes - offset)
            chunk = item.read_range(offset, size)
            if len(chunk) != size:
                raise RuntimeError(f"producer stream returned an incomplete range: {item.path}")
            block_sha256s.append(hashlib.sha256(chunk).digest())
            offset += size
            if progress is not None:
                progress(item.path, offset, item.bytes)
            yield chunk

    if item.bytes >= pack_member_bytes:
        manifest = hash_raw_source_chunks(
            path=item.path,
            chunks=chunks(),
            expected_bytes=item.bytes,
            part_plaintext_bytes=raw_part_bytes,
        )
        sha256 = manifest.summary.sha256
        raw_parts: dict[str, object] | None = {
            "part_plaintext_bytes": manifest.summary.part_plaintext_bytes,
            "part_count": manifest.summary.part_count,
            "ordered_sha256": manifest.summary.ordered_part_sha256,
        }
        raw_digest_spool: RawSourceHash | None = manifest
    else:
        digest = hashlib.sha256()
        for chunk in chunks():
            digest.update(chunk)
        sha256 = digest.hexdigest()
        raw_parts = None
        raw_digest_spool = None
    if sha256 != item.sha256:
        raise RuntimeError(f"producer stream identity changed before upload: {item.path}")
    verified_reader = _VerifiedRangeReader(
        source=item.read_range,
        path=item.path,
        bytes=item.bytes,
        block_sha256s=block_sha256s,
    )
    return _Source(
        path=item.path,
        bytes=item.bytes,
        sha256=item.sha256,
        provenance=provenance,
        raw_parts=raw_parts,
        raw_digest_spool=raw_digest_spool,
        reader=verified_reader,
    )


@dataclass(slots=True)
class _DigestSpool:
    """Disk-backed verification digests; source plaintext is never spooled."""

    _values: BinaryIO = dataclass_field(default_factory=lambda: tempfile.TemporaryFile(mode="w+b"))
    _count: int = 0
    _lock: threading.Lock = dataclass_field(default_factory=threading.Lock)

    def append(self, digest: bytes) -> None:
        if len(digest) != 32:
            raise ValueError("verification digest must be SHA-256")
        with self._lock:
            self._values.seek(self._count * 32)
            self._values.write(digest)
            self._count += 1

    def get(self, index: int) -> bytes:
        if index < 0 or index >= self._count:
            raise RuntimeError("producer verification digest is unavailable")
        with self._lock:
            self._values.seek(index * 32)
            value = self._values.read(32)
        if len(value) != 32:
            raise RuntimeError("producer verification digest spool is incomplete")
        return value

    def close(self) -> None:
        self._values.close()

    def __del__(self) -> None:
        self._values.close()


@dataclass(frozen=True, slots=True)
class _VerifiedRangeReader:
    source: RangeReader
    path: str
    bytes: int
    block_sha256s: _DigestSpool

    def __call__(self, offset: int, size: int) -> builtins.bytes:
        if offset < 0 or size < 0 or offset + size > self.bytes:
            raise RuntimeError(f"producer source requested an invalid range: {self.path}")
        if size == 0:
            return b""
        first = offset // _STREAM_VERIFY_BLOCK_BYTES
        last = (offset + size - 1) // _STREAM_VERIFY_BLOCK_BYTES
        chunks: list[builtins.bytes] = []
        for block in range(first, last + 1):
            block_offset = block * _STREAM_VERIFY_BLOCK_BYTES
            block_size = min(_STREAM_VERIFY_BLOCK_BYTES, self.bytes - block_offset)
            content = self.source(block_offset, block_size)
            if len(content) != block_size:
                raise RuntimeError(
                    f"producer stream returned an incomplete verified block: {self.path}"
                )
            if hashlib.sha256(content).digest() != self.block_sha256s.get(block):
                raise RuntimeError(
                    f"producer source changed during upload verification: {self.path}"
                )
            chunks.append(content)
        combined = b"".join(chunks)
        relative = offset - first * _STREAM_VERIFY_BLOCK_BYTES
        return combined[relative : relative + size]

    def close(self) -> None:
        self.block_sha256s.close()


def _content_source(
    path: str,
    content: bytes,
    *,
    provenance: dict[str, object],
) -> _Source:
    value = bytes(content)
    return _Source(
        path=normalize_relpath(path),
        bytes=len(value),
        sha256=hashlib.sha256(value).hexdigest(),
        provenance=dict(provenance),
        content=value,
    )


def _source_identity(source: _Source) -> tuple[str, int, str]:
    return source.path, source.bytes, source.sha256


def _registered_identity(source: _Source) -> tuple[str, int, str, str]:
    return (*_source_identity(source), repr(sorted(source.provenance.items())))


def _source_registration(source: _Source) -> dict[str, object]:
    return {
        "path": source.path,
        "bytes": source.bytes,
        "sha256": source.sha256,
        **({"raw_parts": source.raw_parts} if source.raw_parts is not None else {}),
        **({"provenance": source.provenance} if source.provenance else {}),
    }


def _register_source_raw_digests(
    api: ApiClient,
    collection_id: CollectionId,
    source: _Source,
) -> None:
    spool = source.raw_digest_spool
    if spool is None:
        return
    for first_part, sha256s in spool.iter_batches():
        api.register_collection_upload_session_raw_part_digests(
            collection_id,
            {
                "path": source.path,
                "first_part": first_part,
                "sha256s": list(sha256s),
            },
        )


def _omitted(reason: str) -> dict[str, object]:
    return {"status": "omitted", "omission_reason": reason}


def _provenance(
    value: Mapping[str, object] | None,
    *,
    default: dict[str, object],
) -> dict[str, object]:
    if value is None:
        return dict(default)
    status = str(value.get("status") or "")
    if status == "captured" and set(value) == {"status", "journal_id", "current_state_id"}:
        journal_id = str(value.get("journal_id") or "")
        current_state_id = str(value.get("current_state_id") or "")
        if journal_id and current_state_id:
            return {
                "status": "captured",
                "journal_id": journal_id,
                "current_state_id": current_state_id,
            }
    if status == "omitted" and set(value) == {"status", "omission_reason"}:
        reason = str(value.get("omission_reason") or "")
        if reason and reason == reason.strip():
            return {"status": "omitted", "omission_reason": reason}
    raise ValueError("producer file provenance binding is invalid")


def _finalized_receipt(payload: Mapping[str, Any]) -> ProducedCollection:
    collection = payload.get("collection")
    if not isinstance(collection, Mapping):
        raise RuntimeError("finalized Riverhog upload has no collection receipt")
    collection_id = int(collection["id"])
    content_identity = str(
        payload.get("content_identity") or collection.get("content_identity") or ""
    )
    archive_root_sha256 = str(
        collection.get("archive_root_sha256") or payload.get("archive_root_sha256") or ""
    )
    if len(archive_root_sha256) != 64:
        raise RuntimeError("finalized Riverhog receipt has no immutable archive-root identity")
    if len(content_identity) != 64:
        raise RuntimeError("finalized Riverhog receipt has no content identity")
    return ProducedCollection(
        collection_id=collection_id,
        archive_root_sha256=archive_root_sha256,
        content_identity=content_identity,
        receipt=dict(payload),
    )


def _custody_heartbeat_interval(session: Mapping[str, object]) -> float:
    value = session.get("upload_state_expires_at")
    if not isinstance(value, str) or not value:
        return 60.0
    try:
        expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("Riverhog upload session returned an invalid custody expiry") from exc
    if expires.tzinfo is None:
        raise RuntimeError("Riverhog upload session returned a naive custody expiry")
    remaining = (expires.astimezone(UTC) - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        return 0.1
    return min(60.0, max(0.1, remaining / 3))


__all__ = [
    "CollectionProducer",
    "IncrementalCollectionProducer",
    "ProducedCollection",
    "ProducerFile",
    "ProducerArtifactIdentity",
    "ProducerArtifactCustody",
    "ProducerInput",
    "ProducerStream",
    "RangeReader",
]
