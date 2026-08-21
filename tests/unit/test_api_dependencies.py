from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from riverhog_api import deps
from riverhog_core.runtime_config import RuntimeConfig, StorageAdapterRegistration


def test_default_container_closes_startup_resources_after_adapter_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = StorageAdapterRegistration(
        name="archive",
        base_url="http://adapter.example.test",
        token_file=tmp_path / "adapter.token",
        allow_insecure_http=True,
    )
    config = RuntimeConfig(
        database_url="sqlite+pysqlite:///:memory:",
        archive_stores={"archive": registration},
    )
    session_factory = object()
    closed: list[str] = []

    class RejectingAdapter:
        def check_readiness(self) -> None:
            return None

        def descriptor(self) -> Any:
            return SimpleNamespace(
                minimum_nonfinal_part_bytes=config.archive_multipart_part_bytes + 1,
                maximum_part_bytes=config.archive_multipart_part_bytes + 2,
            )

        def close(self) -> None:
            closed.append("adapter")

    monkeypatch.setattr(deps, "load_runtime_config", lambda: config)
    monkeypatch.setattr(deps, "validate_db", lambda _url: None)
    monkeypatch.setattr(deps, "make_session_factory", lambda _url: session_factory)
    monkeypatch.setattr(deps, "dispose_session_factory", lambda _factory: closed.append("db"))
    monkeypatch.setattr(deps, "_adapter_client", lambda _registration: RejectingAdapter())
    deps.default_container.cache_clear()

    with pytest.raises(ValueError, match="does not accept the configured multipart size"):
        deps.default_container()

    assert closed == ["adapter", "db"]
    deps.default_container.cache_clear()
