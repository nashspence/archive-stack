from __future__ import annotations

import hashlib

_UPLOAD_ID_PREFIX = ".riverhog/uploads/by-target/"
_UPLOAD_ID_DIGEST_LENGTH = 64


def tusd_upload_id_for_target_path(target_path: str) -> str:
    normalized = target_path.lstrip("/")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{_UPLOAD_ID_PREFIX}{digest}"


def tusd_upload_id_from_locator(locator: str) -> str | None:
    """Recover Riverhog's opaque id from a tusd data-plane locator.

    tusd's S3 backend appends an implementation-owned ``+...`` suffix to the
    pre-create hook's requested id. Riverhog authorizes the stable opaque
    prefix while leaving the backend suffix entirely opaque.
    """
    normalized = locator.lstrip("/")
    if not normalized.startswith(_UPLOAD_ID_PREFIX):
        return None
    remainder = normalized.removeprefix(_UPLOAD_ID_PREFIX)
    digest = remainder[:_UPLOAD_ID_DIGEST_LENGTH]
    suffix = remainder[_UPLOAD_ID_DIGEST_LENGTH:]
    if len(digest) != _UPLOAD_ID_DIGEST_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        return None
    if suffix and not suffix.startswith("+"):
        return None
    return f"{_UPLOAD_ID_PREFIX}{digest}"
