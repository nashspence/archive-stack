from __future__ import annotations

import os
import secrets
from collections.abc import Sequence

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)



def require_api_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    expected_token: str | None = None,
) -> None:
    token = expected_token if expected_token is not None else os.getenv("ARC_API_TOKEN", "")
    if not token:
        return
    supplied = credentials.credentials if credentials else ""
    if not secrets.compare_digest(supplied, token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid api token",
            headers={"WWW-Authenticate": "Bearer"},
        )



def api_auth_dependencies() -> Sequence[Depends]:
    if not os.getenv("ARC_API_TOKEN"):
        return []
    return [Depends(require_api_auth)]
