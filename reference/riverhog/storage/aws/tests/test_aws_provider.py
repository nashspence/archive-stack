from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from riverhog_storage_adapter_aws.provider import (
    AwsCloudFrontConfig,
    AwsCloudFrontObjectReader,
    AwsDeepArchiveReadPreparation,
)


class _RestoreClient:
    def __init__(self) -> None:
        self.head: dict[str, Any] = {"StorageClass": "DEEP_ARCHIVE"}
        self.requests: list[dict[str, Any]] = []

    def head_object(self, **request: Any) -> dict[str, Any]:
        assert request == {
            "Bucket": "archive-bucket",
            "Key": "archives/object.age",
            "VersionId": "provider-version",
        }
        return dict(self.head)

    def restore_object(self, **request: Any) -> None:
        self.requests.append(request)
        self.head["Restore"] = 'ongoing-request="true"'


class _DeliverySource:
    def head_object(self, **request: Any) -> dict[str, Any]:
        assert request == {
            "Bucket": "private-bucket",
            "Key": "archives/path with space/object.age",
            "VersionId": "provider/version+id=",
        }
        return {"ContentLength": 10, "VersionId": "provider/version+id="}


def test_aws_restore_mechanics_are_adapter_configuration() -> None:
    client = _RestoreClient()
    preparation = AwsDeepArchiveReadPreparation(tier="Bulk", days=3)
    objects = (("archives/object.age", "provider-version"),)

    requested = preparation.prepare(
        client=client,
        bucket="archive-bucket",
        objects=objects,
    )

    assert requested.state == "requested"
    assert client.requests == [
        {
            "Bucket": "archive-bucket",
            "Key": "archives/object.age",
            "VersionId": "provider-version",
            "RestoreRequest": {
                "Days": 3,
                "GlacierJobParameters": {"Tier": "Bulk"},
            },
        }
    ]

    client.head["Restore"] = 'ongoing-request="false", expiry-date="Tue, 01 Jan 2030 00:00:00 GMT"'
    ready = preparation.status(client=client, bucket="archive-bucket", objects=objects)
    assert ready.state == "ready"
    assert ready.available_until == "2030-01-01T00:00:00.000000Z"


def test_aws_immediate_object_never_requests_restore() -> None:
    client = _RestoreClient()
    client.head = {"StorageClass": "STANDARD"}
    preparation = AwsDeepArchiveReadPreparation()

    status = preparation.prepare(
        client=client,
        bucket="archive-bucket",
        objects=(("archives/object.age", "provider-version"),),
    )

    assert status.state == "ready"
    assert not client.requests


def _private_key(path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def test_cloudfront_delivery_binds_exact_version_range_and_length(tmp_path: Path) -> None:
    key_path = tmp_path / "cloudfront.pem"
    _private_key(key_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            206,
            headers={
                "Content-Length": "4",
                "Content-Range": "bytes 3-6/10",
            },
            content=b"3456",
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    reader = AwsCloudFrontObjectReader(
        AwsCloudFrontConfig(
            base_url="https://archive.example.test",
            public_key_id="public-key-id",
            private_key_path=key_path,
        ),
        client=http,
    )
    try:
        content = b"".join(
            reader.read_object(
                client=_DeliverySource(),
                bucket="private-bucket",
                key="archives/path with space/object.age",
                object_path="archives/path with space/object.age",
                revision="provider/version+id=",
                offset=3,
                size=4,
                expected_bytes=10,
                chunk_bytes=64 * 1024,
            ).content
        )
    finally:
        reader.close()
        http.close()

    assert content == b"3456"
    assert len(requests) == 1
    request = requests[0]
    assert request.url.raw_path.startswith(b"/archives/path%20with%20space/object.age?")
    assert request.url.params["versionId"] == "provider/version+id="
    assert request.url.params["Key-Pair-Id"] == "public-key-id"
    assert request.headers["Range"] == "bytes=3-6"
    assert request.headers["Accept-Encoding"] == "identity"


def test_cloudfront_configuration_rejects_non_https_origins(tmp_path: Path) -> None:
    key_path = tmp_path / "unused.pem"
    try:
        AwsCloudFrontConfig(
            base_url="http://archive.example.test",
            public_key_id="key",
            private_key_path=key_path,
        )
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("non-HTTPS CloudFront URL was accepted")
