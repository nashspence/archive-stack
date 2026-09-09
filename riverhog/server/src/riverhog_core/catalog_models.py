from __future__ import annotations

import builtins
import secrets

from riverhog_protocol import (
    COLLECTION_DESCRIPTION_UTF8_BYTES_MAX,
    COLLECTION_TAG_NODE_BYTES_MAX,
    COLLECTION_TAG_UTF8_BYTES_MAX,
    MAX_COLLECTION_DESCRIPTION_REVISION,
    MAX_COLLECTION_TAG_REVISION,
    collection_tag_set_identity,
)
from riverhog_protocol.collection_upload_transport import collection_upload_path_order_key
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
from riverhog_core.catalog_types import (
    archive_object_order_type,
    archive_sequence_type,
    authority_ordinal_type,
)

COLLECTION_ID_TYPE = BigInteger().with_variant(Integer, "sqlite")
_ARCHIVE_SEQUENCE_ZERO = "0" * 64
_AUTHORITY_ORDINAL_ZERO = "0" * 64


def _fixed_lowercase_integer_check(column: str, width: int) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = {width} AND lower({column}) = {column} AND length({remainder}) = 0"


class CollectionTagRecord(Base):
    """Rebuildable catalog projection of one exact canonical tag value."""

    __tablename__ = "collection_tags"

    tag_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    tag: Mapped[str] = mapped_column(Text, unique=True)
    search_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    collection_count: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default=text("0"),
    )
    memberships: Mapped[list[CollectionTagMembershipRecord]] = relationship(
        back_populates="tag_record",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("collection_count >= 0", name="ck_collection_tags_collection_count"),
        CheckConstraint(
            f"octet_length(tag) > 0 AND octet_length(tag) <= {COLLECTION_TAG_UTF8_BYTES_MAX}",
            name="ck_collection_tags_bytes",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("tag_sha256", 64),
            name="ck_collection_tags_sha256",
        ),
        Index("ix_collection_tags_created_at", "created_at", "tag_sha256"),
        Index("ix_collection_tags_updated_at", "updated_at", "tag_sha256"),
        Index("ix_collection_tags_count", "collection_count", "tag_sha256"),
        Index(
            "ix_collection_tags_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
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
    encryption_format: Mapped[str] = mapped_column(String, nullable=False)
    passphrase_id: Mapped[str] = mapped_column(String, nullable=False)
    provenance_mode: Mapped[str] = mapped_column(String, default="omitted")
    provenance_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inventory_identity: Mapped[str] = mapped_column(String(64))
    archive_root_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    catalog_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ingest_source: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_search: Mapped[str] = mapped_column(
        Text,
        default="",
        server_default=text("''"),
    )
    description_revision: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default=text("0"),
    )
    description_identity: Mapped[str] = mapped_column(
        String(64),
        default="0" * 64,
        server_default=text(f"'{'0' * 64}'"),
    )
    description_mutation_state: Mapped[str] = mapped_column(
        String,
        default="idle",
        server_default=text("'idle'"),
    )
    pending_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_description_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pending_description_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description_attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    description_next_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    description_last_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    description_failure: Mapped[str | None] = mapped_column(Text, nullable=True)
    tag_revision: Mapped[int] = mapped_column(BigInteger, default=1, server_default=text("1"))
    tag_root_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tag_set_identity: Mapped[str] = mapped_column(
        String(64),
        default=lambda: collection_tag_set_identity(None),
        server_default=text(f"'{collection_tag_set_identity(None)}'"),
    )
    tag_head_identity: Mapped[str] = mapped_column(
        String(64), default="0" * 64, server_default=text(f"'{'0' * 64}'")
    )
    tag_mutation_operation_id: Mapped[str | None] = mapped_column(String, nullable=True)
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
    tags: Mapped[list[CollectionTagMembershipRecord]] = relationship(
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
        Index(
            "ix_collections_description_search_trgm",
            "description_search",
            postgresql_using="gin",
            postgresql_ops={"description_search": "gin_trgm_ops"},
        ),
        CheckConstraint("file_count >= 0", name="ck_collections_file_count"),
        CheckConstraint("file_bytes >= 0", name="ck_collections_file_bytes"),
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
        CheckConstraint(
            "archive_root_sha256 IS NULL OR length(archive_root_sha256) = 64",
            name="ck_collections_archive_root_sha256",
        ),
        CheckConstraint(
            "catalog_revision IS NULL OR catalog_revision > 0",
            name="ck_collections_catalog_revision",
        ),
        CheckConstraint(
            f"description IS NULL OR octet_length(description) <= "
            f"{COLLECTION_DESCRIPTION_UTF8_BYTES_MAX}",
            name="ck_collections_description_bytes",
        ),
        CheckConstraint(
            "description IS NULL AND description_search = '' OR "
            "description IS NOT NULL AND length(description_search) > 0",
            name="ck_collections_description_search",
        ),
        CheckConstraint(
            f"description_revision >= 0 AND description_revision <= "
            f"{MAX_COLLECTION_DESCRIPTION_REVISION}",
            name="ck_collections_description_revision",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("description_identity", 64),
            name="ck_collections_description_identity",
        ),
        CheckConstraint(
            "description_revision > 0 OR description IS NULL",
            name="ck_collections_initial_description",
        ),
        CheckConstraint(
            "description_mutation_state IN ('idle','pending','publishing','retry_wait')",
            name="ck_collections_description_mutation_state",
        ),
        CheckConstraint(
            "description_mutation_state = 'idle' AND pending_description_revision IS NULL "
            "AND pending_description_identity IS NULL AND description_next_attempt_at IS NULL OR "
            "description_mutation_state != 'idle' AND pending_description_revision IS NOT NULL "
            "AND pending_description_revision = description_revision + 1 "
            "AND pending_description_identity IS NOT NULL "
            "AND description_next_attempt_at IS NOT NULL",
            name="ck_collections_description_pending",
        ),
        CheckConstraint(
            f"pending_description_revision IS NULL OR pending_description_revision <= "
            f"{MAX_COLLECTION_DESCRIPTION_REVISION}",
            name="ck_collections_pending_description_revision",
        ),
        CheckConstraint(
            f"pending_description IS NULL OR octet_length(pending_description) <= "
            f"{COLLECTION_DESCRIPTION_UTF8_BYTES_MAX}",
            name="ck_collections_pending_description_bytes",
        ),
        CheckConstraint(
            "pending_description_identity IS NULL OR "
            + _fixed_lowercase_integer_check("pending_description_identity", 64),
            name="ck_collections_pending_description_identity",
        ),
        CheckConstraint(
            "description_attempt_count >= 0",
            name="ck_collections_description_attempt_count",
        ),
        CheckConstraint(
            f"tag_revision >= 1 AND tag_revision <= {MAX_COLLECTION_TAG_REVISION}",
            name="ck_collections_tag_revision",
        ),
        CheckConstraint(
            "tag_root_sha256 IS NULL OR " + _fixed_lowercase_integer_check("tag_root_sha256", 64),
            name="ck_collections_tag_root_sha256",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("tag_set_identity", 64),
            name="ck_collections_tag_set_identity",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("tag_head_identity", 64),
            name="ck_collections_tag_head_identity",
        ),
    )


class CollectionTagMembershipRecord(Base):
    __tablename__ = "collection_tag_memberships"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    tag_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    added_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["tag_sha256"], ["collection_tags.tag_sha256"], ondelete="CASCADE"),
        Index("ix_collection_tag_memberships_tag", "tag_sha256", "collection_id"),
    )

    collection: Mapped[CollectionRecord] = relationship(back_populates="tags")
    tag_record: Mapped[CollectionTagRecord] = relationship(back_populates="memberships")


class CollectionTagNodeRecord(Base):
    __tablename__ = "collection_tag_nodes"

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    encoded: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            _fixed_lowercase_integer_check("digest", 64),
            name="ck_collection_tag_nodes_digest",
        ),
        CheckConstraint(
            f"length(encoded) > 0 AND length(encoded) <= {COLLECTION_TAG_NODE_BYTES_MAX}",
            name="ck_collection_tag_nodes_bytes",
        ),
    )


class CollectionTagNodeReclamationRecord(Base):
    """Durable fence while one unowned catalog tag node is dismantled."""

    __tablename__ = "collection_tag_node_reclamations"

    node_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    claimed_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            _fixed_lowercase_integer_check("node_digest", 64),
            name="ck_collection_tag_node_reclamations_digest",
        ),
    )


class CollectionTagNodeEdgeRecord(Base):
    """One immutable structural edge in the content-addressed tag tree."""

    __tablename__ = "collection_tag_node_edges"

    parent_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    child_digest: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["parent_digest"],
            ["collection_tag_nodes.digest"],
        ),
        ForeignKeyConstraint(["child_digest"], ["collection_tag_nodes.digest"]),
        CheckConstraint(
            _fixed_lowercase_integer_check("parent_digest", 64),
            name="ck_collection_tag_node_edges_parent_digest",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("child_digest", 64),
            name="ck_collection_tag_node_edges_child_digest",
        ),
        Index(
            "ix_collection_tag_node_edges_child",
            "child_digest",
            "parent_digest",
        ),
    )


class CollectionTagRevisionRecord(Base):
    __tablename__ = "collection_tag_revisions"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    root_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tag_set_identity: Mapped[str] = mapped_column(String(64))
    head_identity: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String)
    cleanup_started_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
        CheckConstraint(
            f"revision >= 1 AND revision <= {MAX_COLLECTION_TAG_REVISION}",
            name="ck_collection_tag_revisions_revision",
        ),
        CheckConstraint(
            "root_sha256 IS NULL OR " + _fixed_lowercase_integer_check("root_sha256", 64),
            name="ck_collection_tag_revisions_root",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("tag_set_identity", 64),
            name="ck_collection_tag_revisions_set_identity",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("head_identity", 64),
            name="ck_collection_tag_revisions_head_identity",
        ),
        Index(
            "ix_collection_tag_revisions_cleanup",
            "cleanup_started_at",
            "collection_id",
            "revision",
        ),
    )


class CollectionTagMutationRecord(Base):
    __tablename__ = "collection_tag_mutations"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    operation_id: Mapped[str] = mapped_column(String, primary_key=True)
    action: Mapped[str] = mapped_column(String)
    tag: Mapped[str] = mapped_column(Text)
    tag_sha256: Mapped[str] = mapped_column(String(64))
    expected_revision: Mapped[int] = mapped_column(BigInteger)
    expected_tag_set_identity: Mapped[str] = mapped_column(String(64))
    result_revision: Mapped[int] = mapped_column(BigInteger)
    result_root_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_tag_set_identity: Mapped[str] = mapped_column(String(64))
    result_head_identity: Mapped[str] = mapped_column(String(64))
    changed: Mapped[bool] = mapped_column(Boolean)
    state: Mapped[str] = mapped_column(String)
    initiated_by_app: Mapped[str] = mapped_column(String)
    initiated_by_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
        CheckConstraint("action IN ('add','remove')", name="ck_collection_tag_mutations_action"),
        CheckConstraint(
            "state IN ('pending','retry_wait','succeeded')",
            name="ck_collection_tag_mutations_state",
        ),
        CheckConstraint(
            f"octet_length(tag) > 0 AND octet_length(tag) <= {COLLECTION_TAG_UTF8_BYTES_MAX}",
            name="ck_collection_tag_mutations_tag_bytes",
        ),
        CheckConstraint(
            f"expected_revision >= 1 AND expected_revision < {MAX_COLLECTION_TAG_REVISION}",
            name="ck_collection_tag_mutations_expected_revision",
        ),
        CheckConstraint(
            "changed AND result_revision = expected_revision + 1 OR "
            "NOT changed AND result_revision = expected_revision",
            name="ck_collection_tag_mutations_result_revision",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("tag_sha256", 64),
            name="ck_collection_tag_mutations_tag_sha256",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("expected_tag_set_identity", 64),
            name="ck_collection_tag_mutations_expected_identity",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("result_tag_set_identity", 64),
            name="ck_collection_tag_mutations_result_identity",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("result_head_identity", 64),
            name="ck_collection_tag_mutations_head_identity",
        ),
        Index("ix_collection_tag_mutations_work", "state", "updated_at", "collection_id"),
    )


class CollectionTagMutationNodeReferenceRecord(Base):
    """Construction protection for nodes introduced by one unsettled mutation."""

    __tablename__ = "collection_tag_mutation_node_references"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    operation_id: Mapped[str] = mapped_column(String, primary_key=True)
    node_digest: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "operation_id"],
            ["collection_tag_mutations.collection_id", "collection_tag_mutations.operation_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["node_digest"], ["collection_tag_nodes.digest"]),
        CheckConstraint(
            _fixed_lowercase_integer_check("node_digest", 64),
            name="ck_collection_tag_mutation_node_references_digest",
        ),
        Index(
            "ix_collection_tag_mutation_node_references_digest",
            "node_digest",
            "collection_id",
            "operation_id",
        ),
    )


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
    ordinal: Mapped[int] = mapped_column(authority_ordinal_type(), primary_key=True)
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
        CheckConstraint(
            _fixed_lowercase_integer_check("ordinal", 64),
            name="ck_provenance_journal_chunks_ordinal",
        ),
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
        order_by="CollectionArchiveObjectRecord.object_order",
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
    description_publication: Mapped[CollectionDescriptionPublicationRecord | None] = relationship(
        back_populates="copy",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    tag_publication: Mapped[CollectionTagPublicationRecord | None] = relationship(
        back_populates="copy",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class CollectionDescriptionPublicationRecord(Base):
    __tablename__ = "collection_description_publications"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    store: Mapped[str] = mapped_column(String, primary_key=True)
    desired_revision: Mapped[int] = mapped_column(BigInteger)
    desired_identity: Mapped[str] = mapped_column(String(64))
    published_revision: Mapped[int] = mapped_column(BigInteger)
    published_identity: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    next_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_revision: Mapped[str | None] = mapped_column(String, nullable=True)
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
            "ix_collection_description_publications_due",
            "state",
            "next_attempt_at",
            "collection_id",
            "store",
        ),
        CheckConstraint(
            f"desired_revision >= 0 AND desired_revision <= {MAX_COLLECTION_DESCRIPTION_REVISION}",
            name="ck_description_publications_desired_revision",
        ),
        CheckConstraint(
            f"published_revision >= 0 AND published_revision <= "
            f"{MAX_COLLECTION_DESCRIPTION_REVISION}",
            name="ck_description_publications_published_revision",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("desired_identity", 64),
            name="ck_description_publications_desired_identity",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("published_identity", 64),
            name="ck_description_publications_published_identity",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_description_publications_attempt_count"),
        CheckConstraint(
            "state IN ('pending','publishing','published','retry_wait')",
            name="ck_description_publications_state",
        ),
        CheckConstraint(
            "state = 'published' AND next_attempt_at IS NULL OR "
            "state != 'published' AND next_attempt_at IS NOT NULL",
            name="ck_description_publications_next_attempt",
        ),
        CheckConstraint(
            "published_revision = 0 AND object_path IS NULL AND stored_bytes IS NULL "
            "AND stored_sha256 IS NULL OR published_revision > 0 AND object_path IS NOT NULL "
            "AND stored_bytes IS NOT NULL AND stored_sha256 IS NOT NULL",
            name="ck_description_publications_receipt",
        ),
    )

    copy: Mapped[CollectionArchiveCopyRecord] = relationship(
        back_populates="description_publication"
    )


class CollectionMutableDocumentReclamationRecord(Base):
    """Exact provider revision retained until superseded sidecar reclamation."""

    __tablename__ = "collection_mutable_document_reclamations"

    receipt_identity: Mapped[str] = mapped_column(String(64), primary_key=True)
    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE)
    store: Mapped[str] = mapped_column(String)
    document_kind: Mapped[str] = mapped_column(String)
    object_path: Mapped[str] = mapped_column(String)
    provider_revision: Mapped[str] = mapped_column(String)
    stored_bytes: Mapped[int] = mapped_column(BigInteger)
    stored_sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String, default="pending")
    next_attempt_at: Mapped[str] = mapped_column(String)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            _fixed_lowercase_integer_check("receipt_identity", 64),
            name="ck_mutable_document_reclamations_identity",
        ),
        CheckConstraint(
            "document_kind IN ('description','tag_head')",
            name="ck_mutable_document_reclamations_kind",
        ),
        CheckConstraint("stored_bytes > 0", name="ck_mutable_document_reclamations_bytes"),
        CheckConstraint(
            _fixed_lowercase_integer_check("stored_sha256", 64),
            name="ck_mutable_document_reclamations_stored_sha256",
        ),
        CheckConstraint(
            "state IN ('pending','deleting','retry_wait')",
            name="ck_mutable_document_reclamations_state",
        ),
        Index(
            "ix_collection_mutable_document_reclamations_due",
            "state",
            "next_attempt_at",
            "receipt_identity",
        ),
        Index(
            "ix_collection_mutable_document_reclamations_owner",
            "collection_id",
            "store",
            "document_kind",
        ),
    )


class CollectionTagPublicationRecord(Base):
    __tablename__ = "collection_tag_publications"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    store: Mapped[str] = mapped_column(String, primary_key=True)
    desired_revision: Mapped[int] = mapped_column(BigInteger)
    desired_tag_set_identity: Mapped[str] = mapped_column(String(64))
    desired_head_identity: Mapped[str] = mapped_column(String(64))
    published_revision: Mapped[int] = mapped_column(BigInteger, default=0)
    published_tag_set_identity: Mapped[str] = mapped_column(
        String(64), default=lambda: collection_tag_set_identity(None)
    )
    published_head_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String)
    next_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)
    head_object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    head_provider_revision: Mapped[str | None] = mapped_column(String, nullable=True)
    head_stored_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    head_stored_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "store"],
            ["collection_archive_copies.collection_id", "collection_archive_copies.store"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            f"desired_revision >= 1 AND desired_revision <= {MAX_COLLECTION_TAG_REVISION}",
            name="ck_collection_tag_publications_desired_revision",
        ),
        CheckConstraint(
            f"published_revision >= 0 AND published_revision <= {MAX_COLLECTION_TAG_REVISION}",
            name="ck_collection_tag_publications_published_revision",
        ),
        CheckConstraint(
            "state IN ('pending','publishing_nodes','publishing_head','published','retry_wait')",
            name="ck_collection_tag_publications_state",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("desired_tag_set_identity", 64),
            name="ck_collection_tag_publications_desired_set_identity",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("desired_head_identity", 64),
            name="ck_collection_tag_publications_desired_head_identity",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("published_tag_set_identity", 64),
            name="ck_collection_tag_publications_published_set_identity",
        ),
        Index(
            "ix_collection_tag_publications_due",
            "state",
            "next_attempt_at",
            "collection_id",
            "store",
        ),
    )

    copy: Mapped[CollectionArchiveCopyRecord] = relationship(back_populates="tag_publication")


class CollectionTagPublicationFrontierRecord(Base):
    __tablename__ = "collection_tag_publication_frontier"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    store: Mapped[str] = mapped_column(String, primary_key=True)
    head_identity: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    expanded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    published: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "store"],
            ["collection_tag_publications.collection_id", "collection_tag_publications.store"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("head_identity", 64),
            name="ck_collection_tag_publication_frontier_head",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("node_digest", 64),
            name="ck_collection_tag_publication_frontier_node",
        ),
        Index(
            "ix_collection_tag_publication_frontier_work",
            "collection_id",
            "store",
            "head_identity",
            "published",
            "expanded",
            "node_digest",
        ),
    )


class CollectionTagPublishedNodeRecord(Base):
    __tablename__ = "collection_tag_published_nodes"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    store: Mapped[str] = mapped_column(String, primary_key=True)
    node_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_path: Mapped[str] = mapped_column(String)
    provider_revision: Mapped[str | None] = mapped_column(String, nullable=True)
    stored_bytes: Mapped[int] = mapped_column(BigInteger)
    stored_sha256: Mapped[str] = mapped_column(String(64))
    published_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "store"],
            ["collection_archive_copies.collection_id", "collection_archive_copies.store"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("node_digest", 64),
            name="ck_collection_tag_published_nodes_digest",
        ),
        CheckConstraint("stored_bytes > 0", name="ck_collection_tag_published_nodes_bytes"),
        CheckConstraint(
            _fixed_lowercase_integer_check("stored_sha256", 64),
            name="ck_collection_tag_published_nodes_stored_sha256",
        ),
    )


class CollectionTagNodeGcRecord(Base):
    """One fenced exact obsolete-node reclamation effect."""

    __tablename__ = "collection_tag_node_gc"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    store: Mapped[str] = mapped_column(String, primary_key=True)
    node_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    expected_head_identity: Mapped[str] = mapped_column(String(64))
    object_path: Mapped[str] = mapped_column(String)
    provider_revision: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String, default="pending")
    next_attempt_at: Mapped[str] = mapped_column(String)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "store", "node_digest"],
            [
                "collection_tag_published_nodes.collection_id",
                "collection_tag_published_nodes.store",
                "collection_tag_published_nodes.node_digest",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("node_digest", 64),
            name="ck_collection_tag_node_gc_digest",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("expected_head_identity", 64),
            name="ck_collection_tag_node_gc_head",
        ),
        CheckConstraint(
            "state IN ('pending','deleting','retry_wait')",
            name="ck_collection_tag_node_gc_state",
        ),
        Index(
            "ix_collection_tag_node_gc_due",
            "state",
            "next_attempt_at",
            "collection_id",
            "store",
            "node_digest",
        ),
    )


class CollectionArchiveObjectRecord(Base):
    __tablename__ = "collection_archive_objects"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    store: Mapped[str] = mapped_column(String, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_order: Mapped[int] = mapped_column(archive_object_order_type())
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
        CheckConstraint(
            _fixed_lowercase_integer_check("object_order", 65),
            name="ck_collection_archive_objects_order",
        ),
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
    sequence: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
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
    batch_start_order: Mapped[int | None] = mapped_column(
        archive_object_order_type(), nullable=True
    )
    batch_end_order: Mapped[int | None] = mapped_column(archive_object_order_type(), nullable=True)
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
        CheckConstraint(
            "batch_start_order IS NULL OR "
            f"{_fixed_lowercase_integer_check('batch_start_order', 65)}",
            name="ck_archive_copy_jobs_batch_start_order",
        ),
        CheckConstraint(
            f"batch_end_order IS NULL OR {_fixed_lowercase_integer_check('batch_end_order', 65)}",
            name="ck_archive_copy_jobs_batch_end_order",
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

    sequence: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True, autoincrement=True)
    revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True, unique=True)
    change: Mapped[str] = mapped_column(String)
    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE)
    occurred_at: Mapped[str] = mapped_column(String)
    inventory_identity: Mapped[str] = mapped_column(String(64))
    archive_root_sha256: Mapped[str] = mapped_column(String(64))
    content_identity: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    description_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    tag_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tag_set_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    before_tag_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    after_tag_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    committed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    published: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )

    __table_args__ = (
        Index("ix_catalog_events_collection", "collection_id", "sequence"),
        Index("ix_catalog_events_published", "published", "sequence"),
        Index("ix_catalog_events_revision", "revision"),
        Index("ix_catalog_events_committed", "committed_at", "revision"),
        CheckConstraint(
            "revision IS NULL OR revision > 0",
            name="ck_catalog_events_revision",
        ),
        CheckConstraint(
            f"description IS NULL OR octet_length(description) <= "
            f"{COLLECTION_DESCRIPTION_UTF8_BYTES_MAX}",
            name="ck_catalog_events_description_bytes",
        ),
        CheckConstraint(
            f"description_revision >= 0 AND description_revision <= "
            f"{MAX_COLLECTION_DESCRIPTION_REVISION}",
            name="ck_catalog_events_description_revision",
        ),
        CheckConstraint(
            f"tag_revision >= 1 AND tag_revision <= {MAX_COLLECTION_TAG_REVISION}",
            name="ck_catalog_events_tag_revision",
        ),
        CheckConstraint(
            f"before_tag_revision IS NULL OR before_tag_revision >= 1 AND "
            f"before_tag_revision <= {MAX_COLLECTION_TAG_REVISION}",
            name="ck_catalog_events_before_tag_revision",
        ),
        CheckConstraint(
            f"after_tag_revision IS NULL OR after_tag_revision >= 1 AND "
            f"after_tag_revision <= {MAX_COLLECTION_TAG_REVISION}",
            name="ck_catalog_events_after_tag_revision",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("tag_set_identity", 64),
            name="ck_catalog_events_tag_set_identity",
        ),
    )


class CatalogSyncStateRecord(Base):
    __tablename__ = "catalog_sync_state"

    singleton: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_identity: Mapped[str] = mapped_column(String(64))
    committed_revision: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    retained_revision: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))

    __table_args__ = (
        CheckConstraint("singleton = 1", name="ck_catalog_sync_state_singleton"),
        CheckConstraint("committed_revision >= 0", name="ck_catalog_sync_state_committed_revision"),
        CheckConstraint(
            "retained_revision >= 0 AND retained_revision <= committed_revision",
            name="ck_catalog_sync_state_retained_revision",
        ),
    )


class CollectionTagVisibilityRecord(Base):
    """One half-open tag-revision interval used by historical authorization."""

    __tablename__ = "collection_tag_visibility"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    tag_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    start_revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    end_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        CheckConstraint(
            _fixed_lowercase_integer_check("tag_sha256", 64),
            name="ck_collection_tag_visibility_sha256",
        ),
        CheckConstraint(
            f"start_revision >= 1 AND start_revision <= {MAX_COLLECTION_TAG_REVISION}",
            name="ck_collection_tag_visibility_start",
        ),
        CheckConstraint(
            f"end_revision IS NULL OR end_revision > start_revision AND "
            f"end_revision <= {MAX_COLLECTION_TAG_REVISION}",
            name="ck_collection_tag_visibility_end",
        ),
        Index(
            "ix_collection_tag_visibility_lookup",
            "collection_id",
            "tag_sha256",
            "start_revision",
            "end_revision",
        ),
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
        Index(
            "ix_lifecycle_events_context_expiry",
            "context_expires_at",
            "sequence",
        ),
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


class RetrievalPlanRecord(Base):
    __tablename__ = "retrieval_plans"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    app: Mapped[str] = mapped_column(String)
    initiated_by_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String)
    creation_identity_sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String)
    request_json: Mapped[str] = mapped_column(Text)
    lease_seconds: Mapped[int] = mapped_column(BigInteger)
    restore_policy: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
    ready_at: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[str] = mapped_column(String)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_file_order: Mapped[int] = mapped_column(Integer, default=0)
    next_placement_sequence: Mapped[int] = mapped_column(authority_ordinal_type(), default=0)
    object_count: Mapped[int] = mapped_column(authority_ordinal_type(), default=0)
    retrieval_bytes: Mapped[int] = mapped_column(authority_ordinal_type(), default=0)
    requires_restore: Mapped[bool] = mapped_column(Boolean, default=False)
    file_commitment_sha256: Mapped[str] = mapped_column(String(64))
    segment_commitment_sha256: Mapped[str] = mapped_column(String(64))
    etag: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_retrieval_plans_owner", "app", "initiated_by_key_id", "id"),
        UniqueConstraint(
            "app",
            "initiated_by_key_id",
            "idempotency_key",
            name="uq_retrieval_plans_key_idempotency",
        ),
        CheckConstraint(
            "state IN ('planning','ready','consumed','expired','failed')",
            name="ck_retrieval_plans_state",
        ),
        CheckConstraint("lease_seconds > 0", name="ck_retrieval_plans_lease"),
        CheckConstraint(
            "restore_policy IN ('allow','never')",
            name="ck_retrieval_plans_restore_policy",
        ),
        CheckConstraint("next_file_order >= 0", name="ck_retrieval_plans_file_order"),
    )

    files: Mapped[list[RetrievalPlanFileRecord]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
    )
    objects: Mapped[list[RetrievalPlanObjectRecord]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
    )
    jobs: Mapped[list[RetrievalJobRecord]] = relationship(back_populates="plan")


class RetrievalPlanFileRecord(Base):
    __tablename__ = "retrieval_plan_files"

    plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    file_order: Mapped[int] = mapped_column(Integer, primary_key=True)
    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE)
    path: Mapped[str] = mapped_column(String)
    bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    source_store: Mapped[str] = mapped_column(String)
    requires_restore: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        ForeignKeyConstraint(["plan_id"], ["retrieval_plans.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["collection_id", "path"],
            ["collection_files.collection_id", "collection_files.path"],
        ),
        UniqueConstraint("plan_id", "collection_id", "path"),
        Index("ix_retrieval_plan_files_collection", "collection_id", "plan_id"),
        CheckConstraint("file_order >= 0", name="ck_retrieval_plan_files_order"),
        CheckConstraint("bytes >= 0", name="ck_retrieval_plan_files_bytes"),
    )

    plan: Mapped[RetrievalPlanRecord] = relationship(back_populates="files")


class RetrievalPlanObjectRecord(Base):
    __tablename__ = "retrieval_plan_objects"

    plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_order: Mapped[int] = mapped_column(authority_ordinal_type(), primary_key=True)
    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE)
    source_store: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    plaintext_bytes: Mapped[int] = mapped_column(BigInteger)
    stored_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    read_mode: Mapped[str] = mapped_column(String)
    cache_store: Mapped[str | None] = mapped_column(String, nullable=True)
    retrieval_bytes: Mapped[int] = mapped_column(authority_ordinal_type(), default=0)

    __table_args__ = (
        ForeignKeyConstraint(["plan_id"], ["retrieval_plans.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["collection_id", "source_store", "object_id"],
            [
                "collection_archive_objects.collection_id",
                "collection_archive_objects.store",
                "collection_archive_objects.object_id",
            ],
        ),
        UniqueConstraint("plan_id", "collection_id", "source_store", "object_id"),
        Index(
            "ix_retrieval_plan_objects_copy",
            "collection_id",
            "source_store",
            "plan_id",
        ),
        CheckConstraint("kind IN ('pack','segment')", name="ck_retrieval_plan_objects_kind"),
        CheckConstraint(
            "read_mode IN ('immediate','restore_required','cache')",
            name="ck_retrieval_plan_objects_read_mode",
        ),
        CheckConstraint("plaintext_bytes >= 0", name="ck_retrieval_plan_objects_plaintext"),
        CheckConstraint("stored_bytes > 0", name="ck_retrieval_plan_objects_stored"),
    )

    plan: Mapped[RetrievalPlanRecord] = relationship(back_populates="objects")


class RetrievalPlanPlacementRecord(Base):
    __tablename__ = "retrieval_plan_placements"

    plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    file_order: Mapped[int] = mapped_column(Integer, primary_key=True)
    sequence: Mapped[int] = mapped_column(authority_ordinal_type(), primary_key=True)
    object_order: Mapped[int] = mapped_column(authority_ordinal_type())
    file_offset: Mapped[int] = mapped_column(BigInteger)
    object_offset: Mapped[int] = mapped_column(BigInteger)
    bytes: Mapped[int] = mapped_column(BigInteger)
    member: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "file_order"],
            ["retrieval_plan_files.plan_id", "retrieval_plan_files.file_order"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["plan_id", "object_order"],
            ["retrieval_plan_objects.plan_id", "retrieval_plan_objects.object_order"],
        ),
        Index("ix_retrieval_plan_placements_object", "plan_id", "object_order"),
        CheckConstraint("file_offset >= 0", name="ck_retrieval_plan_placements_file_offset"),
        CheckConstraint("object_offset >= 0", name="ck_retrieval_plan_placements_object_offset"),
        CheckConstraint("bytes >= 0", name="ck_retrieval_plan_placements_bytes"),
    )


class RetrievalJobRecord(Base):
    __tablename__ = "retrieval_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[str] = mapped_column(String, unique=True)
    app: Mapped[str] = mapped_column(String)
    initiated_by_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event_context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String)
    plan_etag: Mapped[str] = mapped_column(String(64))
    lease_seconds: Mapped[int] = mapped_column(BigInteger)
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
        ForeignKeyConstraint(["plan_id"], ["retrieval_plans.id"]),
        UniqueConstraint("id", "plan_id"),
        Index("ix_retrieval_jobs_due", "state", "next_poll_at", "id"),
        CheckConstraint(
            "state IN ('requested','ready','completed','canceled','expired','failed')",
            name="ck_retrieval_jobs_state",
        ),
        CheckConstraint("length(plan_etag) = 64", name="ck_retrieval_jobs_plan_etag"),
        CheckConstraint("lease_seconds > 0", name="ck_retrieval_jobs_lease"),
    )

    plan: Mapped[RetrievalPlanRecord] = relationship(back_populates="jobs")
    progress: Mapped[list[RetrievalJobObjectProgressRecord]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )


class RetrievalJobObjectProgressRecord(Base):
    __tablename__ = "retrieval_job_object_progress"

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_order: Mapped[int] = mapped_column(authority_ordinal_type(), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    prepare_requested_at: Mapped[str | None] = mapped_column(String, nullable=True)
    next_poll_at: Mapped[str] = mapped_column(String)
    cache_store: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["job_id", "plan_id"],
            ["retrieval_jobs.id", "retrieval_jobs.plan_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["plan_id", "object_order"],
            ["retrieval_plan_objects.plan_id", "retrieval_plan_objects.object_order"],
        ),
        CheckConstraint(
            "state IN ('preparing','requested','ready')",
            name="ck_retrieval_job_object_progress_state",
        ),
        Index("ix_retrieval_job_object_progress_due", "state", "next_poll_at", "job_id"),
    )

    job: Mapped[RetrievalJobRecord] = relationship(back_populates="progress")


class RetrievalCacheObjectRecord(Base):
    __tablename__ = "retrieval_cache_objects"

    source_store: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    cache_store: Mapped[str] = mapped_column(String)
    object_path: Mapped[str] = mapped_column(String)
    revision: Mapped[str | None] = mapped_column(String, nullable=True)
    stored_bytes: Mapped[int] = mapped_column(BigInteger)
    stored_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cached_at: Mapped[str] = mapped_column(String)
    verified_at: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="ready")
    search_text: Mapped[str] = mapped_column(
        String,
        Computed("lower(source_store || ' ' || cache_store || ' ' || object_id)"),
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
            "ix_retrieval_cache_objects_store_cleanup",
            "cache_store",
            "state",
            "cached_at",
            "collection_id",
            "source_store",
            "object_id",
        ),
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
        CheckConstraint(
            "stored_sha256 IS NULL OR length(stored_sha256) = 64",
            name="ck_retrieval_cache_objects_sha256",
        ),
        CheckConstraint(
            "state IN ('ready','delete_pending','deleting')",
            name="ck_retrieval_cache_objects_state",
        ),
    )


class RetrievalCachePopulationRecord(Base):
    __tablename__ = "retrieval_cache_populations"

    source_store: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    cache_store: Mapped[str | None] = mapped_column(String, nullable=True)
    object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    write_token: Mapped[str | None] = mapped_column(String, nullable=True)
    expected_bytes: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String)
    initiated_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "ix_retrieval_cache_populations_store_state",
            "cache_store",
            "state",
            "updated_at",
            "collection_id",
            "source_store",
            "object_id",
        ),
        CheckConstraint(
            "expected_bytes >= 1",
            name="ck_retrieval_cache_populations_expected_bytes",
        ),
        CheckConstraint(
            "state IN ('waiting','admitting','admitted','writing','abandoning')",
            name="ck_retrieval_cache_populations_state",
        ),
        CheckConstraint(
            "cache_store IS NULL AND object_path IS NULL AND write_token IS NULL "
            "AND state IN ('waiting','abandoning') OR "
            "cache_store IS NOT NULL AND object_path IS NOT NULL AND "
            "(write_token IS NULL AND state = 'admitting' OR "
            "write_token IS NOT NULL AND state IN ('admitted','writing') OR "
            "state = 'abandoning')",
            name="ck_retrieval_cache_populations_session",
        ),
    )


class RetrievalCachePopulationClaimRecord(Base):
    __tablename__ = "retrieval_cache_population_claims"

    owner: Mapped[str] = mapped_column(String, primary_key=True)
    source_store: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_store", "collection_id", "object_id"],
            [
                "retrieval_cache_populations.source_store",
                "retrieval_cache_populations.collection_id",
                "retrieval_cache_populations.object_id",
            ],
            ondelete="CASCADE",
        ),
        Index(
            "ix_retrieval_cache_population_claims_object",
            "source_store",
            "collection_id",
            "object_id",
            "owner",
        ),
    )


class RetrievalCacheStoreAccountingRecord(Base):
    __tablename__ = "retrieval_cache_store_accounting"

    cache_store: Mapped[str] = mapped_column(String, primary_key=True)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    committed_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    generation: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "reserved_bytes >= 0",
            name="ck_retrieval_cache_store_accounting_reserved",
        ),
        CheckConstraint(
            "committed_bytes >= 0",
            name="ck_retrieval_cache_store_accounting_committed",
        ),
        CheckConstraint(
            "generation >= 0",
            name="ck_retrieval_cache_store_accounting_generation",
        ),
    )


class RetrievalCacheAccountingReconciliationRecord(Base):
    __tablename__ = "retrieval_cache_accounting_reconciliations"

    cache_store: Mapped[str] = mapped_column(String, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger)
    after_source_store: Mapped[str | None] = mapped_column(String, nullable=True)
    after_collection_id: Mapped[int | None] = mapped_column(
        COLLECTION_ID_TYPE,
        nullable=True,
    )
    after_object_id: Mapped[str | None] = mapped_column(String, nullable=True)
    accumulated_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    started_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        ForeignKeyConstraint(
            ["cache_store"],
            ["retrieval_cache_store_accounting.cache_store"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "generation >= 0",
            name="ck_cache_accounting_reconciliations_generation",
        ),
        CheckConstraint(
            "accumulated_bytes >= 0",
            name="ck_cache_accounting_reconciliations_bytes",
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
    ingest_source: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    description_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description_publication_receipt_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    tag_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tag_staging_root_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tag_staging_set_identity: Mapped[str] = mapped_column(
        String(64),
        default=lambda: collection_tag_set_identity(None),
        server_default=text(f"'{collection_tag_set_identity(None)}'"),
    )
    tag_root_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tag_set_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tag_head_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tag_publication_receipt_json: Mapped[str | None] = mapped_column(Text, nullable=True)
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
        archive_sequence_type(), default=0, server_default=text(f"'{_ARCHIVE_SEQUENCE_ZERO}'")
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
        archive_sequence_type(), default=0, server_default=text(f"'{_ARCHIVE_SEQUENCE_ZERO}'")
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
            _fixed_lowercase_integer_check("archive_volume_next_sequence", 64),
            name="ck_collection_uploads_volume_progress",
        ),
        CheckConstraint(
            "provenance_validation_next_file_order >= 0 AND "
            "provenance_archive_next_file_order >= 0 AND "
            "provenance_archive_current_journal_offset >= 0 AND "
            f"{_fixed_lowercase_integer_check('provenance_archive_next_sequence', 64)}",
            name="ck_collection_uploads_provenance_progress",
        ),
        CheckConstraint(
            "catalog_phase IN ("
            "'content-identity','inventory-identity','collection','tags','files','journals',"
            "'provenance-relations','bindings','archive-objects','file-objects',"
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
        CheckConstraint(
            f"description IS NULL OR octet_length(description) <= "
            f"{COLLECTION_DESCRIPTION_UTF8_BYTES_MAX}",
            name="ck_collection_uploads_description_bytes",
        ),
        CheckConstraint(
            "description_revision IS NULL AND description_identity IS NULL OR "
            f"description_revision >= 0 AND description_revision <= "
            f"{MAX_COLLECTION_DESCRIPTION_REVISION} AND description_identity IS NOT NULL",
            name="ck_collection_uploads_description_state",
        ),
        CheckConstraint(
            "tag_revision IS NULL AND tag_set_identity IS NULL AND tag_head_identity IS NULL OR "
            f"tag_revision >= 1 AND tag_revision <= {MAX_COLLECTION_TAG_REVISION} "
            "AND tag_set_identity IS NOT NULL AND tag_head_identity IS NOT NULL",
            name="ck_collection_uploads_tag_state",
        ),
        CheckConstraint(
            "tag_root_sha256 IS NULL OR " + _fixed_lowercase_integer_check("tag_root_sha256", 64),
            name="ck_collection_uploads_tag_root",
        ),
        CheckConstraint(
            "tag_staging_root_sha256 IS NULL OR "
            + _fixed_lowercase_integer_check("tag_staging_root_sha256", 64),
            name="ck_collection_uploads_tag_staging_root",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("tag_staging_set_identity", 64),
            name="ck_collection_uploads_tag_staging_identity",
        ),
        CheckConstraint("archive_attempt_count >= 0", name="ck_collection_uploads_attempt_count"),
        {"sqlite_autoincrement": True},
    )


class CollectionUploadTagRecord(Base):
    __tablename__ = "collection_upload_tags"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    tag_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    tag: Mapped[str] = mapped_column(Text)
    added_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"], ["collection_uploads.collection_id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            f"octet_length(tag) > 0 AND octet_length(tag) <= {COLLECTION_TAG_UTF8_BYTES_MAX}",
            name="ck_collection_upload_tags_bytes",
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("tag_sha256", 64),
            name="ck_collection_upload_tags_sha256",
        ),
        UniqueConstraint("collection_id", "tag", name="uq_collection_upload_tags_value"),
    )


class CollectionUploadTagPublicationFrontierRecord(Base):
    __tablename__ = "collection_upload_tag_publication_frontier"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    node_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    expanded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    published: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_revision: Mapped[str | None] = mapped_column(String, nullable=True)
    stored_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    stored_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"], ["collection_uploads.collection_id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            _fixed_lowercase_integer_check("node_digest", 64),
            name="ck_collection_upload_tag_frontier_digest",
        ),
        CheckConstraint(
            "published = false AND object_path IS NULL AND stored_bytes IS NULL "
            "AND stored_sha256 IS NULL AND published_at IS NULL OR "
            "published = true AND object_path IS NOT NULL AND stored_bytes > 0 "
            "AND stored_sha256 IS NOT NULL AND published_at IS NOT NULL",
            name="ck_collection_upload_tag_frontier_receipt",
        ),
        Index(
            "ix_collection_upload_tag_frontier_work",
            "collection_id",
            "published",
            "expanded",
            "node_digest",
        ),
    )


class CollectionUploadTagNodeReferenceRecord(Base):
    """Keep immutable staging nodes available until upload publication settles."""

    __tablename__ = "collection_upload_tag_node_references"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    node_digest: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"], ["collection_uploads.collection_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["node_digest"], ["collection_tag_nodes.digest"], ondelete="RESTRICT"),
        CheckConstraint(
            _fixed_lowercase_integer_check("node_digest", 64),
            name="ck_collection_upload_tag_node_references_digest",
        ),
        Index(
            "ix_collection_upload_tag_node_references_digest",
            "node_digest",
            "collection_id",
        ),
    )


class CollectionUploadFileRecord(Base):
    __tablename__ = "collection_upload_files"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    path_sort_key: Mapped[bytes] = mapped_column(
        LargeBinary,
        default=lambda context: relpath_sort_key(str(context.get_current_parameters()["path"])),
    )
    semantic_order_rank: Mapped[int] = mapped_column(
        Integer,
        default=lambda context: collection_upload_path_order_key(
            str(context.get_current_parameters()["path"])
        )[0],
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
            "idx_collection_upload_files_semantic_order",
            "collection_id",
            "semantic_order_rank",
            "path_sort_key",
        ),
        Index(
            "ux_collection_upload_files_order",
            "collection_id",
            "file_order",
            unique=True,
        ),
        CheckConstraint("file_order >= 0", name="ck_collection_upload_files_order"),
        CheckConstraint(
            "semantic_order_rank >= 0 AND semantic_order_rank <= 2",
            name="ck_collection_upload_files_semantic_order_rank",
        ),
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
    next_chunk_ordinal: Mapped[int] = mapped_column(
        authority_ordinal_type(),
        default=0,
        server_default=text(f"'{_AUTHORITY_ORDINAL_ZERO}'"),
    )
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
            _fixed_lowercase_integer_check("next_chunk_ordinal", 64),
            name="ck_upload_provenance_journals_next_chunk_ordinal",
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
    ordinal: Mapped[int] = mapped_column(authority_ordinal_type(), primary_key=True)
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
        CheckConstraint(
            _fixed_lowercase_integer_check("ordinal", 64),
            name="ck_upload_provenance_journal_chunks_ordinal",
        ),
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
    sequence: Mapped[int] = mapped_column(archive_sequence_type(), primary_key=True)
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
        CheckConstraint(
            _fixed_lowercase_integer_check("sequence", 64),
            name="ck_upload_provenance_archive_volumes_sequence",
        ),
        CheckConstraint(
            "kind IN ('bindings','journal')",
            name="ck_upload_provenance_archive_volumes_kind",
        ),
    )


class CollectionArchiveObjectUploadRecord(Base):
    __tablename__ = "collection_archive_object_uploads"

    collection_id: Mapped[int] = mapped_column(COLLECTION_ID_TYPE, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    sequence: Mapped[int] = mapped_column(archive_sequence_type())
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
        CheckConstraint(
            _fixed_lowercase_integer_check("sequence", 64),
            name="ck_archive_object_uploads_sequence",
        ),
        CheckConstraint("plaintext_bytes >= 0", name="ck_archive_object_uploads_plaintext"),
        CheckConstraint("source_bytes >= 0", name="ck_archive_object_uploads_source"),
        CheckConstraint(
            "kind = 'pack' AND source_path IS NULL AND source_first_part IS NULL "
            "AND source_part_count IS NULL OR "
            "kind = 'segment' AND source_path IS NOT NULL AND source_first_part >= 0 "
            "AND source_part_count > 0",
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
