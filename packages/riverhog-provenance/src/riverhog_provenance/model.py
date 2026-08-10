from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from os import PathLike
from typing import Any

type PathInput = str | bytes | PathLike[str] | PathLike[bytes]
type JsonObject = dict[str, Any]


class LargeValueDisposition(StrEnum):
    """How to retain native values larger than the inline threshold."""

    DIGEST_ONLY = "digest_only"
    NOT_RETAINED = "not_retained"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class ObservationPolicy:
    """Capture policy shared by all platform observers.

    The defaults are intentionally archival rather than minimal. They capture all
    native categories that the reference implementation can enumerate without
    interpreting the primary file bytes.
    """

    strict_consistency: bool = True
    attempt_noatime: bool = True
    verify_path_binding: bool = True
    second_content_hash: bool = False
    hash_chunk_bytes: int = 8 * 1024 * 1024
    inline_native_value_bytes: int = 1024 * 1024
    maximum_native_value_bytes: int = 256 * 1024 * 1024
    large_value_disposition: LargeValueDisposition = LargeValueDisposition.DIGEST_ONLY
    capture_xattrs: bool = True
    capture_acl: bool = True
    capture_file_flags: bool = True
    capture_sparse_map: bool = True
    capture_special_features: bool = True
    capture_native_stat: bool = True
    resolve_principals: bool = True
    include_access_time: bool = True
    include_hostname: bool = True
    include_effective_principal: bool = True
    maximum_sparse_extents: int = 100_000
    resource_fork_chunk_bytes: int = 8 * 1024 * 1024
    native_stream_chunk_bytes: int = 8 * 1024 * 1024
    maximum_native_streams: int = 10_000
    capture_system_acl: bool = False
    windows_allow_shared_write: bool = False
    windows_allow_shared_delete: bool = False
    windows_follow_non_name_surrogate_reparse_points: bool = True
    windows_capture_usn: bool = True
    windows_capture_object_id: bool = True

    def __post_init__(self) -> None:
        positive = {
            "hash_chunk_bytes": self.hash_chunk_bytes,
            "inline_native_value_bytes": self.inline_native_value_bytes,
            "maximum_native_value_bytes": self.maximum_native_value_bytes,
            "maximum_sparse_extents": self.maximum_sparse_extents,
            "resource_fork_chunk_bytes": self.resource_fork_chunk_bytes,
            "native_stream_chunk_bytes": self.native_stream_chunk_bytes,
            "maximum_native_streams": self.maximum_native_streams,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.inline_native_value_bytes > self.maximum_native_value_bytes:
            raise ValueError("inline_native_value_bytes cannot exceed maximum_native_value_bytes")


@dataclass(frozen=True, slots=True)
class PayloadBindingRequest:
    """Request a Riverhog provenance payload-binding assertion for the observed state."""

    relative_path: PathInput | None = None
    role: str = "co_resident_primary_payload"
    replaces_binding_id: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ObservationRequest:
    """Inputs needed to produce one immutable Riverhog provenance file-state observation.

    ``host_id`` is the durable host/naming-authority URI used to scope UIDs,
    GIDs, mount IDs, device IDs, and similar host-local observations.
    ``host_entity_id`` is the per-environment Host Entity that scopes source and
    mount locators; it is normally minted automatically so that
    successive technical-environment snapshots do not collide in one journal.
    """

    path: PathInput
    lineage_id: str
    host_id: str
    host_entity_id: str | None = None
    observer_agent_id: str | None = None
    state_id: str | None = None
    capture_id: str | None = None
    environment_id: str | None = None
    binding_id: str | None = None
    payload_binding: PayloadBindingRequest | None = None
    policy: ObservationPolicy = field(default_factory=ObservationPolicy)
    additional_agents: tuple[Mapping[str, Any], ...] = ()
    additional_associations: tuple[Mapping[str, Any], ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NativeStat:
    """Cross-platform stability and basic-filesystem snapshot."""

    device: int
    inode: int
    mode: int
    nlink: int
    uid: int
    gid: int
    size: int
    atime_ns: int
    mtime_ns: int
    ctime_ns: int
    birthtime_ns: int | None = None
    blocks: int | None = None
    block_size: int | None = None
    flags: int | None = None
    generation: int | None = None
    mount_id: int | None = None
    rdev: int | None = None
    extras: Mapping[str, Any] = field(default_factory=dict)

    def stability_key(self) -> tuple[int, ...]:
        """Fields expected to remain stable while a regular-file state is read.

        Access time is deliberately excluded: reading the payload can update it on
        hosts where a non-atime descriptor cannot be obtained. The state records
        the post-read access time and emits a diagnostic where that caveat applies.
        """

        return (
            self.device,
            self.inode,
            self.mode,
            self.nlink,
            self.uid,
            self.gid,
            self.size,
            self.mtime_ns,
            self.ctime_ns,
            self.flags if self.flags is not None else -1,
            self.generation if self.generation is not None else -1,
        )


@dataclass(frozen=True, slots=True)
class ExtensionDraft:
    """Backend-supplied semantic assertion awaiting local subject IDs."""

    subject_role: str
    property: str
    value: JsonObject
    confidence: str = "high"
    note: str | None = None


@dataclass(slots=True)
class NativeCollection:
    timestamps: list[JsonObject] = field(default_factory=list)
    access: JsonObject | None = None
    native_identifiers: list[JsonObject] = field(default_factory=list)
    native_metadata: list[JsonObject] = field(default_factory=list)
    coverage: dict[str, str] = field(default_factory=dict)
    diagnostics: list[JsonObject] = field(default_factory=list)
    environment: JsonObject | None = None
    extension_drafts: list[ExtensionDraft] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ObservationResult:
    """Schema-shaped result returned by every Riverhog provenance observer."""

    state: JsonObject
    capture: JsonObject
    environment: JsonObject
    agents: tuple[JsonObject, ...]
    payload_bindings: tuple[JsonObject, ...] = ()
    extensions: tuple[JsonObject, ...] = ()

    @property
    def payload_binding(self) -> JsonObject | None:
        """Return the newest bind assertion, for simple one-binding callers."""

        for item in reversed(self.payload_bindings):
            if item.get("operation") == "bind":
                return item
        return None

    def graph_fragment(self, *, omit_object_ids: Sequence[str] = ()) -> JsonObject:
        """Return an atomic Riverhog provenance graph fragment.

        ``omit_object_ids`` lets a journal writer suppress Agents or other objects
        that are already active in earlier entries. Riverhog provenance forbids duplicate
        active object IDs, while a standalone observer result remains self-contained
        by default.
        """

        omitted = set(omit_object_ids)
        fragment: JsonObject = {}
        categories = {
            "states": (self.state,),
            "captures": (self.capture,),
            "environments": (self.environment,),
            "agents": self.agents,
            "payload_bindings": self.payload_bindings,
            "extensions": self.extensions,
        }
        for category, values in categories.items():
            retained = [item for item in values if item.get("id") not in omitted]
            if retained:
                fragment[category] = retained
        return fragment

    def assertion_body(self, *, omit_object_ids: Sequence[str] = ()) -> JsonObject:
        return {"assertions": self.graph_fragment(omit_object_ids=omit_object_ids)}

    def make_assertion_entry(
        self,
        *,
        journal_id: str,
        sequence: int,
        previous_entry_id: str,
        previous_entry_json_sha256: str,
        previous_sequence: int | None = None,
        entry_id: str | None = None,
        recorded_at: str | None = None,
        recorded_by_agent_id: str | None = None,
        notes: Sequence[str] = (),
        omit_object_ids: Sequence[str] = (),
    ) -> JsonObject:
        """Create a complete assertion-entry object, without RFC 7464 framing.

        Journal append, locking, canonical serialization, and hash-chain mutation
        remain the responsibility of the Riverhog provenance journal writer.
        """

        from .common import new_urn_uuid, utc_now
        from .constants import PROVENANCE_ENTRY_SCHEMA, PROVENANCE_PROFILE

        if sequence < 1:
            raise ValueError("assertion entries require sequence >= 1")
        if previous_sequence is None:
            previous_sequence = sequence - 1
        entry: JsonObject = {
            "$schema": PROVENANCE_ENTRY_SCHEMA,
            "profile": PROVENANCE_PROFILE,
            "schema_version": "1.0.0",
            "id": entry_id or new_urn_uuid(),
            "type": "riverhog_provenance_journal_entry",
            "journal_id": journal_id,
            "sequence": sequence,
            "recorded_at": recorded_at or utc_now(),
            "recorded_by_agent_id": recorded_by_agent_id
            or self.capture["associations"][0]["agent_id"],
            "recording_environment_id": self.environment["id"],
            "entry_kind": "assertion",
            "previous_entry": {
                "entry_id": previous_entry_id,
                "sequence": previous_sequence,
                "json_sha256": previous_entry_json_sha256,
            },
            "body": self.assertion_body(omit_object_ids=omit_object_ids),
        }
        if notes:
            entry["notes"] = list(notes)
        return entry
