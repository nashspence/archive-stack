from __future__ import annotations

from collections.abc import Iterator

from arc_core.services.resumable_uploads import UploadLifecycleState, sync_upload_state


class _MissingUploadStore:
    def __init__(self) -> None:
        self.read_target_calls = 0

    def create_upload(self, target_path: str, length: int) -> str:
        raise AssertionError("create_upload should not be called")

    def get_offset(self, tus_url: str) -> int:
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
        raise FileNotFoundError(target_path)

    def iter_target(self, target_path: str) -> Iterator[bytes]:
        yield self.read_target(target_path)

    def delete_target(self, target_path: str) -> None:
        raise AssertionError("delete_target should not be called")

    def cancel_upload(self, tus_url: str) -> None:
        raise AssertionError("cancel_upload should not be called")


def test_sync_upload_state_preserves_progress_when_upload_disappears_mid_sync() -> None:
    store = _MissingUploadStore()
    current = UploadLifecycleState(
        tus_url="/uploads/fx-1/e1",
        uploaded_bytes=61,
        upload_expires_at=None,
    )

    updated = sync_upload_state(
        current=current,
        target_path="/.arc/recovery/fx-1/e1.enc",
        length=61,
        upload_store=store,
    )

    assert updated == current
    assert store.read_target_calls == 1


def test_sync_upload_state_preserves_partial_state_when_upload_disappears_mid_sync() -> None:
    store = _MissingUploadStore()
    current = UploadLifecycleState(
        tus_url="/uploads/fx-1/e1",
        uploaded_bytes=21,
        upload_expires_at="2026-04-26T00:00:00Z",
    )

    updated = sync_upload_state(
        current=current,
        target_path="/.arc/recovery/fx-1/e1.enc",
        length=61,
        upload_store=store,
    )

    assert updated == current
    assert store.read_target_calls == 1
