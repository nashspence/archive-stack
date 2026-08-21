"""Persistent pooled HTTP client for one configured storage adapter."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import TypeVar

import httpx
from http_api_contracts import safe_http_base_url
from pydantic import BaseModel, TypeAdapter, ValidationError
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
    StorageAdapterErrorCode,
    StorageAdapterRejection,
)

from riverhog_storage_adapter_support.framing import framed_request

ModelT = TypeVar("ModelT", bound=BaseModel)
_PARTS = TypeAdapter(tuple[MultipartPartReceipt, ...])


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
        return self._model("GET", "/v1/adapter", AdapterDescriptor)

    def create_multipart_upload(self, request: MultipartCreateRequest) -> MultipartUpload:
        return self._model("POST", "/v1/multipart/create", MultipartUpload, request)

    def upload_part(
        self,
        *,
        upload: MultipartUpload,
        number: int,
        content: bytes,
    ) -> MultipartPartReceipt:
        request = MultipartPartWriteRequest(
            upload=upload,
            number=number,
            stored_bytes=len(content),
        )
        return self._model(
            "POST",
            "/v1/multipart/part",
            MultipartPartReceipt,
            content=framed_request(request, content),
        )

    def list_parts(self, upload: MultipartUpload) -> tuple[MultipartPartReceipt, ...]:
        response = self._request("POST", "/v1/multipart/list", payload=upload)
        self._require_success(response)
        try:
            return _PARTS.validate_json(response.content)
        except ValidationError as exc:
            raise StorageAdapterProtocolError("adapter returned invalid multipart parts") from exc

    def complete_multipart_upload(
        self,
        request: MultipartCompleteRequest,
    ) -> CompletedObjectReceipt:
        return self._model(
            "POST",
            "/v1/multipart/complete",
            CompletedObjectReceipt,
            request,
        )

    def head_completed_object(
        self,
        request: MultipartHeadRequest,
    ) -> CompletedObjectReceipt | None:
        return self._optional_model(
            "POST",
            "/v1/multipart/head",
            CompletedObjectReceipt,
            request,
        )

    def abort_multipart_upload(self, upload: MultipartUpload) -> None:
        self._empty("POST", "/v1/multipart/abort", upload)

    def put_small_object(
        self,
        request: SmallObjectWriteRequest,
        content: bytes,
    ) -> ImmutableObjectReceipt:
        return self._model(
            "POST",
            "/v1/objects/put",
            ImmutableObjectReceipt,
            content=framed_request(request, content),
        )

    def head_object(self, request: ObjectHeadRequest) -> ObjectMetadataReceipt | None:
        return self._optional_model(
            "POST",
            "/v1/objects/head",
            ObjectMetadataReceipt,
            request,
        )

    def iter_object(self, request: ObjectReadRequest) -> Iterator[bytes]:
        if request.size == 0:
            return
        expected = request.size if request.size is not None else request.expected_bytes
        expected_status = 206 if request.size is not None else 200
        with self._client.stream(
            "POST",
            f"{self.base_url}/v1/objects/read",
            headers=self._headers,
            json=request.model_dump(mode="json", exclude_none=True),
        ) as response:
            if response.status_code != expected_status:
                self._raise_response(response)
            raw_length = response.headers.get("Content-Length")
            if raw_length is None or int(raw_length) != expected:
                raise StorageAdapterProtocolError(
                    "adapter object response length differs from the request"
                )
            if (
                request.object.revision is not None
                and response.headers.get("X-Riverhog-Object-Revision") != request.object.revision
            ):
                raise StorageAdapterProtocolError("adapter object revision differs")
            if request.offset is not None and request.size is not None:
                end = request.offset + request.size - 1
                expected_range = f"bytes {request.offset}-{end}/{request.expected_bytes}"
                if response.headers.get("Content-Range") != expected_range:
                    raise StorageAdapterProtocolError(
                        "adapter object range differs from the request"
                    )
            emitted = 0
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                emitted += len(chunk)
                if emitted > expected:
                    raise StorageAdapterProtocolError(
                        "adapter object response contains trailing bytes"
                    )
                yield chunk
            if emitted != expected:
                raise StorageAdapterProtocolError(
                    "adapter object response ended before its declared length"
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
        return self._model("POST", "/v1/reads/prepare", ReadStatus, request)

    def read_status(self, request: ReadPreparationRequest) -> ReadStatus:
        return self._model("POST", "/v1/reads/status", ReadStatus, request)

    def cleanup_read(self, request: ReadPreparationRequest) -> None:
        self._empty("POST", "/v1/reads/cleanup", request)

    def abort_incomplete_uploads(self, request: AbortIncompleteUploadsRequest) -> int:
        return self._model(
            "POST",
            "/v1/maintenance/abort-incomplete",
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
    ) -> ModelT:
        response = self._request(method, path, payload=payload, content=content)
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
