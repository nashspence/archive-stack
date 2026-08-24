from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from riverhog_core.catalog_base import Base

COLLECTION_ID_TYPE = BigInteger().with_variant(Integer, "sqlite")


class TagRecord(Base):
    __tablename__ = "tags"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_by_app: Mapped[str] = mapped_column(String)
    created_by_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)

    assignments: Mapped[list[CollectionTagRecord]] = relationship(
        back_populates="tag",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class CollectionRecord(Base):
    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    creation_idempotency_key: Mapped[str] = mapped_column(String)
    content_identity: Mapped[str] = mapped_column(String(64))
    encryption_format: Mapped[str] = mapped_column(String, nullable=False)
    passphrase_id: Mapped[str] = mapped_column(String, nullable=False)
    provenance_mode: Mapped[str] = mapped_column(String, default="omitted")
    provenance_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    record_etag: Mapped[str] = mapped_column(String(64))
    metadata_revision: Mapped[int] = mapped_column(BigInteger, default=1)
    metadata_updated_at: Mapped[str] = mapped_column(String)
    ingest_source: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by_app: Mapped[str] = mapped_column(String, default="riverhog")
    created_by_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    files: Mapped[list[CollectionFileRecord]] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    archive_copies: Mapped[list[CollectionArchiveCopyRecord]] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    tags: Mapped[list[CollectionTagRecord]] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    provenance_journals: Mapped[list[CollectionProvenanceJournalRecord]] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "created_by_app",
            "creation_idempotency_key",
            name="uq_collections_application_idempotency_key",
        ),
        Index("ix_collections_encryption_format", "encryption_format", "id"),
        Index("ix_collections_passphrase_id", "passphrase_id", "id"),
    )


class CollectionTagRecord(Base):
    __tablename__ = "collection_tags"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    tag_id: Mapped[str] = mapped_column(String, primary_key=True)
    assigned_by_app: Mapped[str] = mapped_column(String)
    assigned_by_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    assigned_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["tag_id"], ["tags.id"], ondelete="RESTRICT"),
        Index("ix_collection_tags_tag", "tag_id", "collection_id"),
    )

    collection: Mapped[CollectionRecord] = relationship(back_populates="tags")
    tag: Mapped[TagRecord] = relationship(back_populates="assignments")


class CollectionDeletionRecord(Base):
    __tablename__ = "collection_deletions"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    challenge: Mapped[str] = mapped_column(String)
    plan_json: Mapped[str] = mapped_column(Text)
    started_at: Mapped[str] = mapped_column(String)


class ArchiveCopyRetirementRecord(Base):
    __tablename__ = "archive_copy_retirements"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    store: Mapped[str] = mapped_column(String, primary_key=True)
    challenge: Mapped[str] = mapped_column(String)
    plan_json: Mapped[str] = mapped_column(Text)
    started_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "store"],
            ["collection_archive_copies.collection_id", "collection_archive_copies.store"],
            ondelete="CASCADE",
        ),
    )


class CollectionFileRecord(Base):
    __tablename__ = "collection_files"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            ondelete="CASCADE",
        ),
    )

    collection: Mapped[CollectionRecord] = relationship(back_populates="files")


class CollectionProvenanceJournalRecord(Base):
    __tablename__ = "collection_provenance_journals"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    journal_bytes: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    entries: Mapped[int] = mapped_column(BigInteger)
    agent_ids_json: Mapped[str] = mapped_column(Text)
    entity_counts_json: Mapped[str] = mapped_column(Text)
    current_state_id: Mapped[str] = mapped_column(String)
    current_path: Mapped[str] = mapped_column(String)
    current_bytes: Mapped[int] = mapped_column(BigInteger)
    current_sha256: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
        Index("ix_collection_provenance_journals_sha256", "sha256", "collection_id"),
    )

    collection: Mapped[CollectionRecord] = relationship(back_populates="provenance_journals")


class CollectionProvenanceExternalStateReferenceRecord(Base):
    __tablename__ = "collection_provenance_external_state_references"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    from_journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    to_journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String, primary_key=True)
    state_id: Mapped[str] = mapped_column(String, primary_key=True)
    entry_json_sha256: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "from_journal_id"],
            [
                "collection_provenance_journals.collection_id",
                "collection_provenance_journals.journal_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["collection_id", "to_journal_id"],
            [
                "collection_provenance_journals.collection_id",
                "collection_provenance_journals.journal_id",
            ],
            ondelete="CASCADE",
        ),
        Index(
            "ix_collection_provenance_external_state_references_target",
            "collection_id",
            "to_journal_id",
        ),
    )


class CollectionFileProvenanceRecord(Base):
    __tablename__ = "collection_file_provenance"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String)
    journal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_state_id: Mapped[str | None] = mapped_column(String, nullable=True)
    omission_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "path"],
            ["collection_files.collection_id", "collection_files.path"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["collection_id", "journal_id"],
            [
                "collection_provenance_journals.collection_id",
                "collection_provenance_journals.journal_id",
            ],
            ondelete="CASCADE",
        ),
        Index("ix_collection_file_provenance_journal", "collection_id", "journal_id"),
    )


class CollectionProvenanceEntityRecord(Base):
    __tablename__ = "collection_provenance_entities"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String, primary_key=True)
    entity_id: Mapped[str] = mapped_column(String, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String)
    document_json: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "journal_id"],
            [
                "collection_provenance_journals.collection_id",
                "collection_provenance_journals.journal_id",
            ],
            ondelete="CASCADE",
        ),
        Index(
            "ix_collection_provenance_entities_type",
            "collection_id",
            "entity_type",
            "entity_id",
        ),
    )


class CollectionArchiveCopyRecord(Base):
    __tablename__ = "collection_archive_copies"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    store: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String, default="pending")
    archive_storage_prefix: Mapped[str | None] = mapped_column(String, nullable=True)
    last_uploaded_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_verified_at: Mapped[str | None] = mapped_column(String, nullable=True)
    failure: Mapped[str | None] = mapped_column(String, nullable=True)
    objects: Mapped[list[CollectionArchiveObjectRecord]] = relationship(
        back_populates="copy",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            ondelete="CASCADE",
        ),
    )

    collection: Mapped[CollectionRecord] = relationship(back_populates="archive_copies")
    metadata_publication: Mapped[CollectionMetadataPublicationRecord | None] = relationship(
        back_populates="copy",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    proof_maturation: Mapped[CollectionProofMaturationRecord | None] = relationship(
        back_populates="copy",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    attestation: Mapped[CollectionArchiveAttestationRecord | None] = relationship(
        back_populates="copy",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class CollectionMetadataPublicationRecord(Base):
    __tablename__ = "collection_metadata_publications"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    store: Mapped[str] = mapped_column(String, primary_key=True)
    desired_revision: Mapped[int] = mapped_column(BigInteger)
    published_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    state: Mapped[str] = mapped_column(String)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[str] = mapped_column(String)
    last_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    stored_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    stored_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "store"],
            ["collection_archive_copies.collection_id", "collection_archive_copies.store"],
            ondelete="CASCADE",
        ),
        Index(
            "ix_collection_metadata_publications_due",
            "state",
            "next_attempt_at",
            "collection_id",
            "store",
        ),
    )

    copy: Mapped[CollectionArchiveCopyRecord] = relationship(back_populates="metadata_publication")


class CollectionProofMaturationRecord(Base):
    __tablename__ = "collection_proof_maturations"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    store: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[str] = mapped_column(String)
    last_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    matured_at: Mapped[str | None] = mapped_column(String, nullable=True)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "store"],
            ["collection_archive_copies.collection_id", "collection_archive_copies.store"],
            ondelete="CASCADE",
        ),
        Index(
            "ix_collection_proof_maturations_due",
            "state",
            "next_attempt_at",
            "collection_id",
            "store",
        ),
    )

    copy: Mapped[CollectionArchiveCopyRecord] = relationship(back_populates="proof_maturation")


class CollectionArchiveAttestationRecord(Base):
    __tablename__ = "collection_archive_attestations"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    store: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[str] = mapped_column(String)
    last_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    published_at: Mapped[str | None] = mapped_column(String, nullable=True)
    matured_at: Mapped[str | None] = mapped_column(String, nullable=True)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "store"],
            ["collection_archive_copies.collection_id", "collection_archive_copies.store"],
            ondelete="CASCADE",
        ),
        Index(
            "ix_collection_archive_attestations_due",
            "state",
            "next_attempt_at",
            "collection_id",
            "store",
        ),
    )

    copy: Mapped[CollectionArchiveCopyRecord] = relationship(back_populates="attestation")


class CollectionArchiveObjectRecord(Base):
    __tablename__ = "collection_archive_objects"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    store: Mapped[str] = mapped_column(String, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_order: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String)
    object_path: Mapped[str] = mapped_column(String)
    plaintext_bytes: Mapped[int] = mapped_column(BigInteger)
    stored_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stored_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    age_state_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    part_receipts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    index_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uploaded_at: Mapped[str] = mapped_column(String)
    verified_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "store"],
            ["collection_archive_copies.collection_id", "collection_archive_copies.store"],
            ondelete="CASCADE",
        ),
        Index(
            "idx_collection_archive_objects_order",
            "collection_id",
            "store",
            "object_order",
        ),
    )

    copy: Mapped[CollectionArchiveCopyRecord] = relationship(back_populates="objects")
    placements: Mapped[list[CollectionArchiveFileObjectRecord]] = relationship(
        back_populates="object",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class CollectionArchiveFileObjectRecord(Base):
    __tablename__ = "collection_archive_file_objects"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    store: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    object_id: Mapped[str] = mapped_column(String)
    file_offset: Mapped[int] = mapped_column(BigInteger)
    object_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes: Mapped[int] = mapped_column(BigInteger)
    member: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "store", "object_id"],
            [
                "collection_archive_objects.collection_id",
                "collection_archive_objects.store",
                "collection_archive_objects.object_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["collection_id", "path"],
            ["collection_files.collection_id", "collection_files.path"],
            ondelete="CASCADE",
        ),
        Index(
            "idx_collection_archive_file_objects_object",
            "collection_id",
            "store",
            "object_id",
        ),
    )

    object: Mapped[CollectionArchiveObjectRecord] = relationship(back_populates="placements")


class ArchiveDownloadUsageRecord(Base):
    __tablename__ = "archive_download_usage"

    store: Mapped[str] = mapped_column(String, primary_key=True)
    month_started_at: Mapped[str] = mapped_column(String)
    accounted_bytes: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[str] = mapped_column(String)


class ArchiveDownloadReservationRecord(Base):
    __tablename__ = "archive_download_reservations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    store: Mapped[str] = mapped_column(String)
    month_started_at: Mapped[str] = mapped_column(String)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        ForeignKeyConstraint(
            ["store"],
            ["archive_download_usage.store"],
            ondelete="CASCADE",
        ),
        Index("ix_archive_download_reservations_expiry", "store", "expires_at"),
    )


class ArchiveCopyJobRecord(Base):
    __tablename__ = "archive_copy_jobs"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    destination_store: Mapped[str] = mapped_column(String, primary_key=True)
    destination_storage_prefix: Mapped[str] = mapped_column(String)
    source_store: Mapped[str] = mapped_column(String)
    initiated_by_app: Mapped[str] = mapped_column(String)
    initiated_by_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event_context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String)
    requested_at: Mapped[str] = mapped_column(String)
    read_requested_at: Mapped[str | None] = mapped_column(String, nullable=True)
    ready_at: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    next_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    failure: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "source_store"],
            ["collection_archive_copies.collection_id", "collection_archive_copies.store"],
            ondelete="CASCADE",
        ),
        Index("ix_archive_copy_jobs_due", "state", "next_attempt_at", "requested_at"),
    )


class ArchiveCopyObjectUploadRecord(Base):
    __tablename__ = "archive_copy_object_uploads"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    destination_store: Mapped[str] = mapped_column(String, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    object_path: Mapped[str] = mapped_column(String)
    plaintext_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    multipart_upload_id: Mapped[str | None] = mapped_column(String, nullable=True)
    multipart_content_length: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    multipart_parts_json: Mapped[str | None] = mapped_column(String, nullable=True)
    uploaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    uploaded_parts: Mapped[int] = mapped_column(Integer, default=0)
    total_parts: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "destination_store"],
            ["archive_copy_jobs.collection_id", "archive_copy_jobs.destination_store"],
            ondelete="CASCADE",
        ),
    )


class CatalogEventRecord(Base):
    __tablename__ = "catalog_events"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change: Mapped[str] = mapped_column(String)
    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE)
    occurred_at: Mapped[str] = mapped_column(String)
    record_etag: Mapped[str] = mapped_column(String(64))

    __table_args__ = (Index("ix_catalog_events_collection", "collection_id", "sequence"),)


class CatalogEventTagRecord(Base):
    __tablename__ = "catalog_event_tags"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    phase: Mapped[str] = mapped_column(String, primary_key=True)
    tag_id: Mapped[str] = mapped_column(String, primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(["sequence"], ["catalog_events.sequence"], ondelete="CASCADE"),
        CheckConstraint("phase IN ('before', 'after')", name="ck_catalog_event_tags_phase"),
        Index("ix_catalog_event_tags_visibility", "phase", "tag_id", "sequence"),
    )


class LifecycleEventRecord(Base):
    __tablename__ = "lifecycle_events"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String, unique=True)
    owner_app: Mapped[str] = mapped_column(String)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    event_json: Mapped[str] = mapped_column(Text)
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_lifecycle_events_owner_sequence", "owner_app", "sequence"),
        Index(
            "ix_lifecycle_events_owner_subject_context",
            "owner_app",
            "subject",
            "context_expires_at",
        ),
        Index("ix_lifecycle_events_context_expiry", "context_expires_at"),
    )


class AppKeyRecord(Base):
    __tablename__ = "app_keys"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    app: Mapped[str] = mapped_column(String)
    token_sha256: Mapped[str] = mapped_column(String(64))
    monthly_download_quota_bytes: Mapped[int | None] = mapped_column(
        BigInteger,
        default=0,
        nullable=True,
    )
    created_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_used_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_app_keys_app", "app", "id"),
        Index("ux_app_keys_token_sha256", "token_sha256", unique=True),
    )


class AppKeyAccessGrantRecord(Base):
    __tablename__ = "app_key_access_grants"

    key_id: Mapped[str] = mapped_column(String, primary_key=True)
    permission: Mapped[str] = mapped_column(String, primary_key=True)
    resource: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        ForeignKeyConstraint(["key_id"], ["app_keys.id"], ondelete="CASCADE"),
        Index("ix_app_key_access_grants_permission", "permission", "resource", "key_id"),
        Index("ix_app_key_access_grants_resource", "resource", "permission", "key_id"),
    )


class KeyDownloadUsageRecord(Base):
    __tablename__ = "key_download_usage"

    key_id: Mapped[str] = mapped_column(String, primary_key=True)
    month_started_at: Mapped[str] = mapped_column(String)
    accounted_bytes: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (ForeignKeyConstraint(["key_id"], ["app_keys.id"], ondelete="CASCADE"),)


class KeyDownloadReservationRecord(Base):
    __tablename__ = "key_download_reservations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    key_id: Mapped[str] = mapped_column(String)
    job_id: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    month_started_at: Mapped[str] = mapped_column(String)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        ForeignKeyConstraint(["key_id"], ["app_keys.id"], ondelete="CASCADE"),
        Index(
            "ix_key_download_reservations_key_month",
            "key_id",
            "month_started_at",
        ),
        Index("ix_key_download_reservations_job", "job_id", "kind"),
        Index("ix_key_download_reservations_expiry", "expires_at", "key_id"),
    )


class RetrievalJobRecord(Base):
    __tablename__ = "retrieval_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    app: Mapped[str] = mapped_column(String)
    initiated_by_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event_context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String)
    plan_etag: Mapped[str] = mapped_column(String(64))
    constraints_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String)
    requested_at: Mapped[str | None] = mapped_column(String, nullable=True)
    restore_requested_at: Mapped[str | None] = mapped_column(String, nullable=True)
    ready_at: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    next_poll_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    canceled_at: Mapped[str | None] = mapped_column(String, nullable=True)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_retrieval_jobs_due", "state", "next_poll_at", "id"),)

    files: Mapped[list[RetrievalJobFileRecord]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )
    objects: Mapped[list[RetrievalJobObjectRecord]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )


class RetrievalJobFileRecord(Base):
    __tablename__ = "retrieval_job_files"

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    file_order: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        ForeignKeyConstraint(["job_id"], ["retrieval_jobs.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["collection_id", "path"],
            ["collection_files.collection_id", "collection_files.path"],
        ),
        Index("ix_retrieval_job_files_order", "job_id", "file_order"),
    )

    job: Mapped[RetrievalJobRecord] = relationship(back_populates="files")


class RetrievalJobObjectRecord(Base):
    __tablename__ = "retrieval_job_objects"

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    source_store: Mapped[str] = mapped_column(String, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_order: Mapped[int] = mapped_column(Integer)
    read_mode: Mapped[str] = mapped_column(String)

    __table_args__ = (
        ForeignKeyConstraint(["job_id"], ["retrieval_jobs.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["collection_id", "source_store", "object_id"],
            [
                "collection_archive_objects.collection_id",
                "collection_archive_objects.store",
                "collection_archive_objects.object_id",
            ],
        ),
        Index("ix_retrieval_job_objects_order", "job_id", "object_order"),
    )

    job: Mapped[RetrievalJobRecord] = relationship(back_populates="objects")


class RetrievalCacheObjectRecord(Base):
    __tablename__ = "retrieval_cache_objects"

    source_store: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_path: Mapped[str] = mapped_column(String)
    version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    stored_bytes: Mapped[int] = mapped_column(BigInteger)
    stored_sha256: Mapped[str] = mapped_column(String(64))
    cached_at: Mapped[str] = mapped_column(String)
    verified_at: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="ready")

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "source_store", "object_id"],
            [
                "collection_archive_objects.collection_id",
                "collection_archive_objects.store",
                "collection_archive_objects.object_id",
            ],
            ondelete="CASCADE",
        ),
        Index("ix_retrieval_cache_objects_cleanup", "state", "cached_at"),
    )


class RetrievalCacheLeaseRecord(Base):
    __tablename__ = "retrieval_cache_leases"

    owner: Mapped[str] = mapped_column(String, primary_key=True)
    source_store: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    expires_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_store", "collection_id", "object_id"],
            [
                "retrieval_cache_objects.source_store",
                "retrieval_cache_objects.collection_id",
                "retrieval_cache_objects.object_id",
            ],
            ondelete="CASCADE",
        ),
        Index("ix_retrieval_cache_leases_expiry", "expires_at", "owner"),
    )


class CollectionUploadRecord(Base):
    __tablename__ = "collection_uploads"

    collection_id: Mapped[int] = mapped_column(
        COLLECTION_ID_TYPE,
        Identity(),
        primary_key=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String)
    ingest_source: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance_mode: Mapped[str] = mapped_column(String)
    provenance_omission_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    encryption_format: Mapped[str] = mapped_column(String, nullable=False)
    passphrase_id: Mapped[str] = mapped_column(String, nullable=False)
    initiated_by_app: Mapped[str] = mapped_column(String, default="riverhog")
    initiated_by_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event_context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String, default="open")
    archive_store: Mapped[str] = mapped_column(String, nullable=False)
    opened_at: Mapped[str] = mapped_column(String)
    last_activity_at: Mapped[str] = mapped_column(String)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_phase: Mapped[str] = mapped_column(String, default="planning")
    archive_phase_updated_at: Mapped[str] = mapped_column(String)
    archive_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    archive_next_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_last_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_failure: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_storage_prefix: Mapped[str] = mapped_column(String)
    collection_manifest_bytes_b64: Mapped[str | None] = mapped_column(String, nullable=True)
    collection_manifest_proof_bytes_b64: Mapped[str | None] = mapped_column(String, nullable=True)
    planner_checkpoint_json: Mapped[str] = mapped_column(Text)

    files: Mapped[list[CollectionUploadFileRecord]] = relationship(
        back_populates="upload",
        cascade="all, delete-orphan",
    )
    tags: Mapped[list[CollectionUploadTagRecord]] = relationship(
        back_populates="upload",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    archive_objects: Mapped[list[CollectionArchiveObjectUploadRecord]] = relationship(
        back_populates="upload",
        cascade="all, delete-orphan",
    )
    provenance_journals: Mapped[list[CollectionUploadProvenanceJournalRecord]] = relationship(
        back_populates="upload",
        cascade="all, delete-orphan",
    )
    __table_args__ = (
        Index(
            "ux_collection_uploads_application_idempotency_key",
            "initiated_by_app",
            "idempotency_key",
            unique=True,
        ),
        {"sqlite_autoincrement": True},
    )


class CollectionUploadTagRecord(Base):
    __tablename__ = "collection_upload_tags"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    tag_id: Mapped[str] = mapped_column(String, primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_uploads.collection_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["tag_id"], ["tags.id"], ondelete="RESTRICT"),
        Index("ix_collection_upload_tags_tag", "tag_id", "collection_id"),
    )

    upload: Mapped[CollectionUploadRecord] = relationship(back_populates="tags")


class CollectionUploadFileRecord(Base):
    __tablename__ = "collection_upload_files"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    file_order: Mapped[int] = mapped_column(Integer)
    bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    raw_part_plaintext_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_digest_manifest_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance_status: Mapped[str] = mapped_column(String)
    provenance_journal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance_current_state_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance_omission_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_uploads.collection_id"],
            ondelete="CASCADE",
        ),
        Index("idx_collection_upload_files_collection_order", "collection_id", "file_order"),
        Index(
            "ux_collection_upload_files_order",
            "collection_id",
            "file_order",
            unique=True,
        ),
    )

    upload: Mapped[CollectionUploadRecord] = relationship(back_populates="files")


class CollectionUploadProvenanceJournalRecord(Base):
    __tablename__ = "collection_upload_provenance_journals"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    journal_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    current_state_id: Mapped[str] = mapped_column(String)
    current_path: Mapped[str] = mapped_column(String)
    current_bytes: Mapped[int] = mapped_column(BigInteger)
    current_sha256: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_uploads.collection_id"],
            ondelete="CASCADE",
        ),
    )

    upload: Mapped[CollectionUploadRecord] = relationship(back_populates="provenance_journals")


class CollectionArchiveObjectUploadRecord(Base):
    __tablename__ = "collection_archive_object_uploads"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String)
    relative_path: Mapped[str] = mapped_column(String)
    object_path: Mapped[str] = mapped_column(String)
    plaintext_bytes: Mapped[int] = mapped_column(BigInteger)
    source_bytes: Mapped[int] = mapped_column(BigInteger)
    unit_plaintext_bytes: Mapped[int] = mapped_column(BigInteger)
    plan_json: Mapped[str] = mapped_column(Text)
    plan_sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String, default="planned")
    checkpoint_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    sealed_receipt_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    uploaded_parts: Mapped[int] = mapped_column(Integer, default=0)
    total_parts: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[str] = mapped_column(String)
    sealed_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_uploads.collection_id"],
            ondelete="CASCADE",
        ),
        Index(
            "ux_collection_archive_object_uploads_sequence",
            "collection_id",
            "sequence",
            unique=True,
        ),
    )

    upload: Mapped[CollectionUploadRecord] = relationship(back_populates="archive_objects")
