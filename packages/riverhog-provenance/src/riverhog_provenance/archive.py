from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from .common import canonical_json, provenance_journal_filename
from .journal import (
    JournalSummary,
    ProvenanceValidationError,
    validate_journal_set,
    verify_payload_binding,
)

PROVENANCE_INDEX_SCHEMA = "riverhog-provenance-index/v1"
PROVENANCE_SET_SCHEMA = "riverhog-provenance-set/v1"
PROVENANCE_BUNDLE_FORMAT = "riverhog-provenance-bundle/v1"
MAX_BUNDLE_JOURNALS = 256
MAX_BUNDLE_PLAINTEXT_BYTES = 64 * 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_JOURNAL_ID_RE = re.compile(
    r"urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


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
class ProvenanceBundle:
    bundle_id: str
    relative_path: str
    content: bytes
    sha256: str
    journal_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProvenanceArchive:
    index_bytes: bytes
    identity: str
    bundles: tuple[ProvenanceBundle, ...]


@dataclass(frozen=True, slots=True)
class ValidatedProvenanceIndex:
    bindings: tuple[FileProvenanceBinding, ...]
    journals: dict[str, JournalSummary]
    journal_bytes: dict[str, bytes]
    identity: str


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return canonical_json(value) + b"\n"


def build_provenance_archive(
    *,
    bindings: Sequence[FileProvenanceBinding],
    journals: Mapping[str, bytes],
) -> ProvenanceArchive:
    normalized_bindings, summaries = _validate_bindings(bindings, journals)
    bundles = _build_bundles(journals)
    journal_locations = {
        journal_id: (bundle.bundle_id, _journal_member(journal_id))
        for bundle in bundles
        for journal_id in bundle.journal_ids
    }
    payload: dict[str, Any] = {
        "schema": PROVENANCE_INDEX_SCHEMA,
        "bundle_format": PROVENANCE_BUNDLE_FORMAT,
        "files": [_binding_row(item) for item in normalized_bindings],
        "bundles": [
            {
                "id": bundle.bundle_id,
                "path": bundle.relative_path,
                "plaintext_bytes": len(bundle.content),
                "sha256": bundle.sha256,
                "journals": len(bundle.journal_ids),
            }
            for bundle in bundles
        ],
        "journals": [
            {
                "journal_id": journal_id,
                "bundle_id": journal_locations[journal_id][0],
                "member": journal_locations[journal_id][1],
                "bytes": len(journals[journal_id]),
                "sha256": summaries[journal_id].journal_sha256,
            }
            for journal_id in sorted(journals)
        ],
    }
    index_bytes = canonical_json_bytes(payload)
    return ProvenanceArchive(
        index_bytes=index_bytes,
        identity=hashlib.sha256(index_bytes).hexdigest(),
        bundles=bundles,
    )


def validate_provenance_archive(
    index_content: bytes,
    bundles: Mapping[str, bytes],
) -> ValidatedProvenanceIndex:
    payload = _strict_json_object(index_content, label="provenance index")
    expected = {"schema", "bundle_format", "files", "bundles", "journals"}
    if (
        set(payload) != expected
        or payload.get("schema") != PROVENANCE_INDEX_SCHEMA
        or payload.get("bundle_format") != PROVENANCE_BUNDLE_FORMAT
    ):
        raise ProvenanceValidationError("provenance index schema mismatch")
    bundle_rows = _object_list(payload.get("bundles"), "provenance bundles")
    journal_rows = _object_list(payload.get("journals"), "provenance journals")
    files = _parse_binding_rows(payload.get("files"))

    expected_bundles: dict[str, dict[str, Any]] = {}
    for sequence, row in enumerate(bundle_rows):
        bundle_id = _bundle_id(sequence)
        if row != {
            "id": bundle_id,
            "path": f"provenance/{bundle_id}.tar.age",
            "plaintext_bytes": row.get("plaintext_bytes"),
            "sha256": row.get("sha256"),
            "journals": row.get("journals"),
        }:
            raise ProvenanceValidationError("provenance bundle descriptor is not canonical")
        _nonnegative_int(row.get("plaintext_bytes"), "bundle plaintext bytes")
        _positive_int(row.get("journals"), "bundle journal count")
        _sha256(row.get("sha256"), "bundle SHA-256")
        expected_bundles[bundle_id] = row
    if set(bundles) != set(expected_bundles):
        raise ProvenanceValidationError("provenance bundle set does not match the index")

    extracted: dict[str, bytes] = {}
    for bundle_id, row in expected_bundles.items():
        content = bundles[bundle_id]
        if (
            len(content) != row["plaintext_bytes"]
            or hashlib.sha256(content).hexdigest() != row["sha256"]
        ):
            raise ProvenanceValidationError(f"provenance bundle identity mismatch: {bundle_id}")
        members = _read_bundle(content)
        if len(members) != row["journals"]:
            raise ProvenanceValidationError(
                f"provenance bundle journal count mismatch: {bundle_id}"
            )
        for member, journal in members.items():
            journal_id = _journal_id_from_member(member)
            if journal_id in extracted:
                raise ProvenanceValidationError(f"provenance repeats journal {journal_id}")
            extracted[journal_id] = journal

    expected_journal_ids: set[str] = set()
    for row in journal_rows:
        if set(row) != {"journal_id", "bundle_id", "member", "bytes", "sha256"}:
            raise ProvenanceValidationError("provenance journal descriptor is invalid")
        journal_id = _journal_id(row.get("journal_id"))
        if journal_id in expected_journal_ids:
            raise ProvenanceValidationError(f"provenance repeats journal {journal_id}")
        expected_journal_ids.add(journal_id)
        bundle_id = str(row.get("bundle_id", ""))
        member = str(row.get("member", ""))
        journal_content = extracted.get(journal_id)
        if (
            bundle_id not in expected_bundles
            or member != _journal_member(journal_id)
            or journal_content is None
            or len(journal_content) != _nonnegative_int(row.get("bytes"), "journal bytes")
            or hashlib.sha256(journal_content).hexdigest()
            != _sha256(row.get("sha256"), "journal SHA-256")
        ):
            raise ProvenanceValidationError(f"provenance journal identity mismatch: {journal_id}")
        if member not in _read_bundle(bundles[bundle_id]):
            raise ProvenanceValidationError(
                f"provenance journal bundle mapping mismatch: {journal_id}"
            )
    if expected_journal_ids != set(extracted):
        raise ProvenanceValidationError("provenance bundle contains an unindexed journal")
    normalized_bindings, summaries = _validate_bindings(files, extracted)
    return ValidatedProvenanceIndex(
        bindings=normalized_bindings,
        journals=summaries,
        journal_bytes=extracted,
        identity=hashlib.sha256(index_content).hexdigest(),
    )


def build_portable_provenance_set(
    *,
    bindings: Sequence[FileProvenanceBinding],
    journals: Mapping[str, bytes],
) -> bytes:
    normalized_bindings, summaries = _validate_bindings(bindings, journals)
    payload: dict[str, Any] = {
        "schema": PROVENANCE_SET_SCHEMA,
        "files": [_binding_row(item) for item in normalized_bindings],
        "journals": [
            {
                "journal_id": journal_id,
                "path": _journal_member(journal_id),
                "bytes": len(journals[journal_id]),
                "sha256": summaries[journal_id].journal_sha256,
            }
            for journal_id in sorted(journals)
        ],
    }
    return canonical_json_bytes(payload)


def validate_portable_provenance_set(
    index_content: bytes,
    journals: Mapping[str, bytes],
) -> ValidatedProvenanceIndex:
    payload = _strict_json_object(index_content, label="portable provenance index")
    if (
        set(payload) != {"schema", "files", "journals"}
        or payload.get("schema") != PROVENANCE_SET_SCHEMA
    ):
        raise ProvenanceValidationError("portable provenance index schema mismatch")
    files = _parse_binding_rows(payload.get("files"))
    rows = _object_list(payload.get("journals"), "portable provenance journals")
    expected: set[str] = set()
    for row in rows:
        if set(row) != {"journal_id", "path", "bytes", "sha256"}:
            raise ProvenanceValidationError("portable journal descriptor is invalid")
        journal_id = _journal_id(row.get("journal_id"))
        if row.get("path") != _journal_member(journal_id):
            raise ProvenanceValidationError("portable journal path is not canonical")
        content = journals.get(journal_id)
        if (
            journal_id in expected
            or content is None
            or len(content) != _nonnegative_int(row.get("bytes"), "journal bytes")
            or hashlib.sha256(content).hexdigest() != _sha256(row.get("sha256"), "journal SHA-256")
        ):
            raise ProvenanceValidationError(f"portable journal identity mismatch: {journal_id}")
        expected.add(journal_id)
    if expected != set(journals):
        raise ProvenanceValidationError("portable provenance journal set differs from its index")
    normalized_bindings, summaries = _validate_bindings(files, journals)
    return ValidatedProvenanceIndex(
        bindings=normalized_bindings,
        journals=summaries,
        journal_bytes=dict(journals),
        identity=hashlib.sha256(index_content).hexdigest(),
    )


def _validate_bindings(
    bindings: Sequence[FileProvenanceBinding], journals: Mapping[str, bytes]
) -> tuple[tuple[FileProvenanceBinding, ...], dict[str, JournalSummary]]:
    ordered = tuple(sorted(bindings, key=lambda item: item.path.encode("utf-8")))
    if not ordered or len({item.path for item in ordered}) != len(ordered):
        raise ProvenanceValidationError("provenance must account for each file exactly once")
    summaries = validate_journal_set(journals)
    directly_bound: set[str] = set()
    for item in ordered:
        _relative_path(item.path)
        _nonnegative_int(item.bytes, "file bytes")
        _sha256(item.sha256, "file SHA-256")
        if item.status == "captured":
            if item.omission_reason is not None:
                raise ProvenanceValidationError(
                    "captured provenance cannot have an omission reason"
                )
            journal_id = _journal_id(item.journal_id)
            summary = summaries.get(journal_id)
            if summary is None or item.current_state_id != summary.current_state_id:
                raise ProvenanceValidationError(f"captured file state is unresolved: {item.path}")
            verify_payload_binding(
                summary,
                path=item.path,
                byte_count=item.bytes,
                sha256=item.sha256,
            )
            directly_bound.add(journal_id)
        elif item.status == "omitted":
            if item.journal_id is not None or item.current_state_id is not None:
                raise ProvenanceValidationError("omitted provenance cannot bind a journal")
            if not item.omission_reason or item.omission_reason != item.omission_reason.strip():
                raise ProvenanceValidationError("provenance omission requires a visible reason")
        else:
            raise ProvenanceValidationError("file provenance status is invalid")
    if summaries and not directly_bound:
        raise ProvenanceValidationError("provenance contains journals with no captured file")
    reachable = set(directly_bound)
    pending = list(directly_bound)
    while pending:
        current = summaries[pending.pop()]
        for reference in current.external_states:
            if reference.journal_id not in reachable:
                reachable.add(reference.journal_id)
                pending.append(reference.journal_id)
    if reachable != set(summaries):
        raise ProvenanceValidationError("provenance contains an unreachable journal")
    return ordered, summaries


def _build_bundles(journals: Mapping[str, bytes]) -> tuple[ProvenanceBundle, ...]:
    if not journals:
        return ()
    groups: list[list[tuple[str, bytes]]] = []
    current: list[tuple[str, bytes]] = []
    current_bytes = 0
    for journal_id, content in sorted(journals.items()):
        _journal_id(journal_id)
        contribution = len(content) + 1024
        if contribution > MAX_BUNDLE_PLAINTEXT_BYTES:
            raise ProvenanceValidationError(
                f"provenance journal exceeds the bundle limit: {journal_id}"
            )
        if current and (
            len(current) >= MAX_BUNDLE_JOURNALS
            or current_bytes + contribution > MAX_BUNDLE_PLAINTEXT_BYTES
        ):
            groups.append(current)
            current = []
            current_bytes = 0
        current.append((journal_id, content))
        current_bytes += contribution
    if current:
        groups.append(current)
    result: list[ProvenanceBundle] = []
    for sequence, group in enumerate(groups):
        bundle_id = _bundle_id(sequence)
        content = _tar_bytes(group)
        if len(content) > MAX_BUNDLE_PLAINTEXT_BYTES:
            raise ProvenanceValidationError(
                f"provenance bundle exceeds its canonical limit: {bundle_id}"
            )
        result.append(
            ProvenanceBundle(
                bundle_id=bundle_id,
                relative_path=f"provenance/{bundle_id}.tar.age",
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
                journal_ids=tuple(journal_id for journal_id, _ in group),
            )
        )
    return tuple(result)


def _tar_bytes(journals: Sequence[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for journal_id, content in journals:
            info = tarfile.TarInfo(_journal_member(journal_id))
            info.size = len(content)
            info.mode = 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


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
                    raise ProvenanceValidationError("provenance bundle member is not canonical")
                extracted = archive.extractfile(info)
                if extracted is None:
                    raise ProvenanceValidationError("provenance bundle member is unreadable")
                result[info.name] = extracted.read()
    except (tarfile.TarError, OSError) as exc:
        raise ProvenanceValidationError("provenance bundle is not a standard tar stream") from exc
    if (
        _tar_bytes([(_journal_id_from_member(name), body) for name, body in result.items()])
        != content
    ):
        raise ProvenanceValidationError("provenance bundle tar bytes are not canonical")
    return result


def _binding_row(item: FileProvenanceBinding) -> dict[str, Any]:
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
    return row


def _parse_binding_rows(value: object) -> tuple[FileProvenanceBinding, ...]:
    rows = _object_list(value, "provenance files")
    result: list[FileProvenanceBinding] = []
    for row in rows:
        status = row.get("status")
        common = {"path", "bytes", "sha256", "status"}
        if status == "captured" and set(row) == common | {"journal_id", "current_state_id"}:
            result.append(
                FileProvenanceBinding(
                    path=str(row.get("path", "")),
                    bytes=_nonnegative_int(row.get("bytes"), "file bytes"),
                    sha256=_sha256(row.get("sha256"), "file SHA-256"),
                    status="captured",
                    journal_id=str(row.get("journal_id", "")),
                    current_state_id=str(row.get("current_state_id", "")),
                )
            )
        elif status == "omitted" and set(row) == common | {"omission_reason"}:
            result.append(
                FileProvenanceBinding(
                    path=str(row.get("path", "")),
                    bytes=_nonnegative_int(row.get("bytes"), "file bytes"),
                    sha256=_sha256(row.get("sha256"), "file SHA-256"),
                    status="omitted",
                    omission_reason=str(row.get("omission_reason", "")),
                )
            )
        else:
            raise ProvenanceValidationError("provenance file binding is invalid")
    return tuple(result)


def _strict_json_object(content: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvenanceValidationError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != content:
        raise ProvenanceValidationError(f"{label} is not canonical JSON")
    return cast(dict[str, Any], value)


def _object_list(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ProvenanceValidationError(f"{label} must be an object list")
    return cast(list[dict[str, Any]], value)


def _bundle_id(sequence: int) -> str:
    return f"bundle-{sequence:012d}"


def _journal_id(value: object) -> str:
    text = str(value or "")
    if _JOURNAL_ID_RE.fullmatch(text) is None:
        raise ProvenanceValidationError("provenance journal ID is invalid")
    return text


def _journal_member(journal_id: str) -> str:
    return f"journals/{provenance_journal_filename(_journal_id(journal_id))}"


def _journal_id_from_member(member: str) -> str:
    prefix = "journals/"
    suffix = ".json-seq"
    if not member.startswith(prefix) or not member.endswith(suffix):
        raise ProvenanceValidationError("provenance bundle member path is invalid")
    return _journal_id(member[len(prefix) : -len(suffix)])


def _relative_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or value.startswith(".riverhog/")
    ):
        raise ProvenanceValidationError(f"provenance file path is invalid: {value}")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProvenanceValidationError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    result = _nonnegative_int(value, label)
    if result < 1:
        raise ProvenanceValidationError(f"{label} must be positive")
    return result


def _sha256(value: object, label: str) -> str:
    text = str(value or "")
    if _SHA256_RE.fullmatch(text) is None:
        raise ProvenanceValidationError(f"{label} is invalid")
    return text
