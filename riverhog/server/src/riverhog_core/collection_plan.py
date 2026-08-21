from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from riverhog_age import CHUNK_SIZE
from riverhog_protocol.pack_ingress import RESERVED_ARCHIVE_PREFIX, canonical_json_bytes
from riverhog_protocol.paths import normalize_relpath

from riverhog_core.archive_manifest import collection_tree_identity
from riverhog_core.domain.archive import ArchiveFile, PackVolumePlan, RawVolumePlan
from riverhog_core.pack_volume import (
    DEFAULT_PACK_FILES,
    DEFAULT_PACK_MEMBER_BYTES,
    DEFAULT_PACK_SOURCE_BYTES,
    DEFAULT_PART_PLAINTEXT_BYTES,
    plan_pack_volumes,
)
from riverhog_core.raw_volume import (
    DEFAULT_RAW_PART_PLAINTEXT_BYTES,
    DEFAULT_RAW_VOLUME_PLAINTEXT_BYTES,
    plan_raw_volumes,
)

COLLECTION_VOLUME_PLAN_SCHEMA = "collection-volume-plan/v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_BYTES_RE = re.compile(r"^(\d+(?:_\d+)*)([kmgt]i?b?|b)?$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CollectionVolumePolicy:
    pack_source_bytes: int = DEFAULT_PACK_SOURCE_BYTES
    pack_files: int = DEFAULT_PACK_FILES
    pack_member_bytes: int = DEFAULT_PACK_MEMBER_BYTES
    pack_part_plaintext_bytes: int = DEFAULT_PART_PLAINTEXT_BYTES
    raw_volume_plaintext_bytes: int = DEFAULT_RAW_VOLUME_PLAINTEXT_BYTES
    raw_part_plaintext_bytes: int = DEFAULT_RAW_PART_PLAINTEXT_BYTES

    def __post_init__(self) -> None:
        positive = (
            self.pack_source_bytes,
            self.pack_files,
            self.pack_member_bytes,
            self.pack_part_plaintext_bytes,
            self.raw_volume_plaintext_bytes,
            self.raw_part_plaintext_bytes,
        )
        if any(current < 1 for current in positive):
            raise ValueError("collection volume policy values must be positive")
        if self.pack_part_plaintext_bytes % CHUNK_SIZE:
            raise ValueError("pack part plaintext target must align to age chunks")
        if self.raw_part_plaintext_bytes % CHUNK_SIZE:
            raise ValueError("raw part plaintext target must align to age chunks")
        portable_min_part_bytes = 5 * 1024 * 1024
        if self.pack_part_plaintext_bytes < portable_min_part_bytes:
            raise ValueError("pack part plaintext target is below the portable multipart minimum")
        if self.raw_part_plaintext_bytes < portable_min_part_bytes:
            raise ValueError("raw part plaintext target is below the portable multipart minimum")
        if self.raw_volume_plaintext_bytes < self.raw_part_plaintext_bytes:
            raise ValueError("raw volume must contain at least one configured raw part")
        if self.raw_volume_plaintext_bytes % self.raw_part_plaintext_bytes:
            raise ValueError("raw volume size must be a multiple of the raw part size")
        maximum_part_bytes = 4 * 1024**3
        if max(self.pack_part_plaintext_bytes, self.raw_part_plaintext_bytes) > (
            maximum_part_bytes
        ):
            raise ValueError("archive part plaintext target exceeds the portable bound")
        if self.raw_volume_plaintext_bytes > 4 * 1024**4:
            raise ValueError("raw volume plaintext target exceeds the portable object bound")
        if self.raw_volume_plaintext_bytes // self.raw_part_plaintext_bytes > 10_000:
            raise ValueError("raw volume policy would exceed the portable multipart part limit")

    @classmethod
    def from_env(cls, values: Mapping[str, str]) -> CollectionVolumePolicy:
        """Load layout knobs that are persisted into each immutable collection plan."""

        common_part_bytes = _env_bytes(
            values,
            "RIVERHOG_ARCHIVE_MULTIPART_PART_BYTES",
            DEFAULT_PART_PLAINTEXT_BYTES,
        )
        return cls(
            pack_source_bytes=_env_bytes(
                values,
                "RIVERHOG_PACK_SOURCE_BYTES",
                DEFAULT_PACK_SOURCE_BYTES,
            ),
            pack_files=_env_int(
                values,
                "RIVERHOG_PACK_FILES",
                DEFAULT_PACK_FILES,
            ),
            pack_member_bytes=_env_bytes(
                values,
                "RIVERHOG_PACK_MEMBER_BYTES",
                DEFAULT_PACK_MEMBER_BYTES,
            ),
            pack_part_plaintext_bytes=_env_bytes(
                values,
                "RIVERHOG_PACK_PART_PLAINTEXT_BYTES",
                common_part_bytes,
            ),
            raw_volume_plaintext_bytes=_env_bytes(
                values,
                "RIVERHOG_RAW_VOLUME_PLAINTEXT_BYTES",
                DEFAULT_RAW_VOLUME_PLAINTEXT_BYTES,
            ),
            raw_part_plaintext_bytes=_env_bytes(
                values,
                "RIVERHOG_RAW_PART_PLAINTEXT_BYTES",
                common_part_bytes,
            ),
        )


@dataclass(frozen=True, slots=True)
class CollectionVolumePlan:
    files: tuple[ArchiveFile, ...]
    policy: CollectionVolumePolicy
    packs: tuple[PackVolumePlan, ...]
    raw_volumes: tuple[RawVolumePlan, ...]
    tree_sha256: str
    plan_sha256: str

    @property
    def volume_count(self) -> int:
        return len(self.packs) + len(self.raw_volumes)


def plan_collection_volumes(
    files: Sequence[ArchiveFile],
    *,
    policy: CollectionVolumePolicy | None = None,
) -> CollectionVolumePlan:
    effective_policy = policy or CollectionVolumePolicy()
    normalized = _normalized_files(files)
    packed_files = tuple(
        current for current in normalized if current.bytes < effective_policy.pack_member_bytes
    )
    raw_files = tuple(
        current for current in normalized if current.bytes >= effective_policy.pack_member_bytes
    )
    packs = (
        plan_pack_volumes(
            packed_files,
            source_bytes_per_volume=effective_policy.pack_source_bytes,
            files_per_volume=effective_policy.pack_files,
            max_member_bytes=effective_policy.pack_member_bytes,
            part_plaintext_bytes=effective_policy.pack_part_plaintext_bytes,
        )
        if packed_files
        else ()
    )
    raw_volumes = (
        plan_raw_volumes(
            raw_files,
            starting_sequence=len(packs),
            max_plaintext_bytes=effective_policy.raw_volume_plaintext_bytes,
        )
        if raw_files
        else ()
    )
    sequences = [current.sequence for current in packs] + [
        current.sequence for current in raw_volumes
    ]
    if sequences != list(range(len(sequences))):
        raise RuntimeError("collection volume planner produced non-canonical sequences")
    tree = collection_tree_identity(normalized)
    base = _base_payload(
        files=normalized,
        policy=effective_policy,
        packs=packs,
        raw_volumes=raw_volumes,
        tree=tree,
    )
    plan_sha256 = hashlib.sha256(canonical_json_bytes(base)).hexdigest()
    return CollectionVolumePlan(
        files=normalized,
        policy=effective_policy,
        packs=tuple(packs),
        raw_volumes=tuple(raw_volumes),
        tree_sha256=str(tree["sha256"]),
        plan_sha256=plan_sha256,
    )


def _base_payload(
    *,
    files: Sequence[ArchiveFile],
    policy: CollectionVolumePolicy,
    packs: Sequence[PackVolumePlan],
    raw_volumes: Sequence[RawVolumePlan],
    tree: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema": COLLECTION_VOLUME_PLAN_SCHEMA,
        "policy": {
            "pack_source_bytes": policy.pack_source_bytes,
            "pack_files": policy.pack_files,
            "pack_member_bytes": policy.pack_member_bytes,
            "pack_part_plaintext_bytes": policy.pack_part_plaintext_bytes,
            "raw_volume_plaintext_bytes": policy.raw_volume_plaintext_bytes,
            "raw_part_plaintext_bytes": policy.raw_part_plaintext_bytes,
        },
        "tree": dict(tree),
        "files": [
            {"path": current.path, "bytes": current.bytes, "sha256": current.sha256}
            for current in files
        ],
        "volumes": [
            *(
                {
                    "id": current.volume_id,
                    "sequence": current.sequence,
                    "kind": "pack",
                    "plaintext_bytes": current.plaintext_bytes,
                    "files": len(current.members),
                    "source_bytes": sum(member.bytes for member in current.members),
                    "index_sha256": current.index_sha256,
                    "plan_sha256": current.plan_sha256,
                }
                for current in packs
            ),
            *(
                {
                    "id": current.volume_id,
                    "sequence": current.sequence,
                    "kind": "segment",
                    "source_path": current.source_path,
                    "file_offset": current.file_offset,
                    "plaintext_bytes": current.plaintext_bytes,
                    "file_bytes": current.file_bytes,
                    "file_sha256": current.file_sha256,
                }
                for current in raw_volumes
            ),
        ],
    }


def _normalized_files(files: Sequence[ArchiveFile]) -> tuple[ArchiveFile, ...]:
    normalized: list[ArchiveFile] = []
    seen: set[str] = set()
    for current in files:
        path = normalize_relpath(current.path)
        if path.startswith(RESERVED_ARCHIVE_PREFIX) or path in seen:
            raise ValueError(f"collection volume plan path is invalid: {path}")
        if current.bytes < 0 or _SHA256_RE.fullmatch(current.sha256) is None:
            raise ValueError(f"collection volume plan file identity is invalid: {path}")
        seen.add(path)
        normalized.append(ArchiveFile(path=path, bytes=current.bytes, sha256=current.sha256))
    if not normalized:
        raise ValueError("collection volume plan requires at least one file")
    return tuple(sorted(normalized, key=lambda current: current.path))


def _env_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_bytes(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    candidate = raw.strip().replace(" ", "")
    match = _BYTES_RE.fullmatch(candidate)
    if match is None:
        raise ValueError(f"{name} must be a byte size such as 64MiB")
    amount = int(match.group(1).replace("_", ""))
    unit = (match.group(2) or "b").casefold()
    scale = {
        "b": 1,
        "k": 1000,
        "kb": 1000,
        "m": 1000**2,
        "mb": 1000**2,
        "g": 1000**3,
        "gb": 1000**3,
        "t": 1000**4,
        "tb": 1000**4,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
    }[unit]
    return amount * scale
