from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from riverhog_api_client.client import ApiClient
from riverhog_protocol import (
    CollectionUploadArtifactCustodyReceiptDocument,
    CollectionUploadCustodyObjectDocument,
)
from riverhog_protocol.errors import (
    BadRequest,
    DownloadAllowanceExceeded,
    Forbidden,
    InvalidState,
    ServiceUnavailable,
    Unauthorized,
)

UPLOAD_REGISTRATION_CONSTRAINTS = {
    "pack_member_bytes": 1024,
    "raw_part_plaintext_bytes": 65536,
}


class RecordingClient(ApiClient):
    def __init__(self) -> None:
        super().__init__(base_url="https://example.invalid")
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, path, kwargs))
        payload: dict[str, Any] = {"ok": True}
        if method == "POST" and path.endswith("/files"):
            payload = {
                "collection_id": int(path.split("/")[-2]),
                "state": "open",
                "files": [{**item, "custody_receipt": None} for item in kwargs["json"]["files"]],
            }
        return httpx.Response(200, json=payload, request=httpx.Request(method, path))


class WrongCustodyReceiptClient(RecordingClient):
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = super()._request(method, path, **kwargs)
        if method != "POST" or not path.endswith("/files"):
            return response
        payload = response.json()
        row = payload["files"][0]
        row["custody_receipt"] = CollectionUploadArtifactCustodyReceiptDocument.seal(
            collection_id=int(payload["collection_id"]),
            path="other.txt",
            bytes=int(row["bytes"]),
            sha256=str(row["sha256"]),
            archive_objects=(
                CollectionUploadCustodyObjectDocument(
                    volume_id="raw-000000000001",
                    sealed_receipt_sha256="b" * 64,
                ),
            ),
        ).model_dump(mode="json")
        return httpx.Response(200, json=payload, request=httpx.Request(method, path))


class ImpossibleRegistrationStateClient(RecordingClient):
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = super()._request(method, path, **kwargs)
        if method != "POST" or not path.endswith("/files"):
            return response
        payload = response.json()
        payload["state"] = "uploading"
        return httpx.Response(200, json=payload, request=httpx.Request(method, path))


def test_client_host_header_environment_reaches_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIVERHOG_HOST_HEADER", "archive.internal")
    client = ApiClient(base_url="https://example.invalid")
    request_client = client._make_client(timeout_seconds=client.timeout_seconds)
    try:
        assert client.host_header == "archive.internal"
        assert request_client.headers["host"] == "archive.internal"
    finally:
        request_client.close()
        client.close()


def test_client_download_timeout_environment_reaches_download_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_DOWNLOAD_TIMEOUT_SECONDS", "47")
    client = ApiClient(base_url="https://example.invalid")
    try:
        assert client._persistent_download_client().timeout.read == 47
    finally:
        client.close()


@pytest.mark.parametrize(
    ("code", "error_type", "status"),
    [
        ("unauthorized", Unauthorized, 400),
        ("forbidden", Forbidden, 400),
        ("download_allowance_exceeded", DownloadAllowanceExceeded, 429),
    ],
)
def test_client_preserves_actionable_api_error_types(
    code: str,
    error_type: type[Exception],
    status: int,
) -> None:
    client = ApiClient(base_url="https://example.invalid")
    response = httpx.Response(
        status,
        json={"error": {"code": code, "message": "action denied"}},
        request=httpx.Request("GET", "https://example.invalid/v1/test"),
    )

    with pytest.raises(error_type, match="action denied"):
        client._raise_for_error(response)


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_client_maps_transient_http_statuses_to_retryable_service_unavailable(status: int) -> None:
    client = ApiClient(base_url="https://example.invalid")
    response = httpx.Response(
        status,
        json={"error": {"code": "internal_error", "message": "retry later"}},
        request=httpx.Request("PUT", "https://example.invalid/v1/collection-upload-sessions/1"),
    )

    with pytest.raises(ServiceUnavailable, match="retry later"):
        client._raise_for_error(response)


def test_search_uses_current_collection_filters() -> None:
    client = RecordingClient()
    client.search(
        "tax",
        collection=1,
        sort="path",
        order="desc",
        all_items=True,
    )

    assert client.calls == [
        (
            "GET",
            "/v1/search",
            {
                "params": {
                    "page": 1,
                    "per_page": 25,
                    "sort": "path",
                    "order": "desc",
                    "q": "tax",
                    "collection": 1,
                    "all": True,
                }
            },
        )
    ]


def test_collection_upload_custody_transfer_and_operator_controls_use_exact_routes() -> None:
    client = RecordingClient()

    client.create_or_resume_collection_upload_session(
        "execution-1",
        ("derived",),
        provenance_mode="omitted",
        provenance_omission_reason="fixture",
        custody_mode="custody-transfer",
    )
    client.heartbeat_collection_upload_session(42)
    client.plan_collection_upload_discard(42)
    client.discard_collection_upload(42, challenge="discard-upload:fixture")

    assert client.calls == [
        (
            "POST",
            "/v1/collection-upload-sessions",
            {
                "json": {
                    "idempotency_key": "execution-1",
                    "tags": ["derived"],
                    "provenance_mode": "omitted",
                    "custody_mode": "custody-transfer",
                    "provenance_omission_reason": "fixture",
                }
            },
        ),
        ("POST", "/v1/collection-upload-sessions/42/heartbeat", {}),
        ("POST", "/v1/collection-upload-sessions/42/discard-plan", {}),
        (
            "POST",
            "/v1/collection-upload-sessions/42/discard",
            {
                "json": {"challenge": "discard-upload:fixture"},
                "timeout": 1800.0,
            },
        ),
    ]


def test_collection_upload_selects_archive_store_without_materialization_policy() -> None:
    client = RecordingClient()

    client.create_or_resume_collection_upload_session(
        "upload-one",
        [],
        archive_store="b2",
        provenance_mode="omitted",
        provenance_omission_reason="fixture source has no provenance",
    )
    client.register_collection_upload_session_files(
        1,
        [
            {
                "path": "one.txt",
                "bytes": 1,
                "sha256": "a" * 64,
                "provenance": {
                    "status": "omitted",
                    "omission_reason": "fixture source has no provenance",
                },
            }
        ],
        registration_constraints=UPLOAD_REGISTRATION_CONSTRAINTS,
    )
    client.complete_collection_upload_session(
        1,
        files_total=1,
        content_identity="b" * 64,
        provenance_identity=None,
    )

    assert client.calls[0][2]["json"] == {
        "idempotency_key": "upload-one",
        "tags": [],
        "archive_store": "b2",
        "provenance_mode": "omitted",
        "provenance_omission_reason": "fixture source has no provenance",
    }
    assert client.calls[1][2]["json"] == {
        "files": [
            {
                "path": "one.txt",
                "bytes": 1,
                "sha256": "a" * 64,
                "raw_parts": None,
                "provenance": {
                    "status": "omitted",
                    "omission_reason": "fixture source has no provenance",
                },
            }
        ],
    }
    assert client.calls[2][2]["json"] == {
        "files_total": 1,
        "content_identity": "b" * 64,
        "provenance_identity": None,
    }


def test_collection_upload_client_rejects_a_custody_receipt_for_another_artifact() -> None:
    client = WrongCustodyReceiptClient()

    with pytest.raises(InvalidState, match="invalid collection upload file response"):
        client.register_collection_upload_session_files(
            1,
            [
                {
                    "path": "one.txt",
                    "bytes": 1,
                    "sha256": "a" * 64,
                    "provenance": {
                        "status": "omitted",
                        "omission_reason": "fixture source has no provenance",
                    },
                }
            ],
            registration_constraints=UPLOAD_REGISTRATION_CONSTRAINTS,
        )


def test_collection_upload_client_rejects_an_impossible_registration_state() -> None:
    client = ImpossibleRegistrationStateClient()

    with pytest.raises(InvalidState, match="invalid collection upload file response"):
        client.register_collection_upload_session_files(
            1,
            [
                {
                    "path": "one.txt",
                    "bytes": 1,
                    "sha256": "a" * 64,
                    "provenance": {
                        "status": "omitted",
                        "omission_reason": "fixture source has no provenance",
                    },
                }
            ],
            registration_constraints=UPLOAD_REGISTRATION_CONSTRAINTS,
        )


def test_client_rejects_invalid_upload_provenance_before_transport() -> None:
    client = RecordingClient()

    with pytest.raises(BadRequest, match="provenance_mode"):
        client.create_or_resume_collection_upload_session(
            "upload-one",
            [],
            provenance_mode="captured",
            provenance_omission_reason="not omitted",
        )

    with pytest.raises(BadRequest):
        client.create_or_resume_collection_upload_session(" padded ", [])
    with pytest.raises(BadRequest):
        client.register_collection_upload_session_files(
            1,
            [
                {
                    "path": "camera/../clip.mp4",
                    "bytes": 1,
                    "sha256": "a" * 64,
                    "provenance": {
                        "status": "omitted",
                        "omission_reason": "source did not expose provenance",
                    },
                }
            ],
            registration_constraints=UPLOAD_REGISTRATION_CONSTRAINTS,
        )
    with pytest.raises(BadRequest, match="provenance_mode"):
        client.create_or_resume_collection_upload_session(
            "upload-one",
            [],
            provenance_mode="omitted",
        )
    with pytest.raises(BadRequest, match="provenance_mode"):
        client.create_or_resume_collection_upload_session(
            "upload-one",
            [],
            provenance_mode="obsolete",  # type: ignore[arg-type]
        )

    assert client.calls == []


def test_collection_upload_cancellation_allows_bounded_remote_cleanup() -> None:
    client = RecordingClient()

    client.cancel_collection_upload_session(1)

    assert client.calls == [
        (
            "POST",
            "/v1/collection-upload-sessions/1/cancel",
            {"timeout": 1800.0},
        )
    ]


def test_collection_deletion_carries_optional_event_context() -> None:
    client = RecordingClient()

    client.delete_collection(
        42,
        challenge="delete-challenge",
        event_context={"workflow": "direct-delete"},
    )

    assert client.calls == [
        (
            "POST",
            "/v1/collections/42/delete",
            {
                "json": {
                    "challenge": "delete-challenge",
                    "event_context": {"workflow": "direct-delete"},
                }
            },
        )
    ]


def test_retrieval_plan_and_job_share_exact_file_selection() -> None:
    client = RecordingClient()
    files = [(42, "invoice.pdf")]

    client.plan_retrieval(files, lease_seconds=3600)
    client.create_retrieval_job(files, plan_etag="a" * 64, lease_seconds=3600)

    payload = {
        "files": [
            {
                "collection_id": 42,
                "path": "invoice.pdf",
            }
        ],
        "lease_seconds": 3600,
        "restore_policy": "allow",
    }
    assert client.calls == [
        ("POST", "/v1/retrieval-plans", {"json": payload}),
        (
            "POST",
            "/v1/retrieval-jobs",
            {"json": payload, "headers": {"If-Match": '"' + "a" * 64 + '"'}},
        ),
    ]


def test_client_rejects_unknown_restore_policy_before_transport() -> None:
    client = RecordingClient()

    with pytest.raises(BadRequest, match="restore_policy"):
        client.plan_retrieval([], restore_policy="sometimes")  # type: ignore[arg-type]
    with pytest.raises(BadRequest, match="restore_policy"):
        client.create_retrieval_job(
            [],
            plan_etag="a" * 64,
            restore_policy="sometimes",  # type: ignore[arg-type]
        )

    assert client.calls == []


def test_client_rejects_noncanonical_resource_identities_before_transport() -> None:
    client = RecordingClient()

    with pytest.raises(BadRequest, match="application"):
        client.create_app_key("Media Indexer", access=[])
    with pytest.raises(BadRequest, match="application"):
        client.revoke_app_key("media-indexer", "ABCDEF0123456789")
    with pytest.raises(BadRequest, match="archive store"):
        client.get_archive_store("Deep Archive")
    with pytest.raises(BadRequest, match="must differ"):
        client.create_or_resume_archive_copy(
            1,
            destination_store="archive",
            source_store="archive",
        )
    with pytest.raises(BadRequest, match="unique"):
        client.plan_retrieval([(1, "a.txt"), (1, "a.txt")])

    assert client.calls == []


def test_retrieval_cache_reads_use_list_and_composite_identity_routes() -> None:
    client = RecordingClient()

    client.retrieval_cache_status()
    client.list_retrieval_cache_objects(
        q="pack",
        tag="docs",
        collection_id=42,
        source_store="deep",
        state="ready",
        protection="protected",
        expires_before="2026-08-15T00:00:00Z",
        expires_after="2026-08-14T00:00:00Z",
        sort="stored_bytes",
        order="asc",
        all_items=True,
    )
    client.get_retrieval_cache_object(42, "deep", "pack-000000000000")

    assert client.calls == [
        ("GET", "/v1/retrieval-cache", {}),
        (
            "GET",
            "/v1/retrieval-cache/objects",
            {
                "params": {
                    "page": 1,
                    "per_page": 25,
                    "sort": "stored_bytes",
                    "order": "asc",
                    "q": "pack",
                    "tag": "docs",
                    "collection_id": 42,
                    "source_store": "deep",
                    "state": "ready",
                    "protection": "protected",
                    "expires_before": "2026-08-15T00:00:00Z",
                    "expires_after": "2026-08-14T00:00:00Z",
                    "all": True,
                }
            },
        ),
        (
            "GET",
            "/v1/retrieval-cache/objects/42/deep/pack-000000000000",
            {},
        ),
    ]


def test_retrieval_file_download_uses_the_logical_file_endpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = ApiClient(base_url="https://example.invalid")
    calls: list[tuple[str, Path]] = []
    output = tmp_path / "document.txt"

    def download(path: str, destination: Path, **kwargs: object) -> int:
        calls.append((path, destination))
        assert kwargs == {"expected_bytes": 42, "expected_sha256": "a" * 64, "progress": None}
        return 42

    monkeypatch.setattr(client, "_download", download)

    result = client.download_retrieval_file(
        "job-id",
        collection_id=42,
        path="docs/document.txt",
        output=output,
        expected_bytes=42,
        expected_sha256="a" * 64,
    )

    assert result == 42
    assert calls == [
        (
            "/v1/retrieval-jobs/job-id/content?collection_id=42&path=docs%2Fdocument.txt",
            output,
        )
    ]


def test_retrieval_file_download_streams_and_verifies_catalog_identity(
    tmp_path: Path,
) -> None:
    content = b"retrieved archive object"
    sha256 = hashlib.sha256(content).hexdigest()

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/retrieval-jobs/job-id/content"
        assert dict(request.url.params) == {
            "collection_id": "42",
            "path": "docs/document.txt",
        }
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Length": str(len(content)), "ETag": f'"{sha256}"'},
        )

    output = tmp_path / "document.txt"
    client = ApiClient(base_url="https://riverhog.test")
    client._download_client = httpx.Client(
        base_url=client.base_url,
        transport=httpx.MockTransport(handle),
    )
    try:
        result = client.download_retrieval_file(
            "job-id",
            collection_id=42,
            path="docs/document.txt",
            output=output,
            expected_bytes=len(content),
            expected_sha256=sha256,
        )
    finally:
        client.close()

    assert result == len(content)
    assert output.read_bytes() == content


def test_one_application_token_reaches_the_complete_client_surface(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_TOKEN", "application-token")

    client = ApiClient()
    assert client.token == "application-token"
    assert callable(client.plan_retrieval)
    assert callable(client.create_or_resume_collection_upload_session)
    assert callable(client.create_app_key)


def test_client_manages_application_keys_with_explicit_access() -> None:
    client = RecordingClient()

    client.list_apps(q="local", active=True, all_items=True)
    client.create_app_key(
        "local",
        access=[
            {"permission": "catalog:read", "resource": "tag:photos"},
            {"permission": "retrieval:manage", "resource": "tag:photos"},
        ],
        expires_in_seconds=3600,
    )
    client.list_app_keys("local", active=False, all_items=True)
    client.revoke_app_key("local", "0123456789abcdef")

    assert client.calls == [
        (
            "GET",
            "/v1/apps",
            {
                "params": {
                    "page": 1,
                    "per_page": 25,
                    "sort": "name",
                    "order": "asc",
                    "q": "local",
                    "active": "true",
                    "all": True,
                }
            },
        ),
        (
            "POST",
            "/v1/apps/local/keys",
            {
                "json": {
                    "access": [
                        {"permission": "catalog:read", "resource": "tag:photos"},
                        {"permission": "retrieval:manage", "resource": "tag:photos"},
                    ],
                    "expires_in_seconds": 3600,
                }
            },
        ),
        (
            "GET",
            "/v1/apps/local/keys",
            {
                "params": {
                    "page": 1,
                    "per_page": 25,
                    "sort": "created_at",
                    "order": "desc",
                    "active": "false",
                    "all": True,
                }
            },
        ),
        (
            "POST",
            "/v1/apps/local/keys/0123456789abcdef/revoke",
            {},
        ),
    ]


def test_client_manages_explicit_tags() -> None:
    client = RecordingClient()
    client.create_tag("photos")
    client.get_tag("photos")
    client.list_tags(q="photo", all_items=True)
    assert client.calls == [
        ("POST", "/v1/tags", {"json": {"id": "photos"}}),
        ("GET", "/v1/tags/photos", {}),
        (
            "GET",
            "/v1/tags",
            {
                "params": {
                    "page": 1,
                    "per_page": 25,
                    "sort": "id",
                    "order": "asc",
                    "q": "photo",
                    "all": True,
                }
            },
        ),
    ]


def test_collection_upload_unit_uses_the_canonical_content_contract() -> None:
    client = RecordingClient()

    client.put_collection_upload_session_unit(
        42,
        "pack-000000000000",
        3,
        plan_sha256="a" * 64,
        content=b"source bytes",
    )

    assert client.calls == [
        (
            "PUT",
            "/v1/collection-upload-sessions/42/volumes/pack-000000000000/units/3",
            {
                "headers": {
                    "Content-Type": "application/octet-stream",
                    "If-Match": '"' + "a" * 64 + '"',
                },
                "content": b"source bytes",
                "timeout": 1800.0,
            },
        )
    ]


def test_provenance_client_methods_use_the_collection_scoped_contract() -> None:
    client = RecordingClient()

    client.list_collection_provenance(
        42,
        q="movie",
        status="captured",
        sort="bytes",
        order="desc",
        all_items=True,
    )
    client.get_collection_file_provenance(42, "media/movie.mov")
    client.trace_collection_file_provenance(42, "media/movie.mov")
    client.verify_collection_provenance(42)

    assert client.calls == [
        (
            "GET",
            "/v1/collections/42/provenance/files",
            {
                "params": {
                    "page": 1,
                    "per_page": 25,
                    "sort": "bytes",
                    "order": "desc",
                    "q": "movie",
                    "status": "captured",
                    "all": True,
                }
            },
        ),
        ("GET", "/v1/collections/42/provenance/files/media/movie.mov", {}),
        ("GET", "/v1/collections/42/provenance/trace/media/movie.mov", {}),
        ("POST", "/v1/collections/42/provenance/verify", {}),
    ]


@pytest.mark.parametrize("collection_id", (0, -1, True))
def test_client_rejects_nonpositive_collection_identities(collection_id: object) -> None:
    client = RecordingClient()

    with pytest.raises(BadRequest):
        client.get_collection(collection_id)  # type: ignore[arg-type]
    assert client.calls == []


def test_provenance_client_rejects_noncanonical_read_identities() -> None:
    client = RecordingClient()

    with pytest.raises(BadRequest):
        client.get_collection_file_provenance(42, "media/../movie.mov")
    with pytest.raises(BadRequest):
        client.export_collection_provenance_journal(42, "journal-1")
    assert client.calls == []


def test_quota_client_rejects_negative_limits() -> None:
    client = RecordingClient()

    with pytest.raises(BadRequest):
        client.set_app_key_download_quota(
            "reader",
            "0123456789abcdef",
            monthly_bytes=-1,
        )
    assert client.calls == []


def test_collection_upload_unit_uses_its_dedicated_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_TIMEOUT_SECONDS", "47")
    client = RecordingClient()

    client.put_collection_upload_session_unit(
        42,
        "pack-000000000000",
        0,
        plan_sha256="a" * 64,
        content=b"source bytes",
    )

    assert client.calls[0][2]["timeout"] == 47
    worker = client.spawn()
    try:
        assert worker.upload_timeout_seconds == 47
        assert worker.timeout_seconds == client.timeout_seconds
    finally:
        worker.close()
