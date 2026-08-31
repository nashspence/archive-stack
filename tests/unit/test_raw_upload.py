from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import pytest
from riverhog_age import CHUNK_SIZE, ResumableAgeScryptSession
from riverhog_core.domain.archive import RawVolumePlan
from riverhog_core.ports.archive_objects import (
    CompletedObjectReceipt,
    ResumableWriteConstraints,
    WriteSegmentReceipt,
    WriteSession,
)
from riverhog_core.raw_upload import (
    RawUploadCheckpoint,
    RawVolumeUploader,
    merge_raw_upload_checkpoints,
)
from riverhog_core.raw_volume import raw_age_aligned_unit_plans

ARCHIVE_UNIT_BYTES = 5 * 1024 * 1024


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
    segment_tokens: dict[int, str]


class MemoryResumableStore:
    def __init__(self) -> None:
        self.uploads: dict[str, _Upload] = {}
        self.objects: dict[str, tuple[bytes, str, dict[str, str], CompletedObjectReceipt]] = {}
        self.next_id = 1

    def write_constraints(self) -> ResumableWriteConstraints:
        return ResumableWriteConstraints(1, None, None)

    def begin_write(
        self,
        *,
        object_path: str,
        content_type: str,
        metadata: dict[str, str],
    ) -> WriteSession:
        write_token = f"raw-{self.next_id}"
        self.next_id += 1
        self.uploads[write_token] = _Upload(
            object_path,
            content_type,
            dict(metadata),
            {},
            {},
        )
        return WriteSession(object_path, write_token)

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        content: bytes,
    ) -> WriteSegmentReceipt:
        row = self.uploads[session.write_token]
        segment_token = f'"part-{number}-{hashlib.sha256(content).hexdigest()[:16]}"'
        row.parts[number] = content
        row.segment_tokens[number] = segment_token
        return WriteSegmentReceipt(number, segment_token, len(content))

    def list_segments(self, *, session: WriteSession) -> tuple[WriteSegmentReceipt, ...]:
        row = self.uploads[session.write_token]
        return tuple(
            WriteSegmentReceipt(number, row.segment_tokens[number], len(row.parts[number]))
            for number in sorted(row.parts)
        )

    def complete_write(
        self,
        *,
        session: WriteSession,
        segments: tuple[WriteSegmentReceipt, ...],
        expected_bytes: int,
        expected_content_type: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt:
        row = self.uploads[session.write_token]
        assert row.content_type == expected_content_type
        assert all(row.metadata.get(key) == value for key, value in expected_metadata.items())
        content = b"".join(row.parts[current.number] for current in segments)
        assert len(content) == expected_bytes
        receipt = CompletedObjectReceipt(
            object_path=session.object_path,
            revision="version-raw",
            entity_token='"complete-raw"',
            bytes=len(content),
            completed_at="2026-08-03T00:00:00Z",
        )
        self.objects[session.object_path] = (content, row.content_type, row.metadata, receipt)
        del self.uploads[session.write_token]
        return receipt

    def find_completed_write(
        self,
        *,
        object_path: str,
        expected_content_type: str,
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt | None:
        found = self.objects.get(object_path)
        if found is None:
            return None
        if found[1] != expected_content_type or any(
            found[2].get(key) != value for key, value in expected_metadata.items()
        ):
            return None
        return found[3]

    def abort_write(self, *, session: WriteSession) -> None:
        self.uploads.pop(session.write_token, None)


def _plan(content: bytes) -> RawVolumePlan:
    return RawVolumePlan(
        volume_id="segment-" + "0" * 64,
        sequence=0,
        source_path="large.bin",
        file_offset=0,
        plaintext_bytes=len(content),
        file_bytes=len(content),
        file_sha256=hashlib.sha256(content).hexdigest(),
    )


def _uploader(
    object_store: MemoryResumableStore,
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
    store = MemoryResumableStore()
    checkpoints = MemoryRawCheckpointStore()
    uploader = _uploader(store, checkpoints)
    checkpoint = uploader.open(
        collection_id=1,
        plan=plan,
        object_path="archives/opaque/volumes/segment-" + "0" * 64 + ".bin.age",
        relative_path="volumes/segment-" + "0" * 64 + ".bin.age",
        target_part_plaintext_bytes=ARCHIVE_UNIT_BYTES,
    )
    session = ResumableAgeScryptSession.from_state("archive passphrase", checkpoint.age_state_json)
    part_plans = raw_age_aligned_unit_plans(
        plan,
        session,
        target_plaintext_bytes=ARCHIVE_UNIT_BYTES,
    )
    assert len(part_plans) == 2

    first = part_plans[0]
    checkpoint = uploader.upload_next_unit(
        plan=plan,
        checkpoint=checkpoint,
        plaintext=content[first.plaintext_start : first.plaintext_end],
    )
    resumed = _uploader(store, checkpoints).open(
        collection_id=1,
        plan=plan,
        object_path=checkpoint.object_path,
        relative_path=checkpoint.relative_path,
        target_part_plaintext_bytes=ARCHIVE_UNIT_BYTES,
    )
    assert resumed.next_part == 1

    second = part_plans[1]
    resumed = _uploader(store, checkpoints).upload_next_unit(
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
    assert resumed.write_segments
    assert all(current.sha256 is not None for current in resumed.write_segments)


def test_raw_upload_revalidates_the_registered_part_identity() -> None:
    content = b"registered content"
    plan = _plan(content)
    store = MemoryResumableStore()
    checkpoints = MemoryRawCheckpointStore()
    uploader = _uploader(store, checkpoints)
    checkpoint = uploader.open(
        collection_id=1,
        plan=plan,
        object_path="archives/opaque/volumes/segment-" + "0" * 64 + ".bin.age",
        relative_path="volumes/segment-" + "0" * 64 + ".bin.age",
        target_part_plaintext_bytes=ARCHIVE_UNIT_BYTES,
        expected_part_sha256s=(hashlib.sha256(content).hexdigest(),),
    )

    with pytest.raises(ValueError, match="registered digest manifest"):
        uploader.upload_next_unit(
            plan=plan,
            checkpoint=checkpoint,
            plaintext=b"registered contenU",
        )


def test_raw_checkpoint_round_trips_and_rejects_unaligned_part_target() -> None:
    content = b"raw content"
    plan = _plan(content)
    store = MemoryResumableStore()
    checkpoints = MemoryRawCheckpointStore()
    uploader = _uploader(store, checkpoints)
    checkpoint = uploader.open(
        collection_id=1,
        plan=plan,
        object_path="archives/opaque/volumes/segment-" + "0" * 64 + ".bin.age",
        relative_path="volumes/segment-" + "0" * 64 + ".bin.age",
        target_part_plaintext_bytes=ARCHIVE_UNIT_BYTES,
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
