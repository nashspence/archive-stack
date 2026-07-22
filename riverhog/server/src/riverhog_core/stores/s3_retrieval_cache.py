from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from typing import Any, cast

from time_formats import utc_timestamp_now

from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_support import create_retrieval_cache_s3_client

_MIN_MULTIPART_BYTES = 5 * 1024 * 1024


class S3RetrievalCache:
    def __init__(self, config: RuntimeConfig) -> None:
        if config.retrieval_cache is None:
            raise ValueError("retrieval cache is not configured")
        self._config = config
        self._cache = config.retrieval_cache
        self._client = create_retrieval_cache_s3_client(config, self._cache)

    def _object_path(self, source_store: str, collection_id: str, object_id: str) -> str:
        identity = f"{source_store}\0{collection_id}\0{object_id}".encode()
        digest = hashlib.sha256(identity).hexdigest()
        parts = [part for part in (self._cache.prefix, "objects", digest[:2], digest) if part]
        return "/".join(parts)

    def put(
        self,
        *,
        source_store: str,
        collection_id: str,
        object_id: str,
        content: Iterable[bytes],
        content_length: int,
    ) -> RetrievalCacheReceipt:
        if content_length < 0:
            raise ValueError("retrieval cache content length must be non-negative")
        object_path = self._object_path(source_store, collection_id, object_id)
        digest = hashlib.sha256()
        written = 0
        version_id: str | None = None
        metadata = {
            "riverhog-cache-format": "encrypted-archive-object-v1",
            "riverhog-source-store": source_store,
            "riverhog-source-identity": hashlib.sha256(
                f"{collection_id}\0{object_id}".encode()
            ).hexdigest(),
        }

        if content_length < _MIN_MULTIPART_BYTES:
            small_body = bytearray()
            for chunk in content:
                small_body.extend(chunk)
                digest.update(chunk)
                written += len(chunk)
            if written != content_length:
                raise ValueError("retrieval cache stream length changed")
            response = cast(
                dict[str, Any],
                self._client.put_object(
                    Bucket=self._cache.bucket,
                    Key=object_path,
                    Body=bytes(small_body),
                    ContentLength=written,
                    Metadata=metadata,
                ),
            )
            version_id = str(response["VersionId"]) if response.get("VersionId") else None
        else:
            created = cast(
                dict[str, Any],
                self._client.create_multipart_upload(
                    Bucket=self._cache.bucket,
                    Key=object_path,
                    Metadata=metadata,
                ),
            )
            upload_id = str(created["UploadId"])
            parts: list[dict[str, object]] = []
            buffer = bytearray()
            try:
                for chunk in content:
                    digest.update(chunk)
                    written += len(chunk)
                    buffer.extend(chunk)
                    while len(buffer) >= self._config.archive_multipart_part_bytes:
                        part_body = bytes(buffer[: self._config.archive_multipart_part_bytes])
                        del buffer[: self._config.archive_multipart_part_bytes]
                        parts.append(
                            self._upload_part(
                                object_path=object_path,
                                upload_id=upload_id,
                                part_number=len(parts) + 1,
                                body=part_body,
                            )
                        )
                if buffer:
                    parts.append(
                        self._upload_part(
                            object_path=object_path,
                            upload_id=upload_id,
                            part_number=len(parts) + 1,
                            body=bytes(buffer),
                        )
                    )
                if written != content_length:
                    raise ValueError("retrieval cache stream length changed")
                completed = cast(
                    dict[str, Any],
                    self._client.complete_multipart_upload(
                        Bucket=self._cache.bucket,
                        Key=object_path,
                        UploadId=upload_id,
                        MultipartUpload={"Parts": parts},
                    ),
                )
                version_id = str(completed["VersionId"]) if completed.get("VersionId") else None
            except Exception:
                self._client.abort_multipart_upload(
                    Bucket=self._cache.bucket,
                    Key=object_path,
                    UploadId=upload_id,
                )
                raise

        current = utc_timestamp_now()
        head_args: dict[str, object] = {"Bucket": self._cache.bucket, "Key": object_path}
        if version_id is not None:
            head_args["VersionId"] = version_id
        head = cast(dict[str, Any], self._client.head_object(**head_args))
        if int(head.get("ContentLength", -1)) != content_length:
            raise RuntimeError("retrieval cache verification length mismatch")
        return RetrievalCacheReceipt(
            object_path=object_path,
            version_id=version_id,
            stored_bytes=written,
            stored_sha256=digest.hexdigest(),
            cached_at=current,
            verified_at=current,
        )

    def _upload_part(
        self,
        *,
        object_path: str,
        upload_id: str,
        part_number: int,
        body: bytes,
    ) -> dict[str, object]:
        response = self._client.upload_part(
            Bucket=self._cache.bucket,
            Key=object_path,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=body,
            ContentLength=len(body),
        )
        return {"PartNumber": part_number, "ETag": str(response["ETag"])}

    def iter_object(
        self,
        *,
        object_path: str,
        version_id: str | None,
        expected_bytes: int,
        expected_sha256: str,
    ) -> Iterator[bytes]:
        request: dict[str, object] = {"Bucket": self._cache.bucket, "Key": object_path}
        if version_id is not None:
            request["VersionId"] = version_id
        response = self._client.get_object(**request)
        body = response["Body"]
        digest = hashlib.sha256()
        size = 0
        try:
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                yield chunk
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        if size != expected_bytes or digest.hexdigest() != expected_sha256:
            raise RuntimeError("retrieval cache object does not match its verified record")

    def delete(self, *, object_path: str, version_id: str | None) -> None:
        request: dict[str, object] = {"Bucket": self._cache.bucket, "Key": object_path}
        if version_id is not None:
            request["VersionId"] = version_id
        self._client.delete_object(**request)
