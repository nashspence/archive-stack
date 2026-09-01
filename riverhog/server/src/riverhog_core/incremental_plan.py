from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from riverhog_archive_contracts import format_archive_sequence
from riverhog_protocol.pack_ingress import RESERVED_ARCHIVE_PREFIX, canonical_json_bytes
from riverhog_protocol.paths import normalize_relpath

from riverhog_core.checkpoint_sha256 import CheckpointSHA256
from riverhog_core.collection_plan import CollectionVolumePolicy
from riverhog_core.domain.archive import ArchiveFile, PackVolumePlan, RawVolumePlan
from riverhog_core.pack_volume import plan_pack_volume
from riverhog_core.raw_volume import plan_raw_volumes

INCREMENTAL_VOLUME_PLANNER_CHECKPOINT_SCHEMA = "incremental-volume-planner-checkpoint/v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CONTENT_IDENTITY_PREFIX = b'{"files":['
_CONTENT_IDENTITY_SUFFIX = b'],"format":"riverhog-collection-content/v1"}'


@dataclass(frozen=True, slots=True)
class OrderedArchiveFile:
    order: int
    file: ArchiveFile


@dataclass(frozen=True, slots=True)
class IncrementalVolumePlannerCheckpoint:
    policy: CollectionVolumePolicy
    content_hash_state: str
    next_file_order: int = 0
    next_sequence: int = 0
    files_seen: int = 0
    bytes_seen: int = 0
    pending_pack_files: tuple[ArchiveFile, ...] = ()
    closed: bool = False
    content_identity: str | None = None

    def __post_init__(self) -> None:
        if (
            min(
                self.next_file_order,
                self.next_sequence,
                self.files_seen,
                self.bytes_seen,
            )
            < 0
        ):
            raise ValueError("incremental planner counters must be non-negative")
        if self.next_file_order != self.files_seen:
            raise ValueError("incremental planner file order does not match files seen")
        format_archive_sequence(self.next_sequence)
        if len(self.pending_pack_files) > self.policy.pack_files:
            raise ValueError("incremental planner pending pack exceeds its file limit")
        pending_bytes = 0
        seen: set[str] = set()
        for current in self.pending_pack_files:
            normalized = _normalized_file(current)
            if normalized.path in seen:
                raise ValueError("incremental planner repeats a pending path")
            if normalized.bytes >= self.policy.pack_member_bytes:
                raise ValueError("incremental planner pending file is outside pack policy")
            seen.add(normalized.path)
            pending_bytes += normalized.bytes
        if pending_bytes > self.policy.pack_source_bytes:
            raise ValueError("incremental planner pending pack exceeds its byte limit")
        if self.closed and self.pending_pack_files:
            raise ValueError("closed incremental planner cannot retain pending files")
        try:
            digest = CheckpointSHA256.from_state(self.content_hash_state)
        except ValueError as exc:
            raise ValueError("incremental planner content hash state is invalid") from exc
        if self.closed:
            expected_identity = digest.copy().update(_CONTENT_IDENTITY_SUFFIX).hexdigest()
            if self.content_identity != expected_identity:
                raise ValueError("closed incremental planner content identity is invalid")
        elif self.content_identity is not None:
            raise ValueError("open incremental planner cannot have a content identity")


@dataclass(frozen=True, slots=True)
class IncrementalVolumePlanBatch:
    checkpoint: IncrementalVolumePlannerCheckpoint
    packs: tuple[PackVolumePlan, ...]
    raw_volumes: tuple[RawVolumePlan, ...]

    @property
    def volumes(self) -> tuple[PackVolumePlan | RawVolumePlan, ...]:
        return tuple(sorted((*self.packs, *self.raw_volumes), key=lambda current: current.sequence))


def new_incremental_volume_planner(
    *,
    policy: CollectionVolumePolicy | None = None,
) -> IncrementalVolumePlannerCheckpoint:
    digest = CheckpointSHA256(_CONTENT_IDENTITY_PREFIX)
    return IncrementalVolumePlannerCheckpoint(
        policy=policy or CollectionVolumePolicy(),
        content_hash_state=digest.export_state(),
    )


def normalize_ordered_archive_file(
    value: OrderedArchiveFile,
) -> OrderedArchiveFile:
    if value.order < 0:
        raise ValueError("incremental planner file order must be non-negative")
    return OrderedArchiveFile(order=value.order, file=_normalized_file(value.file))


def advance_incremental_volume_plan(
    checkpoint: IncrementalVolumePlannerCheckpoint,
    files: Sequence[OrderedArchiveFile],
    *,
    final: bool = False,
) -> IncrementalVolumePlanBatch:
    """Advance bounded collection planning without retaining the whole file set.

    The caller must persist the returned checkpoint and every emitted immutable volume plan in
    one database transaction. Input file rows are ordered registration records; a unique
    ``(collection_id, path)`` database constraint remains the collection-wide duplicate guard.
    """

    if checkpoint.closed:
        raise ValueError("incremental volume planner is already closed")
    pending = list(checkpoint.pending_pack_files)
    pending_bytes = sum(current.bytes for current in pending)
    next_order = checkpoint.next_file_order
    next_sequence = checkpoint.next_sequence
    files_seen = checkpoint.files_seen
    bytes_seen = checkpoint.bytes_seen
    content_digest = CheckpointSHA256.from_state(checkpoint.content_hash_state)
    packs: list[PackVolumePlan] = []
    raw_volumes: list[RawVolumePlan] = []

    def flush_pack() -> None:
        nonlocal pending, pending_bytes, next_sequence
        if not pending:
            return
        packs.append(
            plan_pack_volume(
                pending,
                sequence=next_sequence,
                max_member_bytes=checkpoint.policy.pack_member_bytes,
                part_plaintext_bytes=checkpoint.policy.pack_part_plaintext_bytes,
            )
        )
        next_sequence += 1
        pending = []
        pending_bytes = 0

    for raw_ordered in files:
        ordered = normalize_ordered_archive_file(raw_ordered)
        if ordered.order != next_order:
            raise ValueError("incremental planner file order is not contiguous")
        current = _normalized_file(ordered.file)
        if files_seen:
            content_digest.update(b",")
        content_digest.update(_content_identity_member_bytes(current))
        if current.bytes < checkpoint.policy.pack_member_bytes:
            if pending and (
                len(pending) >= checkpoint.policy.pack_files
                or pending_bytes + current.bytes > checkpoint.policy.pack_source_bytes
            ):
                flush_pack()
            pending.append(current)
            pending_bytes += current.bytes
        else:
            flush_pack()
            planned = plan_raw_volumes(
                (current,),
                starting_sequence=next_sequence,
                max_plaintext_bytes=checkpoint.policy.raw_volume_plaintext_bytes,
            )
            raw_volumes.extend(planned)
            next_sequence += len(planned)
        next_order += 1
        files_seen += 1
        bytes_seen += current.bytes

    if final:
        flush_pack()
    content_identity = (
        content_digest.copy().update(_CONTENT_IDENTITY_SUFFIX).hexdigest() if final else None
    )
    next_checkpoint = IncrementalVolumePlannerCheckpoint(
        policy=checkpoint.policy,
        content_hash_state=content_digest.export_state(),
        next_file_order=next_order,
        next_sequence=next_sequence,
        files_seen=files_seen,
        bytes_seen=bytes_seen,
        pending_pack_files=tuple(pending),
        closed=final,
        content_identity=content_identity,
    )
    emitted: list[PackVolumePlan | RawVolumePlan] = [*packs, *raw_volumes]
    emitted.sort(key=lambda current: current.sequence)
    if [current.sequence for current in emitted] != list(
        range(checkpoint.next_sequence, next_sequence)
    ):
        raise RuntimeError("incremental planner emitted non-canonical volume sequences")
    packs_by_sequence = tuple(current for current in emitted if isinstance(current, PackVolumePlan))
    raw_by_sequence = tuple(current for current in emitted if isinstance(current, RawVolumePlan))
    return IncrementalVolumePlanBatch(
        checkpoint=next_checkpoint,
        packs=packs_by_sequence,
        raw_volumes=raw_by_sequence,
    )


def incremental_volume_planner_checkpoint_payload(
    checkpoint: IncrementalVolumePlannerCheckpoint,
) -> dict[str, object]:
    IncrementalVolumePlannerCheckpoint(
        policy=checkpoint.policy,
        content_hash_state=checkpoint.content_hash_state,
        next_file_order=checkpoint.next_file_order,
        next_sequence=checkpoint.next_sequence,
        files_seen=checkpoint.files_seen,
        bytes_seen=checkpoint.bytes_seen,
        pending_pack_files=checkpoint.pending_pack_files,
        closed=checkpoint.closed,
        content_identity=checkpoint.content_identity,
    )
    return {
        "schema": INCREMENTAL_VOLUME_PLANNER_CHECKPOINT_SCHEMA,
        "policy": _policy_payload(checkpoint.policy),
        "content_hash_state": checkpoint.content_hash_state,
        "next_file_order": checkpoint.next_file_order,
        "next_sequence": checkpoint.next_sequence,
        "files_seen": checkpoint.files_seen,
        "bytes_seen": checkpoint.bytes_seen,
        "pending_pack_files": [
            {
                "path": current.path,
                "bytes": current.bytes,
                "sha256": current.sha256,
            }
            for current in checkpoint.pending_pack_files
        ],
        "closed": checkpoint.closed,
        "content_identity": checkpoint.content_identity,
    }


def incremental_volume_planner_checkpoint_bytes(
    checkpoint: IncrementalVolumePlannerCheckpoint,
) -> bytes:
    return canonical_json_bytes(incremental_volume_planner_checkpoint_payload(checkpoint))


def parse_incremental_volume_planner_checkpoint(
    content: bytes | str,
) -> IncrementalVolumePlannerCheckpoint:
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    try:
        payload = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("incremental volume planner checkpoint is not valid JSON") from exc
    expected = {
        "schema",
        "policy",
        "content_hash_state",
        "next_file_order",
        "next_sequence",
        "files_seen",
        "bytes_seen",
        "pending_pack_files",
        "closed",
        "content_identity",
    }
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != INCREMENTAL_VOLUME_PLANNER_CHECKPOINT_SCHEMA
        or set(payload) != expected
    ):
        raise ValueError("incremental volume planner checkpoint schema mismatch")
    policy = _parse_policy(payload.get("policy"))
    raw_pending = payload.get("pending_pack_files")
    if not isinstance(raw_pending, list):
        raise ValueError("incremental planner pending files must be a list")
    pending: list[ArchiveFile] = []
    for raw in raw_pending:
        if not isinstance(raw, dict) or set(raw) != {"path", "bytes", "sha256"}:
            raise ValueError("incremental planner pending file is invalid")
        pending.append(
            _normalized_file(
                ArchiveFile(
                    path=str(raw.get("path", "")),
                    bytes=_uint(raw.get("bytes"), label="pending file bytes"),
                    sha256=str(raw.get("sha256", "")),
                )
            )
        )
    closed = payload.get("closed")
    if not isinstance(closed, bool):
        raise ValueError("incremental planner closed flag must be boolean")
    checkpoint = IncrementalVolumePlannerCheckpoint(
        policy=policy,
        content_hash_state=str(payload.get("content_hash_state", "")),
        next_file_order=_uint(payload.get("next_file_order"), label="next file order"),
        next_sequence=_uint(payload.get("next_sequence"), label="next sequence"),
        files_seen=_uint(payload.get("files_seen"), label="files seen"),
        bytes_seen=_uint(payload.get("bytes_seen"), label="bytes seen"),
        pending_pack_files=tuple(pending),
        closed=closed,
        content_identity=(
            str(payload["content_identity"])
            if payload.get("content_identity") is not None
            else None
        ),
    )
    if incremental_volume_planner_checkpoint_bytes(checkpoint) != canonical_json_bytes(payload):
        raise ValueError("incremental planner checkpoint is not canonical")
    return checkpoint


def _normalized_file(file: ArchiveFile) -> ArchiveFile:
    path = normalize_relpath(file.path)
    if path.startswith(RESERVED_ARCHIVE_PREFIX):
        raise ValueError("incremental planner file uses the reserved archive namespace")
    if file.bytes < 0 or _SHA256_RE.fullmatch(file.sha256) is None:
        raise ValueError("incremental planner file identity is invalid")
    return ArchiveFile(path=path, bytes=file.bytes, sha256=file.sha256)


def _content_identity_member_bytes(file: ArchiveFile) -> bytes:
    # Match riverhog_protocol.manifest exactly.  The checkpoint encoding is private,
    # while the completed digest remains the existing public collection identity.
    return json.dumps(
        {"path": file.path, "bytes": file.bytes, "sha256": file.sha256},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _policy_payload(policy: CollectionVolumePolicy) -> dict[str, int]:
    return {
        "pack_source_bytes": policy.pack_source_bytes,
        "pack_files": policy.pack_files,
        "pack_member_bytes": policy.pack_member_bytes,
        "pack_part_plaintext_bytes": policy.pack_part_plaintext_bytes,
        "raw_volume_plaintext_bytes": policy.raw_volume_plaintext_bytes,
        "raw_part_plaintext_bytes": policy.raw_part_plaintext_bytes,
    }


def _parse_policy(value: object) -> CollectionVolumePolicy:
    expected = {
        "pack_source_bytes",
        "pack_files",
        "pack_member_bytes",
        "pack_part_plaintext_bytes",
        "raw_volume_plaintext_bytes",
        "raw_part_plaintext_bytes",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("incremental planner policy is invalid")
    return CollectionVolumePolicy(
        pack_source_bytes=_positive(value.get("pack_source_bytes"), "pack source bytes"),
        pack_files=_positive(value.get("pack_files"), "pack files"),
        pack_member_bytes=_positive(value.get("pack_member_bytes"), "pack member bytes"),
        pack_part_plaintext_bytes=_positive(
            value.get("pack_part_plaintext_bytes"), "pack part plaintext bytes"
        ),
        raw_volume_plaintext_bytes=_positive(
            value.get("raw_volume_plaintext_bytes"), "raw volume plaintext bytes"
        ),
        raw_part_plaintext_bytes=_positive(
            value.get("raw_part_plaintext_bytes"), "raw part plaintext bytes"
        ),
    )


def _positive(value: object, label: str) -> int:
    parsed = _uint(value, label=label)
    if parsed < 1:
        raise ValueError(f"{label} must be positive")
    return parsed


def _uint(value: object, *, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative integer")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a non-negative integer") from exc
    if parsed < 0 or str(parsed) != str(value):
        raise ValueError(f"{label} must be a canonical non-negative integer")
    return parsed
