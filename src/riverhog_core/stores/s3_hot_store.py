from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from typing import Any, cast

from riverhog_core.domain.errors import NotFound
from riverhog_core.ports.hot_store import HotFileStat
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores.s3_support import create_s3_client

_MIN_MULTIPART_PART_SIZE = 5 * 1024 * 1024
_MAX_MULTIPART_PART_SIZE = 5 * 1024 * 1024 * 1024
_MAX_MULTIPART_PARTS = 10_000
_FILE_BYTES_METADATA = "riverhog-file-bytes"
_FILE_SHA256_METADATA = "riverhog-file-sha256"


def _multipart_part_size(content_length: int) -> int:
    part_size = max(
        _MIN_MULTIPART_PART_SIZE,
        (content_length + _MAX_MULTIPART_PARTS - 1) // _MAX_MULTIPART_PARTS,
    )
    if part_size > _MAX_MULTIPART_PART_SIZE:
        raise ValueError("collection file stream exceeds S3 multipart object size limit")
    return part_size


class S3HotStore:
    def __init__(self, config: RuntimeConfig) -> None:
        self._bucket = config.s3_bucket
        self._client = create_s3_client(config)

    def _key(self, collection_id: str, path: str) -> str:
        return f"collections/{collection_id}/{path}"

    def put_collection_file(self, collection_id: str, path: str, content: bytes) -> None:
        sha256 = hashlib.sha256(content).hexdigest()
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._key(collection_id, path),
            Body=content,
            ContentLength=len(content),
            Metadata=_file_metadata(content_length=len(content), sha256=sha256),
        )

    def put_collection_file_stream(
        self,
        collection_id: str,
        path: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
        sha256: str | None = None,
    ) -> None:
        final_key = self._key(collection_id, path)
        metadata = _file_metadata(content_length=content_length, sha256=sha256)
        if content_length == 0:
            size = sum(len(chunk) for chunk in chunks)
            if size != 0:
                raise ValueError(f"collection file stream byte count mismatch: {path}")
            self._client.put_object(
                Bucket=self._bucket,
                Key=final_key,
                Body=b"",
                ContentLength=0,
                Metadata=metadata,
            )
            return

        upload_id: str | None = None
        part_number = 1
        uploaded_parts: list[dict[str, object]] = []
        buffer = bytearray()
        part_size = _multipart_part_size(content_length)
        size = 0

        def ensure_upload() -> str:
            nonlocal upload_id
            if upload_id is None:
                response = cast(
                    dict[str, Any],
                    self._client.create_multipart_upload(
                        Bucket=self._bucket,
                        Key=final_key,
                        Metadata=metadata,
                    ),
                )
                upload_id = str(response["UploadId"])
            return upload_id

        def upload_part(body: bytes) -> None:
            nonlocal part_number
            response = cast(
                dict[str, Any],
                self._client.upload_part(
                    Bucket=self._bucket,
                    Key=final_key,
                    UploadId=ensure_upload(),
                    PartNumber=part_number,
                    Body=body,
                ),
            )
            uploaded_parts.append({"PartNumber": part_number, "ETag": str(response["ETag"])})
            part_number += 1

        try:
            for chunk in chunks:
                size += len(chunk)
                chunk_view = memoryview(chunk)
                offset = 0
                while offset < len(chunk_view):
                    bytes_to_copy = min(
                        part_size - len(buffer),
                        len(chunk_view) - offset,
                    )
                    buffer.extend(chunk_view[offset : offset + bytes_to_copy])
                    offset += bytes_to_copy
                    if len(buffer) == part_size:
                        upload_part(bytes(buffer))
                        buffer.clear()

            if size != content_length:
                raise ValueError(f"collection file stream byte count mismatch: {path}")
            if buffer:
                upload_part(bytes(buffer))
                buffer.clear()
            if upload_id is None:
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=final_key,
                    Body=b"",
                    ContentLength=0,
                    Metadata=metadata,
                )
                return
            self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=final_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": uploaded_parts},
            )
        except Exception as exc:
            if upload_id is not None:
                try:
                    self._client.abort_multipart_upload(
                        Bucket=self._bucket,
                        Key=final_key,
                        UploadId=upload_id,
                    )
                except Exception as cleanup_exc:
                    exc.add_note(
                        f"failed to abort S3 multipart upload {upload_id}: {cleanup_exc!r}"
                    )
            raise

    def get_collection_file(self, collection_id: str, path: str) -> bytes:
        return b"".join(self.iter_collection_file(collection_id, path))

    def iter_collection_file(
        self,
        collection_id: str,
        path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if size is not None and size < 0:
            raise ValueError("size must be >= 0")
        if size == 0:
            return

        request: dict[str, object] = {
            "Bucket": self._bucket,
            "Key": self._key(collection_id, path),
        }
        if offset or size is not None:
            if size is None:
                request["Range"] = f"bytes={offset}-"
            else:
                request["Range"] = f"bytes={offset}-{offset + size - 1}"

        try:
            response = self._client.get_object(**request)
        except self._client.exceptions.ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise
            raise NotFound(f"file not found in hot store: {collection_id}/{path}") from exc
        body = response["Body"]
        try:
            iter_chunks = getattr(body, "iter_chunks", None)
            if callable(iter_chunks):
                yield from iter_chunks(chunk_size=1024 * 1024)
            else:
                yield cast(bytes, body.read())
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    def stat_collection_file(self, collection_id: str, path: str) -> HotFileStat | None:
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=self._key(collection_id, path))
        except self._client.exceptions.ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise
        metadata = cast(dict[str, str], head.get("Metadata") or {})
        sha256 = metadata.get(_FILE_SHA256_METADATA)
        return HotFileStat(bytes=int(head.get("ContentLength", 0)), sha256=sha256 or None)

    def has_collection_file(self, collection_id: str, path: str) -> bool:
        return self.stat_collection_file(collection_id, path) is not None

    def delete_collection_file(self, collection_id: str, path: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=self._key(collection_id, path))

    def list_collection_files(self, collection_id: str) -> list[tuple[str, int]]:
        paginator = self._client.get_paginator("list_objects_v2")
        prefix = f"collections/{collection_id}/"
        results: list[tuple[str, int]] = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for entry in page.get("Contents", []):
                key = str(entry["Key"])
                if key.endswith(".info") or key.endswith(".part"):
                    continue
                results.append((key.removeprefix(prefix), int(entry.get("Size", 0))))
        return sorted(results)


def _file_metadata(*, content_length: int, sha256: str | None) -> dict[str, str]:
    metadata = {_FILE_BYTES_METADATA: str(content_length)}
    if sha256:
        metadata[_FILE_SHA256_METADATA] = sha256
    return metadata
