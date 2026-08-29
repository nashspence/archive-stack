from __future__ import annotations

import pytest
from http_api_contracts import (
    parse_quoted_sha256_identity,
    quote_sha256_identity,
    validate_sha256_identity,
)
from pydantic import ValidationError


def test_sha256_http_identity_round_trips_exactly() -> None:
    identity = "a" * 64

    assert validate_sha256_identity(identity) == identity
    assert quote_sha256_identity(identity) == f'"{identity}"'
    assert parse_quoted_sha256_identity(f'"{identity}"') == identity


@pytest.mark.parametrize(
    "value",
    (
        "A" * 64,
        "a" * 63,
        "a" * 65,
        f' "{"a" * 64}"',
        f'W/"{"a" * 64}"',
        "a" * 64,
    ),
)
def test_quoted_sha256_http_identity_rejects_noncanonical_forms(value: str) -> None:
    with pytest.raises(ValidationError):
        parse_quoted_sha256_identity(value)
