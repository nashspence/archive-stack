from __future__ import annotations

import base64
import hashlib
import http.client
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote, urljoin, urlparse

import httpx
from http_api_contracts import parse_error_payload, safe_http_base_url
from jeb_protocol import attempt_state, attempt_watch_finished
from lifecycle_events import EventPage
from riverhog_provenance import FileProvenanceBinding, build_portable_provenance_set
from tus_transport import DEFAULT_TUS_UPLOAD_CHUNK_MIB, TusTransport

QueryValue = str | int | float | bool | None
DEFAULT_INGRESS_URL = "http://127.0.0.1:1081"


class JebApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "client_error",
        status: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = dict(details or {})


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


def _timeout() -> float:
    raw = os.getenv("JEB_HTTP_TIMEOUT_SECONDS", "300")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("JEB_HTTP_TIMEOUT_SECONDS must be a positive number") from exc
    if value <= 0:
        raise ValueError("JEB_HTTP_TIMEOUT_SECONDS must be a positive number")
    return value


class JebApiClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        allow_insecure_http: bool | None = None,
    ) -> None:
        self.allow_insecure_http = (
            _bool_env("JEB_ALLOW_INSECURE_HTTP", False)
            if allow_insecure_http is None
            else allow_insecure_http
        )
        self.base_url = safe_http_base_url(
            base_url or os.getenv("JEB_BASE_URL") or "http://127.0.0.1:8081",
            setting="JEB_BASE_URL",
            allow_insecure_http=self.allow_insecure_http,
        )
        self.token = token or os.getenv("JEB_TOKEN")
        self.http2 = _bool_env("JEB_HTTP2", True)
        self.timeout_seconds = _timeout()
        self._client: httpx.Client | None = None

    def _persistent_client(self) -> httpx.Client:
        if self._client is None:
            headers = {"Accept": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._client = httpx.Client(
                base_url=self.base_url,
                headers=headers,
                timeout=self.timeout_seconds,
                http2=self.http2,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, QueryValue] | None = None,
        json: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        response = self._persistent_client().request(
            method,
            path,
            params=params,
            json=json,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise JebApiError(
                f"Jeb returned HTTP {response.status_code} without JSON",
                code="invalid_response",
                status=response.status_code,
            ) from exc
        if response.status_code >= 400:
            code, message, details = parse_error_payload(
                payload,
                fallback_message=f"Jeb returned HTTP {response.status_code}",
            )
            raise JebApiError(
                message,
                code=code,
                status=response.status_code,
                details=details,
            )
        if not isinstance(payload, dict):
            raise JebApiError(
                "Jeb returned a non-object JSON response",
                code="invalid_response",
                status=response.status_code,
            )
        return payload

    def get_status(self, *, include_backlog: bool = True) -> dict[str, Any]:
        return self._json(
            "GET",
            "/v1/status",
            params={"include_backlog": str(include_backlog).lower()},
        )

    def list_lifecycle_events(
        self,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> EventPage:
        return EventPage.model_validate(
            self._json(
                "GET",
                "/v1/events",
                params={"after": after or "0", "limit": limit},
            )
        )

    def list_attempts(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "updated_at",
        order: str = "desc",
        resolution: str = "unresolved",
        state: str | None = None,
        source: str | None = None,
        target: str | None = None,
        query: str | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, QueryValue] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
            "resolution": resolution,
        }
        for key, value in {
            "state": state,
            "source": source,
            "target": target,
            "q": query,
        }.items():
            if value is not None:
                params[key] = value
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/attempts", params=params)

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/attempts/{quote(attempt_id, safe='')}")

    def cancel_attempt(self, attempt_id: str) -> dict[str, Any]:
        return self._json("DELETE", f"/v1/attempts/{quote(attempt_id, safe='')}")

    def wait_for_attempt(
        self,
        attempt_id: str,
        *,
        interval: float = 10.0,
        on_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if interval <= 0:
            raise ValueError("interval must be positive")
        last_state: str | None = None
        while True:
            try:
                attempt = self.get_attempt(attempt_id)
            except httpx.TransportError:
                time.sleep(interval)
                continue
            state = attempt_state(attempt)
            if not state:
                raise JebApiError("Jeb attempt response is missing state")
            if on_update is not None and state != last_state:
                on_update(attempt)
            if attempt_watch_finished(attempt):
                return attempt
            last_state = state
            time.sleep(interval)

    def check_config(self) -> dict[str, Any]:
        return self._json("GET", "/v1/config/check")

    def run_once(self) -> dict[str, Any]:
        return self._json("POST", "/v1/once")

    def list_operations(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "started_at",
        order: str = "desc",
        state: str | None = None,
        query: str | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, QueryValue] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if state is not None:
            params["state"] = state
        if query is not None:
            params["q"] = query
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/operations", params=params)

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/operations/{quote(operation_id, safe='')}")

    def wait_for_operation(
        self,
        operation_id: str,
        *,
        interval: float = 1.0,
    ) -> dict[str, Any]:
        if interval <= 0:
            raise ValueError("interval must be positive")
        while True:
            try:
                operation = self.get_operation(operation_id)
            except httpx.TransportError:
                time.sleep(interval)
                continue
            if operation.get("state") in {"succeeded", "failed"}:
                return operation
            time.sleep(interval)

    def archive_source_now(
        self,
        *,
        source: str,
        process: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/archive-now",
            json={"source": source, "process": process, "dry_run": dry_run},
        )

    def list_sources(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "id",
        order: str = "asc",
        query: str | None = None,
        enabled: bool | None = None,
        adapter: str | None = None,
        target: str | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, QueryValue] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        for key, value in {
            "q": query,
            "enabled": None if enabled is None else str(enabled).lower(),
            "adapter": adapter,
            "target": target,
        }.items():
            if value is not None:
                params[key] = value
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/sources", params=params)

    def get_source(self, source_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/sources/{quote(source_id, safe='')}")

    def create_source(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._json("POST", "/v1/sources", json=payload)

    def update_source(
        self,
        source_id: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/v1/sources/{quote(source_id, safe='')}",
            json=changes,
        )

    def enable_source(self, source_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/sources/{quote(source_id, safe='')}/enable")

    def disable_source(self, source_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/sources/{quote(source_id, safe='')}/disable")

    def rotate_source_credential(
        self,
        source_id: str,
        *,
        credential: str | None = None,
    ) -> dict[str, Any]:
        payload = {} if credential is None else {"credential": credential}
        return self._json(
            "POST",
            f"/v1/sources/{quote(source_id, safe='')}/credential",
            json=payload,
        )

    def plan_source_removal(
        self,
        source_id: str,
        *,
        purge: bool,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/sources/{quote(source_id, safe='')}/removal-plan",
            json={"purge": purge},
        )

    def remove_source(self, source_id: str, *, challenge: str) -> dict[str, Any]:
        return self._json(
            "DELETE",
            f"/v1/sources/{quote(source_id, safe='')}",
            json={"challenge": challenge},
        )


class JebIngressClient:
    """Official Jeb file-ingress client with separately verified provenance."""

    def __init__(
        self,
        *,
        source: str,
        password: str,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        allow_insecure_http: bool | None = None,
    ) -> None:
        if not source.strip() or source != source.strip():
            raise ValueError("Jeb ingress source must be a non-empty canonical value")
        if not password:
            raise ValueError("Jeb ingress password is required")
        self.allow_insecure_http = (
            _bool_env("JEB_ALLOW_INSECURE_HTTP", False)
            if allow_insecure_http is None
            else allow_insecure_http
        )
        self.base_url = safe_http_base_url(
            base_url or os.getenv("JEB_INGRESS_URL") or DEFAULT_INGRESS_URL,
            setting="JEB_INGRESS_URL",
            allow_insecure_http=self.allow_insecure_http,
        )
        self.source = source
        self._http = httpx.Client(
            auth=httpx.BasicAuth(source, password),
            transport=transport,
            timeout=_timeout(),
            http2=_bool_env("JEB_HTTP2", True),
        )
        authorization = base64.b64encode(f"{source}:{password}".encode()).decode("ascii")
        self._tus = TusTransport(
            client=self._http,
            patch_client=self._http if transport is not None else None,
            headers={"Authorization": f"Basic {authorization}"},
            timeout_seconds=_timeout(),
        )

    def close(self) -> None:
        self._tus.close()
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def upload_file(
        self,
        path: Path,
        *,
        relative_path: str,
        binding: Mapping[str, object],
        journals: Mapping[str, bytes],
        chunk_mib: int = DEFAULT_TUS_UPLOAD_CHUNK_MIB,
    ) -> dict[str, Any]:
        if chunk_mib <= 0:
            raise ValueError("Jeb upload chunk size must be positive")
        size = path.stat().st_size
        normalized_binding = _provenance_binding(binding)
        if normalized_binding.bytes != size:
            raise ValueError("Jeb provenance binding size does not match the payload")
        provenance = build_portable_provenance_set(
            bindings=(normalized_binding,),
            journals=journals,
        )
        upload_url = self._tus.create_upload(
            urljoin(self.base_url.rstrip("/") + "/", "files/"),
            length=size,
            metadata={
                "path": relative_path,
                "sha256": normalized_binding.sha256,
                "provenance_sha256": hashlib.sha256(provenance).hexdigest(),
            },
        )
        upload_id = Path(urlparse(upload_url).path.rstrip("/")).name
        if not upload_id:
            raise RuntimeError("Jeb TUS upload returned an invalid identity")
        for journal_id, content in sorted(journals.items()):
            self.put_provenance_journal(
                upload_id,
                journal_id,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            )
        self.put_provenance_binding(upload_id, binding)

        chunk_bytes = chunk_mib * 1024 * 1024
        offset = self._tus.head_offset(upload_url)
        if offset < 0 or offset > size:
            raise RuntimeError("Jeb TUS upload disappeared or reported an invalid offset")
        with path.open("rb") as stream:
            stream.seek(offset)
            while offset < size:
                content = stream.read(min(chunk_bytes, size - offset))
                if not content:
                    raise RuntimeError("Jeb upload source ended before its declared size")
                try:
                    offset = self._tus.patch_chunk(
                        upload_url,
                        offset=offset,
                        content=content,
                        checksum_algorithm="sha256",
                    )
                except (OSError, http.client.HTTPException, httpx.TransportError):
                    resumed = self._tus.head_offset(upload_url)
                    if resumed < offset or resumed > offset + len(content):
                        raise RuntimeError("Jeb TUS retry returned an invalid offset") from None
                    offset = resumed
                    stream.seek(offset)
        return {
            "status": "uploaded",
            "upload_id": upload_id,
            "path": relative_path,
            "bytes": size,
            "provenance": dict(binding),
        }

    def put_provenance_journal(
        self,
        upload_id: str,
        journal_id: str,
        *,
        content: bytes,
        sha256: str,
    ) -> dict[str, Any]:
        """Publish one exact provenance journal for an open ingress upload."""

        response = self._http.put(
            self._provenance_url(upload_id, f"journals/{quote(journal_id, safe='')}"),
            content=content,
            headers={
                "Content-Type": "application/json-seq",
                "X-Riverhog-Provenance-SHA256": sha256,
            },
        )
        self._raise_ingress_error(response)
        return self._ingress_json(response)

    def put_provenance_binding(
        self,
        upload_id: str,
        binding: Mapping[str, object],
    ) -> dict[str, Any]:
        """Publish the payload binding for an open ingress upload."""

        response = self._http.put(
            self._provenance_url(upload_id, "binding"),
            json=dict(binding),
        )
        self._raise_ingress_error(response)
        return self._ingress_json(response)

    def _provenance_url(self, upload_id: str, suffix: str) -> str:
        return urljoin(
            self.base_url.rstrip("/") + "/",
            f"provenance/{quote(upload_id, safe='')}/{suffix}",
        )

    @staticmethod
    def _raise_ingress_error(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            _code, message, _details = parse_error_payload(
                payload,
                fallback_message=f"Jeb ingress returned HTTP {response.status_code}",
            )
        else:
            message = f"Jeb ingress returned HTTP {response.status_code}"
        raise JebApiError(message, status=response.status_code)

    @staticmethod
    def _ingress_json(response: httpx.Response) -> dict[str, Any]:
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError("Jeb ingress returned an invalid JSON receipt")
        return value


def _provenance_binding(value: Mapping[str, object]) -> FileProvenanceBinding:
    status = str(value.get("status") or "")
    path = str(value.get("path") or "")
    byte_count = int(str(value.get("bytes") or "0"))
    sha256 = str(value.get("sha256") or "")
    if status == "captured":
        return FileProvenanceBinding(
            path=path,
            bytes=byte_count,
            sha256=sha256,
            status="captured",
            journal_id=str(value.get("journal_id") or ""),
            current_state_id=str(value.get("current_state_id") or ""),
        )
    if status == "omitted":
        return FileProvenanceBinding(
            path=path,
            bytes=byte_count,
            sha256=sha256,
            status="omitted",
            omission_reason=str(value.get("omission_reason") or ""),
        )
    raise ValueError("Jeb provenance binding status is invalid")


__all__ = ["JebApiClient", "JebApiError", "JebIngressClient"]
