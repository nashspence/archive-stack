from __future__ import annotations

from fastapi import Request

from riverhog_core.runtime_config import load_runtime_config


def public_request_url(request: Request) -> str:
    public_base_url = load_runtime_config().public_base_url
    if public_base_url:
        return f"{public_base_url.rstrip('/')}{request.url.path}"
    return str(request.url)
