from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uuid_str() -> str:
    return str(uuid4())


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    keep_buffer_after_archive: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    directories: Mapped[list["JobDirectory"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    files: Mapped[list["JobFile"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobDirectory(Base):
    __tablename__ = "job_directories"
    __table_args__ = (UniqueConstraint("job_id", "relative_path", name="uq_job_directories_job_path"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    job: Mapped[Job] = relationship(back_populates="directories")


class JobFile(Base):
    __tablename__ = "job_files"
    __table_args__ = (
        UniqueConstraint("job_id", "relative_path", name="uq_job_files_job_path"),
        Index("ix_job_files_job_id_relative_path", "job_id", "relative_path"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actual_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default="0644", nullable=False)
    mtime: Mapped[str] = mapped_column(String(64), nullable=False)
    uid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    buffer_abs_path: Mapped[str | None] = mapped_column(String, nullable=True)
    materialized_abs_path: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending_upload", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    job: Mapped[Job] = relationship(back_populates="files")
    archive_pieces: Mapped[list["ArchivePiece"]] = relationship(back_populates="job_file", cascade="all, delete-orphan")
    uploads: Mapped[list["UploadSlot"]] = relationship(back_populates="job_file")


class Disc(Base):
    __tablename__ = "discs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="offline", nullable=False)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    root_abs_path: Mapped[str] = mapped_column(String, nullable=False)
    contents_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    total_root_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cached_root_abs_path: Mapped[str | None] = mapped_column(String, nullable=True)
    iso_abs_path: Mapped[str | None] = mapped_column(String, nullable=True)
    iso_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    burn_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    entries: Mapped[list["DiscEntry"]] = relationship(back_populates="disc", cascade="all, delete-orphan")
    archive_pieces: Mapped[list["ArchivePiece"]] = relationship(back_populates="disc", cascade="all, delete-orphan")
    cache_sessions: Mapped[list["CacheSession"]] = relationship(back_populates="disc", cascade="all, delete-orphan")
    finalization_notifications: Mapped[list["DiscFinalizationNotification"]] = relationship(
        back_populates="disc",
        cascade="all, delete-orphan",
    )


class DiscEntry(Base):
    __tablename__ = "disc_entries"
    __table_args__ = (UniqueConstraint("disc_id", "relative_path", name="uq_disc_entries_disc_path"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    disc_id: Mapped[str] = mapped_column(ForeignKey("discs.id", ondelete="CASCADE"), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    stored_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    stored_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    disc: Mapped[Disc] = relationship(back_populates="entries")


class ArchivePiece(Base):
    __tablename__ = "archive_pieces"
    __table_args__ = (
        UniqueConstraint("disc_id", "payload_relpath", name="uq_archive_pieces_disc_payload"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    disc_id: Mapped[str] = mapped_column(ForeignKey("discs.id", ondelete="CASCADE"), nullable=False, index=True)
    job_file_id: Mapped[str] = mapped_column(ForeignKey("job_files.id", ondelete="CASCADE"), nullable=False, index=True)
    payload_relpath: Mapped[str] = mapped_column(String, nullable=False)
    sidecar_relpath: Mapped[str] = mapped_column(String, nullable=False)
    payload_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    disc: Mapped[Disc] = relationship(back_populates="archive_pieces")
    job_file: Mapped[JobFile] = relationship(back_populates="archive_pieces")


class CacheSession(Base):
    __tablename__ = "cache_sessions"
    __table_args__ = (Index("ix_cache_sessions_disc_id", "disc_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    disc_id: Mapped[str] = mapped_column(ForeignKey("discs.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    expected_total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uploaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    disc: Mapped[Disc] = relationship(back_populates="cache_sessions")
    uploads: Mapped[list["UploadSlot"]] = relationship(back_populates="cache_session")


class UploadSlot(Base):
    __tablename__ = "upload_slots"
    __table_args__ = (
        UniqueConstraint("upload_id", name="uq_upload_slots_upload_id"),
        UniqueConstraint("upload_token", name="uq_upload_slots_upload_token"),
        Index("ix_upload_slots_job_file_id", "job_file_id"),
        Index("ix_upload_slots_cache_session_id", "cache_session_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    upload_id: Mapped[str] = mapped_column(String(64), nullable=False)
    upload_token: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    relative_path: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actual_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_offset: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    final_abs_path: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_file_id: Mapped[str | None] = mapped_column(ForeignKey("job_files.id", ondelete="CASCADE"), nullable=True)
    cache_session_id: Mapped[str | None] = mapped_column(ForeignKey("cache_sessions.id", ondelete="CASCADE"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    job_file: Mapped[JobFile | None] = relationship(back_populates="uploads")
    cache_session: Mapped[CacheSession | None] = relationship(back_populates="uploads")


class DownloadSession(Base):
    __tablename__ = "download_sessions"
    __table_args__ = (Index("ix_download_sessions_disc_id", "disc_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    disc_id: Mapped[str] = mapped_column(ForeignKey("discs.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ready", nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class DiscFinalizationWebhookSubscription(Base):
    __tablename__ = "disc_finalization_webhook_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    webhook_url: Mapped[str] = mapped_column(String, nullable=False)
    reminder_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    notifications: Mapped[list["DiscFinalizationNotification"]] = relationship(
        back_populates="subscription",
        cascade="all, delete-orphan",
    )


class DiscFinalizationNotification(Base):
    __tablename__ = "disc_finalization_notifications"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "disc_id",
            name="uq_disc_finalization_notifications_subscription_disc",
        ),
        Index("ix_disc_finalization_notifications_next_attempt_at", "next_attempt_at"),
        Index("ix_disc_finalization_notifications_disc_id", "disc_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("disc_finalization_webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    disc_id: Mapped[str] = mapped_column(ForeignKey("discs.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    reminder_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    initial_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    subscription: Mapped[DiscFinalizationWebhookSubscription] = relationship(back_populates="notifications")
    disc: Mapped[Disc] = relationship(back_populates="finalization_notifications")
