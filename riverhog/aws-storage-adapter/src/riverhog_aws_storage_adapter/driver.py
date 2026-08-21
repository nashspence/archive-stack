"""AWS-owned implementation of the provider-neutral storage-adapter seam."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, cast
from urllib.parse import quote

import httpx
from botocore.signers import CloudFrontSigner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from riverhog_storage_adapter_protocol import (
    ObjectLocator,
    ReadRequest,
    ReadStatus,
    StorageAdapterDescriptor,
    StorageAdapterDescriptorPayload,
    StorageProfile,
    StorageProfilePayload,
    UploadDeclaration,
)
from riverhog_storage_adapter_s3_support import S3CompatibleStorageDriver, make_s3_client
from riverhog_storage_adapter_support import StorageDriverError

from riverhog_aws_storage_adapter.config import AwsStorageAdapterConfig

_CLOUDFRONT_TTL = timedelta(minutes=15)
_RESTORE_RE = re.compile(r'ongoing-request="(true|false)"')
_EXPIRY_RE = re.compile(r'expiry-date="([^"]+)"')


class AwsStorageDriver(S3CompatibleStorageDriver):
    """One AWS target; Deep Archive and CloudFront remain AWS-owned behavior."""

    def __init__(
        self,
        config: AwsStorageAdapterConfig,
        *,
        implementation_version: str,
        source_revision: str,
        client: Any | None = None,
        cloudfront_client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        profile = StorageProfile.seal(
            StorageProfilePayload(
                profile_id=config.profile_id,
                read_mode=cast(Any, config.read_mode),
                egress_accounting_id=config.egress_accounting_id,
            )
        )
        descriptor = StorageAdapterDescriptor.seal(
            StorageAdapterDescriptorPayload(
                implementation_id="riverhog.aws-storage-adapter/v1",
                implementation_version=implementation_version,
                source_revision=source_revision,
                profile=profile,
                minimum_nonfinal_part_bytes=5 * 1024**2,
                maximum_part_bytes=5 * 1024**3,
                maximum_part_count=10_000,
            )
        )
        super().__init__(
            target=config,
            descriptor=descriptor,
            client=client
            or make_s3_client(
                endpoint_url=config.endpoint_url,
                region=config.region,
                access_key_id=config.access_key_id,
                secret_access_key=config.secret_access_key,
                session_token=config.session_token,
                force_path_style=config.force_path_style,
                max_pool_connections=config.max_pool_connections,
            ),
            provider_label="AWS",
        )
        self._cloudfront_client = cloudfront_client
        self._cloudfront_signer: CloudFrontSigner | None = None
        if config.cloudfront_base_url is not None:
            key_file = config.cloudfront_private_key_file
            key_id = config.cloudfront_public_key_id
            if key_file is None or key_id is None:
                raise ValueError("AWS CloudFront configuration is incomplete")
            private_key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
            if not isinstance(private_key, rsa.RSAPrivateKey):
                raise ValueError("AWS CloudFront private key must be RSA")

            def rsa_signer(message: bytes) -> bytes:
                return private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())

            self._cloudfront_signer = CloudFrontSigner(key_id, rsa_signer)
            self._cloudfront_client = self._cloudfront_client or httpx.Client(
                http2=True,
                follow_redirects=False,
                timeout=httpx.Timeout(connect=10, read=300, write=60, pool=10),
            )

    def iter_object_content(
        self,
        locator: ObjectLocator,
        *,
        offset: int | None,
        size: int | None,
    ) -> Iterator[bytes]:
        if self._cloudfront_signer is None:
            yield from super().iter_object_content(locator, offset=offset, size=size)
            return
        revision = self._decode_revision(locator.revision)
        head = self._head_exact(locator.object_path, locator.revision)
        yield from self._iter_cloudfront(
            locator,
            revision,
            etag=str(head.get("ETag", "")),
            offset=offset,
            size=size,
        )

    def prepare_read(self, request: ReadRequest) -> ReadStatus:
        if self.config.read_mode == "immediate":
            return super().prepare_read(request)
        for locator in request.objects:
            revision = self._decode_revision(locator.revision)
            call: dict[str, Any] = {
                "Bucket": self.config.bucket,
                "Key": self._key(locator.object_path),
                "RestoreRequest": {
                    "Days": self.config.restore_hold_days,
                    "GlacierJobParameters": {"Tier": self.config.restore_tier},
                },
            }
            if revision.get("version_id") is not None:
                call["VersionId"] = str(revision["version_id"])
            try:
                self._client.restore_object(**call)
            except Exception as exc:
                if self._error_code(exc) not in {
                    "RestoreAlreadyInProgress",
                    "ObjectAlreadyInActiveTierError",
                }:
                    raise self._error(exc) from exc
        return self.read_status(request)

    def read_status(self, request: ReadRequest) -> ReadStatus:
        if self.config.read_mode == "immediate":
            return super().read_status(request)
        statuses = [self._read_status(locator) for locator in request.objects]
        if all(current.state == "ready" for current in statuses):
            expiries = [current.expires_at for current in statuses if current.expires_at]
            return ReadStatus(state="ready", expires_at=min(expiries) if expiries else None)
        if any(current.state == "expired" for current in statuses):
            return ReadStatus(state="expired")
        return ReadStatus(state="requested")

    def _create_upload_parameters(self, declaration: UploadDeclaration) -> dict[str, object]:
        _ = declaration
        return {"StorageClass": self.config.storage_class}

    def _put_object_parameters(self, declaration: UploadDeclaration) -> dict[str, object]:
        _ = declaration
        return {"StorageClass": self.config.storage_class}

    def _read_status(self, locator: ObjectLocator) -> ReadStatus:
        head = self._head_exact(locator.object_path, locator.revision)
        restore = str(head.get("Restore", ""))
        match = _RESTORE_RE.search(restore)
        if match is None:
            storage_class = str(head.get("StorageClass", "")).upper()
            if storage_class in {"", "STANDARD", "REDUCED_REDUNDANCY"}:
                return ReadStatus(state="ready")
            return ReadStatus(state="expired")
        expiry_match = _EXPIRY_RE.search(restore)
        expires: datetime | None = (
            parsedate_to_datetime(expiry_match.group(1)).astimezone(UTC)
            if expiry_match is not None
            else None
        )
        expires_at = expires.isoformat().replace("+00:00", "Z") if expires is not None else None
        if match.group(1) == "true":
            return ReadStatus(state="requested", expires_at=expires_at)
        if expires is not None and expires <= datetime.now(UTC):
            return ReadStatus(state="expired", expires_at=expires_at)
        return ReadStatus(state="ready", expires_at=expires_at)

    def _iter_cloudfront(
        self,
        locator: ObjectLocator,
        revision: dict[str, object],
        *,
        etag: str,
        offset: int | None,
        size: int | None,
    ) -> Iterator[bytes]:
        signer = self._cloudfront_signer
        client = self._cloudfront_client
        base_url = self.config.cloudfront_base_url
        if signer is None or client is None or base_url is None:
            raise StorageDriverError("internal_failure", "CloudFront is not configured")
        url = f"{base_url}/{quote(self._key(locator.object_path), safe='/')}"
        if revision.get("version_id") is not None:
            url += f"?versionId={quote(str(revision['version_id']), safe='')}"
        signed = signer.generate_presigned_url(
            url, date_less_than=datetime.now(UTC) + _CLOUDFRONT_TTL
        )
        headers = {"Accept-Encoding": "identity"}
        if etag:
            headers["If-Match"] = etag
        expected = 200
        if offset is not None and size is not None:
            headers["Range"] = f"bytes={offset}-{offset + size - 1}"
            expected = 206
        try:
            with client.stream("GET", signed, headers=headers) as response:
                if response.status_code != expected:
                    raise StorageDriverError(
                        "provider_unavailable",
                        f"CloudFront returned HTTP {response.status_code}",
                    )
                yield from response.iter_bytes(chunk_size=8 * 1024**2)
        except httpx.HTTPError as exc:
            raise StorageDriverError("provider_unavailable", "CloudFront read failed") from exc


__all__ = ["AwsStorageDriver"]
