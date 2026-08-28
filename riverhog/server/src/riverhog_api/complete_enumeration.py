from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from fastapi.responses import StreamingResponse
from http_api_contracts import (
    JSON_SEQUENCE_MEDIA_TYPE,
    bounded_list_operation,
    complete_enumeration_operation,
    complete_enumeration_schema_identity,
    iter_complete_enumeration,
)
from pydantic import TypeAdapter


class CompleteEnumerationResponse(StreamingResponse):
    media_type = JSON_SEQUENCE_MEDIA_TYPE


def complete_enumeration_response(
    items: Iterable[object],
    *,
    query: Mapping[str, object],
    item_type: object,
    schema_id: str,
) -> CompleteEnumerationResponse:
    """Stream one exact read snapshot using the shared complete-enumeration wire contract."""

    adapter: TypeAdapter[Any] = TypeAdapter(item_type)
    item_schema = complete_enumeration_schema_identity(item_type, schema_id=schema_id)
    encoded_items = (
        adapter.dump_python(adapter.validate_python(item), mode="json", warnings="error")
        for item in items
    )
    return CompleteEnumerationResponse(
        iter_complete_enumeration(encoded_items, query=query, item_schema=item_schema),
        media_type=JSON_SEQUENCE_MEDIA_TYPE,
    )


__all__ = [
    "bounded_list_operation",
    "CompleteEnumerationResponse",
    "complete_enumeration_operation",
    "complete_enumeration_response",
]
