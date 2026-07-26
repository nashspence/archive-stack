#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import Lock
from typing import Any

from riverhog_core.archive_objects import CollectionArchive, CollectionArchiveDataObject
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
    ArchiveObjectIdentity,
    CollectionArchiveIdentity,
)
from riverhog_core.runtime_config import load_runtime_config
from riverhog_core.stores.s3_archive_store import ArchiveMultipartTiming, S3ArchiveStore
from riverhog_core.stores.s3_support import create_archive_s3_client

MIB = 1024 * 1024
READ_BYTES = 8 * MIB
MINIMUM_PROBE_BYTES = 5 * MIB
COLLECTION_ID = 1


@dataclass(frozen=True)
class ThroughputResult:
    bytes: int
    checkpoint_seconds: float
    concurrency: int
    data_object_seconds: float
    encryption_and_buffering_seconds: float
    mib_per_second: float
    multipart_parts: int
    part_mib: float
    provider_request_seconds_total: float
    source_read_seconds: float
    source_scan_mib_per_second: float
    source_scan_seconds: float
    store: str
    verification_seconds: float


class _TimedFileSource:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.bytes_read = 0
        self.read_seconds = 0.0
        self._lock = Lock()

    def chunks(self) -> Iterator[bytes]:
        yield from self.chunks_range(0, self.path.stat().st_size)

    def chunks_range(self, offset: int, size: int) -> Iterator[bytes]:
        with self.path.open("rb") as source:
            source.seek(offset)
            remaining = size
            while remaining:
                started = time.perf_counter()
                chunk = source.read(min(READ_BYTES, remaining))
                elapsed = time.perf_counter() - started
                if not chunk:
                    raise ValueError("archive throughput source ended before requested range")
                with self._lock:
                    self.bytes_read += len(chunk)
                    self.read_seconds += elapsed
                remaining -= len(chunk)
                yield chunk


class _MemoryMultipartTracker:
    def __init__(self) -> None:
        self.state: ArchiveMultipartUploadState | None = None
        self.parts: dict[int, ArchiveMultipartUploadedPart] = {}

    def load_multipart_upload(self, **_kwargs: object) -> ArchiveMultipartUploadState | None:
        return replace(self.state, parts=tuple(self.parts.values())) if self.state else None

    def save_multipart_upload(
        self,
        *,
        collection_id: int,
        state: ArchiveMultipartUploadState,
    ) -> None:
        _ = collection_id
        self.state = state
        self.parts.clear()

    def record_multipart_upload_progress(
        self,
        *,
        collection_id: int,
        state: ArchiveMultipartUploadState,
        part: ArchiveMultipartUploadedPart,
        uploaded_bytes: int,
        uploaded_parts: int,
        total_parts: int,
    ) -> None:
        _ = collection_id, state, uploaded_bytes, uploaded_parts, total_parts
        self.parts[part.part_number] = part

    def clear_multipart_upload(
        self,
        *,
        collection_id: int,
        object_id: str,
        upload_id: str,
    ) -> None:
        _ = collection_id, object_id, upload_id
        self.state = None
        self.parts.clear()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _scan_source(path: Path) -> tuple[str, int, float]:
    digest = hashlib.sha256()
    total = 0
    started = time.perf_counter()
    with path.open("rb") as source:
        while chunk := source.read(READ_BYTES):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total, time.perf_counter() - started


def _archive(source: _TimedFileSource, *, bytes_total: int, sha256: str) -> CollectionArchive:
    data = CollectionArchiveDataObject(
        object_id="data-000000",
        kind="file",
        plaintext_bytes=bytes_total,
        sha256=sha256,
        placements=(),
        _chunks=source.chunks,
        _chunks_range=source.chunks_range,
    )
    manifest = b"format: riverhog-archive-throughput-probe-v1\n"
    proof = b"throughput probe\n"
    return CollectionArchive(
        collection_id=COLLECTION_ID,
        files=(),
        data_objects=(data,),
        manifest_bytes=manifest,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        proof_bytes=proof,
        proof_sha256=hashlib.sha256(proof).hexdigest(),
    )


def _cleanup_probe(client: Any, *, bucket: str, prefix: str) -> None:
    request: dict[str, Any] = {"Bucket": bucket, "Prefix": f"{prefix}/"}
    while True:
        response = client.list_multipart_uploads(**request)
        for upload in response.get("Uploads") or ():
            client.abort_multipart_upload(
                Bucket=bucket,
                Key=upload["Key"],
                UploadId=upload["UploadId"],
            )
        if not response.get("IsTruncated"):
            break
        request["KeyMarker"] = response["NextKeyMarker"]
        request["UploadIdMarker"] = response["NextUploadIdMarker"]

    listing = client.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/")
    for item in listing.get("Contents") or ():
        client.delete_object(Bucket=bucket, Key=item["Key"])
    remaining = client.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/")
    if remaining.get("Contents"):
        raise RuntimeError("archive throughput probe cleanup could not be verified")


def _run(
    source_path: Path,
    *,
    store_name: str,
    concurrency: int,
    part_mib: int,
) -> ThroughputResult:
    source_path = source_path.resolve(strict=True)
    sha256, bytes_total, scan_seconds = _scan_source(source_path)
    if bytes_total < MINIMUM_PROBE_BYTES:
        raise ValueError("archive throughput source must be at least 5 MiB")

    base_config = load_runtime_config()
    if store_name not in base_config.archive_stores:
        raise ValueError(f"archive store is not configured: {store_name}")
    config = replace(
        base_config,
        archive_multipart_concurrency=concurrency,
        archive_multipart_part_bytes=part_mib * MIB,
    )
    store_config = config.archive_store(store_name)
    timings: list[ArchiveMultipartTiming] = []
    store = S3ArchiveStore(
        config,
        store_config,
        multipart_timing_observer=timings.append,
    )
    if bytes_total > store.max_plaintext_object_bytes():
        raise ValueError("archive throughput source exceeds one stored-object limit")

    source = _TimedFileSource(source_path)
    archive = _archive(source, bytes_total=bytes_total, sha256=sha256)
    prefix = store.new_collection_archive_storage_prefix()
    cleanup_client = create_archive_s3_client(config, store_config)
    try:
        receipt = store.upload_collection_archive(
            collection_id=COLLECTION_ID,
            archive=archive,
            archive_storage_prefix=prefix,
            multipart_tracker=_MemoryMultipartTracker(),
        )
        verification_started = time.perf_counter()
        store.verify_collection_archive(
            collection_id=COLLECTION_ID,
            archive=CollectionArchiveIdentity(
                objects=tuple(
                    ArchiveObjectIdentity(
                        object_id=item.object_id,
                        kind=item.kind,
                        object_path=item.object_path,
                        plaintext_bytes=item.plaintext_bytes,
                        stored_bytes=item.stored_bytes,
                        sha256=item.sha256,
                    )
                    for item in receipt.objects
                )
            ),
        )
        verification_seconds = time.perf_counter() - verification_started
        if source.bytes_read != bytes_total:
            raise RuntimeError("archive throughput source was not read exactly once")
        if len(timings) != 1:
            raise RuntimeError("archive throughput probe did not observe one multipart object")
        timing = timings[0]
        return ThroughputResult(
            bytes=bytes_total,
            checkpoint_seconds=round(timing.checkpoint_seconds, 6),
            concurrency=timing.concurrency,
            data_object_seconds=round(timing.elapsed_seconds, 6),
            encryption_and_buffering_seconds=round(
                max(0.0, timing.preparation_seconds - source.read_seconds),
                6,
            ),
            mib_per_second=round(bytes_total / MIB / timing.elapsed_seconds, 2),
            multipart_parts=timing.parts,
            part_mib=round(config.archive_multipart_part_bytes / MIB, 2),
            provider_request_seconds_total=round(timing.upload_request_seconds, 6),
            source_read_seconds=round(source.read_seconds, 6),
            source_scan_mib_per_second=round(bytes_total / MIB / scan_seconds, 2),
            source_scan_seconds=round(scan_seconds, 6),
            store=store_name,
            verification_seconds=round(verification_seconds, 6),
        )
    finally:
        _cleanup_probe(cleanup_client, bucket=store_config.bucket, prefix=prefix)
        close = getattr(cleanup_client, "close", None)
        if callable(close):
            close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure one independently encrypted archive object through Riverhog's production "
            "S3-compatible upload path, then verify and delete the probe. Provider request time "
            "is client-observed HTTP round-trip time and includes provider processing."
        )
    )
    parser.add_argument("source", type=Path, help="existing source file of at least 5 MiB")
    parser.add_argument(
        "--store",
        help="configured archive store name (default: RIVERHOG_ARCHIVE_WRITE_STORE)",
    )
    parser.add_argument(
        "--concurrency",
        type=_positive_int,
        help="multipart requests in flight (default: configured Riverhog value)",
    )
    parser.add_argument(
        "--part-mib",
        type=_positive_int,
        help="multipart target size (default: configured Riverhog value)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = load_runtime_config()
    store_name = args.store or config.archive_write_store
    result = _run(
        args.source,
        store_name=store_name,
        concurrency=args.concurrency or config.archive_multipart_concurrency,
        part_mib=args.part_mib or max(1, config.archive_multipart_part_bytes // MIB),
    )
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
