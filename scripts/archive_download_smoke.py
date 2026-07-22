#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from riverhog_core.archive_objects import CollectionArchive, CollectionArchiveDataObject
from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    CollectionArchiveIdentity,
)
from riverhog_core.runtime_config import load_runtime_config
from riverhog_core.stores.s3_archive_store import S3ArchiveStore
from riverhog_core.stores.s3_support import create_archive_s3_client

COLLECTION_ID = "archive-download-smoke/20000101T000000Z"


@dataclass(frozen=True)
class SmokeResult:
    bytes: int
    cleanup_verified: bool
    elapsed_seconds: float
    objects: int
    store: str
    transport: str
    verified: bool


def _archive(content: bytes) -> CollectionArchive:
    data = CollectionArchiveDataObject(
        object_id="data-000000",
        kind="file",
        plaintext_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        placements=(),
        _chunks=lambda: iter((content,)),
    )
    manifest = b"format: riverhog-archive-download-smoke-v1\n"
    proof = b"archive download smoke proof\n"
    return CollectionArchive(
        collection_id=COLLECTION_ID,
        files=(),
        data_objects=(data,),
        manifest_bytes=manifest,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        proof_bytes=proof,
        proof_sha256=hashlib.sha256(proof).hexdigest(),
    )


def _cleanup(client: Any, *, bucket: str, prefix: str) -> None:
    object_prefix = f"{prefix}/"
    request: dict[str, Any] = {"Bucket": bucket, "Prefix": object_prefix}
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

    listing_request: dict[str, Any] = {"Bucket": bucket, "Prefix": object_prefix}
    while True:
        listing = client.list_objects_v2(**listing_request)
        for item in listing.get("Contents") or ():
            client.delete_object(Bucket=bucket, Key=item["Key"])
        continuation = listing.get("NextContinuationToken")
        if not continuation:
            break
        listing_request["ContinuationToken"] = continuation

    remaining = client.list_objects_v2(Bucket=bucket, Prefix=object_prefix)
    if remaining.get("Contents"):
        raise RuntimeError("archive download smoke cleanup could not be verified")


def _select_store(config: Any, requested: str | None) -> str:
    if requested:
        return requested
    candidates = [
        name
        for name, store in config.archive_stores.items()
        if store.cloudfront_base_url is not None
    ]
    if len(candidates) != 1:
        raise ValueError(
            "select one CloudFront-enabled archive store with --store"
            if candidates
            else "no CloudFront-enabled archive store is configured"
        )
    return candidates[0]


def _run(store_name: str | None) -> SmokeResult:
    base_config = load_runtime_config()
    selected_store = _select_store(base_config, store_name)
    if selected_store not in base_config.archive_stores:
        raise ValueError(f"archive store is not configured: {selected_store}")
    store_config = replace(base_config.archive_store(selected_store), storage_class="STANDARD")
    if store_config.backend.casefold() != "aws" or store_config.cloudfront_base_url is None:
        raise ValueError("archive download smoke requires a CloudFront-enabled AWS store")
    config = replace(
        base_config,
        archive_stores={**base_config.archive_stores, selected_store: store_config},
    )
    store = S3ArchiveStore(config, store_config)
    cleanup_client = create_archive_s3_client(config, store_config)
    prefix = store.new_collection_archive_storage_prefix()
    content = b"riverhog-cloudfront-smoke\0" + secrets.token_bytes(32)
    archive = _archive(content)
    started = time.perf_counter()
    try:
        receipt = store.upload_collection_archive(
            collection_id=COLLECTION_ID,
            archive=archive,
            archive_storage_prefix=prefix,
        )
        identity = CollectionArchiveIdentity(
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
        )
        store.verify_collection_archive(collection_id=COLLECTION_ID, archive=identity)
        restored = b"".join(
            store.iter_archive_object(
                collection_id=COLLECTION_ID,
                object=identity.data_objects[0],
            )
        )
        if restored != content:
            raise RuntimeError("CloudFront archive download did not reproduce the probe")
        objects = len(receipt.objects)
    finally:
        _cleanup(cleanup_client, bucket=store_config.bucket, prefix=prefix)
        close = getattr(cleanup_client, "close", None)
        if callable(close):
            close()
    return SmokeResult(
        bytes=len(content),
        cleanup_verified=True,
        elapsed_seconds=round(time.perf_counter() - started, 6),
        objects=objects,
        store=selected_store,
        transport="cloudfront",
        verified=True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Upload a tiny independently encrypted object set to Standard S3, download and "
            "decrypt its data object through configured CloudFront, then delete the probe."
        )
    )
    parser.add_argument(
        "--store",
        help="CloudFront-enabled AWS archive store (auto-selected when exactly one exists)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    print(json.dumps(asdict(_run(args.store)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
