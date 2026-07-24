from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from riverhog_core.app_permissions import KEYS_MANAGE, RETRIEVAL_MANAGE, ApplicationPrincipal
from riverhog_core.catalog_db import initialize_db
from riverhog_core.ports.download_allowance import DownloadAttribution
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from riverhog_core.services.download_allowances import SqlAlchemyDownloadAllowance
from riverhog_protocol.errors import DownloadAllowanceExceeded

from tests.unit.db_helpers import sqlite_url

BOOTSTRAP = ApplicationPrincipal(
    app="bootstrap",
    key_id=None,
    permissions=frozenset({KEYS_MANAGE}),
    collection_grants=frozenset({"*"}),
    unrestricted_delegation=True,
)


@dataclass
class _Clock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def _config(
    path: Path,
    *,
    allowance: int | None = 100,
    buffer: int = 10,
) -> RuntimeConfig:
    config = RuntimeConfig(database_url=sqlite_url(path))
    store = replace(
        config.archive_store("deep"),
        monthly_download_allowance_bytes=allowance,
        download_safety_buffer_bytes=buffer,
    )
    return replace(config, archive_stores={"deep": store})


def _service(
    path: Path,
    *,
    clock: _Clock,
    allowance: int | None = 100,
    buffer: int = 10,
) -> SqlAlchemyDownloadAllowance:
    config = _config(path, allowance=allowance, buffer=buffer)
    initialize_db(config.database_url)
    return SqlAlchemyDownloadAllowance(config, clock=clock)


def _key(config: RuntimeConfig, *, app: str) -> dict[str, object]:
    return SqlAlchemyAppKeyService(config).create(
        app=app,
        permissions=(RETRIEVAL_MANAGE,),
        collection_grants=("*",),
        grantor=BOOTSTRAP,
    )


def test_download_allowance_accounts_remote_bytes_and_releases_reservation(
    tmp_path: Path,
) -> None:
    clock = _Clock(datetime(2026, 7, 18, tzinfo=UTC))
    service = _service(tmp_path / "catalog.sqlite3", clock=clock)

    content = service.track(
        store="deep",
        expected_bytes=5,
        content=iter((b"abc", b"de")),
    )
    reserved = service.get_statuses()[0]
    assert reserved.accounted_bytes == 0
    assert reserved.reserved_bytes == 5
    assert b"".join(content) == b"abcde"

    status = service.get_statuses()[0]
    assert status.state == "open"
    assert status.allowance_bytes == 100
    assert status.safety_buffer_bytes == 10
    assert status.effective_limit_bytes == 90
    assert status.accounted_bytes == 5
    assert status.reserved_bytes == 0
    assert status.remaining_bytes == 85
    assert status.month_started_at == "2026-07-01T00:00:00.000000Z"
    assert status.resets_at == "2026-08-01T00:00:00.000000Z"


def test_download_allowance_counts_partial_reads_and_retries(
    tmp_path: Path,
) -> None:
    clock = _Clock(datetime(2026, 7, 18, tzinfo=UTC))
    service = _service(tmp_path / "catalog.sqlite3", clock=clock)

    def interrupted():
        yield b"partial"
        raise RuntimeError("connection lost")

    with pytest.raises(RuntimeError, match="connection lost"):
        b"".join(
            service.track(
                store="deep",
                expected_bytes=20,
                content=interrupted(),
            )
        )
    assert (
        b"".join(
            service.track(
                store="deep",
                expected_bytes=5,
                content=iter((b"retry",)),
            )
        )
        == b"retry"
    )

    status = service.get_statuses()[0]
    assert status.accounted_bytes == len(b"partialretry")
    assert status.reserved_bytes == 0


def test_download_allowance_reservations_are_atomic(
    tmp_path: Path,
) -> None:
    clock = _Clock(datetime(2026, 7, 18, tzinfo=UTC))
    service = _service(tmp_path / "catalog.sqlite3", clock=clock)

    def reserve() -> object | None:
        try:
            return service.track(
                store="deep",
                expected_bytes=15,
                content=iter((b"x" * 15,)),
            )
        except DownloadAllowanceExceeded:
            return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(lambda _: reserve(), range(10)))

    accepted = [current for current in results if current is not None]
    assert len(accepted) == 6
    status = service.get_statuses()[0]
    assert status.accounted_bytes == 0
    assert status.reserved_bytes == 90
    assert status.remaining_bytes == 0
    assert status.state == "closed"

    for current in accepted:
        assert len(b"".join(current)) == 15  # type: ignore[arg-type]
    assert service.get_statuses()[0].accounted_bytes == 90


def test_download_allowance_rejects_an_object_that_would_cross_the_limit(
    tmp_path: Path,
) -> None:
    clock = _Clock(datetime(2026, 7, 18, tzinfo=UTC))
    service = _service(tmp_path / "catalog.sqlite3", clock=clock)
    assert b"".join(service.track(store="deep", expected_bytes=80, content=iter((b"x" * 80,))))

    with pytest.raises(
        DownloadAllowanceExceeded,
        match="10 bytes remaining; 11 bytes were requested; resets at 2026-08-01",
    ):
        service.track(store="deep", expected_bytes=11, content=iter((b"x" * 11,)))


def test_download_allowance_counts_abandoned_reservation_conservatively(
    tmp_path: Path,
) -> None:
    clock = _Clock(datetime(2026, 7, 18, tzinfo=UTC))
    service = _service(tmp_path / "catalog.sqlite3", clock=clock)
    _abandoned = service.track(
        store="deep",
        expected_bytes=30,
        content=iter((b"unused",)),
    )

    clock.value += timedelta(hours=2)

    status = service.get_statuses()[0]
    assert status.accounted_bytes == 30
    assert status.reserved_bytes == 0


def test_download_allowance_resets_by_utc_month_and_protects_crossing_reads(
    tmp_path: Path,
) -> None:
    clock = _Clock(datetime(2026, 7, 31, 23, 59, tzinfo=UTC))
    service = _service(tmp_path / "catalog.sqlite3", clock=clock)
    assert b"".join(service.track(store="deep", expected_bytes=20, content=iter((b"x" * 20,))))
    crossing = service.track(
        store="deep",
        expected_bytes=30,
        content=iter((b"ten-bytes!",)),
    )

    clock.value = datetime(2026, 8, 1, 0, 1, tzinfo=UTC)
    assert b"".join(crossing) == b"ten-bytes!"

    status = service.get_statuses()[0]
    assert status.month_started_at == "2026-08-01T00:00:00.000000Z"
    assert status.accounted_bytes == 30
    assert status.reserved_bytes == 0
    assert status.resets_at == "2026-09-01T00:00:00.000000Z"


def test_download_allowance_is_a_no_op_for_an_unconfigured_store(
    tmp_path: Path,
) -> None:
    clock = _Clock(datetime(2026, 7, 18, tzinfo=UTC))
    service = _service(
        tmp_path / "catalog.sqlite3",
        clock=clock,
        allowance=None,
        buffer=0,
    )

    content = iter((b"unmetered",))
    assert service.track(store="deep", expected_bytes=9, content=content) is content
    assert service.get_statuses() == ()


def test_key_quota_starts_blocked_then_reserves_and_accounts_actual_remote_bytes(
    tmp_path: Path,
) -> None:
    clock = _Clock(datetime(2026, 7, 18, tzinfo=UTC))
    config = _config(tmp_path / "catalog.sqlite3")
    initialize_db(config.database_url)
    key = _key(config, app="review")
    service = SqlAlchemyDownloadAllowance(config, clock=clock)
    key_id = str(key["id"])

    with pytest.raises(DownloadAllowanceExceeded, match="0 bytes remaining"):
        service.reserve_retrieval(
            key_id=key_id,
            job_id="blocked-job",
            expected_bytes=1,
            expires_at="2026-07-20T00:00:00.000000Z",
        )

    assigned = service.set_key_quota(app="review", key_id=key_id, monthly_bytes=20)
    assert assigned["monthly_bytes"] == 20
    assert assigned["remaining_bytes"] == 20
    service.reserve_retrieval(
        key_id=key_id,
        job_id="job-1",
        expected_bytes=10,
        expires_at="2026-07-20T00:00:00.000000Z",
    )
    reserved = service.get_key_quota(key_id=key_id)
    assert reserved["reserved_bytes"] == 10
    assert reserved["remaining_bytes"] == 10

    content = service.track(
        store="deep",
        expected_bytes=10,
        content=iter((b"actual!",)),
        attribution=DownloadAttribution(key_id=key_id, job_id="job-1"),
    )
    assert b"".join(content) == b"actual!"
    status = service.get_key_quota(key_id=key_id)
    assert status["accounted_bytes"] == 7
    assert status["reserved_bytes"] == 0
    assert status["remaining_bytes"] == 13

    retry = service.track(
        store="deep",
        expected_bytes=10,
        content=iter((b"retry",)),
        attribution=DownloadAttribution(key_id=key_id, job_id="job-1"),
    )
    assert b"".join(retry) == b"retry"
    retried = service.get_key_quota(key_id=key_id)
    assert retried["accounted_bytes"] == 12
    assert retried["remaining_bytes"] == 8


def test_key_quota_release_and_database_list_projection(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 7, 18, tzinfo=UTC))
    config = _config(tmp_path / "catalog.sqlite3")
    initialize_db(config.database_url)
    alpha = _key(config, app="alpha")
    beta = _key(config, app="beta")
    service = SqlAlchemyDownloadAllowance(config, clock=clock)
    service.set_key_quota(app="alpha", key_id=str(alpha["id"]), monthly_bytes=12)
    service.set_key_quota(app="beta", key_id=str(beta["id"]), monthly_bytes=None)
    service.reserve_retrieval(
        key_id=str(alpha["id"]),
        job_id="job-2",
        expected_bytes=8,
        expires_at="2026-07-20T00:00:00.000000Z",
    )
    service.release_retrieval(job_id="job-2")

    payload = service.list_key_quotas(
        page=1,
        per_page=1,
        q=None,
        sort="app",
        order="asc",
        active=True,
    )
    assert payload["total"] == 2
    assert payload["pages"] == 2
    assert payload["quotas"][0]["app"] == "alpha"
    assert payload["quotas"][0]["reserved_bytes"] == 0
    unlimited = service.get_key_quota(key_id=str(beta["id"]))
    assert unlimited["monthly_bytes"] is None
    assert unlimited["remaining_bytes"] is None
