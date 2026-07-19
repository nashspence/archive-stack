from __future__ import annotations

from typing import Any

import httpx

from lifecycle_events.models import EventPage


class LifecycleEventClient:
    def __init__(
        self,
        events_url: str,
        *,
        token: str,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.events_url = events_url
        self.token = token
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> LifecycleEventClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def page(self, *, after: str | None, limit: int = 100) -> EventPage:
        response = self._client.get(
            self.events_url,
            params={"after": after or "0", "limit": limit},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        response.raise_for_status()
        return EventPage.model_validate(response.json())


__all__ = ["LifecycleEventClient"]
