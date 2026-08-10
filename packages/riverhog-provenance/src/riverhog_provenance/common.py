from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import locale
import os
import secrets
import stat as statmod
import time
import urllib.parse
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from enum import Enum
from typing import Any, cast

from .constants import (
    CAPTURE_PLAN_ID,
    COVERAGE_CATEGORIES,
    DEFAULT_OBSERVER_AGENT_ID,
    PACKAGE_NAME,
    PACKAGE_VERSION,
)
from .errors import SymlinkRefusedError, UnstableFileError, UnsupportedFileTypeError
from .interface import PlatformBackend
from .model import (
    ExtensionDraft,
    JsonObject,
    LargeValueDisposition,
    NativeStat,
    ObservationRequest,
    ObservationResult,
)

UTC = dt.UTC


def new_urn_uuid() -> str:
    """Return a UUIDv7 URN without depending on a particular Python minor."""

    if hasattr(uuid, "uuid7"):
        uuid7 = cast(Any, uuid).uuid7
        return f"urn:uuid:{uuid7()}"
    timestamp_ms = time.time_ns() // 1_000_000
    if timestamp_ms >= (1 << 48):
        raise OverflowError("current Unix time does not fit UUIDv7")
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (timestamp_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return f"urn:uuid:{uuid.UUID(int=value)}"


def require_urn_uuid(value: str, field: str) -> str:
    prefix = "urn:uuid:"
    if not value.startswith(prefix):
        raise ValueError(f"{field} must be a lowercase UUID URN")
    try:
        parsed = uuid.UUID(value[len(prefix) :])
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid UUID URN") from exc
    canonical = f"urn:uuid:{parsed}"
    if value != canonical:
        raise ValueError(f"{field} must use canonical lowercase UUID URN syntax")
    return canonical


def provenance_journal_filename(journal_id: str) -> str:
    """Return the canonical single-segment filename for a provenance journal."""

    canonical = require_urn_uuid(journal_id, "journal_id")
    return f"{canonical}.json-seq"


def utc_now() -> str:
    return format_utc_ns(time.time_ns())


def format_utc_ns(epoch_ns: int) -> str:
    seconds, nanoseconds = divmod(epoch_ns, 1_000_000_000)
    moment = dt.datetime.fromtimestamp(seconds, tz=UTC)
    base = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if nanoseconds:
        fraction = f"{nanoseconds:09d}".rstrip("0")
        return f"{base}.{fraction}Z"
    return f"{base}Z"


def utc_offset_string() -> str:
    now = dt.datetime.now().astimezone()
    offset = now.utcoffset()
    if offset is None:
        return "+00:00"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def safe_portable_text(value: str) -> str:
    if "\x00" in value or any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        raise ValueError("text contains a JSON/PostgreSQL-incompatible character")
    return value


def path_bytes(path: str | bytes) -> bytes:
    return path if isinstance(path, bytes) else os.fsencode(path)


def bytes_display(data: bytes) -> str:
    return "bytes:" + urllib.parse.quote_from_bytes(data, safe="/-._~")


def portable_text_from_bytes(data: bytes) -> str:
    try:
        return safe_portable_text(data.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError):
        return bytes_display(data)


def locator_from_path(
    path: str | bytes,
    *,
    kind: str,
    authority_id: str | None = None,
) -> JsonObject:
    raw = path_bytes(path)
    try:
        text = raw.decode("utf-8", "strict")
        safe_portable_text(text)
        text_role = "exact"
        source_encoding = "UTF-8"
    except (UnicodeDecodeError, ValueError):
        text = bytes_display(raw)
        text_role = "display"
        source_encoding = None
    locator: JsonObject = {
        "syntax": "posix",
        "kind": kind,
        "text": text,
        "bytes": {
            "encoding": "base64",
            "data": base64.b64encode(raw).decode("ascii"),
            "byte_length": len(raw),
        },
        "text_role": text_role,
    }
    if source_encoding is not None:
        locator["source_encoding"] = source_encoding
    if authority_id is not None:
        locator["authority_id"] = authority_id
    return locator


def native_name_fields(name: bytes) -> JsonObject:
    encoded = {
        "encoding": "base64",
        "data": base64.b64encode(name).decode("ascii"),
        "byte_length": len(name),
    }
    try:
        text = safe_portable_text(name.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError):
        return {
            "name": bytes_display(name),
            "name_bytes": encoded,
            "name_role": "display",
        }
    return {
        "name": text,
        "name_bytes": encoded,
        "name_role": "exact",
        "name_source_encoding": "UTF-8",
    }


def source(platform: str, api: str, field: str | None = None) -> JsonObject:
    result: JsonObject = {"platform": platform, "api": api}
    if field is not None:
        result["field"] = field
    return result


def diagnostic(
    *,
    severity: str,
    category: str,
    code: str,
    message: str,
    native_code: str | None = None,
    source_descriptor: JsonObject | None = None,
) -> JsonObject:
    item: JsonObject = {
        "severity": severity,
        "category": category,
        "code": code,
        "message": message,
    }
    if native_code:
        item["native_code"] = native_code
    if source_descriptor:
        item["source"] = source_descriptor
    return item


def digest_assertion(
    value: str,
    *,
    agent_id: str,
    purpose: str = "fixity",
    algorithm: str = "sha-256",
) -> JsonObject:
    return {
        "algorithm": algorithm,
        "encoding": "hex",
        "value": value,
        "purpose": purpose,
        "originator_agent_id": agent_id,
    }


def bytes_value(data: bytes, *, agent_id: str | None = None) -> JsonObject:
    value: JsonObject = {
        "type": "bytes",
        "encoding": "base64",
        "data": base64.b64encode(data).decode("ascii"),
        "byte_length": len(data),
    }
    if agent_id is not None:
        value["digests"] = [
            digest_assertion(
                hashlib.sha256(data).hexdigest(),
                agent_id=agent_id,
                purpose="native_metadata",
            )
        ]
    return value


def digest_only_value(data: bytes, *, agent_id: str) -> JsonObject:
    return {
        "type": "digest",
        "byte_length": len(data),
        "digests": [
            digest_assertion(
                hashlib.sha256(data).hexdigest(),
                agent_id=agent_id,
                purpose="native_metadata",
            )
        ],
    }


def retained_native_value(
    data: bytes,
    *,
    agent_id: str,
    request: ObservationRequest,
) -> tuple[str, JsonObject | None, str | None]:
    policy = request.policy
    if len(data) <= policy.inline_native_value_bytes:
        return "captured", bytes_value(data, agent_id=agent_id), None
    if len(data) > policy.maximum_native_value_bytes:
        if policy.large_value_disposition is LargeValueDisposition.FAIL:
            raise ValueError(f"native value length {len(data)} exceeds configured maximum")
        return (
            "not_retained",
            None,
            "Value exceeded maximum_native_value_bytes and was not retained.",
        )
    if policy.large_value_disposition is LargeValueDisposition.DIGEST_ONLY:
        return "digest_only", digest_only_value(data, agent_id=agent_id), None
    if policy.large_value_disposition is LargeValueDisposition.NOT_RETAINED:
        return (
            "not_retained",
            None,
            "Value exceeded inline_native_value_bytes and policy forbids retention.",
        )
    raise ValueError("native value exceeds inline threshold")


def timestamp_observation(
    *,
    kind: str,
    epoch_ns: int,
    platform: str,
    api: str,
    field: str,
    resolution_ns: int = 1,
) -> JsonObject:
    return {
        "kind": kind,
        "value_status": "exact",
        "value": format_utc_ns(epoch_ns),
        "resolution_ns": max(1, resolution_ns),
        "source": source(platform, api, field),
        "raw_value": str(epoch_ns),
        "raw_unit": "nanoseconds",
        "raw_epoch": "1970-01-01T00:00:00Z",
    }


def identifier(
    *,
    scheme: str,
    value: str,
    scope: str,
    authority_id: str | None = None,
) -> JsonObject:
    result: JsonObject = {
        "scheme": scheme,
        "value": value,
        "scope": scope,
        "representation": "clear",
    }
    if authority_id is not None:
        result["authority_id"] = authority_id
    return result


def observed_identifier(
    *,
    scheme: str,
    value: str,
    scope: str,
    platform: str,
    api: str,
    field: str | None = None,
    authority_id: str | None = None,
) -> JsonObject:
    result = identifier(scheme=scheme, value=value, scope=scope, authority_id=authority_id)
    result["source"] = source(platform, api, field)
    return result


def resolve_principal(
    *,
    numeric_id: int,
    kind: str,
    host_id: str,
    attempt_resolution: bool,
) -> JsonObject:
    scheme = "posix-uid" if kind == "user" else "posix-gid"
    principal: JsonObject = {
        "kind": kind,
        "identifiers": [
            identifier(
                scheme=scheme,
                value=str(numeric_id),
                scope="host",
                authority_id=host_id,
            )
        ],
        "resolution": "not_attempted",
    }
    if not attempt_resolution:
        return principal
    try:
        if kind == "user":
            import pwd

            name = pwd.getpwuid(numeric_id).pw_name
        else:
            import grp

            name = grp.getgrgid(numeric_id).gr_name
    except (KeyError, ImportError):
        principal["resolution"] = "unresolved"
    else:
        principal["name"] = safe_portable_text(name)
        principal["resolution"] = "resolved"
    return principal


def basic_access(stat: NativeStat, request: ObservationRequest) -> JsonObject:
    return {
        "owner": resolve_principal(
            numeric_id=stat.uid,
            kind="user",
            host_id=request.host_id,
            attempt_resolution=request.policy.resolve_principals,
        ),
        "group": resolve_principal(
            numeric_id=stat.gid,
            kind="group",
            host_id=request.host_id,
            attempt_resolution=request.policy.resolve_principals,
        ),
        "posix_mode": f"{statmod.S_IMODE(stat.mode):04o}",
    }


def effective_principal(host_id: str) -> JsonObject:
    uid = os.geteuid() if hasattr(os, "geteuid") else 0
    return resolve_principal(
        numeric_id=uid,
        kind="user",
        host_id=host_id,
        attempt_resolution=True,
    )


def runtime_environment(host_id: str, *, include_principal: bool) -> JsonObject:
    runtime: JsonObject = {
        "process_architecture": os.uname().machine if hasattr(os, "uname") else "unknown",
        "time_zone": dt.datetime.now().astimezone().tzname() or "unknown",
        "utc_offset": utc_offset_string(),
        "character_encoding": locale.getpreferredencoding(False) or "unknown",
        "privilege": ("root" if hasattr(os, "geteuid") and os.geteuid() == 0 else "unprivileged"),
    }
    loc = locale.setlocale(locale.LC_CTYPE, None)
    if loc:
        runtime["locale"] = loc
    if include_principal and hasattr(os, "geteuid"):
        runtime["effective_principal"] = effective_principal(host_id)
    return runtime


def observer_agent(agent_id: str) -> JsonObject:
    return {
        "id": agent_id,
        "type": "software",
        "name": PACKAGE_NAME,
        "version": PACKAGE_VERSION,
        "vendor": "Riverhog",
    }


def hash_fd(fd: int, *, chunk_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, chunk_bytes)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def sparse_extents(
    fd: int, size: int, *, maximum_extents: int
) -> tuple[list[dict[str, int | str]], bool]:
    """Return SEEK_DATA/SEEK_HOLE extents and whether enumeration completed."""

    seek_data = getattr(os, "SEEK_DATA", 3)
    seek_hole = getattr(os, "SEEK_HOLE", 4)
    if size == 0:
        return [], True
    extents: list[dict[str, int | str]] = []
    position = 0
    complete = True
    while position < size:
        if len(extents) >= maximum_extents:
            complete = False
            break
        try:
            data_offset = os.lseek(fd, position, seek_data)
        except OSError as exc:
            if exc.errno in {6, 61}:  # ENXIO, platform variants
                if position < size:
                    extents.append({"kind": "hole", "offset": position, "length": size - position})
                break
            raise
        if data_offset > position:
            extents.append({"kind": "hole", "offset": position, "length": data_offset - position})
            if len(extents) >= maximum_extents:
                complete = False
                break
        try:
            hole_offset = os.lseek(fd, data_offset, seek_hole)
        except OSError as exc:
            if exc.errno in {6, 61}:  # no later hole
                hole_offset = size
            else:
                raise
        hole_offset = min(hole_offset, size)
        if hole_offset > data_offset:
            extents.append(
                {
                    "kind": "data",
                    "offset": data_offset,
                    "length": hole_offset - data_offset,
                }
            )
        if hole_offset <= position:
            complete = False
            break
        position = hole_offset
    os.lseek(fd, 0, os.SEEK_SET)
    return extents, complete


def make_sparse_map_row(
    extents: list[dict[str, int | str]],
    *,
    platform: str,
    agent_id: str,
    complete: bool,
) -> JsonObject:
    return {
        "kind": "sparse_map",
        "coverage_category": "storage_layout",
        "namespace": platform,
        "name": "data-and-hole-extents",
        "capture_status": "captured",
        "source": source(platform, "lseek(2)", "SEEK_DATA/SEEK_HOLE"),
        "value": {
            "type": "json",
            "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/sparse-map.json",
            "data": {"complete": complete, "extents": extents},
        },
        "interpretations": [
            {
                "kind": "structured_parse",
                "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/sparse-map.json",
                "value": {
                    "type": "json",
                    "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/sparse-map.json",
                    "data": {"complete": complete, "extents": extents},
                },
                "agent_id": agent_id,
                "confidence": "high",
            }
        ],
        "sensitivity": "public",
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def observation_policy_document(request: ObservationRequest) -> JsonObject:
    return cast(JsonObject, _jsonable(asdict(request.policy)))


def semantic_assertion_from_draft(
    draft: ExtensionDraft,
    *,
    subject_ids: Mapping[str, str],
    agent_id: str,
) -> JsonObject:
    try:
        subject_id = subject_ids[draft.subject_role]
    except KeyError as exc:
        raise ValueError(f"unknown extension subject role: {draft.subject_role}") from exc
    assertion: JsonObject = {
        "id": new_urn_uuid(),
        "type": "semantic_assertion",
        "subject_id": subject_id,
        "property": draft.property,
        "value": draft.value,
        "asserted_by_agent_id": agent_id,
        "confidence": draft.confidence,
    }
    if draft.note:
        assertion["note"] = draft.note
    return assertion


def default_coverage() -> dict[str, str]:
    coverage = {name: "not_requested" for name in COVERAGE_CATEGORIES}
    coverage.update(
        {
            "content_fixity": "complete",
            "locator": "complete",
            "basic_filesystem": "complete",
        }
    )
    return coverage


def merge_coverage(coverage: dict[str, str], category: str, status: str) -> None:
    """Merge independent capture attempts without hiding a partial failure."""

    priority = {
        "not_requested": 0,
        "not_applicable": 1,
        "not_supported": 2,
        "complete": 3,
        "partial": 4,
        "failed": 5,
    }
    current = coverage.get(category)
    if current is None or priority[status] > priority[current]:
        coverage[category] = status


def combine_coverage(coverage: Mapping[str, str]) -> dict[str, str]:
    result = default_coverage()
    result.update(coverage)
    return result


def capture_outcome(coverage: Mapping[str, str], diagnostics: Iterable[JsonObject]) -> str:
    if any(value in {"partial", "failed"} for value in coverage.values()):
        return "partial"
    if any(item.get("severity") == "error" for item in diagnostics):
        return "partial"
    return "success"


def _lstat_without_follow(path: str | bytes) -> os.stat_result:
    try:
        result = os.lstat(path)
    except FileNotFoundError:
        raise
    if statmod.S_ISLNK(result.st_mode):
        raise SymlinkRefusedError("refusing to observe a symbolic-link final component")
    return result


class DescriptorFileStateObserver:
    """Platform-independent implementation of the shared observer interface."""

    platform_family: str

    def __init__(self, backend: PlatformBackend) -> None:
        self.backend = backend
        self.platform_family = backend.platform_family

    def observe(self, request: ObservationRequest) -> ObservationResult:
        self.backend.assert_supported()
        require_urn_uuid(request.lineage_id, "lineage_id")
        require_urn_uuid(request.host_id, "host_id")
        observer_agent_id = require_urn_uuid(
            request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID,
            "observer_agent_id",
        )
        state_id = require_urn_uuid(request.state_id or new_urn_uuid(), "state_id")
        capture_id = require_urn_uuid(request.capture_id or new_urn_uuid(), "capture_id")
        environment_id = require_urn_uuid(
            request.environment_id or new_urn_uuid(), "environment_id"
        )
        host_entity_id = require_urn_uuid(
            request.host_entity_id or new_urn_uuid(), "host_entity_id"
        )
        absolute = self.backend.absolute_path(request.path)
        self.backend.preflight_path(absolute)

        started_at = utc_now()
        fd = -1
        open_diagnostics: list[JsonObject] = []
        noatime_effective = False
        try:
            fd, open_diagnostics, noatime_effective = self.backend.open_readonly(absolute, request)
            before = self.backend.stat_fd(fd)
            if not statmod.S_ISREG(before.mode):
                raise UnsupportedFileTypeError("opened target is not a regular file")
            if not self.backend.path_matches(absolute, before):
                raise UnstableFileError("path changed between preflight and descriptor open")

            content_sha256, bytes_read = hash_fd(fd, chunk_bytes=request.policy.hash_chunk_bytes)

            post_read = self.backend.stat_fd(fd)
            collection = self.backend.collect(fd, absolute, post_read, request)
            second_digest: str | None = None
            second_size: int | None = None
            if request.policy.second_content_hash:
                second_digest, second_size = hash_fd(
                    fd, chunk_bytes=request.policy.hash_chunk_bytes
                )

            # This is the authoritative end-of-observation stat.  It follows
            # every primary-stream read and every native metadata operation,
            # including the optional verification hash.
            after = self.backend.stat_fd(fd)
            # A read length must agree with both endpoints.  Comparing only the
            # first and final stat snapshots can miss a truncate-and-restore race.
            primary_size_mismatch = bytes_read != before.size or bytes_read != after.size
            if primary_size_mismatch:
                collection.diagnostics.append(
                    diagnostic(
                        severity="error",
                        category="content_fixity",
                        code="primary_read_size_mismatch",
                        message=(
                            f"Read {bytes_read} primary-stream bytes; the initial and "
                            f"final sizes were {before.size} and {after.size}."
                        ),
                    )
                )

            second_hash_mismatch = bool(
                request.policy.second_content_hash
                and (second_size != after.size or second_digest != content_sha256)
            )
            if second_hash_mismatch:
                collection.diagnostics.append(
                    diagnostic(
                        severity="error",
                        category="content_fixity",
                        code="second_hash_mismatch",
                        message=(
                            "A second primary-stream read did not match the first "
                            "digest and final byte length."
                        ),
                    )
                )

            # Reading may legitimately advance atime, which is deliberately
            # excluded from the stability key.  Each backend retains the native
            # representation while updating the final observed value.
            self.backend.finalize_timestamps(collection, after, request)

            differences = self.backend.stability_differences(before, after)
            path_differences: list[str] = []
            if request.policy.verify_path_binding and not self.backend.path_matches(
                absolute, after
            ):
                path_differences.append("path_identity")

            if primary_size_mismatch or second_hash_mismatch:
                reasons: list[str] = []
                if primary_size_mismatch:
                    reasons.append("primary_read_size")
                if second_hash_mismatch:
                    reasons.append("second_content_read")
                # Riverhog provenance requires complete primary-stream fixity for every state.
                # A proven incomplete or inconsistent content read cannot be emitted
                # even under best-effort metadata consistency.
                raise UnstableFileError(
                    "content changed or could not be read completely: " + ", ".join(reasons)
                )

            instability_reasons = differences + path_differences
            unstable = bool(instability_reasons)
            if unstable and request.policy.strict_consistency:
                raise UnstableFileError(
                    "file state changed during strict observation: "
                    + ", ".join(instability_reasons)
                )
            if unstable:
                collection.diagnostics.append(
                    diagnostic(
                        severity="error",
                        category="basic_filesystem",
                        code="state_changed_during_observation",
                        message="File state changed during observation: "
                        + ", ".join(instability_reasons),
                    )
                )
                collection.coverage["native_metadata_other"] = "partial"

            if request.policy.include_access_time and not noatime_effective:
                collection.diagnostics.append(
                    diagnostic(
                        severity="warning",
                        category="timestamps",
                        code="access_time_may_be_observer_affected",
                        message=(
                            "The descriptor was not opened with an effective no-atime "
                            "guarantee. The recorded access time is the post-read value."
                        ),
                    )
                )

            if collection.environment is None:
                raise RuntimeError("platform backend did not produce an environment")
            collection.environment["id"] = environment_id
            host_object = collection.environment.get("host")
            if not isinstance(host_object, dict):
                raise RuntimeError("platform backend did not produce a host object")
            host_object["id"] = host_entity_id
            stable_host_identifier = identifier(
                scheme="riverhog-host-authority",
                value=request.host_id,
                scope="global",
            )
            existing_host_identifiers = host_object.get("identifiers")
            if isinstance(existing_host_identifiers, list):
                if stable_host_identifier not in existing_host_identifiers:
                    existing_host_identifiers.append(stable_host_identifier)
            else:
                host_object["identifiers"] = [stable_host_identifier]

            filesystem_object = collection.environment.get("filesystem")
            if isinstance(filesystem_object, dict):
                mount_locator = filesystem_object.get("mount_locator")
                if isinstance(mount_locator, dict):
                    # A locator authority is an Entity that supplies the locator's
                    # naming context.  The durable host UUID scopes native numeric
                    # identifiers, while this environment's Host Entity scopes paths.
                    mount_locator["authority_id"] = host_entity_id

            # Core metadata is built from the post-read native stat snapshot. This
            # gives a coherent end-of-capture state while stability checks establish
            # that content and state-defining fields did not move during acquisition.
            if collection.access is None and self.platform_family != "windows":
                collection.access = basic_access(after, request)
            coverage = combine_coverage(collection.coverage)
            diagnostics = open_diagnostics + collection.diagnostics
            outcome = capture_outcome(coverage, diagnostics)
            consistency = "best_effort" if unstable else "verified_unchanged"

            filesystem_metadata: JsonObject = {
                "timestamps": collection.timestamps,
                "native_identifiers": collection.native_identifiers,
                "native_metadata": collection.native_metadata,
            }
            if collection.access is not None:
                filesystem_metadata["access"] = collection.access
            state: JsonObject = {
                "id": state_id,
                "type": "regular_file_state",
                "lineage_id": request.lineage_id,
                "locator": self.backend.locator(
                    absolute, kind="absolute", authority_id=host_entity_id
                ),
                "content": {
                    "size_bytes": after.size,
                    "digests": [
                        digest_assertion(
                            content_sha256,
                            agent_id=observer_agent_id,
                            purpose="fixity",
                        )
                    ],
                },
                "filesystem_metadata": filesystem_metadata,
            }
            if request.notes:
                state["notes"] = list(request.notes)

            associations: list[JsonObject] = [
                {
                    "agent_id": observer_agent_id,
                    "role": "executing_software",
                    "plan_id": CAPTURE_PLAN_ID,
                }
            ]
            associations.extend(dict(item) for item in request.additional_associations)
            operations = ["metadata_extraction", "message_digest_calculation"]
            if request.policy.resolve_principals:
                operations.append("principal_resolution")
            policy_document = observation_policy_document(request)
            policy_digest = hashlib.sha256(canonical_json(policy_document)).hexdigest()
            capture: JsonObject = {
                "id": capture_id,
                "type": "file_state_capture",
                "state_id": state_id,
                "operations": operations,
                "started_at": started_at,
                "ended_at": utc_now(),
                "outcome": outcome,
                "consistency": consistency,
                "environment_id": environment_id,
                "associations": associations,
                "coverage": coverage,
                "detail": {
                    "profile_id": CAPTURE_PLAN_ID,
                    "configuration_digest": digest_assertion(
                        policy_digest,
                        agent_id=observer_agent_id,
                        purpose="configuration",
                    ),
                },
            }
            if diagnostics:
                capture["diagnostics"] = diagnostics

            agents: list[JsonObject] = [observer_agent(observer_agent_id)]
            agents.extend(dict(item) for item in request.additional_agents)

            bindings: list[JsonObject] = []
            if request.payload_binding is not None and not unstable:
                binding_id = require_urn_uuid(request.binding_id or new_urn_uuid(), "binding_id")
                relative = request.payload_binding.relative_path
                if relative is None:
                    relative = self.backend.path_basename(absolute)
                relative_raw = os.fspath(relative)
                if self.backend.path_is_absolute(relative_raw):
                    raise ValueError("payload binding path must be relative")
                predecessor_binding_id: str | None = None
                if request.payload_binding.replaces_binding_id is not None:
                    replaced_id = require_urn_uuid(
                        request.payload_binding.replaces_binding_id,
                        "replaces_binding_id",
                    )
                    predecessor_binding_id = new_urn_uuid()
                    bindings.append(
                        {
                            "id": predecessor_binding_id,
                            "type": "payload_binding",
                            "operation": "unbind",
                            "role": request.payload_binding.role,
                            "replaces_binding_id": replaced_id,
                            "asserted_by_agent_id": observer_agent_id,
                            "note": "Retires the previous effective payload binding.",
                        }
                    )
                binding: JsonObject = {
                    "id": binding_id,
                    "type": "payload_binding",
                    "operation": "bind",
                    "role": request.payload_binding.role,
                    "state": {"id": state_id, "scope": "local"},
                    "relative_payload_locator": locator_from_path(relative_raw, kind="relative"),
                    "established_by_capture_id": capture_id,
                    "basis": "size_and_sha256",
                    "asserted_by_agent_id": observer_agent_id,
                }
                if predecessor_binding_id is not None:
                    # Payload-binding events form a strict predecessor chain per
                    # role.  The new bind follows the unbind generated above.
                    binding["replaces_binding_id"] = predecessor_binding_id
                if request.payload_binding.note is not None:
                    binding["note"] = request.payload_binding.note
                bindings.append(binding)

            extension_drafts = [
                ExtensionDraft(
                    subject_role="capture",
                    property="https://nashspence.github.io/riverhog/v1/provenance/observers/vocab/observation-policy",
                    value={
                        "type": "json",
                        "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/observation-policy.json",
                        "data": policy_document,
                    },
                    note="Exact policy whose canonical JSON SHA-256 is recorded in capture.detail.",
                ),
                *collection.extension_drafts,
            ]
            subject_ids = {
                "state": state_id,
                "capture": capture_id,
                "environment": environment_id,
            }
            extensions = tuple(
                semantic_assertion_from_draft(
                    draft, subject_ids=subject_ids, agent_id=observer_agent_id
                )
                for draft in extension_drafts
            )

            return ObservationResult(
                state=state,
                capture=capture,
                environment=collection.environment,
                agents=tuple(agents),
                payload_bindings=tuple(bindings),
                extensions=extensions,
            )
        finally:
            if fd >= 0:
                self.backend.release_fd(fd)
                os.close(fd)


def canonical_json(document: Mapping[str, Any]) -> bytes:
    """Deterministic compact JSON useful for tests and handoff to journal tools."""

    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
