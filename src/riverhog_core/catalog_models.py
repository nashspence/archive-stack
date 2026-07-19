from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKeyConstraint, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from riverhog_core.catalog_db import Base


class CollectionRecord(Base):
    __tablename__ = "collections"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    manifest_etag: Mapped[str] = mapped_column(String(64))
    ingest_source: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by_app: Mapped[str] = mapped_column(String, default="riverhog")
    created_by_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
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


class CatalogEventRecord(Base):
    __tablename__ = "catalog_events"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change: Mapped[str] = mapped_column(String)
    collection_id: Mapped[str] = mapped_column(String)
    occurred_at: Mapped[str] = mapped_column(String)
    manifest_etag: Mapped[str] = mapped_column(String(64))

    __table_args__ = (Index("ix_catalog_events_collection", "collection_id", "sequence"),)


class LifecycleEventRecord(Base):
    __tablename__ = "lifecycle_events"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String, unique=True)
    owner_app: Mapped[str] = mapped_column(String)
    event_json: Mapped[str] = mapped_column(Text)
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_lifecycle_events_owner_sequence", "owner_app", "sequence"),
        Index("ix_lifecycle_events_context_expiry", "context_expires_at"),
    )


class AppKeyRecord(Base):
    __tablename__ = "app_keys"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    app: Mapped[str] = mapped_column(String)
    token_sha256: Mapped[str] = mapped_column(String(64))
    permissions_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_used_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_app_keys_app", "app", "id"),
        Index("ux_app_keys_token_sha256", "token_sha256", unique=True),
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
    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
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
    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
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
    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_path: Mapped[str] = mapped_column(String)
    version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    stored_bytes: Mapped[int] = mapped_column(BigInteger)
    stored_sha256: Mapped[str] = mapped_column(String(64))
    cached_at: Mapped[str] = mapped_column(String)
    verified_at: Mapped[str] = mapped_column(String)

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
    )


class RetrievalCacheLeaseRecord(Base):
    __tablename__ = "retrieval_cache_leases"

    owner: Mapped[str] = mapped_column(String, primary_key=True)
    source_store: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
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

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    ingest_source: Mapped[str | None] = mapped_column(String, nullable=True)
    initiated_by_app: Mapped[str] = mapped_column(String, default="riverhog")
    initiated_by_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event_context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str | None] = mapped_column(String, default="uploading", nullable=True)
    archive_store: Mapped[str] = mapped_column(String, nullable=False)
    opened_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_activity_at: Mapped[str | None] = mapped_column(String, nullable=True)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_phase: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_phase_updated_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_attempt_count: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)
    archive_next_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_last_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_failure: Mapped[str | None] = mapped_column(String, nullable=True)
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
    ingress_bytes: Mapped[int] = mapped_column(BigInteger)
    ingress_uploaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    ingress_secret_envelope: Mapped[str] = mapped_column(Text)
    ingress_state_json: Mapped[str] = mapped_column(Text)
    ingress_upload_id: Mapped[str] = mapped_column(String)
    upload_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    tus_url: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_uploads.collection_id"],
            ondelete="CASCADE",
        ),
        Index("idx_collection_upload_files_collection_order", "collection_id", "file_order"),
        Index("ux_collection_upload_files_ingress_id", "ingress_upload_id", unique=True),
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
    cache_object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    cache_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    cache_stored_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_stored_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cache_cached_at: Mapped[str | None] = mapped_column(String, nullable=True)
    cache_verified_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_uploads.collection_id"],
            ondelete="CASCADE",
        ),
    )

    upload: Mapped[CollectionUploadRecord] = relationship(back_populates="archive_objects")
