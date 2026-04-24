from __future__ import annotations

from sqlalchemy import Boolean, ForeignKeyConstraint, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from arc_core.sqlite_db import Base


class CollectionRecord(Base):
    __tablename__ = "collections"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_staging_path: Mapped[str] = mapped_column(String, unique=True)
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
    content: Mapped[bytes] = mapped_column(LargeBinary)
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


class ImageCopyRecord(Base):
    __tablename__ = "image_copies"

    image_id: Mapped[str] = mapped_column(String, primary_key=True)
    copy_id: Mapped[str] = mapped_column(String, primary_key=True)
    location: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)

    __table_args__ = (
        ForeignKeyConstraint(
            ["image_id"],
            ["finalized_images.image_id"],
            ondelete="CASCADE",
        ),
    )

    image: Mapped[FinalizedImageRecord] = relationship(back_populates="copies")


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
    content: Mapped[bytes] = mapped_column(LargeBinary)
    uploaded_bytes: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    upload_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["fetch_id"],
            ["active_pins.fetch_id"],
            ondelete="CASCADE",
        ),
    )
