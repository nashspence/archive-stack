"""Automatic continuation of exact per-file provenance through transforms."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from riverhog_api_client.producer import (
    ProducerArtifactIdentity,
    ProducerProvenance,
    ProvenanceBuilder,
)
from riverhog_protocol.collection_workflows import ArtifactDisposition
from riverhog_protocol.errors import NotFound
from riverhog_protocol.paths import CollectionId
from riverhog_provenance import (
    create_derivative_journal_from_identity,
    current_state_reference,
    validate_journal,
    verify_payload_binding,
)
from riverhog_provenance_contracts import ProvenanceJournalId

from riverhog_transform_sdk.models import ClaimedArtifact

_JOURNAL_NAMESPACE = uuid.UUID("d3581958-1067-433c-80a6-a2c60250ba70")


class _TransformProvenanceApi(Protocol):
    def list_collection_provenance(
        self,
        collection_id: CollectionId,
        *,
        all_items: bool = False,
    ) -> dict[str, Any]: ...

    def export_collection_provenance_journal(
        self,
        collection_id: CollectionId,
        journal_id: ProvenanceJournalId,
    ) -> bytes: ...

    def export_collection_upload_session_provenance_journal(
        self,
        collection_id: CollectionId,
        journal_id: ProvenanceJournalId,
    ) -> bytes: ...


@dataclass(frozen=True, slots=True)
class IncrementalArtifactProvenance:
    binding: Mapping[str, object]
    journals: Mapping[str, bytes]


@dataclass(frozen=True, slots=True)
class IncrementalTransformProvenance:
    """Exact source histories reusable as target artifacts finalize."""

    api: _TransformProvenanceApi
    current_by_input: Mapping[tuple[int, str], bytes]
    journals: Mapping[str, bytes]
    execution_id: str
    operation_id: str
    producer_app: str
    producer_version: str
    started_at: str
    heartbeat: Callable[[], None] | None

    @property
    def captured(self) -> bool:
        return bool(self.current_by_input)

    def artifact(
        self,
        collection_id: CollectionId,
        identity: ProducerArtifactIdentity,
        *,
        sources: Sequence[ClaimedArtifact],
        resumed: bool,
    ) -> IncrementalArtifactProvenance | None:
        ordered = tuple(
            self.current_by_input[item.key]
            for item in sorted(sources, key=lambda current: current.key)
            if item.key in self.current_by_input
        )
        if not ordered:
            return None
        if self.heartbeat is not None:
            self.heartbeat()
        journal_id = _output_journal_id(self.execution_id, identity.path)
        journal: bytes | None = None
        if resumed:
            try:
                journal = self.api.export_collection_upload_session_provenance_journal(
                    collection_id,
                    journal_id,
                )
            except NotFound:
                pass
        if journal is None:
            journal = create_derivative_journal_from_identity(
                relative_path=identity.path,
                byte_count=identity.bytes,
                sha256=identity.sha256,
                source_journals=ordered,
                agent_name=self.producer_app,
                agent_version=self.producer_version,
                event_label=self.operation_id,
                started_at=self.started_at,
                ended_at=_utc_now(),
                journal_id=journal_id,
            )
        summary = validate_journal(journal)
        verify_payload_binding(
            summary,
            path=identity.path,
            byte_count=identity.bytes,
            sha256=identity.sha256,
        )
        expected_sources = {
            (
                reference.journal_id,
                reference.entry_id,
                reference.entry_json_sha256,
                reference.state_id,
            )
            for reference in map(current_state_reference, ordered)
        }
        actual_sources = {
            (
                reference.journal_id,
                reference.entry_id,
                reference.entry_json_sha256,
                reference.state_id,
            )
            for reference in summary.external_states
        }
        if summary.journal_id != journal_id or actual_sources != expected_sources:
            raise RuntimeError("staged transform provenance differs from exact source lineage")
        journals = dict(self.journals)
        existing = journals.get(journal_id)
        if existing is not None and existing != journal:
            raise RuntimeError("transform provenance journal identity collides")
        journals[journal_id] = journal
        return IncrementalArtifactProvenance(
            binding={
                "status": "captured",
                "journal_id": journal_id,
                "current_state_id": summary.current_state_id,
            },
            journals=journals,
        )


def prepare_incremental_transform_provenance(
    api: _TransformProvenanceApi,
    *,
    inventory: Sequence[ClaimedArtifact],
    execution_id: str,
    operation_id: str,
    producer_app: str,
    producer_version: str,
    started_at: str,
    heartbeat: Callable[[], None] | None = None,
) -> IncrementalTransformProvenance:
    """Capture available input histories before incremental output publication."""

    rows: dict[tuple[int, str], Mapping[str, object]] = {}
    by_key = {item.key: item for item in inventory}
    for collection_id in sorted({item.root.collection_id for item in inventory}):
        if heartbeat is not None:
            heartbeat()
        payload = api.list_collection_provenance(collection_id, all_items=True)
        raw_files = payload.get("files")
        if not isinstance(raw_files, list):
            raise RuntimeError("Riverhog provenance inventory has no file rows")
        for raw in raw_files:
            if not isinstance(raw, Mapping):
                raise RuntimeError("Riverhog provenance inventory contains an invalid row")
            key = (
                _positive_int(raw.get("collection_id"), "provenance collection id"),
                str(raw.get("path") or ""),
            )
            if key in by_key:
                rows[key] = raw
    journals: dict[str, bytes] = {}
    loaded: set[tuple[int, str]] = set()
    current: dict[tuple[int, str], bytes] = {}
    for key, artifact in sorted(by_key.items()):
        row = rows.get(key)
        if row is None:
            raise RuntimeError("Riverhog provenance inventory omitted a claimed input")
        if (
            _nonnegative_int(row.get("bytes"), "provenance artifact bytes") != artifact.bytes
            or str(row.get("sha256") or "") != artifact.sha256
        ):
            raise RuntimeError("Riverhog provenance inventory changed an input identity")
        binding = row.get("provenance")
        if not isinstance(binding, Mapping):
            raise RuntimeError("Riverhog provenance inventory has no artifact binding")
        if str(binding.get("status") or "") == "omitted":
            continue
        if str(binding.get("status") or "") != "captured":
            raise RuntimeError("Riverhog provenance inventory has an invalid status")
        journal_id = str(binding.get("journal_id") or "")
        content = _load_journal_closure(
            api,
            collection_id=key[0],
            journal_id=journal_id,
            journals=journals,
            loaded_journals=loaded,
            heartbeat=heartbeat,
        )
        summary = validate_journal(content)
        verify_payload_binding(
            summary,
            path=artifact.path,
            byte_count=artifact.bytes,
            sha256=artifact.sha256,
        )
        if str(binding.get("current_state_id") or "") != summary.current_state_id:
            raise RuntimeError("Riverhog provenance projection changed its current state")
        current[key] = content
    return IncrementalTransformProvenance(
        api=api,
        current_by_input=current,
        journals=journals,
        execution_id=execution_id,
        operation_id=operation_id,
        producer_app=producer_app,
        producer_version=producer_version,
        started_at=started_at,
        heartbeat=heartbeat,
    )


def prepare_transform_provenance(
    api: _TransformProvenanceApi,
    *,
    inventory: Sequence[ClaimedArtifact],
    dispositions: Sequence[ArtifactDisposition],
    execution_id: str,
    operation_id: str,
    producer_app: str,
    producer_version: str,
    started_at: str,
    heartbeat: Callable[[], None] | None = None,
) -> ProvenanceBuilder | None:
    """Capture exact available source histories for publication-time continuation."""

    artifacts = {item.key: item for item in inventory}
    relevant = {
        (item.input_collection_id, item.input_path) for item in dispositions if item.outputs
    }
    rows: dict[tuple[int, str], Mapping[str, object]] = {}
    for collection_id in sorted({item[0] for item in relevant}):
        if heartbeat is not None:
            heartbeat()
        payload = api.list_collection_provenance(collection_id, all_items=True)
        raw_files = payload.get("files")
        if not isinstance(raw_files, list):
            raise RuntimeError("Riverhog provenance inventory has no file rows")
        for raw in raw_files:
            if not isinstance(raw, Mapping):
                raise RuntimeError("Riverhog provenance inventory contains an invalid row")
            row_collection_id = _positive_int(
                raw.get("collection_id"),
                "provenance collection id",
            )
            path = str(raw.get("path") or "")
            key = (row_collection_id, path)
            if key in relevant:
                if key in rows:
                    raise RuntimeError("Riverhog provenance inventory repeats an input")
                rows[key] = raw

    journals: dict[str, bytes] = {}
    loaded_journals: set[tuple[int, str]] = set()
    current_by_input: dict[tuple[int, str], bytes] = {}
    for key in sorted(relevant):
        artifact = artifacts.get(key)
        row = rows.get(key)
        if artifact is None or row is None:
            raise RuntimeError("Riverhog provenance inventory omitted a claimed input")
        if (
            _nonnegative_int(row.get("bytes"), "provenance artifact bytes") != artifact.bytes
            or str(row.get("sha256") or "") != artifact.sha256
        ):
            raise RuntimeError("Riverhog provenance inventory changed an input identity")
        binding = row.get("provenance")
        if not isinstance(binding, Mapping):
            raise RuntimeError("Riverhog provenance inventory has no artifact binding")
        status = str(binding.get("status") or "")
        if status == "omitted":
            continue
        if status != "captured":
            raise RuntimeError("Riverhog provenance inventory has an invalid status")
        journal_id = str(binding.get("journal_id") or "")
        if not journal_id:
            raise RuntimeError("captured Riverhog provenance has no journal identity")
        content = _load_journal_closure(
            api,
            collection_id=key[0],
            journal_id=journal_id,
            journals=journals,
            loaded_journals=loaded_journals,
            heartbeat=heartbeat,
        )
        summary = validate_journal(content)
        verify_payload_binding(
            summary,
            path=artifact.path,
            byte_count=artifact.bytes,
            sha256=artifact.sha256,
        )
        if str(binding.get("current_state_id") or "") != summary.current_state_id:
            raise RuntimeError("Riverhog provenance projection changed its current state")
        current_by_input[key] = content

    if not current_by_input:
        return None
    return _TransformProvenanceBuilder(
        api=api,
        dispositions=tuple(dispositions),
        current_by_input=current_by_input,
        journals=journals,
        execution_id=execution_id,
        operation_id=operation_id,
        producer_app=producer_app,
        producer_version=producer_version,
        started_at=started_at,
        heartbeat=heartbeat,
    )


@dataclass(frozen=True, slots=True)
class _TransformProvenanceBuilder:
    api: _TransformProvenanceApi
    dispositions: tuple[ArtifactDisposition, ...]
    current_by_input: Mapping[tuple[int, str], bytes]
    journals: Mapping[str, bytes]
    execution_id: str
    operation_id: str
    producer_app: str
    producer_version: str
    started_at: str
    heartbeat: Callable[[], None] | None

    def __call__(
        self,
        collection_id: CollectionId,
        resumed: bool,
        artifacts: tuple[ProducerArtifactIdentity, ...],
    ) -> ProducerProvenance:
        by_path = {item.path: item for item in artifacts}
        source_by_output: dict[str, list[tuple[tuple[int, str], bytes]]] = {}
        for disposition in self.dispositions:
            key = (disposition.input_collection_id, disposition.input_path)
            source = self.current_by_input.get(key)
            if source is None:
                continue
            for output in disposition.outputs:
                source_by_output.setdefault(output, []).append((key, source))

        bindings: dict[str, Mapping[str, object]] = {}
        journals = dict(self.journals)
        ended_at = _utc_now()
        for path, sources in sorted(source_by_output.items()):
            if self.heartbeat is not None:
                self.heartbeat()
            artifact = by_path.get(path)
            if artifact is None:
                raise RuntimeError(
                    f"artifact disposition references an absent producer output: {path}"
                )
            ordered_sources = tuple(content for _key, content in sorted(sources))
            journal_id = _output_journal_id(self.execution_id, path)
            journal: bytes | None = None
            if resumed:
                try:
                    journal = self.api.export_collection_upload_session_provenance_journal(
                        collection_id,
                        journal_id,
                    )
                except NotFound:
                    pass
            if journal is None:
                journal = create_derivative_journal_from_identity(
                    relative_path=artifact.path,
                    byte_count=artifact.bytes,
                    sha256=artifact.sha256,
                    source_journals=ordered_sources,
                    agent_name=self.producer_app,
                    agent_version=self.producer_version,
                    event_label=self.operation_id,
                    started_at=self.started_at,
                    ended_at=ended_at,
                    journal_id=journal_id,
                )
            summary = validate_journal(journal)
            verify_payload_binding(
                summary,
                path=artifact.path,
                byte_count=artifact.bytes,
                sha256=artifact.sha256,
            )
            expected_sources = {
                (
                    reference.journal_id,
                    reference.entry_id,
                    reference.entry_json_sha256,
                    reference.state_id,
                )
                for reference in map(current_state_reference, ordered_sources)
            }
            actual_sources = {
                (
                    reference.journal_id,
                    reference.entry_id,
                    reference.entry_json_sha256,
                    reference.state_id,
                )
                for reference in summary.external_states
            }
            if summary.journal_id != journal_id or actual_sources != expected_sources:
                raise RuntimeError("staged transform provenance differs from exact source lineage")
            existing = journals.get(journal_id)
            if existing is not None and existing != journal:
                raise RuntimeError("transform provenance journal identity collides")
            journals[journal_id] = journal
            bindings[path] = {
                "status": "captured",
                "journal_id": journal_id,
                "current_state_id": summary.current_state_id,
            }
        return ProducerProvenance(bindings=bindings, journals=journals)


def _load_journal_closure(
    api: _TransformProvenanceApi,
    *,
    collection_id: CollectionId,
    journal_id: ProvenanceJournalId,
    journals: dict[str, bytes],
    loaded_journals: set[tuple[int, str]],
    heartbeat: Callable[[], None] | None,
) -> bytes:
    pending = [journal_id]
    while pending:
        if heartbeat is not None:
            heartbeat()
        current_id = pending.pop()
        collection_key = (collection_id, current_id)
        existing = journals.get(current_id)
        content = (
            existing
            if collection_key in loaded_journals and existing is not None
            else api.export_collection_provenance_journal(collection_id, current_id)
        )
        summary = validate_journal(content)
        if summary.journal_id != current_id:
            raise RuntimeError("Riverhog exported a different provenance journal identity")
        prior = journals.get(current_id)
        if prior is not None and prior != content:
            raise RuntimeError("Riverhog provenance journal identity has different bytes")
        journals[current_id] = content
        loaded_journals.add(collection_key)
        for reference in summary.external_states:
            if (collection_id, reference.journal_id) not in loaded_journals:
                pending.append(reference.journal_id)
    return journals[journal_id]


def _output_journal_id(execution_id: str, path: str) -> str:
    return f"urn:uuid:{uuid.uuid5(_JOURNAL_NAMESPACE, execution_id + chr(0) + path)}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError(f"{label} is invalid")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"{label} is invalid")
    return value


__all__: list[str] = []
