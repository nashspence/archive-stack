from __future__ import annotations

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
