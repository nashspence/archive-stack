from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from http_api_contracts import cursor_feed_operation
from riverhog_core.app_permissions import EVENTS_READ_ALL
from riverhog_protocol.lifecycle_events import RiverhogEventPage

from riverhog_api.auth import EventsReader
from riverhog_api.deps import ContainerDep

router = APIRouter(tags=["events"])


@router.get(
    "/events",
    response_model=RiverhogEventPage,
    response_model_exclude_none=True,
    openapi_extra=cursor_feed_operation(cursor_parameter="after", limit_parameter="limit"),
)
def list_lifecycle_events(
    container: ContainerDep,
    principal: EventsReader,
    after: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100),
) -> RiverhogEventPage:
    try:
        return container.lifecycle_events.page(
            owner_app=None if principal.allows(EVENTS_READ_ALL) else principal.app,
            after=after,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


__all__ = ["router"]
