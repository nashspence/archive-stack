from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, ForeignKeyConstraint, Index, Integer, String, Text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from riverhog_core.catalog_db import Base


class CollectionRecord(Base):
    __tablename__ = "collections"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    ingest_source: Mapped[str | None] = mapped_column(String, nullable=True)
    notify_json: Mapped[str | None] = mapped_column(String, nullable=True)
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


class CollectionDeletionRecord(Base):
    __tablename__ = "collection_deletions"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    challenge: Mapped[str] = mapped_column(String)
    plan_json: Mapped[str] = mapped_column(Text)
    started_at: Mapped[str] = mapped_column(String)


class ArchiveCopyRetirementRecord(Base):
    __tablename__ = "archive_copy_retirements"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
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

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    hot: Mapped[bool] = mapped_column(Boolean, default=True)
    hot_multipart_upload_id: Mapped[str | None] = mapped_column(String, nullable=True)
    hot_multipart_part_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    hot_multipart_parts_json: Mapped[str | None] = mapped_column(String, nullable=True)
    hot_multipart_uploaded_bytes: Mapped[int | None] = mapped_column(
        BigInteger, default=0, nullable=True
    )
    hot_multipart_uploaded_parts: Mapped[int | None] = mapped_column(
        Integer, default=0, nullable=True
    )
    hot_multipart_total_parts: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            ondelete="CASCADE",
        ),
    )

    collection: Mapped[CollectionRecord] = relationship(back_populates="files")


class CollectionArchiveCopyRecord(Base):
    __tablename__ = "collection_archive_copies"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    store: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String, default="pending")
    archive_storage_prefix: Mapped[str | None] = mapped_column(String, nullable=True)
    backend: Mapped[str | None] = mapped_column(String, nullable=True)
    storage_class: Mapped[str | None] = mapped_column(String, nullable=True)
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


class CollectionArchiveObjectRecord(Base):
    __tablename__ = "collection_archive_objects"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    store: Mapped[str] = mapped_column(String, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_order: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String)
    object_path: Mapped[str] = mapped_column(String)
    plaintext_bytes: Mapped[int] = mapped_column(BigInteger)
    stored_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    backend: Mapped[str] = mapped_column(String)
    storage_class: Mapped[str] = mapped_column(String)
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

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    store: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    object_id: Mapped[str] = mapped_column(String)
    file_offset: Mapped[int] = mapped_column(BigInteger)
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


class ArchiveUsageSnapshotRecord(Base):
    __tablename__ = "archive_usage_snapshots"

    captured_at: Mapped[str] = mapped_column(String, primary_key=True)
    uploaded_collections: Mapped[int] = mapped_column(Integer)
    measured_storage_bytes: Mapped[int] = mapped_column(BigInteger)


class ArchiveCopyJobRecord(Base):
    __tablename__ = "archive_copy_jobs"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    destination_store: Mapped[str] = mapped_column(String, primary_key=True)
    destination_storage_prefix: Mapped[str] = mapped_column(String)
    source_store: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    requested_at: Mapped[str] = mapped_column(String)
    read_requested_at: Mapped[str | None] = mapped_column(String, nullable=True)
    ready_at: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    next_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    failure: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "source_store"],
            ["collection_archive_copies.collection_id", "collection_archive_copies.store"],
            ondelete="CASCADE",
        ),
        Index("ix_archive_copy_jobs_due", "state", "next_attempt_at", "requested_at"),
    )


class ArchiveRestoreRecord(Base):
    __tablename__ = "archive_restores"

    restore_id: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
    requested_at: Mapped[str | None] = mapped_column(String, nullable=True)
    ready_at: Mapped[str | None] = mapped_column(String, nullable=True)
    next_poll_at: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    canceled_at: Mapped[str | None] = mapped_column(String, nullable=True)
    latest_message: Mapped[str | None] = mapped_column(String, nullable=True)
    retrieval_tier: Mapped[str] = mapped_column(String)
    hold_days: Mapped[int] = mapped_column(Integer)
    warnings_json: Mapped[str] = mapped_column(String)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_failure_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_failure: Mapped[str | None] = mapped_column(String, nullable=True)
    last_failure_notification_at: Mapped[str | None] = mapped_column(String, nullable=True)
    started_notification_sent_at: Mapped[str | None] = mapped_column(String, nullable=True)
    started_notification_next_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    started_notification_failure: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_notification_sent_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_notification_next_attempt_at: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    completed_notification_failure: Mapped[str | None] = mapped_column(String, nullable=True)
    canceled_notification_sent_at: Mapped[str | None] = mapped_column(String, nullable=True)
    canceled_notification_next_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    canceled_notification_failure: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_verification_state: Mapped[str | None] = mapped_column(
        String, default="pending", nullable=True
    )
    extraction_state: Mapped[str | None] = mapped_column(String, default="pending", nullable=True)
    materialization_state: Mapped[str | None] = mapped_column(
        String, default="pending", nullable=True
    )
    __table_args__ = (
        Index("ix_archive_restores_state_created", "state", "created_at", "restore_id"),
    )

    files: Mapped[list[ArchiveRestoreFileRecord]] = relationship(
        back_populates="restore",
        cascade="all, delete-orphan",
    )
    objects: Mapped[list[ArchiveRestoreObjectRecord]] = relationship(
        back_populates="restore",
        cascade="all, delete-orphan",
    )


class ArchiveRestoreFileRecord(Base):
    __tablename__ = "archive_restore_files"

    restore_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    archive_store: Mapped[str] = mapped_column(String)
    file_order: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        ForeignKeyConstraint(
            ["restore_id"],
            ["archive_restores.restore_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["collection_id", "path"],
            ["collection_files.collection_id", "collection_files.path"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["collection_id", "archive_store"],
            ["collection_archive_copies.collection_id", "collection_archive_copies.store"],
        ),
        Index("idx_archive_restore_files_order", "restore_id", "file_order"),
    )

    restore: Mapped[ArchiveRestoreRecord] = relationship(back_populates="files")
    archive_copy: Mapped[CollectionArchiveCopyRecord] = relationship()


class ArchiveRestoreObjectRecord(Base):
    __tablename__ = "archive_restore_objects"

    restore_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    archive_store: Mapped[str] = mapped_column(String, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_order: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        ForeignKeyConstraint(
            ["restore_id"],
            ["archive_restores.restore_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["collection_id", "archive_store", "object_id"],
            [
                "collection_archive_objects.collection_id",
                "collection_archive_objects.store",
                "collection_archive_objects.object_id",
            ],
        ),
        Index("idx_archive_restore_objects_order", "restore_id", "object_order"),
    )

    restore: Mapped[ArchiveRestoreRecord] = relationship(back_populates="objects")
    archive_object: Mapped[CollectionArchiveObjectRecord] = relationship()


class FetchRecord(Base):
    __tablename__ = "fetches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String)
    files: Mapped[list[FetchFileRecord]] = relationship(
        back_populates="fetch",
        cascade="all, delete-orphan",
    )


class FetchFileRecord(Base):
    __tablename__ = "fetch_files"

    fetch_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    file_order: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        ForeignKeyConstraint(["fetch_id"], ["fetches.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["collection_id", "path"],
            ["collection_files.collection_id", "collection_files.path"],
            ondelete="CASCADE",
        ),
        Index("idx_fetch_files_order", "fetch_id", "file_order"),
        Index("ix_fetch_files_collection", "collection_id", "fetch_id"),
    )

    fetch: Mapped[FetchRecord] = relationship(back_populates="files")


class CollectionUploadRecord(Base):
    __tablename__ = "collection_uploads"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    ingest_source: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str | None] = mapped_column(String, default="uploading", nullable=True)
    notify_json: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_store: Mapped[str] = mapped_column(String, nullable=False)
    retain_hot: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    opened_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_activity_at: Mapped[str | None] = mapped_column(String, nullable=True)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_phase: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_phase_updated_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_attempt_count: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)
    archive_next_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_last_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_failure: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_last_failure_notification_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_storage_prefix: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_receipt_json: Mapped[str | None] = mapped_column(String, nullable=True)
    collection_manifest_bytes_b64: Mapped[str | None] = mapped_column(String, nullable=True)
    collection_manifest_proof_bytes_b64: Mapped[str | None] = mapped_column(String, nullable=True)

    files: Mapped[list[CollectionUploadFileRecord]] = relationship(
        back_populates="upload",
        cascade="all, delete-orphan",
    )
    archive_objects: Mapped[list[CollectionArchiveObjectUploadRecord]] = relationship(
        back_populates="upload",
        cascade="all, delete-orphan",
    )


class CollectionUploadFileRecord(Base):
    __tablename__ = "collection_upload_files"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    file_order: Mapped[int] = mapped_column(Integer)
    bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    uploaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    upload_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    tus_url: Mapped[str | None] = mapped_column(String, nullable=True)
    hot_materialized_at: Mapped[str | None] = mapped_column(String, nullable=True)
    hot_multipart_upload_id: Mapped[str | None] = mapped_column(String, nullable=True)
    hot_multipart_part_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    hot_multipart_parts_json: Mapped[str | None] = mapped_column(String, nullable=True)
    hot_multipart_uploaded_bytes: Mapped[int | None] = mapped_column(
        BigInteger, default=0, nullable=True
    )
    hot_multipart_uploaded_parts: Mapped[int | None] = mapped_column(
        Integer, default=0, nullable=True
    )
    hot_multipart_total_parts: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_uploads.collection_id"],
            ondelete="CASCADE",
        ),
        Index("idx_collection_upload_files_collection_order", "collection_id", "file_order"),
    )

    upload: Mapped[CollectionUploadRecord] = relationship(back_populates="files")


class CollectionArchiveObjectUploadRecord(Base):
    __tablename__ = "collection_archive_object_uploads"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    object_path: Mapped[str] = mapped_column(String)
    plaintext_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    multipart_upload_id: Mapped[str | None] = mapped_column(String, nullable=True)
    multipart_part_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    multipart_content_length: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    multipart_parts_json: Mapped[str | None] = mapped_column(String, nullable=True)
    encryption_state_json: Mapped[str | None] = mapped_column(String, nullable=True)
    uploaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    uploaded_parts: Mapped[int] = mapped_column(Integer, default=0)
    total_parts: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_uploads.collection_id"],
            ondelete="CASCADE",
        ),
    )

    upload: Mapped[CollectionUploadRecord] = relationship(back_populates="archive_objects")
