from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArchiveFile:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SealedProvenanceObject:
    object_id: str
    kind: str
    relative_path: str
    plaintext_bytes: int
    plaintext_sha256: str
    stored_bytes: int
    stored_sha256: str
    version_id: str | None
    completed_at: str


@dataclass(frozen=True, slots=True)
class PackMemberPlan:
    path: str
    bytes: int
    sha256: str
    unit: int
    header_offset: int
    data_offset: int
    end_offset: int


@dataclass(frozen=True, slots=True)
class PackPaddingPlan:
    path: str
    header_offset: int
    payload_bytes: int
    end_offset: int


@dataclass(frozen=True, slots=True)
class PackUploadUnitPlan:
    unit: int
    plaintext_start: int
    plaintext_end: int
    sources: tuple[ArchiveFile, ...]
    padding: PackPaddingPlan | None = None
    includes_index: bool = False
    includes_end_markers: bool = False

    @property
    def plaintext_bytes(self) -> int:
        return self.plaintext_end - self.plaintext_start

    @property
    def payload_bytes(self) -> int:
        return sum(current.bytes for current in self.sources)

    @property
    def final(self) -> bool:
        return self.includes_end_markers


@dataclass(frozen=True, slots=True)
class PackVolumePlan:
    volume_id: str
    sequence: int
    max_member_bytes: int
    part_plaintext_bytes: int
    members: tuple[PackMemberPlan, ...]
    units: tuple[PackUploadUnitPlan, ...]
    index_bytes: bytes
    index_sha256: str
    plaintext_bytes: int
    plan_sha256: str


@dataclass(frozen=True, slots=True)
class RawVolumePlan:
    volume_id: str
    sequence: int
    source_path: str
    file_offset: int
    plaintext_bytes: int
    file_bytes: int
    file_sha256: str


@dataclass(frozen=True, slots=True)
class StoredPartReceipt:
    number: int
    plaintext_start: int
    plaintext_bytes: int
    plaintext_sha256: str
    stored_bytes: int
    stored_sha256: str
    etag: str


@dataclass(frozen=True, slots=True)
class SealedPackVolume:
    volume_id: str
    sequence: int
    relative_path: str
    files: int
    source_bytes: int
    plaintext_bytes: int
    age_state_json: str
    index_sha256: str
    plan_sha256: str
    parts: tuple[StoredPartReceipt, ...]
    version_id: str | None
    completed_at: str

    @property
    def stored_bytes(self) -> int:
        return sum(current.stored_bytes for current in self.parts)


@dataclass(frozen=True, slots=True)
class VerifiedRawFile:
    path: str
    bytes: int
    sha256: str
    volume_set_sha256: str
    verified_at: str


@dataclass(frozen=True, slots=True)
class SealedRawVolume:
    volume_id: str
    sequence: int
    relative_path: str
    source_path: str
    file_offset: int
    plaintext_bytes: int
    file_bytes: int
    file_sha256: str
    age_state_json: str
    parts: tuple[StoredPartReceipt, ...]
    version_id: str | None
    completed_at: str

    @property
    def stored_bytes(self) -> int:
        return sum(current.stored_bytes for current in self.parts)
