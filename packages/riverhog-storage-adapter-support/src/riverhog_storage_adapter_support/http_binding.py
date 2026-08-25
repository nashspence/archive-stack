"""Framework-neutral HTTP binding for opaque storage capabilities."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TypeVar

from http_api_contracts import HttpErrorContract, HttpOperationContract, http_operation_for_request
from pydantic import BaseModel, ValidationError
from riverhog_storage_adapter_protocol import (
    AbortIncompleteUploadsRequest,
    AdapterDescriptor,
    CompletedObjectReceipt,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ImmutableObjectReceipt,
    MaintenanceResult,
    MultipartCompleteRequest,
    MultipartCreateRequest,
    MultipartHeadRequest,
    MultipartPartReceipt,
    MultipartPartWriteRequest,
    MultipartUpload,
    ObjectHeadRequest,
    ObjectMetadataReceipt,
    ObjectReadRequest,
    ReadPreparationRequest,
    ReadStatus,
    SmallObjectWriteRequest,
    StorageAdapterError,
    StorageAdapterErrorBody,
    StorageAdapterErrorCode,
    StorageAdapterPort,
    StorageAdapterRejection,
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
        if _ERROR_STATUS[code] != status:
            raise ValueError("storage adapter error code does not match its HTTP status")
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
        operation = http_operation_for_request(
            STORAGE_ADAPTER_HTTP_OPERATIONS,
            normalized_method,
            path,
        )
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
                part_request, content = self._parse_framed(body, MultipartPartWriteRequest)
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
                small_request, content = self._parse_framed(body, SmallObjectWriteRequest)
                if hashlib.sha256(content).hexdigest() != small_request.stored_sha256:
                    return _error(409, "integrity_failure", "small object digest differs")
                small_receipt = self.adapter.put_small_object(small_request, content)
                if small_receipt.object_path != small_request.object_path:
                    raise RuntimeError("adapter small-object receipt differs from its request")
                if small_request.mode == "replace_current" and (
                    small_receipt.stored_bytes != small_request.stored_bytes
                    or small_receipt.stored_sha256 != small_request.stored_sha256
                ):
                    raise RuntimeError("adapter replacement receipt differs from its request")
                return _model_response(small_receipt)
            if normalized_method == "POST" and path == "/v1/objects/head":
                metadata_receipt = self.adapter.head_object(self._parse(body, ObjectHeadRequest))
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
            if path in STORAGE_ADAPTER_HTTP_PATHS:
                return _error(405, "method_not_allowed", "adapter method is not allowed")
            return _error(404, "not_found", "adapter endpoint was not found")
        except StorageAdapterServiceError as exc:
            if operation is None or not operation.accepts_error(status=exc.status, code=exc.code):
                return _error(500, "internal_failure", "storage adapter operation failed")
            return _error(exc.status, exc.code, exc.message)
        except StorageAdapterRejection as exc:
            status = _ERROR_STATUS[exc.code]
            if operation is None or not operation.accepts_error(status=status, code=exc.code):
                return _error(500, "internal_failure", "storage adapter operation failed")
            return _error(status, exc.code, exc.message)
        except Exception:
            return _error(500, "internal_failure", "storage adapter operation failed")

    def _parse(self, body: bytes, model: type[ModelT]) -> ModelT:
        if len(body) > self.maximum_control_bytes:
            raise StorageAdapterServiceError(
                413,
                "request_too_large",
                "adapter control request exceeds its size limit",
            )
        try:
            return model.model_validate_json(body)
        except (ValidationError, ValueError) as exc:
            raise StorageAdapterServiceError(
                400,
                "invalid_request",
                "adapter request is invalid",
            ) from exc

    def _parse_framed(
        self,
        body: bytes,
        model: type[ModelT],
    ) -> tuple[ModelT, bytes]:
        try:
            return parse_framed_request(body, model)
        except (ValidationError, ValueError) as exc:
            raise StorageAdapterServiceError(
                400,
                "invalid_request",
                "adapter request is invalid",
            ) from exc


_ERROR_STATUS: dict[StorageAdapterErrorCode, int] = {
    "unauthorized": 401,
    "invalid_request": 400,
    "not_found": 404,
    "method_not_allowed": 405,
    "request_too_large": 413,
    "identity_conflict": 409,
    "invalid_path": 400,
    "invalid_range": 416,
    "read_not_ready": 409,
    "read_expired": 409,
    "integrity_failure": 409,
    "provider_unavailable": 503,
    "internal_failure": 500,
}
_COMMON_OPERATION_ERRORS: tuple[StorageAdapterErrorCode, ...] = (
    "unauthorized",
    "invalid_request",
    "provider_unavailable",
    "internal_failure",
)


def _adapter_errors(
    *additional: StorageAdapterErrorCode,
) -> tuple[HttpErrorContract, ...]:
    codes = dict.fromkeys((*_COMMON_OPERATION_ERRORS, *additional))
    return tuple(HttpErrorContract(code, _ERROR_STATUS[code]) for code in codes)


STORAGE_ADAPTER_HTTP_OPERATIONS = (
    HttpOperationContract(
        "GET",
        "/v1/adapter",
        response_type=AdapterDescriptor,
        errors=_adapter_errors(),
    ),
    HttpOperationContract(
        "POST",
        "/v1/multipart/create",
        MultipartCreateRequest,
        MultipartUpload,
        "json",
        errors=_adapter_errors("request_too_large", "invalid_path"),
    ),
    HttpOperationContract(
        "POST",
        "/v1/multipart/part",
        MultipartPartWriteRequest,
        MultipartPartReceipt,
        "framed",
        errors=_adapter_errors("request_too_large", "invalid_path", "not_found"),
    ),
    HttpOperationContract(
        "POST",
        "/v1/multipart/list",
        MultipartUpload,
        list[MultipartPartReceipt],
        "json",
        errors=_adapter_errors("request_too_large", "invalid_path", "not_found"),
    ),
    HttpOperationContract(
        "POST",
        "/v1/multipart/complete",
        MultipartCompleteRequest,
        CompletedObjectReceipt,
        "json",
        errors=_adapter_errors(
            "request_too_large",
            "invalid_path",
            "not_found",
            "identity_conflict",
            "integrity_failure",
        ),
    ),
    HttpOperationContract(
        "POST",
        "/v1/multipart/head",
        MultipartHeadRequest,
        CompletedObjectReceipt,
        "json",
        errors=_adapter_errors(
            "request_too_large",
            "invalid_path",
            "not_found",
            "identity_conflict",
            "integrity_failure",
        ),
    ),
    HttpOperationContract(
        "POST",
        "/v1/multipart/abort",
        MultipartUpload,
        None,
        "json",
        "none",
        (204,),
        _adapter_errors("request_too_large", "invalid_path", "not_found"),
    ),
    HttpOperationContract(
        "POST",
        "/v1/objects/put",
        SmallObjectWriteRequest,
        ImmutableObjectReceipt,
        "framed",
        errors=_adapter_errors(
            "request_too_large",
            "invalid_path",
            "identity_conflict",
            "integrity_failure",
        ),
    ),
    HttpOperationContract(
        "POST",
        "/v1/objects/head",
        ObjectHeadRequest,
        ObjectMetadataReceipt,
        "json",
        errors=_adapter_errors(
            "request_too_large",
            "invalid_path",
            "not_found",
            "identity_conflict",
            "integrity_failure",
        ),
    ),
    HttpOperationContract(
        "POST",
        "/v1/objects/read",
        ObjectReadRequest,
        None,
        "json",
        "binary",
        (200, 206),
        _adapter_errors(
            "request_too_large",
            "invalid_path",
            "not_found",
            "invalid_range",
            "read_not_ready",
            "read_expired",
            "integrity_failure",
        ),
    ),
    HttpOperationContract(
        "POST",
        "/v1/objects/delete",
        DeleteObjectRequest,
        None,
        "json",
        "none",
        (204,),
        _adapter_errors("request_too_large", "invalid_path", "not_found"),
    ),
    HttpOperationContract(
        "POST",
        "/v1/objects/delete-prefix",
        DeletePrefixRequest,
        MaintenanceResult,
        "json",
        errors=_adapter_errors("request_too_large", "invalid_path"),
    ),
    HttpOperationContract(
        "POST",
        "/v1/reads/prepare",
        ReadPreparationRequest,
        ReadStatus,
        "json",
        errors=_adapter_errors(
            "request_too_large",
            "invalid_path",
            "not_found",
            "read_not_ready",
            "read_expired",
        ),
    ),
    HttpOperationContract(
        "POST",
        "/v1/reads/status",
        ReadPreparationRequest,
        ReadStatus,
        "json",
        errors=_adapter_errors(
            "request_too_large",
            "invalid_path",
            "not_found",
            "read_not_ready",
            "read_expired",
        ),
    ),
    HttpOperationContract(
        "POST",
        "/v1/reads/cleanup",
        ReadPreparationRequest,
        None,
        "json",
        "none",
        (204,),
        _adapter_errors(
            "request_too_large",
            "invalid_path",
            "not_found",
            "read_not_ready",
            "read_expired",
        ),
    ),
    HttpOperationContract(
        "POST",
        "/v1/maintenance/abort-incomplete",
        AbortIncompleteUploadsRequest,
        MaintenanceResult,
        "json",
        errors=_adapter_errors("request_too_large", "invalid_path"),
    ),
)
STORAGE_ADAPTER_HTTP_PATHS = frozenset(
    operation.path for operation in STORAGE_ADAPTER_HTTP_OPERATIONS
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
    if _ERROR_STATUS[code] != status:
        raise ValueError("storage adapter HTTP binding emitted an undeclared code/status")
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
    "STORAGE_ADAPTER_HTTP_PATHS",
    "STORAGE_ADAPTER_HTTP_OPERATIONS",
    "StorageAdapterHttpBinding",
    "StorageAdapterHttpResponse",
    "StorageAdapterServiceError",
]
