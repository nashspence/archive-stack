"""Reclaim superseded provider revisions of Riverhog-owned mutable sidecars."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session
from time_formats import format_utc_timestamp, utc_now, utc_timestamp_now

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import SessionFactory, session_scope
from riverhog_core.catalog_models import (
    CollectionDescriptionPublicationRecord,
    CollectionMutableDocumentReclamationRecord,
    CollectionTagPublicationRecord,
)

MutableDocumentKind = Literal["description", "tag_head"]


class MutableDocumentReceipt(Protocol):
    @property
    def object_path(self) -> str: ...

    @property
    def revision(self) -> str | None: ...

    @property
    def stored_bytes(self) -> int: ...

    @property
    def stored_sha256(self) -> str: ...


def retain_superseded_mutable_document(
    session: Session,
    *,
    collection_id: int,
    store: str,
    document_kind: MutableDocumentKind,
    object_path: str | None,
    provider_revision: str | None,
    stored_bytes: int | None,
    stored_sha256: str | None,
    replacement: MutableDocumentReceipt,
) -> None:
    """Atomically retain the prior exact receipt before accepting its replacement."""

    if provider_revision is None:
        return
    if object_path is None or stored_bytes is None or stored_sha256 is None:
        raise RuntimeError("mutable collection document receipt is incomplete")
    if (
        replacement.object_path == object_path
        and replacement.revision == provider_revision
        and replacement.stored_sha256 == stored_sha256
    ):
        return
    identity = hashlib.sha256(
        b"riverhog-mutable-document-reclamation/v1\x00"
        + document_kind.encode("ascii")
        + b"\x00"
        + str(collection_id).encode("ascii")
        + b"\x00"
        + store.encode("utf-8")
        + b"\x00"
        + object_path.encode("utf-8")
        + b"\x00"
        + provider_revision.encode("utf-8")
        + b"\x00"
        + stored_sha256.encode("ascii")
    ).hexdigest()
    if session.get(CollectionMutableDocumentReclamationRecord, identity) is None:
        session.add(
            CollectionMutableDocumentReclamationRecord(
                receipt_identity=identity,
                collection_id=collection_id,
                store=store,
                document_kind=document_kind,
                object_path=object_path,
                provider_revision=provider_revision,
                stored_bytes=stored_bytes,
                stored_sha256=stored_sha256,
                state="pending",
                next_attempt_at=utc_timestamp_now(),
                failure=None,
            )
        )


def process_due_mutable_document_reclamations(
    session_factory: SessionFactory,
    archive_stores: ArchiveStoreRegistry,
    *,
    document_kind: MutableDocumentKind,
    limit: int = 1,
) -> int:
    """Apply at most ``limit`` exact provider effects and ledger-row changes."""

    processed = 0
    for _ in range(max(0, limit)):
        with session_scope(session_factory) as session:
            now = utc_timestamp_now()
            row = session.scalar(
                select(CollectionMutableDocumentReclamationRecord)
                .where(
                    CollectionMutableDocumentReclamationRecord.document_kind == document_kind,
                    CollectionMutableDocumentReclamationRecord.state.in_(("pending", "retry_wait")),
                    CollectionMutableDocumentReclamationRecord.next_attempt_at <= now,
                )
                .order_by(
                    CollectionMutableDocumentReclamationRecord.next_attempt_at,
                    CollectionMutableDocumentReclamationRecord.receipt_identity,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                break
            if _is_current_receipt(session, row):
                row.state = "retry_wait"
                row.next_attempt_at = format_utc_timestamp(utc_now() + timedelta(seconds=2))
                row.failure = "superseded receipt is still current"
                processed += 1
                continue
            row.state = "deleting"
            identity = row.receipt_identity
            store_name = row.store
            object_path = row.object_path
            provider_revision = row.provider_revision
            stored_sha256 = row.stored_sha256
        try:
            archive_stores.require(store_name).store.delete_collection_document_revision(
                object_path=object_path,
                provider_revision=provider_revision,
                expected_stored_sha256=stored_sha256,
            )
        except Exception as exc:
            with session_scope(session_factory) as session:
                current = session.get(CollectionMutableDocumentReclamationRecord, identity)
                if current is not None:
                    current.state = "retry_wait"
                    current.next_attempt_at = format_utc_timestamp(utc_now() + timedelta(seconds=2))
                    current.failure = f"{type(exc).__name__}: {exc}"[:1000]
        else:
            with session_scope(session_factory) as session:
                current = session.get(CollectionMutableDocumentReclamationRecord, identity)
                if current is not None:
                    session.delete(current)
        processed += 1
    return processed


def requeue_interrupted_mutable_document_reclamations(
    session: Session,
    *,
    document_kind: MutableDocumentKind,
    limit: int,
    now: str,
) -> int:
    """Return interrupted provider effects to the durable pending queue."""

    rows = list(
        session.scalars(
            select(CollectionMutableDocumentReclamationRecord)
            .where(
                CollectionMutableDocumentReclamationRecord.document_kind == document_kind,
                CollectionMutableDocumentReclamationRecord.state == "deleting",
            )
            .order_by(CollectionMutableDocumentReclamationRecord.receipt_identity)
            .limit(max(0, limit))
            .with_for_update(skip_locked=True)
        )
    )
    for row in rows:
        row.state = "pending"
        row.next_attempt_at = now
        row.failure = "reclamation interrupted before completion"
    return len(rows)


def _is_current_receipt(
    session: Session,
    row: CollectionMutableDocumentReclamationRecord,
) -> bool:
    if row.document_kind == "description":
        current = session.get(
            CollectionDescriptionPublicationRecord,
            (row.collection_id, row.store),
        )
        return bool(
            current is not None
            and current.object_path == row.object_path
            and current.provider_revision == row.provider_revision
            and current.stored_sha256 == row.stored_sha256
        )
    current_tag = session.get(
        CollectionTagPublicationRecord,
        (row.collection_id, row.store),
    )
    return bool(
        current_tag is not None
        and current_tag.head_object_path == row.object_path
        and current_tag.head_provider_revision == row.provider_revision
        and current_tag.head_stored_sha256 == row.stored_sha256
    )


__all__ = [
    "MutableDocumentKind",
    "process_due_mutable_document_reclamations",
    "requeue_interrupted_mutable_document_reclamations",
    "retain_superseded_mutable_document",
]
