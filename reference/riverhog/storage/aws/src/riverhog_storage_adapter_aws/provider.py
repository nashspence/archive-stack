"""AWS-only delivery and archive-restoration mechanics."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit

import httpx
from botocore.exceptions import ClientError
from botocore.signers import CloudFrontSigner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from riverhog_storage_adapter_protocol import (
    ReadExpired,
    ReadReadiness,
    ReadReady,
    ReadRequested,
)
from time_formats import format_utc_timestamp, utc_now

_ARCHIVE_STORAGE_CLASSES = frozenset({"DEEP_ARCHIVE", "GLACIER"})
_RESTORE_VALUE = re.compile(r'([a-z-]+)="([^"]*)"')


@dataclass(frozen=True, slots=True)
class AwsDeepArchiveReadPreparation:
    """AWS restore policy configured inside one adapter instance."""

    tier: str = "Bulk"
    days: int = 3

    def __post_init__(self) -> None:
        if self.tier not in {"Bulk", "Standard", "Expedited"}:
            raise ValueError("AWS restore tier must be Bulk, Standard, or Expedited")
        if self.days < 1 or self.days > 365:
            raise ValueError("AWS restore days must be within 1..365")

    def prepare(
        self,
        *,
        client: Any,
        bucket: str,
        objects: tuple[tuple[str, str | None], ...],
    ) -> ReadReadiness:
        for key, revision in objects:
            current = _object_status(client, bucket=bucket, key=key, revision=revision)
            if current.state == "ready" or current.state == "requested":
                continue
            request: dict[str, Any] = {
                "Bucket": bucket,
                "Key": key,
                "RestoreRequest": {
                    "Days": self.days,
                    "GlacierJobParameters": {"Tier": self.tier},
                },
            }
            if revision is not None:
                request["VersionId"] = revision
            try:
                client.restore_object(**request)
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code == "ObjectAlreadyInActiveTierError":
                    continue
                if code != "RestoreAlreadyInProgress":
                    raise
        return self.status(client=client, bucket=bucket, objects=objects)

    def status(
        self,
        *,
        client: Any,
        bucket: str,
        objects: tuple[tuple[str, str | None], ...],
    ) -> ReadReadiness:
        statuses = tuple(
            _object_status(client, bucket=bucket, key=key, revision=revision)
            for key, revision in objects
        )
        if any(current.state == "expired" for current in statuses):
            return ReadExpired()
        if all(current.state == "ready" for current in statuses):
            expirations = sorted(
                current.available_until
                for current in statuses
                if isinstance(current, ReadReady) and current.available_until is not None
            )
            return ReadReady(
                available_until=expirations[0] if expirations else None,
            )
        return ReadRequested()

    def cleanup(
        self,
        *,
        client: Any,
        bucket: str,
        objects: tuple[tuple[str, str | None], ...],
    ) -> None:
        _ = client, bucket, objects


@dataclass(frozen=True, slots=True)
class AwsCloudFrontConfig:
    base_url: str
    public_key_id: str
    private_key_path: Path
    url_ttl: timedelta = timedelta(minutes=15)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("CloudFront base URL must be an authority-only HTTPS URL")
        if not self.public_key_id.strip():
            raise ValueError("CloudFront public key id must be nonempty")
        if self.url_ttl.total_seconds() <= 0:
            raise ValueError("CloudFront signed URL lifetime must be positive")


class AwsCloudFrontObjectReader:
    """Exact version/range CloudFront delivery kept wholly inside the AWS adapter."""

    def __init__(
        self,
        config: AwsCloudFrontConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        private_key = serialization.load_pem_private_key(
            config.private_key_path.read_bytes(),
            password=None,
        )
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("CloudFront private key must be an RSA private key")

        def rsa_signer(message: bytes) -> bytes:
            return private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())

        self._config = config
        self._signer = CloudFrontSigner(config.public_key_id, rsa_signer)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            http2=True,
            follow_redirects=False,
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def iter_object(
        self,
        *,
        client: Any,
        bucket: str,
        key: str,
        revision: str | None,
        offset: int | None,
        size: int | None,
        expected_bytes: int,
        chunk_bytes: int,
    ) -> Iterator[bytes]:
        _ = client, bucket
        if size == 0:
            return
        object_url = f"{self._config.base_url.rstrip('/')}/{quote(key, safe='/')}"
        if revision is not None:
            object_url = f"{object_url}?versionId={quote(revision, safe='')}"
        signed_url = self._signer.generate_presigned_url(
            object_url,
            date_less_than=datetime.now(UTC) + self._config.url_ttl,
        )
        headers = {"Accept-Encoding": "identity"}
        expected = expected_bytes
        expected_status = 200
        expected_range: str | None = None
        if offset is not None and size is not None:
            headers["Range"] = f"bytes={offset}-{offset + size - 1}"
            expected = size
            expected_status = 206
            expected_range = f"bytes {offset}-{offset + size - 1}/{expected_bytes}"
        try:
            with self._client.stream("GET", signed_url, headers=headers) as response:
                if response.status_code != expected_status:
                    raise RuntimeError(
                        f"CloudFront returned unexpected HTTP {response.status_code}"
                    )
                content_length = response.headers.get("content-length")
                if content_length is None or int(content_length) != expected:
                    raise RuntimeError("CloudFront response length differs from its request")
                if expected_range is not None and response.headers.get("content-range") != (
                    expected_range
                ):
                    raise RuntimeError("CloudFront response range differs from its request")
                emitted = 0
                for chunk in response.iter_bytes(chunk_size=chunk_bytes):
                    if not chunk:
                        continue
                    emitted += len(chunk)
                    if emitted > expected:
                        raise RuntimeError("CloudFront response contains trailing bytes")
                    yield chunk
                if emitted != expected:
                    raise RuntimeError("CloudFront response ended before its declared length")
        except httpx.HTTPError:
            raise RuntimeError("CloudFront object delivery failed") from None


def _object_status(
    client: Any,
    *,
    bucket: str,
    key: str,
    revision: str | None,
) -> ReadReadiness:
    request: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if revision is not None:
        request["VersionId"] = revision
    head = cast(dict[str, Any], client.head_object(**request))
    storage_class = str(head.get("StorageClass") or "STANDARD").upper()
    if storage_class not in _ARCHIVE_STORAGE_CLASSES:
        return ReadReady()
    restore = _parse_restore_header(head.get("Restore"))
    if restore is None:
        return ReadExpired()
    if restore["ongoing"]:
        return ReadRequested()
    expires = restore["expires_at"]
    if expires is not None and expires <= utc_now():
        return ReadExpired()
    return ReadReady(
        available_until=format_utc_timestamp(expires) if expires is not None else None,
    )


def _parse_restore_header(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    values = {key: item for key, item in _RESTORE_VALUE.findall(value)}
    ongoing = values.get("ongoing-request")
    if ongoing not in {"true", "false"}:
        return None
    expires: datetime | None = None
    if raw_expiry := values.get("expiry-date"):
        try:
            expires = datetime.strptime(raw_expiry, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=UTC)
        except ValueError:
            return None
    return {"ongoing": ongoing == "true", "expires_at": expires}


__all__ = [
    "AwsCloudFrontConfig",
    "AwsCloudFrontObjectReader",
    "AwsDeepArchiveReadPreparation",
]
