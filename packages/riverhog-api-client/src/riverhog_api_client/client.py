from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast
from urllib.parse import quote
from xml.etree import ElementTree

import httpx
from file_download import verified_download
from http_api_contracts import (
    CanonicalVisibleText,
    closed_literal_values,
    parse_error_payload,
    parse_quoted_sha256_identity,
    quote_sha256_identity,
    safe_http_base_url,
    validate_sha256_identity,
)
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from riverhog_application_access import (
    ApplicationAccessGrant,
    ApplicationAccessGrantSet,
    ApplicationKeyId,
    ApplicationName,
    MonthlyDownloadQuotaBytes,
)
from riverhog_application_access import (
    ApplicationPermission as ApplicationPermission,
)
from riverhog_application_access import (
    ApplicationResource as ApplicationResource,
)
from riverhog_protocol import (
    ApplicationAccessSort,
    ApplicationKeySort,
    ApplicationSort,
    ArchiveCopySort,
    ArchiveCopyState,
    ArchiveCopyStoreSelectionDocument,
    ArchiveStoreName,
    ArchiveStoreSort,
    CollectionAccessGroupSort,
    CollectionAccessGroupStatus,
    CollectionId,
    CollectionSort,
    CollectionUploadArtifactCustodyReceiptDocument,
    CollectionUploadCustodyMode,
    CollectionUploadFileBatchDocument,
    CollectionUploadFileIn,
    CollectionUploadProvenanceJournalCreateDocument,
    CollectionUploadProvenanceJournalStatusDocument,
    CollectionUploadRawDigestBatchDocument,
    CollectionUploadRawDigestProgressDocument,
    CollectionUploadRegistrationConstraintsDocument,
    CollectionUploadSort,
    CollectionUploadState,
    CollectionUploadUnitNumber,
    CollectionUploadUnitWorkDocument,
    CollectionUploadVolumeId,
    CollectionUploadWorkBatchDocument,
    DownloadQuotaSort,
    ImmutableFileIdentityDocument,
    PortableCollectionInventoryPage,
    ProcessingClaimId,
    ProvenanceSort,
    ProvenanceStatus,
    RetrievalCacheProtection,
    RetrievalCacheSort,
    RetrievalCacheState,
    RetrievalCacheStoreName,
    RetrievalFileReferenceSetDocument,
    SearchSort,
    SortOrder,
    validate_collection_upload_artifact_custody_receipt,
    validate_collection_upload_batch_against_registration_constraints,
)
from riverhog_protocol.errors import (
    BadRequest,
    Conflict,
    HashMismatch,
    InvalidState,
    RiverhogError,
    ServiceUnavailable,
    error_type_for_code,
)
from riverhog_protocol.lifecycle_events import LifecycleEventCursor, RiverhogEventPage
from riverhog_protocol.paths import (
    CanonicalRelPath,
    normalize_collection_id,
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
type RetrievalPlanIdempotencyKey = Annotated[
    CanonicalVisibleText,
    Field(max_length=200),
]
_PROVENANCE_JOURNAL_ID: TypeAdapter[str] = TypeAdapter(ProvenanceJournalId)
_APPLICATION_NAME: TypeAdapter[str] = TypeAdapter(ApplicationName)
_APPLICATION_KEY_ID: TypeAdapter[str] = TypeAdapter(ApplicationKeyId)
_APPLICATION_PERMISSION: TypeAdapter[str] = TypeAdapter(ApplicationPermission)
_APPLICATION_RESOURCE: TypeAdapter[str] = TypeAdapter(ApplicationResource)
_ARCHIVE_STORE_NAME: TypeAdapter[str] = TypeAdapter(ArchiveStoreName)
_RETRIEVAL_CACHE_STORE_NAME: TypeAdapter[str] = TypeAdapter(RetrievalCacheStoreName)
_COLLECTION_ID: TypeAdapter[int] = TypeAdapter(CollectionId)
_CANONICAL_RELPATH: TypeAdapter[str] = TypeAdapter(CanonicalRelPath)
_MONTHLY_DOWNLOAD_QUOTA_BYTES: TypeAdapter[int] = TypeAdapter(MonthlyDownloadQuotaBytes)
_PROCESSING_CLAIM_ID: TypeAdapter[str] = TypeAdapter(ProcessingClaimId)
_COLLECTION_UPLOAD_VOLUME_ID: TypeAdapter[str] = TypeAdapter(CollectionUploadVolumeId)
_COLLECTION_UPLOAD_UNIT_NUMBER: TypeAdapter[int] = TypeAdapter(CollectionUploadUnitNumber)
_SORT_ORDERS = closed_literal_values(SortOrder)
_COLLECTION_SORTS = closed_literal_values(CollectionSort)
_COLLECTION_UPLOAD_SORTS = closed_literal_values(CollectionUploadSort)
_COLLECTION_UPLOAD_STATES = closed_literal_values(CollectionUploadState)
_RETRIEVAL_CACHE_SORTS = closed_literal_values(RetrievalCacheSort)
_RETRIEVAL_CACHE_STATES = closed_literal_values(RetrievalCacheState)
_RETRIEVAL_CACHE_PROTECTIONS = closed_literal_values(RetrievalCacheProtection)
_SEARCH_SORTS = closed_literal_values(SearchSort)
_PROVENANCE_SORTS = closed_literal_values(ProvenanceSort)
_PROVENANCE_STATUSES = closed_literal_values(ProvenanceStatus)
_ARCHIVE_STORE_SORTS = closed_literal_values(ArchiveStoreSort)
_APPLICATION_SORTS = closed_literal_values(ApplicationSort)
_APPLICATION_KEY_SORTS = closed_literal_values(ApplicationKeySort)
_APPLICATION_ACCESS_SORTS = closed_literal_values(ApplicationAccessSort)
_COLLECTION_ACCESS_GROUP_SORTS = closed_literal_values(CollectionAccessGroupSort)
_DOWNLOAD_QUOTA_SORTS = closed_literal_values(DownloadQuotaSort)
_ARCHIVE_COPY_SORTS = closed_literal_values(ArchiveCopySort)
_ARCHIVE_COPY_STATES = closed_literal_values(ArchiveCopyState)

_COLLECTION_UPLOAD_IDEMPOTENCY_KEY: TypeAdapter[CollectionUploadIdempotencyKey] = TypeAdapter(
    CollectionUploadIdempotencyKey
)
_RETRIEVAL_PLAN_IDEMPOTENCY_KEY: TypeAdapter[RetrievalPlanIdempotencyKey] = TypeAdapter(
    RetrievalPlanIdempotencyKey
)


def _one_of(value: str, allowed: frozenset[str], label: str) -> str:
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise BadRequest(f"{label} must be one of: {choices}")
    return value


def _page_params(*, page_size: int, page_token: str | None) -> dict[str, object]:
    if page_size < 1 or page_size > 100:
        raise ValueError("page_size must be between 1 and 100")
    params: dict[str, object] = {"page_size": page_size}
    if page_token is not None:
        params["page_token"] = page_token
    return params


def _sha256_identity(value: str, label: str) -> str:
    try:
        return validate_sha256_identity(value)
    except ValueError as exc:
        raise BadRequest(f"{label} must be an exact lowercase SHA-256 identity") from exc


def _response_sha256_etag(value: str) -> str:
    try:
        return parse_quoted_sha256_identity(value)
    except ValueError as exc:
        raise InvalidState("API returned an invalid SHA-256 ETag identity") from exc


def _response_model[ResponseModelT: BaseModel](
    model: type[ResponseModelT],
    payload: object,
) -> ResponseModelT:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise InvalidState("API returned an invalid typed response document") from exc


def _collection_upload_volume_id(value: str) -> str:
    try:
        return _COLLECTION_UPLOAD_VOLUME_ID.validate_python(value, strict=True)
    except ValidationError as exc:
        raise BadRequest(str(exc)) from exc


def _collection_upload_unit_number(value: int) -> int:
    try:
        return _COLLECTION_UPLOAD_UNIT_NUMBER.validate_python(value, strict=True)
    except ValidationError as exc:
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


def _application_permission(value: str) -> str:
    try:
        return _APPLICATION_PERMISSION.validate_python(value, strict=True)
    except ValidationError as exc:
        raise BadRequest(str(exc)) from exc


def _application_resource(value: str) -> str:
    try:
        return _APPLICATION_RESOURCE.validate_python(value, strict=True)
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


def _retrieval_cache_store_name(value: str) -> str:
    try:
        return _RETRIEVAL_CACHE_STORE_NAME.validate_python(value, strict=True)
    except ValidationError as exc:
        raise BadRequest(
            "retrieval cache store name must use lowercase letters, digits, and single dashes"
        ) from exc


def _collection_id(value: int) -> int:
    try:
        return _COLLECTION_ID.validate_python(value)
    except ValidationError as exc:
        raise BadRequest("collection id must be a positive integer") from exc


def _provenance_journal_id(value: ProvenanceJournalId) -> str:
    try:
        return _PROVENANCE_JOURNAL_ID.validate_python(value, strict=True)
    except ValidationError as exc:
        raise BadRequest(str(exc)) from exc


def _canonical_relpath(value: str) -> str:
    try:
        return _CANONICAL_RELPATH.validate_python(value, strict=True)
    except ValidationError as exc:
        raise BadRequest("collection path must be canonical") from exc


def _validated_collection_upload_idempotency_key(
    value: CollectionUploadIdempotencyKey,
) -> str:
    try:
        return _COLLECTION_UPLOAD_IDEMPOTENCY_KEY.validate_python(value, strict=True)
    except ValidationError as exc:
        raise BadRequest(str(exc)) from exc


def _validated_retrieval_plan_idempotency_key(
    value: RetrievalPlanIdempotencyKey,
) -> str:
    try:
        return _RETRIEVAL_PLAN_IDEMPOTENCY_KEY.validate_python(value, strict=True)
    except ValidationError as exc:
        raise BadRequest(str(exc)) from exc


def _validated_collection_upload_file_response(
    collection_id: int,
    payload: dict[str, Any],
    *,
    expected_state: str | None = None,
) -> dict[str, Any]:
    try:
        if _COLLECTION_ID.validate_python(payload.get("collection_id")) != collection_id:
            raise ValueError("collection upload file response differs from its request")
        if expected_state is not None and payload.get("state") != expected_state:
            raise ValueError("collection upload file response has an impossible state")
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
            receipt = CollectionUploadArtifactCustodyReceiptDocument.model_validate_json(
                json.dumps(receipt_value, sort_keys=True, separators=(",", ":"))
            )
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


def _riverhog_application_access_payload(
    access: Sequence[Mapping[str, str]],
) -> list[dict[str, Any]]:
    try:
        grants = ApplicationAccessGrantSet.model_validate([dict(current) for current in access])
    except ValidationError as exc:
        raise BadRequest(str(exc)) from exc
    return cast(list[dict[str, Any]], grants.model_dump(mode="json"))


def _riverhog_application_access_grant_payload(
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

    @contextmanager
    def _stream_verified_body(
        self,
        path: str,
        *,
        media_type: str,
    ) -> Iterator[Iterator[bytes]]:
        client = self._persistent_download_client()
        try:
            with client.stream("GET", path, headers={"Accept": media_type}) as response:
                if not response.is_success:
                    response.read()
                    self._raise_for_error(response)
                returned_media_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                if returned_media_type != media_type:
                    raise InvalidState("API returned an invalid binary response media type")
                raw_length = response.headers.get("Content-Length")
                try:
                    expected_bytes = int(raw_length) if raw_length is not None else -1
                except ValueError as exc:
                    raise InvalidState("API returned an invalid Content-Length") from exc
                if expected_bytes < 0:
                    raise InvalidState("API returned no exact Content-Length")
                expected_sha256 = _response_sha256_etag(response.headers.get("ETag", ""))
                observed_bytes = 0
                digest = hashlib.sha256()
                complete = False

                def content() -> Iterator[bytes]:
                    nonlocal observed_bytes, complete
                    for chunk in response.iter_bytes(chunk_size=_DOWNLOAD_CHUNK_BYTES):
                        observed_bytes += len(chunk)
                        digest.update(chunk)
                        yield chunk
                    if observed_bytes != expected_bytes:
                        raise InvalidState("binary response differs from its Content-Length")
                    if digest.hexdigest() != expected_sha256:
                        raise InvalidState("binary response differs from its ETag")
                    complete = True

                yield content()
                if not complete:
                    raise InvalidState("binary response was not consumed completely")
        except httpx.TransportError:
            self.close()
            raise

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
        expected_identity = _sha256_identity(expected_sha256, "expected download SHA-256")
        with client.stream(
            "GET",
            path,
            headers={"If-Match": quote_sha256_identity(expected_identity)},
        ) as response:
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
            returned_etag = _response_sha256_etag(response.headers.get("ETag", ""))
            if returned_etag != expected_identity:
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
        after: LifecycleEventCursor | None = None,
        limit: int = 100,
    ) -> RiverhogEventPage:
        payload = self._json(
            "GET",
            "/v1/events",
            params={"after": "0" if after is None else after, "limit": limit},
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
                location.split("/v1/catalog/collections/", 1)[-1].rsplit("/inventory", 1)[0]
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
                loc.split("/v1/catalog/collections/", 1)[-1].rsplit("/inventory", 1)[0]
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

    def get_portable_collection_inventory(
        self,
        collection_id: CollectionId,
        *,
        cursor: str | None = None,
        limit: int = 100,
        inventory_identity: str | None = None,
    ) -> PortableCollectionInventoryPage:
        if cursor is not None and inventory_identity is None:
            raise BadRequest("inventory continuation requires its exact identity")
        headers = (
            {"If-Match": quote_sha256_identity(_sha256_identity(inventory_identity, "inventory"))}
            if inventory_identity is not None
            else None
        )
        params: dict[str, object] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = self._request(
            "GET",
            f"/v1/catalog/collections/{str(_collection_id(collection_id))}/inventory",
            params=params,
            headers=headers,
        )
        page = _response_model(PortableCollectionInventoryPage, response.json())
        try:
            response_identity = parse_quoted_sha256_identity(response.headers.get("ETag", ""))
        except ValueError as exc:
            raise InvalidState("API returned an invalid collection inventory identity") from exc
        if page.authority.inventory_identity != response_identity:
            raise InvalidState("collection inventory HTTP identity differs")
        return page

    def plan_retrieval(
        self,
        files: Sequence[tuple[int, str]],
        *,
        idempotency_key: RetrievalPlanIdempotencyKey | None = None,
        lease_seconds: int | None = None,
        restore_policy: RestorePolicy = "allow",
    ) -> dict[str, Any]:
        validated_restore_policy = _restore_policy(restore_policy)
        payload: dict[str, Any] = {
            "files": _file_selections_payload(files),
            "idempotency_key": _validated_retrieval_plan_idempotency_key(
                secrets.token_hex(16) if idempotency_key is None else idempotency_key
            ),
            "restore_policy": validated_restore_policy,
        }
        if lease_seconds is not None:
            payload["lease_seconds"] = lease_seconds
        plan = self._json("POST", "/v1/retrieval-plans", json=payload)
        while plan["state"] == "planning":
            plan = self.advance_retrieval_plan(str(plan["id"]))
        if plan["state"] != "ready":
            failure = str(plan.get("failure") or plan["state"])
            raise InvalidState(f"retrieval plan did not become ready: {failure}")
        return plan

    def get_retrieval_plan(self, plan_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/retrieval-plans/{quote(plan_id, safe='')}")

    def advance_retrieval_plan(self, plan_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/retrieval-plans/{quote(plan_id, safe='')}/advance")

    def list_retrieval_plan_files(
        self,
        plan_id: str,
        *,
        plan_etag: str,
        start_ordinal: int = 0,
        page_size: int = 100,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/retrieval-plans/{quote(plan_id, safe='')}/files",
            params={"start_ordinal": start_ordinal, "page_size": page_size},
            headers={"If-Match": quote_sha256_identity(_sha256_identity(plan_etag, "plan ETag"))},
        )

    def create_retrieval_job(
        self,
        plan_id: str,
        *,
        plan_etag: str,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"plan_id": plan_id}
        if event_context is not None:
            payload["event_context"] = dict(event_context)
        return self._json(
            "POST",
            "/v1/retrieval-jobs",
            json=payload,
            headers={"If-Match": quote_sha256_identity(_sha256_identity(plan_etag, "plan ETag"))},
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
        page_size: int = 25,
        page_token: str | None = None,
        q: str | None = None,
        collection_id: CollectionId | None = None,
        source_store: ArchiveStoreName | None = None,
        cache_store: RetrievalCacheStoreName | None = None,
        state: RetrievalCacheState | None = None,
        protection: RetrievalCacheProtection | None = None,
        expires_before: str | None = None,
        expires_after: str | None = None,
        sort: RetrievalCacheSort = "cached_at",
        order: SortOrder = "desc",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page_size": page_size,
            "sort": _one_of(
                sort,
                _RETRIEVAL_CACHE_SORTS,
                "retrieval-cache sort",
            ),
            "order": _one_of(order, _SORT_ORDERS, "sort order"),
        }
        if page_token is not None:
            params["page_token"] = page_token
        if q is not None:
            params["q"] = q
        if collection_id is not None:
            params["collection_id"] = _collection_id(collection_id)
        if source_store is not None:
            params["source_store"] = _archive_store_name(source_store)
        if cache_store is not None:
            params["cache_store"] = _retrieval_cache_store_name(cache_store)
        if state:
            params["state"] = _one_of(
                state,
                _RETRIEVAL_CACHE_STATES,
                "retrieval-cache state",
            )
        if protection:
            params["protection"] = _one_of(
                protection,
                _RETRIEVAL_CACHE_PROTECTIONS,
                "retrieval-cache protection",
            )
        if expires_before:
            params["expires_before"] = expires_before
        if expires_after:
            params["expires_after"] = expires_after
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
        digest = _sha256_identity(expected_sha256, "expected retrieval SHA-256")
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
        headers: dict[str, str] = {
            "Accept-Encoding": "identity",
            "If-Match": quote_sha256_identity(digest),
        }
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
                returned_etag = _response_sha256_etag(response.headers.get("ETag", ""))
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
            expected_state="open",
        )

    def list_collection_upload_session_files(
        self,
        collection_id: CollectionId,
        *,
        page_size: int = 25,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        normalized_collection_id = _collection_id(collection_id)
        return _validated_collection_upload_file_response(
            normalized_collection_id,
            self._json(
                "GET",
                f"/v1/collection-upload-sessions/{str(normalized_collection_id)}/files",
                params=_page_params(page_size=page_size, page_token=page_token),
            ),
        )

    def register_collection_upload_session_raw_part_digests(
        self,
        collection_id: CollectionId,
        batch: CollectionUploadRawDigestBatchDocument | Mapping[str, Any],
    ) -> CollectionUploadRawDigestProgressDocument:
        try:
            document = (
                batch
                if isinstance(batch, CollectionUploadRawDigestBatchDocument)
                else CollectionUploadRawDigestBatchDocument.model_validate(dict(batch))
            )
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        payload = self._json(
            "POST",
            f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/raw-part-digests",
            json=document.model_dump(mode="json"),
        )
        return CollectionUploadRawDigestProgressDocument.model_validate(payload)

    def create_collection_upload_session_provenance_journal(
        self,
        collection_id: CollectionId,
        journal_id: ProvenanceJournalId,
        *,
        byte_count: int,
        sha256: str,
    ) -> CollectionUploadProvenanceJournalStatusDocument:
        canonical_journal_id = _provenance_journal_id(journal_id)
        if isinstance(byte_count, bool) or byte_count < 1:
            raise BadRequest("provenance byte count must be positive")
        return CollectionUploadProvenanceJournalStatusDocument.model_validate(
            self._json(
                "PUT",
                f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/provenance/journals/"
                f"{quote(canonical_journal_id, safe='')}",
                json=CollectionUploadProvenanceJournalCreateDocument(
                    bytes=byte_count,
                    sha256=_sha256_identity(sha256, "provenance SHA-256"),
                ).model_dump(mode="json"),
            )
        )

    def append_collection_upload_session_provenance_journal(
        self,
        collection_id: CollectionId,
        journal_id: ProvenanceJournalId,
        *,
        offset: int,
        content: bytes,
    ) -> CollectionUploadProvenanceJournalStatusDocument:
        canonical_journal_id = _provenance_journal_id(journal_id)
        chunk = bytes(content)
        if offset < 0 or not chunk or len(chunk) > 1024 * 1024:
            raise BadRequest("provenance append is outside its bounded transport contract")
        return CollectionUploadProvenanceJournalStatusDocument.model_validate(
            self._json(
                "PATCH",
                f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/provenance/journals/"
                f"{quote(canonical_journal_id, safe='')}",
                headers={
                    "Content-Type": "application/json-seq",
                    "Content-Length": str(len(chunk)),
                    "Upload-Offset": str(offset),
                },
                content=chunk,
                timeout=self.upload_timeout_seconds,
            )
        )

    def seal_collection_upload_session_provenance_journal(
        self,
        collection_id: CollectionId,
        journal_id: ProvenanceJournalId,
    ) -> CollectionUploadProvenanceJournalStatusDocument:
        canonical_journal_id = _provenance_journal_id(journal_id)
        return CollectionUploadProvenanceJournalStatusDocument.model_validate(
            self._json(
                "POST",
                f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/provenance/journals/"
                f"{quote(canonical_journal_id, safe='')}/seal",
            )
        )

    def get_collection_upload_session_provenance_journal(
        self,
        collection_id: CollectionId,
        journal_id: ProvenanceJournalId,
    ) -> CollectionUploadProvenanceJournalStatusDocument:
        canonical_journal_id = _provenance_journal_id(journal_id)
        return CollectionUploadProvenanceJournalStatusDocument.model_validate(
            self._json(
                "GET",
                f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/provenance/journals/"
                f"{quote(canonical_journal_id, safe='')}",
            )
        )

    def upload_collection_upload_session_provenance_journal(
        self,
        collection_id: CollectionId,
        journal_id: ProvenanceJournalId,
        *,
        content: Iterable[bytes],
        byte_count: int,
        sha256: str,
    ) -> CollectionUploadProvenanceJournalStatusDocument:
        status = self.create_collection_upload_session_provenance_journal(
            collection_id,
            journal_id,
            byte_count=byte_count,
            sha256=sha256,
        )
        skip = status.accepted_bytes
        offset = 0
        buffer = bytearray()
        for source in content:
            buffer.extend(bytes(source))
            while len(buffer) >= 1024 * 1024:
                current = bytes(buffer[: 1024 * 1024])
                del buffer[: 1024 * 1024]
                if offset + len(current) > skip:
                    start = max(0, skip - offset)
                    status = self.append_collection_upload_session_provenance_journal(
                        collection_id,
                        journal_id,
                        offset=offset + start,
                        content=current[start:],
                    )
                offset += len(current)
        if buffer:
            current = bytes(buffer)
            if offset + len(current) > skip:
                start = max(0, skip - offset)
                status = self.append_collection_upload_session_provenance_journal(
                    collection_id,
                    journal_id,
                    offset=offset + start,
                    content=current[start:],
                )
            offset += len(current)
        if offset != byte_count or status.accepted_bytes != byte_count:
            raise BadRequest("provenance content differs from its declared byte count")
        status = self.seal_collection_upload_session_provenance_journal(
            collection_id,
            journal_id,
        )
        while status.state == "validating":
            status = self.seal_collection_upload_session_provenance_journal(
                collection_id,
                journal_id,
            )
        if status.state != "sealed":
            raise Conflict(status.failure or "provenance journal validation failed")
        return status

    def list_collection_upload_sessions(
        self,
        *,
        page_size: int = 25,
        page_token: str | None = None,
        q: str | None = None,
        state: CollectionUploadState | None = None,
        sort: CollectionUploadSort = "created_at",
        order: SortOrder = "desc",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page_size": page_size,
            "sort": _one_of(
                sort,
                _COLLECTION_UPLOAD_SORTS,
                "collection-upload sort",
            ),
            "order": _one_of(order, _SORT_ORDERS, "sort order"),
        }
        if page_token is not None:
            params["page_token"] = page_token
        if q is not None:
            params["q"] = q
        if state:
            params["state"] = _one_of(
                state,
                _COLLECTION_UPLOAD_STATES,
                "collection-upload state",
            )
        return self._json("GET", "/v1/collection-upload-sessions", params=params)

    def complete_collection_upload_session(
        self,
        collection_id: CollectionId,
        *,
        files_total: int,
        content_identity: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/complete",
            json={
                "files_total": files_total,
                "content_identity": content_identity,
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

    def acquire_collection_upload_session_work(
        self,
        collection_id: CollectionId,
        *,
        limit: int = 16,
    ) -> CollectionUploadWorkBatchDocument:
        return _response_model(
            CollectionUploadWorkBatchDocument,
            self._json(
                "GET",
                f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/work",
                params={"limit": limit},
            ),
        )

    def get_collection_upload_session_unit(
        self,
        collection_id: CollectionId,
        volume_id: CollectionUploadVolumeId,
        unit: CollectionUploadUnitNumber,
    ) -> CollectionUploadUnitWorkDocument:
        return _response_model(
            CollectionUploadUnitWorkDocument,
            self._json(
                "GET",
                f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/volumes/"
                f"{quote(_collection_upload_volume_id(volume_id), safe='')}/units/"
                f"{str(_collection_upload_unit_number(unit))}",
            ),
        )

    def put_collection_upload_session_unit(
        self,
        collection_id: CollectionId,
        volume_id: CollectionUploadVolumeId,
        unit: CollectionUploadUnitNumber,
        *,
        plan_sha256: str,
        content: bytes,
    ) -> CollectionUploadUnitWorkDocument:
        normalized_volume_id = _collection_upload_volume_id(volume_id)
        normalized_unit = _collection_upload_unit_number(unit)
        return _response_model(
            CollectionUploadUnitWorkDocument,
            self._json(
                "PUT",
                f"/v1/collection-upload-sessions/{str(_collection_id(collection_id))}/volumes/"
                f"{quote(normalized_volume_id, safe='')}/units/{str(normalized_unit)}",
                headers={
                    "Content-Type": "application/octet-stream",
                    "If-Match": quote_sha256_identity(
                        _sha256_identity(plan_sha256, "upload plan SHA-256")
                    ),
                },
                content=content,
                timeout=self.upload_timeout_seconds,
            ),
        )

    def search(
        self,
        query: str | None = None,
        *,
        page_size: int = 25,
        page_token: str | None = None,
        sort: SearchSort = "file_ref",
        order: SortOrder = "asc",
        collection: CollectionId | None = None,
    ) -> dict[str, Any]:
        params: dict[str, object] = {
            "page_size": page_size,
            "sort": _one_of(
                sort,
                _SEARCH_SORTS,
                "search sort",
            ),
            "order": _one_of(order, _SORT_ORDERS, "sort order"),
        }
        if page_token is not None:
            params["page_token"] = page_token
        if query is not None:
            params["q"] = query
        if collection is not None:
            params["collection"] = _collection_id(collection)
        return self._json("GET", "/v1/search", params=params)

    def get_collection(self, collection_id: CollectionId) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collections/{str(_collection_id(collection_id))}",
        )

    def list_collection_archive_copies(
        self,
        collection_id: CollectionId,
        *,
        page_size: int = 25,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collections/{_collection_id(collection_id)}/archive-copies",
            params=_page_params(page_size=page_size, page_token=page_token),
        )

    def list_collection_provenance(
        self,
        collection_id: CollectionId,
        *,
        page_size: int = 25,
        page_token: str | None = None,
        q: str | None = None,
        status: ProvenanceStatus | None = None,
        sort: ProvenanceSort = "path",
        order: SortOrder = "asc",
    ) -> dict[str, Any]:
        params: dict[str, object] = {
            "page_size": page_size,
            "sort": _one_of(
                sort,
                _PROVENANCE_SORTS,
                "provenance sort",
            ),
            "order": _one_of(order, _SORT_ORDERS, "sort order"),
        }
        if page_token is not None:
            params["page_token"] = page_token
        if q is not None:
            params["q"] = q
        if status:
            params["status"] = _one_of(
                status,
                _PROVENANCE_STATUSES,
                "provenance status",
            )
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
        *,
        page_size: int = 25,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collections/{_collection_id(collection_id)}/provenance/trace/"
            f"{quote(_canonical_relpath(path), safe='/')}",
            params=_page_params(page_size=page_size, page_token=page_token),
        )

    @contextmanager
    def stream_collection_provenance_journal(
        self,
        collection_id: CollectionId,
        journal_id: ProvenanceJournalId,
        *,
        start: int = 0,
        end: int | None = None,
        expected_bytes: int | None = None,
        expected_sha256: str | None = None,
        chunk_size: int = _DOWNLOAD_CHUNK_BYTES,
    ) -> Iterator[Iterator[bytes]]:
        """Stream one exact journal or an identity-bound resumable byte range."""

        canonical_collection_id = _collection_id(collection_id)
        try:
            canonical_journal_id = _PROVENANCE_JOURNAL_ID.validate_python(
                journal_id,
                strict=True,
            )
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        path = (
            f"/v1/collections/{canonical_collection_id}/provenance/journals/"
            f"{quote(canonical_journal_id, safe='')}"
        )
        if expected_bytes is None or expected_sha256 is None:
            metadata_bytes, metadata_sha256 = self.collection_provenance_journal_metadata(
                canonical_collection_id,
                canonical_journal_id,
            )
            if expected_bytes is None:
                expected_bytes = metadata_bytes
            elif expected_bytes != metadata_bytes:
                raise InvalidState("provenance journal byte count changed")
            if expected_sha256 is None:
                expected_sha256 = metadata_sha256
            elif (
                _sha256_identity(
                    expected_sha256,
                    "expected provenance journal SHA-256",
                )
                != metadata_sha256
            ):
                raise InvalidState("provenance journal identity changed")
        if isinstance(expected_bytes, bool) or expected_bytes < 1:
            raise ValueError("expected provenance journal bytes must be positive")
        digest = _sha256_identity(
            expected_sha256,
            "expected provenance journal SHA-256",
        )
        resolved_end = expected_bytes if end is None else end
        if (
            isinstance(start, bool)
            or isinstance(resolved_end, bool)
            or start < 0
            or resolved_end <= start
            or resolved_end > expected_bytes
        ):
            raise ValueError("provenance journal byte range is invalid")
        if chunk_size < 1:
            raise ValueError("provenance journal stream chunk size must be positive")
        partial = start != 0 or resolved_end != expected_bytes
        headers = {
            "Accept": "application/json-seq",
            "Accept-Encoding": "identity",
        }
        if partial:
            headers["Range"] = f"bytes={start}-{resolved_end - 1}"
            headers["If-Match"] = quote_sha256_identity(digest)
        client = self._persistent_download_client()
        try:
            with client.stream("GET", path, headers=headers) as response:
                if not response.is_success:
                    response.read()
                    self._raise_for_error(response)
                expected_status = 206 if partial else 200
                if response.status_code != expected_status:
                    raise InvalidState(
                        "provenance journal returned an unexpected HTTP status: "
                        f"{response.status_code}"
                    )
                returned_media_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                if returned_media_type != "application/json-seq":
                    raise InvalidState("API returned an invalid provenance journal media type")
                if _response_sha256_etag(response.headers.get("ETag", "")) != digest:
                    raise InvalidState("provenance journal ETag does not match its authority")
                expected_length = resolved_end - start
                try:
                    returned_length = int(response.headers.get("Content-Length", ""))
                except ValueError as exc:
                    raise InvalidState(
                        "provenance journal returned an invalid Content-Length"
                    ) from exc
                if returned_length != expected_length:
                    raise InvalidState(
                        "provenance journal Content-Length differs from the requested range"
                    )
                if partial:
                    expected_range = f"bytes {start}-{resolved_end - 1}/{expected_bytes}"
                    if response.headers.get("Content-Range") != expected_range:
                        raise InvalidState("provenance journal Content-Range is inconsistent")
                returned = 0
                hasher = hashlib.sha256() if not partial else None

                def content() -> Iterator[bytes]:
                    nonlocal returned
                    for chunk in response.iter_bytes(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        returned += len(chunk)
                        if returned > expected_length:
                            raise InvalidState(
                                "provenance journal exceeded the requested byte range"
                            )
                        if hasher is not None:
                            hasher.update(chunk)
                        yield chunk

                yield content()
                if returned != expected_length:
                    raise InvalidState("provenance journal ended before the requested byte range")
                if hasher is not None and hasher.hexdigest() != digest:
                    raise HashMismatch("provenance journal SHA-256 verification failed")
        except httpx.TransportError as exc:
            self.close()
            raise ServiceUnavailable("provenance journal stream was interrupted") from exc

    def collection_provenance_journal_metadata(
        self,
        collection_id: CollectionId,
        journal_id: ProvenanceJournalId,
    ) -> tuple[int, str]:
        """Return the exact immutable byte count and SHA-256 for one journal."""

        try:
            canonical_journal_id = _PROVENANCE_JOURNAL_ID.validate_python(
                journal_id,
                strict=True,
            )
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        response = self._request(
            "HEAD",
            f"/v1/collections/{_collection_id(collection_id)}/provenance/journals/"
            f"{quote(canonical_journal_id, safe='')}",
            headers={"Accept": "application/json-seq", "Accept-Encoding": "identity"},
        )
        try:
            byte_count = int(response.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise InvalidState("provenance journal returned an invalid Content-Length") from exc
        if byte_count < 1:
            raise InvalidState("provenance journal returned an invalid byte count")
        return byte_count, _response_sha256_etag(response.headers.get("ETag", ""))

    def download_collection_provenance_journal(
        self,
        collection_id: CollectionId,
        journal_id: ProvenanceJournalId,
        *,
        output: Path,
    ) -> tuple[int, str]:
        """Download one exact journal with a durable identity-bound continuation."""

        byte_count, sha256 = self.collection_provenance_journal_metadata(
            collection_id,
            journal_id,
        )
        destination = Path(output)
        if not destination.parent.is_dir():
            raise ValueError(f"output parent directory does not exist: {destination.parent}")
        partial = destination.with_name(f".{destination.name}.part")
        checkpoint = destination.with_name(f".{destination.name}.part.json")
        expected_checkpoint = {
            "collection_id": _collection_id(collection_id),
            "journal_id": str(journal_id),
            "bytes": byte_count,
            "sha256": sha256,
        }
        resumable = False
        if partial.is_file() and checkpoint.is_file():
            try:
                resumable = (
                    json.loads(checkpoint.read_text(encoding="utf-8")) == expected_checkpoint
                )
            except (OSError, json.JSONDecodeError):
                resumable = False
        if not resumable:
            partial.unlink(missing_ok=True)
            checkpoint.unlink(missing_ok=True)
            checkpoint.write_text(
                json.dumps(expected_checkpoint, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
        start = partial.stat().st_size if partial.exists() else 0
        if start > byte_count:
            partial.unlink()
            start = 0
        if start < byte_count:
            with self.stream_collection_provenance_journal(
                collection_id,
                journal_id,
                start=start,
                expected_bytes=byte_count,
                expected_sha256=sha256,
            ) as chunks:
                with partial.open("ab") as handle:
                    for chunk in chunks:
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
        digest = hashlib.sha256()
        observed = 0
        with partial.open("rb") as handle:
            while chunk := handle.read(_DOWNLOAD_CHUNK_BYTES):
                digest.update(chunk)
                observed += len(chunk)
        if observed != byte_count or digest.hexdigest() != sha256:
            raise HashMismatch("downloaded provenance journal differs from its authority")
        os.replace(partial, destination)
        checkpoint.unlink(missing_ok=True)
        return byte_count, sha256

    def list_collection_provenance_journal_agents(
        self,
        collection_id: CollectionId,
        journal_id: ProvenanceJournalId,
        *,
        page_size: int = 25,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        try:
            canonical_journal_id = _PROVENANCE_JOURNAL_ID.validate_python(
                journal_id,
                strict=True,
            )
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        return self._json(
            "GET",
            f"/v1/collections/{_collection_id(collection_id)}/provenance/journals/"
            f"{quote(canonical_journal_id, safe='')}/agents",
            params=_page_params(page_size=page_size, page_token=page_token),
        )

    def request_collection_provenance_verification(
        self, collection_id: CollectionId
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collections/{_collection_id(collection_id)}/provenance/verification",
        )

    def get_collection_provenance_verification(self, collection_id: CollectionId) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collections/{_collection_id(collection_id)}/provenance/verification",
        )

    def cancel_collection_provenance_verification(
        self, collection_id: CollectionId
    ) -> dict[str, Any]:
        return self._json(
            "DELETE",
            f"/v1/collections/{_collection_id(collection_id)}/provenance/verification",
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
        page_size: int = 25,
        page_token: str | None = None,
        q: str | None = None,
        encryption_format: str | None = None,
        passphrase_id: str | None = None,
        sort: CollectionSort = "id",
        order: SortOrder = "asc",
    ) -> dict[str, Any]:
        params: dict[str, Any] = _page_params(page_size=page_size, page_token=page_token)
        if sort != "id":
            params["sort"] = _one_of(
                sort,
                _COLLECTION_SORTS,
                "collection sort",
            )
        if order != "asc":
            params["order"] = _one_of(
                order,
                _SORT_ORDERS,
                "sort order",
            )
        if q is not None:
            params["q"] = q
        if encryption_format:
            params["encryption_format"] = encryption_format
        if passphrase_id:
            params["passphrase_id"] = passphrase_id
        return self._json("GET", "/v1/collections", params=params)

    def list_archive_stores(
        self,
        *,
        page_size: int = 25,
        page_token: str | None = None,
        q: str | None = None,
        sort: ArchiveStoreSort = "store",
        order: SortOrder = "asc",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            **_page_params(page_size=page_size, page_token=page_token),
            "sort": _one_of(
                sort,
                _ARCHIVE_STORE_SORTS,
                "archive-store sort",
            ),
            "order": _one_of(order, _SORT_ORDERS, "sort order"),
        }
        if q is not None:
            params["q"] = q
        return self._json("GET", "/v1/archive/stores", params=params)

    def get_archive_store(self, store: ArchiveStoreName) -> dict[str, Any]:
        return self._json("GET", f"/v1/archive/stores/{quote(_archive_store_name(store), safe='')}")

    def list_apps(
        self,
        *,
        page_size: int = 25,
        page_token: str | None = None,
        q: str | None = None,
        sort: ApplicationSort = "name",
        order: SortOrder = "asc",
        active: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            **_page_params(page_size=page_size, page_token=page_token),
            "sort": _one_of(
                sort,
                _APPLICATION_SORTS,
                "application sort",
            ),
            "order": _one_of(order, _SORT_ORDERS, "sort order"),
        }
        if q is not None:
            params["q"] = q
        if active is not None:
            params["active"] = str(active).lower()
        return self._json("GET", "/v1/apps", params=params)

    def create_app_key(
        self,
        app: ApplicationName,
        *,
        access: Sequence[Mapping[str, str]],
        expires_in_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"access": _riverhog_application_access_payload(access)}
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
        page_size: int = 25,
        page_token: str | None = None,
        q: str | None = None,
        sort: ApplicationKeySort = "created_at",
        order: SortOrder = "desc",
        active: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            **_page_params(page_size=page_size, page_token=page_token),
            "sort": _one_of(
                sort,
                _APPLICATION_KEY_SORTS,
                "application-key sort",
            ),
            "order": _one_of(order, _SORT_ORDERS, "sort order"),
        }
        if q is not None:
            params["q"] = q
        if active is not None:
            params["active"] = str(active).lower()
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
        page_size: int = 25,
        page_token: str | None = None,
        q: str | None = None,
        sort: ApplicationAccessSort = "permission",
        order: SortOrder = "asc",
        app: ApplicationName | None = None,
        key_id: ApplicationKeyId | None = None,
        permission: ApplicationPermission | None = None,
        resource: ApplicationResource | None = None,
        active: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            **_page_params(page_size=page_size, page_token=page_token),
            "sort": _one_of(
                sort,
                _APPLICATION_ACCESS_SORTS,
                "application-access sort",
            ),
            "order": _one_of(order, _SORT_ORDERS, "sort order"),
        }
        if q is not None:
            params["q"] = q
        if app is not None:
            params["app"] = _application_name(app)
        if key_id is not None:
            params["key"] = _application_key_id(key_id)
        if permission:
            params["permission"] = _application_permission(permission)
        if resource:
            params["resource"] = _application_resource(resource)
        if active is not None:
            params["active"] = str(active).lower()
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
            json={"access": _riverhog_application_access_payload(access)},
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
            json=_riverhog_application_access_grant_payload(permission, resource),
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
            json=_riverhog_application_access_grant_payload(permission, resource),
        )

    def create_collection_access_group(
        self,
        *,
        idempotency_key: str,
        display_label: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"idempotency_key": idempotency_key}
        if display_label is not None:
            payload["display_label"] = display_label
        return self._json("POST", "/v1/collection-access-groups", json=payload)

    def get_collection_access_group(self, group_id: str) -> dict[str, Any]:
        return self._json(
            "GET", f"/v1/collection-access-groups/{_sha256_identity(group_id, 'group id')}"
        )

    def list_collection_access_groups(
        self,
        *,
        page_size: int = 25,
        page_token: str | None = None,
        q: str | None = None,
        status: CollectionAccessGroupStatus | None = None,
        sort: CollectionAccessGroupSort = "id",
        order: SortOrder = "asc",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            **_page_params(page_size=page_size, page_token=page_token),
            "sort": _one_of(
                sort,
                _COLLECTION_ACCESS_GROUP_SORTS,
                "collection-access-group sort",
            ),
            "order": _one_of(order, _SORT_ORDERS, "sort order"),
        }
        if q is not None:
            params["q"] = q
        if status is not None:
            params["status"] = _one_of(
                status,
                frozenset({"active", "disabled"}),
                "collection-access-group status",
            )
        return self._json("GET", "/v1/collection-access-groups", params=params)

    def update_collection_access_group(
        self,
        group_id: str,
        *,
        display_label: str | None,
        status: CollectionAccessGroupStatus,
    ) -> dict[str, Any]:
        return self._json(
            "PUT",
            f"/v1/collection-access-groups/{_sha256_identity(group_id, 'group id')}",
            json={"display_label": display_label, "status": status},
        )

    def list_collection_access_group_members(
        self,
        group_id: str,
        *,
        page_size: int = 25,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collection-access-groups/{_sha256_identity(group_id, 'group id')}/collections",
            params=_page_params(page_size=page_size, page_token=page_token),
        )

    def list_collection_access_groups_for_collection(
        self,
        collection_id: CollectionId,
        *,
        page_size: int = 25,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collections/{_collection_id(collection_id)}/access-groups",
            params=_page_params(page_size=page_size, page_token=page_token),
        )

    def add_collection_access_group_member(
        self,
        group_id: str,
        collection_id: CollectionId,
    ) -> dict[str, Any]:
        return self._json(
            "PUT",
            f"/v1/collection-access-groups/{_sha256_identity(group_id, 'group id')}/collections/"
            f"{_collection_id(collection_id)}",
        )

    def remove_collection_access_group_member(
        self,
        group_id: str,
        collection_id: CollectionId,
    ) -> dict[str, Any]:
        return self._json(
            "DELETE",
            f"/v1/collection-access-groups/{_sha256_identity(group_id, 'group id')}/collections/"
            f"{_collection_id(collection_id)}",
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
        page_size: int = 25,
        page_token: str | None = None,
        q: str | None = None,
        sort: DownloadQuotaSort = "app",
        order: SortOrder = "asc",
        app: ApplicationName | None = None,
        active: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            **_page_params(page_size=page_size, page_token=page_token),
            "sort": _one_of(
                sort,
                _DOWNLOAD_QUOTA_SORTS,
                "download-quota sort",
            ),
            "order": _one_of(order, _SORT_ORDERS, "sort order"),
        }
        if q is not None:
            params["q"] = q
        if app is not None:
            params["app"] = _application_name(app)
        if active is not None:
            params["active"] = str(active).lower()
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
        page_size: int = 25,
        page_token: str | None = None,
        q: str | None = None,
        state: ArchiveCopyState | None = None,
        sort: ArchiveCopySort = "requested_at",
        order: SortOrder = "desc",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            **_page_params(page_size=page_size, page_token=page_token),
            "sort": _one_of(
                sort,
                _ARCHIVE_COPY_SORTS,
                "archive-copy sort",
            ),
            "order": _one_of(order, _SORT_ORDERS, "sort order"),
        }
        if q is not None:
            params["q"] = q
        if state:
            params["state"] = _one_of(
                state,
                _ARCHIVE_COPY_STATES,
                "archive-copy state",
            )
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
