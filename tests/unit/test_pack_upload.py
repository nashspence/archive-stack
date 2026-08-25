from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pytest
from riverhog_core.domain.archive import ArchiveFile
from riverhog_core.pack_upload import (
    PackUploadCheckpoint,
    PackVolumeUploader,
    merge_pack_upload_checkpoints,
)
from riverhog_core.pack_volume import pack_unit_descriptors, plan_pack_volume
from riverhog_core.ports.archive_objects import (
    CompletedObjectReceipt,
    ResumableWriteConstraints,
    WriteSegmentReceipt,
    WriteSession,
)

ARCHIVE_UNIT_BYTES = 5 * 1024 * 1024


def _file(path: str, content: bytes) -> ArchiveFile:
    return ArchiveFile(path=path, bytes=len(content), sha256=hashlib.sha256(content).hexdigest())


class MemoryCheckpointStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[int, str], str] = {}

    def load_pack_upload_checkpoint(self, *, collection_id: int, volume_id: str) -> str | None:
        return self.rows.get((collection_id, volume_id))

    def merge_pack_upload_checkpoint(
        self, *, collection_id: int, volume_id: str, checkpoint_json: str
    ) -> str:
        key = (collection_id, volume_id)
        candidate = PackUploadCheckpoint.from_json(checkpoint_json)
        current = PackUploadCheckpoint.from_json(self.rows[key]) if key in self.rows else candidate
        encoded = merge_pack_upload_checkpoints(current, candidate).to_json()
        self.rows[key] = encoded
        return encoded

    def delete_pack_upload_checkpoint(self, *, collection_id: int, volume_id: str) -> None:
        self.rows.pop((collection_id, volume_id), None)


@dataclass
class _UploadRow:
    path: str
    content_type: str
    metadata: dict[str, str]
    parts: dict[int, bytes]
    segment_tokens: dict[int, str]


class MemoryResumableStore:
    def __init__(self) -> None:
        self.uploads: dict[str, _UploadRow] = {}
        self.objects: dict[str, tuple[bytes, dict[str, str], CompletedObjectReceipt]] = {}
        self.next_id = 1
        self.complete_calls = 0
        self.lose_complete_response = False

    def write_constraints(self) -> ResumableWriteConstraints:
        return ResumableWriteConstraints(1, None, None)

    def begin_write(
        self,
        *,
        object_path: str,
        content_type: str,
        metadata: dict[str, str],
    ) -> WriteSession:
        write_token = f"upload-{self.next_id}"
        self.next_id += 1
        self.uploads[write_token] = _UploadRow(object_path, content_type, dict(metadata), {}, {})
        return WriteSession(object_path, write_token)

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        content: bytes,
    ) -> WriteSegmentReceipt:
        row = self.uploads[session.write_token]
        segment_token = f'"{hashlib.md5(content, usedforsecurity=False).hexdigest()}"'
        row.parts[number] = content
        row.segment_tokens[number] = segment_token
        return WriteSegmentReceipt(number=number, segment_token=segment_token, bytes=len(content))

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
        expected_metadata: dict[str, str],
    ) -> CompletedObjectReceipt:
        self.complete_calls += 1
        row = self.uploads[session.write_token]
        assert all(row.metadata.get(key) == value for key, value in expected_metadata.items())
        content = b"".join(row.parts[current.number] for current in segments)
        assert len(content) == expected_bytes
        receipt = CompletedObjectReceipt(
            object_path=session.object_path,
            revision="version-1",
            entity_token='"completed"',
            bytes=len(content),
            completed_at="2026-08-03T00:00:00Z",
        )
        self.objects[session.object_path] = (content, row.metadata, receipt)
        del self.uploads[session.write_token]
        if self.lose_complete_response:
            self.lose_complete_response = False
            raise ConnectionError("completion response lost")
        return receipt

    def find_completed_write(
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

    def abort_write(self, *, session: WriteSession) -> None:
        self.uploads.pop(session.write_token, None)


def _uploader(
    store: MemoryResumableStore,
    checkpoints: MemoryCheckpointStore,
) -> PackVolumeUploader:
    return PackVolumeUploader(
        object_store=store,
        checkpoint_store=checkpoints,
        passphrase="archive passphrase",
        scrypt_log_n=1,
    )


def _payload(plan, unit, contents: dict[str, bytes]) -> bytes:
    descriptor = pack_unit_descriptors(plan)[unit]
    return b"".join(contents[source.path] for source in descriptor.sources)


def test_pack_is_acknowledged_only_after_final_resumable_write_completion() -> None:
    contents = {"a.txt": b"alpha", "b.txt": b"beta"}
    plan = plan_pack_volume([_file(path, value) for path, value in contents.items()], sequence=0)
    store = MemoryResumableStore()
    checkpoints = MemoryCheckpointStore()
    uploader = _uploader(store, checkpoints)
    checkpoint = uploader.open(
        collection_id=1,
        plan=plan,
        object_path="archives/opaque/volumes/pack-000000000000.tar.age",
        relative_path="volumes/pack-000000000000.tar.age",
    )

    assert checkpoint.completed is None
    checkpoint = uploader.upload_next_unit(
        plan=plan,
        checkpoint=checkpoint,
        payload_chunks=(_payload(plan, 0, contents),),
    )
    receipt = uploader.sealed_receipt(plan=plan, checkpoint=checkpoint)

    assert checkpoint.completed is not None
    assert store.complete_calls == 1
    assert receipt.volume_id == plan.volume_id
    assert receipt.parts[0].plaintext_bytes == plan.plaintext_bytes
    assert receipt.stored_bytes == checkpoint.completed.bytes
    assert checkpoint.write_segments
    assert all(current.sha256 is not None for current in checkpoint.write_segments)


def test_pack_upload_revalidates_the_registered_source_identity() -> None:
    content = b"registered content"
    plan = plan_pack_volume([_file("only.txt", content)], sequence=0)
    store = MemoryResumableStore()
    checkpoints = MemoryCheckpointStore()
    uploader = _uploader(store, checkpoints)
    checkpoint = uploader.open(
        collection_id=1,
        plan=plan,
        object_path="archives/opaque/volumes/pack-000000000000.tar.age",
        relative_path="volumes/pack-000000000000.tar.age",
    )

    with pytest.raises(ValueError, match="pack unit source sha256 mismatch"):
        uploader.upload_next_unit(
            plan=plan,
            checkpoint=checkpoint,
            payload_chunks=(b"registered contenU",),
        )


def test_checkpoint_resumes_between_whole_file_units() -> None:
    contents = {f"f-{index}.bin": bytes([index]) * (1024 * 1024) for index in range(7)}
    plan = plan_pack_volume(
        [_file(path, value) for path, value in contents.items()],
        sequence=0,
        part_plaintext_bytes=ARCHIVE_UNIT_BYTES,
    )
    assert len(plan.units) >= 2
    store = MemoryResumableStore()
    checkpoints = MemoryCheckpointStore()
    first_process = _uploader(store, checkpoints)
    checkpoint = first_process.open(
        collection_id=1,
        plan=plan,
        object_path="archives/opaque/volumes/pack-000000000000.tar.age",
        relative_path="volumes/pack-000000000000.tar.age",
    )
    checkpoint = first_process.upload_next_unit(
        plan=plan,
        checkpoint=checkpoint,
        payload_chunks=(_payload(plan, 0, contents),),
    )

    second_process = _uploader(store, checkpoints)
    resumed = second_process.open(
        collection_id=1,
        plan=plan,
        object_path=checkpoint.object_path,
        relative_path=checkpoint.relative_path,
    )
    assert resumed.next_unit == 1
    while resumed.completed is None:
        resumed = second_process.upload_next_unit(
            plan=plan,
            checkpoint=resumed,
            payload_chunks=(_payload(plan, resumed.next_unit, contents),),
        )

    assert len(resumed.archive_parts) == len(plan.units)
    assert store.complete_calls == 1


def test_lost_complete_response_is_recovered_from_final_object_metadata() -> None:
    contents = {"only.txt": b"content"}
    plan = plan_pack_volume([_file("only.txt", contents["only.txt"])], sequence=0)
    store = MemoryResumableStore()
    checkpoints = MemoryCheckpointStore()
    uploader = _uploader(store, checkpoints)
    checkpoint = uploader.open(
        collection_id=1,
        plan=plan,
        object_path="archives/opaque/volumes/pack-000000000000.tar.age",
        relative_path="volumes/pack-000000000000.tar.age",
    )
    store.lose_complete_response = True

    with pytest.raises(ConnectionError, match="response lost"):
        uploader.upload_next_unit(
            plan=plan,
            checkpoint=checkpoint,
            payload_chunks=(_payload(plan, 0, contents),),
        )

    recovered = _uploader(store, checkpoints).open(
        collection_id=1,
        plan=plan,
        object_path=checkpoint.object_path,
        relative_path=checkpoint.relative_path,
    )
    assert recovered.completed is not None
    assert _uploader(store, checkpoints).sealed_receipt(plan=plan, checkpoint=recovered)


def test_checkpoint_round_trips() -> None:
    contents = {"only.txt": b"content"}
    plan = plan_pack_volume([_file("only.txt", contents["only.txt"])], sequence=0)
    store = MemoryResumableStore()
    checkpoints = MemoryCheckpointStore()
    uploader = _uploader(store, checkpoints)
    checkpoint = uploader.open(
        collection_id=1,
        plan=plan,
        object_path="archives/opaque/volumes/pack-000000000000.tar.age",
        relative_path="volumes/pack-000000000000.tar.age",
    )
    checkpoint = uploader.upload_next_unit(
        plan=plan,
        checkpoint=checkpoint,
        payload_chunks=(_payload(plan, 0, contents),),
    )
    assert PackUploadCheckpoint.from_json(checkpoint.to_json()) == checkpoint


class LoseFirstPartResponseStore(MemoryResumableStore):
    def __init__(self) -> None:
        super().__init__()
        self.lose_part_response = True

    def write_segment(
        self,
        *,
        session: WriteSession,
        number: int,
        content: bytes,
    ) -> WriteSegmentReceipt:
        receipt = super().write_segment(session=session, number=number, content=content)
        if self.lose_part_response:
            self.lose_part_response = False
            raise ConnectionError("part response lost")
        return receipt


def test_lost_part_response_is_retried_at_the_same_deterministic_part_number() -> None:
    contents = {"only.txt": b"content"}
    plan = plan_pack_volume([_file("only.txt", contents["only.txt"])], sequence=0)
    store = LoseFirstPartResponseStore()
    checkpoints = MemoryCheckpointStore()
    uploader = _uploader(store, checkpoints)
    checkpoint = uploader.open(
        collection_id=1,
        plan=plan,
        object_path="archives/opaque/volumes/pack-000000000000.tar.age",
        relative_path="volumes/pack-000000000000.tar.age",
    )

    with pytest.raises(ConnectionError, match="part response lost"):
        uploader.upload_next_unit(
            plan=plan,
            checkpoint=checkpoint,
            payload_chunks=(_payload(plan, 0, contents),),
        )

    persisted = PackUploadCheckpoint.from_json(checkpoints.rows[(1, plan.volume_id)])
    assert persisted.archive_parts == ()
    assert store.uploads[persisted.write_token].parts.keys() == {1}

    resumed_uploader = _uploader(store, checkpoints)
    resumed = resumed_uploader.open(
        collection_id=1,
        plan=plan,
        object_path=persisted.object_path,
        relative_path=persisted.relative_path,
    )
    completed = resumed_uploader.upload_next_unit(
        plan=plan,
        checkpoint=resumed,
        payload_chunks=(_payload(plan, 0, contents),),
    )

    assert completed.completed is not None
    assert [part.number for part in completed.archive_parts] == [1]
    assert store.complete_calls == 1
