from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from riverhog_core.runtime_config import load_runtime_config

_bearer = HTTPBearer(auto_error=False)
ExternalCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


def require_external_app(credentials: ExternalCredentials) -> str:
    tokens = load_runtime_config().external_app_tokens
    if not tokens:
        return "development"
    supplied = credentials.credentials if credentials is not None else ""
    for app, expected in tokens.items():
        if secrets.compare_digest(supplied, expected):
            return app
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid external application token",
        headers={"WWW-Authenticate": "Bearer"},
    )


ExternalApp = Annotated[str, Depends(require_external_app)]
