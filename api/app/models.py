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


class Collection(Base):
    __tablename__ = "collections"

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

    directories: Mapped[list["CollectionDirectory"]] = relationship(back_populates="collection", cascade="all, delete-orphan")
    files: Mapped[list["CollectionFile"]] = relationship(back_populates="collection", cascade="all, delete-orphan")


class CollectionDirectory(Base):
    __tablename__ = "collection_directories"
    __table_args__ = (UniqueConstraint("collection_id", "relative_path", name="uq_collection_directories_collection_path"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    collection_id: Mapped[str] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    collection: Mapped[Collection] = relationship(back_populates="directories")


class CollectionFile(Base):
    __tablename__ = "collection_files"
    __table_args__ = (
        UniqueConstraint("collection_id", "relative_path", name="uq_collection_files_collection_path"),
        Index("ix_collection_files_collection_id_relative_path", "collection_id", "relative_path"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    collection_id: Mapped[str] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"), nullable=False, index=True)
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
    status: Mapped[str] = mapped_column(String(32), default="inactive", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    collection: Mapped[Collection] = relationship(back_populates="files")
    archive_pieces: Mapped[list["ArchivePiece"]] = relationship(back_populates="collection_file", cascade="all, delete-orphan")
class Container(Base):
    __tablename__ = "containers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="inactive", nullable=False)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    root_abs_path: Mapped[str] = mapped_column(String, nullable=False)
    contents_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    total_root_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    active_root_abs_path: Mapped[str | None] = mapped_column(String, nullable=True)
    iso_abs_path: Mapped[str | None] = mapped_column(String, nullable=True)
    iso_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    burn_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finalization_status: Mapped[str] = mapped_column(String(32), default="disabled", nullable=False)
    finalization_reminder_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    finalization_initial_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalization_last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalization_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalization_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalization_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    entries: Mapped[list["ContainerEntry"]] = relationship(back_populates="container", cascade="all, delete-orphan")
    archive_pieces: Mapped[list["ArchivePiece"]] = relationship(back_populates="container", cascade="all, delete-orphan")
    activation_sessions: Mapped[list["ActivationSession"]] = relationship(back_populates="container", cascade="all, delete-orphan")


class ContainerEntry(Base):
    __tablename__ = "container_entries"
    __table_args__ = (UniqueConstraint("container_id", "relative_path", name="uq_container_entries_container_path"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    container_id: Mapped[str] = mapped_column(ForeignKey("containers.id", ondelete="CASCADE"), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    stored_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    stored_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    container: Mapped[Container] = relationship(back_populates="entries")


class ArchivePiece(Base):
    __tablename__ = "archive_pieces"
    __table_args__ = (
        UniqueConstraint("container_id", "payload_relpath", name="uq_archive_pieces_container_payload"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    container_id: Mapped[str] = mapped_column(ForeignKey("containers.id", ondelete="CASCADE"), nullable=False, index=True)
    collection_file_id: Mapped[str] = mapped_column(ForeignKey("collection_files.id", ondelete="CASCADE"), nullable=False, index=True)
    payload_relpath: Mapped[str] = mapped_column(String, nullable=False)
    sidecar_relpath: Mapped[str] = mapped_column(String, nullable=False)
    payload_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    container: Mapped[Container] = relationship(back_populates="archive_pieces")
    collection_file: Mapped[CollectionFile] = relationship(back_populates="archive_pieces")


class ActivationSession(Base):
    __tablename__ = "activation_sessions"
    __table_args__ = (Index("ix_activation_sessions_container_id", "container_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    container_id: Mapped[str] = mapped_column(ForeignKey("containers.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    expected_total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uploaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    container: Mapped[Container] = relationship(back_populates="activation_sessions")
