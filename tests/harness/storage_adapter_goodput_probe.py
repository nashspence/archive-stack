"""Measure the public storage-adapter path with bounded synthetic content."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path

from riverhog_storage_adapter_protocol import (
    DeletePrefixRequest,
    ObjectLocator,
    ObjectReadRequest,
    WriteCompleteRequest,
    WriteStartRequest,
    validate_completed_write_response,
)
from riverhog_storage_adapter_support import StorageAdapterClient

_MIB = 1024 * 1024
_ONE_GBPS_MIB_PER_SECOND = 1_000_000_000 / 8 / _MIB


def _chunks(byte_count: int, *, value: int) -> Iterator[bytes]:
    block = bytes([value]) * _MIB
    remaining = byte_count
    while remaining:
        chunk = block[: min(remaining, len(block))]
        yield chunk
        remaining -= len(chunk)


def run(
    *,
    base_url: str,
    token_file: Path,
    payload_bytes: int,
    baseline_mib_per_second: float,
) -> dict[str, object]:
    if payload_bytes < 1:
        raise ValueError("payload bytes must be positive")
    if baseline_mib_per_second <= 0:
        raise ValueError("baseline must be positive")
    client = StorageAdapterClient.from_token_file(
        base_url,
        token_file=token_file,
        allow_insecure_http=True,
        timeout=300,
    )
    prefix = f"goodput/{uuid.uuid4().hex}"
    object_path = f"{prefix}/payload.bin"
    try:
        descriptor = client.descriptor()
        segment_bytes = descriptor.maximum_segment_bytes
        request = WriteStartRequest(
            object_path=object_path,
            expected_bytes=payload_bytes,
            content_type="application/octet-stream",
            required_identity_assertions={"riverhog-conformance": "goodput/v1"},
            placement="immediate",
        )
        expected = hashlib.sha256()
        expected_offset = 0
        expected_number = 1
        while expected_offset < payload_bytes:
            current_bytes = min(segment_bytes, payload_bytes - expected_offset)
            for chunk in _chunks(current_bytes, value=expected_number % 251):
                expected.update(chunk)
            expected_offset += current_bytes
            expected_number += 1

        upload_started = time.perf_counter()
        session = client.begin_write(request)
        admitted = time.perf_counter()
        receipts = []
        offset = 0
        number = 1
        while offset < payload_bytes:
            current_bytes = min(segment_bytes, payload_bytes - offset)
            value = number % 251
            receipts.append(
                client.write_segment(
                    session=session,
                    number=number,
                    stored_bytes=current_bytes,
                    content=_chunks(current_bytes, value=value),
                )
            )
            offset += current_bytes
            number += 1
        written = time.perf_counter()
        completion = WriteCompleteRequest(
            session=session,
            segments=tuple(receipts),
            expected_bytes=payload_bytes,
            expected_content_type=request.content_type,
            required_identity_assertions=request.required_identity_assertions,
            expected_placement=request.placement,
        )
        completed = client.complete_write(completion)
        validate_completed_write_response(completion, completed)
        completed_at = time.perf_counter()
        upload_seconds = completed_at - upload_started

        observed = hashlib.sha256()
        read_started = time.perf_counter()
        with client.read_object(
            ObjectReadRequest(
                object=ObjectLocator(
                    object_path=completed.object_path,
                    revision=completed.revision,
                ),
                expected_bytes=payload_bytes,
            )
        ) as stream:
            for chunk in stream.content:
                observed.update(chunk)
        read_seconds = time.perf_counter() - read_started
        if observed.digest() != expected.digest():
            raise RuntimeError("storage-adapter goodput probe changed the payload")

        upload_rate = payload_bytes / _MIB / upload_seconds
        read_rate = payload_bytes / _MIB / read_seconds
        return {
            "format": "riverhog-storage-adapter-goodput/v1",
            "admission_seconds": round(admitted - upload_started, 6),
            "baseline_mib_per_second": baseline_mib_per_second,
            "completion_seconds": round(completed_at - written, 6),
            "payload_bytes": payload_bytes,
            "read_seconds": round(read_seconds, 6),
            "read_mib_per_second": round(read_rate, 3),
            "read_target_met": read_rate / baseline_mib_per_second >= 0.9,
            "read_utilization": round(read_rate / baseline_mib_per_second, 4),
            "target_utilization": 0.9,
            "upload_mib_per_second": round(upload_rate, 3),
            "upload_target_met": upload_rate / baseline_mib_per_second >= 0.9,
            "upload_utilization": round(upload_rate / baseline_mib_per_second, 4),
            "write_seconds": round(written - admitted, 6),
        }
    finally:
        client.delete_prefix(DeletePrefixRequest(object_prefix=f"{prefix}/"))
        client.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="storage-adapter-goodput-probe")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--payload-bytes", type=int, default=128 * _MIB)
    parser.add_argument(
        "--baseline-mib-per-second",
        type=float,
        default=_ONE_GBPS_MIB_PER_SECOND,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    print(
        json.dumps(
            run(
                base_url=args.base_url,
                token_file=args.token_file,
                payload_bytes=args.payload_bytes,
                baseline_mib_per_second=args.baseline_mib_per_second,
            ),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run"]
