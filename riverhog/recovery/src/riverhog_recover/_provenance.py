from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

ENTRY_SCHEMA = "https://nashspence.github.io/riverhog/v1/provenance/journal-entry.schema.json"
PROFILE = "https://nashspence.github.io/riverhog/v1/provenance"
INDEX_SCHEMA = "riverhog-provenance-index/v1"
SET_SCHEMA = "riverhog-provenance-set/v1"
BUNDLE_FORMAT = "riverhog-provenance-bundle/v1"
ENTRY_TYPE = "riverhog_provenance_journal_entry"
PRIMARY_PAYLOAD_ROLE = "co_resident_primary_payload"
RS = b"\x1e"
LF = b"\n"
MAX_BUNDLE_JOURNALS = 256
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ProvenanceRecoveryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FileProvenanceBinding:
    path: str
    bytes: int
    sha256: str
    status: Literal["captured", "omitted"]
    journal_id: str | None = None
    current_state_id: str | None = None
    omission_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _JournalFrame:
    document: dict[str, Any]
    json_sha256: str


@dataclass(frozen=True, slots=True)
class _ExternalState:
    journal_id: str
    entry_id: str
    entry_json_sha256: str
    state_id: str


@dataclass(frozen=True, slots=True)
class _JournalSummary:
    journal_id: str
    frames: tuple[_JournalFrame, ...]
    current_state_id: str
    current_path: str
    current_bytes: int
    current_sha256: str
    external_states: tuple[_ExternalState, ...]


@dataclass(frozen=True, slots=True)
class ValidatedProvenance:
    bindings: tuple[FileProvenanceBinding, ...]
    journal_bytes: dict[str, bytes]
    identity: str


def validate_provenance_archive(
    index_content: bytes,
    bundles: Mapping[str, bytes],
) -> ValidatedProvenance:
    payload = _strict_json_object(
        index_content,
        label="provenance index",
        trailing_lf=True,
    )
    if set(payload) != {"schema", "bundle_format", "files", "bundles", "journals"}:
        raise ProvenanceRecoveryError("provenance index fields are invalid")
    if payload.get("schema") != INDEX_SCHEMA or payload.get("bundle_format") != BUNDLE_FORMAT:
        raise ProvenanceRecoveryError("provenance index schema mismatch")

    bundle_rows = _object_list(payload.get("bundles"), "provenance bundles")
    expected_bundles: dict[str, dict[str, Any]] = {}
    for sequence, row in enumerate(bundle_rows):
        bundle_id = f"bundle-{sequence:012d}"
        if set(row) != {"id", "path", "plaintext_bytes", "sha256", "journals"}:
            raise ProvenanceRecoveryError("provenance bundle descriptor is invalid")
        if row.get("id") != bundle_id or row.get("path") != f"provenance/{bundle_id}.tar.age":
            raise ProvenanceRecoveryError("provenance bundle descriptor is not canonical")
        bundle_bytes = _nonnegative_int(row.get("plaintext_bytes"), "bundle plaintext bytes")
        journal_count = _positive_int(row.get("journals"), "bundle journal count")
        if bundle_bytes > MAX_BUNDLE_BYTES or journal_count > MAX_BUNDLE_JOURNALS:
            raise ProvenanceRecoveryError("provenance bundle exceeds the v1 limit")
        _sha256(row.get("sha256"), "bundle SHA-256")
        expected_bundles[bundle_id] = row
    if set(bundles) != set(expected_bundles):
        raise ProvenanceRecoveryError("provenance bundle set does not match the index")

    extracted: dict[str, bytes] = {}
    bundle_members: dict[str, dict[str, bytes]] = {}
    for bundle_id, row in expected_bundles.items():
        content = bundles[bundle_id]
        if len(content) != row["plaintext_bytes"] or _digest(content) != row["sha256"]:
            raise ProvenanceRecoveryError(f"provenance bundle identity mismatch: {bundle_id}")
        members = _read_bundle(content)
        if len(members) != row["journals"]:
            raise ProvenanceRecoveryError(f"provenance bundle journal count mismatch: {bundle_id}")
        bundle_members[bundle_id] = members
        for member, journal in members.items():
            journal_id = _journal_id_from_member(member)
            if journal_id in extracted:
                raise ProvenanceRecoveryError(f"provenance repeats journal {journal_id}")
            extracted[journal_id] = journal

    journal_rows = _object_list(payload.get("journals"), "provenance journals")
    expected_journal_ids: set[str] = set()
    for row in journal_rows:
        if set(row) != {"journal_id", "bundle_id", "member", "bytes", "sha256"}:
            raise ProvenanceRecoveryError("provenance journal descriptor is invalid")
        journal_id = _urn_uuid(row.get("journal_id"), "journal ID")
        bundle_id = str(row.get("bundle_id") or "")
        member = f"journals/{journal_id}.json-seq"
        journal_content = extracted.get(journal_id)
        if (
            journal_id in expected_journal_ids
            or row.get("member") != member
            or bundle_id not in bundle_members
            or member not in bundle_members[bundle_id]
            or journal_content is None
            or len(journal_content) != _nonnegative_int(row.get("bytes"), "journal bytes")
            or _digest(journal_content) != _sha256(row.get("sha256"), "journal SHA-256")
        ):
            raise ProvenanceRecoveryError(f"provenance journal identity mismatch: {journal_id}")
        expected_journal_ids.add(journal_id)
    if expected_journal_ids != set(extracted):
        raise ProvenanceRecoveryError("provenance bundle contains an unindexed journal")

    bindings = _parse_bindings(payload.get("files"))
    _validate_bindings(bindings, extracted)
    return ValidatedProvenance(
        bindings=bindings,
        journal_bytes=extracted,
        identity=_digest(index_content),
    )


def build_portable_provenance_set(
    *,
    bindings: Sequence[FileProvenanceBinding],
    journals: Mapping[str, bytes],
) -> bytes:
    normalized = tuple(sorted(bindings, key=lambda item: item.path.encode("utf-8")))
    _validate_bindings(normalized, journals)
    rows: list[dict[str, Any]] = []
    for item in normalized:
        row: dict[str, Any] = {
            "path": item.path,
            "bytes": item.bytes,
            "sha256": item.sha256,
            "status": item.status,
        }
        if item.status == "captured":
            row.update({"journal_id": item.journal_id, "current_state_id": item.current_state_id})
        else:
            row["omission_reason"] = item.omission_reason
        rows.append(row)
    return (
        _canonical_json(
            {
                "schema": SET_SCHEMA,
                "files": rows,
                "journals": [
                    {
                        "journal_id": journal_id,
                        "path": f"journals/{journal_id}.json-seq",
                        "bytes": len(journals[journal_id]),
                        "sha256": _digest(journals[journal_id]),
                    }
                    for journal_id in sorted(journals)
                ],
            }
        )
        + LF
    )


def _validate_bindings(
    bindings: Sequence[FileProvenanceBinding], journals: Mapping[str, bytes]
) -> None:
    if not bindings or len({item.path for item in bindings}) != len(bindings):
        raise ProvenanceRecoveryError("provenance must account for each file exactly once")
    summaries = _validate_journal_set(journals)
    directly_bound: set[str] = set()
    for item in bindings:
        _relative_path(item.path)
        _nonnegative_int(item.bytes, "file bytes")
        _sha256(item.sha256, "file SHA-256")
        if item.status == "captured":
            journal_id = _urn_uuid(item.journal_id, "journal ID")
            summary = summaries.get(journal_id)
            if summary is None or item.current_state_id != summary.current_state_id:
                raise ProvenanceRecoveryError(f"captured file state is unresolved: {item.path}")
            if (
                summary.current_path != item.path
                or summary.current_bytes != item.bytes
                or summary.current_sha256 != item.sha256
            ):
                raise ProvenanceRecoveryError(
                    "current provenance state does not bind to the payload path, size, and SHA-256"
                )
            directly_bound.add(journal_id)
        elif item.status == "omitted":
            if item.journal_id is not None or item.current_state_id is not None:
                raise ProvenanceRecoveryError("omitted provenance cannot bind a journal")
            if not item.omission_reason or item.omission_reason != item.omission_reason.strip():
                raise ProvenanceRecoveryError("provenance omission requires a visible reason")
        else:
            raise ProvenanceRecoveryError("file provenance status is invalid")
    reachable = set(directly_bound)
    pending = list(directly_bound)
    while pending:
        for reference in summaries[pending.pop()].external_states:
            if reference.journal_id not in reachable:
                reachable.add(reference.journal_id)
                pending.append(reference.journal_id)
    if reachable != set(summaries):
        raise ProvenanceRecoveryError("provenance contains an unreachable journal")


def _validate_journal_set(journals: Mapping[str, bytes]) -> dict[str, _JournalSummary]:
    summaries: dict[str, _JournalSummary] = {}
    for declared_id, content in sorted(journals.items()):
        summary = _validate_journal(content)
        if summary.journal_id != declared_id:
            raise ProvenanceRecoveryError(
                f"journal key {declared_id} does not match {summary.journal_id}"
            )
        summaries[declared_id] = summary
    for summary in summaries.values():
        for reference in summary.external_states:
            target = summaries.get(reference.journal_id)
            if target is None:
                raise ProvenanceRecoveryError("provenance has an unresolved ancestor journal")
            frame = next(
                (item for item in target.frames if item.document.get("id") == reference.entry_id),
                None,
            )
            if frame is None or frame.json_sha256 != reference.entry_json_sha256:
                raise ProvenanceRecoveryError("provenance has an invalid external commitment")
            states = {
                _required_string(state, "id")
                for current in target.frames
                for state in _object_rows(_entry_assertions(current.document), "states")
            }
            if reference.state_id not in states:
                raise ProvenanceRecoveryError("provenance references an absent external state")
    return summaries


def _validate_journal(content: bytes) -> _JournalSummary:
    frames = _parse_journal(content)
    journal_id = _urn_uuid(frames[0].document.get("journal_id"), "journal ID")
    if frames[0].document.get("entry_kind") != "journal_init":
        raise ProvenanceRecoveryError("journal sequence zero must initialize the journal")
    states: dict[str, dict[str, Any]] = {}
    active_bindings: dict[str, dict[str, Any]] = {}
    external: dict[tuple[str, str, str, str], _ExternalState] = {}
    primary_lineage_id = ""
    entry_ids: set[str] = set()
    for sequence, frame in enumerate(frames):
        document = frame.document
        _validate_entry_shape(document, sequence=sequence)
        if document.get("journal_id") != journal_id:
            raise ProvenanceRecoveryError("journal entry changes journal identity")
        entry_id = _urn_uuid(document.get("id"), "entry ID")
        if entry_id in entry_ids:
            raise ProvenanceRecoveryError("journal repeats an entry identity")
        entry_ids.add(entry_id)
        if sequence:
            previous = frames[sequence - 1]
            if document.get("previous_entry") != {
                "entry_id": previous.document["id"],
                "sequence": sequence - 1,
                "json_sha256": previous.json_sha256,
            }:
                raise ProvenanceRecoveryError("journal entry does not commit to its predecessor")
        assertions = _entry_assertions(document)
        if sequence == 0:
            body = document["body"]
            journal = body.get("journal") if isinstance(body, dict) else None
            if not isinstance(journal, dict):
                raise ProvenanceRecoveryError("journal initialization has no policy")
            primary_lineage_id = _urn_uuid(journal.get("primary_lineage_id"), "lineage ID")
        for state in _object_rows(assertions, "states"):
            state_id = _urn_uuid(state.get("id"), "state ID")
            previous_state = states.get(state_id)
            if previous_state is not None and previous_state != state:
                raise ProvenanceRecoveryError("journal redefines a state")
            states[state_id] = state
        for binding in _object_rows(assertions, "payload_bindings"):
            role = _required_string(binding, "role")
            if binding.get("operation") == "unbind":
                active_bindings.pop(role, None)
            elif binding.get("operation") == "bind":
                active_bindings[role] = binding
            else:
                raise ProvenanceRecoveryError("payload binding operation is invalid")
        for reference in _external_state_references(assertions):
            external[
                (
                    reference.journal_id,
                    reference.entry_id,
                    reference.entry_json_sha256,
                    reference.state_id,
                )
            ] = reference

    primary = active_bindings.get(PRIMARY_PAYLOAD_ROLE)
    if primary is None:
        raise ProvenanceRecoveryError("journal has no current primary payload binding")
    state_reference = primary.get("state")
    if not isinstance(state_reference, dict) or state_reference.get("scope") != "local":
        raise ProvenanceRecoveryError("current primary payload binding is not local")
    current_state_id = _urn_uuid(state_reference.get("id"), "state ID")
    current_state = states.get(current_state_id)
    if current_state is None or current_state.get("lineage_id") != primary_lineage_id:
        raise ProvenanceRecoveryError("current payload state is outside the primary lineage")
    locator = primary.get("relative_payload_locator")
    if not isinstance(locator, dict):
        raise ProvenanceRecoveryError("current payload binding has no relative locator")
    current_path = _required_string(locator, "text")
    current_bytes, current_sha256 = _state_content_identity(current_state)
    return _JournalSummary(
        journal_id=journal_id,
        frames=frames,
        current_state_id=current_state_id,
        current_path=current_path,
        current_bytes=current_bytes,
        current_sha256=current_sha256,
        external_states=tuple(external.values()),
    )


def _parse_journal(content: bytes) -> tuple[_JournalFrame, ...]:
    if not content.startswith(RS):
        raise ProvenanceRecoveryError("journal must begin with an RFC 7464 record separator")
    frames: list[_JournalFrame] = []
    for sequence, chunk in enumerate(content.split(RS)[1:]):
        if not chunk.endswith(LF):
            raise ProvenanceRecoveryError(f"journal entry {sequence} has no terminating LF")
        json_bytes = chunk[:-1]
        document = _strict_json_object(json_bytes, label=f"journal entry {sequence}")
        frames.append(_JournalFrame(document=document, json_sha256=_digest(json_bytes)))
    if not frames:
        raise ProvenanceRecoveryError("journal must contain at least one entry")
    return tuple(frames)


def _validate_entry_shape(document: Mapping[str, Any], *, sequence: int) -> None:
    required = {
        "$schema",
        "profile",
        "schema_version",
        "id",
        "type",
        "journal_id",
        "sequence",
        "recorded_at",
        "recorded_by_agent_id",
        "entry_kind",
        "body",
    }
    optional = {"recording_environment_id", "previous_entry", "notes"}
    if not required <= set(document) or not set(document) <= required | optional:
        raise ProvenanceRecoveryError("journal entry fields are invalid")
    if (
        document.get("$schema") != ENTRY_SCHEMA
        or document.get("profile") != PROFILE
        or document.get("schema_version") != "1.0.0"
        or document.get("type") != ENTRY_TYPE
        or document.get("sequence") != sequence
        or not isinstance(document.get("body"), dict)
    ):
        raise ProvenanceRecoveryError("journal entry does not satisfy the v1 envelope")
    _urn_uuid(document.get("journal_id"), "journal ID")
    _urn_uuid(document.get("recorded_by_agent_id"), "recording agent ID")
    if "recording_environment_id" in document:
        _urn_uuid(document.get("recording_environment_id"), "environment ID")
    kind = document.get("entry_kind")
    if kind not in {"journal_init", "assertion", "correction", "checkpoint"}:
        raise ProvenanceRecoveryError("journal entry kind is invalid")
    if (sequence == 0) != (kind == "journal_init"):
        raise ProvenanceRecoveryError("journal initialization sequence is invalid")
    if sequence == 0 and "previous_entry" in document:
        raise ProvenanceRecoveryError("journal initialization has a predecessor")
    if sequence > 0 and "previous_entry" not in document:
        raise ProvenanceRecoveryError("journal continuation has no predecessor")


def _parse_bindings(value: object) -> tuple[FileProvenanceBinding, ...]:
    result: list[FileProvenanceBinding] = []
    for row in _object_list(value, "provenance files"):
        common = {"path", "bytes", "sha256", "status"}
        status = row.get("status")
        if status == "captured" and set(row) == common | {"journal_id", "current_state_id"}:
            result.append(
                FileProvenanceBinding(
                    path=str(row.get("path") or ""),
                    bytes=_nonnegative_int(row.get("bytes"), "file bytes"),
                    sha256=_sha256(row.get("sha256"), "file SHA-256"),
                    status="captured",
                    journal_id=str(row.get("journal_id") or ""),
                    current_state_id=str(row.get("current_state_id") or ""),
                )
            )
        elif status == "omitted" and set(row) == common | {"omission_reason"}:
            result.append(
                FileProvenanceBinding(
                    path=str(row.get("path") or ""),
                    bytes=_nonnegative_int(row.get("bytes"), "file bytes"),
                    sha256=_sha256(row.get("sha256"), "file SHA-256"),
                    status="omitted",
                    omission_reason=str(row.get("omission_reason") or ""),
                )
            )
        else:
            raise ProvenanceRecoveryError("provenance file binding is invalid")
    ordered = tuple(sorted(result, key=lambda item: item.path.encode("utf-8")))
    if tuple(result) != ordered:
        raise ProvenanceRecoveryError("provenance file bindings are not canonical")
    return ordered


def _read_bundle(content: bytes) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            for info in archive.getmembers():
                if (
                    not info.isfile()
                    or info.name in result
                    or info.mode != 0o644
                    or info.mtime != 0
                    or info.uid != 0
                    or info.gid != 0
                    or info.uname
                    or info.gname
                ):
                    raise ProvenanceRecoveryError("provenance bundle member is not canonical")
                extracted = archive.extractfile(info)
                if extracted is None:
                    raise ProvenanceRecoveryError("provenance bundle member is unreadable")
                result[info.name] = extracted.read()
    except (tarfile.TarError, OSError) as exc:
        raise ProvenanceRecoveryError("provenance bundle is not a standard tar stream") from exc
    if _canonical_tar(tuple(result.items())) != content:
        raise ProvenanceRecoveryError("provenance bundle tar bytes are not canonical")
    return result


def _canonical_tar(members: Sequence[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, content in members:
            _journal_id_from_member(name)
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _entry_assertions(document: Mapping[str, Any]) -> Mapping[str, Any]:
    body = document.get("body")
    if not isinstance(body, dict):
        return {}
    key = "replacement" if document.get("entry_kind") == "correction" else "assertions"
    assertions = body.get(key)
    return assertions if isinstance(assertions, dict) else {}


def _object_rows(assertions: Mapping[str, Any], key: str) -> tuple[dict[str, Any], ...]:
    rows = assertions.get(key)
    if not isinstance(rows, list):
        return ()
    return tuple(item for item in rows if isinstance(item, dict))


def _state_content_identity(state: Mapping[str, Any]) -> tuple[int, str]:
    content = state.get("content")
    if not isinstance(content, dict):
        raise ProvenanceRecoveryError("file state has no content identity")
    byte_count = _nonnegative_int(content.get("size_bytes"), "file state byte count")
    digests = content.get("digests")
    if not isinstance(digests, list):
        raise ProvenanceRecoveryError("file state has no digest list")
    sha256 = next(
        (
            item.get("value")
            for item in digests
            if isinstance(item, dict)
            and item.get("algorithm") == "sha-256"
            and item.get("encoding") == "hex"
            and item.get("purpose") == "fixity"
        ),
        None,
    )
    return byte_count, _sha256(sha256, "file state SHA-256")


def _external_state_references(assertions: Mapping[str, Any]) -> tuple[_ExternalState, ...]:
    result: list[_ExternalState] = []
    for item in _walk_json(assertions):
        if item.get("scope") != "external":
            continue
        result.append(
            _ExternalState(
                journal_id=_urn_uuid(item.get("journal_id"), "external journal ID"),
                entry_id=_urn_uuid(item.get("entry_id"), "external entry ID"),
                entry_json_sha256=_sha256(item.get("entry_json_sha256"), "external entry SHA-256"),
                state_id=_urn_uuid(item.get("id"), "external state ID"),
            )
        )
    return tuple(result)


def _walk_json(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _strict_json_object(
    content: bytes,
    *,
    label: str,
    trailing_lf: bool = False,
) -> dict[str, Any]:
    try:
        value = json.loads(
            content,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProvenanceRecoveryError(f"{label} is not strict JSON") from exc
    expected = _canonical_json(value) + (LF if trailing_lf else b"")
    if not isinstance(value, dict) or expected != content:
        raise ProvenanceRecoveryError(f"{label} is not canonical JSON")
    return cast(dict[str, Any], value)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _unique_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value}")


def _object_list(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ProvenanceRecoveryError(f"{label} must be an object list")
    return cast(list[dict[str, Any]], value)


def _required_string(value: Mapping[str, Any], key: str) -> str:
    current = value.get(key)
    if not isinstance(current, str) or not current:
        raise ProvenanceRecoveryError(f"required string {key!r} is absent")
    return current


def _urn_uuid(value: object, label: str) -> str:
    text = str(value or "")
    if not text.startswith("urn:uuid:"):
        raise ProvenanceRecoveryError(f"{label} is invalid")
    try:
        parsed = uuid.UUID(text.removeprefix("urn:uuid:"))
    except ValueError as exc:
        raise ProvenanceRecoveryError(f"{label} is invalid") from exc
    if text != f"urn:uuid:{parsed}":
        raise ProvenanceRecoveryError(f"{label} is not canonical")
    return text


def _journal_id_from_member(member: str) -> str:
    prefix = "journals/"
    suffix = ".json-seq"
    if not member.startswith(prefix) or not member.endswith(suffix):
        raise ProvenanceRecoveryError("provenance bundle member path is invalid")
    return _urn_uuid(member[len(prefix) : -len(suffix)], "journal ID")


def _relative_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or value.startswith(".riverhog/")
    ):
        raise ProvenanceRecoveryError(f"provenance file path is invalid: {value}")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProvenanceRecoveryError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    result = _nonnegative_int(value, label)
    if result < 1:
        raise ProvenanceRecoveryError(f"{label} must be positive")
    return result


def _sha256(value: object, label: str) -> str:
    text = str(value or "")
    if _SHA256_RE.fullmatch(text) is None:
        raise ProvenanceRecoveryError(f"{label} is invalid")
    return text


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
