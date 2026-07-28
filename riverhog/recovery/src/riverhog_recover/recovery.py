from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

_SCHEMA = "collection-archive-manifest/v2"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_OBJECT_ID_RE = re.compile(r"data-[0-9]{6}")


class RecoveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    output: Path
    files: int
    bytes: int


@dataclass(frozen=True, slots=True)
class ObjectRecord:
    id: str
    kind: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Placement:
    object_id: str
    offset: int
    bytes: int
    member: str | None


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    bytes: int
    sha256: str
    placements: tuple[Placement, ...]


@dataclass(frozen=True, slots=True)
class Manifest:
    objects: tuple[ObjectRecord, ...]
    files: tuple[FileRecord, ...]


def recover_archive(
    archive_dir: Path,
    output_dir: Path,
    *,
    passphrase: str,
    age_command: str = "age",
    minisign_public_key: Path | None = None,
    minisign_command: str = "minisign",
) -> RecoverySummary:
    archive = archive_dir.expanduser().resolve()
    output = output_dir.expanduser().absolute()
    if not archive.is_dir():
        raise RecoveryError(f"archive directory does not exist: {archive}")
    if output.exists():
        raise RecoveryError(f"output path already exists: {output}")
    if not passphrase:
        raise RecoveryError("archive passphrase is empty")

    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f".{output.name}.recover-", dir=output.parent))
    staging = scratch / "output"
    staging.mkdir()
    try:
        checksums = _load_checksums(archive)
        if minisign_public_key is not None:
            _verify_minisign(
                archive,
                public_key=minisign_public_key,
                command=minisign_command,
            )
        encrypted_manifest = _archive_file(archive, "manifest.yml.age")
        _verify_inventory_file(encrypted_manifest, "manifest.yml.age", checksums)
        manifest_path = scratch / "manifest.yml"
        _age_decrypt(
            encrypted_manifest,
            manifest_path,
            passphrase=passphrase,
            command=age_command,
        )
        manifest = _parse_manifest(manifest_path.read_bytes())

        files_by_object: dict[str, list[tuple[FileRecord, Placement]]] = {
            current.id: [] for current in manifest.objects
        }
        for file in manifest.files:
            for placement in file.placements:
                files_by_object[placement.object_id].append((file, placement))

        plaintext_object = scratch / "archive-object"
        for current in manifest.objects:
            relative = f"objects/{current.id}.age"
            encrypted_object = _archive_file(archive, relative)
            _verify_inventory_file(encrypted_object, relative, checksums)
            _age_decrypt(
                encrypted_object,
                plaintext_object,
                passphrase=passphrase,
                command=age_command,
            )
            _verify_plaintext(
                plaintext_object,
                expected_bytes=current.bytes,
                expected_sha256=current.sha256,
                label=current.id,
            )
            if current.kind == "pack":
                _recover_pack(
                    plaintext_object,
                    staging=staging,
                    placements=files_by_object[current.id],
                )
            else:
                _recover_raw_object(
                    plaintext_object,
                    staging=staging,
                    placements=files_by_object[current.id],
                )
            plaintext_object.unlink()

        for file in manifest.files:
            _verify_plaintext(
                _output_file(staging, file.path),
                expected_bytes=file.bytes,
                expected_sha256=file.sha256,
                label=file.path,
            )
        os.replace(staging, output)
        return RecoverySummary(
            output=output,
            files=len(manifest.files),
            bytes=sum(file.bytes for file in manifest.files),
        )
    except RecoveryError:
        raise
    except (OSError, ValueError, yaml.YAMLError, tarfile.TarError) as exc:
        raise RecoveryError(str(exc)) from exc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _load_checksums(archive: Path) -> dict[str, str] | None:
    path = archive / "SHA256SUMS"
    if not path.exists():
        return None
    if not path.is_file():
        raise RecoveryError("SHA256SUMS is not a regular file")
    entries: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RecoveryError(f"cannot read SHA256SUMS: {exc}") from exc
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise RecoveryError("SHA256SUMS has an invalid entry")
        relative = _normalize_relpath(match.group(2))
        if relative in entries:
            raise RecoveryError(f"SHA256SUMS repeats {relative}")
        entries[relative] = match.group(1)
        _verify_inventory_file(_archive_file(archive, relative), relative, entries)
    if not entries:
        raise RecoveryError("SHA256SUMS is empty")
    return entries


def _verify_inventory_file(path: Path, relative: str, checksums: Mapping[str, str] | None) -> None:
    if checksums is None:
        return
    expected = checksums.get(relative)
    if expected is None:
        raise RecoveryError(f"SHA256SUMS does not cover {relative}")
    actual = _sha256(path)
    if actual != expected:
        raise RecoveryError(f"ciphertext checksum mismatch: {relative}")


def _verify_minisign(archive: Path, *, public_key: Path, command: str) -> None:
    checksums = archive / "SHA256SUMS"
    signature = archive / "SHA256SUMS.minisig"
    if not checksums.is_file() or not signature.is_file():
        raise RecoveryError("Minisign verification requires SHA256SUMS and SHA256SUMS.minisig")
    try:
        completed = subprocess.run(
            [
                command,
                "-V",
                "-H",
                "-q",
                "-p",
                str(public_key),
                "-m",
                str(checksums),
                "-x",
                str(signature),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RecoveryError(f"cannot run Minisign: {exc}") from exc
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise RecoveryError(message or "Minisign verification failed")


def _age_decrypt(source: Path, destination: Path, *, passphrase: str, command: str) -> None:
    destination.unlink(missing_ok=True)
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, passphrase.encode("utf-8"))
    finally:
        os.close(write_fd)
    env = os.environ.copy()
    env.pop("AGE_PASSPHRASE", None)
    env["AGE_PASSPHRASE_FD"] = str(read_fd)
    try:
        try:
            completed = subprocess.run(
                [command, "--decrypt", "-j", "batchpass", "-o", str(destination), str(source)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                pass_fds=(read_fd,),
            )
        except OSError as exc:
            raise RecoveryError(f"cannot run age: {exc}") from exc
    finally:
        os.close(read_fd)
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise RecoveryError(message or f"age decryption failed: {source.name}")
    if not destination.is_file():
        raise RecoveryError(f"age produced no plaintext for {source.name}")


def _parse_manifest(content: bytes) -> Manifest:
    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise RecoveryError("manifest is not valid YAML") from exc
    if not isinstance(payload, dict) or payload.get("schema") != _SCHEMA:
        raise RecoveryError("manifest schema is not collection-archive-manifest/v2")

    raw_objects = payload.get("objects")
    if not isinstance(raw_objects, list) or not raw_objects:
        raise RecoveryError("manifest objects must be a non-empty list")
    objects: list[ObjectRecord] = []
    for index, value in enumerate(raw_objects):
        if not isinstance(value, dict):
            raise RecoveryError("manifest object is not a mapping")
        object_id = _required_string(value, "id")
        kind = _required_string(value, "kind")
        byte_count = _required_nonnegative_int(value, "bytes")
        sha256 = _required_sha256(value, "sha256")
        if object_id != f"data-{index:06d}" or _OBJECT_ID_RE.fullmatch(object_id) is None:
            raise RecoveryError("manifest object ids are not canonical and sequential")
        if kind not in {"pack", "file", "segment"}:
            raise RecoveryError(f"manifest object kind is invalid: {object_id}")
        objects.append(ObjectRecord(object_id, kind, byte_count, sha256))
    object_by_id = {current.id: current for current in objects}

    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise RecoveryError("manifest files must be a non-empty list")
    files: list[FileRecord] = []
    seen_paths: set[str] = set()
    object_placements: dict[str, list[Placement]] = {current.id: [] for current in objects}
    tree_digest = hashlib.sha256()
    tree_bytes = 0
    for value in raw_files:
        if not isinstance(value, dict):
            raise RecoveryError("manifest file is not a mapping")
        path = _normalize_relpath(_required_string(value, "path"))
        if path in seen_paths:
            raise RecoveryError(f"manifest repeats file path: {path}")
        seen_paths.add(path)
        byte_count = _required_nonnegative_int(value, "bytes")
        sha256 = _required_sha256(value, "sha256")
        raw_placements = value.get("objects")
        if not isinstance(raw_placements, list) or not raw_placements:
            raise RecoveryError(f"manifest file has no object mappings: {path}")
        placements: list[Placement] = []
        expected_offset = 0
        for raw in raw_placements:
            if not isinstance(raw, dict):
                raise RecoveryError(f"manifest file mapping is invalid: {path}")
            object_id = _required_string(raw, "object")
            offset = _required_nonnegative_int(raw, "offset")
            length = _required_nonnegative_int(raw, "bytes")
            member_value = raw.get("member")
            member = _normalize_relpath(member_value) if isinstance(member_value, str) else None
            current = object_by_id.get(object_id)
            if current is None or offset != expected_offset:
                raise RecoveryError(f"manifest file mappings are not contiguous: {path}")
            if current.kind == "pack":
                if len(raw_placements) != 1 or member != path or length != byte_count:
                    raise RecoveryError(f"manifest pack mapping is invalid: {path}")
            elif member is not None:
                raise RecoveryError(f"manifest raw object has a member: {path}")
            placement = Placement(object_id, offset, length, member)
            placements.append(placement)
            object_placements[object_id].append(placement)
            expected_offset += length
        if expected_offset != byte_count:
            raise RecoveryError(f"manifest mappings do not cover file: {path}")
        files.append(FileRecord(path, byte_count, sha256, tuple(placements)))
        tree_digest.update(f"{path}\t{byte_count}\t{sha256}\n".encode())
        tree_bytes += byte_count

    for current in objects:
        placements = object_placements[current.id]
        if not placements:
            raise RecoveryError(f"manifest object is unused: {current.id}")
        if current.kind == "pack":
            if any(item.member is None or item.offset != 0 for item in placements):
                raise RecoveryError(f"manifest pack placements are invalid: {current.id}")
        elif len(placements) != 1 or placements[0].bytes != current.bytes:
            raise RecoveryError(f"manifest raw object placement is invalid: {current.id}")
        elif current.kind == "file" and placements[0].offset != 0:
            raise RecoveryError(f"manifest file object offset is invalid: {current.id}")

    tree = payload.get("tree")
    if not isinstance(tree, dict):
        raise RecoveryError("manifest tree is missing")
    if tree.get("sha256") != tree_digest.hexdigest() or tree.get("total_bytes") != tree_bytes:
        raise RecoveryError("manifest tree digest does not match its files")
    return Manifest(tuple(objects), tuple(files))


def _recover_pack(
    plaintext: Path,
    *,
    staging: Path,
    placements: list[tuple[FileRecord, Placement]],
) -> None:
    expected = {placement.member: file for file, placement in placements}
    if None in expected or len(expected) != len(placements):
        raise RecoveryError("pack manifest contains duplicate or empty members")
    with tarfile.open(plaintext, mode="r:") as archive:
        members = archive.getmembers()
        by_name = {member.name: member for member in members}
        if len(by_name) != len(members) or set(by_name) != set(expected):
            raise RecoveryError("pack members do not exactly match the manifest")
        for name, file in expected.items():
            assert name is not None
            member = by_name[name]
            if not member.isfile() or member.size != file.bytes:
                raise RecoveryError(f"pack member is not the expected regular file: {name}")
            source = archive.extractfile(member)
            if source is None:
                raise RecoveryError(f"cannot read pack member: {name}")
            destination = _output_file(staging, file.path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source, destination.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)


def _recover_raw_object(
    plaintext: Path,
    *,
    staging: Path,
    placements: list[tuple[FileRecord, Placement]],
) -> None:
    if len(placements) != 1:
        raise RecoveryError("raw archive object must map to exactly one file")
    file, placement = placements[0]
    destination = _output_file(staging, file.path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size != placement.offset:
        raise RecoveryError(f"file segments are out of order: {file.path}")
    if not destination.exists() and placement.offset != 0:
        raise RecoveryError(f"file starts with a nonzero segment offset: {file.path}")
    mode = "ab" if destination.exists() else "xb"
    with plaintext.open("rb") as source, destination.open(mode) as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)


def _verify_plaintext(
    path: Path,
    *,
    expected_bytes: int,
    expected_sha256: str,
    label: str,
) -> None:
    if not path.is_file():
        raise RecoveryError(f"recovered file is missing: {label}")
    if path.stat().st_size != expected_bytes or _sha256(path) != expected_sha256:
        raise RecoveryError(f"size or SHA-256 mismatch: {label}")


def _archive_file(root: Path, relative: str) -> Path:
    normalized = _normalize_relpath(relative)
    candidate = root.joinpath(*PurePosixPath(normalized).parts).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise RecoveryError(f"archive file is missing or outside its directory: {normalized}")
    return candidate


def _output_file(root: Path, relative: str) -> Path:
    normalized = _normalize_relpath(relative)
    candidate = root.joinpath(*PurePosixPath(normalized).parts)
    if not candidate.absolute().is_relative_to(root.absolute()):
        raise RecoveryError(f"output path escapes recovery directory: {normalized}")
    return candidate


def _normalize_relpath(value: str) -> str:
    if not value or "\\" in value:
        raise RecoveryError("archive path is empty or contains a backslash")
    path = PurePosixPath(value)
    normalized = str(path)
    invalid_part = any(part in {"", ".", ".."} for part in path.parts)
    if path.is_absolute() or normalized != value or invalid_part:
        raise RecoveryError(f"archive path is not a canonical relative path: {value}")
    return normalized


def _required_string(value: Mapping[str, Any], key: str) -> str:
    current = value.get(key)
    if not isinstance(current, str) or not current:
        raise RecoveryError(f"manifest {key} must be a non-empty string")
    return current


def _required_nonnegative_int(value: Mapping[str, Any], key: str) -> int:
    current = value.get(key)
    if isinstance(current, bool) or not isinstance(current, int) or current < 0:
        raise RecoveryError(f"manifest {key} must be a non-negative integer")
    return current


def _required_sha256(value: Mapping[str, Any], key: str) -> str:
    current = _required_string(value, key)
    if _SHA256_RE.fullmatch(current) is None:
        raise RecoveryError(f"manifest {key} must be a lowercase SHA-256 digest")
    return current


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["RecoveryError", "RecoverySummary", "recover_archive"]
