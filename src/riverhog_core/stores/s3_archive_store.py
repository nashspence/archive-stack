from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, TypedDict, cast
from urllib.parse import quote

import httpx
import yaml
from botocore.signers import CloudFrontSigner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from riverhog_age import (
    AEAD_TAG_SIZE,
    CHUNK_SIZE,
    ResumableAgeScryptSession,
    age_ciphertext_len_for_plaintext_len,
    encrypt_age_scrypt,
    iter_decrypt_age_scrypt,
)
from riverhog_core.archive_custody import ARCHIVE_CUSTODY_WARNING, archive_agents_guidance
from riverhog_core.archive_object_paths import (
    archive_id_from_storage_prefix,
    archive_store_object_path,
)
from riverhog_core.archive_objects import (
    PACK_PAYLOAD_LIMIT,
    STORED_OBJECT_LIMIT,
    CollectionArchive,
    CollectionArchiveDataObject,
    max_age_plaintext_object_bytes,
)
from riverhog_core.ports.archive_store import (
    ArchiveMultipartUploadedPart,
    ArchiveMultipartUploadState,
    ArchiveMultipartUploadTracker,
    ArchiveObjectIdentity,
    ArchiveObjectUploadReceipt,
    ArchiveReadStatus,
    ArchiveVerificationError,
    CollectionArchiveIdentity,
    CollectionArchiveUploadReceipt,
)
from riverhog_core.runtime_config import ArchiveStoreConfig, RuntimeConfig
from riverhog_core.stores.s3_support import create_archive_s3_client
from riverhog_core.timestamps import format_utc_timestamp, utc_timestamp_now

COLLECTION_BYTES_METADATA = "riverhog-collection-bytes"
COLLECTION_SHA256_METADATA = "riverhog-collection-sha256"
ENCRYPTION_METADATA = "riverhog-encryption"
PLAINTEXT_BYTES_METADATA = "riverhog-plaintext-bytes"
PLAINTEXT_SHA256_METADATA = "riverhog-plaintext-sha256"
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


@dataclass(frozen=True, slots=True)
class ArchiveMultipartTiming:
    object_id: str
    stored_bytes: int
    parts: int
    concurrency: int
    elapsed_seconds: float
    preparation_seconds: float
    upload_request_seconds: float
    checkpoint_seconds: float


def _multipart_part_size(content_length: int, configured_part_size: int) -> int:
    part_size = max(
        _MIN_MULTIPART_PART_SIZE,
        configured_part_size,
        (content_length + _MAX_MULTIPART_PARTS - 1) // _MAX_MULTIPART_PARTS,
    )
    if part_size > _MAX_MULTIPART_PART_SIZE:
        raise ValueError("collection archive stream exceeds S3 multipart object size limit")
    return part_size


def _should_log_multipart_progress(part_number: int, expected_part_count: int) -> bool:
    return (
        part_number == 1
        or part_number == expected_part_count
        or expected_part_count <= 20
        or part_number % 100 == 0
    )


def _is_chunk_iterable(content: Any) -> bool:
    return (
        isinstance(content, Iterable)
        and not isinstance(content, (bytes, bytearray, memoryview, str))
        and not callable(getattr(content, "read", None))
    )


def _should_use_multipart(*, content: Any, content_length: int) -> bool:
    return content_length > _MAX_SINGLE_PUT_OBJECT_SIZE or (
        _is_chunk_iterable(content) and content_length >= _MIN_MULTIPART_PART_SIZE
    )


def _iter_content_chunks(content: Any) -> Iterator[bytes]:
    if isinstance(content, bytes):
        yield content
        return
    if isinstance(content, bytearray):
        yield bytes(content)
        return
    if isinstance(content, memoryview):
        yield content.tobytes()
        return

    read = getattr(content, "read", None)
    if callable(read):
        while True:
            chunk = read(1024 * 1024)
            if not chunk:
                return
            yield bytes(chunk)

    for chunk in cast(Iterable[Any], content):
        yield bytes(chunk)


def _single_put_body(content: Any) -> Any:
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    if isinstance(content, memoryview):
        return content.tobytes()
    if callable(getattr(content, "read", None)):
        return content
    return b"".join(_iter_content_chunks(content))


def _iter_chunks_after_skipping(chunks: Iterable[bytes], skip_bytes: int) -> Iterator[bytes]:
    remaining = skip_bytes
    for chunk in chunks:
        if remaining <= 0:
            yield chunk
            continue
        if len(chunk) <= remaining:
            remaining -= len(chunk)
            continue
        yield chunk[remaining:]
        remaining = 0
    if remaining:
        raise ValueError("collection archive stream ended before resumable upload offset")


def _iter_encrypted_object(
    *,
    object: CollectionArchiveDataObject,
    session: ResumableAgeScryptSession,
) -> Iterator[bytes]:
    yield session.age_prefix
    chunks = iter(object.iter_plaintext())
    buffer = bytearray()
    chunk_index = 0
    plaintext_bytes = 0
    for chunk in chunks:
        buffer.extend(chunk)
        while len(buffer) > CHUNK_SIZE:
            plaintext = bytes(buffer[:CHUNK_SIZE])
            del buffer[:CHUNK_SIZE]
            plaintext_bytes += len(plaintext)
            yield session.encrypt_chunk(chunk_index, plaintext, final=False)
            chunk_index += 1
    plaintext_bytes += len(buffer)
    if plaintext_bytes != object.plaintext_bytes:
        raise ValueError("archive object stream size changed during encryption")
    yield session.encrypt_chunk(chunk_index, bytes(buffer), final=True)


def _encrypted_object_part_body(
    *,
    object: CollectionArchiveDataObject,
    session: ResumableAgeScryptSession,
    plan: Any,
) -> bytes:
    plaintext = _read_object_range(object, plan.plaintext_start, plan.plaintext_len)

    def provider(_chunk_index: int, start: int, end: int) -> bytes:
        relative_start = start - plan.plaintext_start
        relative_end = end - plan.plaintext_start
        return plaintext[relative_start:relative_end]

    return session.encrypt_part(plan, provider, plaintext_size=object.plaintext_bytes)


def _read_object_range(
    object: CollectionArchiveDataObject,
    offset: int,
    size: int,
) -> bytes:
    if size == 0:
        return b""
    out = bytearray()
    for chunk in object.iter_plaintext_range(offset, size):
        needed = size - len(out)
        out.extend(chunk[:needed])
        if len(out) == size:
            break
    if len(out) != size:
        raise ValueError("archive object stream ended before encrypted part range")
    return bytes(out)


def _plaintext_chunks(plaintext_size: int) -> Iterator[tuple[int, int, int, bool]]:
    if plaintext_size == 0:
        yield 0, 0, 0, True
        return
    chunk_count = (plaintext_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    for chunk_index in range(chunk_count):
        start = chunk_index * CHUNK_SIZE
        end = min(start + CHUNK_SIZE, plaintext_size)
        yield chunk_index, start, end, chunk_index == chunk_count - 1


def _age_chunks_per_s3_part(target_part_size: int) -> int:
    chunk_ciphertext_size = CHUNK_SIZE + AEAD_TAG_SIZE
    minimum_chunks = (_MIN_MULTIPART_PART_SIZE + chunk_ciphertext_size - 1) // (
        chunk_ciphertext_size
    )
    target_chunks = max(1, target_part_size // chunk_ciphertext_size)
    return max(minimum_chunks, target_chunks)


def _validate_recorded_parts_exist_remotely(
    recorded_parts: list[ArchiveMultipartUploadedPart],
    remote_parts: list[dict[str, object]],
) -> None:
    remote_parts_by_number = {int(str(part["PartNumber"])): part for part in remote_parts}
    for part in recorded_parts:
        remote = remote_parts_by_number.get(part.part_number)
        if remote is None:
            raise ValueError("collection archive multipart upload is missing a recorded part")
        if str(remote["ETag"]) != part.etag or int(str(remote["Size"])) != part.size:
            raise ValueError("collection archive multipart upload remote part mismatch")


class S3ArchiveStore:
    def __init__(
        self,
        config: RuntimeConfig,
        store: ArchiveStoreConfig,
        *,
        multipart_timing_observer: Callable[[ArchiveMultipartTiming], None] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._bucket = store.bucket
        self._client = create_archive_s3_client(config, store)
        self._multipart_timing_observer = multipart_timing_observer
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

    def new_collection_archive_storage_prefix(self) -> str:
        archive_id = secrets.token_hex(_OPAQUE_ARCHIVE_ID_BYTES)
        return archive_store_object_path(self._store.prefix, "archives", archive_id)

    def max_plaintext_object_bytes(self) -> int:
        session = ResumableAgeScryptSession.create(
            self._config.archive_passphrase,
            log_n=self._config.archive_scrypt_work_factor,
        )
        return max_age_plaintext_object_bytes(age_prefix_len=len(session.age_prefix))

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

    def _collection_object_key(
        self,
        *,
        object_id: str,
        archive_storage_prefix: str | None,
    ) -> str:
        collection_prefix = (
            archive_storage_prefix.strip("/")
            if archive_storage_prefix
            else self.new_collection_archive_storage_prefix()
        )
        filename = {
            "manifest": "manifest.yml.age",
            "proof": "manifest.yml.ots.age",
        }.get(object_id, f"objects/{object_id}.age")
        return f"{collection_prefix}/{filename}"

    def _head_object(self, *, object_key: str) -> dict[str, Any] | None:
        try:
            return cast(
                dict[str, Any],
                self._client.head_object(Bucket=self._bucket, Key=object_key),
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
        expected_storage_class: str,
        uploaded_at: str | None = None,
    ) -> ArchiveObjectUploadReceipt:
        _validate_uploaded_collection_metadata(
            object_key=object_key,
            head=head,
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
        if stored_bytes > STORED_OBJECT_LIMIT:
            raise ValueError("encrypted archive object exceeds the 32 GiB stored limit")
        verified_at = utc_timestamp_now()
        return ArchiveObjectUploadReceipt(
            object_id=object_id,
            kind=kind,
            object_path=object_key,
            plaintext_bytes=expected_bytes,
            stored_bytes=stored_bytes,
            sha256=expected_sha256,
            backend=self._store.backend,
            storage_class=_configured_s3_storage_class(expected_storage_class),
            uploaded_at=uploaded_at
            or _format_s3_timestamp(
                head.get("LastModified"),
                fallback=verified_at,
            ),
            verified_at=verified_at,
        )

    def upload_collection_archive(
        self,
        *,
        collection_id: str,
        archive: CollectionArchive,
        archive_storage_prefix: str | None = None,
        multipart_tracker: ArchiveMultipartUploadTracker | None = None,
    ) -> CollectionArchiveUploadReceipt:
        if archive.collection_id != collection_id:
            raise ValueError("collection archive id mismatch")
        storage_prefix = archive_storage_prefix or self.new_collection_archive_storage_prefix()
        receipts: list[ArchiveObjectUploadReceipt] = []

        def upload_data_object(current: CollectionArchiveDataObject) -> ArchiveObjectUploadReceipt:
            return self._put_archive_object(
                collection_id=collection_id,
                object_id=current.object_id,
                kind=current.kind,
                object_key=self._collection_object_key(
                    object_id=current.object_id,
                    archive_storage_prefix=storage_prefix,
                ),
                content=current.iter_plaintext(),
                plaintext_bytes=current.plaintext_bytes,
                sha256=current.sha256,
                data_object=current,
                multipart_tracker=multipart_tracker,
            )

        one_part_limit = min(
            PACK_PAYLOAD_LIMIT,
            max(0, self._config.archive_multipart_part_bytes - 1024 * 1024),
        )
        concurrent_batch: list[CollectionArchiveDataObject] = []

        def flush_concurrent_batch() -> None:
            if not concurrent_batch:
                return
            if self._config.archive_object_concurrency == 1 or len(concurrent_batch) == 1:
                receipts.extend(upload_data_object(current) for current in concurrent_batch)
            else:
                with ThreadPoolExecutor(
                    max_workers=self._config.archive_object_concurrency,
                    thread_name_prefix="riverhog-archive-object",
                ) as executor:
                    receipts.extend(executor.map(upload_data_object, concurrent_batch))
            concurrent_batch.clear()

        for current in archive.data_objects:
            if current.plaintext_bytes <= one_part_limit:
                concurrent_batch.append(current)
                continue
            flush_concurrent_batch()
            receipts.append(upload_data_object(current))
        flush_concurrent_batch()
        for object_id, kind, content, sha256 in (
            ("manifest", "manifest", archive.manifest_bytes, archive.manifest_sha256),
            ("proof", "proof", archive.proof_bytes, archive.proof_sha256),
        ):
            receipts.append(
                self._put_archive_object(
                    collection_id=collection_id,
                    object_id=object_id,
                    kind=kind,
                    object_key=self._collection_object_key(
                        object_id=object_id,
                        archive_storage_prefix=storage_prefix,
                    ),
                    content=content,
                    plaintext_bytes=len(content),
                    sha256=sha256,
                    data_object=None,
                    multipart_tracker=None,
                )
            )
        return CollectionArchiveUploadReceipt(objects=tuple(receipts))

    def verify_collection_archive(
        self,
        *,
        collection_id: str,
        archive: CollectionArchiveIdentity,
    ) -> None:
        _ = collection_id
        for expected in archive.objects:
            storage_class = _collection_object_storage_class(
                archive_storage_class=self._store.storage_class,
                kind=expected.kind,
            )
            head = self._head_object(object_key=expected.object_path)
            if head is None:
                raise ArchiveVerificationError(
                    f"remote collection {expected.kind} object is missing"
                )
            try:
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
        collection_id: str,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> None:
        _ = collection_id
        if not objects:
            raise ValueError("collection archive has no objects")
        object_paths = tuple(current.object_path for current in objects)
        archive_root = f"{archive_store_object_path(self._store.prefix, 'archives')}/"
        archive_prefixes = {
            path.rsplit("/objects/", 1)[0] if "/objects/" in path else path.rsplit("/", 1)[0]
            for path in object_paths
        }
        if len(archive_prefixes) != 1 or any(
            not path.startswith(archive_root) for path in object_paths
        ):
            raise ValueError("collection archive paths are outside one owned archive prefix")
        for path in object_paths:
            self._client.delete_object(Bucket=self._bucket, Key=path)
        remaining = [
            path for path in object_paths if self._head_object(object_key=path) is not None
        ]
        if remaining:
            raise RuntimeError(
                "collection archive deletion could not be verified: " + ", ".join(remaining)
            )

    def publish_restore_catalog(
        self,
        *,
        entries: Sequence[dict[str, object]],
        generated_at: str,
    ) -> None:
        self._put_archive_root_guidance()
        catalog_key = archive_store_object_path(
            self._store.prefix,
            "catalog",
            "collections.yml.age",
        )
        catalog_bytes = yaml.safe_dump(
            {
                "format": "encrypted-archive-catalog-v1",
                "generated_at": generated_at,
                "archives": [
                    {
                        key: value
                        for key, value in {
                            **entry,
                            "archive_id": archive_id_from_storage_prefix(
                                archive_prefix=self._store.prefix,
                                storage_prefix=cast(
                                    str | None,
                                    entry.get("archive_storage_prefix"),
                                ),
                            ),
                        }.items()
                        if value is not None
                    }
                    for entry in entries
                ],
            },
            sort_keys=False,
            allow_unicode=True,
        ).encode("utf-8")
        ciphertext = encrypt_age_scrypt(
            catalog_bytes,
            self._config.archive_passphrase,
            log_n=self._config.archive_scrypt_work_factor,
        )
        self._client.put_object(
            Bucket=self._bucket,
            Key=catalog_key,
            Body=ciphertext,
            ContentLength=len(ciphertext),
            Metadata={
                "archive-catalog-format": "encrypted-archive-catalog-v1",
                ENCRYPTION_METADATA: AGE_SCRYPT_ENCRYPTION,
                PLAINTEXT_BYTES_METADATA: str(len(catalog_bytes)),
                PLAINTEXT_SHA256_METADATA: hashlib.sha256(catalog_bytes).hexdigest(),
            },
        )

    def _put_archive_root_guidance(self) -> None:
        for filename, content, format_name in (
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
        ):
            self._client.put_object(
                Bucket=self._bucket,
                Key=archive_store_object_path(self._store.prefix, filename),
                Body=content,
                ContentLength=len(content),
                Metadata={"archive-guidance-format": format_name},
            )

    def _put_archive_object(
        self,
        *,
        collection_id: str,
        object_id: str,
        object_key: str,
        content: Any,
        plaintext_bytes: int,
        sha256: str,
        kind: str,
        data_object: CollectionArchiveDataObject | None,
        multipart_tracker: ArchiveMultipartUploadTracker | None,
    ) -> ArchiveObjectUploadReceipt:
        storage_class = _collection_object_storage_class(
            archive_storage_class=self._store.storage_class,
            kind=kind,
        )
        existing = self._head_object(object_key=object_key)
        if existing is not None:
            return self._collection_receipt_from_head(
                object_id=object_id,
                kind=kind,
                object_key=object_key,
                head=existing,
                expected_bytes=plaintext_bytes,
                expected_sha256=sha256,
                expected_storage_class=storage_class,
            )

        age_session: ResumableAgeScryptSession | None = None
        if data_object is not None:
            age_session = ResumableAgeScryptSession.create(
                self._config.archive_passphrase,
                log_n=self._config.archive_scrypt_work_factor,
                plaintext_size=plaintext_bytes,
            )
            content_length = age_ciphertext_len_for_plaintext_len(
                plaintext_bytes,
                age_prefix_len=len(age_session.age_prefix),
            )
            content = _iter_encrypted_object(object=data_object, session=age_session)
        else:
            plaintext = _single_put_body(content)
            content = encrypt_age_scrypt(
                plaintext,
                self._config.archive_passphrase,
                log_n=self._config.archive_scrypt_work_factor,
            )
            content_length = len(content)
        if content_length > STORED_OBJECT_LIMIT:
            raise ValueError("encrypted archive object exceeds the 32 GiB stored limit")

        uploaded_at = utc_timestamp_now()
        extra_args: dict[str, Any] = {
            "Metadata": {
                "riverhog-backend": self._store.backend,
                "riverhog-storage-class": _configured_s3_storage_class(storage_class),
                "riverhog-object-kind": f"collection-{kind}",
                "riverhog-object-id": object_id,
                "riverhog-collection-sha256": hashlib.sha256(
                    collection_id.encode("utf-8")
                ).hexdigest(),
                ENCRYPTION_METADATA: AGE_SCRYPT_ENCRYPTION,
                PLAINTEXT_BYTES_METADATA: str(plaintext_bytes),
                PLAINTEXT_SHA256_METADATA: sha256,
                COLLECTION_BYTES_METADATA: str(plaintext_bytes),
                COLLECTION_SHA256_METADATA: sha256,
            }
        }
        if (
            self._uses_aws_restore_api()
            and _configured_s3_storage_class(storage_class) != "STANDARD"
        ):
            extra_args["StorageClass"] = storage_class
        if _should_use_multipart(content=content, content_length=content_length):
            if data_object is not None and data_object.supports_ranges:
                if age_session is None:
                    raise RuntimeError("encrypted object upload session was not initialized")
                self._put_encrypted_object_multipart(
                    collection_id=collection_id,
                    object_id=object_id,
                    object_key=object_key,
                    object=data_object,
                    content_length=content_length,
                    logical_sha256=sha256,
                    initial_session=age_session,
                    extra_args=extra_args,
                    multipart_tracker=multipart_tracker,
                )
            else:
                self._put_archive_object_multipart(
                    collection_id=collection_id,
                    object_id=object_id,
                    object_key=object_key,
                    content=content,
                    content_length=content_length,
                    sha256=sha256,
                    extra_args=extra_args,
                    multipart_tracker=multipart_tracker,
                )
        else:
            self._client.put_object(
                Bucket=self._bucket,
                Key=object_key,
                Body=_single_put_body(content),
                ContentLength=content_length,
                **extra_args,
            )
        head = cast(
            dict[str, Any],
            self._client.head_object(Bucket=self._bucket, Key=object_key),
        )
        return self._collection_receipt_from_head(
            object_id=object_id,
            kind=kind,
            object_key=object_key,
            head=head,
            expected_bytes=plaintext_bytes,
            expected_sha256=sha256,
            expected_storage_class=storage_class,
            uploaded_at=uploaded_at,
        )

    def _put_archive_object_multipart(
        self,
        *,
        collection_id: str,
        object_id: str,
        object_key: str,
        content: Any,
        content_length: int,
        sha256: str,
        extra_args: dict[str, Any],
        multipart_tracker: ArchiveMultipartUploadTracker | None,
    ) -> None:
        part_number = 1
        upload_state: ArchiveMultipartUploadState | None = None
        buffer = bytearray()
        part_size = _multipart_part_size(
            content_length,
            self._config.archive_multipart_part_bytes,
        )
        multipart_concurrency = self._config.archive_multipart_concurrency
        expected_part_count = (content_length + part_size - 1) // part_size
        uploaded_bytes = 0
        size = 0
        resumed_part_count = 0
        skip_bytes = 0
        completed_parts_by_number: dict[int, ArchiveMultipartUploadedPart] = {}

        if multipart_tracker is not None:
            upload_state = multipart_tracker.load_multipart_upload(
                collection_id=collection_id,
                object_id=object_id,
                object_path=object_key,
                part_size=part_size,
                content_length=content_length,
                sha256=sha256,
            )
            if upload_state is not None:
                try:
                    resumed_parts = self._contiguous_uploaded_parts(
                        object_key=object_key,
                        upload_id=upload_state.upload_id,
                        recorded_parts=upload_state.parts,
                        part_size=part_size,
                        content_length=content_length,
                    )
                except Exception as exc:
                    if not _is_missing_upload_error(exc):
                        raise
                    multipart_tracker.clear_multipart_upload(
                        collection_id=collection_id,
                        object_id=object_id,
                        upload_id=upload_state.upload_id,
                    )
                    upload_state = None
                    resumed_parts = []
                if upload_state is not None:
                    resumed_part_count = len(resumed_parts)
                    skip_bytes = sum(part.size for part in resumed_parts)
                    part_number = resumed_part_count + 1
                    uploaded_bytes = skip_bytes
                    completed_parts_by_number.update(
                        (part.part_number, part) for part in resumed_parts
                    )
                    _LOG.info(
                        "resuming S3 multipart upload for %s: upload_id=%s parts=%s/%s bytes=%s/%s",
                        object_key,
                        upload_state.upload_id,
                        resumed_part_count,
                        expected_part_count,
                        uploaded_bytes,
                        content_length,
                    )

        def ensure_upload() -> str:
            nonlocal upload_state
            if upload_state is None:
                _LOG.info(
                    "starting S3 multipart upload for %s: size=%s part_size=%s "
                    "parts=%s concurrency=%s",
                    object_key,
                    content_length,
                    part_size,
                    expected_part_count,
                    multipart_concurrency,
                )
                response = cast(
                    dict[str, Any],
                    self._client.create_multipart_upload(
                        Bucket=self._bucket,
                        Key=object_key,
                        **extra_args,
                    ),
                )
                upload_state = ArchiveMultipartUploadState(
                    object_id=object_id,
                    upload_id=str(response["UploadId"]),
                    object_path=object_key,
                    part_size=part_size,
                    content_length=content_length,
                    sha256=sha256,
                )
                if multipart_tracker is not None:
                    multipart_tracker.save_multipart_upload(
                        collection_id=collection_id,
                        state=upload_state,
                    )
            return upload_state.upload_id

        def upload_part_body(
            *,
            upload_id: str,
            current_part_number: int,
            body: bytes,
        ) -> ArchiveMultipartUploadedPart:
            response = cast(
                dict[str, Any],
                self._client.upload_part(
                    Bucket=self._bucket,
                    Key=object_key,
                    UploadId=upload_id,
                    PartNumber=current_part_number,
                    Body=body,
                ),
            )
            return ArchiveMultipartUploadedPart(
                part_number=current_part_number,
                etag=str(response["ETag"]),
                size=len(body),
            )

        def record_completed_part(part: ArchiveMultipartUploadedPart) -> None:
            nonlocal uploaded_bytes
            completed_parts_by_number[part.part_number] = part
            uploaded_bytes = sum(current.size for current in completed_parts_by_number.values())
            uploaded_parts = len(completed_parts_by_number)
            if multipart_tracker is not None and upload_state is not None:
                multipart_tracker.record_multipart_upload_progress(
                    collection_id=collection_id,
                    state=upload_state,
                    part=part,
                    uploaded_bytes=uploaded_bytes,
                    uploaded_parts=uploaded_parts,
                    total_parts=expected_part_count,
                )
            if _should_log_multipart_progress(uploaded_parts, expected_part_count):
                _LOG.info(
                    "S3 multipart upload progress for %s: part=%s/%s bytes=%s/%s pct=%.2f",
                    object_key,
                    uploaded_parts,
                    expected_part_count,
                    uploaded_bytes,
                    content_length,
                    (uploaded_bytes / content_length * 100.0) if content_length else 100.0,
                )

        try:
            with ThreadPoolExecutor(
                max_workers=multipart_concurrency,
                thread_name_prefix="riverhog-s3-archive",
            ) as executor:
                pending: set[Future[ArchiveMultipartUploadedPart]] = set()

                def drain_completed(*, return_when: str) -> None:
                    if not pending:
                        return
                    done, still_pending = wait(pending, return_when=return_when)
                    pending.clear()
                    pending.update(still_pending)
                    error: BaseException | None = None
                    for future in done:
                        try:
                            record_completed_part(future.result())
                        except BaseException as exc:
                            error = exc
                    if error is not None:
                        for future in pending:
                            future.cancel()
                        raise error

                def submit_part(body: bytes) -> None:
                    nonlocal part_number
                    current_part_number = part_number
                    part_number += 1
                    pending.add(
                        executor.submit(
                            upload_part_body,
                            upload_id=ensure_upload(),
                            current_part_number=current_part_number,
                            body=body,
                        )
                    )
                    if len(pending) >= multipart_concurrency:
                        drain_completed(return_when=FIRST_COMPLETED)

                chunks = _iter_chunks_after_skipping(
                    _iter_content_chunks(content),
                    skip_bytes,
                )
                for chunk in chunks:
                    size += len(chunk)
                    chunk_view = memoryview(chunk)
                    offset = 0
                    while offset < len(chunk_view):
                        bytes_to_copy = min(
                            part_size - len(buffer),
                            len(chunk_view) - offset,
                        )
                        buffer.extend(chunk_view[offset : offset + bytes_to_copy])
                        offset += bytes_to_copy
                        if len(buffer) == part_size:
                            submit_part(bytes(buffer))
                            buffer.clear()

                if size + skip_bytes != content_length:
                    raise ValueError("collection archive stream byte count mismatch")
                if buffer:
                    submit_part(bytes(buffer))
                    buffer.clear()
                drain_completed(return_when=FIRST_COMPLETED)
                while pending:
                    drain_completed(return_when=FIRST_COMPLETED)

            if upload_state is None:
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=object_key,
                    Body=b"",
                    ContentLength=0,
                    **extra_args,
                )
                return
            remote_parts = self._list_uploaded_parts(
                object_key=object_key,
                upload_id=upload_state.upload_id,
            )
            completed_parts = [
                completed_parts_by_number[part_number]
                for part_number in range(1, expected_part_count + 1)
                if part_number in completed_parts_by_number
            ]
            if len(completed_parts) != expected_part_count:
                raise ValueError(
                    "collection archive multipart upload is missing parts before completion"
                )
            _validate_recorded_parts_exist_remotely(completed_parts, remote_parts)
            self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=object_key,
                UploadId=upload_state.upload_id,
                MultipartUpload={
                    "Parts": [
                        {"PartNumber": part.part_number, "ETag": part.etag}
                        for part in completed_parts
                    ]
                },
            )
            if multipart_tracker is not None:
                multipart_tracker.clear_multipart_upload(
                    collection_id=collection_id,
                    object_id=object_id,
                    upload_id=upload_state.upload_id,
                )
            _LOG.info(
                "completed S3 multipart upload for %s: parts=%s bytes=%s",
                object_key,
                len(completed_parts),
                uploaded_bytes,
            )
        except Exception:
            if upload_state is not None:
                if multipart_tracker is None:
                    self._client.abort_multipart_upload(
                        Bucket=self._bucket,
                        Key=object_key,
                        UploadId=upload_state.upload_id,
                    )
                else:
                    _LOG.warning(
                        "leaving incomplete S3 multipart upload for %s resumable: "
                        "upload_id=%s uploaded_bytes=%s/%s resumed_parts=%s",
                        object_key,
                        upload_state.upload_id,
                        uploaded_bytes,
                        content_length,
                        resumed_part_count,
                        exc_info=True,
                    )
            raise

    def _put_encrypted_object_multipart(
        self,
        *,
        collection_id: str,
        object_id: str,
        object_key: str,
        object: CollectionArchiveDataObject,
        content_length: int,
        logical_sha256: str,
        initial_session: ResumableAgeScryptSession,
        extra_args: dict[str, Any],
        multipart_tracker: ArchiveMultipartUploadTracker | None,
    ) -> None:
        started = time.perf_counter()
        upload_state: ArchiveMultipartUploadState | None = None
        part_size = _multipart_part_size(
            content_length,
            self._config.archive_multipart_part_bytes,
        )
        chunks_per_part = _age_chunks_per_s3_part(part_size)
        session = initial_session
        plans = session.s3_part_plans(object.plaintext_bytes, chunks_per_part=chunks_per_part)
        expected_part_count = len(plans)
        multipart_concurrency = self._config.archive_multipart_concurrency
        uploaded_bytes = 0
        resumed_part_count = 0
        completed_parts_by_number: dict[int, ArchiveMultipartUploadedPart] = {}
        preparation_seconds = 0.0
        upload_request_seconds = 0.0
        checkpoint_seconds = 0.0

        def checkpoint(call: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
            nonlocal checkpoint_seconds
            checkpoint_started = time.perf_counter()
            try:
                return call(*args, **kwargs)
            finally:
                checkpoint_seconds += time.perf_counter() - checkpoint_started

        if plans[-1].ciphertext_end != content_length:
            raise RuntimeError("encrypted archive content length plan mismatch")

        if multipart_tracker is not None:
            upload_state = checkpoint(
                multipart_tracker.load_multipart_upload,
                collection_id=collection_id,
                object_id=object_id,
                object_path=object_key,
                part_size=part_size,
                content_length=content_length,
                sha256=logical_sha256,
            )
            if upload_state is not None:
                if not upload_state.encryption_state_json:
                    checkpoint(
                        multipart_tracker.clear_multipart_upload,
                        collection_id=collection_id,
                        object_id=object_id,
                        upload_id=upload_state.upload_id,
                    )
                    upload_state = None
                else:
                    session = ResumableAgeScryptSession.from_state(
                        self._config.archive_passphrase,
                        upload_state.encryption_state_json,
                    )
                    plans = session.s3_part_plans(
                        object.plaintext_bytes,
                        chunks_per_part=chunks_per_part,
                    )
                    try:
                        resumed_parts = self._contiguous_uploaded_parts(
                            object_key=object_key,
                            upload_id=upload_state.upload_id,
                            recorded_parts=upload_state.parts,
                            part_size=part_size,
                            content_length=content_length,
                            expected_part_sizes=tuple(plan.ciphertext_len for plan in plans),
                        )
                    except Exception as exc:
                        if not _is_missing_upload_error(exc):
                            raise
                        checkpoint(
                            multipart_tracker.clear_multipart_upload,
                            collection_id=collection_id,
                            object_id=object_id,
                            upload_id=upload_state.upload_id,
                        )
                        upload_state = None
                        resumed_parts = []
                    if upload_state is not None:
                        resumed_part_count = len(resumed_parts)
                        uploaded_bytes = sum(part.size for part in resumed_parts)
                        completed_parts_by_number.update(
                            (part.part_number, part) for part in resumed_parts
                        )
                        _LOG.info(
                            "resuming encrypted S3 multipart upload for %s: "
                            "upload_id=%s parts=%s/%s bytes=%s/%s",
                            object_key,
                            upload_state.upload_id,
                            resumed_part_count,
                            expected_part_count,
                            uploaded_bytes,
                            content_length,
                        )

        def ensure_upload() -> str:
            nonlocal upload_state
            if upload_state is None:
                _LOG.info(
                    "starting encrypted S3 multipart upload for %s: size=%s "
                    "target_part_size=%s parts=%s chunks_per_part=%s concurrency=%s",
                    object_key,
                    content_length,
                    part_size,
                    expected_part_count,
                    chunks_per_part,
                    multipart_concurrency,
                )
                response = cast(
                    dict[str, Any],
                    self._client.create_multipart_upload(
                        Bucket=self._bucket,
                        Key=object_key,
                        **extra_args,
                    ),
                )
                upload_state = ArchiveMultipartUploadState(
                    object_id=object_id,
                    upload_id=str(response["UploadId"]),
                    object_path=object_key,
                    part_size=part_size,
                    content_length=content_length,
                    sha256=logical_sha256,
                    total_parts=expected_part_count,
                    encryption_state_json=session.export_state(
                        plaintext_size=object.plaintext_bytes
                    )
                    .to_json_bytes()
                    .decode("utf-8"),
                )
                if multipart_tracker is not None:
                    checkpoint(
                        multipart_tracker.save_multipart_upload,
                        collection_id=collection_id,
                        state=upload_state,
                    )
            return upload_state.upload_id

        def record_completed_part(part: ArchiveMultipartUploadedPart) -> None:
            nonlocal uploaded_bytes
            completed_parts_by_number[part.part_number] = part
            uploaded_bytes = sum(current.size for current in completed_parts_by_number.values())
            uploaded_parts = len(completed_parts_by_number)
            if multipart_tracker is not None and upload_state is not None:
                checkpoint(
                    multipart_tracker.record_multipart_upload_progress,
                    collection_id=collection_id,
                    state=upload_state,
                    part=part,
                    uploaded_bytes=uploaded_bytes,
                    uploaded_parts=uploaded_parts,
                    total_parts=expected_part_count,
                )
            if _should_log_multipart_progress(uploaded_parts, expected_part_count):
                _LOG.info(
                    "encrypted S3 multipart upload progress for %s: "
                    "part=%s/%s bytes=%s/%s pct=%.2f",
                    object_key,
                    uploaded_parts,
                    expected_part_count,
                    uploaded_bytes,
                    content_length,
                    (uploaded_bytes / content_length * 100.0) if content_length else 100.0,
                )

        try:
            upload_id = ensure_upload()
            with ThreadPoolExecutor(
                max_workers=multipart_concurrency,
                thread_name_prefix="riverhog-s3-encrypted-archive",
            ) as executor:
                pending: set[
                    Future[tuple[ArchiveMultipartUploadedPart, float]]
                ] = set()

                def upload_part_body(
                    *,
                    plan: Any,
                    body: bytes,
                ) -> tuple[ArchiveMultipartUploadedPart, float]:
                    request_started = time.perf_counter()
                    response = cast(
                        dict[str, Any],
                        self._client.upload_part(
                            Bucket=self._bucket,
                            Key=object_key,
                            UploadId=upload_id,
                            PartNumber=plan.part_number,
                            Body=body,
                        ),
                    )
                    request_seconds = time.perf_counter() - request_started
                    return (
                        ArchiveMultipartUploadedPart(
                            part_number=plan.part_number,
                            etag=str(response["ETag"]),
                            size=len(body),
                        ),
                        request_seconds,
                    )

                def drain_completed(*, return_when: str) -> None:
                    nonlocal upload_request_seconds
                    if not pending:
                        return
                    done, still_pending = wait(pending, return_when=return_when)
                    pending.clear()
                    pending.update(still_pending)
                    error: BaseException | None = None
                    for future in done:
                        try:
                            part, request_seconds = future.result()
                            upload_request_seconds += request_seconds
                            record_completed_part(part)
                        except BaseException as exc:
                            error = exc
                    if error is not None:
                        for future in pending:
                            future.cancel()
                        raise error

                for plan in plans[resumed_part_count:]:
                    preparation_started = time.perf_counter()
                    body = _encrypted_object_part_body(
                        object=object,
                        session=session,
                        plan=plan,
                    )
                    preparation_seconds += time.perf_counter() - preparation_started
                    pending.add(executor.submit(upload_part_body, plan=plan, body=body))
                    if len(pending) >= multipart_concurrency:
                        drain_completed(return_when=FIRST_COMPLETED)

                while pending:
                    drain_completed(return_when=FIRST_COMPLETED)

            remote_parts = self._list_uploaded_parts(object_key=object_key, upload_id=upload_id)
            completed_parts = [
                completed_parts_by_number[part_number]
                for part_number in range(1, expected_part_count + 1)
                if part_number in completed_parts_by_number
            ]
            if len(completed_parts) != expected_part_count:
                raise ValueError(
                    "encrypted collection archive multipart upload is missing parts "
                    "before completion"
                )
            _validate_recorded_parts_exist_remotely(completed_parts, remote_parts)
            self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=object_key,
                UploadId=upload_id,
                MultipartUpload={
                    "Parts": [
                        {"PartNumber": part.part_number, "ETag": part.etag}
                        for part in completed_parts
                    ]
                },
            )
            if multipart_tracker is not None:
                checkpoint(
                    multipart_tracker.clear_multipart_upload,
                    collection_id=collection_id,
                    object_id=object_id,
                    upload_id=upload_id,
                )
            _LOG.info(
                "completed encrypted S3 multipart upload for %s: parts=%s bytes=%s "
                "elapsed_seconds=%.3f preparation_seconds=%.3f "
                "upload_request_seconds=%.3f checkpoint_seconds=%.3f",
                object_key,
                len(completed_parts),
                uploaded_bytes,
                time.perf_counter() - started,
                preparation_seconds,
                upload_request_seconds,
                checkpoint_seconds,
            )
            if self._multipart_timing_observer is not None:
                self._multipart_timing_observer(
                    ArchiveMultipartTiming(
                        object_id=object_id,
                        stored_bytes=content_length,
                        parts=expected_part_count,
                        concurrency=multipart_concurrency,
                        elapsed_seconds=time.perf_counter() - started,
                        preparation_seconds=preparation_seconds,
                        upload_request_seconds=upload_request_seconds,
                        checkpoint_seconds=checkpoint_seconds,
                    )
                )
        except Exception:
            if upload_state is not None:
                _LOG.warning(
                    "leaving incomplete encrypted S3 multipart upload for %s resumable: "
                    "upload_id=%s uploaded_bytes=%s/%s resumed_parts=%s",
                    object_key,
                    upload_state.upload_id,
                    uploaded_bytes,
                    content_length,
                    resumed_part_count,
                    exc_info=True,
                )
            raise

    def _list_uploaded_parts(
        self,
        *,
        object_key: str,
        upload_id: str,
    ) -> list[dict[str, object]]:
        parts: list[dict[str, object]] = []
        marker = 0
        while True:
            request: dict[str, object] = {
                "Bucket": self._bucket,
                "Key": object_key,
                "UploadId": upload_id,
            }
            if marker:
                request["PartNumberMarker"] = marker
            response = cast(
                dict[str, Any],
                self._client.list_parts(**request),
            )
            for part in response.get("Parts", []):
                if not isinstance(part, dict):
                    continue
                part_number = int(part["PartNumber"])
                parts.append(
                    {
                        "PartNumber": part_number,
                        "ETag": str(part["ETag"]),
                        "Size": int(part.get("Size", 0)),
                    }
                )
                marker = part_number
            if not response.get("IsTruncated"):
                return sorted(parts, key=lambda current: int(str(current["PartNumber"])))
            marker = int(str(response.get("NextPartNumberMarker", marker)))

    def _contiguous_uploaded_parts(
        self,
        *,
        object_key: str,
        upload_id: str,
        recorded_parts: tuple[ArchiveMultipartUploadedPart, ...],
        part_size: int,
        content_length: int,
        expected_part_sizes: tuple[int, ...] | None = None,
    ) -> list[ArchiveMultipartUploadedPart]:
        expected_part_count = (content_length + part_size - 1) // part_size
        if expected_part_sizes is not None:
            expected_part_count = len(expected_part_sizes)
        remote_parts_by_number = {
            int(str(part["PartNumber"])): part
            for part in self._list_uploaded_parts(object_key=object_key, upload_id=upload_id)
        }
        recorded_parts_by_number = {part.part_number: part for part in recorded_parts}
        contiguous: list[ArchiveMultipartUploadedPart] = []
        for part_number in range(1, expected_part_count + 1):
            recorded = recorded_parts_by_number.get(part_number)
            remote = remote_parts_by_number.get(part_number)
            if recorded is None or remote is None:
                break
            if expected_part_sizes is not None:
                expected_size = expected_part_sizes[part_number - 1]
            else:
                expected_size = (
                    content_length - part_size * (expected_part_count - 1)
                    if part_number == expected_part_count
                    else part_size
                )
            if recorded.size != expected_size:
                break
            if str(remote["ETag"]) != recorded.etag or int(str(remote["Size"])) != recorded.size:
                break
            contiguous.append(recorded)
        return contiguous

    def prepare_archive_objects_read(
        self,
        *,
        collection_id: str,
        objects: Sequence[ArchiveObjectIdentity],
        retrieval_tier: str,
        hold_days: int,
        requested_at: str,
        estimated_ready_at: str,
    ) -> ArchiveReadStatus:
        _ = collection_id
        statuses = [
            self._request_collection_object_restore(
                object_path=current_object_path,
                retrieval_tier=retrieval_tier,
                hold_days=hold_days,
                requested_at=requested_at,
                estimated_ready_at=estimated_ready_at,
            )
            for current_object_path in (current.object_path for current in objects)
        ]
        if not statuses:
            return ArchiveReadStatus(state="ready", ready_at=requested_at)
        return _combine_fetch_materialization_statuses(statuses)

    def _request_collection_object_restore(
        self,
        *,
        object_path: str,
        retrieval_tier: str,
        hold_days: int,
        requested_at: str,
        estimated_ready_at: str,
    ) -> ArchiveReadStatus:
        head = self._head_object(object_key=object_path)
        if head is None:
            raise RuntimeError(f"Archive object is missing: {object_path}")
        _validate_uploaded_collection_metadata(object_key=object_path, head=head)
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
        try:
            self._client.restore_object(
                Bucket=self._bucket,
                Key=object_path,
                RestoreRequest={
                    "Days": hold_days,
                    "ArchiveJobParameters": {"Tier": _aws_restore_tier(retrieval_tier)},
                },
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
            object_path=object_path,
            requested_at=requested_at,
            estimated_ready_at=estimated_ready_at,
            estimated_expires_at=None,
        )

    def get_archive_objects_read_status(
        self,
        *,
        collection_id: str,
        objects: Sequence[ArchiveObjectIdentity],
        requested_at: str,
        estimated_ready_at: str | None,
        estimated_expires_at: str | None,
    ) -> ArchiveReadStatus:
        _ = collection_id
        statuses = [
            self._collection_object_restore_status(
                object_path=current_object_path,
                requested_at=requested_at,
                estimated_ready_at=estimated_ready_at,
                estimated_expires_at=estimated_expires_at,
            )
            for current_object_path in (current.object_path for current in objects)
        ]
        if not statuses:
            return ArchiveReadStatus(state="ready", ready_at=requested_at)
        return _combine_fetch_materialization_statuses(statuses)

    def _collection_object_restore_status(
        self,
        *,
        object_path: str,
        requested_at: str,
        estimated_ready_at: str | None,
        estimated_expires_at: str | None,
    ) -> ArchiveReadStatus:
        head = self._head_object(object_key=object_path)
        if head is None:
            raise RuntimeError(f"Archive object is missing: {object_path}")
        _validate_uploaded_collection_metadata(object_key=object_path, head=head)
        restore = _parse_restore_header(head.get("Restore"))
        if restore is None:
            if _is_immediately_readable_storage_class(head):
                return ArchiveReadStatus(
                    state="ready",
                    ready_at=requested_at,
                    message="Collection archive object is immediately readable.",
                )
            return ArchiveReadStatus(
                state="requested",
                ready_at=estimated_ready_at,
                expires_at=estimated_expires_at,
                message="Collection archive restore is still in progress.",
            )
        if restore["ongoing"]:
            return ArchiveReadStatus(
                state="requested",
                ready_at=estimated_ready_at,
                expires_at=restore["expires_at"] or estimated_expires_at,
                message="Collection archive restore is still in progress.",
            )
        return ArchiveReadStatus(
            state="ready",
            ready_at=utc_timestamp_now(),
            expires_at=restore["expires_at"],
            message="Collection archive object is restored and readable.",
        )

    def iter_archive_object(
        self,
        *,
        collection_id: str,
        object: ArchiveObjectIdentity,
    ) -> Iterator[bytes]:
        object_path = object.object_path
        head = self._head_object(object_key=object_path)
        if head is None:
            raise RuntimeError(f"Archive object is missing: {object_path}")
        _validate_uploaded_collection_metadata(object_key=object_path, head=head)
        status = self.get_archive_objects_read_status(
            collection_id=collection_id,
            objects=(object,),
            requested_at=utc_timestamp_now(),
            estimated_ready_at=None,
            estimated_expires_at=None,
        )
        if status.state != "ready":
            raise RuntimeError(f"Archive object is not restored yet: {object_path}")
        if self._cloudfront_client is None or self._cloudfront_signer is None:
            response = self._client.get_object(Bucket=self._bucket, Key=object_path)
            body = response["Body"]
            try:
                chunks = body.iter_chunks(chunk_size=1024 * 1024)
                yield from iter_decrypt_age_scrypt(
                    chunks,
                    self._config.archive_passphrase,
                )
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
            return

        base_url = self._store.cloudfront_base_url
        if base_url is None:
            raise RuntimeError("CloudFront download configuration is incomplete")
        object_url = f"{base_url}/{quote(object_path, safe='/')}"
        signed_url = self._cloudfront_signer.generate_presigned_url(
            object_url,
            date_less_than=datetime.now(UTC) + _CLOUDFRONT_URL_TTL,
        )
        try:
            with self._cloudfront_client.stream(
                "GET",
                signed_url,
                headers={"Accept-Encoding": "identity"},
            ) as response:
                if not response.is_success:
                    raise RuntimeError(
                        "CloudFront archive download failed with HTTP "
                        f"{response.status_code}: {object_path}"
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None and int(content_length) != object.stored_bytes:
                    raise RuntimeError(
                        "CloudFront archive download length does not match verified metadata: "
                        f"{object_path}"
                    )
                yield from iter_decrypt_age_scrypt(
                    response.iter_bytes(chunk_size=1024 * 1024),
                    self._config.archive_passphrase,
                )
        except httpx.HTTPError:
            raise RuntimeError(f"CloudFront archive download failed: {object_path}") from None

    def cleanup_archive_objects_read(
        self,
        *,
        collection_id: str,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> None:
        _ = collection_id, objects
        return

    def _uses_aws_restore_api(self) -> bool:
        return self._store.backend.casefold() == "aws"


def _combine_fetch_materialization_statuses(
    statuses: list[ArchiveReadStatus],
) -> ArchiveReadStatus:
    if any(status.state == "expired" for status in statuses):
        return ArchiveReadStatus(state="expired")
    if statuses and all(status.state == "ready" for status in statuses):
        return ArchiveReadStatus(
            state="ready",
            ready_at=_max_timestamp(status.ready_at for status in statuses),
            expires_at=_min_timestamp(status.expires_at for status in statuses),
            message="Archive objects are restored and readable.",
        )
    return ArchiveReadStatus(
        state="requested",
        ready_at=_max_timestamp(status.ready_at for status in statuses),
        expires_at=_min_timestamp(status.expires_at for status in statuses),
        message="Archive object restoration is still in progress.",
    )


def _bucket_recovery_readme() -> str:
    return f"""# Encrypted Riverhog Archive Recovery

{ARCHIVE_CUSTODY_WARNING}

Listing and reading these objects are safe inspection operations. Deletion,
movement, overwriting, lifecycle expiration, storage-class changes, and object-
version removal are mutations. Use Riverhog's guarded archive workflows for an
authorized collection deletion or archive-copy retirement.

This bucket or prefix stores independently encrypted archive objects. Paths are opaque
on purpose so that bucket listings, access logs, and screenshots
do not reveal private collection names.

## What You Need

- S3 credentials, token, or S3 login/session that can list and read this bucket or prefix.
- The archive passphrase.
- Standard recovery tools: an S3 CLI such as `aws`, `age`, `sha256sum`, `tar`, and
  optionally `ots`.

S3 credentials and the archive passphrase are different secrets. The `aws`
commands below will fail until the CLI is authenticated with S3 access. Common
options include `aws configure`, `aws sso login --profile PROFILE`, environment
variables such as `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and
`AWS_SESSION_TOKEN`, or equivalent credentials for an S3-compatible provider.

Do not put the archive passphrase directly in shell commands. The `age`
commands below will prompt for it.

## Find an archive

If `catalog/collections.yml.age` is present, download and decrypt it to map
private collection labels to opaque archive IDs:

```sh
aws s3 cp s3://BUCKET/PREFIX/catalog/collections.yml.age .
age --decrypt -o collections.yml collections.yml.age
# Enter the archive passphrase when age prompts.
```

If there is no catalog, list the opaque archive directories:

```sh
aws s3 ls s3://BUCKET/PREFIX/archives/
```

Replace `BUCKET` and `ARCHIVE_ID` in the examples below. `PREFIX/` is optional:
omit it when this guidance file is stored at the bucket root.

## Inspect the Manifest

Manifests and timestamp proofs are normally stored in regular S3 storage, so
they can usually be downloaded immediately:

```sh
aws s3 cp s3://BUCKET/PREFIX/archives/ARCHIVE_ID/manifest.yml.age .
age --decrypt -o manifest.yml manifest.yml.age
# Enter the archive passphrase when age prompts.

aws s3 cp s3://BUCKET/PREFIX/archives/ARCHIVE_ID/manifest.yml.ots.age .
age --decrypt -o manifest.yml.ots manifest.yml.ots.age
# Enter the same archive passphrase when age prompts.
```

Optional timestamp proof verification:

```sh
ots verify manifest.yml.ots -f manifest.yml
```

## Recover files

The manifest maps each file to one or more `data-*` objects. Download only those
objects from `archives/ARCHIVE_ID/objects/`. An object in S3 Glacier Deep Archive
must be restored before it is readable:

```sh
aws s3api restore-object \\
  --bucket BUCKET \\
  --key PREFIX/archives/ARCHIVE_ID/objects/data-NNNNNN.age \\
  --restore-request '{{"Days":7,"ArchiveJobParameters":{{"Tier":"Bulk"}}}}'
```

After any required restore completes, download and independently decrypt each
needed object:

```sh
aws s3 cp s3://BUCKET/PREFIX/archives/ARCHIVE_ID/objects/data-NNNNNN.age .
age --decrypt -o data-NNNNNN data-NNNNNN.age
# Enter the same archive passphrase when age prompts.
sha256sum data-NNNNNN
```

Compare each decrypted object's digest with its `objects` entry in the
manifest. Then follow the file's ordered `objects` mappings:

- For a `file` object, the decrypted object is the complete logical file.
- For a `segment` object, concatenate decrypted segments in manifest order.
- For a `pack` object, extract the mapping's `member` from the decrypted tar:

```sh
tar -xOf data-NNNNNN MEMBER > recovered-file
```

Finally compare every recovered file's size and SHA-256 digest with its manifest
entry.
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


def _validate_uploaded_collection_metadata(
    *,
    object_key: str,
    head: dict[str, Any],
    expected_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> None:
    metadata = _head_metadata(head)
    metadata_bytes = metadata.get(COLLECTION_BYTES_METADATA)
    metadata_sha256 = metadata.get(COLLECTION_SHA256_METADATA)
    if metadata_bytes is None or metadata_sha256 is None:
        raise RuntimeError(
            f"Archive object is missing collection validation metadata: {object_key}"
        )
    try:
        collection_bytes = int(metadata_bytes)
    except ValueError as exc:
        raise RuntimeError(
            f"Archive object has invalid collection byte metadata: {object_key}"
        ) from exc
    if expected_bytes is not None and collection_bytes != expected_bytes:
        raise RuntimeError(f"Archive object plaintext size does not match its record: {object_key}")
    if not _SHA256_RE.fullmatch(metadata_sha256):
        raise RuntimeError(f"Archive object has invalid collection sha256 metadata: {object_key}")
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
    _validate_uploaded_collection_metadata(
        object_key=object_key,
        head=head,
        expected_sha256=expected.sha256,
    )
    metadata = _head_metadata(head)
    if metadata.get(PLAINTEXT_SHA256_METADATA) != expected.sha256:
        raise RuntimeError(f"Archive object plaintext sha256 changed: {object_key}")
    if metadata.get(ENCRYPTION_METADATA) != AGE_SCRYPT_ENCRYPTION:
        raise RuntimeError(f"Archive object encryption metadata changed: {object_key}")
    if metadata.get("riverhog-object-kind") != f"collection-{kind}":
        raise RuntimeError(f"Archive object kind metadata changed: {object_key}")


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
