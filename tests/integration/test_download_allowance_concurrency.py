from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from riverhog_core.catalog_db import Base, create_catalog_engine, initialize_db
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.download_allowances import SqlAlchemyDownloadAllowance
from riverhog_protocol.errors import DownloadAllowanceExceeded

pytestmark = pytest.mark.integration


@pytest.fixture
def database_url() -> Iterator[str]:
    value = os.getenv("RIVERHOG_TEST_POSTGRES_URL", "").strip()
    if not value:
        pytest.skip("RIVERHOG_TEST_POSTGRES_URL is required")
    engine = create_catalog_engine(value)
    Base.metadata.drop_all(engine)
    engine.dispose()
    initialize_db(value)
    try:
        yield value
    finally:
        engine = create_catalog_engine(value)
        Base.metadata.drop_all(engine)
        engine.dispose()


def _config(database_url: str) -> RuntimeConfig:
    config = RuntimeConfig(database_url=database_url)
    return replace(
        config,
        archive_stores={
            "deep": replace(
                config.archive_store("deep"),
                monthly_download_allowance_bytes=100,
                download_safety_buffer_bytes=10,
            )
        },
    )


def test_postgres_serializes_reservations_across_service_instances(
    database_url: str,
) -> None:
    config = _config(database_url)
    services = (
        SqlAlchemyDownloadAllowance(config),
        SqlAlchemyDownloadAllowance(config),
    )
    barrier = threading.Barrier(2)

    def reserve(service: SqlAlchemyDownloadAllowance) -> object | None:
        barrier.wait()
        try:
            return service.track(
                store="deep",
                expected_bytes=60,
                content=iter((b"x" * 60,)),
            )
        except DownloadAllowanceExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, services))

    accepted = [current for current in results if current is not None]
    assert len(accepted) == 1
    assert services[0].get_statuses()[0].reserved_bytes == 60
    assert len(b"".join(accepted[0])) == 60  # type: ignore[arg-type]
    status = services[1].get_statuses()[0]
    assert status.accounted_bytes == 60
    assert status.reserved_bytes == 0
