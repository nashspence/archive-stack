"""Establish truthful Jeb v1 custody and durable ingress publication state."""

from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath

from alembic import op
from sqlalchemy import Column, String, Text, text

revision: str = "v1_0002"
down_revision: str | None = "v1_0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    connection = op.get_bind()
    old_files = list(
        connection.execute(
            text(
                "SELECT batch_id, input_path, target_path, bytes, mtime_ns, sha256 "
                "FROM files ORDER BY batch_id, target_path"
            )
        ).mappings()
    )
    old_attempt_files = list(
        connection.execute(
            text(
                "SELECT af.attempt_id, a.batch_id, af.target_path, af.staging_path, af.staged_at "
                "FROM attempt_files af JOIN batch_attempts a ON a.id = af.attempt_id "
                "ORDER BY af.attempt_id, af.target_path"
            )
        ).mappings()
    )
    batch_dir = Path(os.environ.get("JEB_BATCH_DIR", "/landing/.jeb-batches"))
    prior_paths: dict[tuple[str, str], list[Path]] = {}
    prior_claimed_at: dict[tuple[str, str], str] = {}
    for row in old_attempt_files:
        batch_id = str(row["batch_id"])
        key = (batch_id, str(row["target_path"]))
        prior_paths.setdefault(key, []).append(Path(str(row["staging_path"])))
        if row["staged_at"] is not None:
            prior_claimed_at[key] = max(
                prior_claimed_at.get(key, ""),
                str(row["staged_at"]),
            )

    migrated_files: list[dict[str, object]] = []
    migrated_custody: dict[tuple[str, str], tuple[Path, str | None]] = {}
    for row in old_files:
        batch_id = str(row["batch_id"])
        target_path = str(row["target_path"])
        existing_paths = prior_paths.get((batch_id, target_path), [])
        batch_root = next(
            (
                parent
                for prior in reversed(existing_paths)
                for parent in prior.parents
                if parent.name == batch_id
            ),
            batch_dir / batch_id,
        )
        custody_path = batch_root / "custody" / PurePosixPath(target_path)
        for prior in reversed(existing_paths):
            if custody_path.exists() or not prior.is_file():
                continue
            custody_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(prior, custody_path)
            break
        source_path = Path(str(row["input_path"]))
        if custody_path.is_file():
            original_stat = os.lstat(custody_path)
            for source_companion, custody_companion in (
                (
                    source_path.with_name(source_path.name + ".riverhog-provenance.json-seq"),
                    custody_path.with_name(custody_path.name + ".riverhog-provenance.json-seq"),
                ),
                (
                    source_path.with_name(f".{source_path.name}.riverhog-provenance"),
                    custody_path.with_name(f".{custody_path.name}.riverhog-provenance"),
                ),
            ):
                if source_companion.exists() and not custody_companion.exists():
                    custody_companion.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source_companion, custody_companion)
            try:
                same_inode = os.path.samefile(source_path, custody_path)
            except FileNotFoundError:
                same_inode = False
            _snapshot_migrated_custody(
                custody_path,
                source_path=source_path if same_inode else None,
            )
            source_device = original_stat.st_dev
            source_inode = original_stat.st_ino
        elif source_path.is_file():
            source_stat = os.lstat(source_path)
            source_device = source_stat.st_dev
            source_inode = source_stat.st_ino
        else:
            source_device = 0
            source_inode = 0
        try:
            custody_stat = os.lstat(custody_path)
            custody_device: int | None = custody_stat.st_dev
            custody_inode: int | None = custody_stat.st_ino
            claimed_at: str | None = prior_claimed_at.get((batch_id, target_path))
        except OSError:
            custody_device = None
            custody_inode = None
            claimed_at = None
        migrated_custody[(batch_id, target_path)] = (custody_path, claimed_at)
        migrated_files.append(
            {
                "batch_id": batch_id,
                "source_path": str(source_path),
                "custody_path": str(custody_path),
                "target_path": target_path,
                "bytes": int(row["bytes"]),
                "mtime_ns": int(row["mtime_ns"]),
                "source_device": source_device,
                "source_inode": source_inode,
                "custody_device": custody_device,
                "custody_inode": custody_inode,
                "sha256": row["sha256"],
                "claimed_at": claimed_at,
            }
        )

    for trigger in (
        "trg_jeb_files_summary_insert",
        "trg_jeb_files_summary_delete",
        "trg_jeb_files_summary_update_same_batch",
        "trg_jeb_files_summary_update_moved_batch",
    ):
        op.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
    op.drop_index("idx_jeb_attempt_files_attempt", table_name="attempt_files")
    op.drop_index("idx_jeb_files_batch", table_name="files")
    op.rename_table("attempt_files", "attempt_files_v1_0001")
    op.rename_table("files", "files_v1_0001")

    op.execute(
        """
        CREATE TABLE files (
            batch_id TEXT NOT NULL,
            source_path TEXT NOT NULL,
            custody_path TEXT NOT NULL,
            target_path TEXT NOT NULL,
            bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            source_device INTEGER NOT NULL,
            source_inode INTEGER NOT NULL,
            custody_device INTEGER,
            custody_inode INTEGER,
            sha256 TEXT,
            claimed_at TEXT,
            PRIMARY KEY (batch_id, target_path),
            FOREIGN KEY(batch_id) REFERENCES batches(id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE attempt_files (
            attempt_id TEXT NOT NULL,
            target_path TEXT NOT NULL,
            ready_at TEXT,
            PRIMARY KEY (attempt_id, target_path),
            FOREIGN KEY(attempt_id) REFERENCES batch_attempts(id)
        )
        """
    )
    if migrated_files:
        connection.execute(
            text(
                "INSERT INTO files("
                "batch_id, source_path, custody_path, target_path, bytes, mtime_ns, "
                "source_device, source_inode, custody_device, custody_inode, sha256, claimed_at"
                ") VALUES("
                ":batch_id, :source_path, :custody_path, :target_path, :bytes, :mtime_ns, "
                ":source_device, :source_inode, :custody_device, :custody_inode, "
                ":sha256, :claimed_at)"
            ),
            migrated_files,
        )
    for row in old_attempt_files:
        _custody_path, claimed_at = migrated_custody[
            (str(row["batch_id"]), str(row["target_path"]))
        ]
        connection.execute(
            text(
                "INSERT INTO attempt_files(attempt_id, target_path, ready_at) "
                "VALUES(:attempt_id, :target_path, :ready_at)"
            ),
            {
                "attempt_id": str(row["attempt_id"]),
                "target_path": str(row["target_path"]),
                "ready_at": claimed_at,
            },
        )
    op.drop_table("attempt_files_v1_0001")
    op.drop_table("files_v1_0001")
    op.create_index("idx_jeb_files_batch", "files", ["batch_id"])
    op.create_index("idx_jeb_attempt_files_attempt", "attempt_files", ["attempt_id"])
    _create_file_summary_triggers()

    op.add_column(
        "source_removals",
        Column("phase", String(), nullable=False, server_default="quiesce"),
    )
    op.add_column("source_removals", Column("last_error", Text(), nullable=True))
    op.execute(
        """
        CREATE TABLE ingress_publications (
            upload_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            declared_bytes INTEGER NOT NULL,
            payload_sha256 TEXT NOT NULL,
            provenance_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            error_code TEXT,
            error_message TEXT,
            destination_path TEXT,
            provenance_identity TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    op.create_index(
        "idx_jeb_ingress_publications_source_status",
        "ingress_publications",
        ["source_id", "status", "created_at"],
    )
    op.create_index(
        "idx_jeb_ingress_publications_status_updated",
        "ingress_publications",
        ["status", "updated_at"],
    )


def _create_file_summary_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_jeb_files_summary_insert AFTER INSERT ON files
        BEGIN
            UPDATE batches SET file_count = file_count + 1,
                total_bytes = total_bytes + NEW.bytes WHERE id = NEW.batch_id;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_jeb_files_summary_delete AFTER DELETE ON files
        BEGIN
            UPDATE batches SET file_count = file_count - 1,
                total_bytes = total_bytes - OLD.bytes WHERE id = OLD.batch_id;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_jeb_files_summary_update_same_batch
        AFTER UPDATE OF batch_id, bytes ON files WHEN OLD.batch_id = NEW.batch_id
        BEGIN
            UPDATE batches SET total_bytes = total_bytes - OLD.bytes + NEW.bytes
            WHERE id = NEW.batch_id;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_jeb_files_summary_update_moved_batch
        AFTER UPDATE OF batch_id, bytes ON files WHEN OLD.batch_id != NEW.batch_id
        BEGIN
            UPDATE batches SET file_count = file_count - 1,
                total_bytes = total_bytes - OLD.bytes WHERE id = OLD.batch_id;
            UPDATE batches SET file_count = file_count + 1,
                total_bytes = total_bytes + NEW.bytes WHERE id = NEW.batch_id;
        END
        """
    )


def _snapshot_migrated_custody(custody: Path, *, source_path: Path | None) -> None:
    original = os.lstat(custody)
    temporary = custody.with_name(f".{custody.name}.v1-custody-migration")
    temporary.unlink(missing_ok=True)
    try:
        source_fd = os.open(custody, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with (
            os.fdopen(source_fd, "rb", closefd=True) as input_stream,
            temporary.open("xb") as output_stream,
        ):
            initial = os.fstat(input_stream.fileno())
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
            final = os.fstat(input_stream.fileno())
        if (
            initial.st_dev != original.st_dev
            or initial.st_ino != original.st_ino
            or final.st_size != initial.st_size
            or final.st_mtime_ns != initial.st_mtime_ns
        ):
            raise RuntimeError("Jeb custody source changed during v1 state migration")
        os.utime(temporary, ns=(initial.st_atime_ns, initial.st_mtime_ns))
        if source_path is not None:
            source_path.unlink()
        os.replace(temporary, custody)
    finally:
        temporary.unlink(missing_ok=True)


def downgrade() -> None:
    raise RuntimeError("Jeb state migrations are forward-only")
