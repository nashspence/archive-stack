from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from fastapi import Request

from riverhog_core.runtime_config import load_runtime_config


def public_request_url(request: Request) -> str:
    public_base_url = load_runtime_config().public_base_url
    if public_base_url:
        raw_path = request.scope.get("raw_path")
        if isinstance(raw_path, bytes):
            path = raw_path.decode("ascii")
        else:
            path = request.url.path
        query = request.scope.get("query_string", b"")
        query_text = f"?{query.decode('ascii')}" if isinstance(query, bytes) and query else ""
        return f"{public_base_url.rstrip('/')}{path}{query_text}"
    return str(request.url)


def _upload_expires_epoch(expires_at: str | None) -> int | None:
    if expires_at is None:
        return None
    normalized = expires_at.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _signed_tusd_query(path: str, *, expires_at: str | None, secret: str) -> dict[str, str]:
    expires = _upload_expires_epoch(expires_at)
    if expires is None:
        return {}
    normalized_uri = unquote(path)
    digest = hashlib.md5(f"{expires}{normalized_uri} {secret}".encode()).digest()
    token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return {"md5": token, "expires": str(expires)}


def public_tusd_upload_url(tus_url: str, *, expires_at: str | None = None) -> str:
    config = load_runtime_config()
    public_base_url = str(config.tusd_public_base_url or config.tusd_base_url).rstrip("/")
    internal_base_url = config.tusd_base_url.rstrip("/")
    parsed = urlsplit(tus_url)
    internal = urlsplit(internal_base_url)
    public = urlsplit(public_base_url)

    internal_path = internal.path.rstrip("/")
    if internal_path and parsed.path.startswith(f"{internal_path}/"):
        suffix = parsed.path.removeprefix(internal_path).lstrip("/")
    else:
        suffix = parsed.path.lstrip("/")
    encoded_suffix = quote(suffix, safe="/+%")
    public_path = (
        f"{public.path.rstrip('/')}/{encoded_suffix}" if encoded_suffix else public.path.rstrip("/")
    )
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if config.tusd_public_signing_secret:
        query.update(
            _signed_tusd_query(
                public_path,
                expires_at=expires_at,
                secret=config.tusd_public_signing_secret,
            )
        )
    return urlunsplit(
        (public.scheme, public.netloc, public_path, urlencode(query), parsed.fragment)
    )
