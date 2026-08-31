"""S3 object operations preserving Riverhog's established storage semantics."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from botocore.exceptions import ClientError
from riverhog_storage_adapter_protocol import (
    ADAPTER_PRIVATE_ASSERTION_PREFIX,
    AbortIncompleteWritesRequest,
    AdapterDescriptor,
    BinaryContent,
    CompletedObjectReceipt,
    CompletedWriteLookupRequest,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ImmutableObjectReceipt,
    ObjectHeadRequest,
    ObjectLocator,
    ObjectMetadataReceipt,
    ObjectPlacement,
    ObjectReadReceipt,
    ObjectReadRequest,
    ObjectReadStream,
    ReadMode,
    ReadPreparationRequest,
    ReadReadiness,
    ReadReady,
    ReadStatus,
    SmallObjectWriteRequest,
    StorageAdapterRejection,
    WriteCompleteRequest,
    WriteSegmentReceipt,
    WriteSegmentSet,
    WriteSession,
    WriteStartRequest,
)
from time_formats import format_utc_timestamp, parse_utc_timestamp, utc_now

_MINIMUM_NONFINAL_PART_BYTES = 5 * 1024 * 1024
_MAXIMUM_PART_BYTES = 5 * 1024 * 1024 * 1024
_MAXIMUM_PART_COUNT = 10_000
_DEFAULT_READ_CHUNK_BYTES = 8 * 1024 * 1024
_DEFAULT_WRITE_CHUNK_BYTES = 1024 * 1024
_STORED_SHA256_METADATA = f"{ADAPTER_PRIVATE_ASSERTION_PREFIX}stored-sha256"
_PLACEMENT_METADATA = f"{ADAPTER_PRIVATE_ASSERTION_PREFIX}placement"
_RESERVED_METADATA = frozenset({_STORED_SHA256_METADATA, _PLACEMENT_METADATA})


@dataclass(frozen=True, slots=True)
class S3StorageAdapterConfig:
    implementation_id: str
    implementation_version: str
    bucket: str
    root_prefix: str = ""
    read_mode: ReadMode = "immediate"
    archive_storage_class: str | None = None
    immediate_storage_class: str | None = None
    read_chunk_bytes: int = _DEFAULT_READ_CHUNK_BYTES

    def __post_init__(self) -> None:
        if not self.implementation_id or not self.implementation_version:
            raise ValueError("S3 adapter implementation identity must be nonempty")
        if not self.bucket.strip():
            raise ValueError("S3 adapter bucket must be nonempty")
        normalized_root = self.root_prefix.strip("/")
        if normalized_root != self.root_prefix:
            raise ValueError("S3 adapter root prefix must be normalized without slashes")
        if self.read_chunk_bytes < 64 * 1024:
            raise ValueError("S3 adapter read chunk bytes must be at least 64 KiB")


class S3ReadPreparation(Protocol):
    """Provider-specific read preparation configured wholly inside an adapter."""

    def prepare(
        self,
        *,
        client: Any,
        bucket: str,
        objects: tuple[tuple[str, str | None], ...],
    ) -> ReadReadiness: ...

    def status(
        self,
        *,
        client: Any,
        bucket: str,
        objects: tuple[tuple[str, str | None], ...],
    ) -> ReadReadiness: ...

    def cleanup(
        self,
        *,
        client: Any,
        bucket: str,
        objects: tuple[tuple[str, str | None], ...],
    ) -> None: ...


class S3ObjectReader(Protocol):
    """Optional provider-owned delivery path such as exact CloudFront reads."""

    def read_object(
        self,
        *,
        client: Any,
        bucket: str,
        key: str,
        object_path: str,
        revision: str | None,
        offset: int | None,
        size: int | None,
        expected_bytes: int,
        chunk_bytes: int,
    ) -> ObjectReadStream: ...


class S3StorageAdapter:
    """One configured S3 target implementing direct opaque-object capabilities."""

    def __init__(
        self,
        client: Any,
        config: S3StorageAdapterConfig,
        *,
        read_preparation: S3ReadPreparation | None = None,
        object_reader: S3ObjectReader | None = None,
    ) -> None:
        if config.read_mode == "restore_required" and read_preparation is None:
            raise ValueError("restore-required S3 adapter needs a read-preparation implementation")
        if config.read_mode == "immediate" and read_preparation is not None:
            raise ValueError("immediate S3 adapter must not configure read preparation")
        self._client = client
        self._config = config
        self._read_preparation = read_preparation
        self._object_reader = object_reader or _DirectS3ObjectReader()

    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            implementation_id=self._config.implementation_id,
            implementation_version=self._config.implementation_version,
            read_mode=self._config.read_mode,
            minimum_nonfinal_segment_bytes=_MINIMUM_NONFINAL_PART_BYTES,
            maximum_segment_bytes=_MAXIMUM_PART_BYTES,
            maximum_segment_count=_MAXIMUM_PART_COUNT,
        )

    def begin_write(self, request: WriteStartRequest) -> WriteSession:
        metadata = self._stored_metadata(
            request.required_identity_assertions,
            placement=request.placement,
        )
        provider_request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": self._key(request.object_path),
            "ContentType": request.content_type,
            "Metadata": metadata,
        }
        if storage_class := self._storage_class(request.placement):
            provider_request["StorageClass"] = storage_class
        response = cast(dict[str, Any], self._client.create_multipart_upload(**provider_request))
        upload_id = str(response.get("UploadId", ""))
        if not upload_id:
            raise RuntimeError("S3 did not return a multipart upload id")
        return WriteSession(
            object_path=request.object_path,
            expected_bytes=request.expected_bytes,
            write_token=upload_id,
        )

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        stored_bytes: int,
        content: BinaryContent,
    ) -> WriteSegmentReceipt:
        if number < 1 or number > _MAXIMUM_PART_COUNT:
            raise StorageAdapterRejection(
                "invalid_request",
                "write segment number is outside the adapter limit",
            )
        if stored_bytes < 1 or stored_bytes > _MAXIMUM_PART_BYTES:
            raise StorageAdapterRejection(
                "invalid_request",
                "write segment size is outside the adapter limit",
            )
        observed = _ObservedContentReader(content, expected_bytes=stored_bytes)
        response = cast(
            dict[str, Any],
            self._client.upload_part(
                Bucket=self._config.bucket,
                Key=self._key(session.object_path),
                UploadId=session.write_token,
                PartNumber=number,
                Body=observed,
                ContentLength=stored_bytes,
            ),
        )
        observed.require_consumed()
        token = str(response.get("ETag", ""))
        if not token:
            raise RuntimeError("S3 did not return a multipart part token")
        return WriteSegmentReceipt(
            number=number,
            segment_token=token,
            stored_bytes=stored_bytes,
        )

    def list_segments(self, session: WriteSession) -> WriteSegmentSet:
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": self._key(session.object_path),
            "UploadId": session.write_token,
        }
        parts: list[WriteSegmentReceipt] = []
        while True:
            response = cast(dict[str, Any], self._client.list_parts(**request))
            for raw in response.get("Parts") or ():
                if not isinstance(raw, dict):
                    continue
                parts.append(
                    WriteSegmentReceipt(
                        number=int(str(raw["PartNumber"])),
                        segment_token=str(raw["ETag"]),
                        stored_bytes=int(str(raw["Size"])),
                    )
                )
            if not response.get("IsTruncated"):
                break
            marker = response.get("NextPartNumberMarker")
            if marker is None:
                raise RuntimeError("S3 multipart listing omitted its next marker")
            request["PartNumberMarker"] = int(str(marker))
        parts.sort(key=lambda current: current.number)
        return WriteSegmentSet(session=session, segments=tuple(parts))

    def complete_write(
        self,
        request: WriteCompleteRequest,
    ) -> CompletedObjectReceipt:
        if len(request.segments) > _MAXIMUM_PART_COUNT or any(
            part.stored_bytes > _MAXIMUM_PART_BYTES for part in request.segments
        ):
            raise StorageAdapterRejection(
                "invalid_request",
                "multipart completion exceeds the S3 adapter limits",
            )
        if any(part.stored_bytes < _MINIMUM_NONFINAL_PART_BYTES for part in request.segments[:-1]):
            raise StorageAdapterRejection(
                "invalid_request",
                "multipart completion contains an undersized non-final part",
            )
        provider_request = {
            "Bucket": self._config.bucket,
            "Key": self._key(request.session.object_path),
            "UploadId": request.session.write_token,
            "MultipartUpload": {
                "Parts": [
                    {"PartNumber": part.number, "ETag": part.segment_token}
                    for part in request.segments
                ]
            },
            "IfNoneMatch": "*",
        }
        try:
            self._client.complete_multipart_upload(**provider_request)
        except ClientError as exc:
            status, code = _client_error_identity(exc)
            if status == 501 or code in {"NotImplemented", "UnsupportedHeader"}:
                raise RuntimeError(
                    "S3 target must support conditional multipart completion"
                ) from exc
            if status not in {409, 412} and code not in {
                "ConditionalRequestConflict",
                "NoSuchUpload",
                "PreconditionFailed",
            }:
                raise
            recovered = self.find_completed_write(
                CompletedWriteLookupRequest(
                    object_path=request.session.object_path,
                    expected_bytes=request.expected_bytes,
                    expected_content_type=request.expected_content_type,
                    required_identity_assertions=request.required_identity_assertions,
                    expected_placement=request.expected_placement,
                )
            )
            if recovered is None:
                raise RuntimeError(
                    "conditional multipart completion failed without a completed object"
                ) from exc
            if recovered.stored_bytes != request.expected_bytes:
                raise StorageAdapterRejection(
                    "identity_conflict",
                    "completed object byte count differs from the upload checkpoint",
                ) from exc
            return recovered
        completed = self.find_completed_write(
            CompletedWriteLookupRequest(
                object_path=request.session.object_path,
                expected_bytes=request.expected_bytes,
                expected_content_type=request.expected_content_type,
                required_identity_assertions=request.required_identity_assertions,
                expected_placement=request.expected_placement,
            )
        )
        if completed is None:
            raise RuntimeError("S3 completion succeeded but the object is not readable")
        if completed.stored_bytes != request.expected_bytes:
            raise RuntimeError("completed S3 object length differs from its parts")
        return completed

    def find_completed_write(
        self,
        request: CompletedWriteLookupRequest,
    ) -> CompletedObjectReceipt | None:
        head = self._head(request.object_path)
        if head is None:
            return None
        metadata = _normalized_metadata(head)
        if any(
            metadata.get(key) != value
            for key, value in request.required_identity_assertions.items()
        ):
            raise StorageAdapterRejection(
                "identity_conflict",
                "object already exists with different required identity assertions",
            )
        if _provider_content_type(head) != request.expected_content_type:
            raise StorageAdapterRejection(
                "identity_conflict",
                "object already exists with a different content type",
            )
        self._validate_placement(head, request.expected_placement)
        return self._completed_receipt(
            request.object_path,
            head,
            verified_identity_assertions=request.required_identity_assertions,
            verified_placement=request.expected_placement,
        )

    def abort_write(self, session: WriteSession) -> None:
        try:
            self._client.abort_multipart_upload(
                Bucket=self._config.bucket,
                Key=self._key(session.object_path),
                UploadId=session.write_token,
            )
        except ClientError as exc:
            status, code = _client_error_identity(exc)
            if status == 404 or code in {"NoSuchUpload", "NotFound"}:
                return
            raise

    def put_small_object(
        self,
        request: SmallObjectWriteRequest,
        content: BinaryContent,
    ) -> ImmutableObjectReceipt:
        observed = _ObservedContentReader(
            content,
            expected_bytes=request.stored_bytes,
            expected_sha256=request.stored_sha256,
        )
        existing = self._head(request.object_path)
        if existing is not None:
            recovered = self._matching_small_receipt(request, existing)
            if recovered is not None:
                observed.drain_and_verify()
                return recovered
            if request.mode == "create_only":
                raise StorageAdapterRejection(
                    "identity_conflict",
                    "object already exists with a different identity",
                )
        metadata = self._stored_metadata(
            request.required_identity_assertions,
            placement=request.placement,
            stored_sha256=request.stored_sha256,
        )
        provider_request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": self._key(request.object_path),
            "Body": observed,
            "ContentLength": request.stored_bytes,
            "ContentType": request.content_type,
            "Metadata": metadata,
        }
        if storage_class := self._storage_class(request.placement):
            provider_request["StorageClass"] = storage_class
        try:
            self._put_small_multipart(
                provider_request,
                create_only=request.mode == "create_only",
            )
        except ClientError as exc:
            status, code = _client_error_identity(exc)
            if status not in {409, 412} and code not in {
                "ConditionalRequestConflict",
                "PreconditionFailed",
            }:
                raise
            recovered_head = self._head(request.object_path)
            recovered = (
                self._matching_small_receipt(request, recovered_head)
                if recovered_head is not None
                else None
            )
            if recovered is None:
                raise StorageAdapterRejection(
                    "identity_conflict",
                    "object already exists with a different identity",
                ) from exc
            observed.drain_and_verify()
            return recovered
        persisted = self._head(request.object_path)
        if persisted is None:
            raise RuntimeError("S3 put succeeded but the object is not readable")
        receipt = self._immutable_receipt(
            request.object_path,
            persisted,
            verified_identity_assertions=request.required_identity_assertions,
            verified_placement=request.placement,
        )
        if (
            receipt.stored_bytes != request.stored_bytes
            or receipt.stored_sha256 != request.stored_sha256
        ):
            raise RuntimeError("persisted S3 object differs from its input")
        self._validate_placement(persisted, request.placement)
        return receipt

    def head_object(self, request: ObjectHeadRequest) -> ObjectMetadataReceipt | None:
        head = self._head(request.object.object_path, revision=request.object.revision)
        if head is None:
            return None
        self._validate_placement(head, request.expected_placement)
        metadata = _normalized_metadata(head)
        stored_sha256 = metadata.get(_STORED_SHA256_METADATA)
        if stored_sha256 is not None and not _valid_sha256(stored_sha256):
            raise RuntimeError("S3 object has invalid stored-digest metadata")
        return ObjectMetadataReceipt(
            object_path=request.object.object_path,
            revision=_provider_revision(head),
            entity_token=_provider_entity_token(head),
            content_type=(
                str(head["ContentType"]) if head.get("ContentType") is not None else None
            ),
            stored_bytes=int(str(head["ContentLength"])),
            stored_sha256=stored_sha256,
            observed_identity_assertions={
                key: value for key, value in metadata.items() if key not in _RESERVED_METADATA
            },
            verified_placement=request.expected_placement,
            completed_at=_provider_timestamp(head),
        )

    def read_object(self, request: ObjectReadRequest) -> ObjectReadStream:
        return self._object_reader.read_object(
            client=self._client,
            bucket=self._config.bucket,
            key=self._key(request.object.object_path),
            object_path=request.object.object_path,
            revision=request.object.revision,
            offset=request.offset,
            size=request.size,
            expected_bytes=request.expected_bytes,
            chunk_bytes=self._config.read_chunk_bytes,
        )

    def delete_object(self, request: DeleteObjectRequest) -> None:
        key = self._key(request.object.object_path)
        if request.mode == "all_versions":
            _delete_exact_all_versions(self._client, bucket=self._config.bucket, key=key)
            return
        provider_request: dict[str, Any] = {"Bucket": self._config.bucket, "Key": key}
        if request.mode == "exact_revision":
            provider_request["VersionId"] = request.object.revision
        self._client.delete_object(**provider_request)

    def delete_prefix(self, request: DeletePrefixRequest) -> int:
        return _delete_prefix_all_versions(
            self._client,
            bucket=self._config.bucket,
            prefix=self._key(request.object_prefix),
        )

    def prepare_read(self, request: ReadPreparationRequest) -> ReadStatus:
        if self._read_preparation is None:
            readiness: ReadReadiness = ReadReady()
        else:
            readiness = self._read_preparation.prepare(
                client=self._client,
                bucket=self._config.bucket,
                objects=self._provider_objects(request),
            )
        return ReadStatus(
            objects=request.objects,
            readiness=readiness,
        )

    def read_status(self, request: ReadPreparationRequest) -> ReadStatus:
        if self._read_preparation is None:
            readiness: ReadReadiness = ReadReady()
        else:
            readiness = self._read_preparation.status(
                client=self._client,
                bucket=self._config.bucket,
                objects=self._provider_objects(request),
            )
        return ReadStatus(
            objects=request.objects,
            readiness=readiness,
        )

    def cleanup_read(self, request: ReadPreparationRequest) -> None:
        if self._read_preparation is not None:
            self._read_preparation.cleanup(
                client=self._client,
                bucket=self._config.bucket,
                objects=self._provider_objects(request),
            )

    def abort_incomplete_writes(self, request: AbortIncompleteWritesRequest) -> int:
        cutoff = parse_utc_timestamp(request.initiated_before).astimezone(UTC)
        provider_request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Prefix": self._key(request.object_prefix),
        }
        aborted = 0
        while True:
            response = cast(dict[str, Any], self._client.list_multipart_uploads(**provider_request))
            for upload in response.get("Uploads") or ():
                if not isinstance(upload, dict):
                    continue
                key = str(upload.get("Key", ""))
                upload_id = str(upload.get("UploadId", ""))
                initiated = upload.get("Initiated")
                if (
                    not key.startswith(str(provider_request["Prefix"]))
                    or not upload_id
                    or not isinstance(initiated, datetime)
                    or initiated.tzinfo is None
                    or initiated.astimezone(UTC) >= cutoff
                ):
                    continue
                self._client.abort_multipart_upload(
                    Bucket=self._config.bucket,
                    Key=key,
                    UploadId=upload_id,
                )
                aborted += 1
            if not response.get("IsTruncated"):
                return aborted
            next_key = str(response.get("NextKeyMarker", ""))
            next_upload = str(response.get("NextUploadIdMarker", ""))
            if not next_key or not next_upload:
                raise RuntimeError("S3 multipart listing omitted pagination markers")
            provider_request["KeyMarker"] = next_key
            provider_request["UploadIdMarker"] = next_upload

    def _key(self, object_path: str) -> str:
        return "/".join(
            part for part in (self._config.root_prefix, object_path.lstrip("/")) if part
        )

    def _provider_objects(
        self,
        request: ReadPreparationRequest,
    ) -> tuple[tuple[str, str | None], ...]:
        return tuple((self._key(item.object_path), item.revision) for item in request.objects)

    def _storage_class(self, placement: ObjectPlacement) -> str | None:
        return (
            self._config.archive_storage_class
            if placement == "archive"
            else self._config.immediate_storage_class
        )

    def _stored_metadata(
        self,
        identity: dict[str, str],
        *,
        placement: ObjectPlacement,
        stored_sha256: str | None = None,
    ) -> dict[str, str]:
        metadata = {**identity, _PLACEMENT_METADATA: placement}
        if stored_sha256 is not None:
            metadata[_STORED_SHA256_METADATA] = stored_sha256
        return metadata

    def _validate_placement(self, head: dict[str, Any], expected: ObjectPlacement) -> None:
        metadata = _normalized_metadata(head)
        if marker := metadata.get(_PLACEMENT_METADATA):
            if marker != expected:
                raise RuntimeError("S3 object placement marker differs from its request")
            return
        expected_class = (self._storage_class(expected) or "STANDARD").upper()
        actual_class = str(head.get("StorageClass") or "STANDARD").upper()
        if expected_class != actual_class:
            raise RuntimeError("S3 object storage placement differs from its request")

    def _matching_small_receipt(
        self,
        request: SmallObjectWriteRequest,
        head: dict[str, Any],
    ) -> ImmutableObjectReceipt | None:
        metadata = _normalized_metadata(head)
        if any(
            metadata.get(key) != value
            for key, value in request.required_identity_assertions.items()
        ):
            return None
        if _provider_content_type(head) != request.content_type:
            return None
        self._validate_placement(head, request.placement)
        receipt = self._immutable_receipt(
            request.object_path,
            head,
            verified_identity_assertions=request.required_identity_assertions,
            verified_placement=request.placement,
        )
        # Stable required identity assertions, rather than incidental stored
        # bytes, define reconciliation identity.  This permits an interrupted
        # create-only publication to recover an earlier randomized encrypted
        # representation of the same exact plaintext authority.
        return receipt

    def _put_small_multipart(
        self,
        request: dict[str, Any],
        *,
        create_only: bool,
    ) -> None:
        create_request = {
            key: request[key]
            for key in ("Bucket", "Key", "ContentType", "Metadata", "StorageClass")
            if key in request
        }
        created = cast(dict[str, Any], self._client.create_multipart_upload(**create_request))
        upload_id = str(created.get("UploadId", ""))
        if not upload_id:
            raise RuntimeError("S3 did not return a multipart upload id")
        upload_request = {
            "Bucket": request["Bucket"],
            "Key": request["Key"],
            "UploadId": upload_id,
        }
        try:
            part = cast(
                dict[str, Any],
                self._client.upload_part(
                    **upload_request,
                    PartNumber=1,
                    Body=request["Body"],
                    ContentLength=request["ContentLength"],
                ),
            )
            token = str(part.get("ETag", ""))
            if not token:
                raise RuntimeError("S3 did not return a multipart part token")
            body = request["Body"]
            if not isinstance(body, _ObservedContentReader):
                raise TypeError("small-object upload body is not observable")
            body.require_consumed()
            completion_request: dict[str, Any] = {
                **upload_request,
                "MultipartUpload": {"Parts": [{"PartNumber": 1, "ETag": token}]},
            }
            if create_only:
                completion_request["IfNoneMatch"] = "*"
            self._client.complete_multipart_upload(**completion_request)
        except Exception:
            try:
                self._client.abort_multipart_upload(**upload_request)
            except ClientError as abort_exc:
                status, code = _client_error_identity(abort_exc)
                if status != 404 and code not in {"NoSuchUpload", "NotFound"}:
                    raise
            raise

    def _head(
        self,
        object_path: str,
        *,
        revision: str | None = None,
    ) -> dict[str, Any] | None:
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": self._key(object_path),
        }
        if revision is not None:
            request["VersionId"] = revision
        try:
            return cast(dict[str, Any], self._client.head_object(**request))
        except ClientError as exc:
            status, code = _client_error_identity(exc)
            if status == 404 or code in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}:
                return None
            raise

    @staticmethod
    def _completed_receipt(
        object_path: str,
        head: dict[str, Any],
        *,
        verified_identity_assertions: dict[str, str],
        verified_placement: ObjectPlacement,
    ) -> CompletedObjectReceipt:
        return CompletedObjectReceipt(
            object_path=object_path,
            revision=_provider_revision(head),
            entity_token=_provider_entity_token(head),
            stored_bytes=int(str(head["ContentLength"])),
            verified_content_type=_provider_content_type(head),
            verified_identity_assertions=verified_identity_assertions,
            verified_placement=verified_placement,
            completed_at=_provider_timestamp(head),
        )

    @staticmethod
    def _immutable_receipt(
        object_path: str,
        head: dict[str, Any],
        *,
        verified_identity_assertions: dict[str, str],
        verified_placement: ObjectPlacement,
    ) -> ImmutableObjectReceipt:
        stored_sha256 = _normalized_metadata(head).get(_STORED_SHA256_METADATA, "")
        if not _valid_sha256(stored_sha256):
            raise RuntimeError("S3 immutable object is missing its stored digest")
        return ImmutableObjectReceipt(
            object_path=object_path,
            revision=_provider_revision(head),
            entity_token=_provider_entity_token(head),
            stored_bytes=int(str(head["ContentLength"])),
            stored_sha256=stored_sha256,
            verified_content_type=_provider_content_type(head),
            verified_identity_assertions=verified_identity_assertions,
            verified_placement=verified_placement,
            completed_at=_provider_timestamp(head),
        )


class _ObservedContentReader:
    """Expose one bounded, non-seekable stream while verifying exact custody."""

    def __init__(
        self,
        content: BinaryContent,
        *,
        expected_bytes: int,
        expected_sha256: str | None = None,
    ) -> None:
        self._chunks = iter((content,) if isinstance(content, bytes) else content)
        self._pending = memoryview(b"")
        self._expected_bytes = expected_bytes
        self._expected_sha256 = expected_sha256
        self._observed_bytes = 0
        self._digest = hashlib.sha256()
        self._ended = False

    def read(self, size: int | None = -1) -> bytes:
        if size == 0 or self._ended:
            return b""
        maximum = (
            _DEFAULT_WRITE_CHUNK_BYTES
            if size is None or size < 0
            else min(
                size,
                _DEFAULT_WRITE_CHUNK_BYTES,
            )
        )
        if maximum < 1:
            return b""
        result = bytearray()
        while len(result) < maximum:
            if not self._pending:
                try:
                    chunk = next(self._chunks)
                except StopIteration:
                    self._ended = True
                    break
                if not isinstance(chunk, bytes):
                    raise StorageAdapterRejection(
                        "integrity_failure",
                        "opaque upload content contains a non-byte chunk",
                    )
                if not chunk:
                    continue
                self._pending = memoryview(chunk)
            remaining = self._expected_bytes - self._observed_bytes - len(result)
            if remaining <= 0:
                raise StorageAdapterRejection(
                    "integrity_failure",
                    "opaque upload content exceeds its declaration",
                )
            count = min(maximum - len(result), len(self._pending), remaining)
            result.extend(self._pending[:count])
            self._pending = self._pending[count:]
        emitted = bytes(result)
        self._observed_bytes += len(emitted)
        self._digest.update(emitted)
        return emitted

    def tell(self) -> int:
        return self._observed_bytes

    def __len__(self) -> int:
        return self._expected_bytes

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def drain_and_verify(self) -> None:
        while self.read(_DEFAULT_WRITE_CHUNK_BYTES):
            pass
        self.require_consumed()

    def require_consumed(self) -> None:
        if self._observed_bytes != self._expected_bytes:
            raise StorageAdapterRejection(
                "integrity_failure",
                "opaque upload content ended before its declaration",
            )
        if not self._ended:
            self.read(1)
        if not self._ended:
            raise StorageAdapterRejection(
                "integrity_failure",
                "opaque upload content exceeds its declaration",
            )
        if self._expected_sha256 is not None and self._digest.hexdigest() != self._expected_sha256:
            raise StorageAdapterRejection(
                "integrity_failure",
                "opaque upload digest differs from its declaration",
            )


class _DirectS3ObjectReader:
    def read_object(
        self,
        *,
        client: Any,
        bucket: str,
        key: str,
        object_path: str,
        revision: str | None,
        offset: int | None,
        size: int | None,
        expected_bytes: int,
        chunk_bytes: int,
    ) -> ObjectReadStream:
        request: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if revision is not None:
            request["VersionId"] = revision
        if offset is not None and size == 0:
            response = cast(dict[str, Any], client.head_object(**request))
            if int(str(response.get("ContentLength", -1))) != expected_bytes:
                raise RuntimeError("S3 object response length differs from its request")
            return ObjectReadStream(
                receipt=ObjectReadReceipt(
                    object=ObjectLocator(
                        object_path=object_path,
                        revision=_provider_revision(response),
                    ),
                    total_bytes=expected_bytes,
                    offset=offset,
                    read_bytes=0,
                ),
                content=iter(()),
            )
        expected = expected_bytes
        if offset is not None and size is not None:
            if size > 0:
                request["Range"] = f"bytes={offset}-{offset + size - 1}"
            expected = size
        response = cast(dict[str, Any], client.get_object(**request))
        if int(str(response.get("ContentLength", -1))) != expected:
            raise RuntimeError("S3 object response length differs from its request")
        if offset is not None and size is not None and size > 0:
            expected_range = f"bytes {offset}-{offset + size - 1}/{expected_bytes}"
            if str(response.get("ContentRange", "")) != expected_range:
                raise RuntimeError("S3 object response range differs from its request")
        body = response["Body"]

        def close_body() -> None:
            close = getattr(body, "close", None)
            if callable(close):
                close()

        def content() -> Iterator[bytes]:
            try:
                yield from (
                    bytes(chunk) for chunk in body.iter_chunks(chunk_size=chunk_bytes) if chunk
                )
            finally:
                close_body()

        return ObjectReadStream(
            receipt=ObjectReadReceipt(
                object=ObjectLocator(
                    object_path=object_path,
                    revision=_provider_revision(response),
                ),
                total_bytes=expected_bytes,
                offset=offset or 0,
                read_bytes=expected,
            ),
            content=content(),
            close=close_body,
        )


def _normalized_metadata(head: dict[str, Any]) -> dict[str, str]:
    return {
        str(key).casefold(): str(value)
        for key, value in cast(dict[str, Any], head.get("Metadata") or {}).items()
    }


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _provider_revision(head: dict[str, Any]) -> str | None:
    value = head.get("VersionId")
    return str(value) if value is not None else None


def _provider_entity_token(head: dict[str, Any]) -> str | None:
    value = head.get("ETag")
    return str(value) if value is not None else None


def _provider_content_type(head: dict[str, Any]) -> str:
    value = str(head.get("ContentType") or "").strip()
    if not value:
        raise RuntimeError("S3 object is missing its content type")
    return value


def _provider_timestamp(head: dict[str, Any]) -> str:
    value = head.get("LastModified")
    return (
        format_utc_timestamp(value)
        if isinstance(value, datetime)
        else format_utc_timestamp(utc_now())
    )


def _client_error_identity(exc: ClientError) -> tuple[int, str]:
    return (
        int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)),
        str(exc.response.get("Error", {}).get("Code", "")),
    )


def _delete_exact_all_versions(client: Any, *, bucket: str, key: str) -> int:
    return _delete_prefix_all_versions(client, bucket=bucket, prefix=key, exact_key=key)


def _delete_prefix_all_versions(
    client: Any,
    *,
    bucket: str,
    prefix: str,
    exact_key: str | None = None,
) -> int:
    deleted: set[tuple[str, str | None]] = set()
    current = client.get_paginator("list_objects_v2")
    for page in current.paginate(Bucket=bucket, Prefix=prefix):
        objects = [
            {"Key": entry["Key"]}
            for entry in page.get("Contents", [])
            if exact_key is None or entry.get("Key") == exact_key
        ]
        if objects:
            client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
            deleted.update((str(item["Key"]), None) for item in objects)
    try:
        paginator = client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            versions = [
                {"Key": entry["Key"], "VersionId": entry["VersionId"]}
                for entry in [
                    *(page.get("Versions") or ()),
                    *(page.get("DeleteMarkers") or ()),
                ]
                if exact_key is None or entry.get("Key") == exact_key
            ]
            if versions:
                client.delete_objects(Bucket=bucket, Delete={"Objects": versions})
                deleted.update((str(item["Key"]), str(item["VersionId"])) for item in versions)
    except Exception as exc:
        if not _version_listing_unsupported(exc):
            raise
    return len(deleted)


def _version_listing_unsupported(exc: Exception) -> bool:
    if not isinstance(exc, ClientError):
        return False
    status, code = _client_error_identity(exc)
    return status in {400, 405, 501} or code in {
        "MethodNotAllowed",
        "NotImplemented",
        "UnsupportedOperation",
    }


__all__ = [
    "S3ObjectReader",
    "S3ReadPreparation",
    "S3StorageAdapter",
    "S3StorageAdapterConfig",
]
