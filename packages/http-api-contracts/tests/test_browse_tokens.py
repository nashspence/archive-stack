from __future__ import annotations

import pytest
from http_api_contracts import (
    MAX_BROWSE_QUERY_CHARACTERS,
    MAX_BROWSE_TOKEN_BYTES,
    BrowsePageToken,
    BrowseQuery,
    BrowseTokenCodec,
    BrowseTokenError,
)
from pydantic import TypeAdapter, ValidationError


def _codec(*, now: float = 100.0) -> BrowseTokenCodec:
    return BrowseTokenCodec(
        b"bounded-browse-test-signing-key-v1",
        lifetime_seconds=60,
        clock=lambda: now,
    )


def test_browse_token_round_trips_opaque_binary_position_across_restart() -> None:
    issued = _codec().issue(
        operation="list_files",
        principal={"app": "reader", "key_id": "key-1"},
        selectors={"q": "camera", "sort": "path", "order": "asc"},
        position=(b"camera/\xff", 41),
    )

    assert _codec().verify(
        issued,
        operation="list_files",
        principal={"app": "reader", "key_id": "key-1"},
        selectors={"q": "camera", "sort": "path", "order": "asc"},
    ) == (b"camera/\xff", 41)


@pytest.mark.parametrize(
    ("operation", "principal", "selectors", "message"),
    (
        ("list_collections", {"app": "reader", "key_id": "key-1"}, {}, "operation"),
        ("list_files", {"app": "other", "key_id": "key-2"}, {}, "principal"),
        (
            "list_files",
            {"app": "reader", "key_id": "key-1"},
            {"q": "changed"},
            "selectors",
        ),
    ),
)
def test_browse_token_fails_closed_outside_its_request_binding(
    operation: str,
    principal: object,
    selectors: dict[str, object],
    message: str,
) -> None:
    token = _codec().issue(
        operation="list_files",
        principal={"app": "reader", "key_id": "key-1"},
        selectors={},
        position=(1,),
    )

    with pytest.raises(BrowseTokenError, match=message):
        _codec().verify(
            token,
            operation=operation,
            principal=principal,
            selectors=selectors,
        )


def test_browse_token_rejects_tampering_and_expiry() -> None:
    token = _codec().issue(
        operation="list_files",
        principal="reader",
        selectors={},
        position=(1,),
    )

    with pytest.raises(BrowseTokenError, match="integrity"):
        _codec().verify(
            token[:-1] + ("A" if token[-1] != "A" else "B"),
            operation="list_files",
            principal="reader",
            selectors={},
        )
    with pytest.raises(BrowseTokenError, match="expired"):
        _codec(now=161).verify(
            token,
            operation="list_files",
            principal="reader",
            selectors={},
        )


def test_browse_token_rejects_a_different_runtime_signing_configuration() -> None:
    token = _codec().issue(
        operation="list_files",
        principal="reader",
        selectors={},
        position=(1,),
    )
    changed = BrowseTokenCodec(
        b"different-browse-test-signing-key-v1",
        lifetime_seconds=60,
        clock=lambda: 100.0,
    )

    with pytest.raises(BrowseTokenError, match="integrity"):
        changed.verify(
            token,
            operation="list_files",
            principal="reader",
            selectors={},
        )


def test_browse_query_is_bounded_canonical_visible_text() -> None:
    adapter = TypeAdapter(BrowseQuery)

    assert adapter.validate_python("A query") == "A query"
    assert adapter.validate_python("x" * MAX_BROWSE_QUERY_CHARACTERS) == (
        "x" * MAX_BROWSE_QUERY_CHARACTERS
    )
    for invalid in ("", " ", " leading", "trailing ", "e\u0301", "x" * 4097):
        with pytest.raises(ValidationError):
            adapter.validate_python(invalid)


def test_browse_page_token_has_one_public_encoded_bound() -> None:
    adapter = TypeAdapter(BrowsePageToken)

    assert adapter.validate_python("x" * MAX_BROWSE_TOKEN_BYTES) == ("x" * MAX_BROWSE_TOKEN_BYTES)
    with pytest.raises(ValidationError):
        adapter.validate_python("")
    with pytest.raises(ValidationError):
        adapter.validate_python("x" * (MAX_BROWSE_TOKEN_BYTES + 1))
