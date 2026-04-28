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
    glacier_state: Mapped[str | None] = mapped_column(String, default="pending", nullable=True)
    glacier_object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    glacier_stored_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    glacier_backend: Mapped[str | None] = mapped_column(String, nullable=True)
    glacier_storage_class: Mapped[str | None] = mapped_column(String, nullable=True)
    glacier_last_uploaded_at: Mapped[str | None] = mapped_column(String, nullable=True)
    glacier_last_verified_at: Mapped[str | None] = mapped_column(String, nullable=True)
    glacier_failure: Mapped[str | None] = mapped_column(String, nullable=True)

    covered_paths: Mapped[list[FinalizedImageCoveredPathRecord]] = relationship(
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


class GlacierUploadJobRecord(Base):
    __tablename__ = "glacier_upload_jobs"

    image_id: Mapped[str] = mapped_column(String, primary_key=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    failed_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["image_id"],
            ["finalized_images.image_id"],
            ondelete="CASCADE",
        ),
    )


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
