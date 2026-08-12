from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import pytest
from riverhog_age import CHUNK_SIZE, S3_MIN_PART_SIZE, ResumableAgeScryptSession
from riverhog_core.domain.archive import RawVolumePlan
from riverhog_core.ports.archive_objects import (
    CompletedObjectReceipt,
    MultipartPartReceipt,
    MultipartUpload,
)
from riverhog_core.raw_upload import (
    RawUploadCheckpoint,
    RawVolumeUploader,
    merge_raw_upload_checkpoints,
)
from riverhog_core.raw_volume import raw_s3_part_plans


class MemoryRawCheckpointStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[int, str], str] = {}

    def load_raw_upload_checkpoint(self, *, collection_id: int, volume_id: str) -> str | None:
        return self.rows.get((collection_id, volume_id))

    def merge_raw_upload_checkpoint(
        self, *, collection_id: int, volume_id: str, checkpoint_json: str
    ) -> str:
        key = (collection_id, volume_id)
        candidate = RawUploadCheckpoint.from_json(checkpoint_json)
        current = RawUploadCheckpoint.from_json(self.rows[key]) if key in self.rows else candidate
        encoded = merge_raw_upload_checkpoints(current, candidate).to_json()
        self.rows[key] = encoded
        return encoded

    def delete_raw_upload_checkpoint(self, *, collection_id: int, volume_id: str) -> None:
        self.rows.pop((collection_id, volume_id), None)


@dataclass
class _Upload:
    path: str
    content_type: str
    metadata: dict[str, str]
    parts: dict[int, bytes]
    etags: dict[int, str]


class MemoryMultipartStore:
    def __init__(self) -> None:
        self.uploads: dict[str, _Upload] = {}
        self.objects: dict[str, tuple[bytes, dict[str, str], CompletedObjectReceipt]] = {}
        self.next_id = 1

    def create_multipart_upload(
        self,
        *,
        object_path: str,
        content_type: str,
        metadata: dict[str, str],
    ) -> MultipartUpload:
        upload_id = f"raw-{self.next_id}"
        self.next_id += 1
        self.uploads[upload_id] = _Upload(
            object_path,
            content_type,
            dict(metadata),
            {},
            {},
        )
        return MultipartUpload(object_path, upload_id)

    def upload_part(
        self,
        *,
        upload: MultipartUpload,
        number: int,
        content: bytes,
    ) -> MultipartPartReceipt:
        row = self.uploads[upload.upload_id]
        etag = f'"part-{number}-{hashlib.sha256(content).hexdigest()[:16]}"'
        row.parts[number] = content
        row.etags[number] = etag
        return MultipartPartReceipt(number, etag, len(content))

    def list_parts(self, *, upload: MultipartUpload) -> tuple[MultipartPartReceipt, ...]:
        row = self.uploads[upload.upload_id]
        return tuple(
            MultipartPartReceipt(number, row.etags[number], len(row.parts[number]))
            for number in sorted(row.parts)
        )

    def complete_multipart_upload(
        self,
        *,
        upload: MultipartUpload,
        parts: tuple[MultipartPartReceipt, ...],
        expected_bytes: int,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt:
        row = self.uploads[upload.upload_id]
        assert all(row.metadata.get(key) == value for key, value in expected_metadata.items())
        content = b"".join(row.parts[current.number] for current in parts)
        assert len(content) == expected_bytes
        receipt = CompletedObjectReceipt(
            object_path=upload.object_path,
            version_id="version-raw",
            etag='"complete-raw"',
            bytes=len(content),
            completed_at="2026-08-03T00:00:00Z",
        )
        self.objects[upload.object_path] = (content, row.metadata, receipt)
        del self.uploads[upload.upload_id]
        return receipt

    def head_completed_object(
        self,
        *,
        object_path: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None:
        found = self.objects.get(object_path)
        if found is None:
            return None
        if any(found[1].get(key) != value for key, value in expected_metadata.items()):
            return None
        return found[2]

    def abort_multipart_upload(self, *, upload: MultipartUpload) -> None:
        self.uploads.pop(upload.upload_id, None)


def _plan(content: bytes) -> RawVolumePlan:
    return RawVolumePlan(
        volume_id="segment-000000000000",
        sequence=0,
        source_path="large.bin",
        file_offset=0,
        plaintext_bytes=len(content),
        file_bytes=len(content),
        file_sha256=hashlib.sha256(content).hexdigest(),
    )


def _uploader(
    object_store: MemoryMultipartStore,
    checkpoint_store: MemoryRawCheckpointStore,
) -> RawVolumeUploader:
    return RawVolumeUploader(
        object_store=object_store,
        checkpoint_store=checkpoint_store,
        passphrase="archive passphrase",
        scrypt_log_n=1,
    )


def test_raw_upload_resumes_on_server_defined_age_part_boundaries() -> None:
    content = (b"0123456789abcdef" * ((6 * 1024 * 1024) // 16)) + b"tail"
    plan = _plan(content)
    store = MemoryMultipartStore()
    checkpoints = MemoryRawCheckpointStore()
    uploader = _uploader(store, checkpoints)
    checkpoint = uploader.open(
        collection_id=1,
        plan=plan,
        object_path="archives/opaque/volumes/segment-000000000000.bin.age",
        relative_path="volumes/segment-000000000000.bin.age",
        target_part_plaintext_bytes=S3_MIN_PART_SIZE,
    )
    session = ResumableAgeScryptSession.from_state("archive passphrase", checkpoint.age_state_json)
    part_plans = raw_s3_part_plans(
        plan,
        session,
        target_plaintext_bytes=S3_MIN_PART_SIZE,
    )
    assert len(part_plans) == 2

    first = part_plans[0]
    checkpoint = uploader.upload_next_part(
        plan=plan,
        checkpoint=checkpoint,
        plaintext=content[first.plaintext_start : first.plaintext_end],
    )
    resumed = _uploader(store, checkpoints).open(
        collection_id=1,
        plan=plan,
        object_path=checkpoint.object_path,
        relative_path=checkpoint.relative_path,
        target_part_plaintext_bytes=S3_MIN_PART_SIZE,
    )
    assert resumed.next_part == 1

    second = part_plans[1]
    resumed = _uploader(store, checkpoints).upload_next_part(
        plan=plan,
        checkpoint=resumed,
        plaintext=content[second.plaintext_start : second.plaintext_end],
    )
    receipt = uploader.sealed_receipt(resumed)

    assert resumed.completed is not None
    assert receipt.source_path == "large.bin"
    assert [part.plaintext_bytes for part in receipt.parts] == [
        first.plaintext_len,
        second.plaintext_len,
    ]
    assert sum(part.plaintext_bytes for part in receipt.parts) == len(content)


def test_raw_upload_revalidates_the_registered_part_identity() -> None:
    content = b"registered content"
    plan = _plan(content)
    store = MemoryMultipartStore()
    checkpoints = MemoryRawCheckpointStore()
    uploader = _uploader(store, checkpoints)
    checkpoint = uploader.open(
        collection_id=1,
        plan=plan,
        object_path="archives/opaque/volumes/segment-000000000000.bin.age",
        relative_path="volumes/segment-000000000000.bin.age",
        target_part_plaintext_bytes=S3_MIN_PART_SIZE,
        expected_part_sha256s=(hashlib.sha256(content).hexdigest(),),
    )

    with pytest.raises(ValueError, match="registered digest manifest"):
        uploader.upload_next_part(
            plan=plan,
            checkpoint=checkpoint,
            plaintext=b"registered contenU",
        )


def test_raw_checkpoint_round_trips_and_rejects_unaligned_part_target() -> None:
    content = b"raw content"
    plan = _plan(content)
    store = MemoryMultipartStore()
    checkpoints = MemoryRawCheckpointStore()
    uploader = _uploader(store, checkpoints)
    checkpoint = uploader.open(
        collection_id=1,
        plan=plan,
        object_path="archives/opaque/volumes/segment-000000000000.bin.age",
        relative_path="volumes/segment-000000000000.bin.age",
        target_part_plaintext_bytes=S3_MIN_PART_SIZE,
    )

    payload = json.loads(checkpoint.to_json())
    assert RawUploadCheckpoint.from_json(checkpoint.to_json()) == checkpoint

    invalid = {**payload, "target_part_plaintext_bytes": CHUNK_SIZE + 1}
    try:
        RawUploadCheckpoint.from_json(json.dumps(invalid))
    except ValueError as exc:
        assert "age-chunk multiple" in str(exc)
    else:
        raise AssertionError("unaligned raw part target was accepted")
