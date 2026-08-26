"""Reusable content-opaque direct-to-final Riverhog collection producer."""

from __future__ import annotations

import builtins
import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from riverhog_protocol.collection_upload_transport import (
    CollectionUploadArtifactCustodyReceiptDocument,
    CollectionUploadRegistrationConstraintsDocument,
    collection_upload_path_order_key,
    validate_collection_upload_artifact_custody_receipt,
)
from riverhog_protocol.collection_workflows import (
    DERIVATION_EVIDENCE_PATH,
    PRODUCER_EVIDENCE_PATH,
    JsonValue,
    ProducerEvidence,
)
from riverhog_protocol.file_identity import ImmutableFileIdentityDocument
from riverhog_protocol.manifest import collection_content_identity_ordered
from riverhog_protocol.paths import CollectionId, normalize_relpath, validate_collection_id
from riverhog_protocol.raw_ingress import hash_raw_source
from riverhog_protocol.storage_names import ArchiveStoreName
from riverhog_provenance import FileProvenanceBinding, build_provenance_archive

from riverhog_api_client.client import ApiClient
from riverhog_api_client.uploads import (
    configured_upload_concurrency,
    configured_upload_window,
    upload_collection_units,
)

ReadProgress = Callable[[str, int, int], None]
RangeReader = Callable[[int, int], bytes]
_STREAM_VERIFY_BLOCK_BYTES = 8 * 1024 * 1024
COLLECTION_UPLOAD_REGISTRATION_BATCH_FILES = 16
ProvenanceStatus = Literal["captured", "omitted"]


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
class ProducerProvenance:
    """Bindings and journals derived after exact producer identities are known."""

    bindings: Mapping[str, Mapping[str, object]]
    journals: Mapping[str, bytes]


ProvenanceBuilder = Callable[
    [CollectionId, bool, tuple[ProducerArtifactIdentity, ...]],
    ProducerProvenance,
]


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


class CollectionProducer:
    """Commit protocol-complete files to one finalized Riverhog collection.

    The calling adapter or transform target retains its source bytes until this
    method returns a finalized receipt. Reusing the same idempotency key safely
    reconciles lost responses and process restarts.
    """

    def __init__(
        self,
        api: ApiClient,
        *,
        producer_app: str,
        adapter_id: str,
        adapter_version: str,
        ingest_source: str,
        tags: Sequence[str],
        archive_store: ArchiveStoreName | None = None,
        provenance_omission_reason: str = (
            "Producer did not receive host provenance; immutable producer evidence records "
            "the source boundary."
        ),
    ) -> None:
        self.api = api
        self.producer_app = producer_app
        self.adapter_id = adapter_id
        self.adapter_version = adapter_version
        self.ingest_source = ingest_source
        self.tags = tuple(tags)
        self.archive_store = archive_store
        reason = provenance_omission_reason.strip()
        if not reason:
            raise ValueError("producer provenance omission reason must be visible")
        self.provenance_omission_reason = reason

    def publish(
        self,
        files: Sequence[ProducerFile],
        *,
        source_event_id: str,
        source_context: Mapping[str, object] | None = None,
        inline_evidence: Mapping[str, bytes] | None = None,
        provenance_journals: Mapping[str, bytes] | None = None,
        provenance_builder: ProvenanceBuilder | None = None,
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
            inline_evidence=inline_evidence,
            provenance_journals=provenance_journals,
            provenance_builder=provenance_builder,
            idempotency_key=idempotency_key,
            event_context=event_context,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
            progress=progress,
        )

    def publish_inputs(
        self,
        files: Sequence[ProducerInput],
        *,
        source_event_id: str,
        source_context: Mapping[str, object] | None = None,
        inline_evidence: Mapping[str, bytes] | None = None,
        provenance_journals: Mapping[str, bytes] | None = None,
        provenance_builder: ProvenanceBuilder | None = None,
        idempotency_key: str | None = None,
        event_context: Mapping[str, object] | None = None,
        poll_seconds: float = 2.0,
        timeout_seconds: float = 24 * 60 * 60,
        progress: ReadProgress | None = None,
    ) -> ProducedCollection:
        if not files and not inline_evidence:
            raise ValueError("producer collection must contain at least one source file")
        evidence = ProducerEvidence(
            producer_app=self.producer_app,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            source_event_id=source_event_id,
            ingest_source=self.ingest_source,
            source_context=cast(dict[str, JsonValue], dict(source_context or {})),
        )
        key = idempotency_key or evidence.sha256
        omitted = _omitted(self.provenance_omission_reason)
        source_paths = [item.path for item in files]
        if any(path.startswith("riverhog/") for path in source_paths):
            raise ValueError("producer source files may not use the Riverhog control namespace")
        evidence_paths = [normalize_relpath(path) for path in (inline_evidence or {})]
        if any(not path.startswith("riverhog/") for path in evidence_paths):
            raise ValueError("inline evidence must use the Riverhog control namespace")
        paths = [PRODUCER_EVIDENCE_PATH, *source_paths, *evidence_paths]
        if len(paths) != len(set(paths)):
            raise ValueError("producer collection paths must be unique")

        normalized_journals = {
            str(key): bytes(value) for key, value in (provenance_journals or {}).items()
        }
        source_provenance = [_provenance(item.provenance, default=omitted) for item in files]
        captured = (
            provenance_builder is not None
            or bool(normalized_journals)
            or any(current.get("status") == "captured" for current in source_provenance)
        )
        session = self.api.create_or_resume_collection_upload_session(
            key,
            self.tags,
            ingest_source=self.ingest_source,
            archive_store=self.archive_store,
            event_context=event_context,
            provenance_mode="captured" if captured else "omitted",
            provenance_omission_reason=None if captured else self.provenance_omission_reason,
        )
        collection_id = validate_collection_id(session.get("collection_id"))
        if str(session.get("state") or "") == "finalized":
            return _finalized_receipt(session)
        constraints = session.get("registration_constraints")
        if not isinstance(constraints, Mapping):
            raise RuntimeError("Riverhog upload session did not return registration constraints")
        constraints_document = CollectionUploadRegistrationConstraintsDocument.model_validate(
            dict(constraints)
        )
        pack_member_bytes = constraints_document.pack_member_bytes
        raw_part_bytes = constraints_document.raw_part_plaintext_bytes

        sources: dict[str, _Source] = {}
        evidence_bytes = evidence.to_json_bytes()
        sources[PRODUCER_EVIDENCE_PATH] = _Source(
            path=PRODUCER_EVIDENCE_PATH,
            bytes=len(evidence_bytes),
            sha256=evidence.sha256,
            content=evidence_bytes,
            provenance=omitted,
        )
        for item, provenance in sorted(
            zip(files, source_provenance, strict=True),
            key=lambda current: current[0].path.encode("utf-8"),
        ):
            if isinstance(item, ProducerFile):
                source = _hash_local_source(
                    item,
                    provenance=provenance,
                    pack_member_bytes=pack_member_bytes,
                    raw_part_bytes=raw_part_bytes,
                    progress=progress,
                )
            else:
                source = _verify_stream_source(
                    item,
                    provenance=provenance,
                    pack_member_bytes=pack_member_bytes,
                    raw_part_bytes=raw_part_bytes,
                    progress=progress,
                )
            sources[item.path] = source
        for path, content in sorted((inline_evidence or {}).items()):
            normalized = normalize_relpath(path)
            value = bytes(content)
            sources[normalized] = _Source(
                path=normalized,
                bytes=len(value),
                sha256=hashlib.sha256(value).hexdigest(),
                content=value,
                provenance=omitted,
            )

        if provenance_builder is not None:
            built = provenance_builder(
                collection_id,
                bool(session.get("resumed")),
                tuple(
                    ProducerArtifactIdentity(
                        path=sources[path].path,
                        bytes=sources[path].bytes,
                        sha256=sources[path].sha256,
                    )
                    for path in sorted(source_paths, key=lambda current: current.encode("utf-8"))
                ),
            )
            supplied_by_path = {
                item.path: item.provenance for item in files if item.provenance is not None
            }
            for path, binding in built.bindings.items():
                current_source = sources.get(path)
                if current_source is None or path not in source_paths:
                    raise ValueError(
                        f"producer provenance builder returned an unknown source path: {path}"
                    )
                normalized_binding = _provenance(binding, default=omitted)
                supplied = supplied_by_path.get(path)
                if supplied is not None and _provenance(supplied, default=omitted) != (
                    normalized_binding
                ):
                    raise ValueError(
                        f"producer provenance builder conflicts with source binding: {path}"
                    )
                sources[path] = replace(current_source, provenance=normalized_binding)
            for journal_id, content in built.journals.items():
                normalized_id = str(journal_id)
                value = bytes(content)
                existing = normalized_journals.get(normalized_id)
                if existing is not None and existing != value:
                    raise ValueError(
                        f"producer provenance builder conflicts with journal: {normalized_id}"
                    )
                normalized_journals[normalized_id] = value

        registration = [
            {
                "path": source.path,
                "bytes": source.bytes,
                "sha256": source.sha256,
                **({"raw_parts": source.raw_parts} if source.raw_parts is not None else {}),
                "provenance": source.provenance,
            }
            for source in _ordered_sources(sources)
        ]
        for journal_id, content in sorted(normalized_journals.items()):
            self.api.put_collection_upload_session_provenance_journal(
                collection_id,
                journal_id,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            )
        for start in range(0, len(registration), COLLECTION_UPLOAD_REGISTRATION_BATCH_FILES):
            self.api.register_collection_upload_session_files(
                collection_id,
                registration[start : start + COLLECTION_UPLOAD_REGISTRATION_BATCH_FILES],
                registration_constraints=constraints_document,
            )

        def content_for_unit(unit: Mapping[str, object]) -> bytes:
            rows = unit.get("sources")
            if not isinstance(rows, list):
                raise RuntimeError("Riverhog upload unit has no source rows")
            chunks: list[bytes] = []
            for row in rows:
                if not isinstance(row, Mapping):
                    raise RuntimeError("Riverhog upload unit source is invalid")
                source_path = str(row.get("path") or "")
                source = sources.get(source_path)
                if source is None:
                    raise RuntimeError(
                        f"Riverhog requested an unknown producer path: {source_path}"
                    )
                chunks.append(
                    source.read_range(int(row.get("offset") or 0), int(row.get("bytes") or 0))
                )
            return b"".join(chunks)

        concurrency = configured_upload_concurrency()
        upload_collection_units(
            self.api,
            collection_id,
            content_for_unit=content_for_unit,
            concurrency=concurrency,
            window=configured_upload_window(concurrency=concurrency),
            client_factory=self.api.spawn,
        )
        ordered = _ordered_sources(sources)
        content_identity = collection_content_identity_ordered(
            (source.path, source.bytes, source.sha256) for source in ordered
        )
        provenance_identity = (
            _provenance_identity(ordered, normalized_journals) if captured else None
        )
        receipt = self.api.complete_collection_upload_session(
            collection_id,
            files_total=len(sources),
            content_identity=content_identity,
            provenance_identity=provenance_identity,
        )
        if str(receipt.get("state") or "") != "finalized":
            # Closing discovery seals the bounded final pack. Upload any units
            # that could not exist during the incremental pre-close pass.
            upload_collection_units(
                self.api,
                collection_id,
                content_for_unit=content_for_unit,
                concurrency=concurrency,
                window=configured_upload_window(concurrency=concurrency),
                client_factory=self.api.spawn,
            )
        deadline = time.monotonic() + timeout_seconds
        while str(receipt.get("state") or "") != "finalized":
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Riverhog collection {collection_id} did not finalize before the timeout"
                )
            time.sleep(max(0.05, poll_seconds))
            receipt = self.api.get_collection_upload_session(collection_id)
        return _finalized_receipt(receipt)


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
        tags: Sequence[str],
        source_event_id: str,
        source_context: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
        archive_store: ArchiveStoreName | None = None,
        event_context: Mapping[str, object] | None = None,
        provenance_mode: Literal["captured", "omitted"] = "omitted",
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
            provenance=_omitted(reason),
        )
        self._journals: dict[str, bytes] = {}
        self._sources: dict[str, _Source] = {}
        self._registered: dict[str, _Source] = {}
        self._custody: dict[str, ProducerArtifactCustody] = {}
        self._closed = False
        self._needs_upload_scan = True
        self._heartbeat_stop = threading.Event()
        self._heartbeat_failure: BaseException | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._finalized: ProducedCollection | None = None
        self.constraints: CollectionUploadRegistrationConstraintsDocument | None
        session = api.create_or_resume_collection_upload_session(
            idempotency_key or evidence.sha256,
            tags,
            ingest_source=ingest_source,
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
        constraints = session.get("registration_constraints")
        if not isinstance(constraints, Mapping):
            raise RuntimeError("Riverhog upload session did not return registration constraints")
        self.constraints = CollectionUploadRegistrationConstraintsDocument.model_validate(
            dict(constraints)
        )
        self._refresh_registered()
        if (
            PRODUCER_EVIDENCE_PATH in self._registered
            and PRODUCER_EVIDENCE_PATH not in self._custody
        ):
            expected = self._registered[PRODUCER_EVIDENCE_PATH]
            if _source_identity(expected) != _source_identity(self._producer_evidence):
                raise RuntimeError("resumed producer evidence identity changed")
            self._sources[PRODUCER_EVIDENCE_PATH] = self._producer_evidence
        self._load_staged_journals()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"riverhog-upload-lease-{self.collection_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    @property
    def custody_receipts(self) -> Mapping[str, ProducerArtifactCustody]:
        return dict(self._custody)

    @property
    def registered_artifacts(self) -> tuple[ProducerArtifactIdentity, ...]:
        return tuple(
            ProducerArtifactIdentity(source.path, source.bytes, source.sha256)
            for source in sorted(
                self._registered.values(),
                key=lambda current: collection_upload_path_order_key(current.path),
            )
        )

    def is_registered(self, path: str) -> bool:
        return normalize_relpath(path) in self._registered

    def heartbeat(self) -> None:
        if not self._closed:
            self.api.heartbeat_collection_upload_session(self.collection_id)
            self._heartbeat_failure = None

    def stop(self) -> None:
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._heartbeat_thread = None

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
        before = set(self._custody)
        candidates: list[_Source] = []
        for item in sorted(
            inputs, key=lambda current: collection_upload_path_order_key(current.path)
        ):
            existing = self._registered.get(item.path)
            expected_identity = expected.get(item.path)
            if (
                existing is not None
                and expected_identity is not None
                and ProducerArtifactIdentity(
                    existing.path,
                    existing.bytes,
                    existing.sha256,
                )
                != expected_identity
            ):
                raise ValueError(
                    f"registered producer artifact differs from its expected identity: {item.path}"
                )
            if existing is not None and item.path in self._custody:
                continue
            provenance = _provenance(
                item.provenance,
                default=_omitted(self.provenance_omission_reason),
            )
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
            if existing is not None and _registered_identity(existing) != _registered_identity(
                source
            ):
                raise RuntimeError(f"resumed producer artifact identity changed: {item.path}")
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
        self._append_sources(candidates)
        if self._needs_upload_scan:
            self._upload_available()
        return tuple(self._custody[path] for path in sorted(set(self._custody) - before))

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
        if set(terminal_evidence) != {DERIVATION_EVIDENCE_PATH}:
            raise ValueError("incremental transform completion requires exact derivation evidence")
        self._stage_journals(
            {str(key): bytes(value) for key, value in (provenance_journals or {}).items()}
        )
        derivation = _content_source(
            DERIVATION_EVIDENCE_PATH,
            bytes(terminal_evidence[DERIVATION_EVIDENCE_PATH]),
            provenance=_omitted(self.provenance_omission_reason),
        )
        self._append_sources([derivation], terminal=True)
        if self._needs_upload_scan:
            self._upload_available()
        ordered = sorted(
            self._registered.values(),
            key=lambda current: collection_upload_path_order_key(current.path),
        )
        content_identity = collection_content_identity_ordered(
            (source.path, source.bytes, source.sha256) for source in ordered
        )
        captured = any(source.provenance.get("status") == "captured" for source in ordered)
        provenance_identity = _provenance_identity(ordered, self._journals) if captured else None
        receipt = self.api.complete_collection_upload_session(
            self.collection_id,
            files_total=len(ordered),
            content_identity=content_identity,
            provenance_identity=provenance_identity,
        )
        if str(receipt.get("state") or "") != "finalized":
            self._upload_available()
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
        self._sources.clear()
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

    def _append_sources(self, values: Sequence[_Source], *, terminal: bool = False) -> None:
        candidates = list(values)
        if PRODUCER_EVIDENCE_PATH not in self._registered:
            first_key = collection_upload_path_order_key(candidates[0].path) if candidates else None
            if terminal or (
                first_key is not None
                and collection_upload_path_order_key(PRODUCER_EVIDENCE_PATH) <= first_key
            ):
                candidates.append(self._producer_evidence)
        candidates.sort(key=lambda current: collection_upload_path_order_key(current.path))
        existing_paths = set(self._registered)
        new = [source for source in candidates if source.path not in existing_paths]
        if not new:
            for source in candidates:
                if source.path not in self._custody:
                    self._sources[source.path] = source
            return
        if self._registered:
            frontier = max(self._registered, key=collection_upload_path_order_key)
            if collection_upload_path_order_key(new[0].path) <= collection_upload_path_order_key(
                frontier
            ):
                raise ValueError(
                    f"incremental producer path is behind the sealed append frontier: {new[0].path}"
                )
        registration = [_source_registration(source) for source in new]
        constraints = self.constraints
        if constraints is None:
            raise RuntimeError("incremental collection producer has no registration constraints")
        for start in range(0, len(registration), COLLECTION_UPLOAD_REGISTRATION_BATCH_FILES):
            registered = self.api.register_collection_upload_session_files(
                self.collection_id,
                registration[start : start + COLLECTION_UPLOAD_REGISTRATION_BATCH_FILES],
                registration_constraints=constraints,
            )
            volumes = registered.get("volumes")
            if isinstance(volumes, list) and volumes:
                self._needs_upload_scan = True
        for source in candidates:
            self._registered[source.path] = source
            if source.path not in self._custody:
                self._sources[source.path] = source

    def _upload_available(self) -> None:
        concurrency = configured_upload_concurrency()

        def content_for_unit(unit: Mapping[str, object]) -> bytes:
            rows = unit.get("sources")
            if not isinstance(rows, list):
                raise RuntimeError("Riverhog upload unit has no source rows")
            chunks: list[bytes] = []
            for row in rows:
                if not isinstance(row, Mapping):
                    raise RuntimeError("Riverhog upload unit source is invalid")
                path = str(row.get("path") or "")
                source = self._sources.get(path)
                if source is None:
                    raise RuntimeError(f"uncustodied producer source is unavailable: {path}")
                chunks.append(
                    source.read_range(int(row.get("offset") or 0), int(row.get("bytes") or 0))
                )
            return b"".join(chunks)

        upload_collection_units(
            self.api,
            self.collection_id,
            content_for_unit=content_for_unit,
            concurrency=concurrency,
            window=configured_upload_window(concurrency=concurrency),
            client_factory=self.api.spawn,
        )
        self._refresh_registered()
        self._needs_upload_scan = False

    def _refresh_registered(self) -> None:
        payload = self.api.list_collection_upload_session_files(
            self.collection_id,
            all_items=True,
        )
        rows = payload.get("files")
        if not isinstance(rows, list):
            raise RuntimeError("Riverhog upload session returned no registered file inventory")
        for row in rows:
            if not isinstance(row, Mapping):
                raise RuntimeError("Riverhog upload session returned an invalid file row")
            provenance = row.get("provenance")
            if not isinstance(provenance, Mapping):
                raise RuntimeError("Riverhog upload file has no provenance binding")
            source = _Source(
                path=str(row.get("path") or ""),
                bytes=int(row.get("bytes") or 0),
                sha256=str(row.get("sha256") or ""),
                provenance=dict(provenance),
            )
            existing = self._registered.get(source.path)
            if existing is not None and _registered_identity(existing) != _registered_identity(
                source
            ):
                raise RuntimeError(f"Riverhog changed a registered artifact: {source.path}")
            self._registered[source.path] = source if existing is None else existing
            receipt_value = row.get("custody_receipt")
            if receipt_value is not None:
                receipt = CollectionUploadArtifactCustodyReceiptDocument.model_validate_json(
                    json.dumps(receipt_value, sort_keys=True, separators=(",", ":"))
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
                self._custody[source.path] = ProducerArtifactCustody(
                    artifact=ProducerArtifactIdentity(source.path, source.bytes, source.sha256),
                    receipt=receipt,
                )
                self._sources.pop(source.path, None)

    def _stage_journals(self, values: Mapping[str, bytes]) -> None:
        for journal_id, content in sorted(values.items()):
            existing = self._journals.get(journal_id)
            if existing is not None:
                if existing != content:
                    raise ValueError(f"producer provenance journal identity changed: {journal_id}")
                continue
            self.api.put_collection_upload_session_provenance_journal(
                self.collection_id,
                journal_id,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            )
            self._journals[journal_id] = content

    def _load_staged_journals(self) -> None:
        for source in self._registered.values():
            if source.provenance.get("status") != "captured":
                continue
            journal_id = str(source.provenance.get("journal_id") or "")
            if journal_id and journal_id not in self._journals:
                self._journals[journal_id] = (
                    self.api.export_collection_upload_session_provenance_journal(
                        self.collection_id,
                        journal_id,
                    )
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
    block_sha256s: list[str] = []

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
            block_sha256s.append(hashlib.sha256(chunk).hexdigest())
            offset += size
            if progress is not None:
                progress(item.path, offset, expected)
            yield chunk

    if expected >= pack_member_bytes:
        manifest = hash_raw_source(
            path=item.path,
            chunks=chunks(),
            expected_bytes=expected,
            part_plaintext_bytes=raw_part_bytes,
        )
        sha256 = manifest.sha256
        raw_parts: dict[str, object] | None = {
            "part_plaintext_bytes": manifest.part_plaintext_bytes,
            "sha256s": list(manifest.part_sha256s),
        }
    else:
        digest = hashlib.sha256()
        for chunk in chunks():
            digest.update(chunk)
        sha256 = digest.hexdigest()
        raw_parts = None
    _require_same_file(item.source.stat(), observed, path=item.path)
    return _Source(
        path=item.path,
        bytes=expected,
        sha256=sha256,
        provenance=provenance,
        raw_parts=raw_parts,
        reader=_VerifiedRangeReader(
            source=read_local,
            path=item.path,
            bytes=expected,
            block_sha256s=tuple(block_sha256s),
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
    block_sha256s: list[str] = []

    def chunks() -> Iterator[bytes]:
        nonlocal offset
        while offset < item.bytes:
            size = min(_STREAM_VERIFY_BLOCK_BYTES, item.bytes - offset)
            chunk = item.read_range(offset, size)
            if len(chunk) != size:
                raise RuntimeError(f"producer stream returned an incomplete range: {item.path}")
            block_sha256s.append(hashlib.sha256(chunk).hexdigest())
            offset += size
            if progress is not None:
                progress(item.path, offset, item.bytes)
            yield chunk

    if item.bytes >= pack_member_bytes:
        manifest = hash_raw_source(
            path=item.path,
            chunks=chunks(),
            expected_bytes=item.bytes,
            part_plaintext_bytes=raw_part_bytes,
        )
        sha256 = manifest.sha256
        raw_parts: dict[str, object] | None = {
            "part_plaintext_bytes": manifest.part_plaintext_bytes,
            "sha256s": list(manifest.part_sha256s),
        }
    else:
        digest = hashlib.sha256()
        for chunk in chunks():
            digest.update(chunk)
        sha256 = digest.hexdigest()
        raw_parts = None
    if sha256 != item.sha256:
        raise RuntimeError(f"producer stream identity changed before upload: {item.path}")
    verified_reader = _VerifiedRangeReader(
        source=item.read_range,
        path=item.path,
        bytes=item.bytes,
        block_sha256s=tuple(block_sha256s),
    )
    return _Source(
        path=item.path,
        bytes=item.bytes,
        sha256=item.sha256,
        provenance=provenance,
        raw_parts=raw_parts,
        reader=verified_reader,
    )


@dataclass(frozen=True, slots=True)
class _VerifiedRangeReader:
    source: RangeReader
    path: str
    bytes: int
    block_sha256s: tuple[str, ...]

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
            if hashlib.sha256(content).hexdigest() != self.block_sha256s[block]:
                raise RuntimeError(
                    f"producer source changed during upload verification: {self.path}"
                )
            chunks.append(content)
        combined = b"".join(chunks)
        relative = offset - first * _STREAM_VERIFY_BLOCK_BYTES
        return combined[relative : relative + size]


def _ordered_sources(values: Mapping[str, _Source]) -> list[_Source]:
    return sorted(
        values.values(), key=lambda current: collection_upload_path_order_key(current.path)
    )


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
        "provenance": source.provenance,
    }


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


def _provenance_identity(
    sources: Sequence[_Source],
    journals: Mapping[str, bytes],
) -> str:
    bindings: list[FileProvenanceBinding] = []
    for source in sources:
        status = str(source.provenance["status"])
        bindings.append(
            FileProvenanceBinding(
                path=source.path,
                bytes=source.bytes,
                sha256=source.sha256,
                status=cast(ProvenanceStatus, status),
                journal_id=(str(source.provenance["journal_id"]) if status == "captured" else None),
                current_state_id=(
                    str(source.provenance["current_state_id"]) if status == "captured" else None
                ),
                omission_reason=(
                    str(source.provenance["omission_reason"]) if status == "omitted" else None
                ),
            )
        )
    return build_provenance_archive(bindings=bindings, journals=journals).identity


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
    "ProducerProvenance",
    "ProducerStream",
    "ProvenanceBuilder",
    "RangeReader",
]
