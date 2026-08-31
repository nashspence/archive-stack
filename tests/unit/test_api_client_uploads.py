from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from riverhog_api_client.uploads import upload_collection_units
from riverhog_protocol import (
    CollectionUploadUnitAssignmentDocument,
    CollectionUploadUnitWorkDocument,
    CollectionUploadVolumeSummaryDocument,
    CollectionUploadWorkBatchDocument,
)


@dataclass(frozen=True)
class _Volume:
    summary: CollectionUploadVolumeSummaryDocument
    plan_sha256: str
    units: tuple[CollectionUploadUnitWorkDocument, ...]


def _volume(sequence: int, units: int) -> _Volume:
    volume_id = f"pack-{sequence:064x}"
    return _Volume(
        summary=CollectionUploadVolumeSummaryDocument(
            volume_id=volume_id,
            sequence=sequence,
            kind="pack",
        ),
        plan_sha256="a" * 64,
        units=tuple(
            CollectionUploadUnitWorkDocument.model_validate(
                {
                    "unit": unit,
                    "payload_bytes": 1,
                    "plaintext_bytes": 1,
                    "sources": [
                        {
                            "path": f"source-{sequence}.bin",
                            "offset": unit,
                            "bytes": 1,
                            "artifact_sha256": "b" * 64,
                        }
                    ],
                    "state": "pending",
                }
            )
            for unit in range(units)
        ),
    )


class UploadApi:
    def __init__(
        self,
        volumes: list[_Volume],
        put: Callable[[str, int], None],
    ) -> None:
        self.volumes = volumes
        self.put = put
        self.closed = False
        self.committed = {volume.summary.volume_id: 0 for volume in volumes}
        self.lock = threading.Lock()

    def spawn(self) -> UploadApi:
        return self

    def close(self) -> None:
        self.closed = True

    def acquire_collection_upload_session_work(
        self,
        collection_id: int,
        *,
        limit: int = 16,
    ) -> CollectionUploadWorkBatchDocument:
        work = []
        with self.lock:
            for volume in self.volumes:
                unit = self.committed[volume.summary.volume_id]
                if unit >= len(volume.units):
                    continue
                work.append(
                    CollectionUploadUnitAssignmentDocument(
                        volume=volume.summary,
                        plan_sha256=volume.plan_sha256,
                        unit=volume.units[unit],
                    )
                )
        work = work[:limit]
        return CollectionUploadWorkBatchDocument(
            collection_id=collection_id,
            planning_complete=True,
            complete=not work,
            committed_payload_bytes=sum(self.committed.values()),
            work=work,
        )

    def get_collection_upload_session_unit(
        self,
        _collection_id: int,
        _volume_id: str,
        unit: int,
    ) -> CollectionUploadUnitWorkDocument:
        volume = next(item for item in self.volumes if item.summary.volume_id == _volume_id)
        return volume.units[unit]

    def put_collection_upload_session_unit(
        self,
        _collection_id: int,
        volume_id: str,
        unit: int,
        *,
        plan_sha256: str,
        content: bytes,
    ) -> CollectionUploadUnitWorkDocument:
        assert plan_sha256 == "a" * 64
        assert content == b"x"
        self.put(volume_id, unit)
        with self.lock:
            self.committed[volume_id] = max(self.committed[volume_id], unit + 1)
        volume = next(item for item in self.volumes if item.summary.volume_id == volume_id)
        return volume.units[unit].model_copy(update={"state": "committed"})


def _upload(api: UploadApi, *, concurrency: int, window: int, **kwargs: object) -> int:
    return upload_collection_units(
        api,
        1,
        content_for_unit=lambda _unit: b"x",
        concurrency=concurrency,
        window=window,
        **kwargs,
    )


def test_independent_volume_checkpoints_are_created_concurrently() -> None:
    rendezvous = threading.Barrier(2)
    completed: list[str] = []

    def put(volume_id: str, _unit: int) -> None:
        rendezvous.wait(timeout=2)
        completed.append(volume_id)

    api = UploadApi([_volume(0, 1), _volume(1, 1)], put)

    assert _upload(api, concurrency=2, window=2) == 2
    assert sorted(completed) == [
        f"pack-{0:064x}",
        f"pack-{1:064x}",
    ]


def test_later_units_begin_after_the_prior_volume_checkpoint() -> None:
    later_units: list[int] = []

    def put(_volume_id: str, unit: int) -> None:
        later_units.append(unit)

    api = UploadApi([_volume(0, 3)], put)

    assert _upload(api, concurrency=3, window=3) == 3
    assert later_units == [0, 1, 2]


def test_progress_callback_does_not_block_upload_workers() -> None:
    condition = threading.Condition()
    callback_started = threading.Event()
    callback_lock = threading.Lock()
    completed = 0
    callbacks: list[int] = []

    def put(volume_id: str, _unit: int) -> None:
        nonlocal completed
        if volume_id != f"pack-{0:064x}":
            assert callback_started.wait(timeout=2)
        with condition:
            completed += 1
            condition.notify_all()

    def committed(accepted: int) -> None:
        with callback_lock:
            callbacks.append(accepted)
            if len(callbacks) == 1:
                callback_started.set()
                with condition:
                    assert condition.wait_for(lambda: completed == 4, timeout=2)

    api = UploadApi([_volume(index, 1) for index in range(4)], put)

    assert _upload(api, concurrency=2, window=4, on_committed=committed) == 4
    assert callbacks == [1, 1, 1, 1]
