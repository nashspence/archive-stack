from __future__ import annotations

import hashlib
import threading
import time

from riverhog_age import S3_MIN_PART_SIZE, ResumableAgeScryptSession
from riverhog_core.domain.archive import RawVolumePlan, StoredPartReceipt
from riverhog_core.raw_retrieval import (
    RawFileRangeReader,
    RawVolumeRangeReader,
    RawVolumeRetrievalSource,
)
from riverhog_core.raw_volume import raw_s3_part_plans


class SlowRangeStore:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.lock = threading.Lock()
        self.active = 0
        self.maximum_active = 0

    def iter_object_range(
        self,
        *,
        object_path: str,
        version_id: str | None,
        expected_bytes: int,
        offset: int,
        size: int,
    ):
        del object_path, version_id
        assert expected_bytes == len(self.content)
        with self.lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        time.sleep(0.02)
        yield self.content[offset : offset + size]
        with self.lock:
            self.active -= 1


def _source(content: bytes):
    plan = RawVolumePlan(
        volume_id="segment-000000000000",
        sequence=0,
        source_path="large.bin",
        file_offset=0,
        plaintext_bytes=len(content),
        file_bytes=len(content),
        file_sha256=hashlib.sha256(content).hexdigest(),
    )
    session = ResumableAgeScryptSession.create(
        "archive passphrase",
        log_n=1,
        plaintext_size=len(content),
    )
    receipts = []
    stored = []
    for part in raw_s3_part_plans(
        plan,
        session,
        target_plaintext_bytes=S3_MIN_PART_SIZE,
    ):
        plaintext = content[part.plaintext_start : part.plaintext_end]
        ciphertext = session.encrypt_part(
            part,
            lambda _chunk_index, start, end: content[start:end],
            plaintext_size=plan.plaintext_bytes,
        )
        stored.append(ciphertext)
        receipts.append(
            StoredPartReceipt(
                number=part.part_number,
                plaintext_start=part.plaintext_start,
                plaintext_bytes=part.plaintext_len,
                plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
                stored_bytes=len(ciphertext),
                stored_sha256=hashlib.sha256(ciphertext).hexdigest(),
                etag=f"part-{part.part_number}",
            )
        )
    state = session.export_state(plaintext_size=len(content)).to_json_bytes().decode("utf-8")
    source = RawVolumeRetrievalSource(
        volume_id=plan.volume_id,
        object_path="archives/x/volumes/segment-000000000000.bin.age",
        version_id="v1",
        source_path=plan.source_path,
        file_offset=0,
        plaintext_bytes=len(content),
        file_bytes=len(content),
        file_sha256=plan.file_sha256,
        age_state_json=state,
        parts=tuple(receipts),
    )
    return source, b"".join(stored)


def test_raw_retrieval_prefetches_parts_in_parallel_but_emits_file_order() -> None:
    content = bytes(range(256)) * (16 * 1024 * 1024 // 256)
    source, ciphertext = _source(content)
    store = SlowRangeStore(ciphertext)
    volume_reader = RawVolumeRangeReader(
        store,
        passphrase="archive passphrase",
        request_concurrency=4,
        max_inflight_bytes=64 * 1024 * 1024,
    )

    recovered = b"".join(RawFileRangeReader(volume_reader).iter_file((source,)))

    assert recovered == content
    assert store.maximum_active >= 2


def test_raw_reader_reports_downstream_backpressure_after_ordered_delivery() -> None:
    import time

    content = b"r" * (2 * 1024 * 1024)
    source, ciphertext = _source(content)
    timings = []
    reader = RawVolumeRangeReader(
        SlowRangeStore(ciphertext),
        passphrase="archive passphrase",
        request_concurrency=2,
        timing_observer=timings.append,
    )

    recovered = bytearray()
    for chunk in reader.iter_volume(source):
        recovered.extend(chunk)
        time.sleep(0.001)

    assert bytes(recovered) == content
    assert timings
    assert all(current.downstream_seconds > 0 for current in timings)
