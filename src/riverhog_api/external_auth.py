from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from riverhog_api.deps import ContainerDep

_bearer = HTTPBearer(auto_error=False)
ExternalCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


def require_external_app(credentials: ExternalCredentials, container: ContainerDep) -> str:
    supplied = credentials.credentials if credentials is not None else ""
    app = container.app_keys.authenticate(supplied)
    if app is not None:
        return app
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid external application token",
        headers={"WWW-Authenticate": "Bearer"},
    )


ExternalApp = Annotated[str, Depends(require_external_app)]
