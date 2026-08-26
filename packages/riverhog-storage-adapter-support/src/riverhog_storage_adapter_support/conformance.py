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
    WriteSession,
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
    continuation_client: StorageAdapterClient,
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
    sparse_write_path = f"{normalized_prefix}/sparse-resumable-write.bin"
    small_content = b"riverhog storage adapter conformance v1\n"
    small_sha256 = hashlib.sha256(small_content).hexdigest()
    small_request = SmallObjectWriteRequest(
        object_path=small_path,
        content_type="application/octet-stream",
        required_identity_assertions={"riverhog-conformance": "small/v1"},
        placement="immediate",
        mode="create_only",
        stored_bytes=len(small_content),
        stored_sha256=small_sha256,
    )
    try:
        first_small = client.put_small_object(small_request, small_content)
        if (
            first_small.verified_identity_assertions != small_request.required_identity_assertions
            or first_small.verified_placement != small_request.placement
        ):
            raise AssertionError("small-object receipt does not attest its exact request")
        retried_small = client.put_small_object(small_request, small_content)
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
            or metadata.required_identity_assertions != small_request.required_identity_assertions
            or metadata.verified_placement != small_request.placement
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

        conflicting_content = small_content + b"different-ciphertext"
        conflicting = small_request.model_copy(
            update={
                "required_identity_assertions": {"riverhog-conformance": "different/v1"},
                "stored_bytes": len(conflicting_content),
                "stored_sha256": hashlib.sha256(conflicting_content).hexdigest(),
            }
        )
        try:
            client.put_small_object(conflicting, conflicting_content)
        except StorageAdapterProtocolError as exc:
            if exc.code != "identity_conflict":
                raise AssertionError("changed identity returned the wrong error code") from exc
        else:
            raise AssertionError("create-only write accepted a changed identity")
        checks.append("identity-conflict")

        if descriptor.maximum_segment_count is None or descriptor.maximum_segment_count >= 2:
            sparse_request = WriteStartRequest(
                object_path=sparse_write_path,
                content_type="application/octet-stream",
                required_identity_assertions={"riverhog-conformance": "sparse-resumable-write/v1"},
                placement="immediate",
            )
            sparse_session = client.begin_write(sparse_request)
            persisted_sparse_session = WriteSession.model_validate_json(
                sparse_session.model_dump_json()
            )
            sparse_content = b"s" * descriptor.minimum_nonfinal_segment_bytes
            second_segment = continuation_client.write_segment(
                session=persisted_sparse_session,
                number=2,
                stored_bytes=len(sparse_content),
                content=sparse_content,
            )
            sparse_listing = continuation_client.list_segments(persisted_sparse_session)
            if sparse_listing.segments != (second_segment,):
                raise AssertionError("sparse write listing differs after continuation restart")
            continuation_client.abort_write(persisted_sparse_session)
            checks.append("sparse-write-reconciliation")

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
            required_identity_assertions={"riverhog-conformance": "resumable-write/v1"},
            placement="immediate",
        )
        session = client.begin_write(write_request)
        first_segment = client.write_segment(
            session=session,
            number=1,
            stored_bytes=len(segment_contents[0]),
            content=segment_contents[0],
        )
        persisted_session = WriteSession.model_validate_json(session.model_dump_json())
        if continuation_client.descriptor() != descriptor:
            raise AssertionError("continuation client names a different adapter contract")
        written_segments = (
            first_segment,
            *(
                continuation_client.write_segment(
                    session=persisted_session,
                    number=index,
                    stored_bytes=len(content),
                    content=content,
                )
                for index, content in enumerate(segment_contents[1:], start=2)
            ),
        )
        listed_segment_set = continuation_client.list_segments(persisted_session)
        if listed_segment_set.segments != written_segments:
            raise AssertionError("write listing differs from written segment receipts")
        checks.append("write-continuation-replay")
        checks.append("write-reconciliation")

        total_bytes = sum(len(content) for content in segment_contents)
        completion_request = WriteCompleteRequest(
            session=session,
            segments=listed_segment_set.segments,
            expected_bytes=total_bytes,
            required_identity_assertions=write_request.required_identity_assertions,
            expected_placement=write_request.placement,
        )
        completed = continuation_client.complete_write(completion_request)
        recovered_completion = continuation_client.complete_write(completion_request)
        if recovered_completion != completed:
            raise AssertionError("lost completion response did not reconcile exactly")
        headed_completion = continuation_client.find_completed_write(
            CompletedWriteLookupRequest(
                object_path=write_path,
                required_identity_assertions=write_request.required_identity_assertions,
                expected_placement=write_request.placement,
            )
        )
        if headed_completion != completed:
            raise AssertionError("completed write lookup differs from its receipt")
        checks.append("write-completion-recovery")

        write_content = b"".join(segment_contents)
        stored_write = b"".join(
            continuation_client.iter_object(
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
        prepared = continuation_client.prepare_read(preparation)
        status = continuation_client.read_status(preparation)
        if prepared.readiness.state not in {"ready", "requested"} or status.readiness.state not in {
            "ready",
            "requested",
            "expired",
        }:
            raise AssertionError("adapter returned an invalid read-preparation state")
        continuation_client.cleanup_read(preparation)
        checks.append("read-preparation")

        aborted = client.begin_write(
            write_request.model_copy(update={"object_path": f"{normalized_prefix}/aborted.bin"})
        )
        persisted_aborted = WriteSession.model_validate_json(aborted.model_dump_json())
        continuation_client.abort_write(persisted_aborted)
        continuation_client.abort_write(persisted_aborted)
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
    continuation_client = StorageAdapterClient.from_token_file(
        args.base_url,
        token_file=args.token_file,
        allow_insecure_http=args.allow_insecure_http,
    )
    try:
        result = run_storage_adapter_conformance(
            client,
            continuation_client=continuation_client,
            object_prefix=run_prefix,
        )
    finally:
        continuation_client.close()
        client.close()
    print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
    return 0


__all__ = [
    "STORAGE_ADAPTER_CONFORMANCE_RESULT",
    "StorageAdapterConformanceResult",
    "main",
    "run_storage_adapter_conformance",
]
