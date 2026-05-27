from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from riverhog_core.recovery_payloads import RecoveryPayloadCodec, decrypt_recovery_payload


@dataclass(frozen=True, slots=True)
class CopyRecoveryMetadata:
    recovery_bytes: int
    recovery_sha256: str
    plaintext_bytes: int | None = None
    plaintext_sha256: str | None = None


def read_copy_recovery_metadata(
    image_root: str,
    disc_path: str,
    recovery_payload_codec: RecoveryPayloadCodec,
    *,
    include_plaintext: bool = False,
) -> CopyRecoveryMetadata:
    payload = (Path(image_root) / disc_path.lstrip("/")).read_bytes()
    plaintext = (
        decrypt_recovery_payload(payload, recovery_payload_codec)
        if include_plaintext
        else None
    )
    return CopyRecoveryMetadata(
        recovery_bytes=len(payload),
        recovery_sha256=hashlib.sha256(payload).hexdigest(),
        plaintext_bytes=len(plaintext) if plaintext is not None else None,
        plaintext_sha256=hashlib.sha256(plaintext).hexdigest() if plaintext is not None else None,
    )
