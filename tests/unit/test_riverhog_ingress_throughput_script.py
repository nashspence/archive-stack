from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "riverhog_ingress_throughput.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("riverhog_ingress_throughput", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeApi:
    def __init__(
        self,
        *,
        files: int,
        fail_patch: bool = False,
        fail_preparation: bool = False,
    ) -> None:
        self.files = files
        self.fail_patch = fail_patch
        self.fail_preparation = fail_preparation
        self.registered: list[dict[str, object]] = []
        self.patch_offsets: dict[str, list[int]] = {}
        self.canceled = False
        self.closed = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(files)

    def create_or_resume_collection_upload_session(self, *_args: Any, **_kwargs: Any):
        return {"collection_id": 1}

    def register_collection_upload_session_file(
        self, _collection_id: int, file: dict[str, object]
    ) -> dict[str, object]:
        self.registered.append(file)
        return {"files": [file]}

    def create_or_resume_collection_file_upload(
        self, _collection_id: int, path: str
    ) -> dict[str, object]:
        if self.fail_preparation:
            raise RuntimeError("preparation failed")
        return {
            "upload_url": f"https://uploads.test/{path}",
            "offset": 0,
            "length": 4 * 1024 * 1024,
            "checksum_algorithm": "sha256",
        }

    def append_upload_chunk(self, upload_url: str, **kwargs: Any) -> dict[str, object]:
        if self.fail_patch:
            raise RuntimeError("probe failed")
        offset = int(kwargs["offset"])
        with self.lock:
            self.patch_offsets.setdefault(upload_url, []).append(offset)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        if offset == 0:
            self.barrier.wait(timeout=2)
        with self.lock:
            self.active -= 1
        return {"offset": offset + len(bytes(kwargs["content"]))}

    def cancel_collection_upload_session(self, _collection_id: int) -> dict[str, object]:
        self.canceled = True
        return {"state": "canceled"}

    def close(self) -> None:
        self.closed += 1


def test_probe_overlaps_uploads_and_cancels_the_incomplete_session() -> None:
    module = _module()
    api = _FakeApi(files=2)

    result = module._run(
        api,
        api_factory=lambda: api,
        tag="ingress-throughput-probe",
        idempotency_key="probe-1",
        archive_store="b2",
        files=2,
        bytes_per_file=2 * module.MIB,
        chunk_bytes=module.MIB,
    )

    assert result.bytes == 4 * module.MIB
    assert result.files == 2
    assert api.max_active == 2
    assert len(api.registered) == 2
    assert all(item["bytes"] == 3 * module.MIB for item in api.registered)
    assert {tuple(offsets) for offsets in api.patch_offsets.values()} == {(0, module.MIB)}
    assert api.canceled
    assert api.closed == 2


def test_probe_cancels_the_session_after_transfer_failure() -> None:
    module = _module()
    api = _FakeApi(files=1, fail_patch=True)

    with pytest.raises(RuntimeError, match="probe failed"):
        module._run(
            api,
            api_factory=lambda: api,
            tag="ingress-throughput-probe",
            idempotency_key="probe-1",
            archive_store="b2",
            files=1,
            bytes_per_file=module.MIB,
            chunk_bytes=module.MIB,
        )

    assert api.canceled
    assert api.closed == 1


def test_probe_cancels_the_session_after_preparation_failure() -> None:
    module = _module()
    api = _FakeApi(files=1, fail_preparation=True)

    with pytest.raises(RuntimeError, match="preparation failed"):
        module._prepare_uploads(
            api,
            tag="ingress-throughput-probe",
            idempotency_key="probe-1",
            archive_store="b2",
            files=1,
            bytes_per_file=module.MIB,
        )

    assert api.canceled


def test_probe_script_is_executable() -> None:
    assert SCRIPT.stat().st_mode & 0o111
