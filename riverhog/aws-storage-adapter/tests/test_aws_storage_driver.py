from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

from riverhog_aws_storage_adapter.config import AwsStorageAdapterConfig
from riverhog_aws_storage_adapter.driver import AwsStorageDriver
from riverhog_storage_adapter_protocol import ObjectLocator, ReadRequest


def _revision(*, version_id: str | None = "provider/version+id=") -> str:
    value = {"revision": "opaque-revision", "version_id": version_id}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class _S3Client:
    def __init__(self) -> None:
        self.restore = ""
        self.restore_requests: list[dict[str, object]] = []
        self.head_requests: list[dict[str, object]] = []

    def head_object(self, **request: object) -> dict[str, object]:
        self.head_requests.append(request)
        return {
            "ContentLength": 20,
            "ContentType": "application/octet-stream",
            "ETag": '"provider-etag"',
            "Metadata": {"riverhog-adapter-revision": "opaque-revision"},
            "StorageClass": "DEEP_ARCHIVE",
            "Restore": self.restore,
        }

    def restore_object(self, **request: object) -> None:
        self.restore_requests.append(request)
        self.restore = 'ongoing-request="true"'


class _CloudFrontResponse:
    def __init__(self, content: bytes, *, status_code: int) -> None:
        self.content = content
        self.status_code = status_code

    def __enter__(self) -> _CloudFrontResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return

    def iter_bytes(self, *, chunk_size: int):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]


class _CloudFrontClient:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def stream(self, method: str, url: str, *, headers: dict[str, str]):
        self.requests.append((method, url, headers))
        status = 206 if "Range" in headers else 200
        return _CloudFrontResponse(self.content, status_code=status)


class _CloudFrontSigner:
    def generate_presigned_url(self, url: str, *, date_less_than: datetime) -> str:
        assert date_less_than > datetime.now(UTC)
        return f"{url}&Signature=fixture"


def _config(tmp_path: Path) -> AwsStorageAdapterConfig:
    return AwsStorageAdapterConfig(
        bucket="archive",
        region="us-west-2",
        profile_id="riverhog.aws-deep-archive/v1",
        egress_accounting_id="aws-internet-egress",
        token_file=tmp_path / "token",
        state_root=tmp_path / "state",
        prefix="qualification",
        endpoint_url="https://s3.us-west-2.amazonaws.com",
        access_key_id="key",
        secret_access_key="secret",
        session_token="session",
        force_path_style=False,
        storage_class="DEEP_ARCHIVE",
        read_mode="restore_required",
        restore_tier="Bulk",
        restore_hold_days=7,
        max_pool_connections=17,
    )


def _driver(tmp_path: Path, client: _S3Client) -> AwsStorageDriver:
    return AwsStorageDriver(
        _config(tmp_path),
        implementation_version="1",
        source_revision="fixture",
        client=client,
    )


def test_deep_archive_restore_is_normalized_behind_the_adapter(tmp_path: Path) -> None:
    client = _S3Client()
    driver = _driver(tmp_path, client)
    locator = ObjectLocator(
        object_path="archives/object.age",
        revision=_revision(),
    )
    request = ReadRequest(objects=(locator,))

    assert driver.prepare_read(request).state == "requested"
    assert client.restore_requests == [
        {
            "Bucket": "archive",
            "Key": "qualification/archives/object.age",
            "RestoreRequest": {
                "Days": 7,
                "GlacierJobParameters": {"Tier": "Bulk"},
            },
            "VersionId": "provider/version+id=",
        }
    ]

    client.restore = 'ongoing-request="false", expiry-date="Fri, 09 Jan 2099 00:00:00 GMT"'
    ready = driver.read_status(request)
    assert ready.state == "ready"
    assert ready.expires_at == "2099-01-09T00:00:00Z"


def test_cloudfront_delivery_preserves_exact_revision_and_range_contract(
    tmp_path: Path,
) -> None:
    s3 = _S3Client()
    cloudfront = _CloudFrontClient(b"selected-range")
    driver = _driver(tmp_path, s3)
    object.__setattr__(
        driver.config,
        "cloudfront_base_url",
        "https://archive.example.test",
    )
    driver._cloudfront_client = cloudfront
    driver._cloudfront_signer = _CloudFrontSigner()  # type: ignore[assignment]
    locator = ObjectLocator(
        object_path="archives/object.age",
        revision=_revision(),
    )

    content = b"".join(driver.iter_object_content(locator, offset=3, size=14))

    assert content == b"selected-range"
    assert cloudfront.requests == [
        (
            "GET",
            "https://archive.example.test/qualification/archives/object.age"
            "?versionId=provider%2Fversion%2Bid%3D&Signature=fixture",
            {
                "Accept-Encoding": "identity",
                "If-Match": '"provider-etag"',
                "Range": "bytes=3-16",
            },
        )
    ]


def test_aws_descriptor_exposes_only_normalized_profile_and_runtime_evidence(
    tmp_path: Path,
) -> None:
    descriptor = _driver(tmp_path, _S3Client()).descriptor()

    assert descriptor.implementation_id == "riverhog.aws-storage-adapter/v1"
    assert descriptor.profile.profile_id == "riverhog.aws-deep-archive/v1"
    assert descriptor.profile.read_mode == "restore_required"
    assert descriptor.profile.egress_accounting_id == "aws-internet-egress"
    assert "bucket" not in type(descriptor).model_fields
    assert "storage_class" not in type(descriptor).model_fields
    assert "cloudfront" not in descriptor.model_dump_json().casefold()


def test_aws_cloudfront_configuration_is_complete_and_adapter_owned(
    tmp_path: Path,
) -> None:
    config = AwsStorageAdapterConfig(
        bucket="archive",
        region="us-west-2",
        profile_id="riverhog.aws-deep-archive/v1",
        egress_accounting_id="aws-internet-egress",
        token_file=tmp_path / "token",
        state_root=tmp_path / "state",
        cloudfront_base_url="https://archive.example.test",
        cloudfront_public_key_id="KFIXTURE",
        cloudfront_private_key_file=tmp_path / "cloudfront.pem",
    )

    assert config.cloudfront_base_url == "https://archive.example.test"
    assert config.cloudfront_public_key_id == "KFIXTURE"
    assert config.cloudfront_private_key_file == tmp_path / "cloudfront.pem"


def test_aws_target_configuration_fields_are_all_observable(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert config.bucket == "archive"
    assert config.region == "us-west-2"
    assert config.profile_id == "riverhog.aws-deep-archive/v1"
    assert config.egress_accounting_id == "aws-internet-egress"
    assert config.token_file == tmp_path / "token"
    assert config.state_root == tmp_path / "state"
    assert config.prefix == "qualification"
    assert config.endpoint_url == "https://s3.us-west-2.amazonaws.com"
    assert config.access_key_id == "key"
    assert config.secret_access_key == "secret"
    assert config.session_token == "session"
    assert config.force_path_style is False
    assert config.storage_class == "DEEP_ARCHIVE"
    assert config.read_mode == "restore_required"
    assert config.restore_tier == "Bulk"
    assert config.restore_hold_days == 7
    assert config.max_pool_connections == 17
