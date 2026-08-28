"""Persistent pooled HTTP client for one configured storage adapter."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import TypeVar

import httpx
from http_api_contracts import safe_http_base_url
from pydantic import BaseModel, ValidationError
from riverhog_storage_adapter_protocol import (
    AbortIncompleteWritesRequest,
    AdapterDescriptor,
    BinaryContent,
    CompletedObjectReceipt,
    CompletedWriteLookupRequest,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ImmutableObjectReceipt,
    MaintenanceResult,
    ObjectHeadRequest,
    ObjectMetadataReceipt,
    ObjectReadReceipt,
    ObjectReadRequest,
    ObjectReadStream,
    ReadPreparationRequest,
    ReadStatus,
    SmallObjectWriteRequest,
    StorageAdapterError,
    StorageAdapterErrorCode,
    StorageAdapterRejection,
    WriteCompleteRequest,
    WriteSegmentReceipt,
    WriteSegmentRequest,
    WriteSegmentSet,
    WriteSession,
    WriteStartRequest,
    validate_completed_write_response,
    validate_object_metadata_response,
    validate_object_read_response,
    validate_read_status_response,
    validate_small_object_response,
    validate_write_completion_request,
    validate_write_segment_request,
    validate_write_segment_response,
    validate_write_segment_set_response,
    validate_write_session_response,
)

from riverhog_storage_adapter_support.framing import (
    FRAMED_BODY_MEDIA_TYPE,
    framed_body,
    framed_body_length,
    parse_framed_stream,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class StorageAdapterProtocolError(StorageAdapterRejection):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: StorageAdapterErrorCode = "internal_failure",
    ) -> None:
        RuntimeError.__init__(self, message)
        self.status_code = status_code
        self.code = code
        self.message = message


class StorageAdapterClient:
    """One registration-scoped client implementing the transport-neutral port."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        allow_insecure_http: bool = False,
        timeout: float | httpx.Timeout | None = 300.0,
        maximum_connections: int = 32,
        client: httpx.Client | None = None,
    ) -> None:
        if maximum_connections < 1:
            raise ValueError("storage adapter maximum connections must be positive")
        credential = token.strip()
        if not credential:
            raise ValueError("storage adapter bearer token must be nonempty")
        self.base_url = safe_http_base_url(
            base_url,
            setting="storage adapter base URL",
            allow_insecure_http=allow_insecure_http,
        )
        self._headers = {"Authorization": f"Bearer {credential}"}
        self._descriptor: AdapterDescriptor | None = None
        self._owns_client = client is None
        self._client = client or httpx.Client(
            http2=True,
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=maximum_connections,
                max_keepalive_connections=maximum_connections,
            ),
        )

    @classmethod
    def from_token_file(
        cls,
        base_url: str,
        *,
        token_file: Path,
        allow_insecure_http: bool = False,
        timeout: float | httpx.Timeout | None = 300.0,
        maximum_connections: int = 32,
        client: httpx.Client | None = None,
    ) -> StorageAdapterClient:
        return cls(
            base_url,
            token=token_file.read_text(encoding="utf-8"),
            allow_insecure_http=allow_insecure_http,
            timeout=timeout,
            maximum_connections=maximum_connections,
            client=client,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def descriptor(self) -> AdapterDescriptor:
        if self._descriptor is None:
            self._descriptor = self._model("GET", "/v1/adapter", AdapterDescriptor)
        return self._descriptor

    def check_readiness(self) -> None:
        self._require_success(self._request("GET", "/health/ready"))

    def begin_write(self, request: WriteStartRequest) -> WriteSession:
        response = self._model("POST", "/v1/writes/begin", WriteSession, request)
        self._validate(validate_write_session_response, request, response)
        return response

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        stored_bytes: int,
        content: BinaryContent,
    ) -> WriteSegmentReceipt:
        request = WriteSegmentRequest(
            session=session,
            number=number,
            stored_bytes=stored_bytes,
        )
        self._validate_request(validate_write_segment_request, request, self.descriptor())
        response = self._model(
            "POST",
            "/v1/writes/segment",
            WriteSegmentReceipt,
            content=framed_body(request, content),
            headers={
                "Content-Length": str(framed_body_length(request)),
                "Content-Type": FRAMED_BODY_MEDIA_TYPE,
            },
        )
        self._validate(validate_write_segment_response, request, response)
        return response

    def list_segments(self, session: WriteSession) -> WriteSegmentSet:
        response = self._model("POST", "/v1/writes/segments", WriteSegmentSet, session)
        self._validate(
            validate_write_segment_set_response,
            session,
            response,
            self.descriptor(),
        )
        return response

    def complete_write(
        self,
        request: WriteCompleteRequest,
    ) -> CompletedObjectReceipt:
        self._validate_request(validate_write_completion_request, request, self.descriptor())
        response = self._model(
            "POST",
            "/v1/writes/complete",
            CompletedObjectReceipt,
            request,
        )
        self._validate(validate_completed_write_response, request, response)
        return response

    def find_completed_write(
        self,
        request: CompletedWriteLookupRequest,
    ) -> CompletedObjectReceipt | None:
        response = self._optional_model(
            "POST",
            "/v1/writes/completed",
            CompletedObjectReceipt,
            request,
        )
        if response is not None:
            self._validate(validate_completed_write_response, request, response)
        return response

    def abort_write(self, session: WriteSession) -> None:
        self._empty("POST", "/v1/writes/abort", session)

    def put_small_object(
        self,
        request: SmallObjectWriteRequest,
        content: BinaryContent,
    ) -> ImmutableObjectReceipt:
        response = self._model(
            "POST",
            "/v1/objects/put",
            ImmutableObjectReceipt,
            content=framed_body(request, content),
            headers={
                "Content-Length": str(framed_body_length(request)),
                "Content-Type": FRAMED_BODY_MEDIA_TYPE,
            },
        )
        self._validate(validate_small_object_response, request, response)
        return response

    def head_object(self, request: ObjectHeadRequest) -> ObjectMetadataReceipt | None:
        response = self._optional_model(
            "POST",
            "/v1/objects/head",
            ObjectMetadataReceipt,
            request,
        )
        if response is not None:
            self._validate(validate_object_metadata_response, request, response)
        return response

    def read_object(self, request: ObjectReadRequest) -> ObjectReadStream:
        expected_status = 206 if request.size is not None and request.size > 0 else 200
        context = self._client.stream(
            "POST",
            f"{self.base_url}/v1/objects/read",
            headers=self._headers,
            json=request.model_dump(mode="json", exclude_none=True),
        )
        response = context.__enter__()
        try:
            if response.status_code != expected_status:
                self._raise_response(response)
            raw_length = response.headers.get("Content-Length")
            if raw_length is None:
                raise StorageAdapterProtocolError("adapter object response has no framed length")
            receipt, content = parse_framed_stream(
                response.iter_bytes(),
                ObjectReadReceipt,
                content_length=int(raw_length),
            )
            self._validate(validate_object_read_response, request, receipt)
            if (
                receipt.object.revision is not None
                and response.headers.get("X-Riverhog-Object-Revision") != receipt.object.revision
            ):
                raise StorageAdapterProtocolError("adapter object revision differs")
            if response.headers.get("X-Riverhog-Object-Bytes") != str(receipt.total_bytes):
                raise StorageAdapterProtocolError("adapter object byte identity differs")
            if receipt.read_bytes > 0 and request.offset is not None:
                end = receipt.offset + receipt.read_bytes - 1
                expected_range = f"bytes {receipt.offset}-{end}/{receipt.total_bytes}"
                if response.headers.get("Content-Range") != expected_range:
                    raise StorageAdapterProtocolError(
                        "adapter object range differs from the request"
                    )
        except BaseException:
            context.__exit__(None, None, None)
            raise

        closed = False

        def close_response() -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            context.__exit__(None, None, None)

        def iter_content() -> Iterator[bytes]:
            try:
                yield from content
                content.require_consumed()
            finally:
                close_response()

        return ObjectReadStream(
            receipt=receipt,
            content=iter_content(),
            close=close_response,
        )

    def delete_object(self, request: DeleteObjectRequest) -> None:
        self._empty("POST", "/v1/objects/delete", request)

    def delete_prefix(self, request: DeletePrefixRequest) -> int:
        return self._model(
            "POST",
            "/v1/objects/delete-prefix",
            MaintenanceResult,
            request,
        ).affected

    def prepare_read(self, request: ReadPreparationRequest) -> ReadStatus:
        response = self._model("POST", "/v1/reads/prepare", ReadStatus, request)
        self._validate(validate_read_status_response, request, response)
        return response

    def read_status(self, request: ReadPreparationRequest) -> ReadStatus:
        response = self._model("POST", "/v1/reads/status", ReadStatus, request)
        self._validate(validate_read_status_response, request, response)
        return response

    def cleanup_read(self, request: ReadPreparationRequest) -> None:
        self._empty("POST", "/v1/reads/cleanup", request)

    def abort_incomplete_writes(self, request: AbortIncompleteWritesRequest) -> int:
        return self._model(
            "POST",
            "/v1/maintenance/abort-incomplete-writes",
            MaintenanceResult,
            request,
        ).affected

    def _model(
        self,
        method: str,
        path: str,
        model: type[ModelT],
        payload: BaseModel | None = None,
        *,
        content: Iterable[bytes] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> ModelT:
        response = self._request(
            method,
            path,
            payload=payload,
            content=content,
            headers=headers,
        )
        self._require_success(response)
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            raise StorageAdapterProtocolError(
                f"adapter returned an invalid {model.__name__}"
            ) from exc

    def _optional_model(
        self,
        method: str,
        path: str,
        model: type[ModelT],
        payload: BaseModel,
    ) -> ModelT | None:
        response = self._request(method, path, payload=payload)
        if response.status_code == 404:
            return None
        self._require_success(response)
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            raise StorageAdapterProtocolError(
                f"adapter returned an invalid {model.__name__}"
            ) from exc

    def _empty(self, method: str, path: str, payload: BaseModel) -> None:
        response = self._request(method, path, payload=payload)
        if response.status_code != 204:
            self._raise_response(response)

    @staticmethod
    def _validate(
        validator: Callable[..., None],
        *arguments: object,
    ) -> None:
        try:
            validator(*arguments)
        except ValueError as exc:
            raise StorageAdapterProtocolError(
                "adapter returned a response inconsistent with its request"
            ) from exc

    @staticmethod
    def _validate_request(
        validator: Callable[..., None],
        *arguments: object,
    ) -> None:
        try:
            validator(*arguments)
        except ValueError as exc:
            raise StorageAdapterProtocolError(str(exc), code="invalid_request") from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: BaseModel | None = None,
        content: Iterable[bytes] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return self._client.request(
                method,
                f"{self.base_url}{path}",
                headers={**self._headers, **dict(headers or {})},
                json=(
                    payload.model_dump(mode="json", exclude_none=True)
                    if payload is not None
                    else None
                ),
                content=content,
            )
        except httpx.HTTPError as exc:
            raise StorageAdapterProtocolError(f"storage adapter request failed: {exc}") from exc

    def _require_success(self, response: httpx.Response) -> None:
        if not response.is_success:
            self._raise_response(response)

    @staticmethod
    def _raise_response(response: httpx.Response) -> None:
        try:
            error = StorageAdapterError.model_validate_json(response.content).error
        except ValidationError:
            raise StorageAdapterProtocolError(
                f"storage adapter returned HTTP {response.status_code}",
                status_code=response.status_code,
            ) from None
        raise StorageAdapterProtocolError(
            error.message,
            status_code=response.status_code,
            code=error.code,
        )


__all__ = ["StorageAdapterClient", "StorageAdapterProtocolError"]
