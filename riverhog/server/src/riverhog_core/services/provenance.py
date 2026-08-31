from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any, cast

from http_api_contracts import BrowseScalar, closed_literal_values
from riverhog_protocol import (
    DERIVATION_EVIDENCE_PATH,
    ProvenanceSort,
    ProvenanceStatus,
    SortOrder,
)
from riverhog_protocol.errors import BadRequest, InvalidState, NotFound
from riverhog_protocol.paths import (
    PathNormalizationError,
    normalize_collection_id,
    normalize_relpath,
    relpath_sort_key,
    text_search_key,
)
from riverhog_provenance import (
    PROVENANCE_BINDING_SEGMENT_FILES_MAX,
    PROVENANCE_JOURNAL_ENTRY_BYTES_MAX,
    PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX,
    FileProvenanceBinding,
    ProvenancePayloadIdentity,
    ProvenanceRootDocument,
    ProvenanceTerminalDocument,
    ProvenanceValidationError,
    ProvenanceVolumeDocument,
    binding_segment_bytes,
    bounded_binding_segment_bytes,
    format_provenance_sequence,
    update_ordered_volume_commitment,
)
from riverhog_provenance.journal import (
    resolve_incremental_journal_current_state,
    validate_incremental_journal_entry,
)
from sqlalchemy import and_, asc, case, delete, desc, func, or_, select, tuple_, update
from sqlalchemy.orm import Session
from state_schema import read_snapshot
from time_formats import utc_timestamp_now

from riverhog_core.app_permissions import (
    ALL_RESOURCES,
    CATALOG_READ,
    PROVENANCE_EXPORT,
    PROVENANCE_READ,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.artifact_access import artifact_scope_filter, require_artifact_scope
from riverhog_core.browse import bounded_page, keyset_statement, validate_page_size
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionFileProvenanceRecord,
    CollectionFileRecord,
    CollectionProvenanceEntityRecord,
    CollectionProvenanceExternalStateReferenceRecord,
    CollectionProvenanceJournalAgentRecord,
    CollectionProvenanceJournalChunkRecord,
    CollectionProvenanceJournalRecord,
    CollectionProvenanceVerificationAgentRecord,
    CollectionProvenanceVerificationEntityRecord,
    CollectionProvenanceVerificationEntryRecord,
    CollectionProvenanceVerificationExternalStateRecord,
    CollectionProvenanceVerificationReachabilityRecord,
    CollectionProvenanceVerificationRecord,
    CollectionRecord,
)
from riverhog_core.checkpoint_sha256 import CheckpointSHA256
from riverhog_core.runtime_config import RuntimeConfig

_SORT_FIELDS = closed_literal_values(ProvenanceSort)
_STATUS_VALUES = closed_literal_values(ProvenanceStatus)
_SORT_ORDERS = closed_literal_values(SortOrder)
_PROVENANCE_JOURNAL_CHUNK_BYTES = 1024 * 1024
_INTERNAL_VERIFIER = ApplicationPrincipal(
    app="riverhog-provenance-verifier",
    key_id=None,
    access=frozenset({ApplicationAccess("*", ALL_RESOURCES)}),
)


class _VerificationCanceled(RuntimeError):
    pass


class SqlAlchemyProvenanceService:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._session_factory = session_factory or make_session_factory(config.database_url)

    def list_files(
        self,
        collection_id: int,
        *,
        page_size: int,
        position: tuple[str | int | bool | bytes | None, ...] | None,
        q: str | None,
        status: str | None,
        sort: str,
        order: str,
        principal: ApplicationPrincipal,
    ) -> dict[str, Any]:
        collection_id = _collection_id(collection_id)
        _page_options(page_size, sort, order)
        if status is not None and status not in _STATUS_VALUES:
            raise BadRequest(f"status must be one of {', '.join(sorted(_STATUS_VALUES))}")
        with read_snapshot(self._session_factory) as session:
            collection = _authorized_collection(session, collection_id, principal)
            joined, key_columns = _provenance_file_statement(
                collection_id=collection_id,
                principal=principal,
                q=q,
                status=status,
                sort=sort,
                order=order,
            )
            rows, next_position = bounded_page(
                list(
                    session.execute(
                        keyset_statement(
                            joined,
                            columns=key_columns,
                            position=position,
                            order=order,
                            page_size=page_size,
                        )
                    )
                ),
                page_size=page_size,
                position_of=lambda row: _provenance_file_position(row[0], sort=sort),
            )
            return {
                "page_size": page_size,
                "_next_position": next_position,
                "sort": sort,
                "order": order,
                "query": q,
                "status": status,
                "collection_id": collection_id,
                "provenance_mode": collection.provenance_mode,
                "provenance_identity": collection.provenance_identity,
                "files": [_file_payload(file, binding, collection) for file, binding in rows],
            }

    def iter_files(
        self,
        collection_id: int,
        *,
        q: str | None,
        status: str | None,
        sort: str,
        order: str,
        principal: ApplicationPrincipal,
    ) -> Iterator[dict[str, Any]]:
        collection_id = _collection_id(collection_id)
        _page_options(100, sort, order)
        if status is not None and status not in _STATUS_VALUES:
            raise BadRequest(f"status must be one of {', '.join(sorted(_STATUS_VALUES))}")
        with read_snapshot(self._session_factory) as session:
            collection = _authorized_collection(session, collection_id, principal)
            statement, key_columns = _provenance_file_statement(
                collection_id=collection_id,
                principal=principal,
                q=q,
                status=status,
                sort=sort,
                order=order,
            )
            direction = desc if order == "desc" else asc
            statement = statement.order_by(*(direction(column) for column in key_columns))
            statement = statement.execution_options(yield_per=100)
            for file, binding in session.execute(statement):
                yield _file_payload(file, binding, collection)

    def show_file(
        self,
        collection_id: int,
        path: str,
        *,
        principal: ApplicationPrincipal,
    ) -> dict[str, Any]:
        collection_id = _collection_id(collection_id)
        path = _path(path)
        with read_snapshot(self._session_factory) as session:
            return _shown_file(session, collection_id, path, principal)

    def trace_file(
        self,
        collection_id: int,
        path: str,
        *,
        page_size: int,
        position: tuple[str | int | bool | bytes | None, ...] | None,
        principal: ApplicationPrincipal,
    ) -> dict[str, Any]:
        collection_id = _collection_id(collection_id)
        path = _path(path)
        validate_page_size(page_size)
        with read_snapshot(self._session_factory) as session:
            shown = _shown_file(session, collection_id, path, principal)
            binding = shown["provenance"]
            if binding["status"] == "omitted":
                return {
                    **shown,
                    "page_size": page_size,
                    "_next_position": None,
                    "items": [],
                }
            journal_statement, reference_statement = _trace_statements(
                collection_id,
                str(binding["journal_id"]),
            )
            items, next_position = _trace_page_items(
                session,
                journal_statement,
                reference_statement,
                position=position,
                page_size=page_size,
            )
            return {
                **shown,
                "page_size": page_size,
                "_next_position": next_position,
                "items": items,
            }

    def iter_trace_file(
        self,
        collection_id: int,
        path: str,
        *,
        principal: ApplicationPrincipal,
    ) -> Iterator[dict[str, Any]]:
        collection_id = _collection_id(collection_id)
        path = _path(path)
        with read_snapshot(self._session_factory) as session:
            shown = _shown_file(session, collection_id, path, principal)
            binding = shown["provenance"]
            if binding["status"] == "omitted":
                return
            journal_statement, reference_statement = _trace_statements(
                collection_id,
                str(binding["journal_id"]),
            )
            for journal in session.scalars(journal_statement.execution_options(yield_per=100)):
                yield {"kind": "journal", "journal": _journal_payload(journal)}
            for reference in session.scalars(reference_statement.execution_options(yield_per=100)):
                yield {
                    "kind": "external_state_reference",
                    "reference": _external_state_reference_payload(reference),
                }

    def journal_metadata(
        self,
        collection_id: int,
        journal_id: str,
        *,
        principal: ApplicationPrincipal,
    ) -> tuple[int, str]:
        collection_id = _collection_id(collection_id)
        with read_snapshot(self._session_factory) as session:
            _authorized_collection(session, collection_id, principal)
            if not principal.allows_collection(PROVENANCE_EXPORT, collection_id):
                raise NotFound(f"collection not found: {collection_id}")
            if not _journal_is_in_artifact_scope(
                session,
                collection_id,
                journal_id,
                principal,
            ):
                raise NotFound(f"provenance journal not found: {journal_id}")
            record = session.get(
                CollectionProvenanceJournalRecord,
                (collection_id, journal_id),
            )
            if record is None:
                raise NotFound(f"provenance journal not found: {journal_id}")
            return record.bytes, record.sha256

    def iter_journal(
        self,
        collection_id: int,
        journal_id: str,
        *,
        principal: ApplicationPrincipal,
    ) -> Iterator[bytes]:
        """Yield one exact journal in bounded database chunks."""

        collection_id = _collection_id(collection_id)
        with read_snapshot(self._session_factory) as session:
            _authorized_collection(session, collection_id, principal)
            if not principal.allows_collection(PROVENANCE_EXPORT, collection_id):
                raise NotFound(f"collection not found: {collection_id}")
            if not _journal_is_in_artifact_scope(
                session,
                collection_id,
                journal_id,
                principal,
            ):
                raise NotFound(f"provenance journal not found: {journal_id}")
            if (
                session.get(
                    CollectionProvenanceJournalRecord,
                    (collection_id, journal_id),
                )
                is None
            ):
                raise NotFound(f"provenance journal not found: {journal_id}")
            yield from _iter_journal_chunks(session, collection_id, journal_id)

    def iter_journal_range(
        self,
        collection_id: int,
        journal_id: str,
        *,
        offset: int,
        size: int,
        principal: ApplicationPrincipal,
    ) -> Iterator[bytes]:
        """Yield one exact bounded range without reading preceding journal bytes."""

        collection_id = _collection_id(collection_id)
        if isinstance(offset, bool) or offset < 0:
            raise BadRequest("provenance journal range offset is invalid")
        if isinstance(size, bool) or size < 1:
            raise BadRequest("provenance journal range size is invalid")
        with read_snapshot(self._session_factory) as session:
            _authorized_collection(session, collection_id, principal)
            if not principal.allows_collection(PROVENANCE_EXPORT, collection_id):
                raise NotFound(f"collection not found: {collection_id}")
            if not _journal_is_in_artifact_scope(
                session,
                collection_id,
                journal_id,
                principal,
            ):
                raise NotFound(f"provenance journal not found: {journal_id}")
            record = session.get(
                CollectionProvenanceJournalRecord,
                (collection_id, journal_id),
            )
            if record is None:
                raise NotFound(f"provenance journal not found: {journal_id}")
            if offset + size > record.bytes:
                raise BadRequest("provenance journal range is outside the journal")
            yield from _iter_journal_chunk_range(
                session,
                collection_id,
                journal_id,
                offset=offset,
                size=size,
            )

    def list_journal_agents(
        self,
        collection_id: int,
        journal_id: str,
        *,
        page_size: int,
        position: tuple[str | int | bool | bytes | None, ...] | None,
        principal: ApplicationPrincipal,
    ) -> dict[str, Any]:
        collection_id = _collection_id(collection_id)
        validate_page_size(page_size)
        with read_snapshot(self._session_factory) as session:
            _require_readable_journal(session, collection_id, journal_id, principal)
            predicate = (
                CollectionProvenanceJournalAgentRecord.collection_id == collection_id,
                CollectionProvenanceJournalAgentRecord.journal_id == journal_id,
            )
            rows, next_position = bounded_page(
                list(
                    session.scalars(
                        keyset_statement(
                            select(CollectionProvenanceJournalAgentRecord.agent_id).where(
                                *predicate
                            ),
                            columns=(CollectionProvenanceJournalAgentRecord.agent_id,),
                            position=position,
                            order="asc",
                            page_size=page_size,
                        )
                    )
                ),
                page_size=page_size,
                position_of=lambda agent_id: (agent_id,),
            )
            return {
                "collection_id": collection_id,
                "journal_id": journal_id,
                "page_size": page_size,
                "_next_position": next_position,
                "agents": [{"agent_id": agent_id} for agent_id in rows],
            }

    def iter_journal_agents(
        self,
        collection_id: int,
        journal_id: str,
        *,
        principal: ApplicationPrincipal,
    ) -> Iterator[dict[str, object]]:
        collection_id = _collection_id(collection_id)
        with read_snapshot(self._session_factory) as session:
            _require_readable_journal(session, collection_id, journal_id, principal)
            statement = (
                select(CollectionProvenanceJournalAgentRecord.agent_id)
                .where(
                    CollectionProvenanceJournalAgentRecord.collection_id == collection_id,
                    CollectionProvenanceJournalAgentRecord.journal_id == journal_id,
                )
                .order_by(CollectionProvenanceJournalAgentRecord.agent_id)
                .execution_options(yield_per=100)
            )
            for agent_id in session.scalars(statement):
                yield {"agent_id": agent_id}

    def request_verification(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
    ) -> dict[str, Any]:
        collection_id = _collection_id(collection_id)
        if principal.has_artifact_scope:
            raise NotFound(f"collection not found: {collection_id}")
        now = utc_timestamp_now()
        with session_scope(self._session_factory) as session:
            _authorized_collection(session, collection_id, principal)
            record = session.get(CollectionProvenanceVerificationRecord, collection_id)
            if record is None:
                record = CollectionProvenanceVerificationRecord(
                    collection_id=collection_id,
                    state="queued",
                    requested_by_app=principal.app,
                    requested_by_key_id=principal.key_id,
                    requested_at=now,
                    next_attempt_at=now,
                    attempts=0,
                    cancel_requested=False,
                    phase="metadata",
                    checkpoint_json="{}",
                )
                session.add(record)
                session.flush()
            elif record.state in {"failed", "canceled"}:
                record.state = "queued"
                record.requested_by_app = principal.app
                record.requested_by_key_id = principal.key_id
                record.requested_at = now
                record.started_at = None
                record.finished_at = None
                record.next_attempt_at = now
                record.cancel_requested = False
                record.result_json = None
                record.failure = None
                record.phase = "cleanup"
                record.checkpoint_json = json.dumps(
                    {"cleanup_stage": "reachability", "restart_after_cleanup": True},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            return _verification_payload(record)

    def get_verification(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
    ) -> dict[str, Any]:
        collection_id = _collection_id(collection_id)
        if principal.has_artifact_scope:
            raise NotFound(f"collection not found: {collection_id}")
        with read_snapshot(self._session_factory) as session:
            _authorized_collection(session, collection_id, principal)
            record = session.get(CollectionProvenanceVerificationRecord, collection_id)
            if record is None:
                raise NotFound(f"collection provenance verification not found: {collection_id}")
            return _verification_payload(record)

    def cancel_verification(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
    ) -> dict[str, Any]:
        collection_id = _collection_id(collection_id)
        if principal.has_artifact_scope:
            raise NotFound(f"collection not found: {collection_id}")
        with session_scope(self._session_factory) as session:
            _authorized_collection(session, collection_id, principal)
            record = session.get(CollectionProvenanceVerificationRecord, collection_id)
            if record is None:
                raise NotFound(f"collection provenance verification not found: {collection_id}")
            if record.state == "queued":
                record.state = "canceled"
                record.cancel_requested = True
                record.finished_at = utc_timestamp_now()
            elif record.state == "running":
                record.state = "canceling"
                record.cancel_requested = True
            return _verification_payload(record)

    def requeue_interrupted_verifications_for_startup(self) -> int:
        now = utc_timestamp_now()
        with session_scope(self._session_factory) as session:
            canceled = session.execute(
                update(CollectionProvenanceVerificationRecord)
                .where(CollectionProvenanceVerificationRecord.state == "canceling")
                .values(state="canceled", finished_at=now, cancel_requested=True)
            )
            requeued = session.execute(
                update(CollectionProvenanceVerificationRecord)
                .where(CollectionProvenanceVerificationRecord.state == "running")
                .values(
                    state="queued",
                    next_attempt_at=now,
                    started_at=None,
                    failure="verification interrupted before completion",
                )
            )
            return int(getattr(canceled, "rowcount", 0) or 0) + int(
                getattr(requeued, "rowcount", 0) or 0
            )

    def process_due_verifications(self, *, limit: int = 1) -> int:
        processed = 0
        for _ in range(max(0, limit)):
            collection_id = self._claim_due_verification()
            if collection_id is None:
                break
            try:
                result = self._advance_verification(collection_id)
            except _VerificationCanceled:
                self._finish_verification(collection_id, state="canceled")
            except Exception as exc:
                self._finish_verification(
                    collection_id,
                    state="failed",
                    failure=f"{type(exc).__name__}: {exc}"[:1000],
                )
            else:
                if result is None:
                    self._requeue_verification(collection_id)
                else:
                    self._finish_verification(collection_id, state="succeeded", result=result)
            processed += 1
        return processed

    def _advance_verification(self, collection_id: int) -> dict[str, Any] | None:
        """Advance one bounded, restartable verification checkpoint."""

        with session_scope(self._session_factory) as session:
            record = session.scalar(
                select(CollectionProvenanceVerificationRecord)
                .where(CollectionProvenanceVerificationRecord.collection_id == collection_id)
                .with_for_update()
            )
            if record is None or record.cancel_requested or record.state == "canceling":
                raise _VerificationCanceled
            collection = _authorized_collection(session, collection_id, _INTERNAL_VERIFIER)
            if record.phase == "metadata":
                return _advance_verification_metadata(session, record, collection)
            if record.phase == "identity-tree":
                _advance_verification_tree(session, record)
            elif record.phase == "identity-bindings":
                _advance_verification_binding_identity(session, record, collection)
            elif record.phase == "identity-journals":
                _advance_verification_journal_identity(session, record, collection)
            elif record.phase == "journal-entries":
                _advance_verification_journal_entry(session, record)
            elif record.phase == "references":
                _advance_verification_projection_comparison(session, record)
            elif record.phase == "reachability":
                result = _advance_verification_reachability(session, record, collection)
                if result is not None:
                    return result
            elif record.phase == "cleanup":
                return _advance_verification_cleanup(session, record, collection)
            elif record.phase == "complete":
                return _verification_result(record, collection)
            else:  # pragma: no cover - constrained durable state
                raise InvalidState("provenance verification phase is invalid")
            return None

    def _requeue_verification(self, collection_id: int) -> None:
        with session_scope(self._session_factory) as session:
            record = session.get(CollectionProvenanceVerificationRecord, collection_id)
            if record is None:
                return
            if record.cancel_requested or record.state == "canceling":
                record.state = "canceled"
                record.finished_at = utc_timestamp_now()
                return
            record.state = "queued"
            record.next_attempt_at = utc_timestamp_now()

    def _claim_due_verification(self) -> int | None:
        now = utc_timestamp_now()
        with session_scope(self._session_factory) as session:
            record = session.scalar(
                select(CollectionProvenanceVerificationRecord)
                .where(
                    CollectionProvenanceVerificationRecord.state == "queued",
                    CollectionProvenanceVerificationRecord.next_attempt_at <= now,
                )
                .order_by(
                    CollectionProvenanceVerificationRecord.next_attempt_at,
                    CollectionProvenanceVerificationRecord.collection_id,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if record is None:
                return None
            record.state = "running"
            record.started_at = now
            record.finished_at = None
            record.attempts += 1
            record.cancel_requested = False
            record.result_json = None
            record.failure = None
            return record.collection_id

    def _finish_verification(
        self,
        collection_id: int,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        failure: str | None = None,
    ) -> None:
        with session_scope(self._session_factory) as session:
            record = session.get(CollectionProvenanceVerificationRecord, collection_id)
            if record is None:
                return
            record.state = state
            record.finished_at = utc_timestamp_now()
            record.cancel_requested = state == "canceled"
            record.result_json = (
                json.dumps(result, sort_keys=True, separators=(",", ":"))
                if result is not None
                else None
            )
            record.failure = failure


def _verification_checkpoint(
    record: CollectionProvenanceVerificationRecord,
) -> dict[str, Any]:
    try:
        value = json.loads(record.checkpoint_json)
    except json.JSONDecodeError as exc:
        raise InvalidState("provenance verification checkpoint is invalid") from exc
    if not isinstance(value, dict):
        raise InvalidState("provenance verification checkpoint is invalid")
    return value


def _set_verification_checkpoint(
    record: CollectionProvenanceVerificationRecord,
    value: dict[str, Any],
) -> None:
    record.checkpoint_json = json.dumps(value, sort_keys=True, separators=(",", ":"))


def _advance_verification_metadata(
    session: Session,
    record: CollectionProvenanceVerificationRecord,
    collection: CollectionRecord,
) -> dict[str, Any] | None:
    collection_id = record.collection_id
    files = int(
        session.scalar(
            select(func.count())
            .select_from(CollectionFileRecord)
            .where(CollectionFileRecord.collection_id == collection_id)
        )
        or 0
    )
    bindings = int(
        session.scalar(
            select(func.count())
            .select_from(CollectionFileProvenanceRecord)
            .where(CollectionFileProvenanceRecord.collection_id == collection_id)
        )
        or 0
    )
    journals = int(
        session.scalar(
            select(func.count())
            .select_from(CollectionProvenanceJournalRecord)
            .where(CollectionProvenanceJournalRecord.collection_id == collection_id)
        )
        or 0
    )
    entities = int(
        session.scalar(
            select(func.count())
            .select_from(CollectionProvenanceEntityRecord)
            .where(CollectionProvenanceEntityRecord.collection_id == collection_id)
        )
        or 0
    )
    references = int(
        session.scalar(
            select(func.count())
            .select_from(CollectionProvenanceExternalStateReferenceRecord)
            .where(CollectionProvenanceExternalStateReferenceRecord.collection_id == collection_id)
        )
        or 0
    )
    mismatch = session.scalar(
        select(CollectionFileRecord.path)
        .outerjoin(
            CollectionFileProvenanceRecord,
            (CollectionFileProvenanceRecord.collection_id == CollectionFileRecord.collection_id)
            & (CollectionFileProvenanceRecord.path == CollectionFileRecord.path),
        )
        .where(
            CollectionFileRecord.collection_id == collection_id,
            CollectionFileRecord.provenance_status
            != func.coalesce(
                CollectionFileProvenanceRecord.status,
                "omitted" if collection.provenance_mode == "omitted" else "missing",
            ),
        )
        .limit(1)
    )
    if mismatch is not None:
        raise InvalidState("catalog file provenance status projection differs")
    checkpoint: dict[str, Any] = {
        "files": files,
        "journals": journals,
        "entities": entities,
        "references": references,
        "archive_generation": collection.archive_generation,
        "provenance_identity": collection.provenance_identity,
        "provenance_mode": collection.provenance_mode,
    }
    if collection.provenance_mode == "omitted":
        nonomitted = session.scalar(
            select(CollectionFileProvenanceRecord.path)
            .where(
                CollectionFileProvenanceRecord.collection_id == collection_id,
                CollectionFileProvenanceRecord.status != "omitted",
            )
            .limit(1)
        )
        if journals or entities or references or nonomitted is not None:
            raise InvalidState("collection-wide provenance omission is inconsistent")
        record.phase = "complete"
        _set_verification_checkpoint(record, checkpoint)
        return _verification_result(record, collection)
    if bindings != files:
        raise InvalidState("provenance does not account for every collection file")
    checkpoint.update(
        {
            "after_path": None,
            "file_order": 0,
            "tree_hash_state": CheckpointSHA256().export_state(),
        }
    )
    record.phase = "identity-tree"
    _set_verification_checkpoint(record, checkpoint)
    return None


def _verification_binding_rows(
    session: Session,
    collection_id: int,
    *,
    after_path: str | None,
    limit: int,
) -> list[tuple[CollectionFileRecord, CollectionFileProvenanceRecord]]:
    terminal_rank = case(
        (CollectionFileRecord.path == DERIVATION_EVIDENCE_PATH, 1),
        else_=0,
    )
    statement = (
        select(CollectionFileRecord, CollectionFileProvenanceRecord)
        .join(
            CollectionFileProvenanceRecord,
            (CollectionFileProvenanceRecord.collection_id == CollectionFileRecord.collection_id)
            & (CollectionFileProvenanceRecord.path == CollectionFileRecord.path),
        )
        .where(CollectionFileRecord.collection_id == collection_id)
        .order_by(terminal_rank, CollectionFileRecord.path_sort_key)
        .limit(limit)
    )
    if after_path is not None:
        after_rank = 1 if after_path == DERIVATION_EVIDENCE_PATH else 0
        statement = statement.where(
            or_(
                terminal_rank > after_rank,
                and_(
                    terminal_rank == after_rank,
                    CollectionFileRecord.path_sort_key > relpath_sort_key(after_path),
                ),
            )
        )
    return list(session.execute(statement).tuples())


def _advance_verification_tree(
    session: Session,
    record: CollectionProvenanceVerificationRecord,
) -> None:
    checkpoint = _verification_checkpoint(record)
    digest = CheckpointSHA256.from_state(str(checkpoint["tree_hash_state"]))
    rows = _verification_binding_rows(
        session,
        record.collection_id,
        after_path=checkpoint.get("after_path"),
        limit=PROVENANCE_BINDING_SEGMENT_FILES_MAX,
    )
    if rows:
        for file, _binding in rows:
            digest.update(f"{file.path}\t{file.bytes}\t{file.sha256}\n".encode())
        checkpoint["after_path"] = rows[-1][0].path
        checkpoint["file_order"] = int(checkpoint["file_order"]) + len(rows)
        checkpoint["tree_hash_state"] = digest.export_state()
        _set_verification_checkpoint(record, checkpoint)
        return
    if int(checkpoint["file_order"]) != int(checkpoint["files"]):
        raise InvalidState("provenance tree verification omitted collection files")
    checkpoint.update(
        {
            "tree_sha256": digest.hexdigest(),
            "after_path": None,
            "file_order": 0,
            "volume_sequence": 0,
            "volume_hash_state": CheckpointSHA256().export_state(),
        }
    )
    checkpoint.pop("tree_hash_state", None)
    record.phase = "identity-bindings"
    _set_verification_checkpoint(record, checkpoint)


def _file_binding(
    file: CollectionFileRecord,
    binding: CollectionFileProvenanceRecord,
) -> FileProvenanceBinding:
    return FileProvenanceBinding(
        path=file.path,
        bytes=file.bytes,
        sha256=file.sha256,
        status=cast(Any, binding.status),
        journal_id=binding.journal_id,
        current_state_id=binding.current_state_id,
        omission_reason=binding.omission_reason,
    )


def _advance_verification_binding_identity(
    session: Session,
    record: CollectionProvenanceVerificationRecord,
    collection: CollectionRecord,
) -> None:
    checkpoint = _verification_checkpoint(record)
    rows = _verification_binding_rows(
        session,
        record.collection_id,
        after_path=checkpoint.get("after_path"),
        limit=PROVENANCE_BINDING_SEGMENT_FILES_MAX,
    )
    if not rows:
        if int(checkpoint["file_order"]) != int(checkpoint["files"]):
            raise InvalidState("provenance binding identity omitted collection files")
        checkpoint.update(
            {
                "after_journal_id": None,
                "current_journal_id": None,
                "journal_offset": 0,
                "journal_count": 0,
            }
        )
        record.phase = "identity-journals"
        _set_verification_checkpoint(record, checkpoint)
        return
    bindings = [_file_binding(file, binding) for file, binding in rows]
    _payload, used = bounded_binding_segment_bytes(
        first_file_order=int(checkpoint["file_order"]),
        files=[_binding_mapping(binding) for binding in bindings],
    )
    rows = rows[:used]
    bindings = bindings[:used]
    first_order = int(checkpoint["file_order"])
    sequence = int(checkpoint["volume_sequence"])
    document = _binding_volume_document(
        archive_generation=collection.archive_generation,
        tree_sha256=str(checkpoint["tree_sha256"]),
        sequence=sequence,
        first_file_order=first_order,
        bindings=bindings,
    )
    digest = CheckpointSHA256.from_state(str(checkpoint["volume_hash_state"]))
    _update_volume_digest(digest, document)
    checkpoint.update(
        {
            "after_path": rows[-1][0].path,
            "file_order": first_order + len(rows),
            "volume_sequence": sequence + 1,
            "volume_hash_state": digest.export_state(),
        }
    )
    _set_verification_checkpoint(record, checkpoint)


def _next_verification_journal(
    session: Session,
    collection_id: int,
    after_journal_id: str | None,
) -> CollectionProvenanceJournalRecord | None:
    statement = select(CollectionProvenanceJournalRecord).where(
        CollectionProvenanceJournalRecord.collection_id == collection_id
    )
    if after_journal_id is not None:
        statement = statement.where(CollectionProvenanceJournalRecord.journal_id > after_journal_id)
    return session.scalar(statement.order_by(CollectionProvenanceJournalRecord.journal_id).limit(1))


def _advance_verification_journal_identity(
    session: Session,
    record: CollectionProvenanceVerificationRecord,
    collection: CollectionRecord,
) -> None:
    checkpoint = _verification_checkpoint(record)
    journal_id = checkpoint.get("current_journal_id")
    journal = (
        session.get(
            CollectionProvenanceJournalRecord,
            (record.collection_id, str(journal_id)),
        )
        if journal_id is not None
        else _next_verification_journal(
            session,
            record.collection_id,
            checkpoint.get("after_journal_id"),
        )
    )
    if journal is None:
        if int(checkpoint["journal_count"]) != int(checkpoint["journals"]):
            raise InvalidState("provenance identity omitted journals")
        digest = CheckpointSHA256.from_state(str(checkpoint["volume_hash_state"]))
        update_ordered_volume_commitment(
            digest,
            ProvenanceTerminalDocument(
                archive_generation=collection.archive_generation,
                archive_tree_sha256=str(checkpoint["tree_sha256"]),
                sequence=int(checkpoint["volume_sequence"]),
            ),
        )
        root = ProvenanceRootDocument(
            archive_generation=collection.archive_generation,
            archive_tree_sha256=str(checkpoint["tree_sha256"]),
            ordered_volume_sha256=digest.hexdigest(),
        )
        if root.identity != collection.provenance_identity:
            raise InvalidState("catalog provenance identity does not match exact bytes")
        checkpoint.update(
            {
                "after_journal_id": None,
                "current_journal_id": None,
                "journal_offset": 0,
                "journal_sequence": 0,
                "journal_previous_entry_id": None,
                "journal_previous_json_sha256": None,
                "journal_primary_lineage_id": None,
                "journal_primary_binding_json": None,
                "journal_entity_counts": {},
                "journal_hash_state": CheckpointSHA256().export_state(),
            }
        )
        checkpoint.pop("volume_hash_state", None)
        record.phase = "journal-entries"
        _set_verification_checkpoint(record, checkpoint)
        return
    if journal_id is None:
        checkpoint["current_journal_id"] = journal.journal_id
        checkpoint["journal_offset"] = 0
    offset = int(checkpoint["journal_offset"])
    if offset == journal.bytes:
        checkpoint["after_journal_id"] = journal.journal_id
        checkpoint["current_journal_id"] = None
        checkpoint["journal_offset"] = 0
        checkpoint["journal_count"] = int(checkpoint["journal_count"]) + 1
        _set_verification_checkpoint(record, checkpoint)
        return
    size = min(PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX, journal.bytes - offset)
    payload = b"".join(
        _iter_journal_chunk_range(
            session,
            record.collection_id,
            journal.journal_id,
            offset=offset,
            size=size,
        )
    )
    if len(payload) != size:
        raise InvalidState("catalog provenance journal byte count differs")
    sequence = int(checkpoint["volume_sequence"])
    document = ProvenanceVolumeDocument(
        archive_generation=collection.archive_generation,
        archive_tree_sha256=str(checkpoint["tree_sha256"]),
        sequence=sequence,
        payload=ProvenancePayloadIdentity(
            kind="journal",
            path=(f"provenance/payloads/volume-{format_provenance_sequence(sequence)}.bin.age"),
            bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        ),
        journal_id=journal.journal_id,
        journal_offset=offset,
        journal_bytes=journal.bytes,
        journal_sha256=journal.sha256,
    )
    digest = CheckpointSHA256.from_state(str(checkpoint["volume_hash_state"]))
    _update_volume_digest(digest, document)
    checkpoint.update(
        {
            "journal_offset": offset + size,
            "volume_sequence": sequence + 1,
            "volume_hash_state": digest.export_state(),
        }
    )
    _set_verification_checkpoint(record, checkpoint)


def _verification_journal_entry_bytes(
    session: Session,
    journal: CollectionProvenanceJournalRecord,
    *,
    offset: int,
) -> bytes:
    rows = session.execute(
        select(
            CollectionProvenanceJournalChunkRecord.byte_offset,
            CollectionProvenanceJournalChunkRecord.content,
        )
        .where(
            CollectionProvenanceJournalChunkRecord.collection_id == journal.collection_id,
            CollectionProvenanceJournalChunkRecord.journal_id == journal.journal_id,
            CollectionProvenanceJournalChunkRecord.byte_offset
            + func.length(CollectionProvenanceJournalChunkRecord.content)
            > offset,
        )
        .order_by(CollectionProvenanceJournalChunkRecord.byte_offset)
        .limit(PROVENANCE_JOURNAL_ENTRY_BYTES_MAX // _PROVENANCE_JOURNAL_CHUNK_BYTES + 2)
    )
    content = bytearray()
    expected = offset
    found_boundary = False
    for row_offset, raw_content in rows:
        row_start = int(row_offset)
        raw = bytes(raw_content)
        start = max(0, expected - row_start)
        if row_start > expected or start >= len(raw):
            raise InvalidState("catalog provenance journal chunks are not contiguous")
        content.extend(raw[start:])
        expected = row_start + len(raw)
        boundary = content.find(b"\x1e", 1)
        if boundary >= 0:
            del content[boundary:]
            found_boundary = True
            break
        if len(content) > PROVENANCE_JOURNAL_ENTRY_BYTES_MAX:
            raise InvalidState("provenance journal entry exceeds its bounded record contract")
        if expected >= journal.bytes:
            break
    if not content or (expected < journal.bytes and not found_boundary):
        raise InvalidState("provenance journal entry boundary is unavailable")
    if len(content) > PROVENANCE_JOURNAL_ENTRY_BYTES_MAX:
        raise InvalidState("provenance journal entry exceeds its bounded record contract")
    return bytes(content)


def _put_verification_agent(
    session: Session,
    record: CollectionProvenanceVerificationRecord,
    journal_id: str,
    agent_id: str,
) -> None:
    key = (record.collection_id, journal_id, agent_id)
    if session.get(CollectionProvenanceVerificationAgentRecord, key) is None:
        session.add(
            CollectionProvenanceVerificationAgentRecord(
                collection_id=record.collection_id,
                journal_id=journal_id,
                agent_id=agent_id,
            )
        )


def _put_verification_entity(
    session: Session,
    record: CollectionProvenanceVerificationRecord,
    journal_id: str,
    *,
    entity_type: str,
    entity_id: str,
    entry_id: str,
    document_json: str,
) -> None:
    key = (record.collection_id, journal_id, entity_type, entity_id)
    existing = session.get(CollectionProvenanceVerificationEntityRecord, key)
    if existing is None:
        session.add(
            CollectionProvenanceVerificationEntityRecord(
                collection_id=record.collection_id,
                journal_id=journal_id,
                entity_type=entity_type,
                entity_id=entity_id,
                entry_id=entry_id,
                document_json=document_json,
            )
        )
        return
    if entity_type == "states" and existing.document_json != document_json:
        raise InvalidState(f"provenance journal redefines state {entity_id}")
    existing.entry_id = entry_id
    existing.document_json = document_json


def _validate_verification_binding(
    session: Session,
    record: CollectionProvenanceVerificationRecord,
    journal_id: str,
    *,
    binding_json: str,
) -> None:
    binding = json.loads(binding_json)
    operation = binding.get("operation")
    if operation == "unbind":
        return
    established_by = binding.get("established_by_capture_id") or binding.get(
        "established_by_activity_id"
    )
    if (
        not isinstance(established_by, str)
        or session.scalar(
            select(CollectionProvenanceVerificationEntityRecord.entity_id)
            .where(
                CollectionProvenanceVerificationEntityRecord.collection_id == record.collection_id,
                CollectionProvenanceVerificationEntityRecord.journal_id == journal_id,
                CollectionProvenanceVerificationEntityRecord.entity_type.in_(
                    ("captures", "activities")
                ),
                CollectionProvenanceVerificationEntityRecord.entity_id == established_by,
            )
            .limit(1)
        )
        is None
    ):
        raise InvalidState("provenance payload binding has no exact establishing event")
    state = binding.get("state")
    if (
        not isinstance(state, dict)
        or state.get("scope") != "local"
        or not isinstance(state.get("id"), str)
        or session.get(
            CollectionProvenanceVerificationEntityRecord,
            (record.collection_id, journal_id, "states", str(state.get("id"))),
        )
        is None
    ):
        raise InvalidState("provenance payload binding references an absent local state")


def _finish_verification_journal(
    session: Session,
    record: CollectionProvenanceVerificationRecord,
    journal: CollectionProvenanceJournalRecord,
    checkpoint: dict[str, Any],
) -> None:
    digest = CheckpointSHA256.from_state(str(checkpoint["journal_hash_state"]))
    binding_json = checkpoint.get("journal_primary_binding_json")
    primary_lineage_id = checkpoint.get("journal_primary_lineage_id")
    if not isinstance(binding_json, str) or not isinstance(primary_lineage_id, str):
        raise InvalidState("provenance journal has no terminal primary binding")
    binding = json.loads(binding_json)
    state = binding.get("state")
    if not isinstance(state, dict) or not isinstance(state.get("id"), str):
        raise InvalidState("provenance journal primary binding is invalid")
    state_row = session.get(
        CollectionProvenanceVerificationEntityRecord,
        (record.collection_id, journal.journal_id, "states", str(state["id"])),
    )
    if state_row is None:
        raise InvalidState("provenance journal current state is absent")
    current_state_id, current_path, current_bytes, current_sha256 = (
        resolve_incremental_journal_current_state(
            primary_lineage_id=primary_lineage_id,
            binding_json=binding_json,
            state_json=state_row.document_json,
        )
    )
    agent_count = int(
        session.scalar(
            select(func.count())
            .select_from(CollectionProvenanceVerificationAgentRecord)
            .where(
                CollectionProvenanceVerificationAgentRecord.collection_id == record.collection_id,
                CollectionProvenanceVerificationAgentRecord.journal_id == journal.journal_id,
            )
        )
        or 0
    )
    entity_counts_json = json.dumps(
        checkpoint["journal_entity_counts"], sort_keys=True, separators=(",", ":")
    )
    if (
        digest.hexdigest() != journal.sha256
        or int(checkpoint["journal_offset"]) != journal.bytes
        or int(checkpoint["journal_sequence"]) != journal.entries
        or agent_count != journal.agent_count
        or entity_counts_json != journal.entity_counts_json
        or current_state_id != journal.current_state_id
        or current_path != journal.current_path
        or current_bytes != journal.current_bytes
        or current_sha256 != journal.current_sha256
    ):
        raise InvalidState("catalog provenance journal summary differs from exact bytes")


def _advance_verification_journal_entry(
    session: Session,
    record: CollectionProvenanceVerificationRecord,
) -> None:
    checkpoint = _verification_checkpoint(record)
    journal_id = checkpoint.get("current_journal_id")
    journal = (
        session.get(
            CollectionProvenanceJournalRecord,
            (record.collection_id, str(journal_id)),
        )
        if journal_id is not None
        else _next_verification_journal(
            session,
            record.collection_id,
            checkpoint.get("after_journal_id"),
        )
    )
    if journal is None:
        record.phase = "references"
        checkpoint.update({"comparison_stage": "agents", "comparison_cursor": None})
        _set_verification_checkpoint(record, checkpoint)
        return
    if journal_id is None:
        checkpoint.update(
            {
                "current_journal_id": journal.journal_id,
                "journal_offset": 0,
                "journal_sequence": 0,
                "journal_previous_entry_id": None,
                "journal_previous_json_sha256": None,
                "journal_primary_lineage_id": None,
                "journal_primary_binding_json": None,
                "journal_entity_counts": {},
                "journal_hash_state": CheckpointSHA256().export_state(),
            }
        )
    offset = int(checkpoint["journal_offset"])
    if offset == journal.bytes:
        _finish_verification_journal(session, record, journal, checkpoint)
        checkpoint.update(
            {
                "after_journal_id": journal.journal_id,
                "current_journal_id": None,
                "journal_offset": 0,
                "journal_sequence": 0,
                "journal_previous_entry_id": None,
                "journal_previous_json_sha256": None,
                "journal_primary_lineage_id": None,
                "journal_primary_binding_json": None,
                "journal_entity_counts": {},
                "journal_hash_state": CheckpointSHA256().export_state(),
            }
        )
        _set_verification_checkpoint(record, checkpoint)
        return
    encoded = _verification_journal_entry_bytes(session, journal, offset=offset)
    try:
        projected = validate_incremental_journal_entry(
            encoded,
            sequence=int(checkpoint["journal_sequence"]),
            journal_id=journal.journal_id,
            previous_entry_id=checkpoint.get("journal_previous_entry_id"),
            previous_json_sha256=checkpoint.get("journal_previous_json_sha256"),
        )
    except ProvenanceValidationError as exc:
        raise InvalidState(str(exc)) from exc
    entry_id = str(projected.frame.document["id"])
    entry_key = (record.collection_id, journal.journal_id, entry_id)
    if session.get(CollectionProvenanceVerificationEntryRecord, entry_key) is not None:
        raise InvalidState(f"provenance journal repeats entry identity {entry_id}")
    session.add(
        CollectionProvenanceVerificationEntryRecord(
            collection_id=record.collection_id,
            journal_id=journal.journal_id,
            entry_id=entry_id,
            json_sha256=projected.frame.sha256,
        )
    )
    if projected.primary_lineage_id is not None:
        if checkpoint.get("journal_primary_lineage_id") is not None:
            raise InvalidState("provenance journal repeats its initialization policy")
        checkpoint["journal_primary_lineage_id"] = projected.primary_lineage_id
    for agent_id in projected.agents:
        _put_verification_agent(session, record, journal.journal_id, agent_id)
    for entity_type, entity_id, document_json in projected.entities:
        _put_verification_entity(
            session,
            record,
            journal.journal_id,
            entity_type=entity_type,
            entity_id=entity_id,
            entry_id=entry_id,
            document_json=document_json,
        )
    session.flush()
    for role, operation, binding_json in projected.bindings:
        _validate_verification_binding(
            session,
            record,
            journal.journal_id,
            binding_json=binding_json,
        )
        if role == "co_resident_primary_payload":
            checkpoint["journal_primary_binding_json"] = (
                None if operation == "unbind" else binding_json
            )
    for reference in projected.external_states:
        key = (
            record.collection_id,
            journal.journal_id,
            reference.journal_id,
            reference.entry_id,
            reference.state_id,
        )
        existing = session.get(CollectionProvenanceVerificationExternalStateRecord, key)
        if existing is not None:
            if existing.entry_json_sha256 != reference.entry_json_sha256:
                raise InvalidState("provenance external-state identity is redefined")
        else:
            session.add(
                CollectionProvenanceVerificationExternalStateRecord(
                    collection_id=record.collection_id,
                    from_journal_id=journal.journal_id,
                    to_journal_id=reference.journal_id,
                    entry_id=reference.entry_id,
                    state_id=reference.state_id,
                    entry_json_sha256=reference.entry_json_sha256,
                )
            )
    counts = checkpoint["journal_entity_counts"]
    if not isinstance(counts, dict):
        raise InvalidState("provenance verification entity-count checkpoint is invalid")
    for entity_type, count in projected.entity_counts:
        counts[entity_type] = int(counts.get(entity_type, 0)) + count
    digest = CheckpointSHA256.from_state(str(checkpoint["journal_hash_state"]))
    digest.update(encoded)
    checkpoint.update(
        {
            "journal_offset": offset + len(encoded),
            "journal_sequence": int(checkpoint["journal_sequence"]) + 1,
            "journal_previous_entry_id": entry_id,
            "journal_previous_json_sha256": projected.frame.sha256,
            "journal_hash_state": digest.export_state(),
        }
    )
    _set_verification_checkpoint(record, checkpoint)


def _keyset_after(columns: tuple[Any, ...], cursor: object) -> Any:
    if not isinstance(cursor, list) or len(cursor) != len(columns):
        return None
    return tuple_(*columns) > tuple(cursor)


def _comparison_rows(
    session: Session,
    record: CollectionProvenanceVerificationRecord,
    *,
    stage: str,
    cursor: object,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], int]:
    collection_id = record.collection_id
    limit = 512
    key_columns: tuple[Any, ...]
    actual_columns: tuple[Any, ...]
    expected: Any
    actual: Any
    if stage == "agents":
        key_columns = (
            CollectionProvenanceVerificationAgentRecord.journal_id,
            CollectionProvenanceVerificationAgentRecord.agent_id,
        )
        expected = select(*key_columns).where(
            CollectionProvenanceVerificationAgentRecord.collection_id == collection_id
        )
        actual_columns = (
            CollectionProvenanceJournalAgentRecord.journal_id,
            CollectionProvenanceJournalAgentRecord.agent_id,
        )
        actual = select(*actual_columns).where(
            CollectionProvenanceJournalAgentRecord.collection_id == collection_id
        )
    elif stage == "entities":
        key_columns = (
            CollectionProvenanceVerificationEntityRecord.journal_id,
            CollectionProvenanceVerificationEntityRecord.entity_type,
            CollectionProvenanceVerificationEntityRecord.entity_id,
        )
        expected = select(
            *key_columns,
            CollectionProvenanceVerificationEntityRecord.entry_id,
            CollectionProvenanceVerificationEntityRecord.document_json,
        ).where(CollectionProvenanceVerificationEntityRecord.collection_id == collection_id)
        actual_columns = (
            CollectionProvenanceEntityRecord.journal_id,
            CollectionProvenanceEntityRecord.entity_type,
            CollectionProvenanceEntityRecord.entity_id,
        )
        actual = select(
            *actual_columns,
            CollectionProvenanceEntityRecord.entry_id,
            CollectionProvenanceEntityRecord.document_json,
        ).where(CollectionProvenanceEntityRecord.collection_id == collection_id)
    elif stage == "external-states":
        key_columns = (
            CollectionProvenanceVerificationExternalStateRecord.from_journal_id,
            CollectionProvenanceVerificationExternalStateRecord.to_journal_id,
            CollectionProvenanceVerificationExternalStateRecord.entry_id,
            CollectionProvenanceVerificationExternalStateRecord.state_id,
        )
        expected = select(
            *key_columns,
            CollectionProvenanceVerificationExternalStateRecord.entry_json_sha256,
        ).where(CollectionProvenanceVerificationExternalStateRecord.collection_id == collection_id)
        actual_columns = (
            CollectionProvenanceExternalStateReferenceRecord.from_journal_id,
            CollectionProvenanceExternalStateReferenceRecord.to_journal_id,
            CollectionProvenanceExternalStateReferenceRecord.entry_id,
            CollectionProvenanceExternalStateReferenceRecord.state_id,
        )
        actual = select(
            *actual_columns,
            CollectionProvenanceExternalStateReferenceRecord.entry_json_sha256,
        ).where(CollectionProvenanceExternalStateReferenceRecord.collection_id == collection_id)
    else:
        raise InvalidState("provenance verification comparison stage is invalid")
    after_expected = _keyset_after(key_columns, cursor)
    after_actual = _keyset_after(actual_columns, cursor)
    if after_expected is not None:
        expected = expected.where(after_expected)
        actual = actual.where(after_actual)
    return (
        list(session.execute(expected.order_by(*key_columns).limit(limit)).tuples()),
        list(session.execute(actual.order_by(*actual_columns).limit(limit)).tuples()),
        len(key_columns),
    )


def _advance_verification_projection_comparison(
    session: Session,
    record: CollectionProvenanceVerificationRecord,
) -> None:
    checkpoint = _verification_checkpoint(record)
    stage = str(checkpoint.get("comparison_stage", "agents"))
    expected, actual, key_length = _comparison_rows(
        session,
        record,
        stage=stage,
        cursor=checkpoint.get("comparison_cursor"),
    )
    if expected != actual:
        raise InvalidState(f"catalog provenance {stage} projection differs from exact journals")
    if expected:
        checkpoint["comparison_cursor"] = list(expected[-1][:key_length])
        _set_verification_checkpoint(record, checkpoint)
        return
    if stage == "agents":
        checkpoint.update({"comparison_stage": "entities", "comparison_cursor": None})
    elif stage == "entities":
        checkpoint.update({"comparison_stage": "external-states", "comparison_cursor": None})
    else:
        checkpoint.update(
            {
                "reachability_stage": "seed",
                "reachability_after_journal_id": None,
            }
        )
        checkpoint.pop("comparison_stage", None)
        checkpoint.pop("comparison_cursor", None)
        record.phase = "reachability"
    _set_verification_checkpoint(record, checkpoint)


def _put_verification_reachable(
    session: Session,
    collection_id: int,
    journal_id: str,
) -> None:
    if (
        session.get(
            CollectionProvenanceVerificationReachabilityRecord,
            (collection_id, journal_id),
        )
        is None
    ):
        session.add(
            CollectionProvenanceVerificationReachabilityRecord(
                collection_id=collection_id,
                journal_id=journal_id,
            )
        )


def _advance_verification_reachability(
    session: Session,
    record: CollectionProvenanceVerificationRecord,
    collection: CollectionRecord,
) -> dict[str, Any] | None:
    checkpoint = _verification_checkpoint(record)
    stage = str(checkpoint.get("reachability_stage", "seed"))
    if stage == "seed":
        after = checkpoint.get("reachability_after_journal_id")
        seed_statement = (
            select(CollectionFileProvenanceRecord.journal_id)
            .where(
                CollectionFileProvenanceRecord.collection_id == record.collection_id,
                CollectionFileProvenanceRecord.status == "captured",
                CollectionFileProvenanceRecord.journal_id.is_not(None),
            )
            .distinct()
            .order_by(CollectionFileProvenanceRecord.journal_id)
            .limit(512)
        )
        if isinstance(after, str):
            seed_statement = seed_statement.where(CollectionFileProvenanceRecord.journal_id > after)
        journal_ids = [str(value) for value in session.scalars(seed_statement)]
        if journal_ids:
            for journal_id in journal_ids:
                _put_verification_reachable(session, record.collection_id, journal_id)
            checkpoint["reachability_after_journal_id"] = journal_ids[-1]
            _set_verification_checkpoint(record, checkpoint)
            return None
        checkpoint["reachability_stage"] = "expand"
        _set_verification_checkpoint(record, checkpoint)
        return None
    reachable = session.scalar(
        select(CollectionProvenanceVerificationReachabilityRecord)
        .where(
            CollectionProvenanceVerificationReachabilityRecord.collection_id
            == record.collection_id,
            CollectionProvenanceVerificationReachabilityRecord.expanded.is_(False),
        )
        .order_by(CollectionProvenanceVerificationReachabilityRecord.journal_id)
        .limit(1)
    )
    if reachable is not None:
        if (
            session.scalar(
                select(CollectionProvenanceVerificationEntryRecord.entry_id)
                .where(
                    CollectionProvenanceVerificationEntryRecord.collection_id
                    == record.collection_id,
                    CollectionProvenanceVerificationEntryRecord.journal_id == reachable.journal_id,
                )
                .limit(1)
            )
            is None
        ):
            raise InvalidState("provenance captured closure references an absent journal")
        columns = (
            CollectionProvenanceVerificationExternalStateRecord.to_journal_id,
            CollectionProvenanceVerificationExternalStateRecord.entry_id,
            CollectionProvenanceVerificationExternalStateRecord.state_id,
        )
        reference_statement = (
            select(CollectionProvenanceVerificationExternalStateRecord)
            .where(
                CollectionProvenanceVerificationExternalStateRecord.collection_id
                == record.collection_id,
                CollectionProvenanceVerificationExternalStateRecord.from_journal_id
                == reachable.journal_id,
            )
            .order_by(*columns)
            .limit(512)
        )
        cursor = (
            reachable.after_to_journal_id,
            reachable.after_entry_id,
            reachable.after_state_id,
        )
        if all(isinstance(value, str) for value in cursor):
            reference_statement = reference_statement.where(tuple_(*columns) > cursor)
        references = list(session.scalars(reference_statement))
        if references:
            for reference in references:
                entry = session.get(
                    CollectionProvenanceVerificationEntryRecord,
                    (
                        record.collection_id,
                        reference.to_journal_id,
                        reference.entry_id,
                    ),
                )
                state = session.get(
                    CollectionProvenanceVerificationEntityRecord,
                    (
                        record.collection_id,
                        reference.to_journal_id,
                        "states",
                        reference.state_id,
                    ),
                )
                if (
                    entry is None
                    or entry.json_sha256 != reference.entry_json_sha256
                    or state is None
                ):
                    raise InvalidState("provenance external state does not resolve exactly")
                _put_verification_reachable(
                    session,
                    record.collection_id,
                    reference.to_journal_id,
                )
            last = references[-1]
            reachable.after_to_journal_id = last.to_journal_id
            reachable.after_entry_id = last.entry_id
            reachable.after_state_id = last.state_id
            return None
        reachable.expanded = True
        return None
    reachable_count = int(
        session.scalar(
            select(func.count())
            .select_from(CollectionProvenanceVerificationReachabilityRecord)
            .where(
                CollectionProvenanceVerificationReachabilityRecord.collection_id
                == record.collection_id
            )
        )
        or 0
    )
    if reachable_count != int(checkpoint["journals"]):
        raise InvalidState("provenance contains an unreachable journal")
    record.phase = "cleanup"
    checkpoint["cleanup_stage"] = "reachability"
    checkpoint["restart_after_cleanup"] = False
    _set_verification_checkpoint(record, checkpoint)
    return None


def _verification_result(
    record: CollectionProvenanceVerificationRecord,
    collection: CollectionRecord,
) -> dict[str, Any]:
    checkpoint = _verification_checkpoint(record)
    return {
        "collection_id": record.collection_id,
        "valid": True,
        "provenance_mode": collection.provenance_mode,
        "provenance_identity": collection.provenance_identity,
        "files": int(checkpoint["files"]),
        "journals": int(checkpoint["journals"]),
        "entities": int(checkpoint["entities"]),
    }


_VERIFICATION_CLEANUP_STAGES: tuple[tuple[str, type[Any], tuple[Any, ...]], ...] = (
    (
        "reachability",
        CollectionProvenanceVerificationReachabilityRecord,
        (
            CollectionProvenanceVerificationReachabilityRecord.collection_id,
            CollectionProvenanceVerificationReachabilityRecord.journal_id,
        ),
    ),
    (
        "external-states",
        CollectionProvenanceVerificationExternalStateRecord,
        (
            CollectionProvenanceVerificationExternalStateRecord.collection_id,
            CollectionProvenanceVerificationExternalStateRecord.from_journal_id,
            CollectionProvenanceVerificationExternalStateRecord.to_journal_id,
            CollectionProvenanceVerificationExternalStateRecord.entry_id,
            CollectionProvenanceVerificationExternalStateRecord.state_id,
        ),
    ),
    (
        "entities",
        CollectionProvenanceVerificationEntityRecord,
        (
            CollectionProvenanceVerificationEntityRecord.collection_id,
            CollectionProvenanceVerificationEntityRecord.journal_id,
            CollectionProvenanceVerificationEntityRecord.entity_type,
            CollectionProvenanceVerificationEntityRecord.entity_id,
        ),
    ),
    (
        "entries",
        CollectionProvenanceVerificationEntryRecord,
        (
            CollectionProvenanceVerificationEntryRecord.collection_id,
            CollectionProvenanceVerificationEntryRecord.journal_id,
            CollectionProvenanceVerificationEntryRecord.entry_id,
        ),
    ),
    (
        "agents",
        CollectionProvenanceVerificationAgentRecord,
        (
            CollectionProvenanceVerificationAgentRecord.collection_id,
            CollectionProvenanceVerificationAgentRecord.journal_id,
            CollectionProvenanceVerificationAgentRecord.agent_id,
        ),
    ),
)


def _advance_verification_cleanup(
    session: Session,
    record: CollectionProvenanceVerificationRecord,
    collection: CollectionRecord,
) -> dict[str, Any] | None:
    checkpoint = _verification_checkpoint(record)
    stage = str(checkpoint.get("cleanup_stage", "reachability"))
    stages = [name for name, _model, _columns in _VERIFICATION_CLEANUP_STAGES]
    if stage not in stages:
        raise InvalidState("provenance verification cleanup checkpoint is invalid")
    stage_index = stages.index(stage)
    _name, model, columns = _VERIFICATION_CLEANUP_STAGES[stage_index]
    keys = list(
        session.execute(
            select(*columns).where(columns[0] == record.collection_id).order_by(*columns).limit(512)
        ).tuples()
    )
    if keys:
        session.execute(delete(model).where(tuple_(*columns).in_(keys)))
        return None
    if stage_index + 1 < len(_VERIFICATION_CLEANUP_STAGES):
        checkpoint["cleanup_stage"] = stages[stage_index + 1]
        _set_verification_checkpoint(record, checkpoint)
        return None
    restart = bool(checkpoint.pop("restart_after_cleanup", False))
    checkpoint.pop("cleanup_stage", None)
    if restart:
        record.phase = "metadata"
        record.checkpoint_json = "{}"
        return None
    record.phase = "complete"
    _set_verification_checkpoint(record, checkpoint)
    return _verification_result(record, collection)


def _binding_volume_document(
    *,
    archive_generation: str,
    tree_sha256: str,
    sequence: int,
    first_file_order: int,
    bindings: list[FileProvenanceBinding],
) -> ProvenanceVolumeDocument:
    payload = binding_segment_bytes(
        first_file_order=first_file_order,
        files=[_binding_mapping(binding) for binding in bindings],
    )
    return ProvenanceVolumeDocument(
        archive_generation=archive_generation,
        archive_tree_sha256=tree_sha256,
        sequence=sequence,
        payload=ProvenancePayloadIdentity(
            kind="bindings",
            path=(f"provenance/payloads/volume-{format_provenance_sequence(sequence)}.bin.age"),
            bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        ),
        first_file_order=first_file_order,
        file_count=len(bindings),
    )


def _binding_mapping(binding: FileProvenanceBinding) -> dict[str, object]:
    value: dict[str, object] = {
        "path": binding.path,
        "bytes": binding.bytes,
        "sha256": binding.sha256,
        "status": binding.status,
    }
    if binding.status == "captured":
        value.update(
            {
                "journal_id": binding.journal_id,
                "current_state_id": binding.current_state_id,
            }
        )
    else:
        value["omission_reason"] = binding.omission_reason
    return value


def _update_volume_digest(
    digest: Any,
    document: ProvenanceVolumeDocument,
) -> None:
    update_ordered_volume_commitment(digest, document)


def _iter_journal_chunks(
    session: Session,
    collection_id: int,
    journal_id: str,
) -> Iterator[bytes]:
    statement = (
        select(CollectionProvenanceJournalChunkRecord.content)
        .where(
            CollectionProvenanceJournalChunkRecord.collection_id == collection_id,
            CollectionProvenanceJournalChunkRecord.journal_id == journal_id,
        )
        .order_by(CollectionProvenanceJournalChunkRecord.ordinal)
        .execution_options(yield_per=16)
    )
    yield from session.scalars(statement)


def _iter_journal_chunk_range(
    session: Session,
    collection_id: int,
    journal_id: str,
    *,
    offset: int,
    size: int,
) -> Iterator[bytes]:
    statement = (
        select(
            CollectionProvenanceJournalChunkRecord.byte_offset,
            CollectionProvenanceJournalChunkRecord.content,
        )
        .where(
            CollectionProvenanceJournalChunkRecord.collection_id == collection_id,
            CollectionProvenanceJournalChunkRecord.journal_id == journal_id,
            CollectionProvenanceJournalChunkRecord.byte_offset
            + func.length(CollectionProvenanceJournalChunkRecord.content)
            > offset,
            CollectionProvenanceJournalChunkRecord.byte_offset < offset + size,
        )
        .order_by(CollectionProvenanceJournalChunkRecord.byte_offset)
        .execution_options(yield_per=16)
    )
    expected = offset
    remaining = size
    for row in session.execute(statement):
        content = bytes(row.content)
        row_offset = int(row.byte_offset)
        start = max(0, expected - row_offset)
        if row_offset > expected or start >= len(content):
            raise RuntimeError("provenance journal chunks are not contiguous")
        current = content[start : start + remaining]
        if current:
            yield current
            expected += len(current)
            remaining -= len(current)
        if remaining == 0:
            return
    if remaining:
        raise RuntimeError("provenance journal range is unavailable")


def _iter_journal_ids(session: Session, collection_id: int) -> Iterator[str]:
    after: str | None = None
    while True:
        statement = select(CollectionProvenanceJournalRecord.journal_id).where(
            CollectionProvenanceJournalRecord.collection_id == collection_id
        )
        if after is not None:
            statement = statement.where(CollectionProvenanceJournalRecord.journal_id > after)
        batch = list(
            session.scalars(
                statement.order_by(CollectionProvenanceJournalRecord.journal_id).limit(100)
            )
        )
        if not batch:
            return
        yield from batch
        after = batch[-1]


def _canonical_text_order(session: Session, column: Any) -> Any:
    bind = session.get_bind()
    return column.collate("C" if bind.dialect.name == "postgresql" else "BINARY")


def _provenance_file_statement(
    *,
    collection_id: int,
    principal: ApplicationPrincipal,
    q: str | None,
    status: str | None,
    sort: str,
    order: str,
) -> tuple[Any, tuple[Any, ...]]:
    joined = (
        select(CollectionFileRecord, CollectionFileProvenanceRecord)
        .outerjoin(
            CollectionFileProvenanceRecord,
            (CollectionFileProvenanceRecord.collection_id == CollectionFileRecord.collection_id)
            & (CollectionFileProvenanceRecord.path == CollectionFileRecord.path),
        )
        .where(CollectionFileRecord.collection_id == collection_id)
    )
    joined = joined.where(
        artifact_scope_filter(
            CollectionFileRecord.collection_id,
            CollectionFileRecord.path,
            principal,
        )
    )
    if q:
        joined = joined.where(
            CollectionFileRecord.path_search_text.like(
                _like_pattern(text_search_key(q)),
                escape="\\",
            )
        )
    effective_status = CollectionFileRecord.provenance_status
    if status is not None:
        joined = joined.where(effective_status == status)
    sort_column = {
        "path": CollectionFileRecord.path_sort_key,
        "bytes": CollectionFileRecord.bytes,
        "status": effective_status,
    }[sort]
    key_columns = tuple(dict.fromkeys((sort_column, CollectionFileRecord.path_sort_key)))
    return joined, key_columns


def _provenance_file_position(
    file: CollectionFileRecord,
    *,
    sort: str,
) -> tuple[BrowseScalar, ...]:
    if sort == "path":
        value: BrowseScalar = file.path_sort_key
    elif sort == "bytes":
        value = file.bytes
    else:
        value = str(file.provenance_status)
    return (value,) if sort == "path" else (value, file.path_sort_key)


def _authorized_collection(
    session: Session,
    collection_id: int,
    principal: ApplicationPrincipal,
    *,
    permission: str = PROVENANCE_READ,
) -> CollectionRecord:
    record = session.get(CollectionRecord, collection_id)
    if (
        record is None
        or not record.is_published
        or not principal.allows_collection(CATALOG_READ, collection_id)
        or not principal.allows_collection(permission, collection_id)
    ):
        raise NotFound(f"collection not found: {collection_id}")
    return record


def _shown_file(
    session: Session,
    collection_id: int,
    path: str,
    principal: ApplicationPrincipal,
) -> dict[str, Any]:
    collection = _authorized_collection(session, collection_id, principal)
    require_artifact_scope(session, principal, collection_id, path)
    row = session.execute(
        select(CollectionFileRecord, CollectionFileProvenanceRecord)
        .outerjoin(
            CollectionFileProvenanceRecord,
            (CollectionFileProvenanceRecord.collection_id == CollectionFileRecord.collection_id)
            & (CollectionFileProvenanceRecord.path == CollectionFileRecord.path),
        )
        .where(
            CollectionFileRecord.collection_id == collection_id,
            CollectionFileRecord.path == path,
        )
    ).one_or_none()
    if row is None:
        raise NotFound(f"collection file not found: {collection_id}::{path}")
    payload = _file_payload(row[0], row[1], collection)
    if row[1] is not None and row[1].journal_id is not None:
        journal = session.get(
            CollectionProvenanceJournalRecord,
            (collection_id, row[1].journal_id),
        )
        if journal is None:
            raise InvalidState("captured provenance journal is missing")
        payload["journal"] = _journal_payload(journal)
    return payload


def _reachable_journals(
    seed: Any,
    *,
    collection_id: int,
    name: str,
) -> Any:
    reachable = seed.cte(name, recursive=True)
    references = CollectionProvenanceExternalStateReferenceRecord.__table__.alias(
        f"{name}_references"
    )
    reachable = reachable.union(
        select(references.c.to_journal_id.label("journal_id")).select_from(
            references.join(
                reachable,
                and_(
                    references.c.collection_id == collection_id,
                    references.c.from_journal_id == reachable.c.journal_id,
                ),
            )
        )
    )
    return reachable


def _trace_statements(collection_id: int, journal_id: str) -> tuple[Any, Any]:
    reachable = _reachable_journals(
        select(CollectionProvenanceJournalRecord.journal_id.label("journal_id")).where(
            CollectionProvenanceJournalRecord.collection_id == collection_id,
            CollectionProvenanceJournalRecord.journal_id == journal_id,
        ),
        collection_id=collection_id,
        name="trace_reachable_journals",
    )
    journals = (
        select(CollectionProvenanceJournalRecord)
        .join(
            reachable,
            reachable.c.journal_id == CollectionProvenanceJournalRecord.journal_id,
        )
        .where(CollectionProvenanceJournalRecord.collection_id == collection_id)
        .order_by(CollectionProvenanceJournalRecord.journal_id)
    )
    references = (
        select(CollectionProvenanceExternalStateReferenceRecord)
        .join(
            reachable,
            reachable.c.journal_id
            == CollectionProvenanceExternalStateReferenceRecord.from_journal_id,
        )
        .where(CollectionProvenanceExternalStateReferenceRecord.collection_id == collection_id)
        .order_by(
            CollectionProvenanceExternalStateReferenceRecord.from_journal_id,
            CollectionProvenanceExternalStateReferenceRecord.to_journal_id,
            CollectionProvenanceExternalStateReferenceRecord.state_id,
            CollectionProvenanceExternalStateReferenceRecord.entry_id,
        )
    )
    return journals, references


def _trace_page_items(
    session: Session,
    journal_statement: Any,
    reference_statement: Any,
    *,
    position: tuple[str | int | bool | bytes | None, ...] | None,
    page_size: int,
) -> tuple[list[dict[str, Any]], tuple[str | int | bool | bytes | None, ...] | None]:
    if position is not None and (not position or position[0] not in {"journal", "reference"}):
        raise BadRequest("page token position is invalid")
    budget = page_size + 1
    items: list[dict[str, Any]] = []
    if position is None or position[0] == "journal":
        after_journal = None if position is None else position[1:]
        journals = session.scalars(
            keyset_statement(
                journal_statement.order_by(None),
                columns=(CollectionProvenanceJournalRecord.journal_id,),
                position=after_journal,
                order="asc",
                page_size=budget - 1,
            ).limit(budget)
        )
        items.extend(
            {"kind": "journal", "journal": _journal_payload(journal)} for journal in journals
        )
    remaining = budget - len(items)
    if remaining > 0:
        after_reference = (
            position[1:] if position is not None and position[0] == "reference" else None
        )
        references = session.scalars(
            keyset_statement(
                reference_statement.order_by(None),
                columns=(
                    CollectionProvenanceExternalStateReferenceRecord.from_journal_id,
                    CollectionProvenanceExternalStateReferenceRecord.to_journal_id,
                    CollectionProvenanceExternalStateReferenceRecord.state_id,
                    CollectionProvenanceExternalStateReferenceRecord.entry_id,
                ),
                position=after_reference,
                order="asc",
                page_size=max(1, remaining - 1),
            ).limit(remaining)
        )
        items.extend(
            {
                "kind": "external_state_reference",
                "reference": _external_state_reference_payload(reference),
            }
            for reference in references
        )
    return bounded_page(items, page_size=page_size, position_of=_trace_item_position)


def _trace_item_position(item: dict[str, Any]) -> tuple[str, ...]:
    if item["kind"] == "journal":
        return "journal", str(item["journal"]["journal_id"])
    reference = item["reference"]
    return (
        "reference",
        str(reference["from_journal_id"]),
        str(reference["to_journal_id"]),
        str(reference["state_id"]),
        str(reference["entry_id"]),
    )


def _require_readable_journal(
    session: Session,
    collection_id: int,
    journal_id: str,
    principal: ApplicationPrincipal,
) -> None:
    _authorized_collection(session, collection_id, principal)
    if not _journal_is_in_artifact_scope(session, collection_id, journal_id, principal):
        raise NotFound(f"provenance journal not found: {journal_id}")
    if session.get(CollectionProvenanceJournalRecord, (collection_id, journal_id)) is None:
        raise NotFound(f"provenance journal not found: {journal_id}")


def _journal_is_in_artifact_scope(
    session: Session,
    collection_id: int,
    journal_id: str,
    principal: ApplicationPrincipal,
) -> bool:
    if not principal.has_artifact_scope:
        return True
    reachable = _reachable_journals(
        select(CollectionFileProvenanceRecord.journal_id.label("journal_id"))
        .where(
            CollectionFileProvenanceRecord.collection_id == collection_id,
            artifact_scope_filter(
                CollectionFileProvenanceRecord.collection_id,
                CollectionFileProvenanceRecord.path,
                principal,
            ),
            CollectionFileProvenanceRecord.journal_id.is_not(None),
        )
        .distinct(),
        collection_id=collection_id,
        name="scope_reachable_journals",
    )
    return (
        session.scalar(
            select(reachable.c.journal_id).where(reachable.c.journal_id == journal_id).limit(1)
        )
        is not None
    )


def _file_payload(
    file: CollectionFileRecord,
    binding: CollectionFileProvenanceRecord | None,
    collection: CollectionRecord,
) -> dict[str, Any]:
    effective_status = (
        binding.status
        if binding is not None
        else "omitted"
        if collection.provenance_mode == "omitted"
        else "missing"
    )
    if file.provenance_status != effective_status:
        raise InvalidState("catalog file provenance status projection differs")
    if binding is None:
        if collection.provenance_mode != "omitted":
            raise InvalidState(f"collection file provenance is missing: {file.path}")
        provenance: dict[str, Any] = {
            "status": "omitted",
            "omission_reason": "collection-wide provenance omitted",
        }
    elif binding.status == "captured":
        provenance = {
            "status": "captured",
            "journal_id": binding.journal_id,
            "current_state_id": binding.current_state_id,
        }
    else:
        provenance = {
            "status": "omitted",
            "omission_reason": binding.omission_reason,
        }
    return {
        "collection_id": file.collection_id,
        "path": file.path,
        "bytes": file.bytes,
        "sha256": file.sha256,
        "provenance": provenance,
    }


def _journal_payload(
    record: CollectionProvenanceJournalRecord,
) -> dict[str, Any]:
    return {
        "journal_id": record.journal_id,
        "bytes": record.bytes,
        "sha256": record.sha256,
        "entries": record.entries,
        "current_state_id": record.current_state_id,
        "current_path": record.current_path,
        "current_bytes": record.current_bytes,
        "current_sha256": record.current_sha256,
        "agent_count": record.agent_count,
        "entity_counts": json.loads(record.entity_counts_json),
    }


def _verification_payload(record: CollectionProvenanceVerificationRecord) -> dict[str, Any]:
    result = json.loads(record.result_json) if record.result_json is not None else None
    return {
        "collection_id": record.collection_id,
        "state": record.state,
        "requested_at": record.requested_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "attempts": record.attempts,
        "result": result,
        "failure": record.failure,
    }


def _external_state_reference_payload(
    record: CollectionProvenanceExternalStateReferenceRecord,
) -> dict[str, str]:
    return {
        "from_journal_id": record.from_journal_id,
        "to_journal_id": record.to_journal_id,
        "state_id": record.state_id,
        "entry_id": record.entry_id,
        "entry_json_sha256": record.entry_json_sha256,
    }


def _collection_id(value: int) -> int:
    try:
        return normalize_collection_id(value)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc


def _path(value: str) -> str:
    try:
        return normalize_relpath(value)
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc


def _page_options(page_size: int, sort: str, order: str) -> None:
    validate_page_size(page_size)
    if sort not in _SORT_FIELDS:
        raise BadRequest(f"sort must be one of {', '.join(sorted(_SORT_FIELDS))}")
    if order not in _SORT_ORDERS:
        raise BadRequest("order must be asc or desc")


def _like_pattern(value: str) -> str:
    return f"%{value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"
