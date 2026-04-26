from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from arc_core.ports.upload_store import UploadStore


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
) -> UploadLifecycleState:
    if current.tus_url is None:
        return current

    offset = upload_store.get_offset(current.tus_url)
    if offset == -1:
        try:
            upload_store.read_target(target_path)
        except Exception:
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
    expires_at = datetime.fromisoformat(current.upload_expires_at.replace("Z", "+00:00"))
    if expires_at > effective_now:
        return current, False

    if current.tus_url is not None:
        offset = upload_store.get_offset(current.tus_url)
        if offset == -1:
            try:
                upload_store.read_target(target_path)
            except Exception:
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


def utc_now() -> datetime:
    return datetime.now(UTC)


def upload_expiry_timestamp(ttl: timedelta) -> str:
    return (utc_now() + ttl).replace(microsecond=0).isoformat().replace("+00:00", "Z")
