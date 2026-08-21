from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import riverhog_storage_adapter_s3_support.driver as driver_module
from riverhog_storage_adapter_protocol import (
    CompleteUploadRequest,
    ObjectLocator,
    StorageAdapterDescriptor,
    StorageAdapterDescriptorPayload,
    StorageProfile,
    StorageProfilePayload,
    UploadDeclaration,
    UploadDeclarationPayload,
    UploadPartReceipt,
    WriteCondition,
)
from riverhog_storage_adapter_s3_support import (
    S3CompatibleStorageDriver,
    make_s3_client,
)
from riverhog_storage_adapter_support import StorageDriverError


class _ProviderError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Body:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = False

    def iter_chunks(self, *, chunk_size: int):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class _Paginator:
    def __init__(self, client: _FakeClient, name: str) -> None:
        self.client = client
        self.name = name

    def paginate(self, **request: object):
        if self.name == "list_objects_v2":
            self.client.object_list_requests.append(request)
            return self.client.object_pages
        self.client.version_list_requests.append(request)
        return self.client.version_pages


class _FakeClient:
    def __init__(self, *, versioned: bool = True) -> None:
        self.versioned = versioned
        self.objects: dict[str, dict[str, Any]] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.next_upload = 1
        self.next_version = 1
        self.delete_requests: list[dict[str, object]] = []
        self.delete_batch_requests: list[dict[str, object]] = []
        self.version_list_requests: list[dict[str, object]] = []
        self.object_list_requests: list[dict[str, object]] = []
        self.get_requests: list[dict[str, object]] = []
        self.version_pages: list[dict[str, object]] = []
        self.object_pages: list[dict[str, object]] = []
        self.multipart_pages: list[dict[str, object]] = []

    def head_bucket(self, **_request: object) -> None:
        return

    def head_object(self, **request: object) -> dict[str, object]:
        key = str(request["Key"])
        try:
            current = self.objects[key]
        except KeyError as exc:
            raise _ProviderError("NoSuchKey") from exc
        requested = request.get("VersionId")
        if requested is not None and current.get("VersionId") != requested:
            raise _ProviderError("NoSuchVersion")
        return {name: value for name, value in current.items() if name != "Body"}

    def create_multipart_upload(self, **request: object) -> dict[str, str]:
        upload_id = f"upload-{self.next_upload}"
        self.next_upload += 1
        self.uploads[upload_id] = {"request": request, "parts": {}}
        return {"UploadId": upload_id}

    def upload_part(self, **request: object) -> dict[str, str]:
        upload = self.uploads[str(request["UploadId"])]
        number = int(str(request["PartNumber"]))
        upload["parts"][number] = bytes(request["Body"])  # type: ignore[index]
        return {"ETag": f'"part-{number}"'}

    def list_parts(self, **request: object) -> dict[str, object]:
        number = int(str(request["PartNumberMarker"])) + 1
        content = self.uploads[str(request["UploadId"])]["parts"].get(number)
        parts = (
            []
            if content is None
            else [{"PartNumber": number, "ETag": f'"part-{number}"', "Size": len(content)}]
        )
        return {"IsTruncated": False, "Parts": parts}

    def complete_multipart_upload(self, **request: object) -> dict[str, object]:
        key = str(request["Key"])
        self._check_condition(key, request)
        upload = self.uploads.pop(str(request["UploadId"]))
        content = b"".join(
            upload["parts"][number]
            for number in sorted(upload["parts"])  # type: ignore[index]
        )
        return self._store(key, content, upload["request"])  # type: ignore[arg-type,index]

    def put_object(self, **request: object) -> dict[str, object]:
        key = str(request["Key"])
        self._check_condition(key, request)
        return self._store(key, bytes(request["Body"]), request)

    def _check_condition(self, key: str, request: dict[str, object]) -> None:
        current = self.objects.get(key)
        if request.get("IfNoneMatch") == "*" and current is not None:
            raise _ProviderError("PreconditionFailed")
        if request.get("IfMatch") is not None and (
            current is None or request["IfMatch"] != current["ETag"]
        ):
            raise _ProviderError("PreconditionFailed")

    def _store(
        self,
        key: str,
        content: bytes,
        request: dict[str, object],
    ) -> dict[str, object]:
        version_id = f"version-{self.next_version}" if self.versioned else None
        self.next_version += 1
        current = {
            "Body": content,
            "ContentLength": len(content),
            "ContentType": request["ContentType"],
            "Metadata": dict(request["Metadata"]),  # type: ignore[arg-type]
            "ETag": f'"etag-{self.next_version}"',
            "LastModified": datetime(2026, 8, 21, tzinfo=UTC),
        }
        if version_id is not None:
            current["VersionId"] = version_id
        self.objects[key] = current
        return {"VersionId": version_id} if version_id is not None else {}

    def get_object(self, **request: object) -> dict[str, object]:
        self.get_requests.append(request)
        key = str(request["Key"])
        current = self.objects[key]
        if request.get("VersionId") is not None:
            assert request["VersionId"] == current.get("VersionId")
        content = bytes(current["Body"])
        raw_range = request.get("Range")
        if raw_range is not None:
            start, end = (int(value) for value in str(raw_range).removeprefix("bytes=").split("-"))
            content = content[start : end + 1]
        return {"Body": _Body(content)}

    def delete_object(self, **request: object) -> None:
        self.delete_requests.append(request)

    def get_paginator(self, name: str) -> _Paginator:
        assert name in {"list_object_versions", "list_objects_v2"}
        return _Paginator(self, name)

    def delete_objects(self, **request: object) -> None:
        self.delete_batch_requests.append(request)

    def list_multipart_uploads(self, **_request: object) -> dict[str, object]:
        return self.multipart_pages.pop(0)

    def abort_multipart_upload(self, **request: object) -> None:
        self.uploads.pop(str(request["UploadId"]), None)


@dataclass(frozen=True)
class _Target:
    bucket: str = "fixture"
    prefix: str = "qualification"


def _descriptor() -> StorageAdapterDescriptor:
    profile = StorageProfile.seal(
        StorageProfilePayload(
            profile_id="fixture.immediate/v1",
            read_mode="immediate",
            egress_accounting_id="fixture",
        )
    )
    return StorageAdapterDescriptor.seal(
        StorageAdapterDescriptorPayload(
            implementation_id="fixture.storage-adapter/v1",
            implementation_version="1",
            source_revision="test",
            profile=profile,
            minimum_nonfinal_part_bytes=1,
            maximum_part_bytes=1024,
            maximum_part_count=10,
        )
    )


def _driver(client: _FakeClient) -> S3CompatibleStorageDriver:
    return S3CompatibleStorageDriver(
        target=_Target(),
        descriptor=_descriptor(),
        client=client,
        provider_label="fixture",
    )


def _declaration(
    *,
    transfer_id: str,
    stored_bytes: int,
    condition: WriteCondition | None = None,
) -> UploadDeclaration:
    return UploadDeclaration.seal(
        UploadDeclarationPayload(
            transfer_id=transfer_id,
            object_path="archives/object.age",
            content_type="application/octet-stream",
            stored_bytes=stored_bytes,
            runtime_descriptor_sha256=_descriptor().runtime_descriptor_sha256,
            condition=condition or WriteCondition(),
        )
    )


def _part(
    driver: S3CompatibleStorageDriver,
    declaration: UploadDeclaration,
    upload,
    content: bytes,
) -> UploadPartReceipt:
    digest = hashlib.sha256(content).hexdigest()
    token = driver.upload_part(
        declaration=declaration,
        upload=upload,
        number=1,
        content=content,
        stored_sha256=digest,
    )
    return UploadPartReceipt(
        number=1,
        part_token=token,
        stored_bytes=len(content),
        stored_sha256=digest,
    )


def test_s3_client_forwards_credentials_pool_and_retry_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def client(service: str, **kwargs: object) -> object:
        assert service == "s3"
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(driver_module.boto3, "client", client)

    make_s3_client(
        endpoint_url="https://objects.example",
        region="test-1",
        access_key_id="key",
        secret_access_key="secret",
        session_token="session",
        force_path_style=True,
        max_pool_connections=17,
    )

    assert captured["aws_session_token"] == "session"
    config = captured["config"]
    assert config.max_pool_connections == 17  # type: ignore[union-attr]
    assert config.retries == {"mode": "standard", "max_attempts": 8}  # type: ignore[union-attr]
    assert config.s3 == {"addressing_style": "path"}  # type: ignore[union-attr]


def test_multipart_round_trip_binds_exact_revision_range_and_delete() -> None:
    client = _FakeClient()
    driver = _driver(client)
    content = b"exact encrypted bytes"
    declaration = _declaration(transfer_id="round-trip", stored_bytes=len(content))
    upload = driver.create_upload(declaration)
    part = _part(driver, declaration, upload, content)
    driver.verify_part_receipt(declaration=declaration, upload=upload, receipt=part)

    receipt = driver.complete_upload(
        declaration=declaration,
        upload=upload,
        completion=CompleteUploadRequest(
            parts=(part,),
            stored_bytes=len(content),
            stored_sha256=hashlib.sha256(content).hexdigest(),
        ),
    )

    assert client.get_requests == []
    locator = ObjectLocator(object_path=receipt.object_path, revision=receipt.revision)
    driver.verify_object(receipt)
    assert b"".join(driver.iter_object_content(locator, offset=6, size=9)) == content[6:15]
    driver.delete_object(locator)
    assert client.delete_requests == [
        {
            "Bucket": "fixture",
            "Key": "qualification/archives/object.age",
            "VersionId": "version-1",
        }
    ]


def test_exact_replacement_requires_the_current_opaque_revision() -> None:
    client = _FakeClient(versioned=False)
    driver = _driver(client)
    initial = _declaration(transfer_id="initial", stored_bytes=0)
    initial_upload = driver.create_upload(initial)
    initial_receipt = driver.complete_upload(
        declaration=initial,
        upload=initial_upload,
        completion=CompleteUploadRequest(
            parts=(),
            stored_bytes=0,
            stored_sha256=hashlib.sha256(b"").hexdigest(),
        ),
    )
    replacement = _declaration(
        transfer_id="replacement",
        stored_bytes=0,
        condition=WriteCondition(
            mode="replace_exact",
            prior_revision=initial_receipt.revision,
        ),
    )

    replacement_upload = driver.create_upload(replacement)
    replacement_receipt = driver.complete_upload(
        declaration=replacement,
        upload=replacement_upload,
        completion=CompleteUploadRequest(
            parts=(),
            stored_bytes=0,
            stored_sha256=hashlib.sha256(b"").hexdigest(),
        ),
    )

    assert replacement_receipt.revision != initial_receipt.revision
    with pytest.raises(StorageDriverError, match="revision changed"):
        driver.create_upload(replacement)


def test_create_only_rejects_an_existing_object() -> None:
    client = _FakeClient()
    driver = _driver(client)
    declaration = _declaration(transfer_id="first", stored_bytes=0)
    upload = driver.create_upload(declaration)
    driver.complete_upload(
        declaration=declaration,
        upload=upload,
        completion=CompleteUploadRequest(
            parts=(),
            stored_bytes=0,
            stored_sha256=hashlib.sha256(b"").hexdigest(),
        ),
    )

    with pytest.raises(StorageDriverError, match="create-only") as captured:
        driver.create_upload(_declaration(transfer_id="second", stored_bytes=0))
    assert captured.value.code == "revision_conflict"


def test_prefix_delete_is_confined_to_exact_versioned_members() -> None:
    client = _FakeClient()
    client.version_pages = [
        {
            "Versions": [
                {"Key": "qualification/archives/a", "VersionId": "a1"},
                {"Key": "qualification/archives/b", "VersionId": "b1"},
            ],
            "DeleteMarkers": [{"Key": "qualification/archives/a", "VersionId": "am"}],
        }
    ]

    assert _driver(client).delete_prefix("archives") == 3
    assert client.version_list_requests == [
        {"Bucket": "fixture", "Prefix": "qualification/archives/"}
    ]
    assert client.delete_batch_requests == [
        {
            "Bucket": "fixture",
            "Delete": {
                "Objects": [
                    {"Key": "qualification/archives/a", "VersionId": "a1"},
                    {"Key": "qualification/archives/b", "VersionId": "b1"},
                    {"Key": "qualification/archives/a", "VersionId": "am"},
                ]
            },
        }
    ]


def test_incomplete_upload_cleanup_obeys_prefix_and_cutoff() -> None:
    client = _FakeClient()
    old = datetime(2026, 8, 20, tzinfo=UTC)
    recent = old + timedelta(days=1)
    client.uploads = {
        "old": {"request": {}, "parts": {}},
        "recent": {"request": {}, "parts": {}},
    }
    client.multipart_pages = [
        {
            "Uploads": [
                {"Key": "qualification/archives/old", "UploadId": "old", "Initiated": old},
                {
                    "Key": "qualification/archives/recent",
                    "UploadId": "recent",
                    "Initiated": recent,
                },
            ],
            "IsTruncated": False,
        }
    ]

    affected = _driver(client).abort_incomplete_uploads(initiated_before="2026-08-20T12:00:00Z")

    assert affected == 1
    assert set(client.uploads) == {"recent"}


def test_recovery_export_source_lists_and_streams_only_the_configured_root() -> None:
    client = _FakeClient()
    driver = _driver(client)
    declaration = _declaration(transfer_id="export", stored_bytes=0)
    upload = driver.create_upload(declaration)
    driver.complete_upload(
        declaration=declaration,
        upload=upload,
        completion=CompleteUploadRequest(
            parts=(),
            stored_bytes=0,
            stored_sha256=hashlib.sha256(b"").hexdigest(),
        ),
    )
    client.object_pages = [{"Contents": [{"Key": "qualification/archives/object.age", "Size": 0}]}]

    entries = tuple(driver.iter_recovery_export_entries())

    assert len(entries) == 1
    assert entries[0].object_path == "archives/object.age"
    assert entries[0].stored_bytes == 0
    assert b"".join(driver.iter_recovery_export_content(entries[0])) == b""
    assert client.object_list_requests == [{"Bucket": "fixture", "Prefix": "qualification/"}]
