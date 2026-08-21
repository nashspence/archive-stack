"""Strict synchronous Riverhog client for one configured storage adapter."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError
from riverhog_storage_adapter_protocol import (
    AbortIncompleteUploadsRequest,
    CompleteUploadRequest,
    MaintenanceResult,
    ObjectDeleteRequest,
    ObjectLocator,
    ObjectReceipt,
    PrefixDeleteRequest,
    ReadRequest,
    ReadStatus,
    StorageAdapterDescriptor,
    StorageAdapterError,
    UploadDeclaration,
    UploadPartReceipt,
    UploadStatus,
)

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class StorageAdapterProtocolError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class StorageAdapterClient:
    """One registration-scoped, provider-agnostic adapter client."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        allow_insecure_http: bool = False,
        timeout: httpx.Timeout | float = 300.0,
        max_connections: int = 32,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("storage-adapter endpoint must be an HTTP(S) URL")
        if parsed.scheme == "http" and not allow_insecure_http:
            raise ValueError(
                "plain HTTP storage-adapter transport requires explicit insecure opt-in"
            )
        credential = token.strip()
        if not credential:
            raise ValueError("storage-adapter bearer token must not be empty")
        if max_connections < 1:
            raise ValueError("storage-adapter max connections must be positive")
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            http2=True,
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_connections,
            ),
        )
        self._headers = {"Authorization": f"Bearer {credential}"}

    @classmethod
    def from_token_file(
        cls,
        base_url: str,
        *,
        token_file: Path,
        allow_insecure_http: bool = False,
        timeout: httpx.Timeout | float = 300.0,
        max_connections: int = 32,
        client: httpx.Client | None = None,
    ) -> StorageAdapterClient:
        return cls(
            base_url,
            token=token_file.read_text(encoding="utf-8"),
            allow_insecure_http=allow_insecure_http,
            timeout=timeout,
            max_connections=max_connections,
            client=client,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def descriptor(self) -> StorageAdapterDescriptor:
        return self._model("GET", "/v1/adapter", StorageAdapterDescriptor)

    def put_upload(self, declaration: UploadDeclaration) -> UploadStatus:
        return self._model(
            "PUT",
            f"/v1/uploads/{declaration.transfer_id}",
            UploadStatus,
            json=declaration.model_dump(mode="json"),
        )

    def get_upload(self, transfer_id: str) -> UploadStatus:
        return self._model("GET", f"/v1/uploads/{transfer_id}", UploadStatus)

    def put_part(
        self,
        *,
        transfer_id: str,
        number: int,
        content: bytes,
    ) -> UploadPartReceipt:
        digest = hashlib.sha256(content).hexdigest()
        return self._model(
            "PUT",
            f"/v1/uploads/{transfer_id}/parts/{number}",
            UploadPartReceipt,
            content=content,
            headers={
                "Content-Length": str(len(content)),
                "Content-Type": "application/octet-stream",
                "X-Riverhog-Stored-Sha256": digest,
            },
        )

    def complete_upload(
        self,
        *,
        transfer_id: str,
        completion: CompleteUploadRequest,
    ) -> ObjectReceipt:
        return self._model(
            "POST",
            f"/v1/uploads/{transfer_id}/complete",
            ObjectReceipt,
            json=completion.model_dump(mode="json"),
        )

    def delete_upload(self, transfer_id: str) -> UploadStatus:
        return self._model("DELETE", f"/v1/uploads/{transfer_id}", UploadStatus)

    def object_metadata(self, locator: ObjectLocator) -> ObjectReceipt:
        return self._model(
            "GET",
            "/v1/objects/metadata",
            ObjectReceipt,
            params={"path": locator.object_path, "revision": locator.revision},
        )

    def iter_object_content(
        self,
        locator: ObjectLocator,
        *,
        offset: int | None = None,
        size: int | None = None,
        chunk_bytes: int = 8 * 1024 * 1024,
    ) -> Iterator[bytes]:
        headers: dict[str, str] = {}
        expected_status = 200
        if (offset is None) != (size is None):
            raise ValueError("object range requires both offset and size")
        if offset is not None and size is not None:
            if offset < 0 or size < 0:
                raise ValueError("object range must be nonnegative")
            if size == 0:
                return
            headers["Range"] = f"bytes={offset}-{offset + size - 1}"
            expected_status = 206
        with self._client.stream(
            "GET",
            self.base_url + "/v1/objects/content",
            headers={**self._headers, **headers},
            params={"path": locator.object_path, "revision": locator.revision},
        ) as response:
            if response.status_code != expected_status:
                self._raise_response(response)
            if response.headers.get("X-Riverhog-Object-Revision") != locator.revision:
                raise StorageAdapterProtocolError("adapter content revision changed")
            expected_length = size
            if expected_length is None:
                raw_length = response.headers.get("Content-Length")
                expected_length = int(raw_length) if raw_length is not None else None
            emitted = 0
            digest = hashlib.sha256()
            for chunk in response.iter_bytes(chunk_size=chunk_bytes):
                emitted += len(chunk)
                digest.update(chunk)
                yield chunk
            if expected_length is not None and emitted != expected_length:
                raise StorageAdapterProtocolError(
                    "adapter content length differs from its response"
                )
            if offset is None:
                expected_digest = response.headers.get("X-Riverhog-Stored-Sha256")
                if expected_digest is None or digest.hexdigest() != expected_digest:
                    raise StorageAdapterProtocolError(
                        "adapter full-object content digest differs from its response"
                    )

    def delete_object(self, locator: ObjectLocator) -> None:
        response = self._request(
            "DELETE",
            "/v1/objects",
            json=ObjectDeleteRequest(object=locator).model_dump(mode="json"),
        )
        if response.status_code != 204:
            self._raise_response(response)

    def delete_prefix(self, object_prefix: str) -> MaintenanceResult:
        return self._model(
            "POST",
            "/v1/object-prefixes/delete",
            MaintenanceResult,
            json=PrefixDeleteRequest(object_prefix=object_prefix).model_dump(mode="json"),
        )

    def prepare_read(self, request: ReadRequest) -> ReadStatus:
        return self._model(
            "POST",
            "/v1/reads/prepare",
            ReadStatus,
            json=request.model_dump(mode="json"),
        )

    def read_status(self, request: ReadRequest) -> ReadStatus:
        return self._model(
            "POST",
            "/v1/reads/status",
            ReadStatus,
            json=request.model_dump(mode="json"),
        )

    def cleanup_read(self, request: ReadRequest) -> None:
        response = self._request(
            "POST",
            "/v1/reads/cleanup",
            json=request.model_dump(mode="json"),
        )
        if response.status_code != 204:
            self._raise_response(response)

    def abort_incomplete_uploads(self, *, initiated_before: str) -> MaintenanceResult:
        return self._model(
            "POST",
            "/v1/maintenance/abort-incomplete-uploads",
            MaintenanceResult,
            json=AbortIncompleteUploadsRequest(initiated_before=initiated_before).model_dump(
                mode="json"
            ),
        )

    def _model(
        self,
        method: str,
        path: str,
        model: type[_ModelT],
        *,
        json: object | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> _ModelT:
        response = self._request(
            method,
            path,
            json=json,
            content=content,
            headers=headers,
            params=params,
        )
        if not response.is_success:
            self._raise_response(response)
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            raise StorageAdapterProtocolError(
                f"adapter returned an invalid {model.__name__}"
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: object | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        return self._client.request(
            method,
            self.base_url + path,
            headers={**self._headers, **(headers or {})},
            json=json,
            content=content,
            params=params,
        )

    @staticmethod
    def _raise_response(response: httpx.Response) -> None:
        try:
            error = StorageAdapterError.model_validate_json(response.content)
        except ValidationError:
            raise StorageAdapterProtocolError(
                f"storage adapter returned HTTP {response.status_code}",
                status=response.status_code,
            ) from None
        raise StorageAdapterProtocolError(
            error.message,
            status=response.status_code,
            code=error.code,
        )


__all__ = ["StorageAdapterClient", "StorageAdapterProtocolError"]
