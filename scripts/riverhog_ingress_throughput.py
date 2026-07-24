#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from riverhog_api_client import ApiClient

MIB = 1024 * 1024
DEFAULT_FILES = 8
MAX_FILES = 64
DEFAULT_MIB_PER_FILE = 100
DEFAULT_CHUNK_MIB = 50
INCOMPLETE_PLAINTEXT_BYTES = MIB


class _Api(Protocol):
    def create_or_resume_collection_upload_session(
        self,
        slug: str,
        *,
        upload_timestamp: str,
        archive_store: str | None,
    ) -> dict[str, Any]: ...

    def register_collection_upload_session_file(
        self,
        collection_id: str,
        file: dict[str, object],
    ) -> dict[str, Any]: ...

    def create_or_resume_collection_file_upload(
        self,
        collection_id: str,
        path: str,
    ) -> dict[str, Any]: ...

    def append_upload_chunk(
        self,
        upload_url: str,
        *,
        offset: int,
        checksum_algorithm: str,
        content: bytes,
    ) -> dict[str, Any]: ...

    def cancel_collection_upload_session(self, collection_id: str) -> dict[str, Any]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class _PreparedUpload:
    checksum_algorithm: str
    path: str
    upload_url: str


@dataclass(frozen=True)
class ThroughputResult:
    bytes: int
    chunk_mib: int
    cleanup_seconds: float
    files: int
    mib_per_second: float
    preparation_seconds: float
    seconds: float


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _file_count(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > MAX_FILES:
        raise argparse.ArgumentTypeError(f"must not exceed {MAX_FILES}")
    return parsed


def _upload_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _prepare_uploads(
    api: _Api,
    *,
    slug: str,
    upload_timestamp: str,
    archive_store: str | None,
    files: int,
    bytes_per_file: int,
) -> tuple[str, list[_PreparedUpload]]:
    session = api.create_or_resume_collection_upload_session(
        slug,
        upload_timestamp=upload_timestamp,
        archive_store=archive_store,
    )
    collection_id = str(session["collection_id"])
    prepared: list[_PreparedUpload] = []
    declared_plaintext_bytes = bytes_per_file + INCOMPLETE_PLAINTEXT_BYTES
    try:
        for index in range(files):
            path = f"probe-{index + 1:04d}.bin"
            api.register_collection_upload_session_file(
                collection_id,
                {
                    "path": path,
                    "bytes": declared_plaintext_bytes,
                    # The session is deliberately canceled before completion or validation.
                    "sha256": "0" * 64,
                },
            )
            upload = api.create_or_resume_collection_file_upload(collection_id, path)
            if int(upload["offset"]) != 0:
                raise RuntimeError(f"throughput probe upload is not empty: {path}")
            if int(upload["length"]) <= bytes_per_file:
                raise RuntimeError(f"throughput probe upload would complete: {path}")
            prepared.append(
                _PreparedUpload(
                    checksum_algorithm=str(upload["checksum_algorithm"]),
                    path=path,
                    upload_url=str(upload["upload_url"]),
                )
            )
    except BaseException as exc:
        canceled = api.cancel_collection_upload_session(collection_id)
        if canceled.get("state") != "canceled":
            raise RuntimeError("throughput probe preparation could not be canceled") from exc
        raise
    return collection_id, prepared


def _upload_probe(
    api_factory: Callable[[], _Api],
    upload: _PreparedUpload,
    *,
    bytes_per_file: int,
    chunk: bytes,
) -> None:
    api = api_factory()
    try:
        offset = 0
        while offset < bytes_per_file:
            content = chunk[: min(len(chunk), bytes_per_file - offset)]
            result = api.append_upload_chunk(
                upload.upload_url,
                offset=offset,
                checksum_algorithm=upload.checksum_algorithm,
                content=content,
            )
            expected_offset = offset + len(content)
            actual_offset = int(result["offset"])
            if actual_offset != expected_offset:
                raise RuntimeError(
                    f"unexpected TUS offset for {upload.path}: "
                    f"{actual_offset}; expected {expected_offset}"
                )
            offset = actual_offset
    finally:
        api.close()


def _run(
    api: _Api,
    *,
    api_factory: Callable[[], _Api],
    slug: str,
    upload_timestamp: str,
    archive_store: str | None,
    files: int,
    bytes_per_file: int,
    chunk_bytes: int,
    clock: Callable[[], float] = time.perf_counter,
) -> ThroughputResult:
    preparation_started = clock()
    collection_id, prepared = _prepare_uploads(
        api,
        slug=slug,
        upload_timestamp=upload_timestamp,
        archive_store=archive_store,
        files=files,
        bytes_per_file=bytes_per_file,
    )
    preparation_seconds = clock() - preparation_started
    chunk = bytes(chunk_bytes)
    transfer_started = clock()
    cleanup_seconds = 0.0
    try:
        with ThreadPoolExecutor(max_workers=files) as executor:
            futures = [
                executor.submit(
                    _upload_probe,
                    api_factory,
                    upload,
                    bytes_per_file=bytes_per_file,
                    chunk=chunk,
                )
                for upload in prepared
            ]
            for future in futures:
                future.result()
        seconds = clock() - transfer_started
    finally:
        cleanup_started = clock()
        canceled = api.cancel_collection_upload_session(collection_id)
        cleanup_seconds = clock() - cleanup_started
        if canceled.get("state") != "canceled":
            raise RuntimeError("throughput probe session was not canceled")

    total_bytes = files * bytes_per_file
    return ThroughputResult(
        bytes=total_bytes,
        chunk_mib=chunk_bytes // MIB,
        cleanup_seconds=round(cleanup_seconds, 3),
        files=files,
        mib_per_second=round(total_bytes / MIB / seconds, 2),
        preparation_seconds=round(preparation_seconds, 3),
        seconds=round(seconds, 3),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure Riverhog's production proxy/TUS ingress path with concurrent, incomplete "
            "uploads. The disposable session is canceled and its objects are deleted without "
            "publishing a collection. Standard RIVERHOG_BASE_URL and RIVERHOG_TOKEN settings "
            "provide credentials."
        )
    )
    parser.add_argument(
        "--files",
        type=_file_count,
        default=DEFAULT_FILES,
        help=f"concurrent probe uploads, at most {MAX_FILES} (default: {DEFAULT_FILES})",
    )
    parser.add_argument(
        "--mib-per-file",
        type=_positive_int,
        default=DEFAULT_MIB_PER_FILE,
        help=f"bytes sent per incomplete upload in MiB (default: {DEFAULT_MIB_PER_FILE})",
    )
    parser.add_argument(
        "--chunk-mib",
        type=_positive_int,
        default=DEFAULT_CHUNK_MIB,
        help=f"TUS PATCH size in MiB (default: {DEFAULT_CHUNK_MIB})",
    )
    parser.add_argument(
        "--archive-store",
        help="archive store recorded on the disposable session",
    )
    parser.add_argument(
        "--slug",
        default="ingress-throughput-probe",
        help="disposable collection slug (default: ingress-throughput-probe)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    api = ApiClient()
    try:
        result = _run(
            api,
            api_factory=ApiClient,
            slug=args.slug,
            upload_timestamp=_upload_timestamp(),
            archive_store=args.archive_store,
            files=args.files,
            bytes_per_file=args.mib_per_file * MIB,
            chunk_bytes=args.chunk_mib * MIB,
        )
    finally:
        api.close()
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
