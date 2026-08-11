from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from riverhog_api_client.uploads import upload_collection_units


def _volume(volume_id: str, units: int) -> dict[str, object]:
    return {
        "volume_id": volume_id,
        "plan_sha256": f"plan-{volume_id}",
        "units": [
            {
                "unit": unit,
                "payload_bytes": 1,
                "sources": [],
                "state": "planned",
            }
            for unit in range(units)
        ],
    }


class UploadApi:
    def __init__(
        self,
        volumes: list[dict[str, object]],
        put: Callable[[str, int], None],
    ) -> None:
        self.volumes = volumes
        self.put = put
        self.closed = False

    def spawn(self) -> UploadApi:
        return UploadApi(self.volumes, self.put)

    def close(self) -> None:
        self.closed = True

    def list_collection_upload_session_volumes(self, _collection_id: int) -> dict[str, Any]:
        return {"volumes": self.volumes}

    def get_collection_upload_session_unit(
        self,
        _collection_id: int,
        _volume_id: str,
        unit: int,
    ) -> dict[str, object]:
        return {"unit": unit, "state": "planned"}

    def put_collection_upload_session_unit(
        self,
        _collection_id: int,
        volume_id: str,
        unit: int,
        *,
        plan_sha256: str,
        content: bytes,
    ) -> dict[str, object]:
        assert plan_sha256 == f"plan-{volume_id}"
        assert content == b"x"
        self.put(volume_id, unit)
        return {"unit": unit, "state": "committed"}


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

    api = UploadApi([_volume("one", 1), _volume("two", 1)], put)

    assert _upload(api, concurrency=2, window=2) == 2
    assert sorted(completed) == ["one", "two"]


def test_later_units_begin_after_the_volume_checkpoint_and_overlap() -> None:
    checkpoint_created = threading.Event()
    later_rendezvous = threading.Barrier(2)
    later_units: list[int] = []

    def put(_volume_id: str, unit: int) -> None:
        if unit == 0:
            time.sleep(0.02)
            checkpoint_created.set()
            return
        assert checkpoint_created.wait(timeout=2)
        later_rendezvous.wait(timeout=2)
        later_units.append(unit)

    api = UploadApi([_volume("one", 3)], put)

    assert _upload(api, concurrency=3, window=3) == 3
    assert sorted(later_units) == [1, 2]


def test_progress_callback_does_not_block_upload_workers() -> None:
    condition = threading.Condition()
    callback_started = threading.Event()
    callback_lock = threading.Lock()
    completed = 0
    callbacks: list[int] = []

    def put(volume_id: str, _unit: int) -> None:
        nonlocal completed
        if volume_id != "volume-0":
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

    api = UploadApi([_volume(f"volume-{index}", 1) for index in range(4)], put)

    assert _upload(api, concurrency=2, window=4, on_committed=committed) == 4
    assert callbacks == [1, 1, 1, 1]
