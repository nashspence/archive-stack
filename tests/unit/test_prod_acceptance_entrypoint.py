from __future__ import annotations

import pytest

from tests.fixtures.production import (
    _reject_prod_djdan_factory_env,
    _require_canonical_test_entrypoint,
)


def test_prod_acceptance_requires_canonical_test_entrypoint(monkeypatch) -> None:
    monkeypatch.delenv("RIVERHOG_TEST_CANONICAL_ENTRYPOINT", raising=False)

    with pytest.raises(pytest.UsageError, match="make prod"):
        _require_canonical_test_entrypoint()


def test_prod_acceptance_allows_canonical_test_entrypoint(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_TEST_CANONICAL_ENTRYPOINT", "1")

    _require_canonical_test_entrypoint()


def test_prod_acceptance_rejects_djdan_factory_overrides() -> None:
    with pytest.raises(RuntimeError, match="DJDAN_READER_FACTORY"):
        _reject_prod_djdan_factory_env(
            {
                "DJDAN_READER_FACTORY": "tests.fixtures.djdan_fakes:FixtureOpticalReader",
                "RIVERHOG_BASE_URL": "http://app:8000",
            }
        )


def test_prod_acceptance_allows_non_factory_djdan_env() -> None:
    _reject_prod_djdan_factory_env(
        {
            "DJDAN_STAGING_DIR": "/tmp/djdan-staging",
            "RIVERHOG_BASE_URL": "http://app:8000",
        }
    )
