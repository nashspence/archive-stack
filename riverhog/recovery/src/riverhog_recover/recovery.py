from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from riverhog_archive_contracts import (
    RECOVERY_DESCRIPTOR_PATH,
    RecoveryDescriptor,
    RecoveryDescriptorError,
)

from ._provenance import (
    ValidatedProvenance,
    build_portable_provenance_set,
    validate_provenance_archive,
)

MANIFEST_SCHEMA = "collection-archive-manifest/v1"
PACK_INDEX_SCHEMA = "riverhog-pack-index/v1"
PACK_INDEX_PATH = ".riverhog/pack-index.json"
PACK_PADDING_PREFIX = ".riverhog/padding/"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VOLUME_ID_RE = re.compile(r"(?:pack|segment)-[0-9]{12}")
_PADDING_PATH_RE = re.compile(r"\.riverhog/padding/pack-[0-9]{12}-[0-9]{6}")


class RecoveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    output: Path
    files: int
    bytes: int
    volumes: int
    provenance_mode: str = "omitted"
    provenance_journals: int = 0


@dataclass(frozen=True, slots=True)
class FileIdentity:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PackFileIdentity:
    path: str
    bytes: int
    sha256: str
    header_offset: int
    data_offset: int


@dataclass(frozen=True, slots=True)
class PartIdentity:
    number: int
    plaintext_start: int
    plaintext_bytes: int
    plaintext_sha256: str
    stored_bytes: int
    stored_sha256: str


@dataclass(frozen=True, slots=True)
class VolumeIdentity:
    id: str
    sequence: int
    kind: str
    path: str
    plaintext_bytes: int
    parts: tuple[PartIdentity, ...]
    files: int | None = None
    source_bytes: int | None = None
    index_sha256: str | None = None
    source_file: FileIdentity | None = None
    file_offset: int | None = None


@dataclass(frozen=True, slots=True)
class Manifest:
    files: int
    bytes: int
    tree_sha256: str
    volumes: tuple[VolumeIdentity, ...]
    provenance: ProvenanceIdentity | None = None


@dataclass(frozen=True, slots=True)
class ProvenanceObjectIdentity:
    id: str
    kind: str
    path: str
    plaintext_bytes: int
    sha256: str
    stored_bytes: int
    stored_sha256: str


@dataclass(frozen=True, slots=True)
class ProvenanceIdentity:
    identity: str
    index: ProvenanceObjectIdentity
    bundles: tuple[ProvenanceObjectIdentity, ...]


def recover_archive(
    archive_dir: Path,
    output_dir: Path,
    *,
    passphrases: Mapping[str, str],
    age_command: str = "age",
    ots_command: str = "ots",
    minisign_public_key: Path | None = None,
    minisign_command: str = "minisign",
) -> RecoverySummary:
    archive = archive_dir.expanduser().resolve()
    output = output_dir.expanduser().absolute()
    if not archive.is_dir():
        raise RecoveryError(f"archive directory does not exist: {archive}")
    if output.exists():
        raise RecoveryError(f"output path already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f".{output.name}.recover-", dir=output.parent))
    staging = scratch / "output"
    staging.mkdir()
    expected_files: dict[str, FileIdentity] = {}
    raw_ranges: dict[str, list[tuple[int, int]]] = {}
    try:
        checksums = _load_checksums(archive)
        if minisign_public_key is not None:
            _verify_minisign(
                archive,
                public_key=minisign_public_key,
                command=minisign_command,
            )

        descriptor = read_recovery_descriptor(archive)
        descriptor_path = _archive_file(archive, RECOVERY_DESCRIPTOR_PATH)
        _verify_inventory_file(descriptor_path, RECOVERY_DESCRIPTOR_PATH, checksums)
        encrypted_manifest = _archive_file(archive, descriptor.root.path)
        _verify_stored_identity(
            encrypted_manifest,
            expected_bytes=descriptor.root.stored_bytes,
            expected_sha256=descriptor.root.stored_sha256,
            label=descriptor.root.path,
        )
        try:
            passphrase = passphrases[descriptor.encryption.passphrase_id]
        except KeyError as exc:
            raise RecoveryError(
                "no passphrase is available for archive key ID "
                f"{descriptor.encryption.passphrase_id}"
            ) from exc
        if not isinstance(passphrase, str) or not passphrase:
            raise RecoveryError(
                f"archive passphrase is empty for key ID {descriptor.encryption.passphrase_id}"
            )

        _verify_inventory_file(encrypted_manifest, descriptor.root.path, checksums)
        manifest_path = scratch / "manifest.json"
        _age_decrypt(
            encrypted_manifest,
            manifest_path,
            passphrase=passphrase,
            command=age_command,
        )

        encrypted_proof = _archive_file(archive, "manifest.json.ots.age")
        _verify_inventory_file(encrypted_proof, "manifest.json.ots.age", checksums)
        proof_path = scratch / "manifest.json.ots"
        _age_decrypt(
            encrypted_proof,
            proof_path,
            passphrase=passphrase,
            command=age_command,
        )
        _verify_timestamp(manifest_path, proof_path, command=ots_command)
        manifest = _parse_manifest(manifest_path.read_bytes())
        _verify_attestation_inventory(checksums, manifest)

        for volume in manifest.volumes:
            encrypted = _archive_file(archive, volume.path)
            _verify_stored_parts(encrypted, volume.parts)
            plaintext = scratch / f"{volume.id}.plaintext"
            _age_decrypt(encrypted, plaintext, passphrase=passphrase, command=age_command)
            _verify_plaintext_parts(plaintext, volume)
            if volume.kind == "pack":
                recovered = _recover_pack(plaintext, staging=staging, volume=volume)
                for current in recovered:
                    if current.path in expected_files:
                        raise RecoveryError(f"archive repeats a logical file: {current.path}")
                    expected_files[current.path] = current
            else:
                if volume.source_file is None or volume.file_offset is None:
                    raise RecoveryError("segment volume has no source mapping")
                current = volume.source_file
                previous = expected_files.get(current.path)
                if previous is not None and previous != current:
                    raise RecoveryError(f"archive disagrees about a logical file: {current.path}")
                expected_files[current.path] = current
                _recover_segment(
                    plaintext,
                    staging=staging,
                    file=current,
                    offset=volume.file_offset,
                )
                raw_ranges.setdefault(current.path, []).append(
                    (volume.file_offset, volume.plaintext_bytes)
                )
            plaintext.unlink()

        _validate_raw_ranges(expected_files, raw_ranges)
        for current in expected_files.values():
            _verify_file(_output_file(staging, current.path), current)
        tree = _tree_identity(tuple(expected_files.values()))
        if (
            tree["files"] != manifest.files
            or tree["bytes"] != manifest.bytes
            or tree["sha256"] != manifest.tree_sha256
        ):
            raise RecoveryError("recovered collection tree does not match the root manifest")
        provenance_mode = "omitted"
        provenance_journals = 0
        if manifest.provenance is not None:
            validated = _recover_provenance(
                archive,
                scratch=scratch,
                staging=staging,
                descriptor=manifest.provenance,
                expected_files=expected_files,
                checksums=checksums,
                passphrase=passphrase,
                age_command=age_command,
            )
            provenance_mode = (
                "mixed"
                if any(item.status == "omitted" for item in validated.bindings)
                else "captured"
            )
            provenance_journals = len(validated.journal_bytes)
        os.replace(staging, output)
        return RecoverySummary(
            output=output,
            files=manifest.files,
            bytes=manifest.bytes,
            volumes=len(manifest.volumes),
            provenance_mode=provenance_mode,
            provenance_journals=provenance_journals,
        )
    except RecoveryError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, tarfile.TarError) as exc:
        raise RecoveryError(str(exc)) from exc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def read_recovery_descriptor(archive_dir: Path) -> RecoveryDescriptor:
    archive = archive_dir.expanduser().resolve()
    try:
        content = _archive_file(archive, RECOVERY_DESCRIPTOR_PATH).read_bytes()
        return RecoveryDescriptor.from_json_bytes(content)
    except RecoveryDescriptorError as exc:
        raise RecoveryError(str(exc)) from exc
    except OSError as exc:
        raise RecoveryError(f"cannot read {RECOVERY_DESCRIPTOR_PATH}: {exc}") from exc


def _verify_stored_identity(
    path: Path,
    *,
    expected_bytes: int,
    expected_sha256: str,
    label: str,
) -> None:
    if path.stat().st_size != expected_bytes or _sha256(path) != expected_sha256:
        raise RecoveryError(f"stored archive object does not match recovery descriptor: {label}")


def _parse_manifest(content: bytes) -> Manifest:
    payload = json.loads(content)
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != MANIFEST_SCHEMA
        or set(payload)
        not in (
            {"schema", "format", "tree", "volumes"},
            {"schema", "format", "tree", "volumes", "provenance"},
        )
    ):
        raise RecoveryError("root manifest schema is not collection-archive-manifest/v1")
    if payload.get("format") != {
        "encryption": "age-v1-scrypt",
        "pack_index": PACK_INDEX_SCHEMA,
        "part_digest": "sha256",
        "selective_read": "age-chunk-range/v1",
    }:
        raise RecoveryError("root manifest format is unsupported")
    tree = payload.get("tree")
    raw_volumes = payload.get("volumes")
    if (
        not isinstance(tree, dict)
        or set(tree) != {"files", "bytes", "sha256"}
        or not isinstance(raw_volumes, list)
        or not raw_volumes
    ):
        raise RecoveryError("root manifest structure is invalid")
    files = _required_nonnegative_int(tree, "files")
    byte_count = _required_nonnegative_int(tree, "bytes")
    tree_sha256 = _required_sha256(tree, "sha256")
    if files < 1:
        raise RecoveryError("root manifest file count must be positive")

    volumes: list[VolumeIdentity] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for sequence, raw in enumerate(raw_volumes):
        volume = _parse_volume(raw, expected_sequence=sequence)
        if volume.id in seen_ids or volume.path in seen_paths:
            raise RecoveryError("root manifest repeats a volume identity")
        seen_ids.add(volume.id)
        seen_paths.add(volume.path)
        volumes.append(volume)
    provenance = _parse_provenance(payload["provenance"]) if "provenance" in payload else None
    return Manifest(files, byte_count, tree_sha256, tuple(volumes), provenance)


def _parse_provenance(value: object) -> ProvenanceIdentity:
    if not isinstance(value, Mapping) or set(value) != {"identity", "index", "bundles"}:
        raise RecoveryError("root manifest provenance descriptor is invalid")
    identity = _required_sha256(value, "identity")
    index = _parse_provenance_object(value.get("index"), kind="provenance-index")
    raw_bundles = value.get("bundles")
    if not isinstance(raw_bundles, list) or not raw_bundles:
        raise RecoveryError("root manifest provenance bundles are invalid")
    bundles = tuple(
        _parse_provenance_object(item, kind="provenance-bundle") for item in raw_bundles
    )
    if index.sha256 != identity:
        raise RecoveryError("root manifest provenance identity does not match its index")
    if [item.id for item in bundles] != [
        f"bundle-{sequence:012d}" for sequence in range(len(bundles))
    ]:
        raise RecoveryError("root manifest provenance bundle order is not canonical")
    return ProvenanceIdentity(identity=identity, index=index, bundles=bundles)


def _parse_provenance_object(value: object, *, kind: str) -> ProvenanceObjectIdentity:
    fields = {
        "id",
        "kind",
        "path",
        "plaintext_bytes",
        "sha256",
        "stored_bytes",
        "stored_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != fields or value.get("kind") != kind:
        raise RecoveryError("root manifest provenance object is invalid")
    object_id = str(value.get("id") or "")
    path = _normalize_relpath(str(value.get("path") or ""))
    expected_path = (
        "provenance/index.json.age"
        if kind == "provenance-index"
        else f"provenance/{object_id}.tar.age"
    )
    if path != expected_path:
        raise RecoveryError("root manifest provenance path is not canonical")
    plaintext_bytes = _required_nonnegative_int(value, "plaintext_bytes")
    stored_bytes = _required_nonnegative_int(value, "stored_bytes")
    if plaintext_bytes < 1 or stored_bytes < 1:
        raise RecoveryError("root manifest provenance object is empty")
    return ProvenanceObjectIdentity(
        id=object_id,
        kind=kind,
        path=path,
        plaintext_bytes=plaintext_bytes,
        sha256=_required_sha256(value, "sha256"),
        stored_bytes=stored_bytes,
        stored_sha256=_required_sha256(value, "stored_sha256"),
    )


def _recover_provenance(
    archive: Path,
    *,
    scratch: Path,
    staging: Path,
    descriptor: ProvenanceIdentity,
    expected_files: Mapping[str, FileIdentity],
    checksums: Mapping[str, str] | None,
    passphrase: str,
    age_command: str,
) -> ValidatedProvenance:
    objects = (descriptor.index, *descriptor.bundles)
    plaintext: dict[str, bytes] = {}
    for current in objects:
        encrypted = _archive_file(archive, current.path)
        _verify_inventory_file(encrypted, current.path, checksums)
        if (
            encrypted.stat().st_size != current.stored_bytes
            or _sha256(encrypted) != current.stored_sha256
        ):
            raise RecoveryError(
                f"provenance ciphertext does not match the root manifest: {current.id}"
            )
        destination = scratch / f"{current.id}.provenance"
        _age_decrypt(encrypted, destination, passphrase=passphrase, command=age_command)
        content = destination.read_bytes()
        if (
            len(content) != current.plaintext_bytes
            or hashlib.sha256(content).hexdigest() != current.sha256
        ):
            raise RecoveryError(
                f"provenance plaintext does not match the root manifest: {current.id}"
            )
        plaintext[current.id] = content
        destination.unlink()
    index_bytes = plaintext.pop(descriptor.index.id)
    validated = validate_provenance_archive(index_bytes, plaintext)
    if validated.identity != descriptor.identity:
        raise RecoveryError("provenance index identity changed during recovery")
    bindings = {item.path: item for item in validated.bindings}
    if set(bindings) != set(expected_files):
        raise RecoveryError("provenance does not account for every recovered file")
    for path, expected in expected_files.items():
        binding = bindings[path]
        if binding.bytes != expected.bytes or binding.sha256 != expected.sha256:
            raise RecoveryError(f"provenance payload binding mismatch: {path}")

    root = staging / ".riverhog" / "provenance"
    journal_dir = root / "journals"
    journal_dir.mkdir(parents=True)
    portable_index = build_portable_provenance_set(
        bindings=validated.bindings,
        journals=validated.journal_bytes,
    )
    (root / "index.json").write_bytes(portable_index)
    for journal_id, content in sorted(validated.journal_bytes.items()):
        (journal_dir / f"{journal_id}.json-seq").write_bytes(content)
    return validated


def _parse_volume(value: object, *, expected_sequence: int) -> VolumeIdentity:
    if not isinstance(value, dict):
        raise RecoveryError("root manifest volume is not a mapping")
    volume_id = str(value.get("id", ""))
    sequence = _required_nonnegative_int(value, "sequence")
    kind = str(value.get("kind", ""))
    pack_fields = {
        "id",
        "sequence",
        "kind",
        "path",
        "files",
        "source_bytes",
        "plaintext_bytes",
        "age_state",
        "index_sha256",
        "plan_sha256",
        "parts",
    }
    segment_fields = {
        "id",
        "sequence",
        "kind",
        "path",
        "plaintext_bytes",
        "age_state",
        "file",
        "parts",
    }
    expected_fields = (
        pack_fields if kind == "pack" else segment_fields if kind == "segment" else set()
    )
    if set(value) != expected_fields:
        raise RecoveryError("root manifest volume fields are invalid")
    path = _normalize_relpath(str(value.get("path", "")))
    plaintext_bytes = _required_nonnegative_int(value, "plaintext_bytes")
    if sequence != expected_sequence or _VOLUME_ID_RE.fullmatch(volume_id) is None:
        raise RecoveryError("root manifest volume identity is invalid")
    if volume_id != f"{kind}-{sequence:012d}":
        raise RecoveryError("root manifest volume kind is invalid")
    suffix = "tar.age" if kind == "pack" else "bin.age"
    if path != f"volumes/{volume_id}.{suffix}":
        raise RecoveryError("root manifest volume path is not canonical")
    _parse_age_state(value.get("age_state"), plaintext_bytes=plaintext_bytes)
    parts = _parse_parts(value.get("parts"), plaintext_bytes=plaintext_bytes)
    if kind == "pack":
        file_count = _required_nonnegative_int(value, "files")
        source_bytes = _required_nonnegative_int(value, "source_bytes")
        index_sha256 = _required_sha256(value, "index_sha256")
        _required_sha256(value, "plan_sha256")
        if file_count < 1:
            raise RecoveryError("pack volume file count must be positive")
        return VolumeIdentity(
            volume_id,
            sequence,
            kind,
            path,
            plaintext_bytes,
            parts,
            files=file_count,
            source_bytes=source_bytes,
            index_sha256=index_sha256,
        )

    raw_file = value.get("file")
    if not isinstance(raw_file, dict) or set(raw_file) != {
        "path",
        "offset",
        "bytes",
        "file_bytes",
        "sha256",
    }:
        raise RecoveryError("segment volume file mapping is invalid")
    source = FileIdentity(
        path=_normalize_relpath(str(raw_file.get("path", ""))),
        bytes=_required_nonnegative_int(raw_file, "file_bytes"),
        sha256=_required_sha256(raw_file, "sha256"),
    )
    offset = _required_nonnegative_int(raw_file, "offset")
    placement_bytes = _required_nonnegative_int(raw_file, "bytes")
    if placement_bytes != plaintext_bytes or offset + placement_bytes > source.bytes:
        raise RecoveryError("segment volume file range is invalid")
    return VolumeIdentity(
        volume_id,
        sequence,
        kind,
        path,
        plaintext_bytes,
        parts,
        source_file=source,
        file_offset=offset,
    )


def _parse_age_state(value: object, *, plaintext_bytes: int) -> None:
    expected = {"format", "header_b64", "payload_nonce_b64", "plaintext_size"}
    if not isinstance(value, dict) or set(value) != expected:
        raise RecoveryError("volume age state is not a canonical mapping")
    if value.get("format") != "age-v1-scrypt-resumable":
        raise RecoveryError("volume age state format is unsupported")
    if _required_nonnegative_int(value, "plaintext_size") != plaintext_bytes:
        raise RecoveryError("volume age state plaintext size mismatch")
    if not _decode_base64(value.get("header_b64")):
        raise RecoveryError("volume age state header is empty")
    if len(_decode_base64(value.get("payload_nonce_b64"))) != 16:
        raise RecoveryError("volume age state payload nonce is invalid")


def _decode_base64(value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise RecoveryError("volume age state base64 value is invalid")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
    except (binascii.Error, TypeError, ValueError) as exc:
        raise RecoveryError("volume age state base64 value is invalid") from exc
    if base64.b64encode(decoded).decode("ascii").rstrip("=") != value:
        raise RecoveryError("volume age state base64 value is not canonical")
    return decoded


def _parse_parts(value: object, *, plaintext_bytes: int) -> tuple[PartIdentity, ...]:
    if not isinstance(value, list) or not value:
        raise RecoveryError("volume parts must be a non-empty list")
    parts: list[PartIdentity] = []
    expected_start = 0
    for number, raw in enumerate(value, start=1):
        if not isinstance(raw, dict) or set(raw) != {
            "number",
            "plaintext_start",
            "plaintext_bytes",
            "plaintext_sha256",
            "stored_bytes",
            "stored_sha256",
        }:
            raise RecoveryError("volume part is not a canonical mapping")
        part = PartIdentity(
            number=_required_nonnegative_int(raw, "number"),
            plaintext_start=_required_nonnegative_int(raw, "plaintext_start"),
            plaintext_bytes=_required_nonnegative_int(raw, "plaintext_bytes"),
            plaintext_sha256=_required_sha256(raw, "plaintext_sha256"),
            stored_bytes=_required_nonnegative_int(raw, "stored_bytes"),
            stored_sha256=_required_sha256(raw, "stored_sha256"),
        )
        if part.number != number or part.plaintext_start != expected_start:
            raise RecoveryError("volume part order is invalid")
        if part.stored_bytes < 1:
            raise RecoveryError("volume stored part must not be empty")
        expected_start += part.plaintext_bytes
        parts.append(part)
    if expected_start != plaintext_bytes:
        raise RecoveryError("volume parts do not cover its plaintext")
    return tuple(parts)


def _recover_pack(
    plaintext: Path,
    *,
    staging: Path,
    volume: VolumeIdentity,
) -> tuple[FileIdentity, ...]:
    if volume.index_sha256 is None or volume.files is None or volume.source_bytes is None:
        raise RecoveryError("pack volume identity is incomplete")
    with tarfile.open(plaintext, mode="r:") as archive:
        members = archive.getmembers()
        by_name: dict[str, tarfile.TarInfo] = {}
        for member in members:
            name = _normalize_relpath(member.name)
            if name in by_name:
                raise RecoveryError(f"pack contains a duplicate member: {name}")
            by_name[name] = member
        index_info = by_name.get(PACK_INDEX_PATH)
        if (
            index_info is None
            or not index_info.isfile()
            or not members
            or _normalize_relpath(members[-1].name) != PACK_INDEX_PATH
        ):
            raise RecoveryError("pack index member is missing or not final")
        index_source = archive.extractfile(index_info)
        if index_source is None:
            raise RecoveryError("pack index member cannot be read")
        with index_source:
            index_bytes = index_source.read()
        if hashlib.sha256(index_bytes).hexdigest() != volume.index_sha256:
            raise RecoveryError("pack index sha256 mismatch")
        files = _parse_pack_index(index_bytes, volume=volume)
        expected_names = {current.path for current in files}
        reserved_names = {
            name
            for name in by_name
            if name == PACK_INDEX_PATH or name.startswith(PACK_PADDING_PREFIX)
        }
        if set(by_name) != expected_names | reserved_names:
            raise RecoveryError("pack members do not exactly match its index")
        for name in sorted(reserved_names - {PACK_INDEX_PATH}):
            _verify_padding_member(archive, by_name[name], name=name)
        for current in files:
            info = by_name[current.path]
            if (
                not info.isfile()
                or info.size != current.bytes
                or info.offset != current.header_offset
                or info.offset_data != current.data_offset
            ):
                raise RecoveryError(f"pack member identity mismatch: {current.path}")
            source = archive.extractfile(info)
            if source is None:
                raise RecoveryError(f"pack member cannot be read: {current.path}")
            destination = _output_file(staging, current.path)
            if destination.exists():
                raise RecoveryError(f"archive repeats a logical file: {current.path}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            byte_count = 0
            with source, destination.open("xb") as target:
                while chunk := source.read(1024 * 1024):
                    byte_count += len(chunk)
                    digest.update(chunk)
                    target.write(chunk)
            if byte_count != current.bytes or digest.hexdigest() != current.sha256:
                raise RecoveryError(f"pack member verification failed: {current.path}")
        return tuple(FileIdentity(current.path, current.bytes, current.sha256) for current in files)


def _parse_pack_index(
    content: bytes,
    *,
    volume: VolumeIdentity,
) -> tuple[PackFileIdentity, ...]:
    payload = json.loads(content)
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != PACK_INDEX_SCHEMA
        or set(payload) != {"schema", "volume", "tree", "files"}
    ):
        raise RecoveryError("pack index schema is invalid")
    volume_row = payload.get("volume")
    tree = payload.get("tree")
    raw_files = payload.get("files")
    if (
        not isinstance(volume_row, dict)
        or set(volume_row) != {"id", "sequence"}
        or not isinstance(tree, dict)
        or set(tree) != {"files", "bytes", "sha256"}
        or not isinstance(raw_files, list)
    ):
        raise RecoveryError("pack index structure is invalid")
    if volume_row.get("id") != volume.id:
        raise RecoveryError("pack index volume id mismatch")
    if _required_nonnegative_int(volume_row, "sequence") != volume.sequence:
        raise RecoveryError("pack index volume sequence mismatch")

    files: list[PackFileIdentity] = []
    seen: set[str] = set()
    tree_digest = hashlib.sha256()
    total_bytes = 0
    previous_data_offset = -1
    previous_unit = -1
    for raw in raw_files:
        if not isinstance(raw, dict) or set(raw) != {
            "path",
            "bytes",
            "sha256",
            "unit",
            "header_offset",
            "data_offset",
        }:
            raise RecoveryError("pack index file is invalid")
        current = PackFileIdentity(
            path=_normalize_relpath(str(raw.get("path", ""))),
            bytes=_required_nonnegative_int(raw, "bytes"),
            sha256=_required_sha256(raw, "sha256"),
            header_offset=_required_nonnegative_int(raw, "header_offset"),
            data_offset=_required_nonnegative_int(raw, "data_offset"),
        )
        unit = _required_nonnegative_int(raw, "unit")
        if current.path.startswith(".riverhog/") or current.path in seen:
            raise RecoveryError("pack index file path is invalid")
        if (
            current.data_offset <= current.header_offset
            or current.data_offset <= previous_data_offset
            or unit < previous_unit
            or unit > previous_unit + 1
            or (previous_unit < 0 and unit != 0)
        ):
            raise RecoveryError("pack index file offsets or units are invalid")
        previous_data_offset = current.data_offset
        previous_unit = unit
        seen.add(current.path)
        files.append(current)
        total_bytes += current.bytes
        tree_digest.update(f"{current.path}\t{current.bytes}\t{current.sha256}\n".encode())
    if volume.files != len(files) or volume.source_bytes != total_bytes:
        raise RecoveryError("pack index counts do not match the root manifest")
    if _required_nonnegative_int(tree, "files") != len(files):
        raise RecoveryError("pack index tree file count mismatch")
    if _required_nonnegative_int(tree, "bytes") != total_bytes:
        raise RecoveryError("pack index tree byte count mismatch")
    if _required_sha256(tree, "sha256") != tree_digest.hexdigest():
        raise RecoveryError("pack index tree sha256 mismatch")
    return tuple(files)


def _verify_padding_member(
    archive: tarfile.TarFile,
    info: tarfile.TarInfo,
    *,
    name: str,
) -> None:
    if _PADDING_PATH_RE.fullmatch(name) is None or not info.isfile() or info.size >= 64 * 1024:
        raise RecoveryError(f"pack padding member is invalid: {name}")
    source = archive.extractfile(info)
    if source is None:
        raise RecoveryError(f"pack padding member cannot be read: {name}")
    with source:
        while chunk := source.read(1024 * 1024):
            if any(chunk):
                raise RecoveryError(f"pack padding member is not zero-filled: {name}")


def _recover_segment(
    plaintext: Path,
    *,
    staging: Path,
    file: FileIdentity,
    offset: int,
) -> None:
    destination = _output_file(staging, file.path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size != offset:
        raise RecoveryError(f"file segments are out of order: {file.path}")
    if not destination.exists() and offset != 0:
        raise RecoveryError(f"file begins with a nonzero segment offset: {file.path}")
    mode = "ab" if destination.exists() else "xb"
    with plaintext.open("rb") as source, destination.open(mode) as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)


def _verify_stored_parts(path: Path, parts: Sequence[PartIdentity]) -> None:
    if path.stat().st_size != sum(current.stored_bytes for current in parts):
        raise RecoveryError(f"stored volume byte count mismatch: {path.name}")
    with path.open("rb") as source:
        for current in parts:
            digest = hashlib.sha256()
            remaining = current.stored_bytes
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RecoveryError(f"stored volume ended during part {current.number}")
                remaining -= len(chunk)
                digest.update(chunk)
            if digest.hexdigest() != current.stored_sha256:
                raise RecoveryError(f"stored volume part sha256 mismatch: {path.name}")
        if source.read(1):
            raise RecoveryError(f"stored volume has trailing bytes: {path.name}")


def _verify_plaintext_parts(path: Path, volume: VolumeIdentity) -> None:
    if path.stat().st_size != volume.plaintext_bytes:
        raise RecoveryError(f"plaintext volume byte count mismatch: {volume.id}")
    with path.open("rb") as source:
        for current in volume.parts:
            digest = hashlib.sha256()
            remaining = current.plaintext_bytes
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RecoveryError(f"plaintext volume ended during part {current.number}")
                remaining -= len(chunk)
                digest.update(chunk)
            if digest.hexdigest() != current.plaintext_sha256:
                raise RecoveryError(f"plaintext volume part sha256 mismatch: {volume.id}")
        if source.read(1):
            raise RecoveryError(f"plaintext volume has trailing bytes: {volume.id}")


def _validate_raw_ranges(
    files: Mapping[str, FileIdentity],
    ranges: Mapping[str, list[tuple[int, int]]],
) -> None:
    for path, current_ranges in ranges.items():
        expected = files[path]
        offset = 0
        for start, byte_count in sorted(current_ranges):
            if start != offset:
                raise RecoveryError(f"raw file segments are not contiguous: {path}")
            offset += byte_count
        if offset != expected.bytes:
            raise RecoveryError(f"raw file segments do not cover the file: {path}")


def _verify_file(path: Path, expected: FileIdentity) -> None:
    if not path.is_file():
        raise RecoveryError(f"recovered file is missing: {expected.path}")
    if path.stat().st_size != expected.bytes or _sha256(path) != expected.sha256:
        raise RecoveryError(f"recovered file verification failed: {expected.path}")


def _tree_identity(files: Sequence[FileIdentity]) -> dict[str, object]:
    digest = hashlib.sha256()
    byte_count = 0
    for current in sorted(files, key=lambda value: value.path):
        digest.update(f"{current.path}\t{current.bytes}\t{current.sha256}\n".encode())
        byte_count += current.bytes
    return {"files": len(files), "bytes": byte_count, "sha256": digest.hexdigest()}


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


def _verify_inventory_file(
    path: Path,
    relative: str,
    checksums: Mapping[str, str] | None,
) -> None:
    if checksums is None:
        return
    expected = checksums.get(relative)
    if expected is None:
        raise RecoveryError(f"SHA256SUMS does not cover {relative}")
    if _sha256(path) != expected:
        raise RecoveryError(f"ciphertext checksum mismatch: {relative}")


def _verify_attestation_inventory(
    checksums: Mapping[str, str] | None,
    manifest: Manifest,
) -> None:
    if checksums is None:
        return
    expected = {RECOVERY_DESCRIPTOR_PATH, "manifest.json.age", "manifest.json.ots.age"}
    if manifest.provenance is not None:
        expected.update(
            current.path for current in (manifest.provenance.index, *manifest.provenance.bundles)
        )
    if set(checksums) != expected:
        raise RecoveryError("SHA256SUMS does not match the immutable root inventory")


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


def _verify_timestamp(manifest: Path, proof: Path, *, command: str) -> None:
    try:
        completed = subprocess.run(
            [command, "verify", str(proof), "-f", str(manifest)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RecoveryError(f"cannot run OpenTimestamps: {exc}") from exc
    if completed.returncode != 0 and not _is_nonfatal_timestamp_status(
        completed.stdout,
        completed.stderr,
    ):
        message = completed.stderr.strip() or completed.stdout.strip()
        raise RecoveryError(message or "OpenTimestamps verification failed")


def _is_nonfatal_timestamp_status(stdout: str, stderr: str) -> bool:
    lines = [
        line.strip() for output in (stdout, stderr) for line in output.splitlines() if line.strip()
    ]

    def pending(line: str) -> bool:
        return line.startswith("Calendar ") and line.endswith(
            ": Pending confirmation in Bitcoin blockchain"
        )

    def awaiting_confirmations(line: str) -> bool:
        return bool(
            re.fullmatch(
                r"Calendar \S+: Timestamped by transaction [0-9a-f]{64}; "
                r"waiting for [1-9][0-9]* confirmations",
                line,
            )
        )

    def ignored_calendar(line: str) -> bool:
        return line.startswith("Ignoring attestation from calendar ") and line.endswith(
            ": Calendar not in whitelist"
        )

    def bitcoin_disabled(line: str) -> bool:
        return line == "Not checking Bitcoin attestation; Bitcoin disabled"

    def manual_check(line: str) -> bool:
        return (
            line.startswith("To verify manually, check that Bitcoin block ")
            and " has merkleroot " in line
        )

    allowed = all(
        pending(line)
        or awaiting_confirmations(line)
        or line.startswith("Got ")
        and " attestation(s) from " in line
        or ignored_calendar(line)
        or bitcoin_disabled(line)
        or manual_check(line)
        for line in lines
    )
    deferred = any(
        pending(line)
        or awaiting_confirmations(line)
        or ignored_calendar(line)
        or bitcoin_disabled(line)
        for line in lines
    )
    described = not any(bitcoin_disabled(line) for line in lines) or any(
        manual_check(line) for line in lines
    )
    return bool(lines) and allowed and deferred and described


def _windows_host() -> bool:
    return os.name == "nt"


def _age_decrypt(source: Path, destination: Path, *, passphrase: str, command: str) -> None:
    destination.unlink(missing_ok=True)
    env = os.environ.copy()
    env.pop("AGE_PASSPHRASE", None)
    env.pop("AGE_PASSPHRASE_FD", None)
    read_fd: int | None = None
    pass_fds: tuple[int, ...] = ()
    if _windows_host():
        env["AGE_PASSPHRASE"] = passphrase
    else:
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, passphrase.encode("utf-8"))
        finally:
            os.close(write_fd)
        env["AGE_PASSPHRASE_FD"] = str(read_fd)
        pass_fds = (read_fd,)
    try:
        try:
            completed = subprocess.run(
                [command, "--decrypt", "-j", "batchpass", "-o", str(destination), str(source)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                pass_fds=pass_fds,
            )
        except OSError as exc:
            raise RecoveryError(f"cannot run age: {exc}") from exc
    finally:
        if read_fd is not None:
            os.close(read_fd)
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise RecoveryError(message or f"age decryption failed: {source.name}")
    if not destination.is_file():
        raise RecoveryError(f"age produced no plaintext for {source.name}")


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


def _required_nonnegative_int(value: Mapping[str, Any], key: str) -> int:
    current = value.get(key)
    if isinstance(current, bool) or not isinstance(current, int) or current < 0:
        raise RecoveryError(f"{key} must be a non-negative integer")
    return current


def _required_sha256(value: Mapping[str, Any], key: str) -> str:
    current = value.get(key)
    if not isinstance(current, str) or _SHA256_RE.fullmatch(current) is None:
        raise RecoveryError(f"{key} is not a sha256 digest")
    return current


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["RecoveryError", "RecoverySummary", "recover_archive"]
