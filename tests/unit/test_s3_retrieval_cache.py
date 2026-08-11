from __future__ import annotations

import hashlib
import threading
from typing import Any

from riverhog_core.runtime_config import RetrievalCacheConfig, RuntimeConfig
from riverhog_core.stores.s3_retrieval_cache import S3RetrievalCache
from riverhog_core.throughput import ArchiveThroughputTuning, ArchiveTransferResources

_PART_BYTES = 5 * 1024 * 1024


class _FakeS3Client:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._two_parts_active = threading.Event()
        self._parts: dict[int, bytes] = {}
        self.object = b""
        self.active = 0
        self.maximum_active = 0

    def create_multipart_upload(self, **kwargs: object) -> dict[str, str]:
        assert kwargs["Metadata"]
        return {"UploadId": "upload-1"}

    def upload_part(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        PartNumber: int,
        Body: bytes,
        ContentLength: int,
    ) -> dict[str, str]:
        del Bucket, Key
        assert UploadId == "upload-1"
        assert ContentLength == len(Body)
        with self._lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.active >= 2:
                self._two_parts_active.set()
        if PartNumber <= 2:
            assert self._two_parts_active.wait(timeout=5)
        self._parts[PartNumber] = Body
        with self._lock:
            self.active -= 1
        return {"ETag": f"etag-{PartNumber}"}

    def complete_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        MultipartUpload: dict[str, object],
    ) -> dict[str, str]:
        del Bucket, Key
        assert UploadId == "upload-1"
        parts = MultipartUpload["Parts"]
        assert isinstance(parts, list)
        numbers = [int(part["PartNumber"]) for part in parts]
        assert numbers == sorted(numbers)
        self.object = b"".join(self._parts[number] for number in numbers)
        return {"VersionId": "version-1"}

    def head_object(self, **kwargs: object) -> dict[str, int]:
        assert kwargs["VersionId"] == "version-1"
        return {"ContentLength": len(self.object)}

    def abort_multipart_upload(self, **kwargs: object) -> None:
        raise AssertionError(f"unexpected abort: {kwargs}")


def test_multipart_cache_hydration_overlaps_bounded_part_uploads(
    monkeypatch: Any,
) -> None:
    content = b"a" * _PART_BYTES + b"b" * _PART_BYTES + b"tail"
    fake = _FakeS3Client()
    config = RuntimeConfig(
        archive_multipart_part_bytes=_PART_BYTES,
        retrieval_cache=RetrievalCacheConfig(
            endpoint_url="https://cache.invalid",
            region="us-east-1",
            bucket="cache",
            access_key_id="test",
            secret_access_key="test",
        ),
    )
    tuning = ArchiveThroughputTuning(
        s3_part_concurrency=2,
        s3_upload_request_concurrency=2,
        upload_max_inflight_bytes=3 * _PART_BYTES,
    )
    monkeypatch.setattr(
        "riverhog_core.stores.s3_retrieval_cache.create_retrieval_cache_s3_client",
        lambda *_args: fake,
    )
    cache = S3RetrievalCache(
        config,
        throughput_tuning=tuning,
        transfer_resources=ArchiveTransferResources.from_tuning(tuning),
    )

    receipt = cache.put(
        source_store="archive",
        collection_id=1,
        object_id="raw-000001",
        content=(
            content[offset : offset + 1024 * 1024] for offset in range(0, len(content), 1024 * 1024)
        ),
        content_length=len(content),
    )

    assert fake.maximum_active == 2
    assert fake.object == content
    assert receipt.version_id == "version-1"
    assert receipt.stored_bytes == len(content)
    assert receipt.stored_sha256 == hashlib.sha256(content).hexdigest()
