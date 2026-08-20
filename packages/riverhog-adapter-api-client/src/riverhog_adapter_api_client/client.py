"""Official resumable client for a deployed Riverhog TUS adapter."""

from __future__ import annotations

import base64
import hashlib
import http.client
import math
import os
import time
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote, urljoin, urlparse

import httpx
from http_api_contracts import parse_error_payload, safe_http_base_url
from riverhog_provenance import (
    FileProvenanceBinding,
    PreparedFileProvenance,
    build_portable_provenance_set,
)
from tus_transport import DEFAULT_TUS_UPLOAD_CHUNK_MIB, TusHttpError, TusTransport


class AdapterApiError(RuntimeError):
    def __init__(
        self, message: str, *, code: str = "adapter_error", status: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class RiverhogAdapterClient:
    """Official operator client for one deployed adapter service."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        allow_insecure_http: bool | None = None,
        timeout_seconds: float | None = None,
        http2: bool | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        allow = (
            _bool_env("RIVERHOG_ADAPTERS_ALLOW_INSECURE_HTTP", False)
            if allow_insecure_http is None
            else allow_insecure_http
        )
        self.base_url = safe_http_base_url(
            base_url or os.getenv("RIVERHOG_ADAPTERS_BASE_URL") or "http://127.0.0.1:8082",
            setting="RIVERHOG_ADAPTERS_BASE_URL",
            allow_insecure_http=allow,
        )
        self.allow_insecure_http = allow
        self.token = token or os.getenv("RIVERHOG_ADAPTERS_TOKEN")
        self.http2 = _bool_env("RIVERHOG_ADAPTERS_HTTP2", True) if http2 is None else http2
        self.timeout_seconds = (
            _timeout_env("RIVERHOG_ADAPTERS_HTTP_TIMEOUT_SECONDS", 300.0)
            if timeout_seconds is None
            else _positive_seconds(timeout_seconds, "timeout_seconds")
        )
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._http = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            http2=self.http2,
            timeout=self.timeout_seconds,
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def adapter_health_live(self) -> dict[str, Any]:
        return self._json("GET", "/health/live", authenticated=False)

    def adapter_health_ready(self) -> dict[str, Any]:
        return self._json("GET", "/health/ready", authenticated=False)

    def get_adapter_status(self) -> dict[str, Any]:
        return self._json("GET", "/v1/status")

    def run_adapter_pass(self) -> dict[str, Any]:
        return self._json("POST", "/v1/run")

    def flush_adapter_source(self, source_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/sources/{quote(source_id, safe='')}/flush")

    def _json(self, method: str, path: str, *, authenticated: bool = True) -> dict[str, Any]:
        if authenticated and not self.token:
            raise AdapterApiError("RIVERHOG_ADAPTERS_TOKEN is required", code="unauthorized")
        response = self._http.request(method, path)
        RiverhogTusClient._raise(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise AdapterApiError("adapter returned a non-object response", code="invalid_response")
        return payload


class RiverhogTusClient:
    def __init__(
        self,
        *,
        source: str,
        password: str,
        base_url: str | None = None,
        allow_insecure_http: bool | None = None,
        timeout_seconds: float | None = None,
        http2: bool | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not source or source != source.strip() or not password:
            raise ValueError("Riverhog TUS adapter credentials are required")
        allow = (
            _bool_env("RIVERHOG_ADAPTERS_ALLOW_INSECURE_HTTP", False)
            if allow_insecure_http is None
            else allow_insecure_http
        )
        self.base_url = safe_http_base_url(
            base_url or os.environ.get("RIVERHOG_ADAPTERS_INGRESS_URL", "https://127.0.0.1:1081"),
            setting="RIVERHOG_ADAPTERS_INGRESS_URL",
            allow_insecure_http=allow,
        )
        self.allow_insecure_http = allow
        self.http2 = _bool_env("RIVERHOG_ADAPTERS_HTTP2", True) if http2 is None else http2
        self.timeout_seconds = (
            _timeout_env("RIVERHOG_ADAPTERS_HTTP_TIMEOUT_SECONDS", 300.0)
            if timeout_seconds is None
            else _positive_seconds(timeout_seconds, "timeout_seconds")
        )
        self.source = source
        self._http = httpx.Client(
            auth=httpx.BasicAuth(source, password),
            transport=transport,
            timeout=self.timeout_seconds,
            http2=self.http2,
        )
        encoded = base64.b64encode(f"{source}:{password}".encode()).decode("ascii")
        self._tus = TusTransport(
            client=self._http,
            patch_client=self._http if transport is not None else None,
            headers={"Authorization": f"Basic {encoded}"},
            timeout_seconds=self.timeout_seconds,
        )

    def close(self) -> None:
        self._tus.close()
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def upload_file(
        self,
        path: Path,
        *,
        relative_path: str,
        provenance: PreparedFileProvenance,
        chunk_mib: int = DEFAULT_TUS_UPLOAD_CHUNK_MIB,
        poll_seconds: float = 1.0,
        timeout_seconds: float = 24 * 60 * 60,
    ) -> dict[str, Any]:
        if chunk_mib < 1:
            raise ValueError("TUS chunk size must be positive")
        binding = provenance.binding
        if binding.path != relative_path or binding.bytes != path.stat().st_size:
            raise ValueError("provenance binding differs from the upload source")
        portable = build_portable_provenance_set(
            bindings=(binding,),
            journals=provenance.journals,
        )
        upload_url = self._tus.create_upload(
            urljoin(self.base_url.rstrip("/") + "/", "files/"),
            length=binding.bytes,
            metadata={
                "path": relative_path,
                "sha256": binding.sha256,
                "provenance_sha256": hashlib.sha256(portable).hexdigest(),
            },
        )
        upload_id = Path(urlparse(upload_url).path.rstrip("/")).name
        if not upload_id:
            raise AdapterApiError(
                "TUS adapter returned no upload identity", code="invalid_response"
            )
        for journal_id, content in sorted(provenance.journals.items()):
            self.put_tus_provenance_journal(
                upload_id,
                journal_id,
                content,
            )
        self.put_tus_provenance_binding(upload_id, _binding_row(binding))
        chunk_bytes = chunk_mib * 1024 * 1024
        offset = self._tus.head_offset(upload_url)
        if offset < 0 or offset > binding.bytes:
            raise AdapterApiError("TUS upload disappeared or returned an invalid offset")
        with path.open("rb") as stream:
            stream.seek(offset)
            while offset < binding.bytes:
                content = stream.read(min(chunk_bytes, binding.bytes - offset))
                if not content:
                    raise AdapterApiError("upload source ended before its declared size")
                try:
                    offset = self._tus.patch_chunk(
                        upload_url,
                        offset=offset,
                        content=content,
                        checksum_algorithm="sha256",
                    )
                except (OSError, http.client.HTTPException, httpx.TransportError, TusHttpError):
                    try:
                        resumed = self._tus.head_offset(upload_url)
                    except (OSError, http.client.HTTPException, httpx.TransportError, TusHttpError):
                        if offset + len(content) == binding.bytes:
                            offset = binding.bytes
                            break
                        raise
                    if resumed == -1 and offset + len(content) == binding.bytes:
                        offset = binding.bytes
                        break
                    if resumed < offset or resumed > offset + len(content):
                        raise AdapterApiError("TUS retry returned an invalid offset") from None
                    offset = resumed
                    stream.seek(offset)
        receipt = self.wait_for_publication(
            upload_id,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )
        expected = (upload_id, relative_path, binding.bytes, binding.sha256)
        actual = (
            str(receipt.get("upload_id") or ""),
            str(receipt.get("path") or ""),
            int(receipt.get("bytes") or -1),
            str(receipt.get("payload_sha256") or ""),
        )
        if actual != expected:
            raise AdapterApiError(
                "finalized adapter receipt differs from the submitted payload",
                code="invalid_response",
            )
        return receipt

    def wait_for_publication(
        self,
        upload_id: str,
        *,
        poll_seconds: float = 1.0,
        timeout_seconds: float = 24 * 60 * 60,
    ) -> dict[str, Any]:
        if poll_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("adapter publication timing must be positive")
        deadline = time.monotonic() + timeout_seconds
        while True:
            payload = self.get_tus_publication(upload_id)
            status = str(payload.get("status") or "")
            if status == "accepted":
                return payload
            if status not in {"pending"}:
                raise AdapterApiError(
                    str(payload.get("message") or "adapter publication was rejected"),
                    code=str(payload.get("code") or "publication_rejected"),
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(f"adapter publication {upload_id} did not finalize")
            time.sleep(poll_seconds)

    def put_tus_provenance_journal(
        self,
        upload_id: str,
        journal_id: str,
        content: bytes,
    ) -> dict[str, Any]:
        response = self._http.put(
            self._url(
                f"v1/tus-publications/{quote(upload_id, safe='')}/journals/"
                f"{quote(journal_id, safe='')}"
            ),
            headers={
                "Content-Type": "application/json-seq",
                "X-Content-SHA256": hashlib.sha256(content).hexdigest(),
            },
            content=content,
        )
        self._raise(response)
        return _object_response(response)

    def put_tus_provenance_binding(
        self,
        upload_id: str,
        binding: dict[str, object],
    ) -> dict[str, Any]:
        response = self._http.put(
            self._url(f"v1/tus-publications/{quote(upload_id, safe='')}/binding"),
            json=binding,
        )
        self._raise(response)
        return _object_response(response)

    def get_tus_publication(self, upload_id: str) -> dict[str, Any]:
        response = self._http.get(self._url(f"v1/tus-publications/{quote(upload_id, safe='')}"))
        self._raise(response)
        return _object_response(response)

    def _url(self, path: str) -> str:
        return urljoin(self.base_url.rstrip("/") + "/", path)

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            payload = response.json()
        except ValueError:
            payload = None
        code, message, _details = parse_error_payload(
            payload,
            fallback_message=response.text or f"HTTP {response.status_code}",
        )
        raise AdapterApiError(message, code=code, status=response.status_code)


def _binding_row(value: FileProvenanceBinding) -> dict[str, object]:
    row: dict[str, object] = {
        "path": value.path,
        "bytes": value.bytes,
        "sha256": value.sha256,
        "status": value.status,
    }
    if value.status == "captured":
        row.update(journal_id=value.journal_id, current_state_id=value.current_state_id)
    else:
        row["omission_reason"] = value.omission_reason
    return row


def _object_response(response: httpx.Response) -> dict[str, Any]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise AdapterApiError("adapter returned a non-object response", code="invalid_response")
    return payload


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _timeout_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return _positive_seconds(float(raw), name)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number of seconds") from exc


def _positive_seconds(value: float, name: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number of seconds")
    return value


__all__ = ["AdapterApiError", "RiverhogAdapterClient", "RiverhogTusClient"]
