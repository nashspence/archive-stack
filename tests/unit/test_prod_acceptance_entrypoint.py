from __future__ import annotations

import pytest

from tests.fixtures.production import (
    _reject_prod_arc_disc_factory_env,
    _require_canonical_test_entrypoint,
)


def test_prod_acceptance_requires_canonical_test_entrypoint(monkeypatch) -> None:
    monkeypatch.delenv("ARC_TEST_CANONICAL_ENTRYPOINT", raising=False)

    with pytest.raises(pytest.UsageError, match="make prod"):
        _require_canonical_test_entrypoint()


def test_prod_acceptance_allows_canonical_test_entrypoint(monkeypatch) -> None:
    monkeypatch.setenv("ARC_TEST_CANONICAL_ENTRYPOINT", "1")

    _require_canonical_test_entrypoint()


def test_prod_acceptance_rejects_arc_disc_factory_overrides() -> None:
    with pytest.raises(RuntimeError, match="ARC_DISC_READER_FACTORY"):
        _reject_prod_arc_disc_factory_env(
            {
                "ARC_DISC_READER_FACTORY": "tests.fixtures.arc_disc_fakes:FixtureOpticalReader",
                "ARC_BASE_URL": "http://app:8000",
            }
        )


def test_prod_acceptance_allows_non_factory_arc_disc_env() -> None:
    _reject_prod_arc_disc_factory_env(
        {
            "ARC_DISC_STAGING_DIR": "/tmp/arc-disc-staging",
            "ARC_BASE_URL": "http://app:8000",
        }
    )
