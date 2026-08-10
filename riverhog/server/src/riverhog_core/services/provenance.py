from __future__ import annotations

import json
from collections import Counter
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
    validate_journal,
    validate_provenance_archive,
)
from sqlalchemy import asc, delete, desc, func, select
from sqlalchemy.orm import Session

from riverhog_core.app_permissions import (
    CATALOG_READ,
    PROVENANCE_EXPORT,
    PROVENANCE_READ,
    ApplicationPrincipal,
)
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionFileProvenanceRecord,
    CollectionFileRecord,
    CollectionProvenanceEntityRecord,
    CollectionProvenanceJournalRecord,
    CollectionRecord,
)
from riverhog_core.provenance_projection import provenance_entity_records
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
        all_items: bool,
        principal: ApplicationPrincipal,
    ) -> dict[str, Any]:
        collection_id = _collection_id(collection_id)
        _page_options(page, per_page, sort, order)
        if status is not None and status not in {"captured", "omitted"}:
            raise BadRequest("status must be captured or omitted")
        with session_scope(self._session_factory) as session:
            collection = _authorized_collection(session, collection_id, principal)
            joined = (
                select(CollectionFileRecord, CollectionFileProvenanceRecord)
                .outerjoin(
                    CollectionFileProvenanceRecord,
                    (
                        CollectionFileProvenanceRecord.collection_id
                        == CollectionFileRecord.collection_id
                    )
                    & (CollectionFileProvenanceRecord.path == CollectionFileRecord.path),
                )
                .where(CollectionFileRecord.collection_id == collection_id)
            )
            if q:
                joined = joined.where(
                    func.lower(CollectionFileRecord.path).like(
                        _like_pattern(q.casefold()), escape="\\"
                    )
                )
            effective_status = func.coalesce(
                CollectionFileProvenanceRecord.status,
                "omitted" if collection.provenance_mode == "omitted" else "missing",
            )
            if status is not None:
                joined = joined.where(effective_status == status)
            total = int(session.scalar(select(func.count()).select_from(joined.subquery())) or 0)
            sort_column = {
                "path": CollectionFileRecord.path,
                "bytes": CollectionFileRecord.bytes,
                "status": effective_status,
            }[sort]
            direction = desc(sort_column) if order == "desc" else asc(sort_column)
            joined = joined.order_by(direction, CollectionFileRecord.path.asc())
            if not all_items:
                joined = joined.offset((page - 1) * per_page).limit(per_page)
            rows = session.execute(joined).all()
            return {
                "page": 1 if all_items else page,
                "per_page": total if all_items else per_page,
                "total": total,
                "pages": (1 if total else 0)
                if all_items
                else ((total + per_page - 1) // per_page if total else 0),
                "sort": sort,
                "order": order,
                "query": q,
                "status": status,
                "collection_id": collection_id,
                "provenance_mode": collection.provenance_mode,
                "provenance_etag": collection.provenance_etag,
                "files": [_file_payload(file, binding, collection) for file, binding in rows],
            }

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
                payload["journal"] = _journal_payload(journal)
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
            return {**shown, "journals": [], "lineage_edges": []}
        journal_id = str(binding["journal_id"])
        with session_scope(self._session_factory) as session:
            _authorized_collection(session, collection_id, principal)
            records = {
                item.journal_id: item
                for item in session.scalars(
                    select(CollectionProvenanceJournalRecord).where(
                        CollectionProvenanceJournalRecord.collection_id == collection_id
                    )
                )
            }
            reachable = {journal_id}
            pending = [journal_id]
            edges: list[dict[str, str]] = []
            while pending:
                current_id = pending.pop()
                current = records.get(current_id)
                if current is None:
                    raise InvalidState(f"provenance ancestor journal is missing: {current_id}")
                for reference in validate_journal(current.journal_bytes).external_states:
                    edges.append(
                        {
                            "from_journal_id": current_id,
                            "to_journal_id": reference.journal_id,
                            "state_id": reference.state_id,
                            "entry_id": reference.entry_id,
                            "entry_json_sha256": reference.entry_json_sha256,
                        }
                    )
                    if reference.journal_id not in reachable:
                        reachable.add(reference.journal_id)
                        pending.append(reference.journal_id)
            return {
                **shown,
                "journals": [_journal_payload(records[item]) for item in sorted(reachable)],
                "lineage_edges": sorted(
                    edges,
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
            if collection.provenance_mode == "omitted":
                if journals or entities or any(item.status != "omitted" for item in bindings):
                    raise InvalidState("collection-wide provenance omission is inconsistent")
                return {
                    "collection_id": collection_id,
                    "valid": True,
                    "provenance_mode": "omitted",
                    "provenance_etag": None,
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
            if archive.identity != collection.provenance_etag:
                raise InvalidState("catalog provenance identity does not match exact bytes")
            expected_entities = _projected_entities(journals)
            actual_entities = {
                (item.journal_id, item.entity_type, item.entity_id): (
                    item.entry_id,
                    item.document_json,
                )
                for item in entities
            }
            if actual_entities != expected_entities:
                raise InvalidState("catalog provenance projection differs from exact journals")
            return {
                "collection_id": collection_id,
                "valid": True,
                "provenance_mode": collection.provenance_mode,
                "provenance_etag": archive.identity,
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
                or collection.provenance_etag != validated.identity
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
            for journal_id, content in sorted(validated.journal_bytes.items()):
                summary = validated.journals[journal_id]
                journal_records.append(
                    CollectionProvenanceJournalRecord(
                        collection_id=collection_id,
                        journal_id=journal_id,
                        journal_bytes=content,
                        bytes=len(content),
                        sha256=summary.journal_sha256,
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
            for journal_id, content in sorted(validated.journal_bytes.items()):
                session.add_all(
                    provenance_entity_records(
                        collection_id=collection_id,
                        journal_id=journal_id,
                        content=content,
                    )
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
                "provenance_etag": validated.identity,
                "files": len(validated.bindings),
                "journals": len(journal_records),
                "entities": entities,
            }


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


def _journal_payload(record: CollectionProvenanceJournalRecord) -> dict[str, Any]:
    summary = validate_journal(record.journal_bytes)
    counts: Counter[str] = Counter()
    for frame in summary.frames:
        body = frame.document.get("body")
        assertions = body.get("assertions") if isinstance(body, dict) else None
        if isinstance(assertions, dict):
            for key, value in assertions.items():
                if isinstance(value, list):
                    counts[key] += len(value)
    return {
        "journal_id": record.journal_id,
        "bytes": record.bytes,
        "sha256": record.sha256,
        "entries": len(summary.frames),
        "current_state_id": record.current_state_id,
        "current_path": record.current_path,
        "current_bytes": record.current_bytes,
        "current_sha256": record.current_sha256,
        "agent_ids": sorted(summary.agent_ids),
        "ancestor_journal_ids": sorted(
            {reference.journal_id for reference in summary.external_states}
        ),
        "entity_counts": dict(sorted(counts.items())),
    }


def _projected_entities(
    journals: list[CollectionProvenanceJournalRecord],
) -> dict[tuple[str, str, str], tuple[str, str]]:
    projected: dict[tuple[str, str, str], tuple[str, str]] = {}
    entity_lists = (
        "agents",
        "lineages",
        "states",
        "environments",
        "captures",
        "activities",
        "relations",
        "payload_bindings",
        "extensions",
    )
    for journal in journals:
        for frame in validate_journal(journal.journal_bytes).frames:
            body = frame.document.get("body")
            assertions = body.get("assertions") if isinstance(body, dict) else None
            if not isinstance(assertions, dict):
                continue
            for entity_type in entity_lists:
                values = assertions.get(entity_type, [])
                if not isinstance(values, list):
                    continue
                for value in values:
                    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
                        continue
                    entry = (
                        str(frame.document["id"]),
                        json.dumps(value, sort_keys=True, separators=(",", ":")),
                    )
                    projected[(journal.journal_id, entity_type, str(value["id"]))] = entry
                    if entity_type == "activities" and isinstance(value.get("evidence"), list):
                        for evidence in value["evidence"]:
                            if isinstance(evidence, dict) and isinstance(evidence.get("id"), str):
                                projected[(journal.journal_id, "evidence", str(evidence["id"]))] = (
                                    str(frame.document["id"]),
                                    json.dumps(
                                        evidence,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    ),
                                )
    return projected


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
