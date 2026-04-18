from __future__ import annotations

import hashlib
import os
import shutil
import shlex
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from tempfile import mkdtemp

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .config import COLD_ISO_ROOT, COLD_JOB_ROOT, EXPORT_JOBS_ROOT, HOT_BUFFER_ROOT, HOT_CACHE_ROOT, HOT_CACHE_STAGING_ROOT, HOT_MATERIALIZED_ROOT, OTS_CLIENT_COMMAND, PARTITION_ROOTS_DIR
from .models import ArchivePiece, CacheSession, Disc, DiscEntry, Job, JobDirectory, JobFile, UploadSlot

JOB_HASH_MANIFEST_NAME = "HASHES.yml"
JOB_HASH_PROOF_NAME = f"{JOB_HASH_MANIFEST_NAME}.ots"
JOB_HASH_BUNDLE_NAME = "hash-manifest-proof.zip"
JOB_HASH_MANIFEST_SCHEMA = "job-hash-manifest/v1"


def normalize_relpath(raw: str) -> str:
    candidate = raw.strip().replace("\\", "/")
    if not candidate or candidate in {".", "/"}:
        raise ValueError("path must not be empty")
    p = PurePosixPath(candidate)
    if p.is_absolute():
        raise ValueError("path must be relative")
    parts = []
    for part in p.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("path must not escape its root")
        parts.append(part)
    if not parts:
        raise ValueError("path must not be empty")
    return "/".join(parts)


def normalize_root_node_name(raw: str) -> str:
    candidate = raw.strip()
    if not candidate:
        raise ValueError("root node name must not be empty")
    normalized = normalize_relpath(candidate)
    if "/" in normalized:
        raise ValueError("root node name must be a single path segment")
    if normalized in {".", ".."}:
        raise ValueError("root node name must not be . or ..")
    return normalized


def path_parents(relpath: str) -> list[str]:
    parts = normalize_relpath(relpath).split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def atomic_replace_file_link(link_path: Path, target: Path) -> None:
    ensure_parent_dir(link_path)
    temp = link_path.with_name(f".{link_path.name}.tmp")
    if temp.exists() or temp.is_symlink():
        temp.unlink()
    os.link(target, temp)
    temp.replace(link_path)


def atomic_replace_file(path: Path, data: bytes) -> None:
    ensure_parent_dir(path)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_bytes(data)
    temp.replace(path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_tree_hash(root: Path) -> tuple[str, int, list[dict[str, object]]]:
    digest = hashlib.sha256()
    total = 0
    rows: list[dict[str, object]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        size = path.stat().st_size
        sha = file_sha256(path)
        total += size
        rows.append({"relative_path": rel, "size_bytes": size, "sha256": sha})
        digest.update(f"{rel}\t{size}\t{sha}\n".encode())
    return digest.hexdigest(), total, rows


def safe_remove_tree(path: Path) -> None:
    if path.exists() or path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)


def safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def job_buffer_path(job_id: str, relative_path: str) -> Path:
    return HOT_BUFFER_ROOT / job_id / normalize_relpath(relative_path)


def cache_staging_root(session_id: str) -> Path:
    return HOT_CACHE_STAGING_ROOT / session_id


def cache_staging_file_path(session_id: str, relative_path: str) -> Path:
    return cache_staging_root(session_id) / normalize_relpath(relative_path)


def active_cache_root(disc_id: str) -> Path:
    return HOT_CACHE_ROOT / disc_id


def active_cache_file_path(disc_id: str, relative_path: str) -> Path:
    return active_cache_root(disc_id) / normalize_relpath(relative_path)


def materialized_job_root(job_id: str) -> Path:
    return HOT_MATERIALIZED_ROOT / job_id


def materialized_job_file_path(job_id: str, relative_path: str) -> Path:
    return materialized_job_root(job_id) / normalize_relpath(relative_path)


def export_job_root(job_id: str) -> Path:
    return EXPORT_JOBS_ROOT / job_id


def partition_root(disc_id: str) -> Path:
    return PARTITION_ROOTS_DIR / disc_id


def registered_iso_storage_path(disc_id: str) -> Path:
    return COLD_ISO_ROOT / f"{disc_id}.iso"


def cold_job_artifact_root(job_id: str) -> Path:
    return COLD_JOB_ROOT / normalize_root_node_name(job_id)


def cold_job_hash_manifest_path(job_id: str) -> Path:
    return cold_job_artifact_root(job_id) / JOB_HASH_MANIFEST_NAME


def cold_job_hash_proof_path(job_id: str) -> Path:
    return cold_job_artifact_root(job_id) / JOB_HASH_PROOF_NAME


def cold_job_hash_bundle_path(job_id: str) -> Path:
    return cold_job_artifact_root(job_id) / JOB_HASH_BUNDLE_NAME


def iso_volume_label(name: str) -> str:
    allowed = []
    for char in name.upper():
        allowed.append(char if char.isalnum() else "_")
    label = "".join(allowed).strip("_") or "ARCHIVE"
    return label[:32]


def job_disc_artifact_relpaths(job_id: str) -> tuple[str, str]:
    name = normalize_root_node_name(job_id)
    return f"jobs/{name}/{JOB_HASH_MANIFEST_NAME}", f"jobs/{name}/{JOB_HASH_PROOF_NAME}"


def aggregate_job_progress(session: Session, job_id: str) -> tuple[int, int]:
    total_size = session.scalar(select(func.coalesce(func.sum(JobFile.size_bytes), 0)).where(JobFile.job_id == job_id)) or 0
    current = session.scalar(
        select(func.coalesce(func.sum(UploadSlot.current_offset), 0)).join(JobFile, UploadSlot.job_file_id == JobFile.id).where(JobFile.job_id == job_id)
    ) or 0
    return int(current), int(total_size)


def aggregate_cache_progress(session: Session, cache_session_id: str) -> tuple[int, int]:
    total = session.scalar(select(CacheSession.expected_total_bytes).where(CacheSession.id == cache_session_id)) or 0
    current = session.scalar(select(func.coalesce(func.sum(UploadSlot.current_offset), 0)).where(UploadSlot.cache_session_id == cache_session_id)) or 0
    return int(current), int(total)


def job_hash_manifest_payload(job: Job) -> bytes:
    files = [
        {
            "path": job_file.relative_path,
            "size_bytes": job_file.size_bytes,
            "sha256": job_file.actual_sha256,
        }
        for job_file in sorted(job.files, key=lambda item: item.relative_path)
        if job_file.actual_sha256
    ]
    if not files:
        raise RuntimeError(f"job {job.id} has no uploaded files to hash")
    return yaml.safe_dump(
        {
            "schema": JOB_HASH_MANIFEST_SCHEMA,
            "job_id": job.id,
            "files": files,
        },
        sort_keys=False,
        allow_unicode=True,
    ).encode("utf-8")


def _run_ots_stamp(manifest_path: Path) -> Path:
    command = shlex.split(OTS_CLIENT_COMMAND)
    if not command:
        raise RuntimeError("OTS_CLIENT_COMMAND must not be empty")
    result = subprocess.run(
        [*command, "stamp", str(manifest_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip() or "OpenTimestamps stamp failed"
        raise RuntimeError(message)
    proof_path = manifest_path.with_name(f"{manifest_path.name}.ots")
    if not proof_path.exists():
        raise RuntimeError("OpenTimestamps stamp did not produce a proof file")
    return proof_path


def refresh_job_hash_artifacts(session: Session, job_id: str) -> None:
    job = session.execute(select(Job).where(Job.id == job_id).options(selectinload(Job.files))).scalar_one()
    payload = job_hash_manifest_payload(job)
    artifact_root = cold_job_artifact_root(job_id)
    artifact_root.mkdir(parents=True, exist_ok=True)
    temp_root = Path(mkdtemp(prefix=".job-hashes-", dir=str(artifact_root)))
    try:
        manifest_path = temp_root / JOB_HASH_MANIFEST_NAME
        manifest_path.write_bytes(payload)
        proof_path = _run_ots_stamp(manifest_path)
        bundle_path = temp_root / JOB_HASH_BUNDLE_NAME
        with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(manifest_path, arcname=JOB_HASH_MANIFEST_NAME)
            bundle.write(proof_path, arcname=JOB_HASH_PROOF_NAME)

        manifest_path.replace(cold_job_hash_manifest_path(job_id))
        proof_path.replace(cold_job_hash_proof_path(job_id))
        bundle_path.replace(cold_job_hash_bundle_path(job_id))
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _piece_online_path(piece: ArchivePiece) -> Path | None:
    disc = piece.disc
    if not disc.cached_root_abs_path:
        return None
    path = Path(disc.cached_root_abs_path) / piece.payload_relpath
    return path if path.exists() else None


def recompute_job_file_runtime(job_file: JobFile) -> tuple[Path | None, str | None, list[str]]:
    if job_file.materialized_abs_path:
        old = Path(job_file.materialized_abs_path)
        if old.exists():
            old.unlink(missing_ok=True)
    job_file.materialized_abs_path = None

    if job_file.buffer_abs_path:
        path = Path(job_file.buffer_abs_path)
        if path.exists():
            job_file.status = "online"
            job_file.error_message = None
            return path, "buffer", []

    pieces = sorted(job_file.archive_pieces, key=lambda p: (p.chunk_index or 0, p.disc_id))
    if not pieces:
        if job_file.status not in {"pending_upload", "uploading", "failed"}:
            job_file.status = "offline"
        return None, None, []

    unsplit_paths = []
    for piece in pieces:
        path = _piece_online_path(piece)
        if path is not None and piece.chunk_count is None:
            unsplit_paths.append((path, piece.disc_id))
    if unsplit_paths:
        job_file.status = "online"
        job_file.error_message = None
        return unsplit_paths[0][0], "cache", []

    count = max((p.chunk_count or 0) for p in pieces)
    available: dict[int, Path] = {}
    missing_discs: set[str] = set()
    for piece in pieces:
        if piece.chunk_count is None or piece.chunk_index is None:
            continue
        path = _piece_online_path(piece)
        if path is not None and piece.chunk_index not in available:
            available[piece.chunk_index] = path
        elif path is None:
            missing_discs.add(piece.disc_id)

    if count >= 2 and all(index in available for index in range(1, count + 1)):
        out = materialized_job_file_path(job_file.job_id, job_file.relative_path)
        ensure_parent_dir(out)
        temp = out.with_name(f".{out.name}.tmp")
        with temp.open("wb") as handle:
            for index in range(1, count + 1):
                with available[index].open("rb") as src:
                    shutil.copyfileobj(src, handle, length=1024 * 1024)
        temp.replace(out)
        job_file.materialized_abs_path = str(out)
        job_file.status = "online"
        job_file.error_message = None
        return out, "materialized", []

    discs = sorted({p.disc_id for p in pieces})
    job_file.status = "offline"
    if count >= 2:
        job_file.error_message = f"This split file is not online right now. Required cached partitions are missing. Candidate partitions: {', '.join(discs)}."
    else:
        job_file.error_message = f"This file is not online right now. It is stored on partition {discs[0]}."
    return None, None, discs


def rebuild_job_export(session: Session, job_id: str) -> None:
    job = (
        session.execute(
            select(Job)
            .where(Job.id == job_id)
            .options(selectinload(Job.directories), selectinload(Job.files).selectinload(JobFile.archive_pieces).selectinload(ArchivePiece.disc))
        )
        .scalar_one()
    )
    root = export_job_root(job_id)
    safe_remove_tree(root)
    root.mkdir(parents=True, exist_ok=True)
    safe_remove_tree(materialized_job_root(job_id))

    explicit_dirs = {d.relative_path for d in job.directories}
    derived_dirs = set()
    for jf in job.files:
        for parent in path_parents(jf.relative_path):
            derived_dirs.add(parent)
    for rel in sorted(explicit_dirs | derived_dirs):
        (root / rel).mkdir(parents=True, exist_ok=True)

    for jf in job.files:
        online_path, _source, _disc_ids = recompute_job_file_runtime(jf)
        if online_path is None:
            continue
        atomic_replace_file_link(root / normalize_relpath(jf.relative_path), online_path)
    session.commit()


def release_job_buffer_files(session: Session, job_id: str) -> bool:
    job = (
        session.execute(
            select(Job)
            .where(Job.id == job_id)
            .options(selectinload(Job.files))
        )
        .scalar_one_or_none()
    )
    if job is None:
        return False

    changed = False
    for job_file in job.files:
        if job_file.buffer_abs_path:
            safe_unlink(Path(job_file.buffer_abs_path))
            job_file.buffer_abs_path = None
            changed = True
    safe_remove_tree(HOT_BUFFER_ROOT / job_id)
    session.commit()
    rebuild_job_export(session, job_id)
    return changed


def maybe_release_job_buffer_after_archive(session: Session, job_id: str) -> bool:
    job = (
        session.execute(
            select(Job)
            .where(Job.id == job_id)
            .options(
                selectinload(Job.files).selectinload(JobFile.archive_pieces),
            )
        )
        .scalar_one_or_none()
    )
    if job is None or job.keep_buffer_after_archive:
        return False
    if any(job_file.buffer_abs_path is None for job_file in job.files):
        return False

    for job_file in job.files:
        archived_bytes = sum(
            piece.payload_size_bytes
            for piece in job_file.archive_pieces
        )
        if archived_bytes != job_file.size_bytes:
            return False

    disc_ids = {
        piece.disc_id
        for job_file in job.files
        for piece in job_file.archive_pieces
    }
    if not disc_ids:
        return False

    discs = session.execute(
        select(Disc).where(Disc.id.in_(disc_ids))
    ).scalars().all()
    if len(discs) != len(disc_ids) or any(disc.burn_confirmed_at is None for disc in discs):
        return False

    return release_job_buffer_files(session, job_id)


def disc_tree_nodes(disc: Disc) -> list[dict[str, object]]:
    dirs = set()
    for entry in disc.entries:
        for parent in path_parents(entry.relative_path):
            dirs.add(parent)
    nodes: list[dict[str, object]] = []
    for rel in sorted(dirs):
        nodes.append({"path": rel, "kind": "directory", "online": bool(disc.cached_root_abs_path), "source": "virtual", "disc_ids": [disc.id], "status": disc.status})
    for entry in sorted(disc.entries, key=lambda x: x.relative_path):
        online = False
        if disc.cached_root_abs_path:
            online = (Path(disc.cached_root_abs_path) / entry.relative_path).exists()
        nodes.append({
            "path": entry.relative_path,
            "kind": "file",
            "size_bytes": entry.size_bytes,
            "online": online,
            "source": "cache" if online else None,
            "disc_ids": [disc.id],
            "status": disc.status,
            "extra": {"entry_kind": entry.kind},
        })
    return nodes
