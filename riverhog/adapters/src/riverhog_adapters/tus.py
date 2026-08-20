"""TUS completion hooks that transfer custody directly to Riverhog."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from riverhog_protocol.paths import normalize_relpath
from riverhog_provenance import FileProvenanceBinding, build_portable_provenance_set

from riverhog_adapters.config import AdapterConfig, SourceConfig
from riverhog_adapters.landing import FinalizedReceiptAdapter

_SOURCE = "riverhog_source"
_PATH = "riverhog_path"
_PAYLOAD_SHA256 = "riverhog_payload_sha256"
_PROVENANCE_SHA256 = "riverhog_provenance_sha256"
_SIGNATURE = "riverhog_signature"


class TusAdapterError(RuntimeError):
    pass


class TusAuthenticationError(TusAdapterError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedTusUpload:
    upload_id: str
    source: str
    relative_path: str
    bytes: int
    payload_sha256: str
    provenance_sha256: str
    signature: str

    def metadata(self) -> dict[str, str]:
        return {
            _SOURCE: self.source,
            _PATH: self.relative_path,
            _PAYLOAD_SHA256: self.payload_sha256,
            _PROVENANCE_SHA256: self.provenance_sha256,
            _SIGNATURE: self.signature,
        }


class TusPublicationService:
    def __init__(
        self,
        config: AdapterConfig,
        adapter: FinalizedReceiptAdapter,
    ) -> None:
        self.config = config
        self.adapter = adapter

    def prepare(
        self,
        *,
        authorization: str | None,
        metadata: Mapping[str, object],
        size: object,
    ) -> PreparedTusUpload:
        source = self._authenticate(authorization)
        byte_count = _nonnegative_int(size, "TUS upload size")
        relative = normalize_relpath(str(metadata.get("path") or metadata.get("filename") or ""))
        payload_sha256 = _sha256(metadata.get("sha256"), "payload")
        provenance_sha256 = _sha256(metadata.get("provenance_sha256"), "provenance")
        upload_id = uuid.uuid4().hex
        signature = self._signature(
            source,
            upload_id=upload_id,
            relative_path=relative,
            byte_count=byte_count,
            payload_sha256=payload_sha256,
            provenance_sha256=provenance_sha256,
        )
        prepared = PreparedTusUpload(
            upload_id=upload_id,
            source=source.id,
            relative_path=relative,
            bytes=byte_count,
            payload_sha256=payload_sha256,
            provenance_sha256=provenance_sha256,
            signature=signature,
        )
        root = self._publication_root(source, upload_id)
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        _write_json(
            root / "intent.json",
            {
                "format": "riverhog-tus-publication/v1",
                "upload_id": prepared.upload_id,
                "source": prepared.source,
                "relative_path": prepared.relative_path,
                "bytes": prepared.bytes,
                "payload_sha256": prepared.payload_sha256,
                "provenance_sha256": prepared.provenance_sha256,
                "signature": prepared.signature,
            },
        )
        return prepared

    def authenticate(self, authorization: str | None) -> str:
        """Authenticate one TUS data-plane request without accepting payload state."""

        return self._authenticate(authorization).id

    def put_journal(
        self,
        upload_id: str,
        journal_id: str,
        *,
        authorization: str | None,
        content: bytes,
        expected_sha256: str,
    ) -> dict[str, object]:
        source, intent = self._authorized_intent(upload_id, authorization)
        root = self._publication_root(source, upload_id)
        with _publication_lock(root):
            if (root / "receipt.json").is_file():
                raise TusAdapterError("TUS publication is already terminal")
            if hashlib.sha256(content).hexdigest() != _sha256(expected_sha256, "journal"):
                raise TusAdapterError("TUS provenance journal digest mismatch")
            name = hashlib.sha256(journal_id.encode()).hexdigest() + ".json-seq"
            path = root / "journals" / name
            if path.exists() and path.read_bytes() != content:
                raise TusAdapterError("TUS provenance journal identity collision")
            if not path.exists():
                _write_bytes(path, content)
            index = self._load_journal_index(source, upload_id)
            previous = index.get(journal_id)
            if previous is not None and previous != name:
                raise TusAdapterError("TUS provenance journal mapping collision")
            index[journal_id] = name
            _write_json(root / "journals.json", index)
            return {
                "journal_id": journal_id,
                "bytes": len(content),
                "sha256": expected_sha256,
            }

    def put_binding(
        self,
        upload_id: str,
        binding: Mapping[str, object],
        *,
        authorization: str | None,
    ) -> dict[str, object]:
        source, intent = self._authorized_intent(upload_id, authorization)
        root = self._publication_root(source, upload_id)
        with _publication_lock(root):
            if (root / "receipt.json").is_file():
                raise TusAdapterError("TUS publication is already terminal")
            normalized = _binding(binding)
            expected = (
                str(intent["relative_path"]),
                _nonnegative_int(intent["bytes"], "TUS upload size"),
                str(intent["payload_sha256"]),
            )
            if (normalized.path, normalized.bytes, normalized.sha256) != expected:
                raise TusAdapterError("TUS provenance binding differs from the upload intent")
            if source.provenance == "capture" and normalized.status != "captured":
                raise TusAdapterError("TUS source requires captured client provenance")
            if source.provenance == "omit" and normalized.status != "omitted":
                raise TusAdapterError("TUS source is configured for explicit provenance omission")
            path = root / "binding.json"
            payload = _binding_row(normalized)
            if path.exists() and _read_json(path) != payload:
                raise TusAdapterError("TUS provenance binding identity collision")
            if not path.exists():
                _write_json(path, payload)
            return payload

    def receipt(
        self,
        upload_id: str,
        *,
        authorization: str | None,
    ) -> dict[str, object]:
        source, _intent = self._authorized_intent(upload_id, authorization)
        path = self._publication_root(source, upload_id) / "receipt.json"
        if not path.is_file():
            return {
                "format": "riverhog-tus-receipt/v1",
                "upload_id": upload_id,
                "status": "pending",
            }
        return _read_json(path)

    def publish(self, upload: Mapping[str, object]) -> dict[str, object]:
        upload_id = _upload_id(upload.get("ID"))
        metadata = _mapping(upload.get("MetaData"), "TUS upload metadata")
        source_id = str(metadata.get(_SOURCE) or "")
        try:
            source = self.config.source(source_id, adapter="tus")
        except KeyError as exc:
            raise TusAuthenticationError("TUS upload source is unknown") from exc
        root = self._publication_root(source, upload_id)
        with _publication_lock(root):
            intent = self._intent(source, upload_id)
            receipt_path = root / "receipt.json"
            if receipt_path.is_file():
                return _read_json(receipt_path)
            self._validate_upload(source, intent, upload, metadata)
            binding_path = root / "binding.json"
            if not binding_path.is_file():
                raise TusAdapterError("TUS upload has no provenance binding")
            binding = _binding(_read_json(binding_path))
            journals = self._load_journals(source, upload_id)
            portable = build_portable_provenance_set(bindings=(binding,), journals=journals)
            if hashlib.sha256(portable).hexdigest() != intent["provenance_sha256"]:
                raise TusAdapterError("TUS provenance set differs from its declared identity")
            source_path, info_path = self._storage_paths(source, upload_id, upload)
            produced = self.adapter.accept_completed_file(
                source,
                source_path,
                relative_path=str(intent["relative_path"]),
                source_event_id=upload_id,
                expected_bytes=_nonnegative_int(intent["bytes"], "TUS upload size"),
                expected_sha256=str(intent["payload_sha256"]),
                provenance=_binding_row(binding),
                provenance_journals=journals,
            )
            receipt = {
                "format": "riverhog-tus-receipt/v1",
                "upload_id": upload_id,
                "status": "accepted",
                "path": binding.path,
                "bytes": binding.bytes,
                "payload_sha256": binding.sha256,
                "provenance_sha256": str(intent["provenance_sha256"]),
                "collection_id": produced.collection_id,
                "manifest_sha256": produced.manifest_sha256,
                "content_etag": produced.content_etag,
            }
            _write_json(receipt_path, receipt)
            info_path.unlink(missing_ok=True)
            return receipt

    def handle_hook(
        self,
        payload: Mapping[str, object],
        *,
        authorization: str | None,
    ) -> dict[str, object]:
        hook_type = str(payload.get("Type") or "")
        event = _mapping(payload.get("Event"), "TUS hook event")
        upload = _mapping(event.get("Upload"), "TUS hook upload")
        if hook_type == "pre-create":
            metadata = _mapping(upload.get("MetaData") or {}, "TUS upload metadata")
            prepared = self.prepare(
                authorization=authorization,
                metadata=metadata,
                size=upload.get("Size"),
            )
            return {"ChangeFileInfo": {"ID": prepared.upload_id, "MetaData": prepared.metadata()}}
        if hook_type == "post-finish":
            self.publish(upload)
        return {}

    def _authenticate(self, authorization: str | None) -> SourceConfig:
        username, password = _basic_credentials(authorization)
        try:
            source = self.config.source(username, adapter="tus")
        except KeyError as exc:
            raise TusAuthenticationError("invalid TUS credentials") from exc
        if not secrets.compare_digest(password, source.credential()):
            raise TusAuthenticationError("invalid TUS credentials")
        return source

    def _authorized_intent(
        self,
        upload_id: str,
        authorization: str | None,
    ) -> tuple[SourceConfig, dict[str, object]]:
        source = self._authenticate(authorization)
        return source, self._intent(source, _upload_id(upload_id))

    def _intent(self, source: SourceConfig, upload_id: str) -> dict[str, object]:
        path = self._publication_root(source, upload_id) / "intent.json"
        try:
            payload = _read_json(path)
        except FileNotFoundError as exc:
            raise TusAuthenticationError("unknown TUS publication") from exc
        if payload.get("source") != source.id or payload.get("upload_id") != upload_id:
            raise TusAuthenticationError("TUS publication source differs from its intent")
        return payload

    def _signature(
        self,
        source: SourceConfig,
        *,
        upload_id: str,
        relative_path: str,
        byte_count: int,
        payload_sha256: str,
        provenance_sha256: str,
    ) -> str:
        document = "\0".join(
            (
                upload_id,
                source.id,
                relative_path,
                str(byte_count),
                payload_sha256,
                provenance_sha256,
            )
        ).encode()
        return hmac.new(source.credential().encode(), document, hashlib.sha256).hexdigest()

    def _validate_upload(
        self,
        source: SourceConfig,
        intent: Mapping[str, object],
        upload: Mapping[str, object],
        metadata: Mapping[str, object],
    ) -> None:
        byte_count = _nonnegative_int(upload.get("Size"), "TUS upload size")
        if _nonnegative_int(upload.get("Offset"), "TUS upload offset") != byte_count:
            raise TusAdapterError("TUS upload is not complete")
        actual = (
            str(metadata.get(_SOURCE) or ""),
            str(metadata.get(_PATH) or ""),
            byte_count,
            str(metadata.get(_PAYLOAD_SHA256) or ""),
            str(metadata.get(_PROVENANCE_SHA256) or ""),
        )
        expected = (
            source.id,
            str(intent["relative_path"]),
            _nonnegative_int(intent["bytes"], "TUS upload size"),
            str(intent["payload_sha256"]),
            str(intent["provenance_sha256"]),
        )
        signature = str(metadata.get(_SIGNATURE) or "")
        expected_signature = self._signature(
            source,
            upload_id=str(intent["upload_id"]),
            relative_path=expected[1],
            byte_count=expected[2],
            payload_sha256=expected[3],
            provenance_sha256=expected[4],
        )
        if actual != expected or not secrets.compare_digest(signature, expected_signature):
            raise TusAuthenticationError("TUS upload differs from its authenticated intent")

    def _storage_paths(
        self,
        source: SourceConfig,
        upload_id: str,
        upload: Mapping[str, object],
    ) -> tuple[Path, Path]:
        storage = _mapping(upload.get("Storage"), "TUS upload storage")
        if storage.get("Type") != "filestore":
            raise TusAdapterError("TUS adapter requires filestore storage")
        uploads = (source.root / "uploads").resolve()
        expected_source = (uploads / upload_id).resolve()
        expected_info = (uploads / f"{upload_id}.info").resolve()
        source_path = Path(str(storage.get("Path") or "")).resolve()
        info_path = Path(str(storage.get("InfoPath") or "")).resolve()
        if source_path != expected_source or info_path != expected_info:
            raise TusAdapterError("TUS upload escaped its configured staging root")
        for path in (source_path, info_path):
            if not path.exists():
                continue
            observed = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(observed.st_mode):
                raise TusAdapterError("TUS staging record must be a regular file")
        return source_path, info_path

    def _publication_root(self, source: SourceConfig, upload_id: str) -> Path:
        return source.root / ".riverhog-adapter" / "tus" / upload_id

    def _load_journal_index(self, source: SourceConfig, upload_id: str) -> dict[str, str]:
        path = self._publication_root(source, upload_id) / "journals.json"
        if not path.is_file():
            return {}
        return {str(key): str(value) for key, value in _read_json(path).items()}

    def _load_journals(self, source: SourceConfig, upload_id: str) -> dict[str, bytes]:
        root = self._publication_root(source, upload_id)
        return {
            journal_id: (root / "journals" / name).read_bytes()
            for journal_id, name in self._load_journal_index(source, upload_id).items()
        }


def _basic_credentials(authorization: str | None) -> tuple[str, str]:
    scheme, _, encoded = (authorization or "").partition(" ")
    if scheme.casefold() != "basic" or not encoded:
        raise TusAuthenticationError("TUS Basic credentials are required")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise TusAuthenticationError("invalid TUS Basic credentials") from exc
    username, separator, password = decoded.partition(":")
    if not separator or not username or not password:
        raise TusAuthenticationError("invalid TUS Basic credentials")
    return username, password


def _upload_id(value: object) -> str:
    text = str(value or "")
    try:
        normalized = uuid.UUID(text).hex
    except (ValueError, AttributeError) as exc:
        raise TusAdapterError("invalid TUS upload identity") from exc
    if text != normalized:
        raise TusAdapterError("invalid TUS upload identity")
    return text


def _sha256(value: object, label: str) -> str:
    text = str(value or "").casefold()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise TusAdapterError(f"invalid TUS {label} SHA-256")
    return text


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise TusAdapterError(f"{label} must be a nonnegative integer")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise TusAdapterError(f"{label} must be a nonnegative integer") from exc
    if parsed < 0:
        raise TusAdapterError(f"{label} must be a nonnegative integer")
    return parsed


def _binding(value: Mapping[str, object]) -> FileProvenanceBinding:
    expected = {"path", "bytes", "sha256", "status"}
    status = str(value.get("status") or "")
    if status == "captured":
        expected |= {"journal_id", "current_state_id"}
    elif status == "omitted":
        expected.add("omission_reason")
    else:
        raise TusAdapterError("invalid TUS provenance binding status")
    if set(value) != expected:
        raise TusAdapterError("TUS provenance binding has unexpected fields")
    return FileProvenanceBinding(
        path=normalize_relpath(str(value["path"])),
        bytes=_nonnegative_int(value["bytes"], "provenance binding bytes"),
        sha256=_sha256(value["sha256"], "provenance binding"),
        status="captured" if status == "captured" else "omitted",
        journal_id=str(value["journal_id"]) if status == "captured" else None,
        current_state_id=str(value["current_state_id"]) if status == "captured" else None,
        omission_reason=str(value["omission_reason"]) if status == "omitted" else None,
    )


def _binding_row(value: FileProvenanceBinding) -> dict[str, object]:
    row: dict[str, object] = {
        "path": value.path,
        "bytes": value.bytes,
        "sha256": value.sha256,
        "status": value.status,
    }
    if value.status == "captured":
        row.update(journal_id=value.journal_id, current_state_id=value.current_state_id)
    else:
        row["omission_reason"] = value.omission_reason
    return row


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TusAdapterError(f"{label} must be an object")
    return {str(key): item for key, item in value.items()}


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return _mapping(payload, str(path))


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    _write_bytes(
        path,
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.part")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def _publication_lock(root: Path) -> Iterator[None]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "publish.lock").open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


__all__ = [
    "PreparedTusUpload",
    "TusAdapterError",
    "TusAuthenticationError",
    "TusPublicationService",
]
