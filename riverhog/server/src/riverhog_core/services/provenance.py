from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from riverhog_protocol.errors import BadRequest, InvalidState, NotFound
from riverhog_protocol.paths import (
    PathNormalizationError,
    normalize_collection_id,
    normalize_relpath,
)
from riverhog_provenance import (
    FileProvenanceBinding,
    build_provenance_archive,
    validate_provenance_archive,
)
from sqlalchemy import asc, delete, desc, func, select
from sqlalchemy.orm import Session, undefer
from state_schema import read_snapshot

from riverhog_core.app_permissions import (
    CATALOG_READ,
    PROVENANCE_EXPORT,
    PROVENANCE_READ,
    ApplicationPrincipal,
)
from riverhog_core.artifact_access import artifact_scope_filter, require_artifact_scope
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionFileProvenanceRecord,
    CollectionFileRecord,
    CollectionProvenanceEntityRecord,
    CollectionProvenanceExternalStateReferenceRecord,
    CollectionProvenanceJournalRecord,
    CollectionRecord,
)
from riverhog_core.provenance_projection import (
    ProvenanceJournalProjection,
    provenance_journal_projection,
)
from riverhog_core.runtime_config import RuntimeConfig

_SORT_FIELDS = {"path", "bytes", "status"}


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
        if status is not None and status not in {"captured", "omitted"}:
            raise BadRequest("status must be captured or omitted")
        with read_snapshot(self._session_factory) as session:
            collection = _authorized_collection(session, collection_id, principal)
            joined = _provenance_file_statement(
                collection_id=collection_id,
                collection=collection,
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
        if status is not None and status not in {"captured", "omitted"}:
            raise BadRequest("status must be captured or omitted")
        with read_snapshot(self._session_factory) as session:
            collection = _authorized_collection(session, collection_id, principal)
            statement = _provenance_file_statement(
                collection_id=collection_id,
                collection=collection,
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
        with session_scope(self._session_factory) as session:
            collection = _authorized_collection(session, collection_id, principal)
            require_artifact_scope(session, principal, collection_id, path)
            row = session.execute(
                select(CollectionFileRecord, CollectionFileProvenanceRecord)
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
                ancestors = tuple(
                    session.scalars(
                        select(
                            CollectionProvenanceExternalStateReferenceRecord.to_journal_id
                        ).where(
                            CollectionProvenanceExternalStateReferenceRecord.collection_id
                            == collection_id,
                            CollectionProvenanceExternalStateReferenceRecord.from_journal_id
                            == row[1].journal_id,
                        )
                    )
                )
                payload["journal"] = _journal_payload(
                    journal,
                    ancestor_journal_ids=ancestors,
                )
            return payload

    def trace_file(
        self,
        collection_id: int,
        path: str,
        *,
        principal: ApplicationPrincipal,
    ) -> dict[str, Any]:
        shown = self.show_file(collection_id, path, principal=principal)
        binding = shown["provenance"]
        if binding["status"] == "omitted":
            return {**shown, "journals": [], "external_state_references": []}
        journal_id = str(binding["journal_id"])
        with session_scope(self._session_factory) as session:
            _authorized_collection(session, collection_id, principal)
            reachable = {journal_id}
            pending = {journal_id}
            edge_records: list[CollectionProvenanceExternalStateReferenceRecord] = []
            while pending:
                current_edges = list(
                    session.scalars(
                        select(CollectionProvenanceExternalStateReferenceRecord).where(
                            CollectionProvenanceExternalStateReferenceRecord.collection_id
                            == collection_id,
                            CollectionProvenanceExternalStateReferenceRecord.from_journal_id.in_(
                                pending
                            ),
                        )
                    )
                )
                edge_records.extend(current_edges)
                discovered = {item.to_journal_id for item in current_edges} - reachable
                reachable.update(discovered)
                pending = discovered
            records = {
                item.journal_id: item
                for item in session.scalars(
                    select(CollectionProvenanceJournalRecord).where(
                        CollectionProvenanceJournalRecord.collection_id == collection_id,
                        CollectionProvenanceJournalRecord.journal_id.in_(reachable),
                    )
                )
            }
            if set(records) != reachable:
                raise InvalidState("provenance ancestor journal projection is incomplete")
            ancestors_by_journal: dict[str, set[str]] = {
                current_id: set() for current_id in reachable
            }
            for edge in edge_records:
                ancestors_by_journal[edge.from_journal_id].add(edge.to_journal_id)
            return {
                **shown,
                "journals": [
                    _journal_payload(
                        records[item],
                        ancestor_journal_ids=ancestors_by_journal[item],
                    )
                    for item in sorted(reachable)
                ],
                "external_state_references": sorted(
                    (_external_state_reference_payload(item) for item in edge_records),
                    key=lambda item: (
                        item["from_journal_id"],
                        item["to_journal_id"],
                        item["state_id"],
                    ),
                ),
            }

    def export_journal(
        self,
        collection_id: int,
        journal_id: str,
        *,
        principal: ApplicationPrincipal,
    ) -> tuple[bytes, str]:
        collection_id = _collection_id(collection_id)
        with session_scope(self._session_factory) as session:
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
            return record.journal_bytes, record.sha256

    def verify(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
    ) -> dict[str, Any]:
        collection_id = _collection_id(collection_id)
        if principal.has_artifact_scope:
            raise NotFound(f"collection not found: {collection_id}")
        with session_scope(self._session_factory) as session:
            collection = _authorized_collection(session, collection_id, principal)
            files = list(
                session.scalars(
                    select(CollectionFileRecord)
                    .where(CollectionFileRecord.collection_id == collection_id)
                    .order_by(CollectionFileRecord.path)
                )
            )
            bindings = list(
                session.scalars(
                    select(CollectionFileProvenanceRecord)
                    .where(CollectionFileProvenanceRecord.collection_id == collection_id)
                    .order_by(CollectionFileProvenanceRecord.path)
                )
            )
            journals = list(
                session.scalars(
                    select(CollectionProvenanceJournalRecord)
                    .options(undefer(CollectionProvenanceJournalRecord.journal_bytes))
                    .where(CollectionProvenanceJournalRecord.collection_id == collection_id)
                    .order_by(CollectionProvenanceJournalRecord.journal_id)
                )
            )
            entities = list(
                session.scalars(
                    select(CollectionProvenanceEntityRecord).where(
                        CollectionProvenanceEntityRecord.collection_id == collection_id
                    )
                )
            )
            external_state_references = list(
                session.scalars(
                    select(CollectionProvenanceExternalStateReferenceRecord).where(
                        CollectionProvenanceExternalStateReferenceRecord.collection_id
                        == collection_id
                    )
                )
            )
            if collection.provenance_mode == "omitted":
                if (
                    journals
                    or entities
                    or external_state_references
                    or any(item.status != "omitted" for item in bindings)
                ):
                    raise InvalidState("collection-wide provenance omission is inconsistent")
                return {
                    "collection_id": collection_id,
                    "valid": True,
                    "provenance_mode": "omitted",
                    "provenance_identity": None,
                    "files": len(files),
                    "journals": 0,
                    "entities": 0,
                }
            if len(bindings) != len(files):
                raise InvalidState("provenance does not account for every collection file")
            files_by_path = {item.path: item for item in files}
            archive = build_provenance_archive(
                bindings=[
                    FileProvenanceBinding(
                        path=item.path,
                        bytes=files_by_path[item.path].bytes,
                        sha256=files_by_path[item.path].sha256,
                        status=item.status,  # type: ignore[arg-type]
                        journal_id=item.journal_id,
                        current_state_id=item.current_state_id,
                        omission_reason=item.omission_reason,
                    )
                    for item in bindings
                ],
                journals={item.journal_id: item.journal_bytes for item in journals},
            )
            if archive.identity != collection.provenance_identity:
                raise InvalidState("catalog provenance identity does not match exact bytes")
            projections = {
                item.journal_id: provenance_journal_projection(
                    collection_id=collection_id,
                    journal_id=item.journal_id,
                    summary=archive.journal_summaries[item.journal_id],
                )
                for item in journals
            }
            expected_entities = {
                (record.journal_id, record.entity_type, record.entity_id): (
                    record.entry_id,
                    record.document_json,
                )
                for projection in projections.values()
                for record in projection.entities
            }
            actual_entities = {
                (item.journal_id, item.entity_type, item.entity_id): (
                    item.entry_id,
                    item.document_json,
                )
                for item in entities
            }
            if actual_entities != expected_entities:
                raise InvalidState("catalog provenance projection differs from exact journals")
            expected_edges = {
                (
                    record.from_journal_id,
                    record.to_journal_id,
                    record.entry_id,
                    record.state_id,
                    record.entry_json_sha256,
                )
                for projection in projections.values()
                for record in projection.external_state_references
            }
            actual_edges = {
                (
                    record.from_journal_id,
                    record.to_journal_id,
                    record.entry_id,
                    record.state_id,
                    record.entry_json_sha256,
                )
                for record in external_state_references
            }
            if actual_edges != expected_edges:
                raise InvalidState("catalog provenance lineage differs from exact journals")
            for journal in journals:
                projection = projections[journal.journal_id]
                if (
                    journal.entries != len(projection.summary.frames)
                    or journal.agent_ids_json
                    != json.dumps(sorted(projection.summary.agent_ids), separators=(",", ":"))
                    or journal.entity_counts_json != projection.entity_counts_json
                ):
                    raise InvalidState(
                        "catalog provenance journal summary differs from exact bytes"
                    )
            return {
                "collection_id": collection_id,
                "valid": True,
                "provenance_mode": collection.provenance_mode,
                "provenance_identity": archive.identity,
                "files": len(files),
                "journals": len(journals),
                "entities": len(entities),
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
                        journal_bytes=content,
                        bytes=len(content),
                        sha256=summary.journal_sha256,
                        entries=len(summary.frames),
                        agent_ids_json=json.dumps(sorted(summary.agent_ids), separators=(",", ":")),
                        entity_counts_json=projection.entity_counts_json,
                        current_state_id=summary.current_state_id,
                        current_path=summary.current_path,
                        current_bytes=summary.current_bytes,
                        current_sha256=summary.current_sha256,
                    )
                )
            session.add_all(journal_records)
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
            for journal_id in sorted(validated.journal_bytes):
                session.add_all(projections[journal_id].entities)
                session.add_all(projections[journal_id].external_state_references)
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


def _provenance_file_statement(
    *,
    collection_id: int,
    collection: CollectionRecord,
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
            func.lower(CollectionFileRecord.path).like(_like_pattern(q.casefold()), escape="\\")
        )
    effective_status = func.coalesce(
        CollectionFileProvenanceRecord.status,
        "omitted" if collection.provenance_mode == "omitted" else "missing",
    )
    if status is not None:
        joined = joined.where(effective_status == status)
    sort_column = {
        "path": CollectionFileRecord.path,
        "bytes": CollectionFileRecord.bytes,
        "status": effective_status,
    }[sort]
    direction = desc(sort_column) if order == "desc" else asc(sort_column)
    return joined.order_by(direction, CollectionFileRecord.path.asc())


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


def _journal_is_in_artifact_scope(
    session: Session,
    collection_id: int,
    journal_id: str,
    principal: ApplicationPrincipal,
) -> bool:
    if not principal.has_artifact_scope:
        return True
    reachable = {
        item
        for item in session.scalars(
            select(CollectionFileProvenanceRecord.journal_id).where(
                CollectionFileProvenanceRecord.collection_id == collection_id,
                artifact_scope_filter(
                    CollectionFileProvenanceRecord.collection_id,
                    CollectionFileProvenanceRecord.path,
                    principal,
                ),
                CollectionFileProvenanceRecord.journal_id.is_not(None),
            )
        )
        if item is not None
    }
    pending = set(reachable)
    while pending:
        discovered = (
            set(
                session.scalars(
                    select(CollectionProvenanceExternalStateReferenceRecord.to_journal_id).where(
                        CollectionProvenanceExternalStateReferenceRecord.collection_id
                        == collection_id,
                        CollectionProvenanceExternalStateReferenceRecord.from_journal_id.in_(
                            pending
                        ),
                    )
                )
            )
            - reachable
        )
        reachable.update(discovered)
        pending = discovered
    return journal_id in reachable


def _file_payload(
    file: CollectionFileRecord,
    binding: CollectionFileProvenanceRecord | None,
    collection: CollectionRecord,
) -> dict[str, Any]:
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
    *,
    ancestor_journal_ids: Iterable[str],
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
        "agent_ids": json.loads(record.agent_ids_json),
        "ancestor_journal_ids": sorted(set(ancestor_journal_ids)),
        "entity_counts": json.loads(record.entity_counts_json),
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
    if order not in {"asc", "desc"}:
        raise BadRequest("order must be asc or desc")


def _like_pattern(value: str) -> str:
    return f"%{value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"
