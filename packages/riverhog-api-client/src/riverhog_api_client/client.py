from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast
from urllib.parse import quote
from xml.etree import ElementTree

import httpx
from application_access import (
    ApplicationAccessGrant,
    ApplicationAccessGrantSet,
    ApplicationKeyId,
    ApplicationName,
    MonthlyDownloadQuotaBytes,
)
from application_access import (
    ApplicationPermission as ApplicationPermission,
)
from application_access import (
    ApplicationResource as ApplicationResource,
)
from file_download import verified_download
from http_api_contracts import CanonicalVisibleText, parse_error_payload, safe_http_base_url
from pydantic import Field, TypeAdapter, ValidationError
from riverhog_protocol import (
    ApplicationAccessSort,
    ApplicationKeySort,
    ApplicationSort,
    ArchiveCopySort,
    ArchiveCopyState,
    ArchiveCopyStoreSelectionDocument,
    ArchiveStoreName,
    ArchiveStoreSort,
    CollectionId,
    CollectionSort,
    CollectionUploadArtifactCustodyReceiptDocument,
    CollectionUploadCustodyMode,
    CollectionUploadFileBatchDocument,
    CollectionUploadFileIn,
    CollectionUploadRegistrationConstraintsDocument,
    CollectionUploadSort,
    CollectionUploadState,
    DownloadQuotaSort,
    ImmutableFileIdentityDocument,
    PortableCollectionRecord,
    ProcessingClaimId,
    ProvenanceSort,
    ProvenanceStatus,
    RetrievalCacheProtection,
    RetrievalCacheSort,
    RetrievalCacheState,
    RetrievalFileReferenceSetDocument,
    SearchSort,
    SortOrder,
    TagSort,
    validate_collection_upload_artifact_custody_receipt,
    validate_collection_upload_batch_against_registration_constraints,
)
from riverhog_protocol.errors import (
    BadRequest,
    HashMismatch,
    InvalidState,
    RiverhogError,
    ServiceUnavailable,
    error_type_for_code,
)
from riverhog_protocol.lifecycle_events import RiverhogEventPage
from riverhog_protocol.paths import (
    CanonicalRelPath,
    normalize_collection_id,
    validate_canonical_tag,
)
from riverhog_provenance_contracts import ProvenanceJournalId

from riverhog_api_client.workflows import CollectionWorkflowMethods

_HTTP_TIMEOUT_SECONDS = 300.0
_UPLOAD_TIMEOUT_SECONDS = 1800.0
_CANCEL_TIMEOUT_SECONDS = 1800.0
_DOWNLOAD_TIMEOUT_SECONDS = 3600.0
_DOWNLOAD_CHUNK_BYTES = 8 * 1024 * 1024
_TRANSIENT_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
DownloadProgress = Callable[[int, int | None], None]

type RestorePolicy = Literal["allow", "never"]
type ProvenanceMode = Literal["captured", "omitted"]
type CollectionUploadIdempotencyKey = Annotated[
    CanonicalVisibleText,
    Field(max_length=200),
]
_PROVENANCE_JOURNAL_ID: TypeAdapter[str] = TypeAdapter(ProvenanceJournalId)
_APPLICATION_NAME: TypeAdapter[str] = TypeAdapter(ApplicationName)
_APPLICATION_KEY_ID: TypeAdapter[str] = TypeAdapter(ApplicationKeyId)
_ARCHIVE_STORE_NAME: TypeAdapter[str] = TypeAdapter(ArchiveStoreName)
_COLLECTION_ID: TypeAdapter[int] = TypeAdapter(CollectionId)
_CANONICAL_RELPATH: TypeAdapter[str] = TypeAdapter(CanonicalRelPath)
_MONTHLY_DOWNLOAD_QUOTA_BYTES: TypeAdapter[int] = TypeAdapter(MonthlyDownloadQuotaBytes)
_PROCESSING_CLAIM_ID: TypeAdapter[str] = TypeAdapter(ProcessingClaimId)

_COLLECTION_UPLOAD_IDEMPOTENCY_KEY: TypeAdapter[CollectionUploadIdempotencyKey] = TypeAdapter(
    CollectionUploadIdempotencyKey
)


def _one_of(value: str, allowed: frozenset[str], label: str) -> str:
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise BadRequest(f"{label} must be one of: {choices}")
    return value


def _canonical_tag(value: str) -> str:
    try:
        return validate_canonical_tag(value)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc


def _application_name(value: str) -> str:
    try:
        return _APPLICATION_NAME.validate_python(value, strict=True)
    except ValidationError as exc:
        raise BadRequest(str(exc)) from exc


def _application_key_id(value: str) -> str:
    try:
        return _APPLICATION_KEY_ID.validate_python(value, strict=True)
    except ValidationError as exc:
        raise BadRequest(str(exc)) from exc


def _processing_claim_id(value: str) -> str:
    try:
        return _PROCESSING_CLAIM_ID.validate_python(value, strict=True)
    except ValidationError as exc:
        raise BadRequest("processing claim id must be a lowercase SHA-256") from exc


def _archive_store_name(value: str) -> str:
    try:
        return _ARCHIVE_STORE_NAME.validate_python(value, strict=True)
    except ValidationError as exc:
        raise BadRequest(
            "archive store name must use lowercase letters, digits, and single dashes"
        ) from exc


def _collection_id(value: int) -> int:
    try:
        return _COLLECTION_ID.validate_python(value)
    except ValidationError as exc:
        raise BadRequest("collection id must be a positive integer") from exc


def _canonical_relpath(value: str) -> str:
    try:
        return _CANONICAL_RELPATH.validate_python(value, strict=True)
    except ValidationError as exc:
        raise BadRequest("collection path must be canonical") from exc


def _canonical_tags(values: Sequence[str]) -> list[str]:
    tags = [_canonical_tag(value) for value in values]
    if len(tags) != len(set(tags)):
        raise BadRequest("collection tags must not contain duplicates")
    return tags


def _validated_collection_upload_idempotency_key(
    value: CollectionUploadIdempotencyKey,
) -> str:
    try:
        return _COLLECTION_UPLOAD_IDEMPOTENCY_KEY.validate_python(value, strict=True)
    except ValidationError as exc:
        raise BadRequest(str(exc)) from exc


def _validated_collection_upload_file_response(
    collection_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        if _COLLECTION_ID.validate_python(payload.get("collection_id")) != collection_id:
            raise ValueError("collection upload file response differs from its request")
        rows = payload.get("files")
        if not isinstance(rows, list):
            raise ValueError("collection upload file response has no file inventory")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("collection upload file response contains an invalid row")
            receipt_value = row.get("custody_receipt")
            if receipt_value is None:
                continue
            artifact = ImmutableFileIdentityDocument.model_validate(
                {
                    "path": row.get("path"),
                    "bytes": row.get("bytes"),
                    "sha256": row.get("sha256"),
                }
            )
            receipt = CollectionUploadArtifactCustodyReceiptDocument.model_validate(receipt_value)
            validate_collection_upload_artifact_custody_receipt(
                collection_id,
                artifact,
                receipt,
            )
    except (TypeError, ValidationError, ValueError) as exc:
        raise InvalidState("API returned an invalid collection upload file response") from exc
    return payload


def _restore_policy(value: RestorePolicy) -> RestorePolicy:
    return cast(RestorePolicy, _one_of(value, frozenset({"allow", "never"}), "restore_policy"))


def _provenance_choice(
    mode: ProvenanceMode,
    omission_reason: str | None,
) -> tuple[ProvenanceMode, str | None]:
    normalized_mode = cast(
        ProvenanceMode,
        _one_of(mode, frozenset({"captured", "omitted"}), "provenance_mode"),
    )
    if normalized_mode == "captured" and omission_reason is None:
        return normalized_mode, None
    if (
        normalized_mode == "omitted"
        and omission_reason is not None
        and omission_reason
        and omission_reason.strip() == omission_reason
    ):
        return normalized_mode, omission_reason
    raise BadRequest("provenance_mode must be captured, or omitted with provenance_omission_reason")


def _application_access_payload(
    access: Sequence[Mapping[str, str]],
) -> list[dict[str, Any]]:
    try:
        grants = ApplicationAccessGrantSet.model_validate([dict(current) for current in access])
    except ValidationError as exc:
        raise BadRequest(str(exc)) from exc
    return cast(list[dict[str, Any]], grants.model_dump(mode="json"))


def _application_access_grant_payload(
    permission: ApplicationPermission,
    resource: ApplicationResource,
) -> dict[str, Any]:
    try:
        grant = ApplicationAccessGrant(permission=permission, resource=resource)
    except ValidationError as exc:
        raise BadRequest(str(exc)) from exc
    return grant.model_dump(mode="json")


def _bool_env(env_name: str, default: bool) -> bool:
    raw_value = os.getenv(env_name)
    if raw_value is None or raw_value.strip() == "":
        return default
    normalized = raw_value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise BadRequest(f"{env_name} must be true or false")


def _timeout_seconds(env_name: str, default: float) -> float:
    raw_value = os.getenv(env_name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise BadRequest(f"{env_name} must be a positive number of seconds") from exc
    if value <= 0:
        raise BadRequest(f"{env_name} must be a positive number of seconds")
    return value


def _file_selections_payload(
    files: Sequence[tuple[int, str]],
) -> list[dict[str, object]]:
    ordered = sorted(files, key=lambda item: (item[0], item[1].encode("utf-8")))
    try:
        document = RetrievalFileReferenceSetDocument.model_validate(
            {
                "files": [
                    {"collection_id": collection_id, "path": path}
                    for collection_id, path in ordered
                ]
            }
        )
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    return [item.model_dump(mode="json") for item in document.files]


class _HttpApiClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        token_env: str,
        allow_insecure_http: bool | None = None,
    ) -> None:
        self.allow_insecure_http = (
            _bool_env("RIVERHOG_ALLOW_INSECURE_HTTP", False)
            if allow_insecure_http is None
            else allow_insecure_http
        )
        try:
            self.base_url = safe_http_base_url(
                base_url or os.getenv("RIVERHOG_BASE_URL") or "http://127.0.0.1:8000",
                setting="RIVERHOG_BASE_URL",
                allow_insecure_http=self.allow_insecure_http,
            )
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        self.token = token or os.getenv(token_env)
        self.host_header = os.getenv("RIVERHOG_HOST_HEADER", "").strip() or None
        self.http2 = _bool_env("RIVERHOG_HTTP2", True)
        self.timeout_seconds = _timeout_seconds(
            "RIVERHOG_HTTP_TIMEOUT_SECONDS",
            _HTTP_TIMEOUT_SECONDS,
        )
        self._request_client: httpx.Client | None = None
        self._download_client: httpx.Client | None = None

    def _make_client(self, *, timeout_seconds: float) -> httpx.Client:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.host_header:
            headers["Host"] = self.host_header
        return httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout_seconds,
            http2=self.http2,
        )

    def _client(self) -> httpx.Client:
        return self._make_client(timeout_seconds=self.timeout_seconds)

    def _persistent_client(self) -> httpx.Client:
        if self._request_client is None:
            self._request_client = self._client()
        return self._request_client

    def close(self) -> None:
        if self._request_client is not None:
            self._request_client.close()
            self._request_client = None
        if self._download_client is not None:
            self._download_client.close()
            self._download_client = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _persistent_download_client(self) -> httpx.Client:
        if self._download_client is None:
            self._download_client = self._make_client(
                timeout_seconds=_timeout_seconds(
                    "RIVERHOG_DOWNLOAD_TIMEOUT_SECONDS",
                    _DOWNLOAD_TIMEOUT_SECONDS,
                )
            )
        return self._download_client

    def _raise_for_error(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            data = response.json()
        except Exception:  # pragma: no cover
            response.raise_for_status()
        code, message, details = parse_error_payload(
            data,
            fallback_message=response.text or f"HTTP {response.status_code}",
        )
        error_type = error_type_for_code(code)
        if error_type is None:
            error_type = (
                ServiceUnavailable
                if response.status_code in _TRANSIENT_HTTP_STATUS_CODES
                else RiverhogError
            )
        raise error_type(
            str(message),
            code=str(code),
            observed_status=response.status_code,
            details=details,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._persistent_client().request(method, path, **kwargs)
        except httpx.TransportError:
            self.close()
            raise
        if response.status_code in _TRANSIENT_HTTP_STATUS_CODES:
            self.close()
        self._raise_for_error(response)
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        payload = self._request(method, path, **kwargs).json()
        if not isinstance(payload, dict):
            raise BadRequest("API returned a non-object JSON payload")
        return payload

    def _download(
        self,
        path: str,
        output: Path,
        *,
        expected_bytes: int,
        expected_sha256: str,
        progress: DownloadProgress | None = None,
    ) -> int:
        client = self._persistent_download_client()
        with client.stream("GET", path) as response:
            if not response.is_success:
                response.read()
                self._raise_for_error(response)

            content_length = response.headers.get("Content-Length")
            try:
                returned_bytes = int(content_length) if content_length is not None else -1
            except ValueError as exc:
                raise InvalidState("download returned an invalid Content-Length") from exc
            if returned_bytes != expected_bytes:
                raise InvalidState(
                    "download Content-Length does not match planned metadata: "
                    f"{returned_bytes} != {expected_bytes}"
                )
            returned_etag = response.headers.get("ETag", "").strip().strip('"').casefold()
            if returned_etag != expected_sha256.casefold():
                raise InvalidState("download ETag does not match planned SHA-256")
            try:
                receipt = verified_download(
                    response.iter_bytes(chunk_size=_DOWNLOAD_CHUNK_BYTES),
                    output=output,
                    expected_bytes=expected_bytes,
                    expected_sha256=expected_sha256,
                    progress=(
                        (lambda current, total: progress(current, total))
                        if progress is not None
                        else None
                    ),
                )
            except httpx.TransportError as exc:
                self.close()
                raise ServiceUnavailable(
                    "download stream was interrupted before completion; "
                    "the partial file was discarded"
                ) from exc
            return receipt.bytes


class ApiClient(CollectionWorkflowMethods, _HttpApiClient):
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        allow_insecure_http: bool | None = None,
    ) -> None:
        super().__init__(
            base_url,
            token,
            token_env="RIVERHOG_TOKEN",
            allow_insecure_http=allow_insecure_http,
        )
        self.upload_timeout_seconds = _timeout_seconds(
            "RIVERHOG_UPLOAD_TIMEOUT_SECONDS",
            _UPLOAD_TIMEOUT_SECONDS,
        )

    def spawn(self) -> ApiClient:
        worker = ApiClient(
            base_url=self.base_url,
            token=self.token,
            allow_insecure_http=self.allow_insecure_http,
        )
        worker.host_header = self.host_header
        worker.http2 = self.http2
        worker.timeout_seconds = self.timeout_seconds
        worker.upload_timeout_seconds = self.upload_timeout_seconds
        return worker

    def list_lifecycle_events(
        self,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> RiverhogEventPage:
        payload = self._json(
            "GET",
            "/v1/events",
            params={"after": after or "0", "limit": limit},
        )
        return RiverhogEventPage.model_validate(payload)

    def resourcesync_discovery(self) -> dict[str, object]:
        response = self._request("GET", "/.well-known/resourcesync")
        root = ElementTree.fromstring(response.content)
        capabilities: list[dict[str, str]] = []
        for url in root:
            location = next((child.text for child in url if child.tag.endswith("loc")), None)
            metadata = next((child for child in url if child.tag.endswith("md")), None)
            if location and metadata is not None:
                capabilities.append(
                    {
                        "capability": str(metadata.attrib.get("capability", "")),
                        "location": location,
                    }
                )
        return {"capabilities": capabilities}

    def resourcesync_capabilities(self) -> dict[str, object]:
        response = self._request("GET", "/resourcesync/capabilitylist.xml")
        root = ElementTree.fromstring(response.content)
        capabilities: list[dict[str, str]] = []
        for url in root:
            location = next((child.text for child in url if child.tag.endswith("loc")), None)
            metadata = next((child for child in url if child.tag.endswith("md")), None)
            if location and metadata is not None:
                capabilities.append(
                    {
                        "capability": str(metadata.attrib.get("capability", "")),
                        "location": location,
                    }
                )
        return {"capabilities": capabilities}

    def resourcesync_resource_pages(self) -> dict[str, object]:
        response = self._request("GET", "/resourcesync/resourcelist.xml")
        root = ElementTree.fromstring(response.content)
        pages = [
            location
            for sitemap in root
            if sitemap.tag.endswith("sitemap")
            if (
                location := next(
                    (child.text for child in sitemap if child.tag.endswith("loc")),
                    None,
                )
            )
        ]
        return {"pages": pages}

    def resourcesync_resources(self, *, page: int = 1) -> dict[str, object]:
        if page < 1:
            raise ValueError("ResourceSync resource-list page must be positive")
        response = self._request("GET", f"/resourcesync/resourcelist/{page}.xml")
        root = ElementTree.fromstring(response.content)
        resources: list[dict[str, object]] = []
        for url in root:
            if not url.tag.endswith("url"):
                continue
            location = next((child.text for child in url if child.tag.endswith("loc")), None)
            metadata = next((child for child in url if child.tag.endswith("md")), None)
            if location is None or metadata is None:
                continue
            collection_id = normalize_collection_id(
                location.split("/v1/catalog/collections/", 1)[-1].rsplit("/manifest", 1)[0]
            )
            resources.append(
                {
                    "collection_id": collection_id,
                    "etag": metadata.attrib.get("hash", "").removeprefix("sha-256:"),
                    "location": location,
                }
            )
        return {"page": page, "resources": resources}

    def catalog_changes(self, *, after: int = 0) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/resourcesync/changelist.xml",
            params={"after": after},
        )
        root = ElementTree.fromstring(response.content)
        changes: list[dict[str, Any]] = []
        for url in root:
            loc = next((child.text for child in url if child.tag.endswith("loc")), None)
            metadata = next((child for child in url if child.tag.endswith("md")), None)
            if loc is None or metadata is None:
                continue
            collection_id = normalize_collection_id(
                loc.split("/v1/catalog/collections/", 1)[-1].rsplit("/manifest", 1)[0]
            )
            changes.append(
                {
                    "collection_id": collection_id,
                    "change": metadata.attrib.get("change"),
                    "datetime": metadata.attrib.get("datetime"),
                    "etag": metadata.attrib.get("hash", "").removeprefix("sha-256:"),
                }
            )
        return {
            "cursor": int(root.attrib.get("data-cursor", after)),
            "has_more": root.attrib.get("data-has-more", "false") == "true",
            "changes": changes,
        }

    def get_portable_collection_manifest(
        self, collection_id: CollectionId
    ) -> PortableCollectionRecord:
        return PortableCollectionRecord.from_mapping(
            self._json(
                "GET",
                f"/v1/catalog/collections/{str(_collection_id(collection_id))}/manifest",
            )
        )

    def plan_retrieval(
        self,
        files: Sequence[tuple[int, str]],
        *,
        lease_seconds: int | None = None,
        restore_policy: RestorePolicy = "allow",
    ) -> dict[str, Any]:
        validated_restore_policy = _restore_policy(restore_policy)
        payload: dict[str, Any] = {
            "files": _file_selections_payload(files),
            "restore_policy": validated_restore_policy,
        }
        if lease_seconds is not None:
            payload["lease_seconds"] = lease_seconds
        return self._json("POST", "/v1/retrieval-plans", json=payload)

    def create_retrieval_job(
        self,
        files: Sequence[tuple[int, str]],
        *,
        plan_etag: str,
        lease_seconds: int | None = None,
        restore_policy: RestorePolicy = "allow",
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        validated_restore_policy = _restore_policy(restore_policy)
        payload: dict[str, Any] = {
            "files": _file_selections_payload(files),
            "restore_policy": validated_restore_policy,
        }
        if lease_seconds is not None:
            payload["lease_seconds"] = lease_seconds
        if event_context is not None:
            payload["event_context"] = dict(event_context)
        return self._json(
            "POST",
            "/v1/retrieval-jobs",
            json=payload,
            headers={"If-Match": f'"{plan_etag}"'},
        )

    def get_retrieval_job(self, job_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/retrieval-jobs/{quote(job_id, safe='')}")

    def cancel_retrieval_job(self, job_id: str) -> dict[str, Any]:
        return self._json("DELETE", f"/v1/retrieval-jobs/{quote(job_id, safe='')}")

    def acknowledge_retrieval_job(self, job_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/retrieval-jobs/{quote(job_id, safe='')}/ack")

    def renew_retrieval_job(self, job_id: str, *, lease_seconds: int) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/retrieval-jobs/{quote(job_id, safe='')}/renew",
            json={"lease_seconds": lease_seconds},
        )

    def retrieval_cache_status(self) -> dict[str, Any]:
        return self._json("GET", "/v1/retrieval-cache")

    def list_retrieval_cache_objects(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        tag: str | None = None,
        collection_id: CollectionId | None = None,
        source_store: ArchiveStoreName | None = None,
        state: RetrievalCacheState | None = None,
        protection: RetrievalCacheProtection | None = None,
        expires_before: str | None = None,
        expires_after: str | None = None,
        sort: RetrievalCacheSort = "cached_at",
        order: SortOrder = "desc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": _one_of(
                sort,
                frozenset(
                    {
                        "collection_id",
                        "source_store",
                        "object_id",
                        "stored_bytes",
                        "cached_at",
                        "verified_at",
                        "protected_until",
                    }
                ),
                "retrieval-cache sort",
            ),
            "order": _one_of(order, frozenset({"asc", "desc"}), "sort order"),
        }
        if q:
            params["q"] = q
        if tag is not None:
            params["tag"] = _canonical_tag(tag)
        if collection_id is not None:
            params["collection_id"] = _collection_id(collection_id)
        if source_store is not None:
            params["source_store"] = _archive_store_name(source_store)
        if state:
            params["state"] = _one_of(
                state,
                frozenset({"ready", "delete_pending", "deleting"}),
                "retrieval-cache state",
            )
        if protection:
            params["protection"] = _one_of(
                protection,
                frozenset({"protected", "unleased"}),
                "retrieval-cache protection",
            )
        if expires_before:
            params["expires_before"] = expires_before
        if expires_after:
            params["expires_after"] = expires_after
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/retrieval-cache/objects", params=params)

    def get_retrieval_cache_object(
        self,
        collection_id: CollectionId,
        source_store: ArchiveStoreName,
        object_id: str,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            "/v1/retrieval-cache/objects/"
            f"{str(_collection_id(collection_id))}/"
            f"{quote(_archive_store_name(source_store), safe='')}/"
            f"{quote(object_id, safe='')}",
        )

    def download_retrieval_file(
        self,
        job_id: str,
        *,
        collection_id: CollectionId,
        path: str,
        output: Path,
        expected_bytes: int,
        expected_sha256: str,
        progress: DownloadProgress | None = None,
    ) -> int:
        result = self._download(
            f"/v1/retrieval-jobs/{quote(job_id, safe='')}/content?"
            f"collection_id={str(_collection_id(collection_id))}&"
            f"path={quote(_canonical_relpath(path), safe='')}",
            output,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
            progress=progress,
        )
        return int(result)

    @contextmanager
    def stream_retrieval_file(
        self,
        job_id: str,
        *,
        collection_id: CollectionId,
        path: str,
        expected_bytes: int,
        expected_sha256: str,
        start: int = 0,
        end: int | None = None,
        chunk_size: int = _DOWNLOAD_CHUNK_BYTES,
    ) -> Iterator[Iterator[bytes]]:
        """Stream one verified retrieval file or byte range.

        The returned iterator must be consumed completely before leaving the
        context. Full-file reads are SHA-256 verified; range reads are bound to
        the whole-file ETag and exact Content-Range returned by Riverhog.
        """

        if isinstance(expected_bytes, bool) or expected_bytes < 0:
            raise ValueError("expected retrieval bytes must be non-negative")
        digest = expected_sha256.casefold()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("expected retrieval SHA-256 is invalid")
        resolved_end = expected_bytes if end is None else end
        if (
            isinstance(start, bool)
            or isinstance(resolved_end, bool)
            or start < 0
            or resolved_end < start
            or resolved_end > expected_bytes
        ):
            raise ValueError("retrieval byte range is invalid")
        if chunk_size < 1:
            raise ValueError("retrieval stream chunk size must be positive")
        partial = start != 0 or resolved_end != expected_bytes
        if partial and start == resolved_end:
            raise ValueError("retrieval byte range must be nonempty")
        headers: dict[str, str] = {"Accept-Encoding": "identity"}
        if partial:
            headers["Range"] = f"bytes={start}-{resolved_end - 1}"
        client = self._persistent_download_client()
        request_path = f"/v1/retrieval-jobs/{quote(job_id, safe='')}/content"
        params: dict[str, str | int] = {
            "collection_id": _collection_id(collection_id),
            "path": _canonical_relpath(path),
        }
        try:
            with client.stream("GET", request_path, params=params, headers=headers) as response:
                if not response.is_success:
                    response.read()
                    self._raise_for_error(response)
                expected_status = 206 if partial else 200
                if response.status_code != expected_status:
                    raise InvalidState(
                        "retrieval stream returned an unexpected HTTP status: "
                        f"{response.status_code}"
                    )
                returned_etag = response.headers.get("ETag", "").strip().strip('"').casefold()
                if returned_etag != digest:
                    raise InvalidState("retrieval stream ETag does not match the planned SHA-256")
                expected_length = resolved_end - start
                raw_length = response.headers.get("Content-Length")
                try:
                    returned_length = int(raw_length) if raw_length is not None else -1
                except ValueError as exc:
                    raise InvalidState(
                        "retrieval stream returned an invalid Content-Length"
                    ) from exc
                if returned_length != expected_length:
                    raise InvalidState(
                        "retrieval stream Content-Length does not match the requested range"
                    )
                if partial:
                    expected_range = f"bytes {start}-{resolved_end - 1}/{expected_bytes}"
                    if response.headers.get("Content-Range") != expected_range:
                        raise InvalidState("retrieval stream Content-Range is inconsistent")

                returned = 0
                hasher = hashlib.sha256() if not partial else None

                def chunks() -> Iterator[bytes]:
                    nonlocal returned
                    for chunk in response.iter_bytes(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        returned += len(chunk)
                        if returned > expected_length:
                            raise InvalidState("retrieval stream exceeded the requested byte range")
                        if hasher is not None:
                            hasher.update(chunk)
                        yield chunk

                yield chunks()
                if returned != expected_length:
                    raise InvalidState("retrieval stream ended before the requested byte range")
                if hasher is not None and hasher.hexdigest() != digest:
                    raise HashMismatch("retrieval stream SHA-256 verification failed")
        except httpx.TransportError as exc:
            self.close()
            raise ServiceUnavailable("retrieval stream was interrupted") from exc

    def create_or_resume_collection_upload_session(
        self,
        idempotency_key: CollectionUploadIdempotencyKey,
        tags: Sequence[str],
        *,
        ingest_source: str | None = None,
        archive_store: ArchiveStoreName | None = None,
        event_context: Mapping[str, Any] | None = None,
        provenance_mode: ProvenanceMode = "captured",
        provenance_omission_reason: str | None = None,
        custody_mode: CollectionUploadCustodyMode = "producer-retained",
    ) -> dict[str, Any]:
        provenance_mode, provenance_omission_reason = _provenance_choice(
            provenance_mode,
            provenance_omission_reason,
        )
        payload: dict[str, Any] = {
            "idempotency_key": _validated_collection_upload_idempotency_key(idempotency_key),
            "tags": _canonical_tags(tags),
            "provenance_mode": provenance_mode,
        }
        normalized_custody_mode = _one_of(
            custody_mode,
            frozenset({"producer-retained", "custody-transfer"}),
            "collection upload custody mode",
        )
        if normalized_custody_mode != "producer-retained":
            payload["custody_mode"] = normalized_custody_mode
        if ingest_source is not None:
            payload["ingest_source"] = ingest_source
        if archive_store is not None:
            payload["archive_store"] = _archive_store_name(archive_store)
        if event_context is not None:
            payload["event_context"] = dict(event_context)
        if provenance_omission_reason is not None:
            payload["provenance_omission_reason"] = provenance_omission_reason
        return self._json("POST", "/v1/collection-upload-sessions", json=payload)

    def register_collection_upload_session_files(
        self,
        collection_id: CollectionId,
        files: Sequence[CollectionUploadFileIn | Mapping[str, Any]],
        *,
        registration_constraints: CollectionUploadRegistrationConstraintsDocument
        | Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            batch = CollectionUploadFileBatchDocument.model_validate(
                {
                    "files": [
                        file.model_dump(mode="json")
                        if isinstance(file, CollectionUploadFileIn)
                        else dict(file)
                        for file in files
                    ]
                }
            )
            constraints_document = (
                registration_constraints
                if isinstance(
                    registration_constraints,
                    CollectionUploadRegistrationConstraintsDocument,
                )
                else CollectionUploadRegistrationConstraintsDocument.model_validate(
                    dict(registration_constraints)
                )
            )
            validate_collection_upload_batch_against_registration_constraints(
                batch,
                constraints_document,
            )
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        normalized_collection_id = _collection_id(collection_id)
        return _validated_collection_upload_file_response(
            normalized_collection_id,
            self._json(
                "POST",
                f"/v1/collection-upload-sessions/{str(normalized_collection_id)}/files",
                json=batch.model_dump(mode="json"),
            ),
        )

    def list_collection_upload_session_files(
        self,
        collection_id: CollectionId,
        *,
        page: int = 1,
        per_page: int = 25,
        all_items: bool = False,
    ) -> dict[str, Any]:
        normalized_collection_id = _collection_id(collection_id)
        return _validated_collection_upload_file_response(
            normalized_collection_id,
            self._json(
                "GET",
                f"/v1/collection-upload-sessions/{str(normalized_collection_id)}/files",
                params={
                    "page": page,
                    "per_page": per_page,
                    "all": str(all_items).lower(),
                },
            ),
        )

    def put_collection_upload_session_provenance_journal(
        self,
        collection_id: CollectionId,
        journal_id: ProvenanceJournalId,
        *,
        content: bytes,
        sha256: str,
    ) -> dict[str, Any]:
        try:
            canonical_journal_id = _PROVENANCE_JOURNAL_ID.validate_python(
                journal_id,
                strict=True,
            )
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        return self._json(
            "PUT",
            f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/provenance/journals/"
            f"{quote(canonical_journal_id, safe='')}",
            headers={
                "Content-Type": "application/json-seq",
                "X-Riverhog-Provenance-SHA256": sha256,
            },
            content=content,
            timeout=self.upload_timeout_seconds,
        )

    def export_collection_upload_session_provenance_journal(
        self,
        collection_id: CollectionId,
        journal_id: ProvenanceJournalId,
    ) -> bytes:
        try:
            canonical_journal_id = _PROVENANCE_JOURNAL_ID.validate_python(
                journal_id,
                strict=True,
            )
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        response = self._request(
            "GET",
            f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/provenance/journals/"
            f"{quote(canonical_journal_id, safe='')}",
        )
        content = response.content
        expected = response.headers.get("ETag", "").strip().strip('"')
        if not expected or hashlib.sha256(content).hexdigest() != expected:
            raise InvalidState("staged provenance journal does not match its ETag")
        return content

    def list_collection_upload_sessions(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        state: CollectionUploadState | None = None,
        tag: str | None = None,
        sort: CollectionUploadSort = "created_at",
        order: SortOrder = "desc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": _one_of(
                sort,
                frozenset({"id", "created_at", "state", "bytes", "files"}),
                "collection-upload sort",
            ),
            "order": _one_of(order, frozenset({"asc", "desc"}), "sort order"),
        }
        if q:
            params["q"] = q
        if state:
            params["state"] = _one_of(
                state,
                frozenset({"open", "uploading", "finalizing", "orphaned", "discarding"}),
                "collection-upload state",
            )
        if tag is not None:
            params["tag"] = _canonical_tag(tag)
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/collection-upload-sessions", params=params)

    def complete_collection_upload_session(
        self,
        collection_id: CollectionId,
        *,
        files_total: int,
        content_identity: str,
        provenance_identity: str | None,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/complete",
            json={
                "files_total": files_total,
                "content_identity": content_identity,
                "provenance_identity": provenance_identity,
            },
        )

    def cancel_collection_upload_session(self, collection_id: CollectionId) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/cancel",
            timeout=_CANCEL_TIMEOUT_SECONDS,
        )

    def get_collection_upload_session(self, collection_id: CollectionId) -> dict[str, Any]:
        return self._json(
            "GET", f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}"
        )

    def heartbeat_collection_upload_session(self, collection_id: CollectionId) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/heartbeat",
        )

    def plan_collection_upload_discard(self, collection_id: CollectionId) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/discard-plan",
        )

    def discard_collection_upload(
        self,
        collection_id: CollectionId,
        *,
        challenge: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/discard",
            json={"challenge": challenge},
            timeout=_CANCEL_TIMEOUT_SECONDS,
        )

    def list_collection_upload_session_volumes(self, collection_id: CollectionId) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/volumes",
        )

    def get_collection_upload_session_volume(
        self,
        collection_id: CollectionId,
        volume_id: str,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/volumes/"
            f"{quote(volume_id, safe='')}",
        )

    def get_collection_upload_session_unit(
        self,
        collection_id: CollectionId,
        volume_id: str,
        unit: int,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/volumes/"
            f"{quote(volume_id, safe='')}/units/{str(unit)}",
        )

    def put_collection_upload_session_unit(
        self,
        collection_id: CollectionId,
        volume_id: str,
        unit: int,
        *,
        plan_sha256: str,
        content: bytes,
    ) -> dict[str, Any]:
        return self._json(
            "PUT",
            f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/volumes/"
            f"{quote(volume_id, safe='')}/units/{str(unit)}",
            headers={
                "Content-Type": "application/octet-stream",
                "If-Match": f'"{plan_sha256}"',
            },
            content=content,
            timeout=self.upload_timeout_seconds,
        )

    def search(
        self,
        query: str | None = None,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: SearchSort = "file_ref",
        order: SortOrder = "asc",
        collection: CollectionId | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, object] = {
            "page": page,
            "per_page": per_page,
            "sort": _one_of(
                sort,
                frozenset({"file_ref", "collection_id", "path", "bytes"}),
                "search sort",
            ),
            "order": _one_of(order, frozenset({"asc", "desc"}), "sort order"),
        }
        if query:
            params["q"] = query
        if collection is not None:
            params["collection"] = _collection_id(collection)
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/search", params=params)

    def get_collection(self, collection_id: CollectionId) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collections/{str(_collection_id(collection_id))}",
        )

    def list_collection_provenance(
        self,
        collection_id: CollectionId,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        status: ProvenanceStatus | None = None,
        sort: ProvenanceSort = "path",
        order: SortOrder = "asc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, object] = {
            "page": page,
            "per_page": per_page,
            "sort": _one_of(
                sort,
                frozenset({"path", "bytes", "status"}),
                "provenance sort",
            ),
            "order": _one_of(order, frozenset({"asc", "desc"}), "sort order"),
        }
        if q:
            params["q"] = q
        if status:
            params["status"] = _one_of(
                status,
                frozenset({"captured", "omitted"}),
                "provenance status",
            )
        if all_items:
            params["all"] = True
        return self._json(
            "GET",
            f"/v1/collections/{_collection_id(collection_id)}/provenance/files",
            params=params,
        )

    def get_collection_file_provenance(
        self,
        collection_id: CollectionId,
        path: str,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collections/{_collection_id(collection_id)}/provenance/files/"
            f"{quote(_canonical_relpath(path), safe='/')}",
        )

    def trace_collection_file_provenance(
        self,
        collection_id: CollectionId,
        path: str,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collections/{_collection_id(collection_id)}/provenance/trace/"
            f"{quote(_canonical_relpath(path), safe='/')}",
        )

    def export_collection_provenance_journal(
        self,
        collection_id: CollectionId,
        journal_id: ProvenanceJournalId,
    ) -> bytes:
        try:
            canonical_journal_id = _PROVENANCE_JOURNAL_ID.validate_python(
                journal_id,
                strict=True,
            )
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        response = self._request(
            "GET",
            f"/v1/collections/{_collection_id(collection_id)}/provenance/journals/"
            f"{quote(canonical_journal_id, safe='')}",
        )
        content = response.content
        expected = response.headers.get("ETag", "").strip().strip('"')
        if not expected or hashlib.sha256(content).hexdigest() != expected:
            raise InvalidState("provenance export does not match its ETag")
        return content

    def verify_collection_provenance(self, collection_id: CollectionId) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collections/{_collection_id(collection_id)}/provenance/verify",
        )

    def plan_collection_deletion(
        self,
        collection_id: CollectionId,
        *,
        retirement_claim_id: ProcessingClaimId | None = None,
    ) -> dict[str, Any]:
        params = (
            {"retirement_claim_id": _processing_claim_id(retirement_claim_id)}
            if retirement_claim_id is not None
            else None
        )
        return self._json(
            "POST",
            f"/v1/collections/{str(_collection_id(collection_id))}/deletion-plan",
            params=params,
        )

    def delete_collection(
        self,
        collection_id: CollectionId,
        *,
        challenge: str,
        retirement_claim_id: ProcessingClaimId | None = None,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"challenge": challenge}
        if retirement_claim_id is not None:
            payload["retirement_claim_id"] = _processing_claim_id(retirement_claim_id)
        if event_context is not None:
            payload["event_context"] = dict(event_context)
        return self._json(
            "POST",
            f"/v1/collections/{str(_collection_id(collection_id))}/delete",
            json=payload,
        )

    def list_collections(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        tag: str | None = None,
        encryption_format: str | None = None,
        passphrase_id: str | None = None,
        sort: CollectionSort = "id",
        order: SortOrder = "asc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
        }
        if sort != "id":
            params["sort"] = _one_of(
                sort,
                frozenset({"id", "created_at", "bytes", "files"}),
                "collection sort",
            )
        if order != "asc":
            params["order"] = _one_of(
                order,
                frozenset({"asc", "desc"}),
                "sort order",
            )
        if q:
            params["q"] = q
        if tag is not None:
            params["tag"] = _canonical_tag(tag)
        if encryption_format:
            params["encryption_format"] = encryption_format
        if passphrase_id:
            params["passphrase_id"] = passphrase_id
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/collections", params=params)

    def list_archive_stores(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        sort: ArchiveStoreSort = "store",
        order: SortOrder = "asc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": _one_of(
                sort,
                frozenset(
                    {
                        "store",
                        "read_mode",
                        "read_priority",
                        "collections",
                        "objects",
                        "stored_bytes",
                    }
                ),
                "archive-store sort",
            ),
            "order": _one_of(order, frozenset({"asc", "desc"}), "sort order"),
        }
        if q:
            params["q"] = q
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/archive/stores", params=params)

    def get_archive_store(self, store: ArchiveStoreName) -> dict[str, Any]:
        return self._json("GET", f"/v1/archive/stores/{quote(_archive_store_name(store), safe='')}")

    def list_apps(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        sort: ApplicationSort = "name",
        order: SortOrder = "asc",
        active: bool | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": _one_of(
                sort,
                frozenset({"name", "keys", "active_keys", "last_used_at"}),
                "application sort",
            ),
            "order": _one_of(order, frozenset({"asc", "desc"}), "sort order"),
        }
        if q:
            params["q"] = q
        if active is not None:
            params["active"] = str(active).lower()
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/apps", params=params)

    def create_app_key(
        self,
        app: ApplicationName,
        *,
        access: Sequence[Mapping[str, str]],
        expires_in_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"access": _application_access_payload(access)}
        if expires_in_seconds is not None:
            payload["expires_in_seconds"] = expires_in_seconds
        return self._json(
            "POST",
            f"/v1/apps/{quote(_application_name(app), safe='')}/keys",
            json=payload,
        )

    def list_app_keys(
        self,
        app: ApplicationName,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        sort: ApplicationKeySort = "created_at",
        order: SortOrder = "desc",
        active: bool | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": _one_of(
                sort,
                frozenset({"id", "created_at", "expires_at", "last_used_at"}),
                "application-key sort",
            ),
            "order": _one_of(order, frozenset({"asc", "desc"}), "sort order"),
        }
        if q:
            params["q"] = q
        if active is not None:
            params["active"] = str(active).lower()
        if all_items:
            params["all"] = True
        return self._json(
            "GET",
            f"/v1/apps/{quote(_application_name(app), safe='')}/keys",
            params=params,
        )

    def revoke_app_key(self, app: ApplicationName, key_id: ApplicationKeyId) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/apps/{quote(_application_name(app), safe='')}/keys/"
            f"{quote(_application_key_id(key_id), safe='')}/revoke",
        )

    def rotate_app_key(self, app: ApplicationName, key_id: ApplicationKeyId) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/apps/{quote(_application_name(app), safe='')}/keys/"
            f"{quote(_application_key_id(key_id), safe='')}/rotate",
        )

    def list_app_key_access(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        sort: ApplicationAccessSort = "permission",
        order: SortOrder = "asc",
        app: ApplicationName | None = None,
        key_id: ApplicationKeyId | None = None,
        permission: str | None = None,
        resource: str | None = None,
        active: bool | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": _one_of(
                sort,
                frozenset({"app", "key_id", "permission", "resource", "created_at"}),
                "application-access sort",
            ),
            "order": _one_of(order, frozenset({"asc", "desc"}), "sort order"),
        }
        if q:
            params["q"] = q
        if app is not None:
            params["app"] = _application_name(app)
        if key_id is not None:
            params["key"] = _application_key_id(key_id)
        if permission:
            params["permission"] = permission
        if resource:
            params["resource"] = resource
        if active is not None:
            params["active"] = str(active).lower()
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/app-key-access", params=params)

    def replace_app_key_access(
        self,
        app: ApplicationName,
        key_id: ApplicationKeyId,
        *,
        access: Sequence[Mapping[str, str]],
    ) -> dict[str, Any]:
        return self._json(
            "PUT",
            f"/v1/apps/{quote(_application_name(app), safe='')}/keys/"
            f"{quote(_application_key_id(key_id), safe='')}/access",
            json={"access": _application_access_payload(access)},
        )

    def add_app_key_access(
        self,
        app: ApplicationName,
        key_id: ApplicationKeyId,
        *,
        permission: ApplicationPermission,
        resource: ApplicationResource,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/apps/{quote(_application_name(app), safe='')}/keys/"
            f"{quote(_application_key_id(key_id), safe='')}/access",
            json=_application_access_grant_payload(permission, resource),
        )

    def remove_app_key_access(
        self,
        app: ApplicationName,
        key_id: ApplicationKeyId,
        *,
        permission: ApplicationPermission,
        resource: ApplicationResource,
    ) -> dict[str, Any]:
        return self._json(
            "DELETE",
            f"/v1/apps/{quote(_application_name(app), safe='')}/keys/"
            f"{quote(_application_key_id(key_id), safe='')}/access",
            json=_application_access_grant_payload(permission, resource),
        )

    def create_tag(self, tag: str) -> dict[str, Any]:
        return self._json("POST", "/v1/tags", json={"id": _canonical_tag(tag)})

    def get_tag(self, tag: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/tags/{quote(_canonical_tag(tag), safe='')}")

    def list_tags(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        sort: TagSort = "id",
        order: SortOrder = "asc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": _one_of(
                sort,
                frozenset({"id", "created_at", "collections"}),
                "tag sort",
            ),
            "order": _one_of(order, frozenset({"asc", "desc"}), "sort order"),
        }
        if q:
            params["q"] = q
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/tags", params=params)

    def plan_tag_deletion(self, tag: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/tags/{quote(_canonical_tag(tag), safe='')}/deletion-plan",
        )

    def delete_tag(self, tag: str, *, challenge: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/tags/{quote(_canonical_tag(tag), safe='')}/delete",
            json={"challenge": challenge},
        )

    def get_collection_tags(self, collection_id: CollectionId) -> dict[str, Any]:
        return self._json("GET", f"/v1/collections/{_collection_id(collection_id)}/tags")

    def replace_collection_tags(
        self,
        collection_id: CollectionId,
        tags: Sequence[str],
        *,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"tags": _canonical_tags(tags)}
        if event_context is not None:
            payload["event_context"] = dict(event_context)
        return self._json(
            "PUT", f"/v1/collections/{_collection_id(collection_id)}/tags", json=payload
        )

    def add_collection_tag(
        self,
        collection_id: CollectionId,
        tag: str,
        *,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {} if event_context is None else {"event_context": dict(event_context)}
        return self._json(
            "POST",
            f"/v1/collections/{_collection_id(collection_id)}/tags/"
            f"{quote(_canonical_tag(tag), safe='')}",
            json=payload,
        )

    def remove_collection_tag(
        self,
        collection_id: CollectionId,
        tag: str,
        *,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {} if event_context is None else {"event_context": dict(event_context)}
        return self._json(
            "DELETE",
            f"/v1/collections/{_collection_id(collection_id)}/tags/"
            f"{quote(_canonical_tag(tag), safe='')}",
            json=payload,
        )

    def get_download_quota(self) -> dict[str, Any]:
        return self._json("GET", "/v1/download-quota")

    def set_app_key_download_quota(
        self,
        app: ApplicationName,
        key_id: ApplicationKeyId,
        *,
        monthly_bytes: MonthlyDownloadQuotaBytes | None,
    ) -> dict[str, Any]:
        try:
            normalized_monthly_bytes = (
                None
                if monthly_bytes is None
                else _MONTHLY_DOWNLOAD_QUOTA_BYTES.validate_python(
                    monthly_bytes,
                    strict=True,
                )
            )
        except ValidationError as exc:
            raise BadRequest("monthly download quota must be non-negative") from exc
        return self._json(
            "PUT",
            f"/v1/apps/{quote(_application_name(app), safe='')}/keys/"
            f"{quote(_application_key_id(key_id), safe='')}/download-quota",
            json={"monthly_bytes": normalized_monthly_bytes},
        )

    def list_download_quotas(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        sort: DownloadQuotaSort = "app",
        order: SortOrder = "asc",
        app: ApplicationName | None = None,
        active: bool | None = None,
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": _one_of(
                sort,
                frozenset(
                    {
                        "app",
                        "key_id",
                        "monthly_bytes",
                        "accounted_bytes",
                        "reserved_bytes",
                        "remaining_bytes",
                    }
                ),
                "download-quota sort",
            ),
            "order": _one_of(order, frozenset({"asc", "desc"}), "sort order"),
        }
        if q:
            params["q"] = q
        if app is not None:
            params["app"] = _application_name(app)
        if active is not None:
            params["active"] = str(active).lower()
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/download-quotas", params=params)

    def create_or_resume_archive_copy(
        self,
        collection_id: CollectionId,
        *,
        destination_store: ArchiveStoreName,
        source_store: ArchiveStoreName | None = None,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            stores = ArchiveCopyStoreSelectionDocument(
                destination_store=destination_store,
                source_store=source_store,
            )
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        payload: dict[str, Any] = {
            "collection_id": _collection_id(collection_id),
            "destination_store": stores.destination_store,
        }
        if stores.source_store is not None:
            payload["source_store"] = stores.source_store
        if event_context is not None:
            payload["event_context"] = dict(event_context)
        return self._json("POST", "/v1/archive/copies", json=payload)

    def list_archive_copy_jobs(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        state: ArchiveCopyState | None = None,
        sort: ArchiveCopySort = "requested_at",
        order: SortOrder = "desc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": _one_of(
                sort,
                frozenset(
                    {
                        "collection_id",
                        "source_store",
                        "destination_store",
                        "state",
                        "requested_at",
                    }
                ),
                "archive-copy sort",
            ),
            "order": _one_of(order, frozenset({"asc", "desc"}), "sort order"),
        }
        if q:
            params["q"] = q
        if state:
            params["state"] = _one_of(
                state,
                frozenset(
                    {
                        "requested",
                        "waiting",
                        "checking",
                        "copying",
                        "canceling",
                        "completed",
                        "failed",
                        "canceled",
                    }
                ),
                "archive-copy state",
            )
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/archive/copies", params=params)

    def get_archive_copy_job(
        self,
        collection_id: CollectionId,
        *,
        destination_store: ArchiveStoreName,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/archive/copies/{_collection_id(collection_id)}/"
            f"{quote(_archive_store_name(destination_store), safe='')}",
        )

    def cancel_archive_copy_job(
        self,
        collection_id: CollectionId,
        *,
        destination_store: ArchiveStoreName,
    ) -> dict[str, Any]:
        return self._json(
            "DELETE",
            f"/v1/archive/copies/{_collection_id(collection_id)}/"
            f"{quote(_archive_store_name(destination_store), safe='')}",
        )

    def plan_archive_copy_retirement(
        self,
        collection_id: CollectionId,
        *,
        store: ArchiveStoreName,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/archive/copies/retirement-plan",
            json={
                "collection_id": _collection_id(collection_id),
                "store": _archive_store_name(store),
            },
        )

    def retire_archive_copy(
        self,
        collection_id: CollectionId,
        *,
        store: ArchiveStoreName,
        challenge: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/archive/copies/retire",
            json={
                "collection_id": _collection_id(collection_id),
                "store": _archive_store_name(store),
                "challenge": challenge,
            },
        )
