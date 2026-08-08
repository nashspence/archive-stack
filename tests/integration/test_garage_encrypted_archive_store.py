from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import replace
from typing import Any

import pytest
from riverhog_age import encrypt_age_scrypt
from riverhog_core.ports.archive_store import ArchiveObjectIdentity
from riverhog_core.runtime_config import load_runtime_config
from riverhog_core.stores.s3_archive_ingress_store import S3ArchiveMultipartObjectStore
from riverhog_core.stores.s3_archive_manifest_store import S3ImmutableArchiveObjectStore
from riverhog_core.stores.s3_archive_range_store import S3ArchiveObjectRangeStore
from riverhog_core.stores.s3_archive_store import (
    AGE_SCRYPT_ENCRYPTION,
    COLLECTION_BYTES_METADATA,
    COLLECTION_SHA256_METADATA,
    ENCRYPTION_METADATA,
    PLAINTEXT_BYTES_METADATA,
    PLAINTEXT_SHA256_METADATA,
    STORED_SHA256_METADATA,
    S3ArchiveStore,
)
from riverhog_core.stores.s3_client import create_archive_s3_client

pytestmark = pytest.mark.integration


def _delete_prefix(client: Any, *, bucket: str, prefix: str) -> None:
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects = [{"Key": entry["Key"]} for entry in page.get("Contents", [])]
        if objects:
            client.delete_objects(Bucket=bucket, Delete={"Objects": objects})


def _abort_multipart_uploads(client: Any, *, bucket: str, prefix: str) -> None:
    response = client.list_multipart_uploads(Bucket=bucket, Prefix=prefix)
    for upload in response.get("Uploads", []):
        client.abort_multipart_upload(
            Bucket=bucket,
            Key=upload["Key"],
            UploadId=upload["UploadId"],
        )


def test_canonical_archive_adapters_against_garage() -> None:
    if os.environ.get("RIVERHOG_GARAGE_ARCHIVE_INGRESS_TEST") != "1":
        pytest.skip("set RIVERHOG_GARAGE_ARCHIVE_INGRESS_TEST=1 to run against Garage")

    prefix = f"garage-archive-ingress-test/{uuid.uuid4().hex}"
    archive_prefix = f"{prefix}/archives/opaque"
    passphrase = os.environ.get(
        "RIVERHOG_ARCHIVE_PASSPHRASE",
        "garage archive ingress integration passphrase",
    )
    base = load_runtime_config()
    store_config = replace(
        base.archive_store(base.archive_write_store),
        prefix=prefix,
        storage_class="STANDARD",
    )
    config = replace(
        base,
        archive_stores={store_config.name: store_config},
        archive_passphrase=passphrase,
        archive_scrypt_work_factor=12,
    )
    client = create_archive_s3_client(config, store_config)
    client.head_bucket(Bucket=store_config.bucket)
    multipart = S3ArchiveMultipartObjectStore(config, store_config)
    immutable = S3ImmutableArchiveObjectStore(config, store_config)
    ranges = S3ArchiveObjectRangeStore(config, store_config)
    archive = S3ArchiveStore(config, store_config)

    plaintext = b"canonical direct-final Garage archive volume"
    ciphertext = encrypt_age_scrypt(
        plaintext,
        passphrase,
        log_n=config.archive_scrypt_work_factor,
    )
    plaintext_sha256 = hashlib.sha256(plaintext).hexdigest()
    stored_sha256 = hashlib.sha256(ciphertext).hexdigest()
    volume_path = f"{archive_prefix}/volumes/segment-000000000000.bin.age"
    metadata = {
        "riverhog-backend": store_config.backend,
        "riverhog-storage-class": "STANDARD",
        "riverhog-object-kind": "collection-segment",
        "riverhog-object-id": "segment-000000000000",
        ENCRYPTION_METADATA: AGE_SCRYPT_ENCRYPTION,
        PLAINTEXT_BYTES_METADATA: str(len(plaintext)),
        PLAINTEXT_SHA256_METADATA: plaintext_sha256,
        COLLECTION_BYTES_METADATA: str(len(plaintext)),
        COLLECTION_SHA256_METADATA: plaintext_sha256,
        STORED_SHA256_METADATA: stored_sha256,
    }

    try:
        upload = multipart.create_multipart_upload(
            object_path=volume_path,
            content_type="application/vnd.riverhog.raw-volume+age",
            metadata=metadata,
        )
        part = multipart.upload_part(upload=upload, number=1, content=ciphertext)
        completed = multipart.complete_multipart_upload(
            upload=upload,
            parts=(part,),
            expected_bytes=len(ciphertext),
            expected_metadata=metadata,
        )
        assert completed.object_path == volume_path

        manifest_content = encrypt_age_scrypt(
            b'{"schema":"collection-archive-manifest/v1"}',
            passphrase,
            log_n=config.archive_scrypt_work_factor,
        )
        root = immutable.put_immutable_object(
            object_path=f"{archive_prefix}/manifest.json.age",
            content=manifest_content,
            content_type="application/vnd.riverhog.collection-manifest+age",
            identity_metadata={"riverhog-plaintext-sha256": "a" * 64},
        )
        assert (
            immutable.put_immutable_object(
                object_path=root.object_path,
                content=b"randomized retry ciphertext",
                content_type="application/vnd.riverhog.collection-manifest+age",
                identity_metadata={"riverhog-plaintext-sha256": "a" * 64},
            )
            == root
        )

        assert (
            b"".join(
                ranges.iter_object_range(
                    object_path=volume_path,
                    version_id=completed.version_id,
                    offset=0,
                    size=min(64, len(ciphertext)),
                )
            )
            == ciphertext[:64]
        )

        identity = ArchiveObjectIdentity(
            object_id="segment-000000000000",
            kind="segment",
            object_path=volume_path,
            plaintext_bytes=len(plaintext),
            stored_bytes=len(ciphertext),
            sha256=plaintext_sha256,
            stored_sha256=stored_sha256,
        )
        assert b"".join(archive.iter_archive_object(collection_id=1, object=identity)) == plaintext
    finally:
        _abort_multipart_uploads(
            client,
            bucket=store_config.bucket,
            prefix=f"{prefix}/",
        )
        _delete_prefix(client, bucket=store_config.bucket, prefix=f"{prefix}/")
