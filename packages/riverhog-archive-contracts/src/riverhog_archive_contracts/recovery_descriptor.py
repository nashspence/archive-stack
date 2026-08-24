from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from riverhog_archive_contracts.archive_manifest import ARCHIVE_ENCRYPTION_FORMAT

RECOVERY_DESCRIPTOR_SCHEMA = "riverhog-recovery-descriptor/v1"
RECOVERY_DESCRIPTOR_PATH = "recovery.json"

_PASSPHRASE_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class RecoveryDescriptorError(ValueError):
    """The plaintext recovery descriptor is not the canonical v1 contract."""


def normalize_passphrase_id(value: str) -> str:
    if not isinstance(value, str) or _PASSPHRASE_ID_RE.fullmatch(value) is None:
        raise RecoveryDescriptorError("passphrase_id must be 16-128 URL-safe opaque characters")
    return value


@dataclass(frozen=True, slots=True)
class CollectionEncryptionBinding:
    format: str
    passphrase_id: str

    def __post_init__(self) -> None:
        if self.format != ARCHIVE_ENCRYPTION_FORMAT:
            raise RecoveryDescriptorError(f"unsupported encryption format: {self.format!r}")
        normalize_passphrase_id(self.passphrase_id)


@dataclass(frozen=True, slots=True)
class ArchiveRootCiphertextIdentity:
    path: str
    stored_bytes: int
    stored_sha256: str

    def __post_init__(self) -> None:
        if self.path != "manifest.json.age":
            raise RecoveryDescriptorError("recovery descriptor root path is not canonical")
        if not isinstance(self.stored_bytes, int) or isinstance(self.stored_bytes, bool):
            raise RecoveryDescriptorError("recovery descriptor root bytes are invalid")
        if self.stored_bytes < 1:
            raise RecoveryDescriptorError("recovery descriptor root must not be empty")
        if _SHA256_RE.fullmatch(self.stored_sha256) is None:
            raise RecoveryDescriptorError("recovery descriptor root sha256 is invalid")


@dataclass(frozen=True, slots=True)
class RecoveryDescriptor:
    encryption: CollectionEncryptionBinding
    root: ArchiveRootCiphertextIdentity
    schema: str = RECOVERY_DESCRIPTOR_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != RECOVERY_DESCRIPTOR_SCHEMA:
            raise RecoveryDescriptorError(f"unsupported recovery schema: {self.schema!r}")

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            {
                "schema": self.schema,
                "encryption": {
                    "format": self.encryption.format,
                    "passphrase_id": self.encryption.passphrase_id,
                },
                "root": {
                    "path": self.root.path,
                    "stored_bytes": self.root.stored_bytes,
                    "stored_sha256": self.root.stored_sha256,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, content: bytes | str) -> RecoveryDescriptor:
        try:
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            value: Any = json.loads(content)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RecoveryDescriptorError("recovery descriptor is not valid JSON") from exc
        if not isinstance(value, dict) or set(value) != {"schema", "encryption", "root"}:
            raise RecoveryDescriptorError("recovery descriptor fields are invalid")
        encryption = value.get("encryption")
        root = value.get("root")
        if not isinstance(encryption, dict) or set(encryption) != {"format", "passphrase_id"}:
            raise RecoveryDescriptorError("recovery descriptor encryption is invalid")
        if not isinstance(root, dict) or set(root) != {
            "path",
            "stored_bytes",
            "stored_sha256",
        }:
            raise RecoveryDescriptorError("recovery descriptor root is invalid")
        stored_bytes = root.get("stored_bytes")
        if not isinstance(stored_bytes, int) or isinstance(stored_bytes, bool):
            raise RecoveryDescriptorError("recovery descriptor root bytes are invalid")
        try:
            descriptor = cls(
                schema=str(value.get("schema", "")),
                encryption=CollectionEncryptionBinding(
                    format=str(encryption.get("format", "")),
                    passphrase_id=str(encryption.get("passphrase_id", "")),
                ),
                root=ArchiveRootCiphertextIdentity(
                    path=str(root.get("path", "")),
                    stored_bytes=stored_bytes,
                    stored_sha256=str(root.get("stored_sha256", "")),
                ),
            )
        except (TypeError, RecoveryDescriptorError) as exc:
            if isinstance(exc, RecoveryDescriptorError):
                raise
            raise RecoveryDescriptorError("recovery descriptor values are invalid") from exc
        if descriptor.to_json_bytes() != content.encode("utf-8"):
            raise RecoveryDescriptorError("recovery descriptor JSON is not canonical")
        return descriptor
