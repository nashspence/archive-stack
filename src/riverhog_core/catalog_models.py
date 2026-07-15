from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, ForeignKeyConstraint, Index, Integer, String, Text
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
    )
    archive: Mapped[CollectionArchiveRecord | None] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
        uselist=False,
    )


class CollectionDeletionRecord(Base):
    __tablename__ = "collection_deletions"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    challenge: Mapped[str] = mapped_column(String)
    plan_json: Mapped[str] = mapped_column(Text)
    started_at: Mapped[str] = mapped_column(String)


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


class CollectionArchiveRecord(Base):
    __tablename__ = "collection_archives"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String, default="pending")
    archive_storage_prefix: Mapped[str | None] = mapped_column(String, nullable=True)
    object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    stored_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backend: Mapped[str | None] = mapped_column(String, nullable=True)
    storage_class: Mapped[str | None] = mapped_column(String, nullable=True)
    last_uploaded_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_verified_at: Mapped[str | None] = mapped_column(String, nullable=True)
    failure: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_format: Mapped[str | None] = mapped_column(String, nullable=True)
    compression: Mapped[str | None] = mapped_column(String, nullable=True)
    manifest_object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_stored_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    manifest_uploaded_at: Mapped[str | None] = mapped_column(String, nullable=True)
    ots_object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    ots_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ots_stored_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ots_uploaded_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            ondelete="CASCADE",
        ),
    )

    collection: Mapped[CollectionRecord] = relationship(back_populates="archive")


class ArchiveUsageSnapshotRecord(Base):
    __tablename__ = "archive_usage_snapshots"

    captured_at: Mapped[str] = mapped_column(String, primary_key=True)
    uploaded_collections: Mapped[int] = mapped_column(Integer)
    measured_storage_bytes: Mapped[int] = mapped_column(BigInteger)


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

    collections: Mapped[list[ArchiveRestoreCollectionRecord]] = relationship(
        back_populates="restore",
        cascade="all, delete-orphan",
    )


class ArchiveRestoreCollectionRecord(Base):
    __tablename__ = "archive_restore_collections"

    restore_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_order: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        ForeignKeyConstraint(
            ["restore_id"],
            ["archive_restores.restore_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            ondelete="CASCADE",
        ),
    )

    restore: Mapped[ArchiveRestoreRecord] = relationship(back_populates="collections")


class FetchRecord(Base):
    __tablename__ = "fetches"

    fetch_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    fetch_order: Mapped[int] = mapped_column(Integer, unique=True)
    fetch_state: Mapped[str] = mapped_column(String)
    collections: Mapped[list[FetchCollectionRecord]] = relationship(
        back_populates="fetch",
        cascade="all, delete-orphan",
    )


class FetchCollectionRecord(Base):
    __tablename__ = "fetch_collections"

    fetch_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_order: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        ForeignKeyConstraint(["fetch_id"], ["fetches.fetch_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
        Index("idx_fetch_collections_order", "fetch_id", "collection_order"),
        Index("ix_fetch_collections_collection", "collection_id", "fetch_id"),
    )

    fetch: Mapped[FetchRecord] = relationship(back_populates="collections")


class CollectionUploadRecord(Base):
    __tablename__ = "collection_uploads"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    ingest_source: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str | None] = mapped_column(String, default="uploading", nullable=True)
    notify_json: Mapped[str | None] = mapped_column(String, nullable=True)
    retain_hot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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
    archive_object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_multipart_upload_id: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_multipart_part_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    archive_multipart_content_length: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    archive_multipart_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archive_multipart_parts_json: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_encryption_state_json: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_multipart_uploaded_bytes: Mapped[int | None] = mapped_column(
        BigInteger, default=0, nullable=True
    )
    archive_multipart_uploaded_parts: Mapped[int | None] = mapped_column(
        Integer, default=0, nullable=True
    )
    archive_multipart_total_parts: Mapped[int | None] = mapped_column(
        Integer, default=0, nullable=True
    )
    archive_receipt_json: Mapped[str | None] = mapped_column(String, nullable=True)
    collection_manifest_bytes_b64: Mapped[str | None] = mapped_column(String, nullable=True)
    collection_manifest_proof_bytes_b64: Mapped[str | None] = mapped_column(String, nullable=True)

    files: Mapped[list[CollectionUploadFileRecord]] = relationship(
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
    hot_promoted_at: Mapped[str | None] = mapped_column(String, nullable=True)
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
