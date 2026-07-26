from __future__ import annotations

import pytest
from riverhog_core.domain.file_paths import parse_logical_path
from riverhog_protocol.errors import InvalidPath


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("12/", "12/"),
        (
            "12/raw/",
            "12/raw/",
        ),
        (
            "12/raw/file.jpg",
            "12/raw/file.jpg",
        ),
    ],
)
def test_parse_logical_path_valid(raw: str, canonical: str) -> None:
    assert parse_logical_path(raw).canonical == canonical


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "12",
        "photos/",
        "12/./raw/",
        "12/a/../b",
        "12//raw/",
        "/12/",
    ],
)
def test_parse_logical_path_invalid(raw: str) -> None:
    with pytest.raises(InvalidPath):
        parse_logical_path(raw)
