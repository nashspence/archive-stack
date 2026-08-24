from __future__ import annotations

import hashlib
import re
import secrets

from application_access.access import *  # noqa: F403
from application_access.access import __all__ as _access_exports

_APP_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def normalize_app_name(value: str) -> str:
    name = value.strip().casefold()
    if not name or _APP_PATTERN.fullmatch(name) is None:
        raise ValueError("app name must use lowercase letters, digits, and single dashes")
    return name


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_key_credentials(prefix: str) -> tuple[str, str, str]:
    key_id = secrets.token_hex(8)
    token = f"{prefix}{secrets.token_urlsafe(32)}"
    return key_id, token, token_sha256(token)


__all__ = ["create_key_credentials", "normalize_app_name", "token_sha256", *_access_exports]
