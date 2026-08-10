from __future__ import annotations

import base64
import uuid
from pathlib import Path

import pytest
from riverhog_provenance import provenance_journal_filename
from riverhog_provenance.common import (
    format_utc_ns,
    locator_from_path,
    new_urn_uuid,
    retained_native_value,
)
from riverhog_provenance.model import (
    LargeValueDisposition,
    ObservationPolicy,
    ObservationRequest,
)


def test_uuid7_urn_is_canonical() -> None:
    value = new_urn_uuid()
    assert value.startswith("urn:uuid:")
    parsed = uuid.UUID(value.removeprefix("urn:uuid:"))
    assert parsed.version == 7
    assert str(parsed) == value.removeprefix("urn:uuid:")


def test_provenance_journal_filename_is_one_canonical_path_segment() -> None:
    journal_id = "urn:uuid:00000000-0000-4000-8000-000000000042"

    filename = provenance_journal_filename(journal_id)

    assert filename == f"{journal_id}.json-seq"
    assert filename == Path(filename).name


@pytest.mark.parametrize(
    "journal_id",
    (
        "00000000-0000-4000-8000-000000000042",
        "urn:uuid:00000000-0000-4000-8000-000000000042/../../outside",
        "urn:uuid:00000000-0000-4000-8000-000000000042\\outside",
        "urn:uuid:00000000-0000-4000-8000-000000000042-extra",
    ),
)
def test_provenance_journal_filename_requires_canonical_uuid_urn(journal_id: str) -> None:
    with pytest.raises(ValueError):
        provenance_journal_filename(journal_id)


def test_format_utc_ns_preserves_nanoseconds() -> None:
    assert format_utc_ns(1_234_567_890) == "1970-01-01T00:00:01.23456789Z"
    assert format_utc_ns(0) == "1970-01-01T00:00:00Z"


def test_locator_preserves_non_utf8_path_bytes() -> None:
    raw = b"/archive/non-utf8-\xff.bin"
    locator = locator_from_path(
        raw,
        kind="absolute",
        authority_id="urn:uuid:00000000-0000-0000-0000-000000000001",
    )
    assert locator["text_role"] == "display"
    assert base64.b64decode(locator["bytes"]["data"]) == raw
    assert locator["text"].startswith("bytes:")


def test_large_native_value_becomes_digest_only(urn_factory) -> None:
    request = ObservationRequest(
        path="unused",
        lineage_id=urn_factory(),
        host_id=urn_factory(),
        policy=ObservationPolicy(
            inline_native_value_bytes=2,
            maximum_native_value_bytes=100,
            large_value_disposition=LargeValueDisposition.DIGEST_ONLY,
        ),
    )
    status, value, note = retained_native_value(b"abcdef", agent_id=urn_factory(), request=request)
    assert status == "digest_only"
    assert value is not None and value["type"] == "digest"
    assert value["byte_length"] == 6
    assert note is None


def test_policy_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError):
        ObservationPolicy(inline_native_value_bytes=10, maximum_native_value_bytes=5)
