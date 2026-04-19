from __future__ import annotations

import hashlib
import os
import stat
import shutil
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from tempfile import mkdtemp

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .config import (
    ACTIVE_BUFFER_ROOT,
    ACTIVE_CONTAINER_ROOT,
    ACTIVE_MATERIALIZED_ROOT,
    ACTIVE_STAGING_ROOT,
    COLLECTION_INTAKE_ROOT,
    CONTAINER_ROOTS_DIR,
    EXPORT_COLLECTIONS_ROOT,
    INACTIVE_COLLECTION_ROOT,
    INACTIVE_ISO_ROOT,
    OTS_CLIENT_COMMAND,
    ensure_managed_directory,
)
from .models import ArchivePiece, Container, ContainerEntry, Collection, CollectionDirectory, CollectionFile

COLLECTION_HASH_MANIFEST_NAME = "HASHES.yml"
COLLECTION_HASH_PROOF_NAME = f"{COLLECTION_HASH_MANIFEST_NAME}.ots"
COLLECTION_HASH_MANIFEST_SCHEMA = "collection-hash-manifest/v1"


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
    ensure_managed_directory(path.parent)


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


def collection_intake_root(collection_id: str) -> Path:
    return COLLECTION_INTAKE_ROOT / normalize_root_node_name(collection_id)


def collection_buffer_path(collection_id: str, relative_path: str) -> Path:
    return ACTIVE_BUFFER_ROOT / collection_id / normalize_relpath(relative_path)


def activation_staging_root(session_id: str) -> Path:
    return ACTIVE_STAGING_ROOT / session_id


def activation_staging_file_path(session_id: str, relative_path: str) -> Path:
    return activation_staging_root(session_id) / normalize_relpath(relative_path)


def active_container_root(container_id: str) -> Path:
    return ACTIVE_CONTAINER_ROOT / container_id


def active_container_file_path(container_id: str, relative_path: str) -> Path:
    return active_container_root(container_id) / normalize_relpath(relative_path)


def materialized_collection_root(collection_id: str) -> Path:
    return ACTIVE_MATERIALIZED_ROOT / collection_id


def materialized_collection_file_path(collection_id: str, relative_path: str) -> Path:
    return materialized_collection_root(collection_id) / normalize_relpath(relative_path)


def export_collection_root(collection_id: str) -> Path:
    return EXPORT_COLLECTIONS_ROOT / collection_id


def container_root(container_id: str) -> Path:
    return CONTAINER_ROOTS_DIR / container_id


def registered_iso_storage_path(container_id: str) -> Path:
    return INACTIVE_ISO_ROOT / f"{container_id}.iso"


def inactive_collection_artifact_root(collection_id: str) -> Path:
    return INACTIVE_COLLECTION_ROOT / normalize_root_node_name(collection_id)


def inactive_collection_hash_manifest_path(collection_id: str) -> Path:
    return inactive_collection_artifact_root(collection_id) / COLLECTION_HASH_MANIFEST_NAME


def inactive_collection_hash_proof_path(collection_id: str) -> Path:
    return inactive_collection_artifact_root(collection_id) / COLLECTION_HASH_PROOF_NAME


def iso_volume_label(name: str) -> str:
    allowed = []
    for char in name.upper():
        allowed.append(char if char.isalnum() else "_")
    label = "".join(allowed).strip("_") or "ARCHIVE"
    return label[:32]


def collection_container_artifact_relpaths(collection_id: str) -> tuple[str, str]:
    name = normalize_root_node_name(collection_id)
    return f"collections/{name}/{COLLECTION_HASH_MANIFEST_NAME}", f"collections/{name}/{COLLECTION_HASH_PROOF_NAME}"


def _mode_string(mode: int) -> str:
    return f"{stat.S_IMODE(mode):04o}"


def _mtime_string(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def scan_collection_root(root: Path) -> tuple[list[str], list[dict[str, object]]]:
    if not root.exists():
        raise ValueError(f"collection source directory is missing: {root}")
    if not root.is_dir():
        raise ValueError(f"collection source is not a directory: {root}")

    directories: list[str] = []
    files: list[dict[str, object]] = []

    for current_root, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current_root)
        dirnames.sort()
        filenames.sort()

        for dirname in list(dirnames):
            candidate = current_path / dirname
            details = os.lstat(candidate)
            if stat.S_ISLNK(details.st_mode):
                rel = candidate.relative_to(root).as_posix()
                raise ValueError(f"symlinks are not supported in collections: {rel}")
            if not stat.S_ISDIR(details.st_mode):
                rel = candidate.relative_to(root).as_posix()
                raise ValueError(f"unsupported directory entry in collection: {rel}")
            directories.append(normalize_relpath(candidate.relative_to(root).as_posix()))

        for filename in filenames:
            candidate = current_path / filename
            details = os.lstat(candidate)
            rel = normalize_relpath(candidate.relative_to(root).as_posix())
            if stat.S_ISLNK(details.st_mode):
                raise ValueError(f"symlinks are not supported in collections: {rel}")
            if not stat.S_ISREG(details.st_mode):
                raise ValueError(f"unsupported file entry in collection: {rel}")
            files.append(
                {
                    "relative_path": rel,
                    "abs_path": candidate,
                    "size_bytes": details.st_size,
                    "mode": _mode_string(details.st_mode),
                    "mtime": _mtime_string(details.st_mtime),
                    "uid": details.st_uid,
                    "gid": details.st_gid,
                    "sha256": file_sha256(candidate),
                }
            )

    return directories, files


def collection_tree_nodes_from_root(root: Path, *, source: str, status: str) -> list[dict[str, object]]:
    directories, files = scan_collection_root(root)
    nodes: list[dict[str, object]] = [
        {
            "path": rel,
            "kind": "directory",
            "active": True,
            "source": source,
            "container_ids": [],
            "status": status,
        }
        for rel in directories
    ]
    nodes.extend(
        {
            "path": str(item["relative_path"]),
            "kind": "file",
            "size_bytes": int(item["size_bytes"]),
            "active": True,
            "source": source,
            "container_ids": [],
            "status": status,
            "extra": None,
        }
        for item in files
    )
    return nodes


def collection_live_counts(collection_id: str) -> tuple[int, int]:
    directories, files = scan_collection_root(collection_intake_root(collection_id))
    return len(files), len(directories)


def sync_collection_from_buffer(session: Session, collection_id: str) -> tuple[int, int]:
    collection = (
        session.execute(
            select(Collection)
            .where(Collection.id == collection_id)
            .options(selectinload(Collection.directories), selectinload(Collection.files))
        )
        .scalar_one()
    )
    root = ACTIVE_BUFFER_ROOT / collection_id
    directories, files = scan_collection_root(root)

    for directory in list(collection.directories):
        session.delete(directory)
    for collection_file in list(collection.files):
        session.delete(collection_file)
    session.flush()

    for rel in directories:
        session.add(CollectionDirectory(collection_id=collection_id, relative_path=rel))
    for item in files:
        session.add(
            CollectionFile(
                collection_id=collection_id,
                relative_path=str(item["relative_path"]),
                size_bytes=int(item["size_bytes"]),
                expected_sha256=str(item["sha256"]),
                actual_sha256=str(item["sha256"]),
                mode=str(item["mode"]),
                mtime=str(item["mtime"]),
                uid=int(item["uid"]),
                gid=int(item["gid"]),
                buffer_abs_path=str(item["abs_path"]),
                status="active",
                error_message=None,
            )
        )
    session.flush()
    session.expire(collection, ["directories", "files"])
    return len(files), len(directories)


def collection_hash_manifest_payload(collection: Collection) -> bytes:
    files = [
        {
            "path": collection_file.relative_path,
            "size_bytes": collection_file.size_bytes,
            "sha256": collection_file.actual_sha256,
        }
        for collection_file in sorted(collection.files, key=lambda item: item.relative_path)
        if collection_file.actual_sha256
    ]
    return yaml.safe_dump(
        {
            "schema": COLLECTION_HASH_MANIFEST_SCHEMA,
            "collection_id": collection.id,
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


def refresh_collection_hash_artifacts(session: Session, collection_id: str) -> None:
    collection = session.execute(select(Collection).where(Collection.id == collection_id).options(selectinload(Collection.files))).scalar_one()
    payload = collection_hash_manifest_payload(collection)
    artifact_root = inactive_collection_artifact_root(collection_id)
    ensure_managed_directory(artifact_root)
    temp_root = Path(mkdtemp(prefix=".collection-hashes-", dir=str(artifact_root)))
    try:
        manifest_path = temp_root / COLLECTION_HASH_MANIFEST_NAME
        manifest_path.write_bytes(payload)
        proof_path = _run_ots_stamp(manifest_path)

        manifest_path.replace(inactive_collection_hash_manifest_path(collection_id))
        proof_path.replace(inactive_collection_hash_proof_path(collection_id))
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _piece_active_path(piece: ArchivePiece) -> Path | None:
    container = piece.container
    if not container.active_root_abs_path:
        return None
    path = Path(container.active_root_abs_path) / piece.payload_relpath
    return path if path.exists() else None


def recompute_collection_file_runtime(collection_file: CollectionFile) -> tuple[Path | None, str | None, list[str]]:
    if collection_file.materialized_abs_path:
        old = Path(collection_file.materialized_abs_path)
        if old.exists():
            old.unlink(missing_ok=True)
    collection_file.materialized_abs_path = None

    if collection_file.buffer_abs_path:
        path = Path(collection_file.buffer_abs_path)
        if path.exists():
            collection_file.status = "active"
            collection_file.error_message = None
            return path, "buffer", []

    pieces = sorted(collection_file.archive_pieces, key=lambda p: (p.chunk_index or 0, p.container_id))
    if not pieces:
        collection_file.status = "inactive"
        return None, None, []

    unsplit_paths = []
    for piece in pieces:
        path = _piece_active_path(piece)
        if path is not None and piece.chunk_count is None:
            unsplit_paths.append((path, piece.container_id))
    if unsplit_paths:
        collection_file.status = "active"
        collection_file.error_message = None
        return unsplit_paths[0][0], "activation", []

    count = max((p.chunk_count or 0) for p in pieces)
    available: dict[int, Path] = {}
    missing_containers: set[str] = set()
    for piece in pieces:
        if piece.chunk_count is None or piece.chunk_index is None:
            continue
        path = _piece_active_path(piece)
        if path is not None and piece.chunk_index not in available:
            available[piece.chunk_index] = path
        elif path is None:
            missing_containers.add(piece.container_id)

    if count >= 2 and all(index in available for index in range(1, count + 1)):
        out = materialized_collection_file_path(collection_file.collection_id, collection_file.relative_path)
        ensure_parent_dir(out)
        temp = out.with_name(f".{out.name}.tmp")
        with temp.open("wb") as handle:
            for index in range(1, count + 1):
                with available[index].open("rb") as src:
                    shutil.copyfileobj(src, handle, length=1024 * 1024)
        temp.replace(out)
        collection_file.materialized_abs_path = str(out)
        collection_file.status = "active"
        collection_file.error_message = None
        return out, "materialized", []

    containers = sorted({p.container_id for p in pieces})
    collection_file.status = "inactive"
    if count >= 2:
        collection_file.error_message = f"This split file is not active right now. Required active containers are missing. Candidate containers: {', '.join(containers)}."
    else:
        collection_file.error_message = f"This file is not active right now. It is stored on container {containers[0]}."
    return None, None, containers


def rebuild_collection_export(session: Session, collection_id: str) -> None:
    collection = (
        session.execute(
            select(Collection)
            .where(Collection.id == collection_id)
            .options(selectinload(Collection.directories), selectinload(Collection.files).selectinload(CollectionFile.archive_pieces).selectinload(ArchivePiece.container))
        )
        .scalar_one()
    )
    root = export_collection_root(collection_id)
    safe_remove_tree(root)
    ensure_managed_directory(root)
    safe_remove_tree(materialized_collection_root(collection_id))

    explicit_dirs = {d.relative_path for d in collection.directories}
    derived_dirs = set()
    for jf in collection.files:
        for parent in path_parents(jf.relative_path):
            derived_dirs.add(parent)
    for rel in sorted(explicit_dirs | derived_dirs):
        ensure_managed_directory(root / rel)

    for jf in collection.files:
        active_path, _source, _container_ids = recompute_collection_file_runtime(jf)
        if active_path is None:
            continue
        atomic_replace_file_link(root / normalize_relpath(jf.relative_path), active_path)
    session.commit()


def release_collection_buffer_files(session: Session, collection_id: str) -> bool:
    collection = (
        session.execute(
            select(Collection)
            .where(Collection.id == collection_id)
            .options(selectinload(Collection.files))
        )
        .scalar_one_or_none()
    )
    if collection is None:
        return False

    changed = False
    for collection_file in collection.files:
        if collection_file.buffer_abs_path:
            safe_unlink(Path(collection_file.buffer_abs_path))
            collection_file.buffer_abs_path = None
            changed = True
    safe_remove_tree(ACTIVE_BUFFER_ROOT / collection_id)
    session.commit()
    rebuild_collection_export(session, collection_id)
    return changed


def maybe_release_collection_buffer_after_archive(session: Session, collection_id: str) -> bool:
    collection = (
        session.execute(
            select(Collection)
            .where(Collection.id == collection_id)
            .options(
                selectinload(Collection.files).selectinload(CollectionFile.archive_pieces),
            )
        )
        .scalar_one_or_none()
    )
    if collection is None or collection.keep_buffer_after_archive:
        return False
    if any(collection_file.buffer_abs_path is None for collection_file in collection.files):
        return False

    for collection_file in collection.files:
        archived_bytes = sum(
            piece.payload_size_bytes
            for piece in collection_file.archive_pieces
        )
        if archived_bytes != collection_file.size_bytes:
            return False

    container_ids = {
        piece.container_id
        for collection_file in collection.files
        for piece in collection_file.archive_pieces
    }
    if not container_ids:
        return False

    containers = session.execute(
        select(Container).where(Container.id.in_(container_ids))
    ).scalars().all()
    if len(containers) != len(container_ids) or any(container.burn_confirmed_at is None for container in containers):
        return False

    return release_collection_buffer_files(session, collection_id)


def container_tree_nodes(container: Container) -> list[dict[str, object]]:
    dirs = set()
    for entry in container.entries:
        for parent in path_parents(entry.relative_path):
            dirs.add(parent)
    nodes: list[dict[str, object]] = []
    for rel in sorted(dirs):
        nodes.append({"path": rel, "kind": "directory", "active": bool(container.active_root_abs_path), "source": "virtual", "container_ids": [container.id], "status": container.status})
    for entry in sorted(container.entries, key=lambda x: x.relative_path):
        active = False
        if container.active_root_abs_path:
            active = (Path(container.active_root_abs_path) / entry.relative_path).exists()
        nodes.append({
            "path": entry.relative_path,
            "kind": "file",
            "size_bytes": entry.size_bytes,
            "active": active,
            "source": "activation" if active else None,
            "container_ids": [container.id],
            "status": container.status,
            "extra": {"entry_kind": entry.kind},
        })
    return nodes
