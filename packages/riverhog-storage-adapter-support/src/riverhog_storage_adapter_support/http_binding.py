"""Framework-neutral HTTP binding for opaque storage capabilities."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TypeVar

from http_api_contracts import HttpErrorContract, HttpOperationContract, http_operation_for_request
from pydantic import BaseModel, ValidationError
from riverhog_storage_adapter_protocol import (
    AbortIncompleteWritesRequest,
    AdapterDescriptor,
    CompletedObjectReceipt,
    CompletedWriteLookupRequest,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ImmutableObjectReceipt,
    MaintenanceResult,
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
    WriteCompleteRequest,
    WriteSegmentReceipt,
    WriteSegmentRequest,
    WriteSegmentSet,
    WriteSession,
    WriteStartRequest,
    validate_completed_write_response,
    validate_object_metadata_response,
    validate_read_status_response,
    validate_small_object_response,
    validate_write_segment_response,
    validate_write_segment_set_response,
    validate_write_session_response,
)

from riverhog_storage_adapter_support.framing import (
    FramedContent,
    FramedRequestError,
    parse_framed_stream,
)

_JSON_CONTENT_TYPE = "application/json"
_BINARY_CONTENT_TYPE = "application/octet-stream"
# This is an operational parser bound, not a semantic object/member limit. It
# accommodates a large write-segment receipt set even when provider
# tokens are unusually large.
_DEFAULT_MAXIMUM_CONTROL_BYTES = 64 * 1024 * 1024

ModelT = TypeVar("ModelT", bound=BaseModel)
_LOGGER = logging.getLogger("riverhog.storage_adapter")


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
        if normalized_method == "POST" and path in FRAMED_STORAGE_ADAPTER_HTTP_PATHS:
            return self.handle_framed(
                normalized_method,
                path,
                (body,),
                content_length=len(body),
            )
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
            if normalized_method == "POST" and path == "/v1/writes/begin":
                start_request = self._parse(body, WriteStartRequest)
                session = self.adapter.begin_write(start_request)
                validate_write_session_response(start_request, session)
                return _model_response(session)
            if normalized_method == "POST" and path == "/v1/writes/segments":
                session = self._parse(body, WriteSession)
                segment_set = self.adapter.list_segments(session)
                validate_write_segment_set_response(
                    session,
                    segment_set,
                    self.adapter.descriptor(),
                )
                return _model_response(segment_set)
            if normalized_method == "POST" and path == "/v1/writes/complete":
                complete_request = self._parse(body, WriteCompleteRequest)
                descriptor = self.adapter.descriptor()
                if (
                    descriptor.maximum_segment_count is not None
                    and len(complete_request.segments) > descriptor.maximum_segment_count
                ):
                    raise StorageAdapterServiceError(
                        400,
                        "invalid_request",
                        "write completion exceeds the adapter segment-count limit",
                    )
                if any(
                    segment.stored_bytes > descriptor.maximum_segment_bytes
                    for segment in complete_request.segments
                    if descriptor.maximum_segment_bytes is not None
                ):
                    raise StorageAdapterServiceError(
                        400,
                        "invalid_request",
                        "write completion contains an oversized segment",
                    )
                if any(
                    segment.stored_bytes < descriptor.minimum_nonfinal_segment_bytes
                    for segment in complete_request.segments[:-1]
                ):
                    raise StorageAdapterServiceError(
                        400,
                        "invalid_request",
                        "write completion contains an undersized non-final segment",
                    )
                completed_receipt = self.adapter.complete_write(complete_request)
                validate_completed_write_response(complete_request, completed_receipt)
                return _model_response(completed_receipt)
            if normalized_method == "POST" and path == "/v1/writes/completed":
                lookup_request = self._parse(body, CompletedWriteLookupRequest)
                head_receipt = self.adapter.find_completed_write(lookup_request)
                if head_receipt is None:
                    return _error(404, "not_found", "completed object was not found")
                validate_completed_write_response(lookup_request, head_receipt)
                return _model_response(head_receipt)
            if normalized_method == "POST" and path == "/v1/writes/abort":
                self.adapter.abort_write(self._parse(body, WriteSession))
                return _empty_response()
            if normalized_method == "POST" and path == "/v1/objects/head":
                head_request = self._parse(body, ObjectHeadRequest)
                metadata_receipt = self.adapter.head_object(head_request)
                if metadata_receipt is None:
                    return _error(404, "not_found", "object was not found")
                validate_object_metadata_response(head_request, metadata_receipt)
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
                preparation_request = self._parse(body, ReadPreparationRequest)
                read_status = self.adapter.prepare_read(preparation_request)
                validate_read_status_response(preparation_request, read_status)
                return _model_response(read_status)
            if normalized_method == "POST" and path == "/v1/reads/status":
                preparation_request = self._parse(body, ReadPreparationRequest)
                read_status = self.adapter.read_status(preparation_request)
                validate_read_status_response(preparation_request, read_status)
                return _model_response(read_status)
            if normalized_method == "POST" and path == "/v1/reads/cleanup":
                self.adapter.cleanup_read(self._parse(body, ReadPreparationRequest))
                return _empty_response()
            if normalized_method == "POST" and path == "/v1/maintenance/abort-incomplete-writes":
                affected = self.adapter.abort_incomplete_writes(
                    self._parse(body, AbortIncompleteWritesRequest)
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
            _LOGGER.exception(
                "storage adapter capability failed",
                extra={"method": normalized_method, "path": path},
            )
            return _error(500, "internal_failure", "storage adapter operation failed")

    def handle_framed(
        self,
        method: str,
        path: str,
        chunks: Iterable[bytes],
        *,
        content_length: int | None,
    ) -> StorageAdapterHttpResponse:
        """Dispatch one authenticated single-pass declaration/payload request."""

        normalized_method = method.upper()
        operation = http_operation_for_request(
            STORAGE_ADAPTER_HTTP_OPERATIONS,
            normalized_method,
            path,
        )
        try:
            if content_length is None:
                raise StorageAdapterServiceError(
                    411,
                    "length_required",
                    "framed adapter requests require Content-Length",
                )
            if content_length < 0:
                raise StorageAdapterServiceError(
                    400,
                    "invalid_request",
                    "adapter request Content-Length is invalid",
                )
            if normalized_method == "POST" and path == "/v1/writes/segment":
                segment_request, content = self._parse_framed_stream(
                    chunks,
                    WriteSegmentRequest,
                    content_length=content_length,
                )
                descriptor = self.adapter.descriptor()
                if (
                    descriptor.maximum_segment_count is not None
                    and segment_request.number > descriptor.maximum_segment_count
                ):
                    raise StorageAdapterServiceError(
                        400,
                        "invalid_request",
                        "write segment number exceeds the adapter limit",
                    )
                if (
                    descriptor.maximum_segment_bytes is not None
                    and segment_request.stored_bytes > descriptor.maximum_segment_bytes
                ):
                    raise StorageAdapterServiceError(
                        413,
                        "request_too_large",
                        "write segment exceeds the adapter byte limit",
                    )
                segment_receipt = self.adapter.write_segment(
                    session=segment_request.session,
                    number=segment_request.number,
                    stored_bytes=segment_request.stored_bytes,
                    content=content,
                )
                content.require_consumed()
                validate_write_segment_response(segment_request, segment_receipt)
                return _model_response(segment_receipt)
            if normalized_method == "POST" and path == "/v1/objects/put":
                small_request, content = self._parse_framed_stream(
                    chunks,
                    SmallObjectWriteRequest,
                    content_length=content_length,
                )
                small_receipt = self.adapter.put_small_object(small_request, content)
                content.require_consumed()
                validate_small_object_response(small_request, small_receipt)
                return _model_response(small_receipt)
            if path in STORAGE_ADAPTER_HTTP_PATHS:
                return _error(405, "method_not_allowed", "adapter method is not allowed")
            return _error(404, "not_found", "adapter endpoint was not found")
        except StorageAdapterServiceError as exc:
            if operation is None or not operation.accepts_error(
                status=exc.status,
                code=exc.code,
            ):
                return _error(500, "internal_failure", "storage adapter operation failed")
            return _error(exc.status, exc.code, exc.message)
        except FramedRequestError:
            if operation is None or not operation.accepts_error(
                status=400,
                code="invalid_request",
            ):
                return _error(500, "internal_failure", "storage adapter operation failed")
            return _error(400, "invalid_request", "adapter request is invalid")
        except StorageAdapterRejection as exc:
            status = _ERROR_STATUS[exc.code]
            if operation is None or not operation.accepts_error(status=status, code=exc.code):
                return _error(500, "internal_failure", "storage adapter operation failed")
            return _error(status, exc.code, exc.message)
        except Exception:
            _LOGGER.exception(
                "storage adapter capability failed",
                extra={"method": normalized_method, "path": path},
            )
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

    def _parse_framed_stream(
        self,
        chunks: Iterable[bytes],
        model: type[ModelT],
        *,
        content_length: int,
    ) -> tuple[ModelT, FramedContent]:
        try:
            return parse_framed_stream(
                chunks,
                model,
                content_length=content_length,
            )
        except (TypeError, ValidationError, ValueError) as exc:
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
    "length_required": 411,
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
        "/v1/writes/begin",
        WriteStartRequest,
        WriteSession,
        "json",
        errors=_adapter_errors("request_too_large", "invalid_path"),
    ),
    HttpOperationContract(
        "POST",
        "/v1/writes/segment",
        WriteSegmentRequest,
        WriteSegmentReceipt,
        "framed",
        errors=_adapter_errors("length_required", "request_too_large", "invalid_path", "not_found"),
    ),
    HttpOperationContract(
        "POST",
        "/v1/writes/segments",
        WriteSession,
        WriteSegmentSet,
        "json",
        errors=_adapter_errors("request_too_large", "invalid_path", "not_found"),
    ),
    HttpOperationContract(
        "POST",
        "/v1/writes/complete",
        WriteCompleteRequest,
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
        "/v1/writes/completed",
        CompletedWriteLookupRequest,
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
        "/v1/writes/abort",
        WriteSession,
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
            "length_required",
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
        "/v1/maintenance/abort-incomplete-writes",
        AbortIncompleteWritesRequest,
        MaintenanceResult,
        "json",
        errors=_adapter_errors("request_too_large", "invalid_path"),
    ),
)
STORAGE_ADAPTER_HTTP_PATHS = frozenset(
    operation.path for operation in STORAGE_ADAPTER_HTTP_OPERATIONS
)
FRAMED_STORAGE_ADAPTER_HTTP_PATHS = frozenset(
    operation.path
    for operation in STORAGE_ADAPTER_HTTP_OPERATIONS
    if operation.request_kind == "framed"
)


def _model_response(model: BaseModel) -> StorageAdapterHttpResponse:
    return StorageAdapterHttpResponse(
        status=200,
        headers=(("Content-Type", _JSON_CONTENT_TYPE),),
        body=model.model_dump_json(exclude_none=True).encode("utf-8"),
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
    "FRAMED_STORAGE_ADAPTER_HTTP_PATHS",
    "StorageAdapterHttpBinding",
    "StorageAdapterHttpResponse",
    "StorageAdapterServiceError",
]
