"""Framework-neutral HTTP binding for opaque storage capabilities."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from riverhog_storage_adapter_protocol import (
    AbortIncompleteUploadsRequest,
    DeleteObjectRequest,
    DeletePrefixRequest,
    MaintenanceResult,
    MultipartCompleteRequest,
    MultipartCreateRequest,
    MultipartHeadRequest,
    MultipartPartWriteRequest,
    MultipartUpload,
    ObjectLocator,
    ObjectReadRequest,
    ReadPreparationRequest,
    SmallObjectWriteRequest,
    StorageAdapterError,
    StorageAdapterErrorBody,
    StorageAdapterErrorCode,
    StorageAdapterPort,
)

from riverhog_storage_adapter_support.framing import parse_framed_request

_JSON_CONTENT_TYPE = "application/json"
_BINARY_CONTENT_TYPE = "application/octet-stream"
# This is an operational parser bound, not a semantic object/member limit. It
# accommodates the protocol's maximum multipart receipt set even when provider
# tokens are unusually large.
_DEFAULT_MAXIMUM_CONTROL_BYTES = 64 * 1024 * 1024

ModelT = TypeVar("ModelT", bound=BaseModel)


class StorageAdapterServiceError(RuntimeError):
    """Expected adapter rejection rendered through the closed error contract."""

    def __init__(
        self,
        status: int,
        code: StorageAdapterErrorCode,
        message: str,
    ) -> None:
        super().__init__(message)
        if status < 400 or status > 599:
            raise ValueError("storage adapter error status must be 4xx or 5xx")
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class StorageAdapterHttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes | Iterator[bytes]


class StorageAdapterHttpBinding:
    """Translate the fixed v1 HTTP surface into one adapter capability port."""

    def __init__(
        self,
        adapter: StorageAdapterPort,
        *,
        maximum_control_bytes: int = _DEFAULT_MAXIMUM_CONTROL_BYTES,
    ) -> None:
        if maximum_control_bytes < 1:
            raise ValueError("storage adapter control request limit must be positive")
        self.adapter = adapter
        self.maximum_control_bytes = maximum_control_bytes

    def handle(
        self,
        method: str,
        path: str,
        body: bytes = b"",
    ) -> StorageAdapterHttpResponse:
        normalized_method = method.upper()
        try:
            if normalized_method == "GET" and path == "/v1/adapter":
                if body:
                    raise StorageAdapterServiceError(
                        400,
                        "invalid_request",
                        "adapter descriptor request must not contain a body",
                    )
                return _model_response(self.adapter.descriptor())
            if normalized_method == "POST" and path == "/v1/multipart/create":
                return _model_response(
                    self.adapter.create_multipart_upload(self._parse(body, MultipartCreateRequest))
                )
            if normalized_method == "POST" and path == "/v1/multipart/part":
                part_request, content = parse_framed_request(body, MultipartPartWriteRequest)
                descriptor = self.adapter.descriptor()
                if part_request.number > descriptor.maximum_part_count:
                    raise StorageAdapterServiceError(
                        400,
                        "invalid_request",
                        "multipart part number exceeds the adapter limit",
                    )
                if len(content) > descriptor.maximum_part_bytes:
                    raise StorageAdapterServiceError(
                        413,
                        "request_too_large",
                        "multipart part exceeds the adapter byte limit",
                    )
                part_receipt = self.adapter.upload_part(
                    upload=part_request.upload,
                    number=part_request.number,
                    content=content,
                )
                if part_receipt.number != part_request.number or part_receipt.stored_bytes != len(
                    content
                ):
                    raise RuntimeError("adapter part receipt differs from its request")
                return _model_response(part_receipt)
            if normalized_method == "POST" and path == "/v1/multipart/list":
                return _models_response(self.adapter.list_parts(self._parse(body, MultipartUpload)))
            if normalized_method == "POST" and path == "/v1/multipart/complete":
                complete_request = self._parse(body, MultipartCompleteRequest)
                descriptor = self.adapter.descriptor()
                if len(complete_request.parts) > descriptor.maximum_part_count:
                    raise StorageAdapterServiceError(
                        400,
                        "invalid_request",
                        "multipart completion exceeds the adapter part-count limit",
                    )
                if any(
                    part.stored_bytes > descriptor.maximum_part_bytes
                    for part in complete_request.parts
                ):
                    raise StorageAdapterServiceError(
                        400,
                        "invalid_request",
                        "multipart completion contains an oversized part",
                    )
                if any(
                    part.stored_bytes < descriptor.minimum_nonfinal_part_bytes
                    for part in complete_request.parts[:-1]
                ):
                    raise StorageAdapterServiceError(
                        400,
                        "invalid_request",
                        "multipart completion contains an undersized non-final part",
                    )
                completed_receipt = self.adapter.complete_multipart_upload(complete_request)
                if (
                    completed_receipt.object_path != complete_request.upload.object_path
                    or completed_receipt.stored_bytes != complete_request.expected_bytes
                ):
                    raise RuntimeError("adapter completion receipt differs from its request")
                return _model_response(completed_receipt)
            if normalized_method == "POST" and path == "/v1/multipart/head":
                head_receipt = self.adapter.head_completed_object(
                    self._parse(body, MultipartHeadRequest)
                )
                if head_receipt is None:
                    return _error(404, "not_found", "completed object was not found")
                return _model_response(head_receipt)
            if normalized_method == "POST" and path == "/v1/multipart/abort":
                self.adapter.abort_multipart_upload(self._parse(body, MultipartUpload))
                return _empty_response()
            if normalized_method == "POST" and path == "/v1/objects/put":
                small_request, content = parse_framed_request(body, SmallObjectWriteRequest)
                if hashlib.sha256(content).hexdigest() != small_request.stored_sha256:
                    return _error(400, "integrity_failure", "small object digest differs")
                small_receipt = self.adapter.put_small_object(small_request, content)
                if (
                    small_receipt.object_path != small_request.object_path
                    or small_receipt.stored_bytes != small_request.stored_bytes
                    or small_receipt.stored_sha256 != small_request.stored_sha256
                ):
                    raise RuntimeError("adapter small-object receipt differs from its request")
                return _model_response(small_receipt)
            if normalized_method == "POST" and path == "/v1/objects/head":
                metadata_receipt = self.adapter.head_object(self._parse(body, ObjectLocator))
                if metadata_receipt is None:
                    return _error(404, "not_found", "object was not found")
                return _model_response(metadata_receipt)
            if normalized_method == "POST" and path == "/v1/objects/read":
                read_request = self._parse(body, ObjectReadRequest)
                expected = (
                    read_request.size
                    if read_request.size is not None
                    else read_request.expected_bytes
                )
                headers: list[tuple[str, str]] = [
                    ("Content-Type", _BINARY_CONTENT_TYPE),
                    ("Content-Length", str(expected)),
                ]
                if read_request.object.revision is not None:
                    headers.append(("X-Riverhog-Object-Revision", read_request.object.revision))
                status = 200
                if (
                    read_request.offset is not None
                    and read_request.size is not None
                    and read_request.size > 0
                ):
                    status = 206
                    end = read_request.offset + read_request.size - 1
                    headers.append(
                        (
                            "Content-Range",
                            f"bytes {read_request.offset}-{end}/{read_request.expected_bytes}",
                        )
                    )
                return StorageAdapterHttpResponse(
                    status=status,
                    headers=tuple(headers),
                    body=_validated_stream(self.adapter.iter_object(read_request), expected),
                )
            if normalized_method == "POST" and path == "/v1/objects/delete":
                self.adapter.delete_object(self._parse(body, DeleteObjectRequest))
                return _empty_response()
            if normalized_method == "POST" and path == "/v1/objects/delete-prefix":
                affected = self.adapter.delete_prefix(self._parse(body, DeletePrefixRequest))
                return _model_response(MaintenanceResult(affected=affected))
            if normalized_method == "POST" and path == "/v1/reads/prepare":
                return _model_response(
                    self.adapter.prepare_read(self._parse(body, ReadPreparationRequest))
                )
            if normalized_method == "POST" and path == "/v1/reads/status":
                return _model_response(
                    self.adapter.read_status(self._parse(body, ReadPreparationRequest))
                )
            if normalized_method == "POST" and path == "/v1/reads/cleanup":
                self.adapter.cleanup_read(self._parse(body, ReadPreparationRequest))
                return _empty_response()
            if normalized_method == "POST" and path == "/v1/maintenance/abort-incomplete":
                affected = self.adapter.abort_incomplete_uploads(
                    self._parse(body, AbortIncompleteUploadsRequest)
                )
                return _model_response(MaintenanceResult(affected=affected))
            if path in _HTTP_PATHS:
                return _error(405, "method_not_allowed", "adapter method is not allowed")
            return _error(404, "not_found", "adapter endpoint was not found")
        except StorageAdapterServiceError as exc:
            return _error(exc.status, exc.code, exc.message)
        except (ValidationError, ValueError):
            return _error(400, "invalid_request", "adapter request is invalid")
        except Exception:
            return _error(500, "internal_failure", "storage adapter operation failed")

    def _parse(self, body: bytes, model: type[ModelT]) -> ModelT:
        if len(body) > self.maximum_control_bytes:
            raise StorageAdapterServiceError(
                413,
                "request_too_large",
                "adapter control request exceeds its size limit",
            )
        return model.model_validate_json(body)


_HTTP_PATHS = frozenset(
    {
        "/v1/adapter",
        "/v1/multipart/create",
        "/v1/multipart/part",
        "/v1/multipart/list",
        "/v1/multipart/complete",
        "/v1/multipart/head",
        "/v1/multipart/abort",
        "/v1/objects/put",
        "/v1/objects/head",
        "/v1/objects/read",
        "/v1/objects/delete",
        "/v1/objects/delete-prefix",
        "/v1/reads/prepare",
        "/v1/reads/status",
        "/v1/reads/cleanup",
        "/v1/maintenance/abort-incomplete",
    }
)


def _model_response(model: BaseModel) -> StorageAdapterHttpResponse:
    return StorageAdapterHttpResponse(
        status=200,
        headers=(("Content-Type", _JSON_CONTENT_TYPE),),
        body=model.model_dump_json(exclude_none=True).encode("utf-8"),
    )


def _models_response(models: tuple[BaseModel, ...]) -> StorageAdapterHttpResponse:
    body = "[" + ",".join(model.model_dump_json(exclude_none=True) for model in models) + "]"
    return StorageAdapterHttpResponse(
        status=200,
        headers=(("Content-Type", _JSON_CONTENT_TYPE),),
        body=body.encode("utf-8"),
    )


def _empty_response() -> StorageAdapterHttpResponse:
    return StorageAdapterHttpResponse(status=204, headers=(), body=b"")


def _error(
    status: int,
    code: StorageAdapterErrorCode,
    message: str,
) -> StorageAdapterHttpResponse:
    payload = StorageAdapterError(error=StorageAdapterErrorBody(code=code, message=message))
    return StorageAdapterHttpResponse(
        status=status,
        headers=(("Content-Type", _JSON_CONTENT_TYPE),),
        body=payload.model_dump_json().encode("utf-8"),
    )


def _validated_stream(content: Iterator[bytes], expected_bytes: int) -> Iterator[bytes]:
    emitted = 0
    for chunk in content:
        if not chunk:
            continue
        emitted += len(chunk)
        if emitted > expected_bytes:
            raise RuntimeError("adapter object stream exceeds its declared length")
        yield chunk
    if emitted != expected_bytes:
        raise RuntimeError("adapter object stream ended before its declared length")


__all__ = [
    "StorageAdapterHttpBinding",
    "StorageAdapterHttpResponse",
    "StorageAdapterServiceError",
]
