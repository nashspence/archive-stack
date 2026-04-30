from __future__ import annotations

from sqlalchemy import Boolean, ForeignKeyConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from arc_core.sqlite_db import Base


class CollectionRecord(Base):
    __tablename__ = "collections"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    ingest_source: Mapped[str | None] = mapped_column(String, nullable=True)
    files: Mapped[list[CollectionFileRecord]] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
    )
    archive: Mapped[CollectionArchiveRecord | None] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
        uselist=False,
    )


class CollectionFileRecord(Base):
    __tablename__ = "collection_files"

    collection_id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
    )
    path: Mapped[str] = mapped_column(String, primary_key=True)
    bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    hot: Mapped[bool] = mapped_column(Boolean, default=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            ondelete="CASCADE",
        ),
    )

    collection: Mapped[CollectionRecord] = relationship(back_populates="files")
    copies: Mapped[list[FileCopyRecord]] = relationship(
        back_populates="file",
        cascade="all, delete-orphan",
    )


class FileCopyRecord(Base):
    __tablename__ = "file_copies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection_id: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    copy_id: Mapped[str] = mapped_column(String)
    volume_id: Mapped[str] = mapped_column(String)
    location: Mapped[str] = mapped_column(String)
    disc_path: Mapped[str] = mapped_column(String)
    enc_json: Mapped[str] = mapped_column(String)
    part_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    part_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    part_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    part_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "path"],
            ["collection_files.collection_id", "collection_files.path"],
            ondelete="CASCADE",
        ),
    )

    file: Mapped[CollectionFileRecord] = relationship(back_populates="copies")


class CollectionArchiveRecord(Base):
    __tablename__ = "collection_archives"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String, default="pending")
    object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    stored_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    manifest_stored_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    manifest_uploaded_at: Mapped[str | None] = mapped_column(String, nullable=True)
    ots_object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    ots_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ots_stored_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ots_uploaded_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            ondelete="CASCADE",
        ),
    )

    collection: Mapped[CollectionRecord] = relationship(back_populates="archive")


class PlannedCandidateRecord(Base):
    __tablename__ = "planned_candidates"

    candidate_id: Mapped[str] = mapped_column(String, primary_key=True)
    finalized_id: Mapped[str] = mapped_column(String, unique=True)
    filename: Mapped[str] = mapped_column(String)
    bytes: Mapped[int] = mapped_column(Integer)
    iso_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    image_root: Mapped[str] = mapped_column(String)
    target_bytes: Mapped[int] = mapped_column(Integer)
    min_fill_bytes: Mapped[int] = mapped_column(Integer)

    covered_paths: Mapped[list[CandidateCoveredPathRecord]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
    )


class CandidateCoveredPathRecord(Base):
    __tablename__ = "candidate_covered_paths"

    candidate_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["candidate_id"],
            ["planned_candidates.candidate_id"],
            ondelete="CASCADE",
        ),
    )

    candidate: Mapped[PlannedCandidateRecord] = relationship(back_populates="covered_paths")


class FinalizedImageRecord(Base):
    __tablename__ = "finalized_images"

    image_id: Mapped[str] = mapped_column(String, primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String)
    filename: Mapped[str] = mapped_column(String)
    bytes: Mapped[int] = mapped_column(Integer)
    image_root: Mapped[str] = mapped_column(String)
    target_bytes: Mapped[int] = mapped_column(Integer)
    required_copy_count: Mapped[int | None] = mapped_column(Integer, default=2, nullable=True)

    covered_paths: Mapped[list[FinalizedImageCoveredPathRecord]] = relationship(
        back_populates="image",
        cascade="all, delete-orphan",
    )
    coverage_parts: Mapped[list[FinalizedImageCoveragePartRecord]] = relationship(
        back_populates="image",
        cascade="all, delete-orphan",
    )
    collection_artifacts: Mapped[list[FinalizedImageCollectionArtifactRecord]] = relationship(
        back_populates="image",
        cascade="all, delete-orphan",
    )
    copies: Mapped[list[ImageCopyRecord]] = relationship(
        back_populates="image",
        cascade="all, delete-orphan",
    )


class FinalizedImageCoveredPathRecord(Base):
    __tablename__ = "finalized_image_covered_paths"

    image_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["image_id"],
            ["finalized_images.image_id"],
            ondelete="CASCADE",
        ),
    )

    image: Mapped[FinalizedImageRecord] = relationship(back_populates="covered_paths")


class FinalizedImageCoveragePartRecord(Base):
    __tablename__ = "finalized_image_coverage_parts"

    image_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    part_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    part_count: Mapped[int] = mapped_column(Integer)
    object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    sidecar_path: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["image_id"],
            ["finalized_images.image_id"],
            ondelete="CASCADE",
        ),
    )

    image: Mapped[FinalizedImageRecord] = relationship(back_populates="coverage_parts")


class FinalizedImageCollectionArtifactRecord(Base):
    __tablename__ = "finalized_image_collection_artifacts"

    image_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    manifest_path: Mapped[str] = mapped_column(String)
    proof_path: Mapped[str] = mapped_column(String)

    __table_args__ = (
        ForeignKeyConstraint(
            ["image_id"],
            ["finalized_images.image_id"],
            ondelete="CASCADE",
        ),
    )

    image: Mapped[FinalizedImageRecord] = relationship(back_populates="collection_artifacts")


class GlacierUsageSnapshotRecord(Base):
    __tablename__ = "glacier_usage_snapshots"

    captured_at: Mapped[str] = mapped_column(String, primary_key=True)
    uploaded_images: Mapped[int] = mapped_column(Integer)
    measured_storage_bytes: Mapped[int] = mapped_column(Integer)
    estimated_billable_bytes: Mapped[int] = mapped_column(Integer)
    estimated_monthly_cost_usd: Mapped[float] = mapped_column()
    pricing_label: Mapped[str] = mapped_column(String)
    glacier_storage_rate_usd_per_gib_month: Mapped[float] = mapped_column()
    standard_storage_rate_usd_per_gib_month: Mapped[float] = mapped_column()
    archived_metadata_bytes_per_object: Mapped[int] = mapped_column(Integer)
    standard_metadata_bytes_per_object: Mapped[int] = mapped_column(Integer)
    minimum_storage_duration_days: Mapped[int] = mapped_column(Integer)


class GlacierRecoverySessionRecord(Base):
    __tablename__ = "glacier_recovery_sessions"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str | None] = mapped_column(String, default="image_rebuild", nullable=True)
    state: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
    approved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    restore_requested_at: Mapped[str | None] = mapped_column(String, nullable=True)
    restore_ready_at: Mapped[str | None] = mapped_column(String, nullable=True)
    restore_next_poll_at: Mapped[str | None] = mapped_column(String, nullable=True)
    restore_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    latest_message: Mapped[str | None] = mapped_column(String, nullable=True)
    retrieval_tier: Mapped[str] = mapped_column(String)
    hold_days: Mapped[int] = mapped_column(Integer)
    estimate_json: Mapped[str] = mapped_column(String)
    warnings_json: Mapped[str] = mapped_column(String)
    reminder_count: Mapped[int] = mapped_column(Integer, default=0)
    next_reminder_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_notified_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_verification_state: Mapped[str | None] = mapped_column(
        String,
        default="pending",
        nullable=True,
    )
    extraction_state: Mapped[str | None] = mapped_column(
        String,
        default="pending",
        nullable=True,
    )
    materialization_state: Mapped[str | None] = mapped_column(
        String,
        default="pending",
        nullable=True,
    )

    images: Mapped[list[GlacierRecoverySessionImageRecord]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )
    collections: Mapped[list[GlacierRecoverySessionCollectionRecord]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )


class GlacierRecoverySessionImageRecord(Base):
    __tablename__ = "glacier_recovery_session_images"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    image_id: Mapped[str] = mapped_column(String, primary_key=True)
    image_order: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id"],
            ["glacier_recovery_sessions.session_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["image_id"],
            ["finalized_images.image_id"],
            ondelete="CASCADE",
        ),
    )

    session: Mapped[GlacierRecoverySessionRecord] = relationship(back_populates="images")


class GlacierRecoverySessionCollectionRecord(Base):
    __tablename__ = "glacier_recovery_session_collections"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_order: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id"],
            ["glacier_recovery_sessions.session_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            ondelete="CASCADE",
        ),
    )

    session: Mapped[GlacierRecoverySessionRecord] = relationship(back_populates="collections")


class ImageCopyRecord(Base):
    __tablename__ = "image_copies"

    image_id: Mapped[str] = mapped_column(String, primary_key=True)
    copy_id: Mapped[str] = mapped_column(String, primary_key=True)
    label_text: Mapped[str] = mapped_column(String)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    state: Mapped[str | None] = mapped_column(String, default="registered", nullable=True)
    verification_state: Mapped[str | None] = mapped_column(String, default="pending", nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["image_id"],
            ["finalized_images.image_id"],
            ondelete="CASCADE",
        ),
    )

    image: Mapped[FinalizedImageRecord] = relationship(back_populates="copies")


class ImageCopyEventRecord(Base):
    __tablename__ = "image_copy_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    image_id: Mapped[str] = mapped_column(String)
    copy_id: Mapped[str] = mapped_column(String)
    occurred_at: Mapped[str] = mapped_column(String)
    event: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    verification_state: Mapped[str] = mapped_column(String)
    location: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["image_id", "copy_id"],
            ["image_copies.image_id", "image_copies.copy_id"],
            ondelete="CASCADE",
        ),
    )


class ActivePinRecord(Base):
    __tablename__ = "active_pins"

    target: Mapped[str] = mapped_column(String, primary_key=True)
    fetch_id: Mapped[str] = mapped_column(String, unique=True)
    fetch_order: Mapped[int] = mapped_column(Integer, unique=True)
    fetch_state: Mapped[str] = mapped_column(String)


class FetchEntryRecord(Base):
    __tablename__ = "fetch_entries"

    fetch_id: Mapped[str] = mapped_column(String, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String, primary_key=True)
    entry_order: Mapped[int] = mapped_column(Integer)
    collection_id: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    recovery_bytes: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_bytes: Mapped[int] = mapped_column(Integer, default=0)
    upload_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    tus_url: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["fetch_id"],
            ["active_pins.fetch_id"],
            ondelete="CASCADE",
        ),
    )


class CollectionUploadRecord(Base):
    __tablename__ = "collection_uploads"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    ingest_source: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str | None] = mapped_column(String, default="uploading", nullable=True)
    archive_attempt_count: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)
    archive_next_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_last_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_failure: Mapped[str | None] = mapped_column(String, nullable=True)

    files: Mapped[list[CollectionUploadFileRecord]] = relationship(
        back_populates="upload",
        cascade="all, delete-orphan",
    )


class CollectionUploadFileRecord(Base):
    __tablename__ = "collection_upload_files"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    file_order: Mapped[int] = mapped_column(Integer)
    bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    uploaded_bytes: Mapped[int] = mapped_column(Integer, default=0)
    upload_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    tus_url: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collection_uploads.collection_id"],
            ondelete="CASCADE",
        ),
    )

    upload: Mapped[CollectionUploadRecord] = relationship(back_populates="files")
