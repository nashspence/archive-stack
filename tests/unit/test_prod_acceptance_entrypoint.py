from __future__ import annotations

import pytest

from tests.fixtures.production import _require_canonical_test_entrypoint


def test_prod_acceptance_requires_canonical_test_entrypoint(monkeypatch) -> None:
    monkeypatch.delenv("ARC_TEST_CANONICAL_ENTRYPOINT", raising=False)

    with pytest.raises(pytest.UsageError, match="make prod"):
        _require_canonical_test_entrypoint()


def test_prod_acceptance_allows_canonical_test_entrypoint(monkeypatch) -> None:
    monkeypatch.setenv("ARC_TEST_CANONICAL_ENTRYPOINT", "1")

    _require_canonical_test_entrypoint()
