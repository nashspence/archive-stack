from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from lifecycle_events import EventPage
from riverhog_api.auth import EventsReader
from riverhog_api.deps import ContainerDep
from riverhog_core.app_permissions import EVENTS_READ_ALL

router = APIRouter(tags=["events"])


@router.get("/events", response_model=EventPage)
def list_events(
    container: ContainerDep,
    principal: EventsReader,
    after: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100),
) -> EventPage:
    try:
        return container.lifecycle_events.page(
            owner_app=None if principal.allows(EVENTS_READ_ALL) else principal.app,
            after=after,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


__all__ = ["router"]
