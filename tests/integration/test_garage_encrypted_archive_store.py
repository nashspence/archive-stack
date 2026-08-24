from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from riverhog_age import encrypt_age_scrypt
from riverhog_core.archive_formats import (
    RAW_VOLUME_STORAGE_FORMAT,
    ROOT_MANIFEST_STORAGE_FORMAT,
)
from riverhog_core.ports.archive_objects import MultipartPartReceipt
from riverhog_core.ports.archive_store import ArchiveObjectIdentity
from riverhog_core.runtime_config import StorageAdapterRegistration, load_runtime_config
from riverhog_core.stores.mirrored_archive_multipart_object_store import (
    MirroredArchiveMultipartObjectStore,
)
from riverhog_core.stores.storage_adapter_archive_objects import (
    StorageAdapterArchiveMultipartObjectStore,
    StorageAdapterArchiveObjectRangeStore,
    StorageAdapterImmutableArchiveObjectStore,
)
from riverhog_core.stores.storage_adapter_archive_store import StorageAdapterArchiveStore
from riverhog_core.stores.storage_adapter_retrieval_cache import StorageAdapterRetrievalCache
from riverhog_core.throughput import ArchiveThroughputTuning, ArchiveTransferResources
from riverhog_storage_adapter_protocol import (
    AbortIncompleteUploadsRequest,
    DeletePrefixRequest,
)
from riverhog_storage_adapter_support import StorageAdapterClient
from time_formats import format_utc_timestamp

pytestmark = pytest.mark.integration


def _client(registration: StorageAdapterRegistration) -> StorageAdapterClient:
    return StorageAdapterClient.from_token_file(
        registration.base_url,
        token_file=registration.token_file,
        allow_insecure_http=registration.allow_insecure_http,
        timeout=registration.timeout_seconds,
        maximum_connections=registration.maximum_connections,
    )


def test_canonical_archive_capabilities_against_garage_adapter() -> None:
    if os.environ.get("RIVERHOG_GARAGE_ARCHIVE_INGRESS_TEST") != "1":
        pytest.skip("set RIVERHOG_GARAGE_ARCHIVE_INGRESS_TEST=1 to run against Garage")

    prefix = f"garage-archive-ingress-test/{uuid.uuid4().hex}"
    archive_prefix = f"archives/{prefix}/opaque"
    passphrase = (
        os.environ.get("RIVERHOG_GARAGE_ARCHIVE_PASSPHRASE", "").strip()
        or "garage archive ingress integration passphrase"
    )
    base = load_runtime_config()
    config = replace(
        base,
        archive_passphrases={"garage-test-key-v1": passphrase},
        archive_active_passphrase_id="garage-test-key-v1",
        archive_scrypt_work_factor=12,
    )
    cache_registration = config.retrieval_cache
    assert cache_registration is not None
    archive_client = _client(config.archive_store(config.archive_write_store))
    cache_client = _client(cache_registration)
    archive_client.check_readiness()
    cache_client.check_readiness()
    multipart = StorageAdapterArchiveMultipartObjectStore(archive_client)
    immutable = StorageAdapterImmutableArchiveObjectStore(archive_client)
    ranges = StorageAdapterArchiveObjectRangeStore(archive_client)
    archive = StorageAdapterArchiveStore(
        config,
        name=config.archive_write_store,
        adapter=archive_client,
    )
    throughput_tuning = ArchiveThroughputTuning.from_env(os.environ)
    cache = StorageAdapterRetrievalCache(
        cache_client,
        multipart_part_bytes=config.archive_multipart_part_bytes,
        throughput_tuning=throughput_tuning,
        transfer_resources=ArchiveTransferResources.from_tuning(throughput_tuning),
    )

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
        "riverhog-format": RAW_VOLUME_STORAGE_FORMAT,
        "riverhog-source-path-sha256": hashlib.sha256(b"source.bin").hexdigest(),
        "riverhog-file-offset": "0",
        "riverhog-plaintext-bytes": str(len(plaintext)),
        "riverhog-file-sha256": plaintext_sha256,
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

        mirrored_path = f"{archive_prefix}/volumes/segment-000000000001.bin.age"
        mirrored = MirroredArchiveMultipartObjectStore(
            archive=multipart,
            cache=cache,
            source_store=config.archive_write_store,
            collection_id=1,
            object_id="segment-000000000001",
        )
        mirrored_upload = mirrored.create_multipart_upload(
            object_path=mirrored_path,
            content_type="application/vnd.riverhog.raw-volume+age",
            metadata=metadata,
        )
        mirrored_part = mirrored.upload_part(
            upload=mirrored_upload,
            number=1,
            content=ciphertext,
        )
        mirrored_completed = mirrored.complete_multipart_upload(
            upload=mirrored_upload,
            parts=(
                MultipartPartReceipt(
                    number=mirrored_part.number,
                    etag=mirrored_part.etag,
                    bytes=mirrored_part.bytes,
                    sha256=stored_sha256,
                ),
            ),
            expected_bytes=len(ciphertext),
            expected_metadata=metadata,
        )
        cache_receipt = mirrored_completed.retrieval_cache
        assert cache_receipt is not None
        assert cache_receipt.stored_sha256 == stored_sha256
        assert (
            b"".join(
                cache.iter_object(
                    object_path=cache_receipt.object_path,
                    version_id=cache_receipt.version_id,
                    expected_bytes=cache_receipt.stored_bytes,
                    expected_sha256=cache_receipt.stored_sha256,
                )
            )
            == ciphertext
        )
        assert (
            b"".join(
                ranges.iter_object_range(
                    object_path=mirrored_path,
                    version_id=mirrored_completed.version_id,
                    expected_bytes=len(ciphertext),
                    offset=0,
                    size=len(ciphertext),
                )
            )
            == ciphertext
        )

        manifest_plaintext = b'{"schema":"collection-archive-manifest/v1"}'
        archive_root_sha256 = hashlib.sha256(manifest_plaintext).hexdigest()
        manifest_content = encrypt_age_scrypt(
            manifest_plaintext,
            passphrase,
            log_n=config.archive_scrypt_work_factor,
        )
        root = immutable.put_immutable_object(
            object_path=f"{archive_prefix}/manifest.json.age",
            content=manifest_content,
            content_type="application/vnd.riverhog.collection-manifest+age",
            identity_metadata={
                "riverhog-format": ROOT_MANIFEST_STORAGE_FORMAT,
                "riverhog-plaintext-bytes": str(len(manifest_plaintext)),
                "riverhog-plaintext-sha256": archive_root_sha256,
            },
            placement="immediate",
        )
        assert (
            immutable.put_immutable_object(
                object_path=root.object_path,
                content=b"randomized retry ciphertext",
                content_type="application/vnd.riverhog.collection-manifest+age",
                identity_metadata={
                    "riverhog-format": ROOT_MANIFEST_STORAGE_FORMAT,
                    "riverhog-plaintext-bytes": str(len(manifest_plaintext)),
                    "riverhog-plaintext-sha256": archive_root_sha256,
                },
                placement="immediate",
            )
            == root
        )

        identity = ArchiveObjectIdentity(
            object_id="segment-000000000000",
            kind="segment",
            object_path=volume_path,
            plaintext_bytes=len(plaintext),
            stored_bytes=len(ciphertext),
            sha256=None,
            stored_sha256=stored_sha256,
            version_id=completed.version_id,
        )
        assert (
            b"".join(
                archive.iter_archive_object(
                    collection_id=1,
                    object=identity,
                    passphrase_id="garage-test-key-v1",
                )
            )
            == plaintext
        )
    finally:
        cutoff = format_utc_timestamp(datetime.now(UTC) + timedelta(seconds=1))
        for client in (archive_client, cache_client):
            client.abort_incomplete_uploads(
                AbortIncompleteUploadsRequest(
                    object_prefix=f"archives/{prefix}/",
                    initiated_before=cutoff,
                )
            )
            client.delete_prefix(DeletePrefixRequest(object_prefix=f"archives/{prefix}/"))
            client.close()
