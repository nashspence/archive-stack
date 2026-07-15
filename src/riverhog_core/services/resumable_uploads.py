from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from riverhog_core.ports.upload_store import UploadStore
from riverhog_core.timestamps import format_utc_timestamp, parse_utc_timestamp, utc_now


@dataclass(frozen=True, slots=True)
class UploadLifecycleState:
    tus_url: str | None
    uploaded_bytes: int
    upload_expires_at: str | None


def upload_state_name(*, uploaded_bytes: int, length: int) -> str:
    if length > 0 and uploaded_bytes >= length:
        return "uploaded"
    if uploaded_bytes > 0:
        return "partial"
    return "pending"


def sync_upload_state(
    *,
    current: UploadLifecycleState,
    target_path: str,
    length: int,
    upload_store: UploadStore,
    force: bool = False,
) -> UploadLifecycleState:
    if current.tus_url is None:
        return current
    if upload_state_name(uploaded_bytes=current.uploaded_bytes, length=length) == "uploaded":
        return current
    if not force and _upload_state_is_live(current=current, length=length):
        return current

    offset = upload_store.get_offset(current.tus_url)
    if offset == -1:
        if not _upload_target_exists(upload_store, target_path):
            # Another worker may have consumed and deleted the backing upload between
            # loading the row and syncing the current offset. Preserve the current
            # lifecycle state here so a stale sync cannot roll committed progress back
            # to zero during fetch completion.
            return current
        offset = length

    expires_at = current.upload_expires_at
    if upload_state_name(uploaded_bytes=offset, length=length) == "uploaded":
        expires_at = None

    return UploadLifecycleState(
        tus_url=current.tus_url,
        uploaded_bytes=offset,
        upload_expires_at=expires_at,
    )


def create_or_resume_upload_state(
    *,
    current: UploadLifecycleState,
    target_path: str,
    length: int,
    upload_store: UploadStore,
    ttl: timedelta,
) -> tuple[UploadLifecycleState, str]:
    synced = sync_upload_state(
        current=current,
        target_path=target_path,
        length=length,
        upload_store=upload_store,
        force=True,
    )
    tus_url = synced.tus_url
    uploaded_bytes = synced.uploaded_bytes
    if tus_url is None:
        tus_url = upload_store.create_upload(target_path, length)
        uploaded_bytes = 0

    expires_at = synced.upload_expires_at
    if upload_state_name(uploaded_bytes=uploaded_bytes, length=length) != "uploaded":
        expires_at = upload_expiry_timestamp(ttl)

    updated = UploadLifecycleState(
        tus_url=tus_url,
        uploaded_bytes=uploaded_bytes,
        upload_expires_at=expires_at,
    )
    return updated, tus_url


def expire_upload_state(
    *,
    current: UploadLifecycleState,
    target_path: str,
    upload_store: UploadStore,
    now: datetime | None = None,
) -> tuple[UploadLifecycleState, bool]:
    if current.upload_expires_at is None:
        return current, False

    effective_now = now or utc_now()
    expires_at = parse_utc_timestamp(current.upload_expires_at)
    if expires_at > effective_now:
        return current, False

    if current.tus_url is not None:
        offset = upload_store.get_offset(current.tus_url)
        if offset == -1:
            if not _upload_target_exists(upload_store, target_path):
                return (
                    UploadLifecycleState(
                        tus_url=None,
                        uploaded_bytes=0,
                        upload_expires_at=None,
                    ),
                    True,
                )
        upload_store.cancel_upload(current.tus_url)
        upload_store.delete_target(target_path)

    return (
        UploadLifecycleState(
            tus_url=None,
            uploaded_bytes=0,
            upload_expires_at=None,
        ),
        True,
    )


def _upload_target_exists(upload_store: UploadStore, target_path: str) -> bool:
    try:
        for _ in upload_store.iter_target(target_path):
            break
    except Exception:
        return False
    return True


def _upload_state_is_live(
    *,
    current: UploadLifecycleState,
    length: int,
    now: datetime | None = None,
) -> bool:
    if current.upload_expires_at is None:
        return False
    if upload_state_name(uploaded_bytes=current.uploaded_bytes, length=length) == "uploaded":
        return False
    effective_now = now or utc_now()
    expires_at = parse_utc_timestamp(current.upload_expires_at)
    return expires_at > effective_now


def upload_expiry_timestamp(ttl: timedelta) -> str:
    return format_utc_timestamp(utc_now() + ttl)
