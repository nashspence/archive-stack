from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from riverhog_protocol.errors import NotFound, ServiceUnavailable
from time_formats import format_utc_timestamp, parse_utc_timestamp, utc_now

from riverhog_core.ports.upload_store import UploadStore


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
        if not force or _upload_target_exists(upload_store, target_path):
            return current
        return _reset_upload_state(
            current=current,
            target_path=target_path,
            upload_store=upload_store,
        )
    if not force and _upload_state_is_live(current=current, length=length):
        return current

    offset = upload_store.get_offset(current.tus_url)
    target_confirmed = False
    if offset == -1:
        if not _upload_target_exists(upload_store, target_path):
            # Another worker may have consumed and deleted the backing upload between
            # loading the row and syncing the current offset. Preserve the current
            # lifecycle state during observational syncs so a stale read cannot roll
            # committed progress back. A forced create-or-resume request is authoritative:
            # replace a lease when neither its upload nor its finalized target exists.
            if not force:
                return current
            return _reset_upload_state(
                current=current,
                target_path=target_path,
                upload_store=upload_store,
            )
        offset = length
        target_confirmed = True

    if (
        upload_state_name(uploaded_bytes=offset, length=length) == "uploaded"
        and not target_confirmed
        and not _upload_target_exists(upload_store, target_path)
    ):
        raise ServiceUnavailable(
            "upload backend accepted all bytes but has not exposed the finalized target; retry"
        )

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
        for _ in upload_store.iter_target(target_path, size=1):
            break
    except NotFound:
        return False
    return True


def _reset_upload_state(
    *,
    current: UploadLifecycleState,
    target_path: str,
    upload_store: UploadStore,
) -> UploadLifecycleState:
    if current.tus_url is not None:
        upload_store.cancel_upload(current.tus_url)
    upload_store.delete_target(target_path)
    return UploadLifecycleState(
        tus_url=None,
        uploaded_bytes=0,
        upload_expires_at=None,
    )


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
