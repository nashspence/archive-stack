from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from riverhog_archive_contracts import (
    PACK_INDEX_SCHEMA,
    RECOVERY_DESCRIPTOR_PATH,
    ArchiveProvenanceIdentity,
    CollectionArchiveManifest,
    CollectionArchiveTerminalDocument,
    CollectionArchiveVolumeDocument,
    PackArchiveVolume,
    RecoveryDescriptor,
    RecoveryDescriptorError,
    SegmentArchiveVolume,
    StoredPartIdentity,
    format_archive_sequence,
    parse_archive_sequence,
    update_archive_sequence_commitment,
)
from riverhog_archive_contracts import (
    ArchiveFileIdentity as FileIdentity,
)
from riverhog_protocol import (
    COLLECTION_DESCRIPTION_RELATIVE_PATH,
    CollectionDescriptionDocument,
)
from riverhog_provenance import (
    PROVENANCE_JOURNAL_ENTRY_BYTES_MAX,
    ProvenanceTerminalDocument,
    ProvenanceVolumeDocument,
    format_provenance_sequence,
    parse_provenance_sequence,
    resolve_incremental_journal_current_state,
    update_ordered_volume_commitment,
    validate_incremental_journal_entry,
)

from ._checkpoint_sha256 import CheckpointSHA256
from ._provenance import (
    parse_segmented_binding_payload,
    parse_segmented_provenance_root,
    parse_segmented_provenance_terminal,
    parse_segmented_provenance_volume,
)

PACK_INDEX_PATH = ".riverhog/pack-index.json"
PACK_PADDING_PREFIX = ".riverhog/padding/"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PADDING_PATH_RE = re.compile(r"\.riverhog/padding/pack-[0-9a-f]{64}-[0-9]{6}")


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
class PackFileIdentity:
    path: str
    bytes: int
    sha256: str
    header_offset: int
    data_offset: int


@dataclass(frozen=True, slots=True)
class _RecoveredJournalCurrent:
    journal_id: str
    state_id: str
    path: str
    bytes: int
    sha256: str


def recover_collection_description(
    archive_dir: Path,
    *,
    passphrases: Mapping[str, str],
    age_command: str = "age",
) -> CollectionDescriptionDocument | None:
    """Read and validate only the small mutable description authority, when present."""

    archive = archive_dir.expanduser().resolve()
    if not archive.is_dir():
        raise RecoveryError(f"archive directory does not exist: {archive}")
    descriptor = read_recovery_descriptor(archive)
    try:
        passphrase = passphrases[descriptor.encryption.passphrase_id]
    except KeyError as exc:
        raise RecoveryError(
            f"no passphrase is available for archive key ID {descriptor.encryption.passphrase_id}"
        ) from exc
    if not isinstance(passphrase, str) or not passphrase:
        raise RecoveryError(
            f"archive passphrase is empty for key ID {descriptor.encryption.passphrase_id}"
        )
    encrypted_description = archive / COLLECTION_DESCRIPTION_RELATIVE_PATH
    if not encrypted_description.exists():
        return None
    if not encrypted_description.is_file() or encrypted_description.is_symlink():
        raise RecoveryError("collection description path is not a regular archive object")
    try:
        encrypted_manifest = _archive_file(archive, descriptor.root.path)
        _verify_stored_identity(
            encrypted_manifest,
            expected_bytes=descriptor.root.stored_bytes,
            expected_sha256=descriptor.root.stored_sha256,
            label=descriptor.root.path,
        )
        with tempfile.TemporaryDirectory(prefix="riverhog-description-") as scratch_name:
            scratch = Path(scratch_name)
            manifest_path = scratch / "manifest.json"
            description_path = scratch / "description.json"
            _age_decrypt(
                encrypted_manifest,
                manifest_path,
                passphrase=passphrase,
                command=age_command,
            )
            manifest_bytes = manifest_path.read_bytes()
            CollectionArchiveManifest.from_json_bytes(manifest_bytes)
            archive_root_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            _age_decrypt(
                encrypted_description,
                description_path,
                passphrase=passphrase,
                command=age_command,
            )
            document = CollectionDescriptionDocument.from_json_bytes(description_path.read_bytes())
        if document.archive_root_sha256 != archive_root_sha256:
            raise RecoveryError("collection description belongs to another archive root")
        return document
    except RecoveryError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise RecoveryError(str(exc)) from exc


def recover_archive(
    archive_dir: Path,
    output_dir: Path,
    *,
    passphrases: Mapping[str, str],
    age_command: str = "age",
) -> RecoverySummary:
    archive = archive_dir.expanduser().resolve()
    output = output_dir.expanduser().absolute()
    if not archive.is_dir():
        raise RecoveryError(f"archive directory does not exist: {archive}")
    if output.exists():
        raise RecoveryError(f"output path already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = output.parent / f".{output.name}.riverhog-recovery"
    staging = scratch / "output"
    scratch.mkdir(mode=0o700, exist_ok=True)
    staging.mkdir(exist_ok=True)
    state = sqlite3.connect(scratch / "state.sqlite3")
    _initialize_recovery_state(state)
    completed = False
    try:
        descriptor = read_recovery_descriptor(archive)
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

        manifest_path = scratch / "manifest.json"
        _age_decrypt(
            encrypted_manifest,
            manifest_path,
            passphrase=passphrase,
            command=age_command,
        )

        manifest = CollectionArchiveManifest.from_json_bytes(manifest_path.read_bytes())
        archive_root_sha256 = hashlib.sha256(manifest.to_json_bytes()).hexdigest()
        _bind_recovery_authority(
            state,
            archive=archive,
            root_stored_sha256=descriptor.root.stored_sha256,
            archive_root_sha256=archive_root_sha256,
        )

        progress = state.execute(
            "SELECT next_sequence, hash_state FROM sequence_progress WHERE name = 'archive'"
        ).fetchone()
        if progress is None:
            volume_next = 0
            volume_set_digest = CheckpointSHA256()
            state.execute(
                "INSERT INTO sequence_progress(name, next_sequence, hash_state) VALUES (?, ?, ?)",
                ("archive", format_archive_sequence(0), volume_set_digest.export_state()),
            )
            state.commit()
        else:
            volume_next = parse_archive_sequence(progress[0], "recovery checkpoint sequence")
            volume_set_digest = CheckpointSHA256.from_state(str(progress[1]))
        terminal_row = state.execute(
            "SELECT value FROM authority WHERE key = 'archive_terminal_sequence'"
        ).fetchone()
        archive_volume_count = (
            parse_archive_sequence(terminal_row[0], "recovery terminal sequence")
            if terminal_row is not None
            else None
        )
        sequence = volume_next
        while archive_volume_count is None:
            sequence_token = format_archive_sequence(sequence)
            metadata_relative = f"metadata/volume-{sequence_token}.json.age"
            encrypted_metadata = _archive_file(archive, metadata_relative)
            metadata_path = scratch / f"volume-{sequence_token}.json"
            _age_decrypt(
                encrypted_metadata,
                metadata_path,
                passphrase=passphrase,
                command=age_command,
            )
            metadata_bytes = metadata_path.read_bytes()
            try:
                metadata_value = json.loads(metadata_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RecoveryError("archive sequence metadata is not valid JSON") from exc
            if not isinstance(metadata_value, dict):
                raise RecoveryError("archive sequence metadata is not an object")
            if metadata_value.get("schema") == "collection-archive-terminal/v1":
                terminal = CollectionArchiveTerminalDocument.from_json_bytes(metadata_bytes)
                if (
                    terminal.archive_generation != manifest.archive_generation
                    or terminal.archive_tree_sha256 != manifest.tree_sha256
                    or terminal.sequence != sequence
                ):
                    raise RecoveryError("archive terminal sequence is not canonical")
                update_archive_sequence_commitment(volume_set_digest, terminal)
                metadata_path.unlink()
                state.execute(
                    "INSERT INTO authority(key, value) VALUES (?, ?)",
                    ("archive_terminal_sequence", format_archive_sequence(sequence)),
                )
                state.execute(
                    "UPDATE sequence_progress SET next_sequence = ?, hash_state = ? "
                    "WHERE name = 'archive'",
                    (format_archive_sequence(sequence), volume_set_digest.export_state()),
                )
                state.commit()
                archive_volume_count = sequence
                break
            document = CollectionArchiveVolumeDocument.from_json_bytes(metadata_bytes)
            volume = document.volume
            if (
                document.archive_generation != manifest.archive_generation
                or document.archive_tree_sha256 != manifest.tree_sha256
                or volume.sequence != sequence
            ):
                raise RecoveryError("archive volume metadata sequence is not canonical")
            update_archive_sequence_commitment(volume_set_digest, document)
            metadata_path.unlink()
            encrypted = _archive_file(archive, volume.path)
            _verify_stored_parts(encrypted, volume.parts)
            plaintext = scratch / f"{volume.id}.plaintext"
            _age_decrypt(encrypted, plaintext, passphrase=passphrase, command=age_command)
            _verify_plaintext_parts(plaintext, volume)
            if isinstance(volume, PackArchiveVolume):
                recovered = _recover_pack(
                    plaintext,
                    staging=staging,
                    volume=volume,
                    state=state,
                )
                for current in recovered:
                    _record_recovered_file(
                        state,
                        current,
                        volume_sequence=sequence,
                        kind="pack",
                    )
            elif isinstance(volume, SegmentArchiveVolume):
                current = volume.source_file
                _recover_segment(
                    plaintext,
                    staging=staging,
                    file=current,
                    offset=volume.file_offset,
                )
                _record_recovered_file(
                    state,
                    current,
                    volume_sequence=sequence,
                    kind="segment",
                )
                state.execute(
                    "INSERT OR IGNORE INTO raw_ranges(path, volume_sequence, offset, bytes) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        current.path,
                        format_archive_sequence(sequence),
                        volume.file_offset,
                        volume.plaintext_bytes,
                    ),
                )
            else:  # pragma: no cover - closed public union
                raise RecoveryError("archive volume kind is unsupported")
            plaintext.unlink()
            state.execute(
                "INSERT INTO volumes(sequence, metadata_sha256) VALUES (?, ?)",
                (
                    format_archive_sequence(sequence),
                    hashlib.sha256(metadata_bytes).hexdigest(),
                ),
            )
            state.execute(
                "UPDATE sequence_progress SET next_sequence = ?, hash_state = ? "
                "WHERE name = 'archive'",
                (format_archive_sequence(sequence + 1), volume_set_digest.export_state()),
            )
            state.commit()
            sequence += 1

        if volume_set_digest.hexdigest() != manifest.ordered_volume_sha256:
            raise RecoveryError("archive volume metadata set does not match the root manifest")

        _validate_raw_ranges_state(state)
        tree = _verify_recovered_tree(state, staging=staging)
        if (
            tree["files"] != manifest.files
            or tree["bytes"] != manifest.bytes
            or tree["sha256"] != manifest.tree_sha256
        ):
            raise RecoveryError("recovered collection tree does not match the root manifest")
        provenance_mode = "omitted"
        provenance_journals = 0
        if manifest.provenance is not None:
            provenance_mode, provenance_journals = _recover_provenance(
                archive,
                scratch=scratch,
                staging=staging,
                descriptor=manifest.provenance,
                archive_generation=manifest.archive_generation,
                expected_files=state,
                expected_tree_sha256=manifest.tree_sha256,
                passphrase=passphrase,
                age_command=age_command,
            )
        os.replace(staging, output)
        completed = True
        return RecoverySummary(
            output=output,
            files=manifest.files,
            bytes=manifest.bytes,
            volumes=archive_volume_count,
            provenance_mode=provenance_mode,
            provenance_journals=provenance_journals,
        )
    except RecoveryError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, tarfile.TarError) as exc:
        raise RecoveryError(str(exc)) from exc
    finally:
        state.close()
        if completed:
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


def _initialize_recovery_state(state: sqlite3.Connection) -> None:
    state.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS authority (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS volumes (
            sequence TEXT PRIMARY KEY
                CHECK(length(sequence) = 64 AND sequence = lower(sequence)
                    AND sequence NOT GLOB '*[^0-9a-f]*'),
            metadata_sha256 TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sequence_progress (
            name TEXT PRIMARY KEY,
            next_sequence TEXT NOT NULL
                CHECK(length(next_sequence) = 64 AND next_sequence = lower(next_sequence)
                    AND next_sequence NOT GLOB '*[^0-9a-f]*'),
            hash_state TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            kind TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS file_volumes (
            path TEXT NOT NULL,
            volume_sequence TEXT NOT NULL
                CHECK(length(volume_sequence) = 64 AND volume_sequence = lower(volume_sequence)
                    AND volume_sequence NOT GLOB '*[^0-9a-f]*'),
            kind TEXT NOT NULL,
            PRIMARY KEY (path, volume_sequence),
            FOREIGN KEY (path) REFERENCES files(path) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS raw_ranges (
            path TEXT NOT NULL,
            volume_sequence TEXT NOT NULL
                CHECK(length(volume_sequence) = 64 AND volume_sequence = lower(volume_sequence)
                    AND volume_sequence NOT GLOB '*[^0-9a-f]*'),
            offset INTEGER NOT NULL,
            bytes INTEGER NOT NULL,
            PRIMARY KEY (path, volume_sequence),
            FOREIGN KEY (path) REFERENCES files(path) ON DELETE CASCADE
        );
        """
    )
    state.commit()


def _initialize_provenance_recovery_state(state: sqlite3.Connection) -> None:
    state.executescript(
        """
        CREATE TABLE IF NOT EXISTS bindings (
            path TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            journal_id TEXT,
            current_state_id TEXT
        );
        CREATE TABLE IF NOT EXISTS entries (
            journal_id TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            PRIMARY KEY (journal_id, entry_id)
        );
        CREATE TABLE IF NOT EXISTS states (
            journal_id TEXT NOT NULL,
            state_id TEXT NOT NULL,
            document_json TEXT NOT NULL,
            PRIMARY KEY (journal_id, state_id)
        );
        CREATE TABLE IF NOT EXISTS journal_progress (
            journal_id TEXT PRIMARY KEY,
            next_sequence INTEGER NOT NULL,
            validated_offset INTEGER NOT NULL,
            previous_entry_id TEXT,
            previous_json_sha256 TEXT,
            primary_lineage_id TEXT,
            current_binding_json TEXT
        );
        CREATE TABLE IF NOT EXISTS external_states (
            from_journal_id TEXT NOT NULL,
            journal_id TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            entry_sha256 TEXT NOT NULL,
            state_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS provenance_progress (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            next_volume TEXT NOT NULL
                CHECK(length(next_volume) = 64 AND next_volume = lower(next_volume)
                    AND next_volume NOT GLOB '*[^0-9a-f]*'),
            hash_state TEXT NOT NULL,
            next_file_order INTEGER NOT NULL,
            previous_path TEXT,
            previous_journal_id TEXT,
            current_journal_id TEXT,
            current_journal_bytes INTEGER NOT NULL,
            current_journal_sha256 TEXT NOT NULL,
            journal_count INTEGER NOT NULL,
            omitted INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS provenance_terminal (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            sequence TEXT NOT NULL
                CHECK(length(sequence) = 64 AND sequence = lower(sequence)
                    AND sequence NOT GLOB '*[^0-9a-f]*')
        );
        """
    )
    state.commit()


def _bind_recovery_authority(
    state: sqlite3.Connection,
    *,
    archive: Path,
    root_stored_sha256: str,
    archive_root_sha256: str,
) -> None:
    expected = {
        "archive": str(archive),
        "root_stored_sha256": root_stored_sha256,
        "archive_root_sha256": archive_root_sha256,
    }
    existing = {
        key: value
        for key, value in state.execute(
            "SELECT key, value FROM authority "
            "WHERE key IN ('archive','root_stored_sha256','archive_root_sha256')"
        )
    }
    if existing and existing != expected:
        raise RecoveryError("recovery checkpoint belongs to a different archive authority")
    state.executemany(
        "INSERT OR IGNORE INTO authority(key, value) VALUES (?, ?)",
        sorted(expected.items()),
    )
    state.commit()


def _record_recovered_file(
    state: sqlite3.Connection,
    file: FileIdentity,
    *,
    volume_sequence: int,
    kind: str,
) -> None:
    existing = state.execute(
        "SELECT bytes, sha256, kind FROM files WHERE path = ?",
        (file.path,),
    ).fetchone()
    if existing is None:
        state.execute(
            "INSERT INTO files(path, bytes, sha256, kind) VALUES (?, ?, ?, ?)",
            (file.path, file.bytes, file.sha256, kind),
        )
    elif (int(existing[0]), str(existing[1])) != (file.bytes, file.sha256):
        raise RecoveryError(f"archive disagrees about a logical file: {file.path}")
    elif str(existing[2]) != kind or kind == "pack":
        prior = state.execute(
            "SELECT 1 FROM file_volumes WHERE path = ? AND volume_sequence != ? LIMIT 1",
            (file.path, format_archive_sequence(volume_sequence)),
        ).fetchone()
        if prior is not None:
            raise RecoveryError(f"archive repeats a logical file: {file.path}")
    state.execute(
        "INSERT OR IGNORE INTO file_volumes(path, volume_sequence, kind) VALUES (?, ?, ?)",
        (file.path, format_archive_sequence(volume_sequence), kind),
    )


def _expected_file(state: sqlite3.Connection, path: str) -> FileIdentity | None:
    row = state.execute(
        "SELECT bytes, sha256 FROM files WHERE path = ?",
        (path,),
    ).fetchone()
    if row is None:
        return None
    return FileIdentity(path=path, bytes=int(row[0]), sha256=str(row[1]))


def _append_exact_segment(path: Path, *, offset: int, content: bytes) -> None:
    size = path.stat().st_size if path.exists() else 0
    if size == offset + len(content):
        with path.open("rb") as source:
            source.seek(offset)
            if source.read(len(content)) == content:
                return
        with path.open("r+b") as target:
            target.truncate(offset)
        size = offset
    elif offset < size < offset + len(content):
        with path.open("r+b") as target:
            target.truncate(offset)
        size = offset
    if size != offset:
        raise RecoveryError("provenance journal segments are not contiguous")
    with path.open("ab" if path.exists() else "xb") as destination:
        destination.write(content)


def _save_provenance_progress(
    state: sqlite3.Connection,
    *,
    next_volume: int,
    volume_digest: CheckpointSHA256,
    next_file_order: int,
    previous_path: bytes | None,
    previous_journal_id: str | None,
    current_journal_id: str | None,
    current_journal_bytes: int,
    current_journal_sha256: str,
    journal_count: int,
    omitted: int,
    terminal_sequence: int | None = None,
) -> None:
    state.execute(
        "UPDATE provenance_progress SET next_volume = ?, hash_state = ?, "
        "next_file_order = ?, previous_path = ?, previous_journal_id = ?, "
        "current_journal_id = ?, current_journal_bytes = ?, "
        "current_journal_sha256 = ?, journal_count = ?, omitted = ? "
        "WHERE singleton = 1",
        (
            format_provenance_sequence(next_volume),
            volume_digest.export_state(),
            next_file_order,
            previous_path.decode("utf-8") if previous_path is not None else None,
            previous_journal_id,
            current_journal_id,
            current_journal_bytes if current_journal_id is not None else 0,
            current_journal_sha256 if current_journal_id is not None else "",
            journal_count,
            omitted,
        ),
    )
    if terminal_sequence is not None:
        state.execute(
            "INSERT INTO provenance_terminal(singleton, sequence) VALUES (1, ?)",
            (format_provenance_sequence(terminal_sequence),),
        )
    state.commit()


def _advance_recovered_journal_validation(
    state: sqlite3.Connection,
    *,
    path: Path,
    journal_id: str,
    complete: bool,
) -> _RecoveredJournalCurrent | None:
    row = state.execute(
        "SELECT next_sequence, validated_offset, previous_entry_id, "
        "previous_json_sha256, primary_lineage_id, current_binding_json "
        "FROM journal_progress WHERE journal_id = ?",
        (journal_id,),
    ).fetchone()
    if row is None:
        state.execute(
            "INSERT INTO journal_progress VALUES (?, 0, 0, NULL, NULL, NULL, NULL)",
            (journal_id,),
        )
        state.commit()
        next_sequence = 0
        validated_offset = 0
        previous_entry_id: str | None = None
        previous_json_sha256: str | None = None
        primary_lineage_id: str | None = None
        current_binding_json: str | None = None
    else:
        next_sequence = int(row[0])
        validated_offset = int(row[1])
        previous_entry_id = str(row[2]) if row[2] is not None else None
        previous_json_sha256 = str(row[3]) if row[3] is not None else None
        primary_lineage_id = str(row[4]) if row[4] is not None else None
        current_binding_json = str(row[5]) if row[5] is not None else None

    with path.open("rb") as source:
        source.seek(validated_offset)
        while source.tell() < path.stat().st_size:
            entry_offset = source.tell()
            if source.read(1) != b"\x1e":
                raise RecoveryError("provenance journal entry has no record separator")
            line = source.readline(PROVENANCE_JOURNAL_ENTRY_BYTES_MAX + 2)
            if not line.endswith(b"\n"):
                if complete or len(line) > PROVENANCE_JOURNAL_ENTRY_BYTES_MAX:
                    raise RecoveryError("provenance journal entry exceeds its bounded contract")
                source.seek(entry_offset)
                break
            encoded = b"\x1e" + line
            try:
                projected = validate_incremental_journal_entry(
                    encoded,
                    sequence=next_sequence,
                    journal_id=journal_id,
                    previous_entry_id=previous_entry_id,
                    previous_json_sha256=previous_json_sha256,
                )
            except ValueError as exc:
                raise RecoveryError(f"provenance journal validation failed: {exc}") from exc
            if projected.primary_lineage_id is not None:
                if primary_lineage_id is not None:
                    raise RecoveryError("provenance journal repeats its lineage authority")
                primary_lineage_id = projected.primary_lineage_id
            state.execute(
                "INSERT INTO entries VALUES (?, ?, ?)",
                (journal_id, str(projected.frame.document["id"]), projected.frame.sha256),
            )
            for state_id, document_json in projected.states:
                existing = state.execute(
                    "SELECT document_json FROM states WHERE journal_id = ? AND state_id = ?",
                    (journal_id, state_id),
                ).fetchone()
                if existing is not None and str(existing[0]) != document_json:
                    raise RecoveryError("provenance journal redefines a state")
                state.execute(
                    "INSERT OR IGNORE INTO states VALUES (?, ?, ?)",
                    (journal_id, state_id, document_json),
                )
            for role, operation, document_json in projected.bindings:
                if role != "co_resident_primary_payload":
                    continue
                current_binding_json = document_json if operation == "bind" else None
            state.executemany(
                "INSERT INTO external_states VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        journal_id,
                        reference.journal_id,
                        reference.entry_id,
                        reference.entry_json_sha256,
                        reference.state_id,
                    )
                    for reference in projected.external_states
                ),
            )
            next_sequence += 1
            validated_offset = source.tell()
            previous_entry_id = str(projected.frame.document["id"])
            previous_json_sha256 = projected.frame.sha256
            state.execute(
                "UPDATE journal_progress SET next_sequence = ?, validated_offset = ?, "
                "previous_entry_id = ?, previous_json_sha256 = ?, primary_lineage_id = ?, "
                "current_binding_json = ? WHERE journal_id = ?",
                (
                    next_sequence,
                    validated_offset,
                    previous_entry_id,
                    previous_json_sha256,
                    primary_lineage_id,
                    current_binding_json,
                    journal_id,
                ),
            )
            state.commit()

    if not complete:
        return None
    if validated_offset != path.stat().st_size or next_sequence < 1:
        raise RecoveryError("provenance journal ends with an incomplete entry")
    if primary_lineage_id is None or current_binding_json is None:
        raise RecoveryError("provenance journal has no current primary binding")
    try:
        binding = json.loads(current_binding_json)
        reference = binding.get("state") if isinstance(binding, dict) else None
        state_id = str(reference.get("id")) if isinstance(reference, dict) else ""
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecoveryError("provenance current binding is invalid") from exc
    state_row = state.execute(
        "SELECT document_json FROM states WHERE journal_id = ? AND state_id = ?",
        (journal_id, state_id),
    ).fetchone()
    if state_row is None:
        raise RecoveryError("provenance current binding references an unknown state")
    try:
        resolved_state_id, current_path, current_bytes, current_sha256 = (
            resolve_incremental_journal_current_state(
                primary_lineage_id=primary_lineage_id,
                binding_json=current_binding_json,
                state_json=str(state_row[0]),
            )
        )
    except ValueError as exc:
        raise RecoveryError(f"provenance current state validation failed: {exc}") from exc
    return _RecoveredJournalCurrent(
        journal_id=journal_id,
        state_id=resolved_state_id,
        path=current_path,
        bytes=current_bytes,
        sha256=current_sha256,
    )


def _verify_stored_identity(
    path: Path,
    *,
    expected_bytes: int,
    expected_sha256: str,
    label: str,
) -> None:
    if path.stat().st_size != expected_bytes or _sha256(path) != expected_sha256:
        raise RecoveryError(f"stored archive object does not match recovery descriptor: {label}")


def _recover_provenance(
    archive: Path,
    *,
    scratch: Path,
    staging: Path,
    descriptor: ArchiveProvenanceIdentity,
    archive_generation: str,
    expected_files: sqlite3.Connection,
    expected_tree_sha256: str,
    passphrase: str,
    age_command: str,
) -> tuple[str, int]:
    encrypted_root = _archive_file(archive, descriptor.root.path)
    _verify_stored_identity(
        encrypted_root,
        expected_bytes=descriptor.root.stored_bytes,
        expected_sha256=descriptor.root.stored_sha256,
        label=descriptor.root.path,
    )
    root_plaintext = scratch / "provenance-root.json"
    _age_decrypt(
        encrypted_root,
        root_plaintext,
        passphrase=passphrase,
        command=age_command,
    )
    root_bytes = root_plaintext.read_bytes()
    root = parse_segmented_provenance_root(root_bytes)
    if root.identity != descriptor.identity:
        raise RecoveryError("provenance root identity changed during recovery")
    if (
        root.archive_generation != archive_generation
        or root.archive_tree_sha256 != expected_tree_sha256
    ):
        raise RecoveryError("provenance root names a different collection tree")
    output_root = staging / ".riverhog" / "provenance"
    metadata_dir = output_root / "metadata"
    payload_dir = output_root / "payloads"
    journal_dir = output_root / "journals"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    payload_dir.mkdir(exist_ok=True)
    journal_dir.mkdir(exist_ok=True)
    (output_root / "root.json").write_bytes(root_bytes)
    state = sqlite3.connect(scratch / "provenance-validation.sqlite3")
    _initialize_provenance_recovery_state(state)
    progress = state.execute(
        "SELECT next_volume, hash_state, next_file_order, previous_path, "
        "previous_journal_id, current_journal_id, current_journal_bytes, "
        "current_journal_sha256, journal_count, omitted "
        "FROM provenance_progress WHERE singleton = 1"
    ).fetchone()
    if progress is None:
        volume_digest = CheckpointSHA256()
        state.execute(
            "INSERT INTO provenance_progress VALUES (1, ?, ?, 0, NULL, NULL, NULL, 0, '', 0, 0)",
            (format_provenance_sequence(0), volume_digest.export_state()),
        )
        state.commit()
        next_volume = 0
        next_file_order = 0
        previous_path: bytes | None = None
        previous_journal_id: str | None = None
        current_journal_id: str | None = None
        current_journal_bytes = 0
        current_journal_sha256 = ""
        journal_count = 0
        omitted = 0
    else:
        next_volume = parse_provenance_sequence(
            progress[0], "provenance recovery checkpoint sequence"
        )
        volume_digest = CheckpointSHA256.from_state(str(progress[1]))
        next_file_order = int(progress[2])
        previous_path = str(progress[3]).encode("utf-8") if progress[3] is not None else None
        previous_journal_id = str(progress[4]) if progress[4] is not None else None
        current_journal_id = str(progress[5]) if progress[5] is not None else None
        current_journal_bytes = int(progress[6])
        current_journal_sha256 = str(progress[7])
        journal_count = int(progress[8])
        omitted = int(progress[9])
    current_journal_path: Path | None = None
    if current_journal_id is not None:
        current_journal_path = journal_dir / f"{current_journal_id}.json-seq"
    terminal_row = state.execute(
        "SELECT sequence FROM provenance_terminal WHERE singleton = 1"
    ).fetchone()
    provenance_volume_count = (
        parse_provenance_sequence(terminal_row[0], "provenance recovery terminal sequence")
        if terminal_row is not None
        else None
    )
    sequence = next_volume
    try:
        while provenance_volume_count is None:
            sequence_token = format_provenance_sequence(sequence)
            metadata_relative = f"provenance/metadata/volume-{sequence_token}.json.age"
            encrypted_metadata = _archive_file(archive, metadata_relative)
            metadata_plaintext = scratch / f"provenance-volume-{sequence_token}.json"
            _age_decrypt(
                encrypted_metadata,
                metadata_plaintext,
                passphrase=passphrase,
                command=age_command,
            )
            metadata_bytes = metadata_plaintext.read_bytes()
            try:
                metadata_value = json.loads(metadata_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RecoveryError("provenance sequence metadata is not valid JSON") from exc
            if not isinstance(metadata_value, dict):
                raise RecoveryError("provenance sequence metadata is not an object")
            if metadata_value.get("schema") == "riverhog-provenance-terminal/v1":
                terminal_document = ProvenanceTerminalDocument.from_json_bytes(metadata_bytes)
                terminal = parse_segmented_provenance_terminal(metadata_bytes)
                if (
                    current_journal_id is not None
                    or terminal.sequence != sequence
                    or terminal.archive_generation != root.archive_generation
                    or terminal.archive_tree_sha256 != root.archive_tree_sha256
                ):
                    raise RecoveryError("provenance terminal sequence is not canonical")
                update_ordered_volume_commitment(volume_digest, terminal_document)
                (metadata_dir / f"volume-{sequence_token}.json").write_bytes(metadata_bytes)
                _save_provenance_progress(
                    state,
                    next_volume=sequence,
                    volume_digest=volume_digest,
                    next_file_order=next_file_order,
                    previous_path=previous_path,
                    previous_journal_id=previous_journal_id,
                    current_journal_id=current_journal_id,
                    current_journal_bytes=current_journal_bytes,
                    current_journal_sha256=current_journal_sha256,
                    journal_count=journal_count,
                    omitted=omitted,
                    terminal_sequence=sequence,
                )
                provenance_volume_count = sequence
                break
            volume = parse_segmented_provenance_volume(metadata_bytes)
            volume_document = ProvenanceVolumeDocument.from_json_bytes(metadata_bytes)
            update_ordered_volume_commitment(volume_digest, volume_document)
            if (
                volume.sequence != sequence
                or volume.archive_generation != root.archive_generation
                or volume.archive_tree_sha256 != root.archive_tree_sha256
            ):
                raise RecoveryError("provenance volume sequence is not canonical")
            (metadata_dir / f"volume-{sequence_token}.json").write_bytes(metadata_bytes)
            encrypted_payload = _archive_file(archive, volume.payload_path)
            payload_plaintext = scratch / f"provenance-payload-{sequence_token}.bin"
            _age_decrypt(
                encrypted_payload,
                payload_plaintext,
                passphrase=passphrase,
                command=age_command,
            )
            payload = payload_plaintext.read_bytes()
            if (
                len(payload) != volume.payload_bytes
                or hashlib.sha256(payload).hexdigest() != volume.payload_sha256
            ):
                raise RecoveryError("provenance volume payload identity differs")
            (payload_dir / f"volume-{sequence_token}.bin").write_bytes(payload)
            if volume.payload_kind == "bindings":
                if current_journal_id is not None or previous_journal_id is not None:
                    raise RecoveryError("provenance bindings follow journal data")
                first, bindings = parse_segmented_binding_payload(payload)
                if first != next_file_order or volume.first_file_order != first:
                    raise RecoveryError("provenance binding order is not contiguous")
                if volume.file_count != len(bindings):
                    raise RecoveryError("provenance binding count differs from metadata")
                for binding in bindings:
                    path_bytes = binding.path.encode("utf-8")
                    if previous_path is not None and path_bytes <= previous_path:
                        raise RecoveryError("provenance file paths are not canonical")
                    expected = _expected_file(expected_files, binding.path)
                    if (
                        expected is None
                        or binding.bytes != expected.bytes
                        or binding.sha256 != expected.sha256
                    ):
                        raise RecoveryError(f"provenance payload binding mismatch: {binding.path}")
                    state.execute(
                        "INSERT INTO bindings VALUES (?, ?, ?, ?)",
                        (
                            binding.path,
                            binding.status,
                            binding.journal_id,
                            binding.current_state_id,
                        ),
                    )
                    omitted += int(binding.status == "omitted")
                    previous_path = path_bytes
                    next_file_order += 1
                _save_provenance_progress(
                    state,
                    next_volume=sequence + 1,
                    volume_digest=volume_digest,
                    next_file_order=next_file_order,
                    previous_path=previous_path,
                    previous_journal_id=previous_journal_id,
                    current_journal_id=current_journal_id,
                    current_journal_bytes=current_journal_bytes,
                    current_journal_sha256=current_journal_sha256,
                    journal_count=journal_count,
                    omitted=omitted,
                )
                sequence += 1
                continue

            if volume.journal_id is None or volume.journal_offset is None:
                raise RecoveryError("provenance journal range is incomplete")
            if current_journal_id is None:
                if previous_journal_id is not None and volume.journal_id <= previous_journal_id:
                    raise RecoveryError("provenance journal order is not canonical")
                if volume.journal_offset != 0:
                    raise RecoveryError("provenance journal does not begin at offset zero")
                current_journal_id = volume.journal_id
                current_journal_path = journal_dir / f"{volume.journal_id}.json-seq"
                current_journal_bytes = int(volume.journal_bytes or 0)
                current_journal_sha256 = str(volume.journal_sha256 or "")
            if (
                volume.journal_id != current_journal_id
                or volume.journal_offset
                != (
                    current_journal_path.stat().st_size
                    if current_journal_path is not None and current_journal_path.exists()
                    else 0
                )
                or volume.journal_bytes != current_journal_bytes
                or volume.journal_sha256 != current_journal_sha256
            ):
                raise RecoveryError("provenance journal segments are not contiguous")
            assert current_journal_path is not None
            _append_exact_segment(
                current_journal_path,
                offset=volume.journal_offset,
                content=payload,
            )
            journal_complete = current_journal_path.stat().st_size == current_journal_bytes
            validated = _advance_recovered_journal_validation(
                state,
                path=current_journal_path,
                journal_id=current_journal_id,
                complete=journal_complete,
            )
            if journal_complete:
                if _sha256(current_journal_path) != current_journal_sha256:
                    raise RecoveryError("provenance journal SHA-256 differs")
                if validated is None or validated.journal_id != current_journal_id:
                    raise RecoveryError("provenance journal identity differs")
                after_path: str | None = None
                while True:
                    direct = state.execute(
                        "SELECT path, current_state_id FROM bindings "
                        "WHERE journal_id = ? AND (? IS NULL OR path > ?) "
                        "ORDER BY path LIMIT 256",
                        (current_journal_id, after_path, after_path),
                    ).fetchall()
                    if not direct:
                        break
                    for path, current_state_id in direct:
                        expected = _expected_file(expected_files, str(path))
                        if expected is None:
                            raise RecoveryError("captured provenance names an unknown file")
                        if (
                            current_state_id != validated.state_id
                            or path != validated.path
                            or expected.bytes != validated.bytes
                            or expected.sha256 != validated.sha256
                        ):
                            raise RecoveryError(
                                "captured provenance binding differs from its journal"
                            )
                    after_path = str(direct[-1][0])
                previous_journal_id = current_journal_id
                current_journal_id = None
                current_journal_path = None
                journal_count += 1
            _save_provenance_progress(
                state,
                next_volume=sequence + 1,
                volume_digest=volume_digest,
                next_file_order=next_file_order,
                previous_path=previous_path,
                previous_journal_id=previous_journal_id,
                current_journal_id=current_journal_id,
                current_journal_bytes=current_journal_bytes,
                current_journal_sha256=current_journal_sha256,
                journal_count=journal_count,
                omitted=omitted,
            )
            sequence += 1
        if volume_digest.hexdigest() != root.ordered_volume_sha256:
            raise RecoveryError("provenance volume sequence does not match its root")
        recovered_file_count = int(
            expected_files.execute("SELECT count(*) FROM files").fetchone()[0]
        )
        if next_file_order != recovered_file_count:
            raise RecoveryError("provenance does not account for every recovered file")
        unresolved = state.execute(
            """
            SELECT 1
            FROM external_states x
            LEFT JOIN entries e
              ON e.journal_id = x.journal_id
             AND e.entry_id = x.entry_id
             AND e.sha256 = x.entry_sha256
            LEFT JOIN states s
              ON s.journal_id = x.journal_id
             AND s.state_id = x.state_id
            WHERE e.entry_id IS NULL OR s.state_id IS NULL
            LIMIT 1
            """
        ).fetchone()
        if unresolved is not None:
            raise RecoveryError("provenance has an unresolved external state")
        unreachable = state.execute(
            """
            WITH RECURSIVE reachable(journal_id) AS (
                SELECT DISTINCT journal_id FROM bindings WHERE status = 'captured'
                UNION
                SELECT x.journal_id
                FROM external_states x
                JOIN reachable r ON x.from_journal_id = r.journal_id
            )
            SELECT 1
            FROM (SELECT DISTINCT journal_id FROM entries) j
            LEFT JOIN reachable r ON r.journal_id = j.journal_id
            WHERE r.journal_id IS NULL
            LIMIT 1
            """
        ).fetchone()
        if unreachable is not None:
            raise RecoveryError("provenance contains an unreachable journal")
    except sqlite3.Error as exc:
        raise RecoveryError(f"provenance validation state failed: {exc}") from exc
    finally:
        state.close()
    return ("mixed" if omitted else "captured", journal_count)


def _recover_pack(
    plaintext: Path,
    *,
    staging: Path,
    volume: PackArchiveVolume,
    state: sqlite3.Connection,
) -> tuple[FileIdentity, ...]:
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
                prior = state.execute(
                    "SELECT volume_sequence FROM file_volumes WHERE path = ? LIMIT 1",
                    (current.path,),
                ).fetchone()
                if (
                    prior is not None
                    and parse_archive_sequence(prior[0], "recovered file volume sequence")
                    != volume.sequence
                ):
                    raise RecoveryError(f"archive repeats a logical file: {current.path}")
                try:
                    _verify_file(
                        destination,
                        FileIdentity(current.path, current.bytes, current.sha256),
                    )
                except RecoveryError:
                    destination.unlink()
                else:
                    source.close()
                    continue
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
    volume: PackArchiveVolume,
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
    plaintext_bytes = plaintext.stat().st_size
    if destination.exists() and destination.stat().st_size == offset + plaintext_bytes:
        if _file_range_matches(destination, offset=offset, expected=plaintext):
            return
        with destination.open("r+b") as target:
            target.truncate(offset)
    if destination.exists() and offset < destination.stat().st_size < offset + plaintext_bytes:
        with destination.open("r+b") as target:
            target.truncate(offset)
    if destination.exists() and destination.stat().st_size != offset:
        raise RecoveryError(f"file segments are out of order: {file.path}")
    if not destination.exists() and offset != 0:
        raise RecoveryError(f"file begins with a nonzero segment offset: {file.path}")
    mode = "ab" if destination.exists() else "xb"
    with plaintext.open("rb") as source, destination.open(mode) as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)


def _file_range_matches(destination: Path, *, offset: int, expected: Path) -> bool:
    with destination.open("rb") as actual, expected.open("rb") as source:
        actual.seek(offset)
        while chunk := source.read(1024 * 1024):
            if actual.read(len(chunk)) != chunk:
                return False
        return True


def _verify_stored_parts(path: Path, parts: Sequence[StoredPartIdentity]) -> None:
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


def _verify_plaintext_parts(path: Path, volume: PackArchiveVolume | SegmentArchiveVolume) -> None:
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


def _validate_raw_ranges_state(state: sqlite3.Connection) -> None:
    after_path: str | None = None
    while True:
        rows = state.execute(
            "SELECT path, bytes FROM files WHERE kind = 'segment' "
            "AND (? IS NULL OR path > ?) ORDER BY path LIMIT 256",
            (after_path, after_path),
        ).fetchall()
        if not rows:
            return
        for path_value, expected_bytes in rows:
            path = str(path_value)
            current_ranges = state.execute(
                "SELECT offset, bytes FROM raw_ranges WHERE path = ? ORDER BY offset",
                (path,),
            )
            offset = 0
            for start, byte_count in current_ranges:
                if int(start) != offset:
                    raise RecoveryError(f"raw file segments are not contiguous: {path}")
                offset += int(byte_count)
            if offset != int(expected_bytes):
                raise RecoveryError(f"raw file segments do not cover the file: {path}")
        after_path = str(rows[-1][0])


def _verify_recovered_tree(
    state: sqlite3.Connection,
    *,
    staging: Path,
) -> dict[str, object]:
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    after_path: str | None = None
    while True:
        rows = state.execute(
            "SELECT path, bytes, sha256 FROM files "
            "WHERE (? IS NULL OR path > ?) ORDER BY path LIMIT 256",
            (after_path, after_path),
        ).fetchall()
        if not rows:
            break
        for path_value, bytes_value, sha256_value in rows:
            current = FileIdentity(
                path=str(path_value),
                bytes=int(bytes_value),
                sha256=str(sha256_value),
            )
            _verify_file(_output_file(staging, current.path), current)
            digest.update(f"{current.path}\t{current.bytes}\t{current.sha256}\n".encode())
            file_count += 1
            byte_count += current.bytes
        after_path = str(rows[-1][0])
    return {"files": file_count, "bytes": byte_count, "sha256": digest.hexdigest()}


def _validate_raw_ranges(
    files: Mapping[str, FileIdentity],
    ranges: Mapping[str, list[tuple[int, int]]],
) -> None:
    """Validate one already-bounded in-memory raw-file fixture."""

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
