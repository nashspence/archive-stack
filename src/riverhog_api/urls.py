from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit

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


def public_tusd_upload_url(tus_url: str) -> str:
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
    return urlunsplit((public.scheme, public.netloc, public_path, parsed.query, parsed.fragment))
