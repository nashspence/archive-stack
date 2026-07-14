from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, ForeignKeyConstraint, Index, Integer, String
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
    operator_summary: Mapped[CollectionOperatorSummaryRecord | None] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
        uselist=False,
    )


class CollectionOperatorSummaryRecord(Base):
    __tablename__ = "collection_operator_summaries"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    files: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    hot_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    disc_redundancy_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    disc_coverage_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    has_registered_image: Mapped[int] = mapped_column(Integer, default=0)
    disc_redundancy_state: Mapped[str] = mapped_column(String, default="none")
    has_archive: Mapped[int] = mapped_column(Integer, default=0)
    archive_state: Mapped[str | None] = mapped_column(String, default="pending", nullable=True)
    archive_object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_stored_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    archive_backend: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_storage_class: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_last_uploaded_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_last_verified_at: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_failure: Mapped[str | None] = mapped_column(String, nullable=True)
    archive_format: Mapped[str | None] = mapped_column(String, nullable=True)
    compression: Mapped[str | None] = mapped_column(String, nullable=True)
    manifest_object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ots_object_path: Mapped[str | None] = mapped_column(String, nullable=True)
    ots_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
    )

    collection: Mapped[CollectionRecord] = relationship(back_populates="operator_summary")


class CollectionImageOperatorSummaryRecord(Base):
    __tablename__ = "collection_image_operator_summaries"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    image_id: Mapped[str] = mapped_column(String, primary_key=True)
    covered_paths_total: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        ForeignKeyConstraint(
            ["image_id"],
            ["finalized_images.image_id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        Index(
            "ix_collection_image_operator_summaries_image",
            "image_id",
            "collection_id",
        ),
    )


class CollectionFileRecord(Base):
    __tablename__ = "collection_files"

    collection_id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
    )
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
    discs: Mapped[list[FileDiscRecord]] = relationship(
        back_populates="file",
        cascade="all, delete-orphan",
    )


class FileDiscRecord(Base):
    __tablename__ = "file_discs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection_id: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    disc_id: Mapped[str] = mapped_column(String)
    image_id: Mapped[str] = mapped_column(String)
    location: Mapped[str] = mapped_column(String)
    disc_path: Mapped[str] = mapped_column(String)
    enc_json: Mapped[str] = mapped_column(String)
    part_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    part_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    part_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    part_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recovery_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    recovery_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_id", "path"],
            ["collection_files.collection_id", "collection_files.path"],
            ondelete="CASCADE",
        ),
    )

    file: Mapped[CollectionFileRecord] = relationship(back_populates="discs")


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


class PlannedCandidateRecord(Base):
    __tablename__ = "planned_candidates"

    candidate_id: Mapped[str] = mapped_column(String, primary_key=True)
    finalized_id: Mapped[str] = mapped_column(String, unique=True)
    plan_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str | None] = mapped_column(String, default="ready", nullable=True)
    failure: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(String, nullable=True)
    filename: Mapped[str] = mapped_column(String)
    bytes: Mapped[int] = mapped_column(BigInteger)
    iso_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    image_root: Mapped[str] = mapped_column(String)
    target_bytes: Mapped[int] = mapped_column(BigInteger)
    min_fill_bytes: Mapped[int] = mapped_column(BigInteger)
    ready_notification_sent_at: Mapped[str | None] = mapped_column(String, nullable=True)
    ready_notification_next_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)
    ready_notification_failure: Mapped[str | None] = mapped_column(String, nullable=True)
    ready_notification_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
        Index(
            "ix_candidate_covered_paths_collection_path",
            "collection_id",
            "path",
        ),
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
    bytes: Mapped[int] = mapped_column(BigInteger)
    image_root: Mapped[str] = mapped_column(String)
    target_bytes: Mapped[int] = mapped_column(BigInteger)
    required_disc_count: Mapped[int | None] = mapped_column(Integer, default=2, nullable=True)

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
    discs: Mapped[list[ImageDiscRecord]] = relationship(
        back_populates="image",
        cascade="all, delete-orphan",
    )
    operator_summary: Mapped[ImageOperatorSummaryRecord | None] = relationship(
        back_populates="image",
        cascade="all, delete-orphan",
        uselist=False,
    )


class ImageOperatorSummaryRecord(Base):
    __tablename__ = "image_operator_summaries"

    image_id: Mapped[str] = mapped_column(String, primary_key=True)
    filename: Mapped[str] = mapped_column(String)
    finalized_at: Mapped[str] = mapped_column(String)
    bytes: Mapped[int] = mapped_column(BigInteger)
    target_bytes: Mapped[int] = mapped_column(BigInteger)
    files: Mapped[int] = mapped_column(BigInteger, default=0)
    collections: Mapped[int] = mapped_column(BigInteger, default=0)
    collection_ids_text: Mapped[str] = mapped_column(String, default="")
    disc_redundancy_state: Mapped[str] = mapped_column(String, default="none")
    discs_required: Mapped[int] = mapped_column(Integer, default=2)
    discs_registered: Mapped[int] = mapped_column(BigInteger, default=0)
    discs_verified: Mapped[int] = mapped_column(BigInteger, default=0)
    discs_missing: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["image_id"],
            ["finalized_images.image_id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
    )

    image: Mapped[FinalizedImageRecord] = relationship(back_populates="operator_summary")


class FinalizedImageCoveredPathRecord(Base):
    __tablename__ = "finalized_image_covered_paths"

    image_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)

    __table_args__ = (
        Index(
            "ix_finalized_image_covered_paths_collection_path",
            "collection_id",
            "path",
        ),
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
        Index(
            "ix_finalized_image_coverage_parts_collection_path",
            "collection_id",
            "path",
        ),
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


class ArchiveUsageSnapshotRecord(Base):
    __tablename__ = "archive_usage_snapshots"

    captured_at: Mapped[str] = mapped_column(String, primary_key=True)
    uploaded_images: Mapped[int] = mapped_column(Integer)
    measured_storage_bytes: Mapped[int] = mapped_column(BigInteger)


class ArchiveRestoreRecord(Base):
    __tablename__ = "archive_restores"

    restore_id: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str | None] = mapped_column(String, default="disc_rebuild", nullable=True)
    state: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
    requested_at: Mapped[str | None] = mapped_column(String, nullable=True)
    ready_at: Mapped[str | None] = mapped_column(String, nullable=True)
    next_poll_at: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    canceled_at: Mapped[str | None] = mapped_column(String, nullable=True)
    paused_at: Mapped[str | None] = mapped_column(String, nullable=True)
    paused_from_state: Mapped[str | None] = mapped_column(String, nullable=True)
    latest_message: Mapped[str | None] = mapped_column(String, nullable=True)
    retrieval_tier: Mapped[str] = mapped_column(String)
    hold_days: Mapped[int] = mapped_column(Integer)
    warnings_json: Mapped[str] = mapped_column(String)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_failure_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_failure: Mapped[str | None] = mapped_column(String, nullable=True)
    last_failure_notification_at: Mapped[str | None] = mapped_column(String, nullable=True)
    reminder_count: Mapped[int] = mapped_column(Integer, default=0)
    next_reminder_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_notified_at: Mapped[str | None] = mapped_column(String, nullable=True)
    started_notification_sent_at: Mapped[str | None] = mapped_column(String, nullable=True)
    started_notification_next_attempt_at: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )
    started_notification_failure: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_notification_sent_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_notification_next_attempt_at: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )
    completed_notification_failure: Mapped[str | None] = mapped_column(String, nullable=True)
    canceled_notification_sent_at: Mapped[str | None] = mapped_column(String, nullable=True)
    canceled_notification_next_attempt_at: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )
    canceled_notification_failure: Mapped[str | None] = mapped_column(String, nullable=True)
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
    paths_json: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index(
            "ix_archive_restores_state_created",
            "state",
            "created_at",
            "restore_id",
        ),
        Index(
            "ix_archive_restores_type_state_created",
            "type",
            "state",
            "created_at",
            "restore_id",
        ),
    )

    images: Mapped[list[ArchiveRestoreImageRecord]] = relationship(
        back_populates="restore",
        cascade="all, delete-orphan",
    )
    collections: Mapped[list[ArchiveRestoreCollectionRecord]] = relationship(
        back_populates="restore",
        cascade="all, delete-orphan",
    )


class ArchiveRestoreImageRecord(Base):
    __tablename__ = "archive_restore_images"

    restore_id: Mapped[str] = mapped_column(String, primary_key=True)
    image_id: Mapped[str] = mapped_column(String, primary_key=True)
    image_order: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        ForeignKeyConstraint(
            ["restore_id"],
            ["archive_restores.restore_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["image_id"],
            ["finalized_images.image_id"],
            ondelete="CASCADE",
        ),
    )

    restore: Mapped[ArchiveRestoreRecord] = relationship(back_populates="images")


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


class ImageDiscRecord(Base):
    __tablename__ = "image_discs"

    image_id: Mapped[str] = mapped_column(String, primary_key=True)
    disc_id: Mapped[str] = mapped_column(String, primary_key=True)
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

    image: Mapped[FinalizedImageRecord] = relationship(back_populates="discs")
    operator_summary: Mapped[DiscOperatorSummaryRecord | None] = relationship(
        back_populates="disc",
        cascade="all, delete-orphan",
        uselist=False,
    )


class DiscOperatorSummaryRecord(Base):
    __tablename__ = "disc_operator_summaries"

    image_id: Mapped[str] = mapped_column(String, primary_key=True)
    disc_id: Mapped[str] = mapped_column(String, primary_key=True)
    label_text: Mapped[str] = mapped_column(String)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    verification_state: Mapped[str] = mapped_column(String)
    filename: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["image_id", "disc_id"],
            ["image_discs.image_id", "image_discs.disc_id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
    )

    disc: Mapped[ImageDiscRecord] = relationship(back_populates="operator_summary")


class ImageDiscEventRecord(Base):
    __tablename__ = "image_disc_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    image_id: Mapped[str] = mapped_column(String)
    disc_id: Mapped[str] = mapped_column(String)
    occurred_at: Mapped[str] = mapped_column(String)
    event: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    verification_state: Mapped[str] = mapped_column(String)
    location: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["image_id", "disc_id"],
            ["image_discs.image_id", "image_discs.disc_id"],
            ondelete="CASCADE",
        ),
    )


class FetchRecord(Base):
    __tablename__ = "fetches"

    fetch_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    fetch_order: Mapped[int] = mapped_column(Integer, unique=True)
    fetch_state: Mapped[str] = mapped_column(String)
    fetch_notification_sent_at: Mapped[str | None] = mapped_column(String, nullable=True)
    fetch_notification_next_attempt_at: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )
    fetch_notification_failure: Mapped[str | None] = mapped_column(String, nullable=True)
    fetch_notification_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selectors: Mapped[list[FetchSelectorRecord]] = relationship(
        back_populates="fetch",
        cascade="all, delete-orphan",
    )
    entries: Mapped[list[FetchEntryRecord]] = relationship(
        cascade="all, delete-orphan",
    )
    operator_summary: Mapped[FetchOperatorSummaryRecord | None] = relationship(
        back_populates="fetch",
        cascade="all, delete-orphan",
        uselist=False,
    )
    operator_files: Mapped[list[FetchOperatorFileRecord]] = relationship(
        back_populates="fetch",
        cascade="all, delete-orphan",
    )


class FetchSelectorRecord(Base):
    __tablename__ = "fetch_selectors"

    fetch_id: Mapped[str] = mapped_column(String, primary_key=True)
    target: Mapped[str] = mapped_column(String, primary_key=True)
    selector_order: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        ForeignKeyConstraint(
            ["fetch_id"],
            ["fetches.fetch_id"],
            ondelete="CASCADE",
        ),
        Index("idx_fetch_selectors_order", "fetch_id", "selector_order"),
    )

    fetch: Mapped[FetchRecord] = relationship(back_populates="selectors")


class FetchOperatorSummaryRecord(Base):
    __tablename__ = "fetch_operator_summaries"

    fetch_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    fetch_order: Mapped[int] = mapped_column(Integer)
    fetch_state: Mapped[str] = mapped_column(String)
    selectors: Mapped[int] = mapped_column(BigInteger, default=0)
    targets_text: Mapped[str] = mapped_column(String, default="")
    files: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    hot_files: Mapped[int] = mapped_column(BigInteger, default=0)
    hot_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    missing_files: Mapped[int] = mapped_column(BigInteger, default=0)
    missing_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    entries_total: Mapped[int] = mapped_column(BigInteger, default=0)
    entries_pending: Mapped[int] = mapped_column(BigInteger, default=0)
    entries_partial: Mapped[int] = mapped_column(BigInteger, default=0)
    entries_byte_complete: Mapped[int] = mapped_column(BigInteger, default=0)
    entries_uploaded: Mapped[int] = mapped_column(BigInteger, default=0)
    entry_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    entry_recovery_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    uploaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    upload_missing_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    upload_state_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["fetch_id"],
            ["fetches.fetch_id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
    )

    fetch: Mapped[FetchRecord] = relationship(back_populates="operator_summary")


class FetchOperatorFileRecord(Base):
    __tablename__ = "fetch_operator_files"

    fetch_id: Mapped[str] = mapped_column(String, primary_key=True)
    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    hot: Mapped[bool] = mapped_column(Boolean, default=False)
    disc_coverage: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["fetch_id"],
            ["fetches.fetch_id"],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        ForeignKeyConstraint(
            ["collection_id", "path"],
            ["collection_files.collection_id", "collection_files.path"],
            ondelete="CASCADE",
        ),
        Index(
            "ix_fetch_operator_files_path",
            "fetch_id",
            "path",
            "collection_id",
        ),
        Index(
            "ix_fetch_operator_files_bytes",
            "fetch_id",
            "bytes",
            "collection_id",
            "path",
        ),
        Index(
            "ix_fetch_operator_files_hot",
            "fetch_id",
            "hot",
            "collection_id",
            "path",
        ),
        Index(
            "ix_fetch_operator_files_disc",
            "fetch_id",
            "disc_coverage",
            "collection_id",
            "path",
        ),
    )

    fetch: Mapped[FetchRecord] = relationship(back_populates="operator_files")


class FetchEntryRecord(Base):
    __tablename__ = "fetch_entries"

    fetch_id: Mapped[str] = mapped_column(String, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String, primary_key=True)
    entry_order: Mapped[int] = mapped_column(Integer)
    collection_id: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    recovery_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    uploaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    upload_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    tus_url: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["fetch_id"],
            ["fetches.fetch_id"],
            ondelete="CASCADE",
        ),
    )


class CollectionUploadRecord(Base):
    __tablename__ = "collection_uploads"

    collection_id: Mapped[str] = mapped_column(String, primary_key=True)
    ingest_source: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str | None] = mapped_column(String, default="uploading", nullable=True)
    notify_json: Mapped[str | None] = mapped_column(String, nullable=True)
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
