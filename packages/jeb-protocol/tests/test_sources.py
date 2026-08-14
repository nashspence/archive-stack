from __future__ import annotations

import pytest
from jeb_protocol import SOURCE_ID_PATTERN, SourceIdError, source_id


@pytest.mark.parametrize("value", ("a", "camera-1", "a" * 63, "0-source-9"))
def test_source_id_accepts_exact_canonical_slugs(value: str) -> None:
    assert source_id(value) == value


@pytest.mark.parametrize(
    "value",
    (
        "",
        ".",
        "..",
        "Camera",
        " camera",
        "camera ",
        "-camera",
        "camera-",
        "camera_1",
        "camera/1",
        "a" * 64,
        "café",
    ),
)
def test_source_id_rejects_noncanonical_or_ambiguous_values(value: str) -> None:
    with pytest.raises(SourceIdError):
        source_id(value)


def test_source_id_pattern_is_the_exported_wire_contract() -> None:
    assert SOURCE_ID_PATTERN == r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
