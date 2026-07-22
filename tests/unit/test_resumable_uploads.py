from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from riverhog_core.services.resumable_uploads import (
    UploadLifecycleState,
    create_or_resume_upload_state,
    sync_upload_state,
)
from riverhog_protocol.errors import NotFound


class _MissingUploadStore:
    def __init__(self) -> None:
        self.get_offset_calls = 0
        self.read_target_calls = 0

    def create_upload(self, target_path: str, length: int) -> str:
        raise AssertionError("create_upload should not be called")

    def get_offset(self, tus_url: str) -> int:
        self.get_offset_calls += 1
        return -1

    def append_upload_chunk(
        self,
        tus_url: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> tuple[int, str | None]:
        raise AssertionError("append_upload_chunk should not be called")

    def read_target(self, target_path: str) -> bytes:
        self.read_target_calls += 1
        raise NotFound(target_path)

    def iter_target(
        self,
        target_path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        _ = offset, size
        yield self.read_target(target_path)

    def delete_target(self, target_path: str) -> None:
        raise AssertionError("delete_target should not be called")

    def cancel_upload(self, tus_url: str) -> None:
        raise AssertionError("cancel_upload should not be called")


def test_sync_upload_state_skips_completed_uploads() -> None:
    store = _MissingUploadStore()
    current = UploadLifecycleState(
        tus_url="/uploads/fx-1/e1",
        uploaded_bytes=61,
        upload_expires_at=None,
    )

    updated = sync_upload_state(
        current=current,
        target_path="/.riverhog/recovery/fx-1/e1.enc",
        length=61,
        upload_store=store,
    )

    assert updated == current
    assert store.get_offset_calls == 0
    assert store.read_target_calls == 0


def test_sync_upload_state_preserves_partial_state_when_upload_disappears_mid_sync() -> None:
    store = _MissingUploadStore()
    current = UploadLifecycleState(
        tus_url="/uploads/fx-1/e1",
        uploaded_bytes=21,
        upload_expires_at="2026-04-26T00:00:00Z",
    )

    updated = sync_upload_state(
        current=current,
        target_path="/.riverhog/recovery/fx-1/e1.enc",
        length=61,
        upload_store=store,
    )

    assert updated == current
    assert store.get_offset_calls == 1
    assert store.read_target_calls == 1


def test_sync_upload_state_skips_live_unexpired_uploads() -> None:
    store = _MissingUploadStore()
    current = UploadLifecycleState(
        tus_url="/uploads/fx-1/e1",
        uploaded_bytes=21,
        upload_expires_at="2999-04-26T00:00:00Z",
    )

    updated = sync_upload_state(
        current=current,
        target_path="/.riverhog/recovery/fx-1/e1.enc",
        length=61,
        upload_store=store,
    )

    assert updated == current
    assert store.get_offset_calls == 0
    assert store.read_target_calls == 0


def test_sync_upload_state_force_checks_live_uploads() -> None:
    store = _MissingUploadStore()
    current = UploadLifecycleState(
        tus_url="/uploads/fx-1/e1",
        uploaded_bytes=21,
        upload_expires_at="2999-04-26T00:00:00Z",
    )

    updated = sync_upload_state(
        current=current,
        target_path="/.riverhog/recovery/fx-1/e1.enc",
        length=61,
        upload_store=store,
        force=True,
    )

    assert updated == current
    assert store.get_offset_calls == 1
    assert store.read_target_calls == 1


class _FinalizedTargetStore(_MissingUploadStore):
    def __init__(self) -> None:
        super().__init__()
        self.offset = 61
        self.created: list[tuple[str, int]] = []
        self.canceled: list[str] = []
        self.deleted: list[str] = []

    def create_upload(self, target_path: str, length: int) -> str:
        self.created.append((target_path, length))
        return "/uploads/fx-1/e2"

    def get_offset(self, tus_url: str) -> int:
        self.get_offset_calls += 1
        return self.offset

    def delete_target(self, target_path: str) -> None:
        self.deleted.append(target_path)

    def cancel_upload(self, tus_url: str) -> None:
        self.canceled.append(tus_url)


def test_forced_sync_requires_the_finalized_target_for_completed_state() -> None:
    store = _FinalizedTargetStore()
    current = UploadLifecycleState(
        tus_url="/uploads/fx-1/e1",
        uploaded_bytes=61,
        upload_expires_at=None,
    )

    updated = sync_upload_state(
        current=current,
        target_path="/.riverhog/recovery/fx-1/e1.enc",
        length=61,
        upload_store=store,
        force=True,
    )

    assert updated == UploadLifecycleState(
        tus_url=None,
        uploaded_bytes=0,
        upload_expires_at=None,
    )
    assert store.canceled == ["/uploads/fx-1/e1"]
    assert store.deleted == ["/.riverhog/recovery/fx-1/e1.enc"]


def test_resume_replaces_a_full_offset_without_a_finalized_target() -> None:
    store = _FinalizedTargetStore()
    current = UploadLifecycleState(
        tus_url="/uploads/fx-1/e1",
        uploaded_bytes=21,
        upload_expires_at="2999-04-26T00:00:00Z",
    )

    updated, tus_url = create_or_resume_upload_state(
        current=current,
        target_path="/.riverhog/recovery/fx-1/e1.enc",
        length=61,
        upload_store=store,
        ttl=timedelta(hours=1),
    )

    assert tus_url == "/uploads/fx-1/e2"
    assert updated.tus_url == tus_url
    assert updated.uploaded_bytes == 0
    assert updated.upload_expires_at is not None
    assert store.canceled == ["/uploads/fx-1/e1"]
    assert store.deleted == ["/.riverhog/recovery/fx-1/e1.enc"]
    assert store.created == [("/.riverhog/recovery/fx-1/e1.enc", 61)]


def test_finalized_target_verification_propagates_store_unavailability() -> None:
    class UnavailableStore(_FinalizedTargetStore):
        def read_target(self, target_path: str) -> bytes:
            raise RuntimeError(f"store unavailable for {target_path}")

    store = UnavailableStore()
    current = UploadLifecycleState(
        tus_url="/uploads/fx-1/e1",
        uploaded_bytes=61,
        upload_expires_at=None,
    )

    with pytest.raises(RuntimeError, match="store unavailable"):
        sync_upload_state(
            current=current,
            target_path="/.riverhog/recovery/fx-1/e1.enc",
            length=61,
            upload_store=store,
            force=True,
        )

    assert store.canceled == []
    assert store.deleted == []
