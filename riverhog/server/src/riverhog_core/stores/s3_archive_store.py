from __future__ import annotations

import hashlib
import logging
import re
import secrets
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, TypedDict, cast
from urllib.parse import quote

import httpx
from botocore.signers import CloudFrontSigner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from riverhog_age import (
    encrypt_age_scrypt,
    iter_decrypt_age_scrypt,
)
from time_formats import format_utc_timestamp, utc_timestamp_now

from riverhog_core.archive_attestations import (
    ATTESTATION_FILENAMES,
    ATTESTATION_OBJECT_KINDS,
)
from riverhog_core.archive_formats import (
    ARCHIVE_OBJECT_STORAGE_FORMATS,
    ROOT_PROOF_STORAGE_FORMAT,
    archive_object_storage_format,
)
from riverhog_core.archive_object_paths import archive_store_object_path
from riverhog_core.archive_safety import ARCHIVE_DATA_LOSS_WARNING, archive_agents_guidance
from riverhog_core.ports.archive_store import (
    ArchiveArtifactRead,
    ArchiveObjectIdentity,
    ArchiveObjectUploadReceipt,
    ArchiveReadStatus,
    ArchiveVerificationError,
    CollectionArchiveIdentity,
    CollectionArchiveUploadReceipt,
    MutableManifestReceipt,
)
from riverhog_core.ports.download_allowance import DownloadAllowance, DownloadAttribution
from riverhog_core.ports.retrieval_cache import RetrievalCache, RetrievalCacheReceipt
from riverhog_core.runtime_config import ArchiveStoreConfig, RuntimeConfig
from riverhog_core.stores.s3_support import (
    create_archive_s3_client,
    delete_exact_object,
    delete_object_versions_with_prefix,
)

ENCRYPTION_METADATA = "riverhog-encryption"
PLAINTEXT_BYTES_METADATA = "riverhog-plaintext-bytes"
PLAINTEXT_SHA256_METADATA = "riverhog-plaintext-sha256"
STORED_SHA256_METADATA = "riverhog-stored-sha256"
AGE_SCRYPT_ENCRYPTION = "age-v1-scrypt"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MIN_MULTIPART_PART_SIZE = 5 * 1024 * 1024
_MAX_MULTIPART_PART_SIZE = 5 * 1024 * 1024 * 1024
_MAX_MULTIPART_PARTS = 10_000
_MAX_SINGLE_PUT_OBJECT_SIZE = 5 * 1024 * 1024 * 1024
_OPAQUE_ARCHIVE_ID_BYTES = 16
_CLOUDFRONT_URL_TTL = timedelta(minutes=15)
_LOG = logging.getLogger(__name__)


class _RestoreHeader(TypedDict):
    ongoing: bool
    expires_at: str | None


class S3ArchiveStore:
    def __init__(
        self,
        config: RuntimeConfig,
        store: ArchiveStoreConfig,
        *,
        retrieval_cache: RetrievalCache | None = None,
        download_allowance: DownloadAllowance | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._bucket = store.bucket
        self._client = create_archive_s3_client(config, store)
        self._retrieval_cache = retrieval_cache
        self._download_allowance = download_allowance
        if store.monthly_download_allowance_bytes is not None and download_allowance is None:
            raise ValueError("configured archive download allowance requires its service")
        self._cloudfront_signer: CloudFrontSigner | None = None
        self._cloudfront_client: httpx.Client | None = None
        if store.cloudfront_base_url is not None:
            private_key_path = store.cloudfront_private_key_path
            public_key_id = store.cloudfront_public_key_id
            if private_key_path is None or public_key_id is None:
                raise ValueError("CloudFront download configuration is incomplete")
            private_key = serialization.load_pem_private_key(
                private_key_path.read_bytes(),
                password=None,
            )
            if not isinstance(private_key, rsa.RSAPrivateKey):
                raise ValueError("CloudFront private key must be an RSA private key")

            def rsa_signer(message: bytes) -> bytes:
                # CloudFront canned-policy signatures require RSA PKCS#1 v1.5 with SHA-1.
                return private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())

            self._cloudfront_signer = CloudFrontSigner(public_key_id, rsa_signer)
            self._cloudfront_client = httpx.Client(
                http2=True,
                follow_redirects=False,
                timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0),
            )

    def read_mode(self) -> str:
        return self._store.read_mode

    def new_collection_archive_storage_prefix(self) -> str:
        archive_id = secrets.token_hex(_OPAQUE_ARCHIVE_ID_BYTES)
        return archive_store_object_path(self._store.prefix, "archives", archive_id)

    def abort_incomplete_multipart_uploads(
        self,
        *,
        initiated_before: datetime,
    ) -> int:
        if initiated_before.tzinfo is None:
            raise ValueError("multipart upload cutoff must be timezone-aware")
        cutoff = initiated_before.astimezone(UTC)
        archive_prefix = f"{archive_store_object_path(self._store.prefix, 'archives')}/"
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Prefix": archive_prefix,
        }
        aborted = 0
        while True:
            response = cast(
                dict[str, Any],
                self._client.list_multipart_uploads(**request),
            )
            for upload in response.get("Uploads") or ():
                if not isinstance(upload, dict):
                    continue
                object_key = str(upload.get("Key", ""))
                upload_id = str(upload.get("UploadId", ""))
                initiated_at = upload.get("Initiated")
                if not object_key or not upload_id or not isinstance(initiated_at, datetime):
                    _LOG.warning(
                        "ignoring incomplete archive multipart upload with invalid listing metadata"
                    )
                    continue
                if initiated_at.tzinfo is None:
                    _LOG.warning(
                        "ignoring incomplete archive multipart upload with naive initiation time"
                    )
                    continue
                if initiated_at.astimezone(UTC) >= cutoff:
                    continue
                self._client.abort_multipart_upload(
                    Bucket=self._bucket,
                    Key=object_key,
                    UploadId=upload_id,
                )
                aborted += 1

            if not response.get("IsTruncated"):
                return aborted
            next_key_marker = str(response.get("NextKeyMarker", ""))
            next_upload_id_marker = str(response.get("NextUploadIdMarker", ""))
            if not next_key_marker or not next_upload_id_marker:
                raise RuntimeError(
                    "multipart upload listing returned incomplete pagination markers"
                )
            request["KeyMarker"] = next_key_marker
            request["UploadIdMarker"] = next_upload_id_marker

    def discard_collection_archive_upload(self, *, archive_storage_prefix: str) -> None:
        archive_root = f"{archive_store_object_path(self._store.prefix, 'archives')}/"
        normalized_prefix = archive_storage_prefix.strip("/")
        if not normalized_prefix.startswith(archive_root):
            raise ValueError("archive upload prefix is outside the owned archive root")
        object_prefix = f"{normalized_prefix}/"
        request: dict[str, Any] = {"Bucket": self._bucket, "Prefix": object_prefix}
        while True:
            response = cast(dict[str, Any], self._client.list_multipart_uploads(**request))
            for upload in response.get("Uploads") or ():
                if not isinstance(upload, dict):
                    continue
                key = str(upload.get("Key", ""))
                upload_id = str(upload.get("UploadId", ""))
                if key.startswith(object_prefix) and upload_id:
                    self._client.abort_multipart_upload(
                        Bucket=self._bucket,
                        Key=key,
                        UploadId=upload_id,
                    )
            if not response.get("IsTruncated"):
                break
            next_key_marker = str(response.get("NextKeyMarker", ""))
            next_upload_id_marker = str(response.get("NextUploadIdMarker", ""))
            if not next_key_marker or not next_upload_id_marker:
                raise RuntimeError(
                    "multipart upload listing returned incomplete pagination markers"
                )
            request["KeyMarker"] = next_key_marker
            request["UploadIdMarker"] = next_upload_id_marker
        delete_object_versions_with_prefix(
            self._client,
            bucket=self._bucket,
            prefix=object_prefix,
        )

    def _head_object(
        self,
        *,
        object_key: str,
        version_id: str | None = None,
    ) -> dict[str, Any] | None:
        request: dict[str, Any] = {"Bucket": self._bucket, "Key": object_key}
        if version_id is not None:
            request["VersionId"] = version_id
        try:
            return cast(
                dict[str, Any],
                self._client.head_object(**request),
            )
        except Exception as exc:
            if _is_missing_object_error(exc):
                return None
            raise

    def _collection_receipt_from_head(
        self,
        *,
        object_id: str,
        kind: str,
        object_key: str,
        head: dict[str, Any],
        expected_bytes: int,
        expected_sha256: str,
        expected_stored_sha256: str,
        expected_storage_class: str,
        uploaded_at: str | None = None,
        retrieval_cache: RetrievalCacheReceipt | None = None,
    ) -> ArchiveObjectUploadReceipt:
        _validate_uploaded_archive_metadata(
            object_key=object_key,
            head=head,
            kind=kind,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
        )
        if self._uses_aws_restore_api():
            _validate_aws_storage_class(
                object_key=object_key,
                head=head,
                expected_storage_class=expected_storage_class,
            )
        stored_bytes = int(head.get("ContentLength", 0))
        if not _SHA256_RE.fullmatch(expected_stored_sha256):
            raise ValueError("stored archive object sha256 is invalid")
        metadata_stored_sha256 = _head_metadata(head).get(STORED_SHA256_METADATA)
        if metadata_stored_sha256 is not None and metadata_stored_sha256 != expected_stored_sha256:
            raise RuntimeError("stored archive object sha256 metadata mismatch")
        verified_at = utc_timestamp_now()
        return ArchiveObjectUploadReceipt(
            object_id=object_id,
            kind=kind,
            object_path=object_key,
            plaintext_bytes=expected_bytes,
            stored_bytes=stored_bytes,
            sha256=expected_sha256,
            stored_sha256=expected_stored_sha256,
            version_id=(str(head["VersionId"]) if head.get("VersionId") is not None else None),
            backend=self._store.backend,
            storage_class=_configured_s3_storage_class(expected_storage_class),
            uploaded_at=uploaded_at
            or _format_s3_timestamp(
                head.get("LastModified"),
                fallback=verified_at,
            ),
            verified_at=verified_at,
            retrieval_cache=retrieval_cache,
        )

    def verify_collection_archive(
        self,
        *,
        collection_id: int,
        archive: CollectionArchiveIdentity,
    ) -> None:
        _ = collection_id
        for expected in archive.objects:
            storage_class = _collection_object_storage_class(
                archive_storage_class=self._store.storage_class,
                kind=expected.kind,
            )
            head = self._head_object(
                object_key=expected.object_path,
                version_id=expected.version_id,
            )
            if head is None:
                raise ArchiveVerificationError(
                    f"remote collection {expected.kind} object is missing"
                )
            try:
                if expected.kind in ATTESTATION_OBJECT_KINDS:
                    _verify_remote_plaintext_attestation(
                        object_key=expected.object_path,
                        head=head,
                        kind=expected.kind,
                        expected=expected,
                    )
                else:
                    _verify_remote_collection_object(
                        object_key=expected.object_path,
                        head=head,
                        kind=expected.kind,
                        expected=expected,
                    )
                if self._uses_aws_restore_api():
                    _validate_aws_storage_class(
                        object_key=expected.object_path,
                        head=head,
                        expected_storage_class=storage_class,
                    )
            except RuntimeError as exc:
                raise ArchiveVerificationError(
                    f"remote collection {expected.kind} object does not match its upload record"
                ) from exc

    def delete_collection_archive(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> None:
        _ = collection_id
        if not objects:
            raise ValueError("collection archive has no objects")
        object_paths = tuple(current.object_path for current in objects)
        archive_root = f"{archive_store_object_path(self._store.prefix, 'archives')}/"
        archive_prefixes = {_archive_storage_prefix(path) for path in object_paths}
        if len(archive_prefixes) != 1 or any(
            not path.startswith(archive_root) for path in object_paths
        ):
            raise ValueError("collection archive paths are outside one owned archive prefix")
        for path in object_paths:
            delete_exact_object(self._client, bucket=self._bucket, key=path)
        remaining = [
            path for path in object_paths if self._head_object(object_key=path) is not None
        ]
        if remaining:
            raise RuntimeError(
                "collection archive deletion could not be verified: " + ", ".join(remaining)
            )

    def publish_collection_metadata(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        manifest: bytes,
    ) -> MutableManifestReceipt:
        self._put_archive_root_guidance()
        object_key = f"{archive_storage_prefix.strip('/')}/metadata.json.age"
        manifest_sha256 = hashlib.sha256(manifest).hexdigest()
        existing = self._head_object(object_key=object_key)
        if existing is not None:
            metadata = _head_metadata(existing)
            if (
                metadata.get("collection-metadata-format") == "riverhog-collection-metadata/v1"
                and metadata.get("riverhog-collection-id") == str(collection_id)
                and metadata.get(ENCRYPTION_METADATA) == AGE_SCRYPT_ENCRYPTION
                and metadata.get(PLAINTEXT_BYTES_METADATA) == str(len(manifest))
                and metadata.get(PLAINTEXT_SHA256_METADATA) == manifest_sha256
            ):
                existing_version_id = _provider_version_id(existing)
                stored = b"".join(
                    self._iter_s3_stored_object(
                        object_key,
                        version_id=existing_version_id,
                    )
                )
                return MutableManifestReceipt(
                    object_path=object_key,
                    version_id=existing_version_id,
                    stored_bytes=len(stored),
                    stored_sha256=hashlib.sha256(stored).hexdigest(),
                    published_at=_format_s3_timestamp(
                        existing.get("LastModified"),
                        fallback=utc_timestamp_now(),
                    ),
                )
        ciphertext = encrypt_age_scrypt(
            manifest,
            self._config.archive_passphrase,
            log_n=self._config.archive_scrypt_work_factor,
        )
        response = self._client.put_object(
            Bucket=self._bucket,
            Key=object_key,
            Body=ciphertext,
            ContentLength=len(ciphertext),
            Metadata={
                "collection-metadata-format": "riverhog-collection-metadata/v1",
                "riverhog-collection-id": str(collection_id),
                ENCRYPTION_METADATA: AGE_SCRYPT_ENCRYPTION,
                PLAINTEXT_BYTES_METADATA: str(len(manifest)),
                PLAINTEXT_SHA256_METADATA: manifest_sha256,
                STORED_SHA256_METADATA: hashlib.sha256(ciphertext).hexdigest(),
            },
        )
        response_version_id = (
            str(response["VersionId"]) if response.get("VersionId") is not None else None
        )
        persisted = self._head_object(
            object_key=object_key,
            version_id=response_version_id,
        )
        if persisted is None:
            raise RuntimeError("persisted collection metadata is missing")
        return MutableManifestReceipt(
            object_path=object_key,
            version_id=(
                str(persisted["VersionId"]) if persisted.get("VersionId") is not None else None
            ),
            stored_bytes=len(ciphertext),
            stored_sha256=hashlib.sha256(ciphertext).hexdigest(),
            published_at=_format_s3_timestamp(
                persisted.get("LastModified"),
                fallback=utc_timestamp_now(),
            ),
        )

    def read_archive_artifact(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
    ) -> ArchiveArtifactRead:
        if object.kind not in {"manifest", "proof"}:
            raise ValueError("only collection manifests and proofs are archive artifacts")
        if object.stored_sha256 is None:
            raise ValueError("archive artifact is missing its stored sha256")
        head = self._head_object(
            object_key=object.object_path,
            version_id=object.version_id,
        )
        if head is None:
            raise RuntimeError(f"Archive object is missing: {object.object_path}")
        metadata = _head_metadata(head)
        try:
            plaintext_bytes = int(metadata[PLAINTEXT_BYTES_METADATA])
            plaintext_sha256 = metadata[PLAINTEXT_SHA256_METADATA]
        except (KeyError, ValueError) as exc:
            raise RuntimeError(
                f"Archive object has invalid v1 validation metadata: {object.object_path}"
            ) from exc
        storage_class = _normalized_s3_storage_class(head) or "STANDARD"
        receipt = self._collection_receipt_from_head(
            object_id=object.object_id,
            kind=object.kind,
            object_key=object.object_path,
            head=head,
            expected_bytes=plaintext_bytes,
            expected_sha256=plaintext_sha256,
            expected_stored_sha256=object.stored_sha256,
            expected_storage_class=storage_class,
        )
        current = ArchiveObjectIdentity(
            object_id=receipt.object_id,
            kind=receipt.kind,
            object_path=receipt.object_path,
            plaintext_bytes=receipt.plaintext_bytes,
            stored_bytes=receipt.stored_bytes,
            sha256=receipt.sha256,
            stored_sha256=receipt.stored_sha256,
            version_id=receipt.version_id,
        )
        _verify_remote_collection_object(
            object_key=object.object_path,
            head=head,
            kind=object.kind,
            expected=current,
        )
        content = b"".join(
            self.iter_archive_object(
                collection_id=collection_id,
                object=current,
            )
        )
        if len(content) != receipt.plaintext_bytes:
            raise RuntimeError(
                f"Archive object plaintext size does not match its metadata: {object.object_path}"
            )
        if hashlib.sha256(content).hexdigest() != receipt.sha256:
            raise RuntimeError(
                f"Archive object plaintext sha256 does not match its metadata: {object.object_path}"
            )
        return ArchiveArtifactRead(receipt=receipt, content=content)

    def replace_archive_proof(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        proof_bytes: bytes,
    ) -> ArchiveObjectUploadReceipt:
        _ = collection_id
        if object.object_id != "proof" or object.kind != "proof":
            raise ValueError("archive proof replacement requires the proof object")
        if not object.object_path.endswith("/manifest.json.ots.age"):
            raise ValueError("archive proof path is not canonical")
        archive_root = f"{archive_store_object_path(self._store.prefix, 'archives')}/"
        if not object.object_path.startswith(archive_root):
            raise ValueError("archive proof path is outside the configured archive root")

        proof_sha256 = hashlib.sha256(proof_bytes).hexdigest()
        existing = self._head_object(object_key=object.object_path)
        existing_stored_sha256 = (
            _head_metadata(existing).get(STORED_SHA256_METADATA) if existing is not None else None
        )
        if existing is not None and existing_stored_sha256 is not None:
            try:
                receipt = self._collection_receipt_from_head(
                    object_id="proof",
                    kind="proof",
                    object_key=object.object_path,
                    head=existing,
                    expected_bytes=len(proof_bytes),
                    expected_sha256=proof_sha256,
                    expected_stored_sha256=existing_stored_sha256,
                    expected_storage_class="STANDARD",
                )
                _verify_remote_collection_object(
                    object_key=object.object_path,
                    head=existing,
                    kind="proof",
                    expected=ArchiveObjectIdentity(
                        object_id=receipt.object_id,
                        kind=receipt.kind,
                        object_path=receipt.object_path,
                        plaintext_bytes=receipt.plaintext_bytes,
                        stored_bytes=receipt.stored_bytes,
                        sha256=receipt.sha256,
                        stored_sha256=receipt.stored_sha256,
                        version_id=receipt.version_id,
                    ),
                )
                return receipt
            except RuntimeError:
                pass

        ciphertext = encrypt_age_scrypt(
            proof_bytes,
            self._config.archive_passphrase,
            log_n=self._config.archive_scrypt_work_factor,
        )
        stored_sha256 = hashlib.sha256(ciphertext).hexdigest()
        uploaded_at = utc_timestamp_now()
        metadata = {
            "riverhog-format": ROOT_PROOF_STORAGE_FORMAT,
            PLAINTEXT_BYTES_METADATA: str(len(proof_bytes)),
            PLAINTEXT_SHA256_METADATA: proof_sha256,
            STORED_SHA256_METADATA: stored_sha256,
        }
        if existing is not None and (
            manifest_sha256 := _head_metadata(existing).get("riverhog-manifest-sha256")
        ):
            metadata["riverhog-manifest-sha256"] = manifest_sha256
        response = self._client.put_object(
            Bucket=self._bucket,
            Key=object.object_path,
            Body=ciphertext,
            ContentLength=len(ciphertext),
            Metadata=metadata,
        )
        head = self._head_object(
            object_key=object.object_path,
            version_id=(
                str(response["VersionId"]) if response.get("VersionId") is not None else None
            ),
        )
        if head is None:
            raise RuntimeError("persisted archive proof is missing")
        return self._collection_receipt_from_head(
            object_id="proof",
            kind="proof",
            object_key=object.object_path,
            head=head,
            expected_bytes=len(proof_bytes),
            expected_sha256=proof_sha256,
            expected_stored_sha256=stored_sha256,
            expected_storage_class="STANDARD",
            uploaded_at=uploaded_at,
        )

    def publish_archive_attestation(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        checksums: bytes,
        signature: bytes,
        proof: bytes,
    ) -> CollectionArchiveUploadReceipt:
        _ = collection_id
        self._put_archive_root_guidance()
        rows = (
            ("checksums", checksums, False),
            ("signature", signature, False),
            ("signature-proof", proof, True),
        )
        return CollectionArchiveUploadReceipt(
            objects=tuple(
                self._put_plaintext_attestation_artifact(
                    object_id=object_id,
                    object_key=(
                        f"{archive_storage_prefix.strip('/')}/{ATTESTATION_FILENAMES[object_id]}"
                    ),
                    content=content,
                    accept_existing=accept_existing,
                )
                for object_id, content, accept_existing in rows
            )
        )

    def read_archive_attestation_artifact(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
    ) -> ArchiveArtifactRead:
        _ = collection_id
        if object.kind not in ATTESTATION_OBJECT_KINDS:
            raise ValueError("archive artifact is not an attestation artifact")
        expected_filename = ATTESTATION_FILENAMES[object.object_id]
        if not object.object_path.endswith(f"/{expected_filename}"):
            raise ValueError("archive attestation artifact path is not canonical")
        head = self._head_object(
            object_key=object.object_path,
            version_id=object.version_id,
        )
        if head is None:
            raise RuntimeError(f"Archive object is missing: {object.object_path}")
        _verify_remote_plaintext_attestation(
            object_key=object.object_path,
            head=head,
            kind=object.kind,
            expected=object,
        )
        content = b"".join(
            self._iter_s3_stored_object(
                object.object_path,
                version_id=object.version_id,
            )
        )
        if len(content) != object.stored_bytes:
            raise RuntimeError("archive attestation artifact byte count changed")
        if hashlib.sha256(content).hexdigest() != object.stored_sha256:
            raise RuntimeError("archive attestation artifact sha256 changed")
        return ArchiveArtifactRead(
            receipt=self._plaintext_attestation_receipt(
                object_id=object.object_id,
                object_key=object.object_path,
                content=content,
                head=head,
            ),
            content=content,
        )

    def replace_archive_attestation_proof(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        proof_bytes: bytes,
    ) -> ArchiveObjectUploadReceipt:
        _ = collection_id
        if object.object_id != "signature-proof" or object.kind != "signature-proof":
            raise ValueError("archive attestation proof replacement requires its proof object")
        if not object.object_path.endswith("/SHA256SUMS.minisig.ots"):
            raise ValueError("archive attestation proof path is not canonical")
        return self._put_plaintext_attestation_artifact(
            object_id="signature-proof",
            object_key=object.object_path,
            content=proof_bytes,
            accept_existing=False,
            replace=True,
        )

    def _put_plaintext_attestation_artifact(
        self,
        *,
        object_id: str,
        object_key: str,
        content: bytes,
        accept_existing: bool,
        replace: bool = False,
    ) -> ArchiveObjectUploadReceipt:
        sha256 = hashlib.sha256(content).hexdigest()
        existing = self._head_object(object_key=object_key)
        if existing is not None:
            existing_content = b"".join(
                self._iter_s3_stored_object(
                    object_key,
                    version_id=_provider_version_id(existing),
                )
            )
            if existing_content == content or (accept_existing and not replace):
                return self._plaintext_attestation_receipt(
                    object_id=object_id,
                    object_key=object_key,
                    content=existing_content,
                    head=existing,
                )
            if not replace:
                raise RuntimeError("archive attestation artifact differs from its durable copy")
        uploaded_at = utc_timestamp_now()
        response = self._client.put_object(
            Bucket=self._bucket,
            Key=object_key,
            Body=content,
            ContentLength=len(content),
            Metadata={
                "riverhog-format": archive_object_storage_format(object_id),
                PLAINTEXT_BYTES_METADATA: str(len(content)),
                PLAINTEXT_SHA256_METADATA: sha256,
                STORED_SHA256_METADATA: sha256,
            },
        )
        head = self._head_object(
            object_key=object_key,
            version_id=(
                str(response["VersionId"]) if response.get("VersionId") is not None else None
            ),
        )
        if head is None:
            raise RuntimeError("persisted archive attestation artifact is missing")
        receipt = self._plaintext_attestation_receipt(
            object_id=object_id,
            object_key=object_key,
            content=content,
            head=head,
            uploaded_at=uploaded_at,
        )
        persisted = b"".join(
            self._iter_s3_stored_object(
                object_key,
                version_id=receipt.version_id,
            )
        )
        if persisted != content:
            raise RuntimeError("persisted archive attestation artifact differs from its input")
        return receipt

    def _plaintext_attestation_receipt(
        self,
        *,
        object_id: str,
        object_key: str,
        content: bytes,
        head: dict[str, Any],
        uploaded_at: str | None = None,
    ) -> ArchiveObjectUploadReceipt:
        sha256 = hashlib.sha256(content).hexdigest()
        return self._collection_receipt_from_head(
            object_id=object_id,
            kind=object_id,
            object_key=object_key,
            head=head,
            expected_bytes=len(content),
            expected_sha256=sha256,
            expected_stored_sha256=sha256,
            expected_storage_class="STANDARD",
            uploaded_at=uploaded_at,
        )

    def _put_archive_root_guidance(self) -> None:
        artifacts = [
            (
                "README.md",
                _bucket_recovery_readme().encode("utf-8"),
                "encrypted-archive-readme-v1",
            ),
            (
                "AGENTS.md",
                archive_agents_guidance().encode("utf-8"),
                "encrypted-archive-agents-v1",
            ),
        ]
        if self._config.attestation_public_key_file is not None:
            public_key = self._config.attestation_public_key_file.read_bytes()
            if not public_key.strip():
                raise RuntimeError("attestation public key is empty")
            artifacts.append(
                (
                    "minisign.pub",
                    public_key,
                    "riverhog-attestation-public-key-v1",
                )
            )
        for filename, content, format_name in artifacts:
            object_key = archive_store_object_path(self._store.prefix, filename)
            existing = self._head_object(object_key=object_key)
            if (
                existing is not None
                and _head_metadata(existing).get("archive-guidance-format") == format_name
                and b"".join(
                    self._iter_s3_stored_object(
                        object_key,
                        version_id=_provider_version_id(existing),
                    )
                )
                == content
            ):
                continue
            self._client.put_object(
                Bucket=self._bucket,
                Key=object_key,
                Body=content,
                ContentLength=len(content),
                Metadata={"archive-guidance-format": format_name},
            )

    def prepare_archive_objects_read(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
        retrieval_tier: str,
        hold_days: int,
        requested_at: str,
        estimated_ready_at: str,
    ) -> ArchiveReadStatus:
        _ = collection_id
        statuses = [
            self._request_collection_object_restore(
                object=current,
                retrieval_tier=retrieval_tier,
                hold_days=hold_days,
                requested_at=requested_at,
                estimated_ready_at=estimated_ready_at,
            )
            for current in objects
        ]
        if not statuses:
            return ArchiveReadStatus(state="ready", ready_at=requested_at)
        return _combine_archive_read_statuses(statuses)

    def _request_collection_object_restore(
        self,
        *,
        object: ArchiveObjectIdentity,
        retrieval_tier: str,
        hold_days: int,
        requested_at: str,
        estimated_ready_at: str,
    ) -> ArchiveReadStatus:
        object_path = object.object_path
        head = self._head_object(
            object_key=object_path,
            version_id=object.version_id,
        )
        if head is None:
            raise RuntimeError(f"Archive object is missing: {object_path}")
        _validate_uploaded_archive_metadata(
            object_key=object_path,
            head=head,
            kind=object.kind,
            expected_bytes=object.plaintext_bytes,
            expected_sha256=object.sha256,
        )
        if _is_immediately_readable_storage_class(head):
            return ArchiveReadStatus(
                state="ready",
                ready_at=requested_at,
                message="Collection archive object is immediately readable.",
            )
        if not self._uses_aws_restore_api():
            raise RuntimeError(
                "archive object read preparation requires an AWS archive store backend"
            )
        restore_request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": object_path,
            "RestoreRequest": {
                "Days": hold_days,
                "GlacierJobParameters": {"Tier": _aws_restore_tier(retrieval_tier)},
            },
        }
        if object.version_id is not None:
            restore_request["VersionId"] = object.version_id
        try:
            self._client.restore_object(
                **restore_request,
            )
        except Exception as exc:
            restore_error = _restore_request_error_code(exc)
            if restore_error == "ObjectAlreadyInActiveTierError":
                return ArchiveReadStatus(
                    state="ready",
                    ready_at=requested_at,
                    message="Collection archive object is already readable.",
                )
            if restore_error != "RestoreAlreadyInProgress":
                raise
        return self._collection_object_restore_status(
            object=object,
            requested_at=requested_at,
            estimated_ready_at=estimated_ready_at,
            estimated_expires_at=None,
        )

    def get_archive_objects_read_status(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
        requested_at: str,
        estimated_ready_at: str | None,
        estimated_expires_at: str | None,
    ) -> ArchiveReadStatus:
        _ = collection_id
        statuses = [
            self._collection_object_restore_status(
                object=current,
                requested_at=requested_at,
                estimated_ready_at=estimated_ready_at,
                estimated_expires_at=estimated_expires_at,
            )
            for current in objects
        ]
        if not statuses:
            return ArchiveReadStatus(state="ready", ready_at=requested_at)
        return _combine_archive_read_statuses(statuses)

    def _collection_object_restore_status(
        self,
        *,
        object: ArchiveObjectIdentity,
        requested_at: str,
        estimated_ready_at: str | None,
        estimated_expires_at: str | None,
    ) -> ArchiveReadStatus:
        object_path = object.object_path
        head = self._head_object(
            object_key=object_path,
            version_id=object.version_id,
        )
        if head is None:
            raise RuntimeError(f"Archive object is missing: {object_path}")
        _validate_uploaded_archive_metadata(
            object_key=object_path,
            head=head,
            kind=object.kind,
            expected_bytes=object.plaintext_bytes,
            expected_sha256=object.sha256,
        )
        restore = _parse_restore_header(head.get("Restore"))
        if restore is None:
            if _is_immediately_readable_storage_class(head):
                return ArchiveReadStatus(
                    state="ready",
                    ready_at=requested_at,
                    message="Collection archive object is immediately readable.",
                )
            return ArchiveReadStatus(
                state="expired",
                message="AWS archive object is not currently restored.",
            )
        if restore["ongoing"]:
            return ArchiveReadStatus(
                state="requested",
                ready_at=estimated_ready_at,
                expires_at=restore["expires_at"] or estimated_expires_at,
                message="AWS archive retrieval is still in progress.",
            )
        if restore["expires_at"] is not None and restore["expires_at"] <= utc_timestamp_now():
            return ArchiveReadStatus(
                state="expired",
                expires_at=restore["expires_at"],
                message="AWS archive object restore has expired.",
            )
        return ArchiveReadStatus(
            state="ready",
            ready_at=utc_timestamp_now(),
            expires_at=restore["expires_at"],
            message="AWS archive object is readable.",
        )

    def iter_archive_object(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]:
        return iter_decrypt_age_scrypt(
            self.iter_stored_archive_object(
                collection_id=collection_id,
                object=object,
                attribution=attribution,
            ),
            self._config.archive_passphrase,
        )

    def iter_stored_archive_object(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
        attribution: DownloadAttribution | None = None,
    ) -> Iterator[bytes]:
        object_path = object.object_path
        head = self._head_object(
            object_key=object_path,
            version_id=object.version_id,
        )
        if head is None:
            raise RuntimeError(f"Archive object is missing: {object_path}")
        _validate_uploaded_archive_metadata(
            object_key=object_path,
            head=head,
            kind=object.kind,
            expected_bytes=object.plaintext_bytes,
            expected_sha256=object.sha256,
        )
        status = self.get_archive_objects_read_status(
            collection_id=collection_id,
            objects=(object,),
            requested_at=utc_timestamp_now(),
            estimated_ready_at=None,
            estimated_expires_at=None,
        )
        if status.state != "ready":
            raise RuntimeError(f"Archive object is not readable yet: {object_path}")
        if self._cloudfront_client is None or self._cloudfront_signer is None:
            content = self._iter_s3_stored_object(
                object_path,
                version_id=object.version_id,
            )
        else:
            content = self._iter_cloudfront_stored_object(object)
        if self._download_allowance is None:
            return content
        return self._download_allowance.track(
            store=self._store.name,
            expected_bytes=object.stored_bytes,
            content=content,
            attribution=attribution,
        )

    def stored_archive_object_sha256(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
    ) -> str:
        digest = self._hash_stored_archive_object(
            collection_id=collection_id,
            object=object,
        )
        if object.stored_sha256 and object.stored_sha256 != "0" * 64:
            if digest != object.stored_sha256:
                raise RuntimeError(f"Archive ciphertext sha256 mismatch: {object.object_path}")
        return digest

    def _hash_stored_archive_object(
        self,
        *,
        collection_id: int,
        object: ArchiveObjectIdentity,
    ) -> str:
        digest = hashlib.sha256()
        byte_count = 0
        for chunk in self.iter_stored_archive_object(
            collection_id=collection_id,
            object=object,
        ):
            byte_count += len(chunk)
            digest.update(chunk)
        if byte_count != object.stored_bytes:
            raise RuntimeError(f"Archive ciphertext byte count mismatch: {object.object_path}")
        return digest.hexdigest()

    def _iter_s3_stored_object(
        self,
        object_path: str,
        *,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        request: dict[str, Any] = {"Bucket": self._bucket, "Key": object_path}
        if version_id is not None:
            request["VersionId"] = version_id
        response = self._client.get_object(**request)
        body = response["Body"]
        try:
            yield from body.iter_chunks(chunk_size=1024 * 1024)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    def _iter_cloudfront_stored_object(
        self,
        object: ArchiveObjectIdentity,
    ) -> Iterator[bytes]:
        client = self._cloudfront_client
        signer = self._cloudfront_signer
        base_url = self._store.cloudfront_base_url
        if client is None or signer is None or base_url is None:
            raise RuntimeError("CloudFront download configuration is incomplete")
        object_url = f"{base_url}/{quote(object.object_path, safe='/')}"
        if object.version_id is not None:
            object_url = f"{object_url}?versionId={quote(object.version_id, safe='')}"
        signed_url = signer.generate_presigned_url(
            object_url,
            date_less_than=datetime.now(UTC) + _CLOUDFRONT_URL_TTL,
        )
        try:
            with client.stream(
                "GET",
                signed_url,
                headers={"Accept-Encoding": "identity"},
            ) as response:
                if not response.is_success:
                    raise RuntimeError(
                        "CloudFront archive download failed with HTTP "
                        f"{response.status_code}: {object.object_path}"
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None and int(content_length) != object.stored_bytes:
                    raise RuntimeError(
                        "CloudFront archive download length does not match verified metadata: "
                        f"{object.object_path}"
                    )
                yield from response.iter_bytes(chunk_size=1024 * 1024)
        except httpx.HTTPError:
            raise RuntimeError(
                f"CloudFront archive download failed: {object.object_path}"
            ) from None

    def cleanup_archive_objects_read(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> None:
        _ = collection_id, objects
        return

    def _uses_aws_restore_api(self) -> bool:
        return self._store.backend.casefold() == "aws"


def _combine_archive_read_statuses(
    statuses: list[ArchiveReadStatus],
) -> ArchiveReadStatus:
    if any(status.state == "expired" for status in statuses):
        return ArchiveReadStatus(state="expired")
    if statuses and all(status.state == "ready" for status in statuses):
        return ArchiveReadStatus(
            state="ready",
            ready_at=_max_timestamp(status.ready_at for status in statuses),
            expires_at=_min_timestamp(status.expires_at for status in statuses),
            message="Archive objects are readable.",
        )
    return ArchiveReadStatus(
        state="requested",
        ready_at=_max_timestamp(status.ready_at for status in statuses),
        expires_at=_min_timestamp(status.expires_at for status in statuses),
        message="Archive object retrieval is still in progress.",
    )


def _archive_storage_prefix(object_path: str) -> str:
    for marker in ("/volumes/", "/objects/"):
        if marker in object_path:
            return object_path.split(marker, 1)[0]
    return object_path.rsplit("/", 1)[0]


def _bucket_recovery_readme() -> str:
    return f"""# Encrypted Riverhog Archive Recovery

{ARCHIVE_DATA_LOSS_WARNING}

Riverhog archives are recoverable with standard S3, `age`, `ots`, `sha256sum`,
`minisign`, and `tar` tools. The `riverhog-recover` command is the maintained
reference implementation of that process. Archive paths are intentionally opaque.

You need read access to this bucket or prefix and the archive passphrase. S3
credentials and the archive passphrase are separate secrets. Treat every object
as read-only unless an exact mutation has been explicitly authorized.

## Locate and verify an archive

List `archives/`, decrypt each candidate `metadata.json.age` to identify the
collection, then download and decrypt `manifest.json.age`. Verify its required
OpenTimestamps proof before recovery:

```sh
aws s3 cp s3://BUCKET/PREFIX/archives/ARCHIVE_ID/manifest.json.age .
aws s3 cp s3://BUCKET/PREFIX/archives/ARCHIVE_ID/manifest.json.ots.age .
age --decrypt -o manifest.json manifest.json.age
age --decrypt -o manifest.json.ots manifest.json.ots.age
ots verify manifest.json.ots -f manifest.json
```

If present, verify `SHA256SUMS` with `SHA256SUMS.minisig` and a public key
obtained from an independent trust source, then verify
`SHA256SUMS.minisig.ots`. The bucket-root `minisign.pub` is a convenience copy,
not an independent trust anchor.

## Recover files

The JSON manifest names encrypted volumes under `volumes/` and maps their
plaintext ranges to logical files. Restore Glacier-class volumes before download.
Decrypt `pack-*.tar.age` volumes and extract their mapped tar members; decrypt
`segment-*.bin.age` volumes and concatenate mapped ranges in manifest order.
Finally verify every recovered file byte count and SHA-256 against the manifest.
"""


def _max_timestamp(values: Iterable[str | None]) -> str | None:
    candidates = [value for value in values if value is not None]
    if not candidates:
        return None
    return max(candidates)


def _min_timestamp(values: Iterable[str | None]) -> str | None:
    candidates = [value for value in values if value is not None]
    if not candidates:
        return None
    return min(candidates)


def _head_metadata(head: dict[str, Any]) -> dict[str, str]:
    metadata = head.get("Metadata", {})
    if not isinstance(metadata, dict):
        return {}
    return {str(key).lower(): str(value) for key, value in metadata.items()}


def _provider_version_id(head: dict[str, Any]) -> str | None:
    value = head.get("VersionId")
    return str(value) if value is not None else None


def _validate_uploaded_archive_metadata(
    *,
    object_key: str,
    head: dict[str, Any],
    kind: str | None = None,
    expected_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> None:
    metadata = _head_metadata(head)
    storage_format = metadata.get("riverhog-format")
    expected_format = archive_object_storage_format(kind) if kind is not None else None
    if storage_format is None or (
        expected_format is not None and storage_format != expected_format
    ):
        raise RuntimeError(f"Archive object has invalid v1 storage format metadata: {object_key}")
    if kind is None and storage_format not in ARCHIVE_OBJECT_STORAGE_FORMATS.values():
        raise RuntimeError(f"Archive object has unknown v1 storage format metadata: {object_key}")
    metadata_bytes = metadata.get(PLAINTEXT_BYTES_METADATA)
    if metadata_bytes is None:
        raise RuntimeError(f"Archive object is missing plaintext byte metadata: {object_key}")
    try:
        plaintext_bytes = int(metadata_bytes)
    except ValueError as exc:
        raise RuntimeError(
            f"Archive object has invalid plaintext byte metadata: {object_key}"
        ) from exc
    if plaintext_bytes < 0:
        raise RuntimeError(f"Archive object has invalid plaintext byte metadata: {object_key}")
    if expected_bytes is not None and plaintext_bytes != expected_bytes:
        raise RuntimeError(f"Archive object plaintext size does not match its record: {object_key}")
    metadata_sha256 = metadata.get(PLAINTEXT_SHA256_METADATA)
    if metadata_sha256 is not None and not _SHA256_RE.fullmatch(metadata_sha256):
        raise RuntimeError(f"Archive object has invalid plaintext sha256 metadata: {object_key}")
    if expected_sha256 is not None and metadata_sha256 != expected_sha256:
        raise RuntimeError(f"Archive object sha256 does not match its record: {object_key}")


def _verify_remote_collection_object(
    *,
    object_key: str,
    head: dict[str, Any],
    kind: str,
    expected: ArchiveObjectIdentity,
) -> None:
    try:
        stored_bytes = int(head["ContentLength"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Archive object has invalid stored byte count: {object_key}") from exc
    if stored_bytes != expected.stored_bytes:
        raise RuntimeError(f"Archive object stored byte count changed: {object_key}")
    _validate_uploaded_archive_metadata(
        object_key=object_key,
        head=head,
        kind=kind,
        expected_bytes=expected.plaintext_bytes,
        expected_sha256=expected.sha256,
    )
    metadata = _head_metadata(head)
    metadata_stored_sha256 = metadata.get(STORED_SHA256_METADATA)
    if (
        metadata_stored_sha256 is not None
        and expected.stored_sha256 is not None
        and metadata_stored_sha256 != expected.stored_sha256
    ):
        raise RuntimeError(f"Archive object stored sha256 metadata changed: {object_key}")


def _verify_remote_plaintext_attestation(
    *,
    object_key: str,
    head: dict[str, Any],
    kind: str,
    expected: ArchiveObjectIdentity,
) -> None:
    try:
        stored_bytes = int(head["ContentLength"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Archive object has invalid stored byte count: {object_key}") from exc
    if stored_bytes != expected.stored_bytes or stored_bytes != expected.plaintext_bytes:
        raise RuntimeError(f"Archive attestation artifact byte count changed: {object_key}")
    _validate_uploaded_archive_metadata(
        object_key=object_key,
        head=head,
        kind=kind,
        expected_bytes=expected.plaintext_bytes,
        expected_sha256=expected.sha256,
    )
    metadata = _head_metadata(head)
    if metadata.get(PLAINTEXT_SHA256_METADATA) != expected.sha256:
        raise RuntimeError(f"Archive attestation artifact sha256 changed: {object_key}")
    if metadata.get(STORED_SHA256_METADATA) != expected.stored_sha256:
        raise RuntimeError(f"Archive attestation stored sha256 changed: {object_key}")


def _format_s3_timestamp(value: object, *, fallback: str) -> str:
    if isinstance(value, datetime):
        return format_utc_timestamp(value)
    return fallback


def _parse_restore_header(value: object) -> _RestoreHeader | None:
    if value is None:
        return None
    text = str(value)
    ongoing_match = re.search(r'ongoing-request="(true|false)"', text)
    if ongoing_match is None:
        return None
    expires_at: str | None = None
    expiry_match = re.search(r'expiry-date="([^"]+)"', text)
    if expiry_match is not None:
        expires_at = _format_s3_timestamp(
            parsedate_to_datetime(expiry_match.group(1)),
            fallback=expiry_match.group(1),
        )
    return {
        "ongoing": ongoing_match.group(1) == "true",
        "expires_at": expires_at,
    }


def _is_immediately_readable_storage_class(head: dict[str, Any]) -> bool:
    storage_class = _normalized_s3_storage_class(head)
    if storage_class == "INTELLIGENT_TIERING" and _normalized_s3_archive_status(head):
        return False
    return storage_class in {"", "STANDARD", "REDUCED_REDUNDANCY", "INTELLIGENT_TIERING"}


def _normalized_s3_storage_class(head: dict[str, Any]) -> str:
    return str(head.get("StorageClass", "")).strip().upper()


def _normalized_s3_archive_status(head: dict[str, Any]) -> str:
    return str(head.get("ArchiveCopyStatus", "")).strip().upper()


def _configured_s3_storage_class(value: str) -> str:
    normalized = value.strip().upper()
    if normalized in {"", "STANDARD"}:
        return "STANDARD"
    return normalized


def _collection_object_storage_class(
    *,
    archive_storage_class: str,
    kind: str,
) -> str:
    if kind in {"pack", "file", "segment"}:
        return _configured_s3_storage_class(archive_storage_class)
    return "STANDARD"


def _validate_aws_storage_class(
    *,
    object_key: str,
    head: dict[str, Any],
    expected_storage_class: str,
) -> None:
    expected = _configured_s3_storage_class(expected_storage_class)
    actual = _normalized_s3_storage_class(head) or "STANDARD"
    if actual == expected:
        return
    raise RuntimeError(
        "existing AWS archive-store object storage class does not match Riverhog's "
        f"expected class for {object_key}: expected {expected}, got {actual}. "
        "Delete the stale object or choose a fresh archive store prefix before rerunning."
    )


def _aws_restore_tier(value: str) -> str:
    if value == "standard":
        return "Standard"
    return "Bulk"


def _is_missing_object_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    if not isinstance(error, dict):
        return False
    code = str(error.get("Code", "")).strip()
    return code in {"NoSuchKey", "404", "NotFound"}


def _is_missing_upload_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    if not isinstance(error, dict):
        return False
    code = str(error.get("Code", "")).strip()
    return code in {"NoSuchUpload", "404", "NotFound"}


def _restore_request_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error", {})
    if not isinstance(error, dict):
        return None
    code = str(error.get("Code", "")).strip()
    return code or None
