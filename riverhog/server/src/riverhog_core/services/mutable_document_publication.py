"""Seal one exact mutable-document provider write across retry and restart."""

from __future__ import annotations

import hashlib
from typing import cast

from riverhog_protocol import CollectionDescriptionDocument, CollectionTagHeadDocument
from sqlalchemy.orm import Session
from time_formats import utc_timestamp_now

from riverhog_core.catalog_models import CollectionMutableDocumentPublicationAttemptRecord
from riverhog_core.services.mutable_document_reclamation import (
    MutableDocumentKind,
    MutableDocumentReceipt,
    retain_superseded_mutable_document,
)

_ATTEMPT_IDENTITY_DOMAIN = b"riverhog-mutable-document-publication-attempt/v1\x00"


def create_mutable_document_publication_attempt(
    session: Session,
    *,
    collection_id: int,
    store: str,
    document_kind: MutableDocumentKind,
    document: bytes,
    archive_storage_prefix: str,
    passphrase_id: str,
    prior_object_path: str | None,
    prior_provider_revision: str | None,
    prior_stored_bytes: int | None,
    prior_stored_sha256: str | None,
) -> CollectionMutableDocumentPublicationAttemptRecord:
    """Persist one immutable destination-scoped attempt before its provider effect."""

    key = (collection_id, store, document_kind)
    if session.get(CollectionMutableDocumentPublicationAttemptRecord, key) is not None:
        raise RuntimeError("mutable collection document publication already has an attempt")
    revision, document_identity = _document_authority(document_kind, document)
    receipt_values = (
        prior_object_path,
        prior_stored_bytes,
        prior_stored_sha256,
    )
    if any(value is None for value in receipt_values) and not all(
        value is None for value in receipt_values
    ):
        raise RuntimeError("prior mutable collection document receipt is incomplete")
    if prior_object_path is None and prior_provider_revision is not None:
        raise RuntimeError("prior provider revision has no mutable document receipt")
    attempt_identity = _attempt_identity(
        collection_id=collection_id,
        store=store,
        document_kind=document_kind,
        document=document,
        archive_storage_prefix=archive_storage_prefix,
        passphrase_id=passphrase_id,
        prior_object_path=prior_object_path,
        prior_provider_revision=prior_provider_revision,
        prior_stored_bytes=prior_stored_bytes,
        prior_stored_sha256=prior_stored_sha256,
    )
    attempt = CollectionMutableDocumentPublicationAttemptRecord(
        collection_id=collection_id,
        store=store,
        document_kind=document_kind,
        attempt_identity=attempt_identity,
        document_revision=revision,
        document_identity=document_identity,
        document_bytes=document,
        archive_storage_prefix=archive_storage_prefix,
        passphrase_id=passphrase_id,
        prior_object_path=prior_object_path,
        prior_provider_revision=prior_provider_revision,
        prior_stored_bytes=prior_stored_bytes,
        prior_stored_sha256=prior_stored_sha256,
        created_at=utc_timestamp_now(),
    )
    session.add(attempt)
    session.flush()
    return attempt


def validate_mutable_document_publication_attempt(
    attempt: CollectionMutableDocumentPublicationAttemptRecord,
) -> CollectionDescriptionDocument | CollectionTagHeadDocument:
    """Return the exact canonical document or reject corrupted attempt state."""

    revision, document_identity = _document_authority(attempt.document_kind, attempt.document_bytes)
    if revision != attempt.document_revision or document_identity != attempt.document_identity:
        raise RuntimeError("mutable collection document publication attempt differs")
    expected = _attempt_identity(
        collection_id=attempt.collection_id,
        store=attempt.store,
        document_kind=attempt.document_kind,
        document=attempt.document_bytes,
        archive_storage_prefix=attempt.archive_storage_prefix,
        passphrase_id=attempt.passphrase_id,
        prior_object_path=attempt.prior_object_path,
        prior_provider_revision=attempt.prior_provider_revision,
        prior_stored_bytes=attempt.prior_stored_bytes,
        prior_stored_sha256=attempt.prior_stored_sha256,
    )
    if expected != attempt.attempt_identity:
        raise RuntimeError("mutable collection document publication attempt identity differs")
    return (
        CollectionDescriptionDocument.from_json_bytes(attempt.document_bytes)
        if attempt.document_kind == "description"
        else CollectionTagHeadDocument.from_json_bytes(attempt.document_bytes)
    )


def retain_attempt_superseded_document(
    session: Session,
    *,
    attempt: CollectionMutableDocumentPublicationAttemptRecord,
    replacement: MutableDocumentReceipt,
) -> None:
    """Transfer the attempt's exact prior receipt into durable cleanup custody."""

    retain_superseded_mutable_document(
        session,
        collection_id=attempt.collection_id,
        store=attempt.store,
        document_kind=cast(MutableDocumentKind, attempt.document_kind),
        object_path=attempt.prior_object_path,
        provider_revision=attempt.prior_provider_revision,
        stored_bytes=attempt.prior_stored_bytes,
        stored_sha256=attempt.prior_stored_sha256,
        replacement=replacement,
    )


def _document_authority(document_kind: str, document: bytes) -> tuple[int, str]:
    if document_kind == "description":
        parsed = CollectionDescriptionDocument.from_json_bytes(document)
        return parsed.revision, parsed.description_identity
    if document_kind == "tag_head":
        head = CollectionTagHeadDocument.from_json_bytes(document)
        return head.revision, head.head_identity
    raise RuntimeError("mutable collection document kind is invalid")


def _attempt_identity(
    *,
    collection_id: int,
    store: str,
    document_kind: str,
    document: bytes,
    archive_storage_prefix: str,
    passphrase_id: str,
    prior_object_path: str | None,
    prior_provider_revision: str | None,
    prior_stored_bytes: int | None,
    prior_stored_sha256: str | None,
) -> str:
    digest = hashlib.sha256(_ATTEMPT_IDENTITY_DOMAIN)
    for value in (
        str(collection_id),
        store,
        document_kind,
        hashlib.sha256(document).hexdigest(),
        archive_storage_prefix,
        passphrase_id,
        prior_object_path,
        prior_provider_revision,
        None if prior_stored_bytes is None else str(prior_stored_bytes),
        prior_stored_sha256,
    ):
        encoded = b"" if value is None else value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


__all__ = [
    "create_mutable_document_publication_attempt",
    "retain_attempt_superseded_document",
    "validate_mutable_document_publication_attempt",
]
