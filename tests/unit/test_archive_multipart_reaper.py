from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

from riverhog_api import app as api_app
from riverhog_api.deps import ServiceContainer


class _ArchiveUploadService:
    def __init__(self) -> None:
        self.initiated_before: datetime | None = None

    def abort_incomplete_multipart_uploads(
        self,
        *,
        initiated_before: datetime,
    ) -> int:
        self.initiated_before = initiated_before
        return 2


def test_archive_multipart_sweep_uses_the_configured_max_age(monkeypatch) -> None:
    service = _ArchiveUploadService()
    container = cast(
        ServiceContainer,
        SimpleNamespace(archive_uploads=service),
    )
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    monkeypatch.setattr(api_app, "utc_now", lambda: now)

    aborted = api_app._abort_incomplete_archive_multipart_uploads(
        container,
        max_age=timedelta(days=3),
    )

    assert aborted == 2
    assert service.initiated_before == datetime(2026, 7, 13, 12, tzinfo=UTC)
