from __future__ import annotations

import pytest
from riverhog_api.routers.retrieval import _parse_range
from riverhog_protocol import InvalidRange


def test_range_parser_resolves_exact_head_and_tail_ranges() -> None:
    assert _parse_range("bytes=0-6", 20) == (0, 7)
    assert _parse_range("bytes=10-999", 20) == (10, 20)
    assert _parse_range("bytes=-7", 20) == (13, 20)


def test_range_parser_declares_unsatisfiable_ranges() -> None:
    with pytest.raises(InvalidRange):
        _parse_range("bytes=20-21", 20)
