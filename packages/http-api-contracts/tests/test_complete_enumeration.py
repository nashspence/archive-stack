from __future__ import annotations

import json
from typing import Literal

import pytest
from http_api_contracts import (
    CompleteEnumerationReader,
    closed_literal_values,
    complete_enumeration_schema_identity,
    iter_complete_enumeration,
)
from pydantic import BaseModel, ConfigDict


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int


type _ModernControl = Literal["alpha", "beta"]
_ConventionalControl = Literal["alpha", "beta"]


@pytest.mark.parametrize("control", (_ModernControl, _ConventionalControl))
def test_closed_literal_values_supports_both_public_alias_styles(control: object) -> None:
    assert closed_literal_values(control) == {"alpha", "beta"}


def _stream(*, chunk_bytes: int = 7) -> tuple[list[bytes], dict[str, object], object]:
    query = {"order": "asc", "q": None}
    schema = complete_enumeration_schema_identity(
        _Item,
        schema_id="fixture-item/v1",
    )
    body = b"".join(
        iter_complete_enumeration(
            ({"id": value} for value in range(3)),
            query=query,
            item_schema=schema,
        )
    )
    return (
        [body[index : index + chunk_bytes] for index in range(0, len(body), chunk_bytes)],
        query,
        schema,
    )


def test_complete_enumeration_round_trips_across_arbitrary_chunks() -> None:
    chunks, query, schema = _stream()
    reader = CompleteEnumerationReader(
        chunks,
        item_type=_Item,
        expected_query=query,
        expected_item_schema=schema,
    )

    assert [item.id for item in reader] == [0, 1, 2]
    reader.require_complete()
    assert reader.count == 3
    assert reader.items_sha256 is not None


@pytest.mark.parametrize("fault", ["truncated", "ordinal", "digest", "schema"])
def test_complete_enumeration_fails_closed(fault: str) -> None:
    chunks, query, schema = _stream(chunk_bytes=10_000)
    body = b"".join(chunks)
    if fault == "truncated":
        body = body.rsplit(b"\x1e", 1)[0]
    else:
        records = [record for record in body.split(b"\x1e") if record]
        index = 1 if fault == "ordinal" else -1 if fault == "digest" else 0
        record = json.loads(records[index])
        if fault == "ordinal":
            record["ordinal"] = 9
        elif fault == "digest":
            record["items_sha256"] = "0" * 64
        else:
            record["item_schema"]["sha256"] = "0" * 64
        records[index] = json.dumps(record, separators=(",", ":")).encode() + b"\n"
        body = b"".join(b"\x1e" + record for record in records)
    reader = CompleteEnumerationReader(
        [body],
        item_type=_Item,
        expected_query=query,
        expected_item_schema=schema,
    )

    with pytest.raises(ValueError):
        list(reader)
    assert reader.complete is False


def test_complete_enumeration_requires_terminal_consumption() -> None:
    chunks, query, schema = _stream()
    reader = CompleteEnumerationReader(
        chunks,
        item_type=_Item,
        expected_query=query,
        expected_item_schema=schema,
    )
    iterator = iter(reader)

    assert next(iterator).id == 0
    with pytest.raises(ValueError, match="terminal proof"):
        reader.require_complete()


def test_complete_enumeration_drains_source_before_consumer_controls_delivery() -> None:
    produced: list[int] = []
    schema = complete_enumeration_schema_identity(_Item, schema_id="fixture-item/v1")

    def items():  # type: ignore[no-untyped-def]
        for value in range(250):
            produced.append(value)
            yield {"id": value}

    stream = iter_complete_enumeration(items(), query={}, item_schema=schema)

    assert json.loads(next(stream)[1:])["type"] == "begin"
    assert produced == list(range(250))
    stream.close()


def test_complete_enumeration_crosses_multiple_database_sized_partitions() -> None:
    query = {"order": "asc"}
    schema = complete_enumeration_schema_identity(_Item, schema_id="fixture-item/v1")
    body = b"".join(
        iter_complete_enumeration(
            ({"id": value} for value in range(250)),
            query=query,
            item_schema=schema,
        )
    )
    chunks = [body[index : index + 31] for index in range(0, len(body), 31)]
    reader = CompleteEnumerationReader(
        chunks,
        item_type=_Item,
        expected_query=query,
        expected_item_schema=schema,
    )

    assert [item.id for item in reader] == list(range(250))
    reader.require_complete()
    assert reader.count == 250


@pytest.mark.parametrize("fault", ["query", "item", "trailing"])
def test_complete_enumeration_rejects_mismatched_query_invalid_items_and_trailing_frames(
    fault: str,
) -> None:
    chunks, query, schema = _stream(chunk_bytes=10_000)
    body = b"".join(chunks)
    if fault == "trailing":
        body += b'\x1e{"frame":"begin"}\n'
    else:
        records = [record for record in body.split(b"\x1e") if record]
        index = 0 if fault == "query" else 1
        record = json.loads(records[index])
        if fault == "query":
            record["query"] = {"order": "desc", "q": None}
        else:
            record["item"] = {"id": "not-an-integer"}
        records[index] = json.dumps(record, separators=(",", ":")).encode() + b"\n"
        body = b"".join(b"\x1e" + record for record in records)
    reader = CompleteEnumerationReader(
        [body],
        item_type=_Item,
        expected_query=query,
        expected_item_schema=schema,
    )

    with pytest.raises(ValueError):
        list(reader)
    assert reader.complete is False
