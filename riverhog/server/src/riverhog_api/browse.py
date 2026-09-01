"""HTTP binding for bounded mutable Riverhog browsing."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated

from fastapi import Query
from http_api_contracts.browse import (
    BrowsePageToken,
    BrowseQuery,
    BrowseScalar,
    BrowseTokenError,
)
from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_protocol.errors import BadRequest

from riverhog_api.deps import ServiceContainer

type BrowsePageTokenQuery = Annotated[BrowsePageToken | None, Query()]
type BrowseQueryParameter = Annotated[BrowseQuery | None, Query()]


def canonical_selectors(**values: object) -> dict[str, object]:
    """Return the exact selector projection bound into a page token."""

    return dict(values)


def page_position(
    container: ServiceContainer,
    principal: ApplicationPrincipal,
    *,
    operation: str,
    page_token: str | None,
    selectors: Mapping[str, object],
) -> tuple[BrowseScalar, ...] | None:
    if page_token is None:
        return None
    try:
        return container.browse_tokens.verify(
            page_token,
            operation=operation,
            principal={"app": principal.app, "key_id": principal.key_id},
            selectors=selectors,
        )
    except BrowseTokenError as exc:
        raise BadRequest(str(exc)) from exc


def page_payload(
    payload: Mapping[str, object],
    *,
    container: ServiceContainer,
    principal: ApplicationPrincipal,
    operation: str,
    selectors: Mapping[str, object],
) -> dict[str, object]:
    result = dict(payload)
    position = result.pop("_next_position", None)
    if position is not None and not isinstance(position, Sequence):
        raise TypeError("mutable browse result has an invalid next position")
    result["next_page_token"] = (
        container.browse_tokens.issue(
            operation=operation,
            principal={"app": principal.app, "key_id": principal.key_id},
            selectors=selectors,
            position=position,
        )
        if position is not None
        else None
    )
    return result


__all__ = [
    "BrowsePageTokenQuery",
    "BrowseQueryParameter",
    "canonical_selectors",
    "page_payload",
    "page_position",
]
