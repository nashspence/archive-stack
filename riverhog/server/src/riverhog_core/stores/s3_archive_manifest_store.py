from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, cast

from botocore.exceptions import ClientError
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.ports.archive_manifest_store import ImmutableObjectReceipt
from riverhog_core.runtime_config import ArchiveStoreConfig, RuntimeConfig
from riverhog_core.stores.s3_client import create_archive_s3_client
from riverhog_core.throughput import S3TransportTuning

_STORED_SHA256_METADATA = "riverhog-stored-sha256"


class S3ImmutableArchiveObjectStore:
    """Create-only S3 objects with logical-identity based retry recovery."""

    def __init__(
        self,
        config: RuntimeConfig,
        store: ArchiveStoreConfig,
        *,
        transport_tuning: S3TransportTuning | None = None,
    ) -> None:
        self._bucket = store.bucket
        self._storage_class = store.storage_class
        self._client = create_archive_s3_client(
            config,
            store,
            tuning=transport_tuning,
        )

    def put_immutable_object(
        self,
        *,
        object_path: str,
        content: bytes,
        content_type: str,
        identity_metadata: dict[str, str],
    ) -> ImmutableObjectReceipt:
        if not object_path or not content or not content_type:
            raise ValueError("immutable archive object identity and content are required")
        normalized_identity = {
            str(key).casefold(): str(value) for key, value in identity_metadata.items()
        }
        existing = self._head_matching(
            object_path=object_path,
            identity_metadata=normalized_identity,
        )
        if existing is not None:
            return existing

        stored_sha256 = hashlib.sha256(content).hexdigest()
        metadata = {**normalized_identity, _STORED_SHA256_METADATA: stored_sha256}
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": object_path,
            "Body": content,
            "ContentLength": len(content),
            "ContentType": content_type,
            "Metadata": metadata,
            "IfNoneMatch": "*",
        }
        if self._storage_class:
            request["StorageClass"] = self._storage_class
        try:
            response = cast(dict[str, Any], self._client.put_object(**request))
        except ClientError as exc:
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if status == 501 or code in {"NotImplemented", "UnsupportedHeader"}:
                raise RuntimeError("archive store must support If-None-Match on PutObject") from exc
            if status not in {409, 412} and code not in {
                "ConditionalRequestConflict",
                "PreconditionFailed",
            }:
                raise
            recovered = self._head_matching(
                object_path=object_path,
                identity_metadata=normalized_identity,
            )
            if recovered is None:
                raise RuntimeError(
                    "immutable archive object already exists with a different identity"
                ) from exc
            return recovered

        head = self._head(object_path)
        if head is None:
            raise RuntimeError("S3 put succeeded but the immutable object is not readable")
        receipt = self._receipt(object_path, head)
        if receipt.stored_bytes != len(content) or receipt.stored_sha256 != stored_sha256:
            raise RuntimeError("S3 immutable object does not match the uploaded content")
        response_version = response.get("VersionId")
        if receipt.version_id is None and response_version is not None:
            receipt = ImmutableObjectReceipt(
                object_path=receipt.object_path,
                version_id=str(response_version),
                etag=receipt.etag,
                stored_bytes=receipt.stored_bytes,
                stored_sha256=receipt.stored_sha256,
                completed_at=receipt.completed_at,
            )
        return receipt

    def _head_matching(
        self,
        *,
        object_path: str,
        identity_metadata: dict[str, str],
    ) -> ImmutableObjectReceipt | None:
        head = self._head(object_path)
        if head is None:
            return None
        metadata = {
            str(key).casefold(): str(value)
            for key, value in cast(dict[str, Any], head.get("Metadata") or {}).items()
        }
        if any(metadata.get(key) != value for key, value in identity_metadata.items()):
            return None
        return self._receipt(object_path, head)

    def _head(self, object_path: str) -> dict[str, Any] | None:
        try:
            return cast(
                dict[str, Any],
                self._client.head_object(Bucket=self._bucket, Key=object_path),
            )
        except ClientError as exc:
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise

    @staticmethod
    def _receipt(object_path: str, head: dict[str, Any]) -> ImmutableObjectReceipt:
        metadata = {
            str(key).casefold(): str(value)
            for key, value in cast(dict[str, Any], head.get("Metadata") or {}).items()
        }
        stored_sha256 = metadata.get(_STORED_SHA256_METADATA, "")
        if len(stored_sha256) != 64 or any(
            current not in "0123456789abcdef" for current in stored_sha256
        ):
            raise RuntimeError("immutable S3 object is missing its stored sha256 metadata")
        last_modified = head.get("LastModified")
        completed_at = (
            format_utc_timestamp(last_modified)
            if isinstance(last_modified, datetime)
            else format_utc_timestamp(utc_now())
        )
        version_id = head.get("VersionId")
        etag = head.get("ETag")
        return ImmutableObjectReceipt(
            object_path=object_path,
            version_id=str(version_id) if version_id is not None else None,
            etag=str(etag) if etag is not None else None,
            stored_bytes=int(str(head["ContentLength"])),
            stored_sha256=stored_sha256,
            completed_at=completed_at,
        )
