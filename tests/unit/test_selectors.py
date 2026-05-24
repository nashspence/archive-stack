from __future__ import annotations

import pytest

from riverhog_core.domain.errors import InvalidTarget
from riverhog_core.domain.selectors import parse_target


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("photos-2024/", "photos-2024/"),
        ("tax/2022/", "tax/2022/"),
        ("photos-2024/raw/", "photos-2024/raw/"),
        ("photos-2024/raw/file.jpg", "photos-2024/raw/file.jpg"),
        ("tax/2022/invoice.pdf", "tax/2022/invoice.pdf"),
    ],
)
def test_parse_target_valid(raw: str, canonical: str) -> None:
    assert parse_target(raw).canonical == canonical


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "photos-2024",
        "photos-2024/./raw/",
        "photos-2024/a/../b",
        "photos-2024//raw/",
        "tax//2022",
        "tax/./2022",
        "/tax/2022",
    ],
)
def test_parse_target_invalid(raw: str) -> None:
    with pytest.raises(InvalidTarget):
        parse_target(raw)
