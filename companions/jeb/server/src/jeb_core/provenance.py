from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from riverhog_provenance import (
    FileProvenanceBinding,
    ProvenanceValidationError,
    append_observation,
    build_portable_provenance_set,
    load_or_create_installation_id,
    provenance_journal_filename,
    validate_journal,
    validate_portable_provenance_set,
)
from riverhog_provenance.archive import ValidatedProvenanceIndex

from jeb_core.domain.models import JebIngressConfig
from jeb_core.ingress import normalize_landing_path, normalize_tus_upload_id


@dataclass(frozen=True, slots=True)
class JebProvenanceSet:
    binding: FileProvenanceBinding
    journals: dict[str, bytes]
    index_bytes: bytes


def put_ingress_journal(
    config: JebIngressConfig,
    *,
    upload_id: str,
    source_id: str,
    journal_id: str,
    content: bytes,
    expected_sha256: str,
) -> dict[str, object]:
    upload_id = normalize_tus_upload_id(upload_id)
    summary = validate_journal(content)
    if summary.journal_id != journal_id:
        raise ProvenanceValidationError("provenance journal identity does not match its path")
    digest = hashlib.sha256(content).hexdigest()
    if digest != expected_sha256:
        raise ProvenanceValidationError("provenance journal digest does not match its header")
    root = _upload_root(config, upload_id)
    _bind_source(root, source_id)
    destination = root / "journals" / provenance_journal_filename(journal_id)
    _write_exact_idempotent(destination, content)
    return {"journal_id": journal_id, "bytes": len(content), "sha256": digest}


def put_ingress_binding(
    config: JebIngressConfig,
    *,
    upload_id: str,
    source_id: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    upload_id = normalize_tus_upload_id(upload_id)
    binding = _binding(payload)
    root = _upload_root(config, upload_id)
    _bind_source(root, source_id)
    journals = _load_journals(root)
    index_bytes = build_portable_provenance_set(bindings=[binding], journals=journals)
    _write_exact_idempotent(root / "binding.json", _canonical_json(dict(payload)))
    return {
        "status": binding.status,
        "path": binding.path,
        "provenance_identity": hashlib.sha256(index_bytes).hexdigest(),
    }


def finalize_ingress_provenance(
    config: JebIngressConfig,
    *,
    upload_id: str,
    source_id: str,
    source: Path,
    target_path: str,
    expected_provenance_sha256: str,
) -> JebProvenanceSet:
    upload_id = normalize_tus_upload_id(upload_id)
    target_path = normalize_landing_path(target_path)
    root = _upload_root(config, upload_id)
    finalized = root / "final"
    if (finalized / "index.json").is_file():
        if (root / "submission.sha256").read_text(encoding="ascii") != expected_provenance_sha256:
            raise ProvenanceValidationError("Jeb ingress provenance identity changed on retry")
        return load_provenance_set(finalized / "index.json")

    binding_path = root / "binding.json"
    if binding_path.is_file():
        if (root / "source").read_text(encoding="utf-8") != source_id:
            raise ProvenanceValidationError("provenance source does not match the upload")
        value = json.loads(binding_path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ProvenanceValidationError("provenance binding must be an object")
        incoming = _binding(value)
        journals = _load_journals(root)
        incoming_index = build_portable_provenance_set(bindings=[incoming], journals=journals)
        if hashlib.sha256(incoming_index).hexdigest() != expected_provenance_sha256:
            raise ProvenanceValidationError(
                "Jeb ingress provenance does not match the signed submission identity"
            )
        _write_exact_idempotent(
            root / "submission.sha256", expected_provenance_sha256.encode("ascii")
        )
        byte_count, digest = _payload_identity(source)
        if incoming.bytes != byte_count or incoming.sha256 != digest:
            raise ProvenanceValidationError("provenance binding does not match the uploaded file")
        if incoming.status == "captured":
            journal_id = str(incoming.journal_id)
            current = journals[journal_id]
            continued = append_observation(
                current,
                source,
                relative_path=target_path,
                host_id=load_or_create_installation_id(config.provenance_installation_id_path),
                agent_name="jeb-server",
                agent_version=importlib.metadata.version("jeb-server"),
            )
            if not continued.startswith(current):
                raise RuntimeError("Jeb did not preserve the incoming provenance prefix")
            journals[journal_id] = continued
            summary = validate_journal(continued)
            binding = FileProvenanceBinding(
                path=target_path,
                bytes=byte_count,
                sha256=digest,
                status="captured",
                journal_id=journal_id,
                current_state_id=summary.current_state_id,
            )
        else:
            binding = FileProvenanceBinding(
                path=target_path,
                bytes=byte_count,
                sha256=digest,
                status="omitted",
                omission_reason=incoming.omission_reason,
            )
    else:
        raise ProvenanceValidationError(
            "Jeb TUS ingress requires an explicit provenance binding before completion"
        )

    index_bytes = build_portable_provenance_set(bindings=[binding], journals=journals)
    _write_set(finalized, index_bytes=index_bytes, journals=journals)
    return JebProvenanceSet(binding=binding, journals=journals, index_bytes=index_bytes)


def publish_ingress_provenance(destination: Path, value: JebProvenanceSet) -> Path:
    root = ingress_provenance_path(destination)
    _write_set(root, index_bytes=value.index_bytes, journals=value.journals)
    return root


def ingress_provenance_path(payload: Path) -> Path:
    return payload.with_name(f".{payload.name}.riverhog-provenance")


def load_provenance_set(index_path: Path) -> JebProvenanceSet:
    journals = {
        path.name.removesuffix(".json-seq"): path.read_bytes()
        for path in sorted((index_path.parent / "journals").glob("*.json-seq"))
    }
    validated = validate_portable_provenance_set(index_path.read_bytes(), journals)
    if len(validated.bindings) != 1:
        raise ProvenanceValidationError("Jeb per-file provenance must have one binding")
    return JebProvenanceSet(
        binding=validated.bindings[0],
        journals=validated.journal_bytes,
        index_bytes=index_path.read_bytes(),
    )


def write_portable_provenance_set(
    root: Path,
    *,
    bindings: list[FileProvenanceBinding],
    journals: Mapping[str, bytes],
) -> ValidatedProvenanceIndex:
    index_bytes = build_portable_provenance_set(bindings=bindings, journals=journals)
    _write_set(root, index_bytes=index_bytes, journals=journals)
    return validate_portable_provenance_set(index_bytes, journals)


def replace_portable_provenance_set(
    root: Path,
    *,
    bindings: list[FileProvenanceBinding],
    journals: Mapping[str, bytes],
) -> ValidatedProvenanceIndex:
    index_bytes = build_portable_provenance_set(bindings=bindings, journals=journals)
    temporary = root.with_name(f".{root.name}.{uuid.uuid4().hex}.replacement")
    backup = root.with_name(f".{root.name}.{uuid.uuid4().hex}.backup")
    try:
        _write_set(temporary, index_bytes=index_bytes, journals=journals)
        os.replace(root, backup)
        try:
            os.replace(temporary, root)
        except Exception:
            os.replace(backup, root)
            raise
        shutil.rmtree(backup)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
    return validate_portable_provenance_set(index_bytes, journals)


def load_portable_provenance_set(index_path: Path) -> ValidatedProvenanceIndex:
    journals = {
        path.name.removesuffix(".json-seq"): path.read_bytes()
        for path in sorted((index_path.parent / "journals").glob("*.json-seq"))
    }
    return validate_portable_provenance_set(index_path.read_bytes(), journals)


def remove_staged_ingress_provenance(config: JebIngressConfig, upload_id: str) -> None:
    root = _upload_root(config, upload_id)
    if root.exists():
        shutil.rmtree(root)


def _upload_root(config: JebIngressConfig, upload_id: str) -> Path:
    return config.tus_staging_dir / ".provenance" / normalize_tus_upload_id(upload_id)


def _bind_source(root: Path, source_id: str) -> None:
    if not source_id or source_id != source_id.strip():
        raise ProvenanceValidationError("provenance source is invalid")
    source_path = root / "source"
    _write_exact_idempotent(source_path, source_id.encode("utf-8"))


def _load_journals(root: Path) -> dict[str, bytes]:
    journal_dir = root / "journals"
    if not journal_dir.is_dir():
        return {}
    return {
        path.name.removesuffix(".json-seq"): path.read_bytes()
        for path in sorted(journal_dir.glob("*.json-seq"))
    }


def _binding(payload: Mapping[str, object]) -> FileProvenanceBinding:
    status = str(payload.get("status") or "")
    common = {
        "path": str(payload.get("path") or ""),
        "bytes": payload.get("bytes"),
        "sha256": str(payload.get("sha256") or ""),
        "status": status,
    }
    if status == "captured":
        expected = {"status", "path", "bytes", "sha256", "journal_id", "current_state_id"}
        if set(payload) != expected:
            raise ProvenanceValidationError("captured provenance binding fields are invalid")
        return FileProvenanceBinding(
            **common,  # type: ignore[arg-type]
            journal_id=str(payload.get("journal_id") or ""),
            current_state_id=str(payload.get("current_state_id") or ""),
        )
    if status == "omitted":
        expected = {"status", "path", "bytes", "sha256", "omission_reason"}
        if set(payload) != expected:
            raise ProvenanceValidationError("omitted provenance binding fields are invalid")
        return FileProvenanceBinding(
            **common,  # type: ignore[arg-type]
            omission_reason=str(payload.get("omission_reason") or ""),
        )
    raise ProvenanceValidationError("provenance binding status is invalid")


def _write_set(root: Path, *, index_bytes: bytes, journals: Mapping[str, bytes]) -> None:
    if (root / "index.json").is_file():
        existing = load_provenance_set(root / "index.json")
        if existing.index_bytes != index_bytes or existing.journals != dict(journals):
            raise ProvenanceValidationError("Jeb provenance retry changed accepted bytes")
        return
    temporary = root.with_name(f".{root.name}.{uuid.uuid4().hex}.part")
    try:
        for journal_id, content in sorted(journals.items()):
            _write_exact_idempotent(
                temporary / "journals" / provenance_journal_filename(journal_id), content
            )
        _write_exact_idempotent(temporary / "index.json", index_bytes)
        root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, root)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _write_exact_idempotent(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ProvenanceValidationError("Jeb provenance retry changed accepted bytes")
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _payload_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()
