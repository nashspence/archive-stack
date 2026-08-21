"""Reusable S3 mechanics for independently owned storage-adapter implementations.

This module is an optional adapter-authoring aid. It is neither a Riverhog runtime
dependency nor a runnable generic S3 adapter. Concrete adapters own their profiles,
configuration, implementation identities, lifecycle behavior, and distribution.
"""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, cast

import boto3
from botocore.config import Config
from riverhog_storage_adapter_protocol import (
    CompleteUploadRequest,
    ObjectLocator,
    ObjectReceipt,
    ReadRequest,
    ReadStatus,
    StorageAdapterDescriptor,
    UploadDeclaration,
    UploadPartReceipt,
)
from riverhog_storage_adapter_support import (
    ProviderUpload,
    RecoveryExportEntry,
    StorageDriverError,
)

_REVISION_METADATA = "riverhog-adapter-revision"
_REQUEST_METADATA = "riverhog-adapter-request-sha256"
_PROFILE_METADATA = "riverhog-adapter-profile-sha256"


class S3Target(Protocol):
    """The deliberately small target surface required by the shared mechanics."""

    @property
    def bucket(self) -> str: ...

    @property
    def prefix(self) -> str: ...


def make_s3_client(
    *,
    endpoint_url: str | None,
    region: str,
    access_key_id: str | None,
    secret_access_key: str | None,
    session_token: str | None = None,
    force_path_style: bool = False,
    max_pool_connections: int = 32,
) -> Any:
    """Build the checked S3 client used by reference adapter implementations."""

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        aws_session_token=session_token,
        config=Config(
            max_pool_connections=max_pool_connections,
            connect_timeout=10,
            read_timeout=300,
            tcp_keepalive=True,
            retries={"mode": "standard", "max_attempts": 8},
            s3={"addressing_style": "path" if force_path_style else "virtual"},
        ),
    )


class S3CompatibleStorageDriver:
    """Shared immediate-read S3 object mechanics behind the public adapter protocol.

    Subclasses may override read lifecycle and content transport while retaining exact
    opaque revision, conditional replacement, multipart, and verification semantics.
    """

    def __init__(
        self,
        *,
        target: S3Target,
        descriptor: StorageAdapterDescriptor,
        client: Any,
        provider_label: str,
    ) -> None:
        self._target = target
        self._descriptor = descriptor
        self._client = client
        self._provider_label = provider_label

    def descriptor(self) -> StorageAdapterDescriptor:
        return self._descriptor

    def ready(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._target.bucket)
        except Exception as exc:
            raise self._error(exc) from exc

    def create_upload(self, declaration: UploadDeclaration) -> ProviderUpload:
        generated_revision = secrets.token_hex(16)
        prior_etag: str | None = None
        if declaration.condition.mode == "create_only":
            if self._head_current(declaration.object_path) is not None:
                raise StorageDriverError("revision_conflict", "create-only object already exists")
        else:
            prior = declaration.condition.prior_revision
            if prior is None:  # pragma: no cover - protocol validation
                raise StorageDriverError("revision_conflict", "prior revision is required")
            head = self._head_exact(declaration.object_path, prior, require_current=True)
            prior_etag = str(head.get("ETag", ""))
            if not prior_etag:
                raise StorageDriverError(
                    "revision_conflict", "prior object has no exact replacement identity"
                )
        provider_upload_id: str | None = None
        if declaration.stored_bytes > 0:
            request: dict[str, Any] = {
                "Bucket": self._target.bucket,
                "Key": self._key(declaration.object_path),
                "ContentType": declaration.content_type,
                "Metadata": self._metadata(declaration, generated_revision),
                **self._create_upload_parameters(declaration),
            }
            try:
                response = cast(dict[str, Any], self._client.create_multipart_upload(**request))
            except Exception as exc:
                raise self._error(exc) from exc
            provider_upload_id = str(response.get("UploadId", ""))
            if not provider_upload_id:
                raise StorageDriverError(
                    "internal_failure",
                    f"{self._provider_label} did not return a multipart upload ID",
                )
        return ProviderUpload(
            _encode(
                {
                    "upload_id": provider_upload_id,
                    "generated_revision": generated_revision,
                    "prior_etag": prior_etag,
                }
            )
        )

    def upload_part(
        self,
        *,
        declaration: UploadDeclaration,
        upload: ProviderUpload,
        number: int,
        content: bytes,
        stored_sha256: str,
    ) -> str:
        provider_upload_id = self._private_upload(upload)["upload_id"]
        if not isinstance(provider_upload_id, str):
            raise StorageDriverError("upload_conflict", "zero-byte upload cannot accept parts")
        try:
            response = cast(
                dict[str, Any],
                self._client.upload_part(
                    Bucket=self._target.bucket,
                    Key=self._key(declaration.object_path),
                    UploadId=provider_upload_id,
                    PartNumber=number,
                    Body=content,
                    ContentLength=len(content),
                ),
            )
        except Exception as exc:
            raise self._error(exc) from exc
        etag = str(response.get("ETag", ""))
        if not etag:
            raise StorageDriverError(
                "internal_failure", f"{self._provider_label} omitted the part token"
            )
        return _encode({"etag": etag, "sha256": stored_sha256})

    def verify_part_receipt(
        self,
        *,
        declaration: UploadDeclaration,
        upload: ProviderUpload,
        receipt: UploadPartReceipt,
    ) -> None:
        provider_upload_id = self._private_upload(upload)["upload_id"]
        if not isinstance(provider_upload_id, str):
            raise StorageDriverError("upload_conflict", "zero-byte upload contains a part")
        expected = _decode(receipt.part_token)
        try:
            response = cast(
                dict[str, Any],
                self._client.list_parts(
                    Bucket=self._target.bucket,
                    Key=self._key(declaration.object_path),
                    UploadId=provider_upload_id,
                    PartNumberMarker=max(0, receipt.number - 1),
                    MaxParts=1,
                ),
            )
        except Exception as exc:
            raise self._error(exc) from exc
        raw_parts = response.get("Parts") or ()
        parts = cast(Sequence[Mapping[str, object]], raw_parts)
        if (
            len(parts) != 1
            or int(str(parts[0].get("PartNumber", 0))) != receipt.number
            or int(str(parts[0].get("Size", -1))) != receipt.stored_bytes
            or str(parts[0].get("ETag", "")) != expected.get("etag")
            or expected.get("sha256") != receipt.stored_sha256
        ):
            raise StorageDriverError("integrity_failure", "multipart receipt changed")

    def complete_upload(
        self,
        *,
        declaration: UploadDeclaration,
        upload: ProviderUpload,
        completion: CompleteUploadRequest,
    ) -> ObjectReceipt:
        private = self._private_upload(upload)
        provider_upload_id = private["upload_id"]
        generated_revision = str(private["generated_revision"])
        prior_etag = cast(str | None, private.get("prior_etag"))
        version_id: str | None = None
        try:
            if provider_upload_id is None:
                request: dict[str, Any] = {
                    "Bucket": self._target.bucket,
                    "Key": self._key(declaration.object_path),
                    "Body": b"",
                    "ContentLength": 0,
                    "ContentType": declaration.content_type,
                    "Metadata": self._metadata(declaration, generated_revision),
                    **self._put_object_parameters(declaration),
                }
                _apply_condition(request, declaration, prior_etag=prior_etag)
                response = cast(dict[str, Any], self._client.put_object(**request))
            else:
                request = {
                    "Bucket": self._target.bucket,
                    "Key": self._key(declaration.object_path),
                    "UploadId": provider_upload_id,
                    "MultipartUpload": {
                        "Parts": [
                            {
                                "PartNumber": part.number,
                                "ETag": str(_decode(part.part_token)["etag"]),
                            }
                            for part in completion.parts
                        ]
                    },
                }
                _apply_condition(request, declaration, prior_etag=prior_etag)
                response = cast(dict[str, Any], self._client.complete_multipart_upload(**request))
            if response.get("VersionId") is not None:
                version_id = str(response["VersionId"])
        except Exception as exc:
            head = self._head_generated(declaration.object_path, generated_revision)
            if head is None:
                raise self._error(exc) from exc
            return self._receipt(
                declaration,
                completion,
                generated_revision=generated_revision,
                version_id=(str(head["VersionId"]) if head.get("VersionId") is not None else None),
                head=head,
            )
        head = self._head_generated(
            declaration.object_path, generated_revision, version_id=version_id
        )
        if head is None:
            raise StorageDriverError(
                "integrity_failure",
                f"{self._provider_label} completion succeeded without the exact object",
            )
        return self._receipt(
            declaration,
            completion,
            generated_revision=generated_revision,
            version_id=version_id,
            head=head,
        )

    def abort_upload(self, *, declaration: UploadDeclaration, upload: ProviderUpload) -> None:
        provider_upload_id = self._private_upload(upload)["upload_id"]
        if provider_upload_id is None:
            return
        try:
            self._client.abort_multipart_upload(
                Bucket=self._target.bucket,
                Key=self._key(declaration.object_path),
                UploadId=str(provider_upload_id),
            )
        except Exception as exc:
            if self._error_code(exc) not in {"NoSuchUpload", "NoSuchKey", "404", "NotFound"}:
                raise self._error(exc) from exc

    def verify_object(self, receipt: ObjectReceipt) -> None:
        head = self._head_exact(receipt.object_path, receipt.revision)
        if (
            int(str(head.get("ContentLength", -1))) != receipt.stored_bytes
            or str(head.get("ContentType", "")) != receipt.content_type
        ):
            raise StorageDriverError("integrity_failure", "object metadata changed")

    def iter_object_content(
        self,
        locator: ObjectLocator,
        *,
        offset: int | None,
        size: int | None,
    ) -> Iterator[bytes]:
        revision = self._decode_revision(locator.revision)
        request: dict[str, Any] = {
            "Bucket": self._target.bucket,
            "Key": self._key(locator.object_path),
        }
        if revision.get("version_id") is not None:
            request["VersionId"] = str(revision["version_id"])
        if offset is not None and size is not None:
            request["Range"] = f"bytes={offset}-{offset + size - 1}"
        try:
            response = cast(dict[str, Any], self._client.get_object(**request))
        except Exception as exc:
            raise self._error(exc) from exc
        yield from _iter_body(response.get("Body"))

    def delete_object(self, locator: ObjectLocator) -> None:
        revision = self._decode_revision(locator.revision)
        request: dict[str, Any] = {
            "Bucket": self._target.bucket,
            "Key": self._key(locator.object_path),
        }
        if revision.get("version_id") is not None:
            request["VersionId"] = str(revision["version_id"])
        else:
            head = self._head_exact(locator.object_path, locator.revision, require_current=True)
            etag = str(head.get("ETag", ""))
            if not etag:
                raise StorageDriverError("revision_conflict", "object has no exact ETag")
            request["IfMatch"] = etag
        try:
            self._client.delete_object(**request)
        except Exception as exc:
            raise self._error(exc) from exc

    def delete_prefix(self, object_prefix: str) -> int:
        key_prefix = self._key(object_prefix)
        if not key_prefix.endswith("/"):
            key_prefix += "/"
        deleted = 0
        try:
            paginator = self._client.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=self._target.bucket, Prefix=key_prefix):
                objects = [
                    {"Key": item["Key"], "VersionId": item["VersionId"]}
                    for item in [
                        *(page.get("Versions") or ()),
                        *(page.get("DeleteMarkers") or ()),
                    ]
                ]
                if objects:
                    self._client.delete_objects(
                        Bucket=self._target.bucket, Delete={"Objects": objects}
                    )
                    deleted += len(objects)
        except Exception as exc:
            raise self._error(exc) from exc
        return deleted

    def prepare_read(self, request: ReadRequest) -> ReadStatus:
        for locator in request.objects:
            self._head_exact(locator.object_path, locator.revision)
        return ReadStatus(state="ready")

    def read_status(self, request: ReadRequest) -> ReadStatus:
        return self.prepare_read(request)

    def cleanup_read(self, request: ReadRequest) -> None:
        _ = request

    def abort_incomplete_uploads(self, *, initiated_before: str) -> int:
        cutoff = datetime.fromisoformat(initiated_before.replace("Z", "+00:00"))
        request: dict[str, Any] = {
            "Bucket": self._target.bucket,
            "Prefix": self._key(""),
        }
        aborted = 0
        try:
            while True:
                response = cast(dict[str, Any], self._client.list_multipart_uploads(**request))
                for upload in response.get("Uploads") or ():
                    initiated = upload.get("Initiated")
                    if not isinstance(initiated, datetime) or initiated.astimezone(UTC) >= cutoff:
                        continue
                    self._client.abort_multipart_upload(
                        Bucket=self._target.bucket,
                        Key=str(upload["Key"]),
                        UploadId=str(upload["UploadId"]),
                    )
                    aborted += 1
                if not response.get("IsTruncated"):
                    break
                request["KeyMarker"] = response["NextKeyMarker"]
                request["UploadIdMarker"] = response["NextUploadIdMarker"]
        except Exception as exc:
            raise self._error(exc) from exc
        return aborted

    def iter_recovery_export_entries(self) -> Iterator[RecoveryExportEntry]:
        """List the current configured root for adapter-local recovery export."""

        root_prefix = self._key("")
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=self._target.bucket,
                Prefix=root_prefix,
            ):
                for item in page.get("Contents") or ():
                    key = str(item["Key"])
                    if root_prefix and not key.startswith(root_prefix):
                        raise StorageDriverError(
                            "integrity_failure",
                            "provider recovery listing escaped the configured root",
                        )
                    object_path = key[len(root_prefix) :] if root_prefix else key
                    head = cast(
                        dict[str, Any],
                        self._client.head_object(Bucket=self._target.bucket, Key=key),
                    )
                    stored_bytes = int(str(head.get("ContentLength", -1)))
                    if stored_bytes < 0:
                        raise StorageDriverError(
                            "integrity_failure",
                            "provider recovery listing omitted object length",
                        )
                    version_id = (
                        str(head["VersionId"]) if head.get("VersionId") is not None else None
                    )
                    yield RecoveryExportEntry(
                        object_path=object_path,
                        stored_bytes=stored_bytes,
                        source_ref=_encode({"key": key, "version_id": version_id}),
                    )
        except StorageDriverError:
            raise
        except Exception as exc:
            raise self._error(exc) from exc

    def iter_recovery_export_content(
        self,
        entry: RecoveryExportEntry,
    ) -> Iterator[bytes]:
        """Stream one exact current-root object selected by recovery export."""

        source = _decode(entry.source_ref)
        key = source.get("key")
        if not isinstance(key, str) or key != self._key(entry.object_path):
            raise StorageDriverError(
                "integrity_failure",
                "recovery-export source binding changed",
            )
        request: dict[str, Any] = {"Bucket": self._target.bucket, "Key": key}
        if source.get("version_id") is not None:
            request["VersionId"] = str(source["version_id"])
        try:
            response = cast(dict[str, Any], self._client.get_object(**request))
            yield from _iter_body(response.get("Body"))
        except Exception as exc:
            raise self._error(exc) from exc

    def _create_upload_parameters(self, declaration: UploadDeclaration) -> dict[str, object]:
        _ = declaration
        return {}

    def _put_object_parameters(self, declaration: UploadDeclaration) -> dict[str, object]:
        _ = declaration
        return {}

    def _metadata(self, declaration: UploadDeclaration, generated_revision: str) -> dict[str, str]:
        return {
            _REVISION_METADATA: generated_revision,
            _REQUEST_METADATA: declaration.request_sha256,
            _PROFILE_METADATA: self._descriptor.profile.profile_contract_sha256,
        }

    def _receipt(
        self,
        declaration: UploadDeclaration,
        completion: CompleteUploadRequest,
        *,
        generated_revision: str,
        version_id: str | None,
        head: dict[str, Any],
    ) -> ObjectReceipt:
        if (
            int(str(head.get("ContentLength", -1))) != completion.stored_bytes
            or _metadata(head).get(_REVISION_METADATA) != generated_revision
            or _metadata(head).get(_REQUEST_METADATA) != declaration.request_sha256
        ):
            raise StorageDriverError("integrity_failure", "completed object changed")
        modified = head.get("LastModified")
        completed_at = (
            modified.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if isinstance(modified, datetime)
            else datetime.now(UTC).isoformat().replace("+00:00", "Z")
        )
        return ObjectReceipt(
            object_path=declaration.object_path,
            revision=_encode({"revision": generated_revision, "version_id": version_id}),
            content_type=declaration.content_type,
            stored_bytes=completion.stored_bytes,
            stored_sha256=completion.stored_sha256,
            completed_at=completed_at,
        )

    def _head_current(self, object_path: str) -> dict[str, Any] | None:
        try:
            return cast(
                dict[str, Any],
                self._client.head_object(Bucket=self._target.bucket, Key=self._key(object_path)),
            )
        except Exception as exc:
            if self._missing(exc):
                return None
            raise self._error(exc) from exc

    def _head_generated(
        self,
        object_path: str,
        generated_revision: str,
        *,
        version_id: str | None = None,
    ) -> dict[str, Any] | None:
        request: dict[str, Any] = {
            "Bucket": self._target.bucket,
            "Key": self._key(object_path),
        }
        if version_id is not None:
            request["VersionId"] = version_id
        try:
            head = cast(dict[str, Any], self._client.head_object(**request))
        except Exception as exc:
            if self._missing(exc):
                return None
            raise self._error(exc) from exc
        return head if _metadata(head).get(_REVISION_METADATA) == generated_revision else None

    def _head_exact(
        self,
        object_path: str,
        public_revision: str,
        *,
        require_current: bool = False,
    ) -> dict[str, Any]:
        revision = self._decode_revision(public_revision)
        request: dict[str, Any] = {
            "Bucket": self._target.bucket,
            "Key": self._key(object_path),
        }
        if revision.get("version_id") is not None and not require_current:
            request["VersionId"] = str(revision["version_id"])
        try:
            head = cast(dict[str, Any], self._client.head_object(**request))
        except Exception as exc:
            raise self._error(exc) from exc
        if _metadata(head).get(_REVISION_METADATA) != revision.get("revision"):
            raise StorageDriverError("revision_conflict", "object revision changed")
        return head

    def _key(self, object_path: str) -> str:
        if not self._target.prefix:
            return object_path
        if not object_path:
            return f"{self._target.prefix}/"
        return f"{self._target.prefix}/{object_path}"

    def _private_upload(self, upload: ProviderUpload) -> dict[str, object]:
        value = _decode(upload.upload_id)
        if "generated_revision" not in value or "upload_id" not in value:
            raise StorageDriverError("upload_conflict", "S3 upload binding is invalid")
        return value

    def _decode_revision(self, value: str) -> dict[str, object]:
        revision = _decode(value)
        if not isinstance(revision.get("revision"), str):
            raise StorageDriverError("revision_conflict", "S3 object revision is invalid")
        return revision

    def _error_code(self, exc: Exception) -> str:
        response = getattr(exc, "response", {})
        if not isinstance(response, dict):
            return ""
        error = response.get("Error", {})
        return str(error.get("Code", "")) if isinstance(error, dict) else ""

    def _missing(self, exc: Exception) -> bool:
        return self._error_code(exc) in {"NoSuchKey", "NoSuchVersion", "404", "NotFound"}

    def _error(self, exc: Exception) -> StorageDriverError:
        code = self._error_code(exc)
        if code in {"NoSuchKey", "NoSuchVersion", "NoSuchUpload", "404", "NotFound"}:
            return StorageDriverError("not_found", "object or upload does not exist")
        if code in {
            "PreconditionFailed",
            "ConditionalRequestConflict",
            "InvalidRequest",
            "InvalidPart",
            "InvalidPartOrder",
        }:
            return StorageDriverError("revision_conflict", "conditional identity changed")
        if code in {"InvalidRange", "RequestedRangeNotSatisfiable"}:
            return StorageDriverError("invalid_range", "provider rejected the object range")
        if code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
            return StorageDriverError(
                "provider_unavailable", "provider credentials are not authorized"
            )
        return StorageDriverError("provider_unavailable", "storage operation failed")


def _apply_condition(
    request: dict[str, Any], declaration: UploadDeclaration, *, prior_etag: str | None
) -> None:
    if declaration.condition.mode == "create_only":
        request["IfNoneMatch"] = "*"
    else:
        if prior_etag is None:
            raise StorageDriverError("revision_conflict", "prior object ETag is unavailable")
        request["IfMatch"] = prior_etag


def _encode(value: dict[str, object]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> dict[str, object]:
    try:
        padding_bytes = "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(value + padding_bytes))
    except (ValueError, json.JSONDecodeError) as exc:
        raise StorageDriverError("revision_conflict", "opaque S3 token is invalid") from exc
    if not isinstance(decoded, dict):
        raise StorageDriverError("revision_conflict", "opaque S3 token is invalid")
    return cast(dict[str, object], decoded)


def _metadata(head: dict[str, Any]) -> dict[str, str]:
    raw = head.get("Metadata")
    if not isinstance(raw, dict):
        return {}
    return {str(key).casefold(): str(value) for key, value in raw.items()}


def _iter_body(body: object) -> Iterator[bytes]:
    try:
        yield from body.iter_chunks(chunk_size=8 * 1024**2)  # type: ignore[attr-defined]
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()


__all__ = ["S3CompatibleStorageDriver", "S3Target", "make_s3_client"]
