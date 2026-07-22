from __future__ import annotations

import pytest
from riverhog_core.domain.file_paths import parse_logical_path
from riverhog_protocol.errors import InvalidPath


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("photos/", "photos/"),
        (
            "photos/20250712T213200Z/",
            "photos/20250712T213200Z/",
        ),
        (
            "photos/20250712T213200Z/raw/",
            "photos/20250712T213200Z/raw/",
        ),
        (
            "photos/20250712T213200Z/raw/file.jpg",
            "photos/20250712T213200Z/raw/file.jpg",
        ),
    ],
)
def test_parse_logical_path_valid(raw: str, canonical: str) -> None:
    assert parse_logical_path(raw).canonical == canonical


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "photos",
        "photos/20250712T213200Z",
        "photos/20250712T213200Z/./raw/",
        "photos/20250712T213200Z/a/../b",
        "photos/20250712T213200Z//raw/",
        "/photos/20250712T213200Z/",
    ],
)
def test_parse_logical_path_invalid(raw: str) -> None:
    with pytest.raises(InvalidPath):
        parse_logical_path(raw)
