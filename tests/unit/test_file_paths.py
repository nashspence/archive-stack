from __future__ import annotations

import pytest

from riverhog_core.domain.errors import InvalidPath
from riverhog_core.domain.file_paths import parse_logical_path


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("2025/", "2025/"),
        (
            "2025/20250712T213200Z__photos/",
            "2025/20250712T213200Z__photos/",
        ),
        (
            "2025/20250712T213200Z__photos/raw/",
            "2025/20250712T213200Z__photos/raw/",
        ),
        (
            "2025/20250712T213200Z__photos/raw/file.jpg",
            "2025/20250712T213200Z__photos/raw/file.jpg",
        ),
    ],
)
def test_parse_logical_path_valid(raw: str, canonical: str) -> None:
    assert parse_logical_path(raw).canonical == canonical


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "2025",
        "2025/20250712T213200Z__photos",
        "2025/20250712T213200Z__photos/./raw/",
        "2025/20250712T213200Z__photos/a/../b",
        "2025/20250712T213200Z__photos//raw/",
        "/2025/20250712T213200Z__photos/",
    ],
)
def test_parse_logical_path_invalid(raw: str) -> None:
    with pytest.raises(InvalidPath):
        parse_logical_path(raw)
