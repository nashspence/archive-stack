"""Reusable content-opaque direct-to-final Riverhog collection producer."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from riverhog_protocol.collection_workflows import PRODUCER_EVIDENCE_PATH, ProducerEvidence
from riverhog_protocol.manifest import collection_content_etag_ordered
from riverhog_protocol.paths import normalize_relpath
from riverhog_protocol.raw_ingress import hash_raw_source
from riverhog_provenance import FileProvenanceBinding, build_provenance_archive

from riverhog_api_client.client import ApiClient
from riverhog_api_client.uploads import (
    configured_upload_concurrency,
    configured_upload_window,
    upload_collection_units,
)

ReadProgress = Callable[[str, int, int], None]
ProvenanceStatus = Literal["captured", "omitted"]


@dataclass(frozen=True, slots=True)
class ProducerFile:
    source: Path
    path: str
    provenance: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", self.source.resolve())
        object.__setattr__(self, "path", normalize_relpath(self.path))
        if not self.source.is_file() or self.source.is_symlink():
            raise ValueError(f"producer source must be a real regular file: {self.source}")
        if self.provenance is not None:
            object.__setattr__(self, "provenance", dict(self.provenance))


@dataclass(frozen=True, slots=True)
class ProducedCollection:
    collection_id: int
    manifest_sha256: str
    content_etag: str
    receipt: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Source:
    path: str
    bytes: int
    sha256: str
    provenance: dict[str, object]
    local: Path | None = None
    content: bytes | None = None
    raw_parts: dict[str, object] | None = None

    def read_range(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > self.bytes:
            raise RuntimeError(f"upload unit requested an invalid source range: {self.path}")
        if self.content is not None:
            return self.content[offset : offset + size]
        if self.local is None:
            raise RuntimeError(f"producer source has no readable content: {self.path}")
        before = self.local.stat()
        with self.local.open("rb") as stream:
            stream.seek(offset)
            content = stream.read(size)
        after = self.local.stat()
        if (
            len(content) != size
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise RuntimeError(f"producer source changed during upload: {self.path}")
        return content


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
        archive_store: str | None = None,
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
            source_context=dict(source_context or {}),
        )
        key = idempotency_key or evidence.sha256
        omitted = _omitted(self.provenance_omission_reason)
        paths = [PRODUCER_EVIDENCE_PATH]
        paths.extend(item.path for item in files)
        paths.extend(normalize_relpath(path) for path in (inline_evidence or {}))
        if len(paths) != len(set(paths)):
            raise ValueError("producer collection paths must be unique")

        normalized_journals = {str(key): bytes(value) for key, value in (provenance_journals or {}).items()}
        source_provenance = [
            _provenance(item.provenance, default=omitted) for item in files
        ]
        captured = bool(normalized_journals) or any(
            current.get("status") == "captured" for current in source_provenance
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
        collection_id = int(session["collection_id"])
        if str(session.get("state") or "") == "finalized":
            return _finalized_receipt(session)
        layout = session.get("layout")
        if not isinstance(layout, Mapping):
            raise RuntimeError("Riverhog upload session did not return a layout")
        pack_member_bytes = int(layout["pack_member_bytes"])
        raw_part_bytes = int(layout["raw_part_plaintext_bytes"])

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
            sources[item.path] = _hash_local_source(
                item,
                provenance=provenance,
                pack_member_bytes=pack_member_bytes,
                raw_part_bytes=raw_part_bytes,
                progress=progress,
            )
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
        self.api.register_collection_upload_session_files(collection_id, registration)

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
                    raise RuntimeError(f"Riverhog requested an unknown producer path: {source_path}")
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
        content_etag = collection_content_etag_ordered(
            (source.path, source.bytes, source.sha256) for source in ordered
        )
        provenance_etag = _provenance_etag(ordered, normalized_journals) if captured else None
        receipt = self.api.complete_collection_upload_session(
            collection_id,
            files_total=len(sources),
            content_etag=content_etag,
            provenance_etag=provenance_etag,
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
    if expected >= pack_member_bytes:
        read = 0

        def chunks():  # type: ignore[no-untyped-def]
            nonlocal read
            with item.source.open("rb") as stream:
                while chunk := stream.read(8 * 1024 * 1024):
                    read += len(chunk)
                    if progress is not None:
                        progress(item.path, read, expected)
                    yield chunk

        manifest = hash_raw_source(
            path=item.path,
            chunks=chunks(),
            expected_bytes=expected,
            part_plaintext_bytes=raw_part_bytes,
        )
        source = _Source(
            path=item.path,
            bytes=expected,
            sha256=manifest.sha256,
            local=item.source,
            provenance=provenance,
            raw_parts={
                "part_plaintext_bytes": manifest.part_plaintext_bytes,
                "sha256s": list(manifest.part_sha256s),
            },
        )
    else:
        digest = hashlib.sha256()
        read = 0
        with item.source.open("rb") as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                digest.update(chunk)
                read += len(chunk)
                if progress is not None:
                    progress(item.path, read, expected)
        source = _Source(
            path=item.path,
            bytes=expected,
            sha256=digest.hexdigest(),
            local=item.source,
            provenance=provenance,
        )
    current = item.source.stat()
    if (
        current.st_dev != observed.st_dev
        or current.st_ino != observed.st_ino
        or current.st_size != observed.st_size
        or current.st_mtime_ns != observed.st_mtime_ns
    ):
        raise RuntimeError(f"producer source changed during hashing: {item.path}")
    return source


def _ordered_sources(values: Mapping[str, _Source]) -> list[_Source]:
    return sorted(values.values(), key=lambda current: current.path.encode("utf-8"))


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


def _provenance_etag(
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
                journal_id=(
                    str(source.provenance["journal_id"]) if status == "captured" else None
                ),
                current_state_id=(
                    str(source.provenance["current_state_id"])
                    if status == "captured"
                    else None
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
    content_etag = str(payload.get("content_etag") or collection.get("content_etag") or "")
    manifest_sha256 = str(collection.get("manifest_sha256") or payload.get("manifest_sha256") or "")
    if len(manifest_sha256) != 64:
        raise RuntimeError("finalized Riverhog receipt has no immutable manifest identity")
    if len(content_etag) != 64:
        raise RuntimeError("finalized Riverhog receipt has no content identity")
    return ProducedCollection(
        collection_id=collection_id,
        manifest_sha256=manifest_sha256,
        content_etag=content_etag,
        receipt=dict(payload),
    )


__all__ = ["CollectionProducer", "ProducedCollection", "ProducerFile"]
