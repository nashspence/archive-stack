"""Destructive, prefix-confined conformance checks for one adapter registration."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from riverhog_storage_adapter_protocol import (
    STORAGE_ADAPTER_PROTOCOL,
    CompletedWriteLookupRequest,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ObjectHeadRequest,
    ObjectLocator,
    ObjectReadRequest,
    ReadPreparationRequest,
    SmallObjectWriteRequest,
    WriteCompleteRequest,
    WriteStartRequest,
    normalize_object_path,
)

from riverhog_storage_adapter_support.client import (
    StorageAdapterClient,
    StorageAdapterProtocolError,
)

STORAGE_ADAPTER_CONFORMANCE_RESULT: Literal["riverhog-storage-adapter-conformance-result/v1"] = (
    "riverhog-storage-adapter-conformance-result/v1"
)


class StorageAdapterConformanceResult(BaseModel):
    """Stable positive evidence returned after the complete check set passes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["riverhog-storage-adapter-conformance-result/v1"] = (
        STORAGE_ADAPTER_CONFORMANCE_RESULT
    )
    protocol: Literal["riverhog-storage-adapter/v1"] = STORAGE_ADAPTER_PROTOCOL
    implementation_id: str
    implementation_version: str
    checks: tuple[str, ...]


def run_storage_adapter_conformance(
    client: StorageAdapterClient,
    *,
    object_prefix: str,
) -> StorageAdapterConformanceResult:
    """Exercise the public contract beneath one disposable, owned prefix.

    The caller must provide a unique prefix that may be deleted in its entirety.
    Provider provisioning and provider-specific restore qualification are outside
    this protocol-level check.
    """

    normalized_prefix = normalize_object_path(object_prefix, allow_prefix=True).rstrip("/")
    cleanup_prefix = f"{normalized_prefix}/"
    descriptor = client.descriptor()
    checks: list[str] = ["descriptor"]
    small_path = f"{normalized_prefix}/small.bin"
    write_path = f"{normalized_prefix}/resumable-write.bin"
    small_content = b"riverhog storage adapter conformance v1\n"
    small_sha256 = hashlib.sha256(small_content).hexdigest()
    small_request = SmallObjectWriteRequest(
        object_path=small_path,
        content_type="application/octet-stream",
        identity_metadata={"riverhog-conformance": "small/v1"},
        placement="immediate",
        mode="create_only",
        stored_bytes=len(small_content),
        stored_sha256=small_sha256,
    )
    try:
        first_small = client.put_small_object(small_request, small_content)
        retry_content = small_content + b"randomized-ciphertext"
        retry_request = small_request.model_copy(
            update={
                "stored_bytes": len(retry_content),
                "stored_sha256": hashlib.sha256(retry_content).hexdigest(),
            }
        )
        retried_small = client.put_small_object(retry_request, retry_content)
        if retried_small != first_small:
            raise AssertionError("create-only retry did not return the original object receipt")
        checks.append("create-only-retry")

        metadata = client.head_object(
            ObjectHeadRequest(
                object=ObjectLocator(object_path=small_path),
                expected_placement="immediate",
            )
        )
        if (
            metadata is None
            or metadata.stored_bytes != len(small_content)
            or metadata.stored_sha256 != small_sha256
            or metadata.identity_metadata != small_request.identity_metadata
        ):
            raise AssertionError("small-object metadata differs from its exact input")
        checks.append("exact-metadata")

        range_content = b"".join(
            client.iter_object(
                ObjectReadRequest(
                    object=ObjectLocator(
                        object_path=small_path,
                        revision=first_small.revision,
                    ),
                    expected_bytes=len(small_content),
                    offset=9,
                    size=15,
                )
            )
        )
        if range_content != small_content[9:24]:
            raise AssertionError("exact range differs from the stored object")
        checks.append("exact-range")

        conflicting = retry_request.model_copy(
            update={"identity_metadata": {"riverhog-conformance": "different/v1"}}
        )
        try:
            client.put_small_object(conflicting, retry_content)
        except StorageAdapterProtocolError as exc:
            if exc.code != "identity_conflict":
                raise AssertionError("changed identity returned the wrong error code") from exc
        else:
            raise AssertionError("create-only write accepted a changed identity")
        checks.append("identity-conflict")

        segment_count = (
            2
            if descriptor.maximum_segment_count is None or descriptor.maximum_segment_count >= 2
            else 1
        )
        first_segment_bytes = descriptor.minimum_nonfinal_segment_bytes if segment_count == 2 else 1
        if (
            descriptor.maximum_segment_bytes is not None
            and first_segment_bytes > descriptor.maximum_segment_bytes
        ):
            raise AssertionError("adapter descriptor contains unusable write-segment limits")
        segment_contents = [b"a" * first_segment_bytes]
        if segment_count == 2:
            segment_contents.append(b"final")
        write_request = WriteStartRequest(
            object_path=write_path,
            content_type="application/octet-stream",
            identity_metadata={"riverhog-conformance": "resumable-write/v1"},
            placement="immediate",
        )
        session = client.begin_write(write_request)
        written_segments = tuple(
            client.write_segment(session=session, number=index, content=content)
            for index, content in enumerate(segment_contents, start=1)
        )
        listed_segments = client.list_segments(session)
        if listed_segments != written_segments:
            raise AssertionError("write listing differs from written segment receipts")
        checks.append("write-reconciliation")

        total_bytes = sum(len(content) for content in segment_contents)
        completion_request = WriteCompleteRequest(
            session=session,
            segments=listed_segments,
            expected_bytes=total_bytes,
            expected_identity_metadata=write_request.identity_metadata,
            expected_placement=write_request.placement,
        )
        completed = client.complete_write(completion_request)
        recovered_completion = client.complete_write(completion_request)
        if recovered_completion != completed:
            raise AssertionError("lost completion response did not reconcile exactly")
        headed_completion = client.find_completed_write(
            CompletedWriteLookupRequest(
                object_path=write_path,
                expected_identity_metadata=write_request.identity_metadata,
                expected_placement=write_request.placement,
            )
        )
        if headed_completion != completed:
            raise AssertionError("completed write lookup differs from its receipt")
        checks.append("write-completion-recovery")

        write_content = b"".join(segment_contents)
        stored_write = b"".join(
            client.iter_object(
                ObjectReadRequest(
                    object=ObjectLocator(
                        object_path=write_path,
                        revision=completed.revision,
                    ),
                    expected_bytes=total_bytes,
                )
            )
        )
        if stored_write != write_content:
            raise AssertionError("completed write bytes differ from its segments")
        checks.append("write-stream")

        preparation = ReadPreparationRequest(
            objects=(
                ObjectLocator(
                    object_path=write_path,
                    revision=completed.revision,
                ),
            )
        )
        prepared = client.prepare_read(preparation)
        status = client.read_status(preparation)
        if prepared.state not in {"ready", "requested"} or status.state not in {
            "ready",
            "requested",
            "expired",
        }:
            raise AssertionError("adapter returned an invalid read-preparation state")
        client.cleanup_read(preparation)
        checks.append("read-preparation")

        aborted = client.begin_write(
            write_request.model_copy(update={"object_path": f"{normalized_prefix}/aborted.bin"})
        )
        client.abort_write(aborted)
        client.abort_write(aborted)
        checks.append("write-abort")

        delete_request = (
            DeleteObjectRequest(
                object=ObjectLocator(
                    object_path=small_path,
                    revision=first_small.revision,
                ),
                mode="exact_revision",
            )
            if first_small.revision is not None
            else DeleteObjectRequest(
                object=ObjectLocator(object_path=small_path),
                mode="current",
            )
        )
        client.delete_object(delete_request)
        if (
            client.head_object(
                ObjectHeadRequest(
                    object=ObjectLocator(object_path=small_path),
                    expected_placement="immediate",
                )
            )
            is not None
        ):
            raise AssertionError("exact object deletion did not remove the target")
        checks.append("exact-deletion")

        affected = client.delete_prefix(DeletePrefixRequest(object_prefix=cleanup_prefix))
        if affected < 1:
            raise AssertionError("version-aware prefix cleanup did not remove its test object")
        if (
            client.head_object(
                ObjectHeadRequest(
                    object=ObjectLocator(object_path=write_path),
                    expected_placement="immediate",
                )
            )
            is not None
        ):
            raise AssertionError("version-aware prefix cleanup left its test object")
        checks.append("version-aware-prefix-cleanup")
    finally:
        client.delete_prefix(DeletePrefixRequest(object_prefix=cleanup_prefix))

    return StorageAdapterConformanceResult(
        implementation_id=descriptor.implementation_id,
        implementation_version=descriptor.implementation_version,
        checks=tuple(checks),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="riverhog-storage-adapter-conformance",
        description=(
            "Run destructive Riverhog storage-adapter checks beneath a unique temporary prefix."
        ),
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--object-prefix", required=True)
    parser.add_argument("--allow-insecure-http", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    base_prefix = normalize_object_path(args.object_prefix, allow_prefix=True).rstrip("/")
    run_prefix = f"{base_prefix}/{uuid.uuid4().hex}"
    client = StorageAdapterClient.from_token_file(
        args.base_url,
        token_file=args.token_file,
        allow_insecure_http=args.allow_insecure_http,
    )
    try:
        result = run_storage_adapter_conformance(client, object_prefix=run_prefix)
    finally:
        client.close()
    print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
    return 0


__all__ = [
    "STORAGE_ADAPTER_CONFORMANCE_RESULT",
    "StorageAdapterConformanceResult",
    "main",
    "run_storage_adapter_conformance",
]
