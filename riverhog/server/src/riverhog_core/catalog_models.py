from __future__ import annotations

import builtins
import secrets

from riverhog_protocol.paths import relpath_search_key, relpath_sort_key, text_search_key
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
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
    collection_count: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default=text("0"),
    )

    assignments: Mapped[list[CollectionTagRecord]] = relationship(
        back_populates="tag",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("collection_count >= 0", name="ck_tags_collection_count"),
        Index("ix_tags_created_at_id", "created_at", "id"),
        Index("ix_tags_collection_count_id", "collection_count", "id"),
        Index(
            "ix_tags_id_trgm",
            "id",
            postgresql_using="gin",
            postgresql_ops={"id": "gin_trgm_ops"},
        ),
    )


class CollectionRecord(Base):
    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    search_text: Mapped[str] = mapped_column(
        String,
        Computed("CAST(id AS TEXT)"),
    )
    creation_idempotency_key: Mapped[str] = mapped_column(String)
    creation_identity_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    creation_custody_mode: Mapped[str] = mapped_column(String, nullable=False)
    archive_generation: Mapped[str] = mapped_column(
        String(64), nullable=False, default=lambda: secrets.token_hex(32)
    )
    content_identity: Mapped[str] = mapped_column(String(64))
    tag_set_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    encryption_format: Mapped[str] = mapped_column(String, nullable=False)
    passphrase_id: Mapped[str] = mapped_column(String, nullable=False)
    provenance_mode: Mapped[str] = mapped_column(String, default="omitted")
    provenance_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inventory_identity: Mapped[str] = mapped_column(String(64))
    metadata_revision: Mapped[int] = mapped_column(BigInteger, default=1)
    metadata_updated_at: Mapped[str] = mapped_column(String)
    ingest_source: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by_app: Mapped[str] = mapped_column(String, default="riverhog")
    created_by_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    is_published: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )
    file_count: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    file_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
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
        Index("ix_collections_created_at_id", "created_at", "id"),
        Index("ix_collections_file_count_id", "file_count", "id"),
        Index("ix_collections_file_bytes_id", "file_bytes", "id"),
        Index(
            "ix_collections_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
        CheckConstraint("file_count >= 0", name="ck_collections_file_count"),
        CheckConstraint("file_bytes >= 0", name="ck_collections_file_bytes"),
        CheckConstraint("metadata_revision >= 1", name="ck_collections_metadata_revision"),
        CheckConstraint(
            "provenance_mode IN ('captured','mixed','omitted')",
            name="ck_collections_provenance_mode",
        ),
        CheckConstraint(
            "provenance_mode IN ('captured','mixed') AND provenance_identity IS NOT NULL OR "
            "provenance_mode = 'omitted' AND provenance_identity IS NULL",
            name="ck_collections_provenance_identity",
        ),
        CheckConstraint("length(content_identity) = 64", name="ck_collections_content_identity"),
        CheckConstraint(
            "length(inventory_identity) = 64", name="ck_collections_inventory_identity"
        ),
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
        Index(
            "ix_collection_tags_tag_trgm",
            "tag_id",
            postgresql_using="gin",
            postgresql_ops={"tag_id": "gin_trgm_ops"},
        ),
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
    provenance_status: Mapped[str] = mapped_column(
        String,
        default="missing",
        server_default=text("'missing'"),
    )
    path_sort_key: Mapped[builtins.bytes] = mapped_column(
        LargeBinary,
        default=lambda context: relpath_sort_key(str(context.get_current_parameters()["path"])),
    )
    search_text: Mapped[str] = mapped_column(
        String,
        default=lambda context: (
            f"{context.get_current_parameters()['collection_id']}/"
            f"{relpath_search_key(str(context.get_current_parameters()['path']))}"
        ),
    )
    path_search_text: Mapped[str] = mapped_column(
        String,
        default=lambda context: relpath_search_key(str(context.get_current_parameters()["path"])),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("bytes >= 0", name="ck_collection_files_bytes"),
        CheckConstraint("length(sha256) = 64", name="ck_collection_files_sha256"),
        CheckConstraint(
            "provenance_status IN ('captured','omitted','missing')",
            name="ck_collection_files_provenance_status",
        ),
        Index("ix_collection_files_path", "path_sort_key", "collection_id"),
        Index(
            "ix_collection_files_collection_path",
            "collection_id",
            "path_sort_key",
        ),
        Index("ix_collection_files_bytes", "bytes", "collection_id", "path_sort_key"),
        Index(
            "ix_collection_files_collection_bytes",
            "collection_id",
            "bytes",
            "path_sort_key",
        ),
        Index(
            "ix_collection_files_collection_provenance",
            "collection_id",
            "provenance_status",
            "path_sort_key",
        ),
        Index(
            "ix_collection_files_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
        Index(
            "ix_collection_files_path_search_trgm",
            "path_search_text",
            postgresql_using="gin",
            postgresql_ops={"path_search_text": "gin_trgm_ops"},
        ),
    )

    collection: Mapped[CollectionRecord] = relationship(back_populates="files")


class CollectionProvenanceJournalRecord(Base):
    __tablename__ = "collection_provenance_journals"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    entries: Mapped[int] = mapped_column(BigInteger)
    agent_count: Mapped[int] = mapped_column(BigInteger)
    entity_counts_json: Mapped[str] = mapped_column(Text)
    current_state_id: Mapped[str] = mapped_column(String)
    current_entry_id: Mapped[str] = mapped_column(String)
    current_entry_json_sha256: Mapped[str] = mapped_column(String(64))
    current_path: Mapped[str] = mapped_column(String)
    current_bytes: Mapped[int] = mapped_column(BigInteger)
    current_sha256: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
        Index("ix_collection_provenance_journals_sha256", "sha256", "collection_id"),
        CheckConstraint("bytes >= 0", name="ck_provenance_journals_bytes"),
        CheckConstraint("entries >= 0", name="ck_provenance_journals_entries"),
        CheckConstraint("agent_count >= 0", name="ck_provenance_journals_agent_count"),
        CheckConstraint("current_bytes >= 0", name="ck_provenance_journals_current_bytes"),
        CheckConstraint("length(sha256) = 64", name="ck_provenance_journals_sha256"),
    )

    collection: Mapped[CollectionRecord] = relationship(back_populates="provenance_journals")
    chunks: Mapped[list[CollectionProvenanceJournalChunkRecord]] = relationship(
        back_populates="journal",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    agents: Mapped[list[CollectionProvenanceJournalAgentRecord]] = relationship(
        back_populates="journal",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class CollectionProvenanceJournalChunkRecord(Base):
    __tablename__ = "collection_provenance_journal_chunks"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    byte_offset: Mapped[int] = mapped_column(BigInteger)
    content: Mapped[bytes] = mapped_column(LargeBinary)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "journal_id"],
            [
                "collection_provenance_journals.collection_id",
                "collection_provenance_journals.journal_id",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint("ordinal >= 0", name="ck_provenance_journal_chunks_ordinal"),
        CheckConstraint("byte_offset >= 0", name="ck_provenance_journal_chunks_offset"),
        CheckConstraint("length(content) > 0", name="ck_provenance_journal_chunks_content"),
    )

    journal: Mapped[CollectionProvenanceJournalRecord] = relationship(back_populates="chunks")


class CollectionProvenanceJournalAgentRecord(Base):
    __tablename__ = "collection_provenance_journal_agents"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String, primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "journal_id"],
            [
                "collection_provenance_journals.collection_id",
                "collection_provenance_journals.journal_id",
            ],
            ondelete="CASCADE",
        ),
        Index("ix_collection_provenance_journal_agents_agent", "agent_id", "collection_id"),
    )

    journal: Mapped[CollectionProvenanceJournalRecord] = relationship(back_populates="agents")


class CollectionProvenanceVerificationRecord(Base):
    __tablename__ = "collection_provenance_verifications"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    state: Mapped[str] = mapped_column(String)
    requested_by_app: Mapped[str] = mapped_column(String)
    requested_by_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    requested_at: Mapped[str] = mapped_column(String)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    next_attempt_at: Mapped[str] = mapped_column(String)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)
    phase: Mapped[str] = mapped_column(
        String, default="metadata", server_default=text("'metadata'")
    )
    checkpoint_json: Mapped[str] = mapped_column(Text, default="{}", server_default=text("'{}'"))

    __table_args__ = (
        ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
        CheckConstraint(
            "state IN ('queued','running','canceling','succeeded','failed','canceled')",
            name="ck_collection_provenance_verifications_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_collection_provenance_verifications_attempts"),
        CheckConstraint(
            "phase IN ('metadata','identity-tree','identity-bindings','identity-journals',"
            "'journal-entries','references','reachability','cleanup','complete')",
            name="ck_collection_provenance_verifications_phase",
        ),
        Index("ix_collection_provenance_verifications_due", "state", "next_attempt_at"),
    )


class CollectionProvenanceVerificationReachabilityRecord(Base):
    __tablename__ = "collection_provenance_verification_reachability"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    expanded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    after_to_journal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    after_entry_id: Mapped[str | None] = mapped_column(String, nullable=True)
    after_state_id: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_provenance_verifications.collection_id"],
            ondelete="CASCADE",
        ),
        Index(
            "ix_provenance_verification_reachability_work",
            "collection_id",
            "expanded",
            "journal_id",
        ),
    )


class CollectionProvenanceVerificationAgentRecord(Base):
    __tablename__ = "collection_provenance_verification_agents"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String, primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_provenance_verifications.collection_id"],
            ondelete="CASCADE",
        ),
    )


class CollectionProvenanceVerificationEntryRecord(Base):
    __tablename__ = "collection_provenance_verification_entries"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String, primary_key=True)
    json_sha256: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_provenance_verifications.collection_id"],
            ondelete="CASCADE",
        ),
    )


class CollectionProvenanceVerificationEntityRecord(Base):
    __tablename__ = "collection_provenance_verification_entities"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String, primary_key=True)
    entity_id: Mapped[str] = mapped_column(String, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String)
    document_json: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_provenance_verifications.collection_id"],
            ondelete="CASCADE",
        ),
        Index(
            "ix_provenance_verification_entities_entry",
            "collection_id",
            "journal_id",
            "entry_id",
        ),
    )


class CollectionProvenanceVerificationExternalStateRecord(Base):
    __tablename__ = "collection_provenance_verification_external_states"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    from_journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    to_journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String, primary_key=True)
    state_id: Mapped[str] = mapped_column(String, primary_key=True)
    entry_json_sha256: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_provenance_verifications.collection_id"],
            ondelete="CASCADE",
        ),
        Index(
            "ix_provenance_verification_external_states_target",
            "collection_id",
            "to_journal_id",
        ),
    )


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
        CheckConstraint(
            "status IN ('captured','omitted')",
            name="ck_collection_file_provenance_status",
        ),
        CheckConstraint(
            "status = 'captured' AND journal_id IS NOT NULL AND current_state_id IS NOT NULL "
            "AND omission_reason IS NULL OR status = 'omitted' AND journal_id IS NULL "
            "AND current_state_id IS NULL AND omission_reason IS NOT NULL",
            name="ck_collection_file_provenance_binding",
        ),
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
        CheckConstraint(
            "state IN ('pending','uploading','uploaded','retrying','failed')",
            name="ck_collection_archive_copies_state",
        ),
    )

    collection: Mapped[CollectionRecord] = relationship(back_populates="archive_copies")
    metadata_publication: Mapped[CollectionMetadataPublicationRecord | None] = relationship(
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
    revision: Mapped[str | None] = mapped_column(String, nullable=True)
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
        CheckConstraint("desired_revision >= 1", name="ck_metadata_publications_desired_revision"),
        CheckConstraint(
            "published_revision IS NULL OR published_revision >= 1",
            name="ck_metadata_publications_published_revision",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_metadata_publications_attempt_count"),
        CheckConstraint(
            "state IN ('pending','publishing','published','retry_wait')",
            name="ck_metadata_publications_state",
        ),
    )

    copy: Mapped[CollectionArchiveCopyRecord] = relationship(back_populates="metadata_publication")


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
    revision: Mapped[str | None] = mapped_column(String, nullable=True)
    age_state_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    archive_parts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
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
        CheckConstraint("object_order >= 0", name="ck_collection_archive_objects_order"),
        CheckConstraint("plaintext_bytes >= 0", name="ck_collection_archive_objects_plaintext"),
        CheckConstraint("stored_bytes >= 0", name="ck_collection_archive_objects_stored"),
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
        CheckConstraint("sequence >= 0", name="ck_archive_file_objects_sequence"),
        CheckConstraint("file_offset >= 0", name="ck_archive_file_objects_file_offset"),
        CheckConstraint("object_offset >= 0", name="ck_archive_file_objects_object_offset"),
        CheckConstraint("bytes >= 0", name="ck_archive_file_objects_bytes"),
    )

    object: Mapped[CollectionArchiveObjectRecord] = relationship(back_populates="placements")


class ArchiveDownloadUsageRecord(Base):
    __tablename__ = "archive_download_usage"

    store: Mapped[str] = mapped_column(String, primary_key=True)
    month_started_at: Mapped[str] = mapped_column(String)
    accounted_bytes: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint("accounted_bytes >= 0", name="ck_archive_download_usage_bytes"),
    )


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
        CheckConstraint("reserved_bytes >= 0", name="ck_archive_download_reservations_bytes"),
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
    batch_start_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch_end_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    destination_discarded_at: Mapped[str | None] = mapped_column(String, nullable=True)
    next_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    failure: Mapped[str | None] = mapped_column(String, nullable=True)
    search_text: Mapped[str] = mapped_column(
        String,
        Computed(
            "lower(CAST(collection_id AS TEXT) || ' ' || source_store || ' ' || "
            "destination_store || ' ' || state)"
        ),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "source_store"],
            ["collection_archive_copies.collection_id", "collection_archive_copies.store"],
            ondelete="CASCADE",
        ),
        Index("ix_archive_copy_jobs_due", "state", "next_attempt_at", "requested_at"),
        Index("ix_archive_copy_jobs_requested", "requested_at", "collection_id"),
        Index("ix_archive_copy_jobs_source", "source_store", "collection_id"),
        Index("ix_archive_copy_jobs_destination", "destination_store", "collection_id"),
        Index("ix_archive_copy_jobs_state", "state", "collection_id"),
        Index(
            "ix_archive_copy_jobs_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
        CheckConstraint(
            "state IN ('requested','waiting','checking','copying','canceling','completed',"
            "'failed','canceled')",
            name="ck_archive_copy_jobs_state",
        ),
        CheckConstraint(
            "batch_start_order IS NULL AND batch_end_order IS NULL OR "
            "batch_start_order IS NOT NULL AND batch_end_order >= batch_start_order",
            name="ck_archive_copy_jobs_batch",
        ),
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
    write_token: Mapped[str | None] = mapped_column(String, nullable=True)
    expected_stored_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    write_segments_json: Mapped[str | None] = mapped_column(String, nullable=True)
    uploaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    uploaded_segments: Mapped[int] = mapped_column(Integer, default=0)
    total_segments: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "destination_store"],
            ["archive_copy_jobs.collection_id", "archive_copy_jobs.destination_store"],
            ondelete="CASCADE",
        ),
        CheckConstraint("plaintext_bytes >= 0", name="ck_archive_copy_uploads_plaintext"),
        CheckConstraint("uploaded_bytes >= 0", name="ck_archive_copy_uploads_uploaded_bytes"),
        CheckConstraint("uploaded_segments >= 0", name="ck_archive_copy_uploads_uploaded_segments"),
        CheckConstraint("total_segments >= 0", name="ck_archive_copy_uploads_total_segments"),
        CheckConstraint(
            "uploaded_segments <= total_segments",
            name="ck_archive_copy_uploads_segment_progress",
        ),
    )


class CatalogEventRecord(Base):
    __tablename__ = "catalog_events"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change: Mapped[str] = mapped_column(String)
    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE)
    occurred_at: Mapped[str] = mapped_column(String)
    inventory_identity: Mapped[str] = mapped_column(String(64))

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
    search_text: Mapped[str] = mapped_column(
        String,
        Computed("lower(app || ' ' || id)"),
    )

    __table_args__ = (
        Index("ix_app_keys_app", "app", "id"),
        Index("ux_app_keys_token_sha256", "token_sha256", unique=True),
        Index("ix_app_keys_app_created", "app", "created_at", "id"),
        Index("ix_app_keys_app_expires", "app", "expires_at", "id"),
        Index("ix_app_keys_app_last_used", "app", "last_used_at", "id"),
        Index(
            "ix_app_keys_app_active",
            "app",
            "revoked_at",
            "expires_at",
            "id",
        ),
        Index(
            "ix_app_keys_active",
            "revoked_at",
            "expires_at",
            "id",
        ),
        Index(
            "ix_app_keys_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
        Index(
            "ix_app_keys_app_trgm",
            "app",
            postgresql_using="gin",
            postgresql_ops={"app": "gin_trgm_ops"},
        ),
        Index(
            "ix_app_keys_id_trgm",
            "id",
            postgresql_using="gin",
            postgresql_ops={"id": "gin_trgm_ops"},
        ),
        CheckConstraint(
            "monthly_download_quota_bytes IS NULL OR monthly_download_quota_bytes >= 0",
            name="ck_app_keys_download_quota",
        ),
        CheckConstraint("length(token_sha256) = 64", name="ck_app_keys_token_sha256"),
    )


class AppKeyAccessGrantRecord(Base):
    __tablename__ = "app_key_access_grants"

    key_id: Mapped[str] = mapped_column(String, primary_key=True)
    permission: Mapped[str] = mapped_column(String, primary_key=True)
    resource: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[str] = mapped_column(String)
    search_text: Mapped[str] = mapped_column(
        String,
        Computed("lower(permission || ' ' || resource)"),
    )

    __table_args__ = (
        ForeignKeyConstraint(["key_id"], ["app_keys.id"], ondelete="CASCADE"),
        Index("ix_app_key_access_grants_permission", "permission", "resource", "key_id"),
        Index("ix_app_key_access_grants_resource", "resource", "permission", "key_id"),
        Index(
            "ix_app_key_access_grants_created",
            "created_at",
            "key_id",
            "permission",
            "resource",
        ),
        Index(
            "ix_app_key_access_grants_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
    )


class KeyDownloadUsageRecord(Base):
    __tablename__ = "key_download_usage"

    key_id: Mapped[str] = mapped_column(String, primary_key=True)
    month_started_at: Mapped[str] = mapped_column(String)
    accounted_bytes: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        ForeignKeyConstraint(["key_id"], ["app_keys.id"], ondelete="CASCADE"),
        CheckConstraint("accounted_bytes >= 0", name="ck_key_download_usage_bytes"),
    )


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
        CheckConstraint("kind IN ('job','stream')", name="ck_key_download_reservations_kind"),
        CheckConstraint("reserved_bytes >= 0", name="ck_key_download_reservations_bytes"),
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

    __table_args__ = (
        Index("ix_retrieval_jobs_due", "state", "next_poll_at", "id"),
        CheckConstraint(
            "state IN ('requested','ready','completed','canceled','expired','failed')",
            name="ck_retrieval_jobs_state",
        ),
        CheckConstraint("length(plan_etag) = 64", name="ck_retrieval_jobs_plan_etag"),
    )

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
        CheckConstraint("file_order >= 0", name="ck_retrieval_job_files_order"),
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
        CheckConstraint("object_order >= 0", name="ck_retrieval_job_objects_order"),
        CheckConstraint(
            "read_mode IN ('immediate','restore_required','cache')",
            name="ck_retrieval_job_objects_read_mode",
        ),
    )

    job: Mapped[RetrievalJobRecord] = relationship(back_populates="objects")


class RetrievalCacheObjectRecord(Base):
    __tablename__ = "retrieval_cache_objects"

    source_store: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_path: Mapped[str] = mapped_column(String)
    revision: Mapped[str | None] = mapped_column(String, nullable=True)
    stored_bytes: Mapped[int] = mapped_column(BigInteger)
    stored_sha256: Mapped[str] = mapped_column(String(64))
    cached_at: Mapped[str] = mapped_column(String)
    verified_at: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="ready")
    search_text: Mapped[str] = mapped_column(
        String,
        Computed("lower(source_store || ' ' || object_id)"),
    )

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
        Index(
            "ix_retrieval_cache_objects_collection", "collection_id", "source_store", "object_id"
        ),
        Index(
            "ix_retrieval_cache_objects_object",
            "object_id",
            "collection_id",
            "source_store",
        ),
        Index(
            "ix_retrieval_cache_objects_bytes",
            "stored_bytes",
            "collection_id",
            "source_store",
            "object_id",
        ),
        Index(
            "ix_retrieval_cache_objects_cached",
            "cached_at",
            "collection_id",
            "source_store",
            "object_id",
        ),
        Index(
            "ix_retrieval_cache_objects_verified",
            "verified_at",
            "collection_id",
            "source_store",
            "object_id",
        ),
        Index(
            "ix_retrieval_cache_objects_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
        CheckConstraint("stored_bytes >= 0", name="ck_retrieval_cache_objects_bytes"),
        CheckConstraint("length(stored_sha256) = 64", name="ck_retrieval_cache_objects_sha256"),
        CheckConstraint(
            "state IN ('ready','delete_pending','deleting')",
            name="ck_retrieval_cache_objects_state",
        ),
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
        Index(
            "ix_retrieval_cache_leases_object_expiry",
            "source_store",
            "collection_id",
            "object_id",
            "expires_at",
            "owner",
        ),
    )


class CollectionUploadRecord(Base):
    __tablename__ = "collection_uploads"

    collection_id: Mapped[int] = mapped_column(
        COLLECTION_ID_TYPE,
        Identity(),
        primary_key=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String)
    creation_identity_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_generation: Mapped[str] = mapped_column(
        String(64), nullable=False, default=lambda: secrets.token_hex(32)
    )
    tag_set_identity: Mapped[str] = mapped_column(String(64), nullable=False)
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
    custody_mode: Mapped[str] = mapped_column(String, default="producer-retained")
    lease_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    orphaned_at: Mapped[str | None] = mapped_column(String, nullable=True)
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
    planner_checkpoint_json: Mapped[str] = mapped_column(Text)
    archive_tree_next_file_order: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    archive_tree_hash_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    archive_tree_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archive_volume_next_sequence: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    archive_volume_hash_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    archive_ordered_volume_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archive_terminal_receipt_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance_validation_next_file_order: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    provenance_closure_validated: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    derivative_provenance_state: Mapped[str] = mapped_column(
        String, default="not-required", server_default=text("'not-required'")
    )
    derivative_provenance_cursor_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'")
    )
    provenance_archive_next_file_order: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    provenance_archive_last_journal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance_archive_current_journal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance_archive_current_journal_offset: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    provenance_archive_next_sequence: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    provenance_archive_hash_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance_archive_ordered_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance_archive_terminal_receipt_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    provenance_archive_root_receipt_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_authority_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    catalog_phase: Mapped[str] = mapped_column(
        String, default="content-identity", server_default=text("'content-identity'")
    )
    catalog_cursor_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'")
    )
    catalog_hash_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    catalog_content_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    catalog_inventory_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_count: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default=text("0"),
    )
    file_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default=text("0"),
    )
    custodied_file_count: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default=text("0"),
    )
    custodied_file_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default=text("0"),
    )
    uploaded_payload_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default=text("0"),
    )
    search_text: Mapped[str] = mapped_column(
        String,
        default=lambda context: text_search_key(
            str(context.get_current_parameters().get("ingest_source") or "")
        ),
    )

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
    provenance_archive_volumes: Mapped[list[CollectionUploadProvenanceArchiveVolumeRecord]] = (
        relationship(
            back_populates="upload",
            cascade="all, delete-orphan",
            passive_deletes=True,
        )
    )
    __table_args__ = (
        Index(
            "ux_collection_uploads_application_idempotency_key",
            "initiated_by_app",
            "idempotency_key",
            unique=True,
        ),
        Index("ix_collection_uploads_opened_at", "opened_at", "collection_id"),
        Index("ix_collection_uploads_state", "state", "collection_id"),
        Index("ix_collection_uploads_file_count", "file_count", "collection_id"),
        Index("ix_collection_uploads_file_bytes", "file_bytes", "collection_id"),
        Index(
            "ix_collection_uploads_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
        CheckConstraint("file_count >= 0", name="ck_collection_uploads_file_count"),
        CheckConstraint(
            "archive_tree_next_file_order >= 0",
            name="ck_collection_uploads_tree_progress",
        ),
        CheckConstraint(
            "archive_volume_next_sequence >= 0",
            name="ck_collection_uploads_volume_progress",
        ),
        CheckConstraint(
            "provenance_validation_next_file_order >= 0 AND "
            "provenance_archive_next_file_order >= 0 AND "
            "provenance_archive_current_journal_offset >= 0 AND "
            "provenance_archive_next_sequence >= 0",
            name="ck_collection_uploads_provenance_progress",
        ),
        CheckConstraint(
            "catalog_phase IN ("
            "'content-identity','inventory-identity','collection','files','journals',"
            "'provenance-relations','bindings','tags','archive-objects','file-objects',"
            "'terminal','complete')",
            name="ck_collection_uploads_catalog_phase",
        ),
        CheckConstraint("file_bytes >= 0", name="ck_collection_uploads_file_bytes"),
        CheckConstraint(
            "custodied_file_count >= 0 AND custodied_file_count <= file_count",
            name="ck_collection_uploads_custodied_file_count",
        ),
        CheckConstraint(
            "custodied_file_bytes >= 0 AND custodied_file_bytes <= file_bytes",
            name="ck_collection_uploads_custodied_file_bytes",
        ),
        CheckConstraint(
            "custodied_file_count > 0 OR custodied_file_bytes = 0",
            name="ck_collection_uploads_empty_custody",
        ),
        CheckConstraint(
            "uploaded_payload_bytes >= 0",
            name="ck_collection_uploads_uploaded_payload_bytes",
        ),
        CheckConstraint(
            "state IN ('open','closing','uploading','finalizing','orphaned','discarding')",
            name="ck_collection_uploads_state",
        ),
        CheckConstraint(
            "custody_mode IN ('producer-retained','custody-transfer')",
            name="ck_collection_uploads_custody_mode",
        ),
        CheckConstraint(
            "provenance_mode IN ('captured','omitted')",
            name="ck_collection_uploads_provenance_mode",
        ),
        CheckConstraint(
            "derivative_provenance_state IN ("
            "'not-required','discovering','copying','generating','complete','failed')",
            name="ck_collection_uploads_derivative_provenance_state",
        ),
        CheckConstraint(
            "archive_phase IN ('planning','uploading','finalization_queued','finalizing',"
            "'retry_wait','orphaned','discarding')",
            name="ck_collection_uploads_archive_phase",
        ),
        CheckConstraint("archive_attempt_count >= 0", name="ck_collection_uploads_attempt_count"),
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
        Index(
            "ix_collection_upload_tags_tag_trgm",
            "tag_id",
            postgresql_using="gin",
            postgresql_ops={"tag_id": "gin_trgm_ops"},
        ),
    )

    upload: Mapped[CollectionUploadRecord] = relationship(back_populates="tags")


class CollectionUploadFileRecord(Base):
    __tablename__ = "collection_upload_files"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    path_sort_key: Mapped[bytes] = mapped_column(
        LargeBinary,
        default=lambda context: relpath_sort_key(str(context.get_current_parameters()["path"])),
    )
    file_order: Mapped[int] = mapped_column(Integer)
    bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    raw_part_plaintext_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_part_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_part_ordered_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_parts_accepted: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default=text("0"),
    )
    raw_part_commitment_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance_status: Mapped[str] = mapped_column(String)
    provenance_journal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance_current_state_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance_omission_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    custodied_at: Mapped[str | None] = mapped_column(String, nullable=True)
    custody_receipt_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_uploads.collection_id"],
            ondelete="CASCADE",
        ),
        Index("idx_collection_upload_files_collection_order", "collection_id", "file_order"),
        Index(
            "idx_collection_upload_files_collection_path",
            "collection_id",
            "path_sort_key",
        ),
        Index(
            "ux_collection_upload_files_order",
            "collection_id",
            "file_order",
            unique=True,
        ),
        CheckConstraint("file_order >= 0", name="ck_collection_upload_files_order"),
        CheckConstraint("bytes >= 0", name="ck_collection_upload_files_bytes"),
        CheckConstraint("raw_parts_accepted >= 0", name="ck_collection_upload_files_raw_parts"),
        CheckConstraint("length(sha256) = 64", name="ck_collection_upload_files_sha256"),
    )

    upload: Mapped[CollectionUploadRecord] = relationship(back_populates="files")


class CollectionUploadRawPartDigestRecord(Base):
    __tablename__ = "collection_upload_raw_part_digests"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    part_number: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "path"],
            ["collection_upload_files.collection_id", "collection_upload_files.path"],
            ondelete="CASCADE",
        ),
        CheckConstraint("part_number >= 0", name="ck_upload_raw_part_digest_number"),
        CheckConstraint("length(sha256) = 64", name="ck_upload_raw_part_digest_sha256"),
    )


class CollectionUploadProvenanceJournalRecord(Base):
    __tablename__ = "collection_upload_provenance_journals"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String, default="accepting")
    accepted_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    content_hash_state: Mapped[str] = mapped_column(Text)
    validation_byte_offset: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    validation_sequence: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    validation_previous_entry_id: Mapped[str | None] = mapped_column(String, nullable=True)
    validation_previous_json_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    primary_lineage_id: Mapped[str | None] = mapped_column(String, nullable=True)
    entity_counts_json: Mapped[str] = mapped_column(Text, default="{}", server_default=text("'{}'"))
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_state_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_entry_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_entry_json_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_path: Mapped[str | None] = mapped_column(String, nullable=True)
    current_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    current_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generated_output_path: Mapped[str | None] = mapped_column(String, nullable=True)
    generation_after_journal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    generation_after_state_id: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_uploads.collection_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("bytes >= 0", name="ck_upload_provenance_journals_bytes"),
        CheckConstraint(
            "accepted_bytes >= 0 AND accepted_bytes <= bytes",
            name="ck_upload_provenance_journals_accepted_bytes",
        ),
        CheckConstraint(
            "validation_byte_offset >= 0 AND validation_byte_offset <= accepted_bytes",
            name="ck_upload_provenance_journals_validation_offset",
        ),
        CheckConstraint(
            "validation_sequence >= 0",
            name="ck_upload_provenance_journals_validation_sequence",
        ),
        CheckConstraint(
            "state IN ('accepting','generating','validating','sealed','failed')",
            name="ck_upload_provenance_journals_state",
        ),
        CheckConstraint(
            "current_bytes IS NULL OR current_bytes >= 0",
            name="ck_upload_provenance_journals_current_bytes",
        ),
        CheckConstraint("length(sha256) = 64", name="ck_upload_provenance_journals_sha256"),
    )

    upload: Mapped[CollectionUploadRecord] = relationship(back_populates="provenance_journals")
    chunks: Mapped[list[CollectionUploadProvenanceJournalChunkRecord]] = relationship(
        back_populates="journal",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class CollectionUploadProvenanceJournalChunkRecord(Base):
    __tablename__ = "collection_upload_provenance_journal_chunks"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    byte_offset: Mapped[int] = mapped_column(BigInteger)
    content: Mapped[bytes] = mapped_column(LargeBinary)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "journal_id"],
            [
                "collection_upload_provenance_journals.collection_id",
                "collection_upload_provenance_journals.journal_id",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint("ordinal >= 0", name="ck_upload_provenance_journal_chunks_ordinal"),
        CheckConstraint("byte_offset >= 0", name="ck_upload_provenance_journal_chunks_offset"),
        CheckConstraint(
            "length(content) > 0",
            name="ck_upload_provenance_journal_chunks_content",
        ),
    )

    journal: Mapped[CollectionUploadProvenanceJournalRecord] = relationship(back_populates="chunks")


class CollectionUploadProvenanceSourceRecord(Base):
    """One exact source journal in a server-generated derivative closure."""

    __tablename__ = "collection_upload_provenance_sources"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    source_collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    expanded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    after_to_journal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    after_entry_id: Mapped[str | None] = mapped_column(String, nullable=True)
    after_state_id: Mapped[str | None] = mapped_column(String, nullable=True)
    copied: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    copy_offset: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_uploads.collection_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_collection_id", "journal_id"],
            [
                "collection_provenance_journals.collection_id",
                "collection_provenance_journals.journal_id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("copy_offset >= 0", name="ck_upload_provenance_sources_offset"),
        Index(
            "ix_collection_upload_provenance_sources_work",
            "collection_id",
            "expanded",
            "copied",
            "source_collection_id",
            "journal_id",
        ),
    )


class CollectionUploadProvenanceValidationFactRecord(Base):
    __tablename__ = "collection_upload_provenance_validation_facts"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    fact_key: Mapped[str] = mapped_column(String, primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "journal_id"],
            [
                "collection_upload_provenance_journals.collection_id",
                "collection_upload_provenance_journals.journal_id",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "kind IN ('entry','agent','event','state','binding','entity','external-state')",
            name="ck_upload_provenance_validation_fact_kind",
        ),
    )


class CollectionUploadProvenanceReachabilityRecord(Base):
    __tablename__ = "collection_upload_provenance_reachability"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    journal_id: Mapped[str] = mapped_column(String, primary_key=True)
    after_external_fact_key: Mapped[str | None] = mapped_column(String, nullable=True)
    expanded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "journal_id"],
            [
                "collection_upload_provenance_journals.collection_id",
                "collection_upload_provenance_journals.journal_id",
            ],
            ondelete="CASCADE",
        ),
        Index(
            "ix_upload_provenance_reachability_pending",
            "collection_id",
            "expanded",
            "journal_id",
        ),
    )


class CollectionUploadProvenanceArchiveVolumeRecord(Base):
    __tablename__ = "collection_upload_provenance_archive_volumes"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    document_json: Mapped[str] = mapped_column(Text)
    payload_receipt_json: Mapped[str] = mapped_column(Text)
    metadata_receipt_json: Mapped[str] = mapped_column(Text)

    upload: Mapped[CollectionUploadRecord] = relationship(
        back_populates="provenance_archive_volumes"
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_uploads.collection_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("sequence >= 0", name="ck_upload_provenance_archive_volumes_sequence"),
        CheckConstraint(
            "kind IN ('bindings','journal')",
            name="ck_upload_provenance_archive_volumes_kind",
        ),
    )


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
    source_path: Mapped[str | None] = mapped_column(String, nullable=True)
    source_first_part: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_part_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    unit_plaintext_bytes: Mapped[int] = mapped_column(BigInteger)
    plan_json: Mapped[str] = mapped_column(Text)
    plan_sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String, default="planned")
    checkpoint_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    sealed_receipt_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_receipt_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    uploaded_units: Mapped[int] = mapped_column(Integer, default=0)
    total_units: Mapped[int] = mapped_column(Integer, default=0)
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
        CheckConstraint("sequence >= 0", name="ck_archive_object_uploads_sequence"),
        CheckConstraint("plaintext_bytes >= 0", name="ck_archive_object_uploads_plaintext"),
        CheckConstraint("source_bytes >= 0", name="ck_archive_object_uploads_source"),
        CheckConstraint(
            "(kind = 'pack' AND source_path IS NULL AND source_first_part IS NULL "
            "AND source_part_count IS NULL) OR "
            "(kind = 'segment' AND source_path IS NOT NULL AND source_first_part >= 0 "
            "AND source_part_count > 0)",
            name="ck_archive_object_uploads_source_parts",
        ),
        CheckConstraint("unit_plaintext_bytes > 0", name="ck_archive_object_uploads_unit"),
        CheckConstraint(
            "state IN ('planned','uploading','sealed')",
            name="ck_archive_object_uploads_state",
        ),
        CheckConstraint("uploaded_bytes >= 0", name="ck_archive_object_uploads_uploaded_bytes"),
        CheckConstraint("uploaded_units >= 0", name="ck_archive_object_uploads_uploaded_units"),
        CheckConstraint("total_units >= 0", name="ck_archive_object_uploads_total_units"),
        CheckConstraint(
            "uploaded_units <= total_units",
            name="ck_archive_object_uploads_unit_progress",
        ),
    )

    upload: Mapped[CollectionUploadRecord] = relationship(back_populates="archive_objects")
