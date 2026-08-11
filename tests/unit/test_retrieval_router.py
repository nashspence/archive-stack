from __future__ import annotations

from collections.abc import Iterator

from riverhog_api.routers.retrieval import _iter_range


def test_whole_file_response_exhausts_verified_retrieval_source() -> None:
    completed: list[bool] = []

    def chunks() -> Iterator[bytes]:
        yield b"payload"
        completed.append(True)

    assert b"".join(_iter_range(chunks(), start=0, size=7, exhaust_source=True)) == b"payload"
    assert completed == [True]


def test_partial_response_stops_after_requested_range() -> None:
    completed: list[bool] = []

    def chunks() -> Iterator[bytes]:
        yield b"payload"
        completed.append(True)

    assert b"".join(_iter_range(chunks(), start=1, size=3)) == b"ayl"
    assert completed == []
