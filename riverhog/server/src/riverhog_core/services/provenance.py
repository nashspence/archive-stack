from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from functools import partial
from itertools import zip_longest
from typing import Any

from http_api_contracts import closed_literal_values
from riverhog_protocol import ProvenanceSort, ProvenanceStatus, SortOrder
from riverhog_protocol.errors import BadRequest, InvalidState, NotFound
from riverhog_protocol.paths import (
    PathNormalizationError,
    normalize_collection_id,
    normalize_relpath,
    text_search_key,
)
from riverhog_provenance import (
    FileProvenanceBinding,
    JournalSummary,
    ProvenanceValidationError,
    reconstruct_provenance_archive_identity,
    validate_journal_chunks,
    validate_provenance_archive,
    verify_payload_binding,
)
from sqlalchemy import and_, asc, delete, desc, func, insert, select, update
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
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionFileProvenanceRecord,
    CollectionFileRecord,
    CollectionProvenanceEntityRecord,
    CollectionProvenanceExternalStateReferenceRecord,
    CollectionProvenanceJournalAgentRecord,
    CollectionProvenanceJournalChunkRecord,
    CollectionProvenanceJournalRecord,
    CollectionProvenanceVerificationRecord,
    CollectionRecord,
)
from riverhog_core.provenance_projection import (
    ProvenanceJournalProjection,
    provenance_journal_projection,
)
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
        page: int,
        per_page: int,
        q: str | None,
        status: str | None,
        sort: str,
        order: str,
        principal: ApplicationPrincipal,
    ) -> dict[str, Any]:
        collection_id = _collection_id(collection_id)
        _page_options(page, per_page, sort, order)
        if status is not None and status not in _STATUS_VALUES:
            raise BadRequest(f"status must be one of {', '.join(sorted(_STATUS_VALUES))}")
        with read_snapshot(self._session_factory) as session:
            collection = _authorized_collection(session, collection_id, principal)
            joined = _provenance_file_statement(
                collection_id=collection_id,
                principal=principal,
                q=q,
                status=status,
                sort=sort,
                order=order,
            )
            total = int(session.scalar(select(func.count()).select_from(joined.subquery())) or 0)
            joined = joined.offset((page - 1) * per_page).limit(per_page)
            rows = session.execute(joined).all()
            return {
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": ((total + per_page - 1) // per_page if total else 0),
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
        _page_options(1, 100, sort, order)
        if status is not None and status not in _STATUS_VALUES:
            raise BadRequest(f"status must be one of {', '.join(sorted(_STATUS_VALUES))}")
        with read_snapshot(self._session_factory) as session:
            collection = _authorized_collection(session, collection_id, principal)
            statement = _provenance_file_statement(
                collection_id=collection_id,
                principal=principal,
                q=q,
                status=status,
                sort=sort,
                order=order,
            ).execution_options(yield_per=100)
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
        page: int,
        per_page: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, Any]:
        collection_id = _collection_id(collection_id)
        path = _path(path)
        _trace_page_options(page, per_page)
        with read_snapshot(self._session_factory) as session:
            shown = _shown_file(session, collection_id, path, principal)
            binding = shown["provenance"]
            if binding["status"] == "omitted":
                return {
                    **shown,
                    "page": page,
                    "per_page": per_page,
                    "total": 0,
                    "pages": 0,
                    "items": [],
                }
            journal_statement, reference_statement = _trace_statements(
                collection_id,
                str(binding["journal_id"]),
            )
            journal_total = _statement_count(session, journal_statement)
            reference_total = _statement_count(session, reference_statement)
            total = journal_total + reference_total
            items = _trace_page_items(
                session,
                journal_statement,
                reference_statement,
                journal_total=journal_total,
                offset=(page - 1) * per_page,
                limit=per_page,
            )
            return {
                **shown,
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": ((total + per_page - 1) // per_page if total else 0),
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

    def list_journal_agents(
        self,
        collection_id: int,
        journal_id: str,
        *,
        page: int,
        per_page: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, Any]:
        collection_id = _collection_id(collection_id)
        if page < 1 or per_page < 1 or per_page > 100:
            raise BadRequest("invalid provenance journal agent pagination")
        with read_snapshot(self._session_factory) as session:
            _require_readable_journal(session, collection_id, journal_id, principal)
            predicate = (
                CollectionProvenanceJournalAgentRecord.collection_id == collection_id,
                CollectionProvenanceJournalAgentRecord.journal_id == journal_id,
            )
            total = int(
                session.scalar(
                    select(func.count())
                    .select_from(CollectionProvenanceJournalAgentRecord)
                    .where(*predicate)
                )
                or 0
            )
            rows = session.scalars(
                select(CollectionProvenanceJournalAgentRecord.agent_id)
                .where(*predicate)
                .order_by(CollectionProvenanceJournalAgentRecord.agent_id)
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
            return {
                "collection_id": collection_id,
                "journal_id": journal_id,
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": (total + per_page - 1) // per_page if total else 0,
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
                result = self.verify(
                    collection_id,
                    principal=_INTERNAL_VERIFIER,
                    cancel_requested=partial(
                        self._verification_cancel_requested,
                        collection_id,
                    ),
                )
            except _VerificationCanceled:
                self._finish_verification(collection_id, state="canceled")
            except Exception as exc:
                self._finish_verification(
                    collection_id,
                    state="failed",
                    failure=f"{type(exc).__name__}: {exc}"[:1000],
                )
            else:
                self._finish_verification(collection_id, state="succeeded", result=result)
            processed += 1
        return processed

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

    def _verification_cancel_requested(self, collection_id: int) -> bool:
        with session_scope(self._session_factory) as session:
            record = session.get(CollectionProvenanceVerificationRecord, collection_id)
            return record is None or record.cancel_requested or record.state == "canceling"

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

    def verify(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        collection_id = _collection_id(collection_id)
        if principal.has_artifact_scope:
            raise NotFound(f"collection not found: {collection_id}")
        if cancel_requested is not None and cancel_requested():
            raise _VerificationCanceled
        with read_snapshot(self._session_factory) as session:
            collection = _authorized_collection(session, collection_id, principal)
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
            external_state_references = int(
                session.scalar(
                    select(func.count())
                    .select_from(CollectionProvenanceExternalStateReferenceRecord)
                    .where(
                        CollectionProvenanceExternalStateReferenceRecord.collection_id
                        == collection_id
                    )
                )
                or 0
            )
            mismatched_status = session.scalar(
                select(CollectionFileRecord.path)
                .outerjoin(
                    CollectionFileProvenanceRecord,
                    (
                        CollectionFileProvenanceRecord.collection_id
                        == CollectionFileRecord.collection_id
                    )
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
            if mismatched_status is not None:
                raise InvalidState("catalog file provenance status projection differs")
            if collection.provenance_mode == "omitted":
                nonomitted = session.scalar(
                    select(CollectionFileProvenanceRecord.path)
                    .where(
                        CollectionFileProvenanceRecord.collection_id == collection_id,
                        CollectionFileProvenanceRecord.status != "omitted",
                    )
                    .limit(1)
                )
                if journals or entities or external_state_references or nonomitted is not None:
                    raise InvalidState("collection-wide provenance omission is inconsistent")
                return {
                    "collection_id": collection_id,
                    "valid": True,
                    "provenance_mode": "omitted",
                    "provenance_identity": None,
                    "files": files,
                    "journals": 0,
                    "entities": 0,
                }
            if bindings != files:
                raise InvalidState("provenance does not account for every collection file")

            def binding_factory() -> Iterator[FileProvenanceBinding]:
                if cancel_requested is not None and cancel_requested():
                    raise _VerificationCanceled
                return _iter_file_provenance_bindings(session, collection_id)

            def journal_factory() -> Iterator[tuple[str, bytes]]:
                for journal in _iter_journal_bytes(session, collection_id):
                    if cancel_requested is not None and cancel_requested():
                        raise _VerificationCanceled
                    yield journal

            try:
                identity = reconstruct_provenance_archive_identity(
                    bindings=binding_factory,
                    journals=journal_factory,
                )
            except ProvenanceValidationError as exc:
                raise InvalidState(str(exc)) from exc
            if identity != collection.provenance_identity:
                raise InvalidState("catalog provenance identity does not match exact bytes")
            projected_entities = 0
            for journal_id in _iter_journal_ids(session, collection_id):
                if cancel_requested is not None and cancel_requested():
                    raise _VerificationCanceled
                journal = session.scalar(
                    select(CollectionProvenanceJournalRecord).where(
                        CollectionProvenanceJournalRecord.collection_id == collection_id,
                        CollectionProvenanceJournalRecord.journal_id == journal_id,
                    )
                )
                if journal is None:
                    raise InvalidState("catalog provenance journal disappeared during snapshot")
                stored_bytes = int(
                    session.scalar(
                        select(
                            func.coalesce(
                                func.sum(
                                    func.length(CollectionProvenanceJournalChunkRecord.content)
                                ),
                                0,
                            )
                        ).where(
                            CollectionProvenanceJournalChunkRecord.collection_id == collection_id,
                            CollectionProvenanceJournalChunkRecord.journal_id == journal_id,
                        )
                    )
                    or 0
                )
                if stored_bytes != journal.bytes:
                    raise InvalidState("catalog provenance journal byte count differs")
                try:
                    summary = validate_journal_chunks(
                        _iter_journal_chunks(session, collection_id, journal_id)
                    )
                except ProvenanceValidationError as exc:
                    raise InvalidState(str(exc)) from exc
                projection = provenance_journal_projection(
                    collection_id=collection_id,
                    journal_id=journal_id,
                    summary=summary,
                )
                _verify_journal_summary(journal, projection)
                _verify_journal_payload_bindings(
                    session,
                    collection_id=collection_id,
                    journal_id=journal_id,
                    summary=summary,
                )
                _verify_journal_entities(session, collection_id, journal_id, projection)
                _verify_journal_references(session, collection_id, journal_id, projection)
                projected_entities += len(projection.entities)
                session.expunge(journal)
            if projected_entities != entities:
                raise InvalidState("catalog provenance projection differs from exact journals")
            reachable = _reachable_journals(
                select(CollectionFileProvenanceRecord.journal_id.label("journal_id"))
                .where(
                    CollectionFileProvenanceRecord.collection_id == collection_id,
                    CollectionFileProvenanceRecord.status == "captured",
                    CollectionFileProvenanceRecord.journal_id.is_not(None),
                )
                .distinct(),
                collection_id=collection_id,
                name="verify_reachable_journals",
            )
            reachable_count = int(session.scalar(select(func.count()).select_from(reachable)) or 0)
            if reachable_count != journals:
                raise InvalidState("provenance contains an unreachable journal")
            return {
                "collection_id": collection_id,
                "valid": True,
                "provenance_mode": collection.provenance_mode,
                "provenance_identity": identity,
                "files": files,
                "journals": journals,
                "entities": entities,
            }

    def rebuild_catalog_projection(
        self,
        collection_id: int,
        *,
        index_content: bytes,
        bundles: dict[str, bytes],
    ) -> dict[str, Any]:
        """Rebuild disposable PostgreSQL provenance rows from immutable archive bytes."""

        collection_id = _collection_id(collection_id)
        validated = validate_provenance_archive(index_content, bundles)
        with session_scope(self._session_factory) as session:
            collection = session.get(CollectionRecord, collection_id)
            if collection is None:
                raise NotFound(f"collection not found: {collection_id}")
            mode = (
                "mixed"
                if any(item.status == "omitted" for item in validated.bindings)
                else "captured"
            )
            if (
                collection.provenance_mode != mode
                or collection.provenance_identity != validated.identity
            ):
                raise InvalidState("immutable provenance identity differs from the collection")
            files = {
                item.path: item
                for item in session.scalars(
                    select(CollectionFileRecord).where(
                        CollectionFileRecord.collection_id == collection_id
                    )
                )
            }
            if set(files) != {item.path for item in validated.bindings}:
                raise InvalidState("immutable provenance does not account for every catalog file")
            for binding in validated.bindings:
                file = files[binding.path]
                if file.bytes != binding.bytes or file.sha256 != binding.sha256:
                    raise InvalidState(
                        f"immutable provenance payload binding differs from catalog: {binding.path}"
                    )

            session.execute(
                delete(CollectionProvenanceExternalStateReferenceRecord).where(
                    CollectionProvenanceExternalStateReferenceRecord.collection_id == collection_id
                )
            )
            session.execute(
                delete(CollectionProvenanceEntityRecord).where(
                    CollectionProvenanceEntityRecord.collection_id == collection_id
                )
            )
            session.execute(
                delete(CollectionFileProvenanceRecord).where(
                    CollectionFileProvenanceRecord.collection_id == collection_id
                )
            )
            session.execute(
                delete(CollectionProvenanceJournalRecord).where(
                    CollectionProvenanceJournalRecord.collection_id == collection_id
                )
            )
            journal_records: list[CollectionProvenanceJournalRecord] = []
            projections: dict[str, ProvenanceJournalProjection] = {}
            for journal_id, content in sorted(validated.journal_bytes.items()):
                summary = validated.journals[journal_id]
                projection = provenance_journal_projection(
                    collection_id=collection_id,
                    journal_id=journal_id,
                    summary=summary,
                )
                projections[journal_id] = projection
                journal_records.append(
                    CollectionProvenanceJournalRecord(
                        collection_id=collection_id,
                        journal_id=journal_id,
                        bytes=len(content),
                        sha256=summary.journal_sha256,
                        entries=summary.entries,
                        agent_count=len(summary.agent_ids),
                        entity_counts_json=projection.entity_counts_json,
                        current_state_id=summary.current_state_id,
                        current_path=summary.current_path,
                        current_bytes=summary.current_bytes,
                        current_sha256=summary.current_sha256,
                    )
                )
            session.add_all(journal_records)
            session.flush()
            for journal_id, content in sorted(validated.journal_bytes.items()):
                for ordinal, offset in enumerate(
                    range(0, len(content), _PROVENANCE_JOURNAL_CHUNK_BYTES)
                ):
                    session.add(
                        CollectionProvenanceJournalChunkRecord(
                            collection_id=collection_id,
                            journal_id=journal_id,
                            ordinal=ordinal,
                            content=content[offset : offset + _PROVENANCE_JOURNAL_CHUNK_BYTES],
                        )
                    )
            session.flush()
            session.add_all(
                CollectionFileProvenanceRecord(
                    collection_id=collection_id,
                    path=item.path,
                    status=item.status,
                    journal_id=item.journal_id,
                    current_state_id=item.current_state_id,
                    omission_reason=item.omission_reason,
                )
                for item in validated.bindings
            )
            session.flush()
            binding_status = (
                select(CollectionFileProvenanceRecord.status)
                .where(
                    CollectionFileProvenanceRecord.collection_id
                    == CollectionFileRecord.collection_id,
                    CollectionFileProvenanceRecord.path == CollectionFileRecord.path,
                )
                .correlate(CollectionFileRecord)
                .scalar_subquery()
            )
            session.execute(
                update(CollectionFileRecord)
                .where(CollectionFileRecord.collection_id == collection_id)
                .values(provenance_status=func.coalesce(binding_status, "missing"))
            )
            for journal_id in sorted(validated.journal_bytes):
                session.add_all(projections[journal_id].entities)
                session.add_all(projections[journal_id].external_state_references)
                session.execute(
                    insert(CollectionProvenanceJournalAgentRecord),
                    [
                        {
                            "collection_id": collection_id,
                            "journal_id": journal_id,
                            "agent_id": agent_id,
                        }
                        for agent_id in sorted(validated.journals[journal_id].agent_ids)
                    ],
                )
            session.flush()
            entities = int(
                session.scalar(
                    select(func.count(CollectionProvenanceEntityRecord.entity_id)).where(
                        CollectionProvenanceEntityRecord.collection_id == collection_id
                    )
                )
                or 0
            )
            return {
                "collection_id": collection_id,
                "provenance_mode": mode,
                "provenance_identity": validated.identity,
                "files": len(validated.bindings),
                "journals": len(journal_records),
                "entities": entities,
            }


def _iter_file_provenance_bindings(
    session: Session,
    collection_id: int,
) -> Iterator[FileProvenanceBinding]:
    path_order = CollectionFileRecord.path_sort_key
    statement = (
        select(CollectionFileRecord, CollectionFileProvenanceRecord)
        .join(
            CollectionFileProvenanceRecord,
            (CollectionFileProvenanceRecord.collection_id == CollectionFileRecord.collection_id)
            & (CollectionFileProvenanceRecord.path == CollectionFileRecord.path),
        )
        .where(CollectionFileRecord.collection_id == collection_id)
        .order_by(path_order)
        .execution_options(yield_per=100)
    )
    for file, binding in session.execute(statement):
        yield FileProvenanceBinding(
            path=file.path,
            bytes=file.bytes,
            sha256=file.sha256,
            status=binding.status,
            journal_id=binding.journal_id,
            current_state_id=binding.current_state_id,
            omission_reason=binding.omission_reason,
        )


def _iter_journal_bytes(
    session: Session,
    collection_id: int,
) -> Iterator[tuple[str, bytes]]:
    for journal_id in _iter_journal_ids(session, collection_id):
        yield journal_id, _journal_bytes(session, collection_id, journal_id)


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


def _journal_bytes(session: Session, collection_id: int, journal_id: str) -> bytes:
    return b"".join(_iter_journal_chunks(session, collection_id, journal_id))


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


def _verify_journal_summary(
    journal: CollectionProvenanceJournalRecord,
    projection: ProvenanceJournalProjection,
) -> None:
    summary = projection.summary
    if (
        journal.journal_id != summary.journal_id
        or journal.sha256 != summary.journal_sha256
        or journal.entries != summary.entries
        or journal.agent_count != len(summary.agent_ids)
        or journal.entity_counts_json != projection.entity_counts_json
        or journal.current_state_id != summary.current_state_id
        or journal.current_path != summary.current_path
        or journal.current_bytes != summary.current_bytes
        or journal.current_sha256 != summary.current_sha256
    ):
        raise InvalidState("catalog provenance journal summary differs from exact bytes")


def _verify_journal_payload_bindings(
    session: Session,
    *,
    collection_id: int,
    journal_id: str,
    summary: JournalSummary,
) -> None:
    statement = (
        select(CollectionFileRecord, CollectionFileProvenanceRecord)
        .join(
            CollectionFileProvenanceRecord,
            (CollectionFileProvenanceRecord.collection_id == CollectionFileRecord.collection_id)
            & (CollectionFileProvenanceRecord.path == CollectionFileRecord.path),
        )
        .where(
            CollectionFileRecord.collection_id == collection_id,
            CollectionFileProvenanceRecord.journal_id == journal_id,
        )
        .order_by(CollectionFileRecord.path_sort_key)
        .execution_options(yield_per=100)
    )
    for file, binding in session.execute(statement):
        if binding.status != "captured" or binding.current_state_id != summary.current_state_id:
            raise InvalidState("captured provenance binding differs from its exact journal")
        try:
            verify_payload_binding(
                summary,
                path=file.path,
                byte_count=file.bytes,
                sha256=file.sha256,
            )
        except ProvenanceValidationError as exc:
            raise InvalidState(str(exc)) from exc


def _verify_journal_entities(
    session: Session,
    collection_id: int,
    journal_id: str,
    projection: ProvenanceJournalProjection,
) -> None:
    actual = session.scalars(
        select(CollectionProvenanceEntityRecord)
        .where(
            CollectionProvenanceEntityRecord.collection_id == collection_id,
            CollectionProvenanceEntityRecord.journal_id == journal_id,
        )
        .order_by(
            CollectionProvenanceEntityRecord.entity_type,
            CollectionProvenanceEntityRecord.entity_id,
        )
        .execution_options(yield_per=100)
    )
    for expected, current in zip_longest(projection.entities, actual):
        if (
            expected is None
            or current is None
            or (
                expected.entity_type,
                expected.entity_id,
                expected.entry_id,
                expected.document_json,
            )
            != (
                current.entity_type,
                current.entity_id,
                current.entry_id,
                current.document_json,
            )
        ):
            raise InvalidState("catalog provenance projection differs from exact journals")


def _verify_journal_references(
    session: Session,
    collection_id: int,
    journal_id: str,
    projection: ProvenanceJournalProjection,
) -> None:
    actual = session.scalars(
        select(CollectionProvenanceExternalStateReferenceRecord)
        .where(
            CollectionProvenanceExternalStateReferenceRecord.collection_id == collection_id,
            CollectionProvenanceExternalStateReferenceRecord.from_journal_id == journal_id,
        )
        .order_by(
            CollectionProvenanceExternalStateReferenceRecord.to_journal_id,
            CollectionProvenanceExternalStateReferenceRecord.entry_id,
            CollectionProvenanceExternalStateReferenceRecord.state_id,
            CollectionProvenanceExternalStateReferenceRecord.entry_json_sha256,
        )
        .execution_options(yield_per=100)
    )
    for expected, current in zip_longest(projection.external_state_references, actual):
        if (
            expected is None
            or current is None
            or (
                expected.to_journal_id,
                expected.entry_id,
                expected.state_id,
                expected.entry_json_sha256,
            )
            != (
                current.to_journal_id,
                current.entry_id,
                current.state_id,
                current.entry_json_sha256,
            )
        ):
            raise InvalidState("catalog provenance lineage differs from exact journals")


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
) -> Any:
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
    ordering = desc if order == "desc" else asc
    return joined.order_by(
        ordering(sort_column),
        ordering(CollectionFileRecord.path_sort_key),
    )


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


def _statement_count(session: Session, statement: Any) -> int:
    return int(
        session.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
    )


def _trace_page_items(
    session: Session,
    journal_statement: Any,
    reference_statement: Any,
    *,
    journal_total: int,
    offset: int,
    limit: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if offset < journal_total:
        journals = session.scalars(journal_statement.offset(offset).limit(limit))
        items.extend(
            {"kind": "journal", "journal": _journal_payload(journal)} for journal in journals
        )
    remaining = limit - len(items)
    if remaining > 0:
        reference_offset = max(0, offset - journal_total)
        references = session.scalars(reference_statement.offset(reference_offset).limit(remaining))
        items.extend(
            {
                "kind": "external_state_reference",
                "reference": _external_state_reference_payload(reference),
            }
            for reference in references
        )
    return items


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


def _page_options(page: int, per_page: int, sort: str, order: str) -> None:
    if page < 1 or per_page < 1:
        raise BadRequest("page and per_page must be positive")
    if sort not in _SORT_FIELDS:
        raise BadRequest(f"sort must be one of {', '.join(sorted(_SORT_FIELDS))}")
    if order not in _SORT_ORDERS:
        raise BadRequest("order must be asc or desc")


def _trace_page_options(page: int, per_page: int) -> None:
    if page < 1 or per_page < 1 or per_page > 100:
        raise BadRequest("trace page and per_page must be between 1 and 100")


def _like_pattern(value: str) -> str:
    return f"%{value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"
