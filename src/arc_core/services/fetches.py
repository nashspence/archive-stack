from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import object_session, selectinload

from arc_core.catalog_models import (
    ActivePinRecord,
    CollectionFileRecord,
    FetchEntryRecord,
    FileCopyRecord,
)
from arc_core.domain.enums import FetchState
from arc_core.domain.errors import HashMismatch, InvalidState, NotFound
from arc_core.domain.models import FetchCopyHint, FetchSummary
from arc_core.domain.selectors import parse_target
from arc_core.domain.types import CopyId, FetchId, TargetStr
from arc_core.ports.hot_store import HotStore
from arc_core.ports.upload_store import UploadStore
from arc_core.recovery_payloads import decrypt_recovery_payload, encrypt_recovery_payload
from arc_core.runtime_config import RuntimeConfig
from arc_core.sqlite_db import make_session_factory, session_scope


@dataclass(frozen=True, slots=True)
class _ManifestCopy:
    id: CopyId
    volume_id: str
    location: str
    disc_path: str
    enc: dict[str, object]
    part_index: int | None
    part_count: int | None
    part_bytes: int | None
    part_sha256: str | None

    @property
    def hint(self) -> FetchCopyHint:
        return FetchCopyHint(id=self.id, volume_id=self.volume_id, location=self.location)


class SqlAlchemyFetchService:
    def __init__(
        self, config: RuntimeConfig, hot_store: HotStore, upload_store: UploadStore
    ) -> None:
        self._config = config
        self._hot_store = hot_store
        self._upload_store = upload_store
        self._upload_ttl = config.incomplete_upload_ttl
        self._session_factory = make_session_factory(str(config.sqlite_path))

    def get(self, fetch_id: str) -> FetchSummary:
        with session_scope(self._session_factory) as session:
            pin_record = _get_pin_record(session, fetch_id)
            entries = _ensure_fetch_entries(session, pin_record, self._hot_store)
            _sync_upload_progress(pin_record, entries, self._upload_store)
            _expire_incomplete_uploads(pin_record, entries, self._upload_store)
            return _summary_from_records(pin_record, entries)

    def manifest(self, fetch_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            pin_record = _get_pin_record(session, fetch_id)
            entries = _ensure_fetch_entries(session, pin_record, self._hot_store)
            _sync_upload_progress(pin_record, entries, self._upload_store)
            _expire_incomplete_uploads(pin_record, entries, self._upload_store)
            return {
                "id": pin_record.fetch_id,
                "target": pin_record.target,
                "entries": [
                    _manifest_entry_payload(self._hot_store, session, entry)
                    for entry in entries
                ],
            }

    def create_or_resume_upload(self, fetch_id: str, entry_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            pin_record = _get_pin_record(session, fetch_id)
            entries = _ensure_fetch_entries(session, pin_record, self._hot_store)
            _sync_upload_progress(pin_record, entries, self._upload_store)
            _expire_incomplete_uploads(pin_record, entries, self._upload_store)
            entry = _get_entry(entries, entry_id)

            target_path = f"/.arc/recovery/{fetch_id}/{entry_id}.enc"

            if entry.tus_url is None:
                tus_url = self._upload_store.create_upload(target_path, entry.recovery_bytes)
                entry.tus_url = tus_url
                current_offset = 0
            else:
                current_offset = self._upload_store.get_offset(entry.tus_url)
                if current_offset == -1:
                    try:
                        self._upload_store.read_target(target_path)
                        current_offset = entry.recovery_bytes
                    except Exception:
                        entry.tus_url = None
                        entry.uploaded_bytes = 0
                        tus_url = self._upload_store.create_upload(
                            target_path, entry.recovery_bytes
                        )
                        entry.tus_url = tus_url
                        current_offset = 0

            entry.uploaded_bytes = current_offset
            if _entry_upload_state(entry) != "uploaded":
                entry.upload_expires_at = _upload_expiry_timestamp(self._upload_ttl)

            if pin_record.fetch_state == FetchState.WAITING_MEDIA.value and current_offset > 0:
                pin_record.fetch_state = FetchState.UPLOADING.value

            return {
                "entry": entry.entry_id,
                "protocol": "tus",
                "upload_url": entry.tus_url,
                "offset": current_offset,
                "length": entry.recovery_bytes,
                "checksum_algorithm": "sha256",
                "expires_at": entry.upload_expires_at,
            }

    def complete(self, fetch_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            pin_record = _get_pin_record(session, fetch_id)
            entries = _ensure_fetch_entries(session, pin_record, self._hot_store)
            _sync_upload_progress(pin_record, entries, self._upload_store)
            _expire_incomplete_uploads(pin_record, entries, self._upload_store)

            if any(_entry_upload_state(entry) != "uploaded" for entry in entries):
                raise InvalidState("fetch is missing required entry uploads")

            pin_record.fetch_state = FetchState.VERIFYING.value
            for entry in entries:
                target_path = f"/.arc/recovery/{fetch_id}/{entry.entry_id}.enc"
                encrypted = self._upload_store.read_target(target_path)

                copies = _entry_copies(entry)
                if copies and any(copy.part_index is not None for copy in copies):
                    part_count = max((copy.part_count or 1) for copy in copies)
                    content = self._hot_store.get_collection_file(
                        entry.collection_id, entry.path
                    )
                    parts = _split_plaintext(content, part_count)
                    sizes = [len(encrypt_recovery_payload(part)) for part in parts]
                    offset = 0
                    plaintext_chunks: list[bytes] = []
                    for size in sizes:
                        plaintext_chunks.append(
                            decrypt_recovery_payload(encrypted[offset : offset + size])
                        )
                        offset += size
                    plaintext = b"".join(plaintext_chunks)
                else:
                    plaintext = decrypt_recovery_payload(encrypted)

                if hashlib.sha256(plaintext).hexdigest() != entry.sha256:
                    raise HashMismatch("sha256 did not match")

                self._hot_store.put_collection_file(entry.collection_id, entry.path, plaintext)
                self._upload_store.delete_target(target_path)

                file_record = session.get(
                    CollectionFileRecord,
                    {"collection_id": entry.collection_id, "path": entry.path},
                )
                if file_record is None:
                    raise NotFound(f"file not found for fetch entry: {entry.path}")
                file_record.hot = True

            pin_record.fetch_state = FetchState.DONE.value
            return {
                "id": pin_record.fetch_id,
                "state": pin_record.fetch_state,
                "hot": _hot_payload(session, pin_record.target),
            }


def _get_pin_record(session, fetch_id: str) -> ActivePinRecord:
    pin_record = session.scalar(
        select(ActivePinRecord).where(ActivePinRecord.fetch_id == fetch_id)
    )
    if pin_record is None:
        raise NotFound(f"fetch not found: {fetch_id}")
    return pin_record


def _selected_files(session, raw_target: str) -> list[CollectionFileRecord]:
    target = parse_target(raw_target)
    records = session.scalars(
        select(CollectionFileRecord).options(selectinload(CollectionFileRecord.copies))
    ).all()
    selected = [
        record
        for record in records
        if (
            f"{record.collection_id}/{record.path}".startswith(target.canonical)
            if target.is_dir
            else f"{record.collection_id}/{record.path}" == target.canonical
        )
    ]
    if not selected:
        raise NotFound(f"target not found: {raw_target}")
    return selected


def _ensure_fetch_entries(
    session,
    pin_record: ActivePinRecord,
    hot_store: HotStore,
) -> list[FetchEntryRecord]:
    existing = session.scalars(
        select(FetchEntryRecord)
        .where(FetchEntryRecord.fetch_id == pin_record.fetch_id)
        .order_by(FetchEntryRecord.entry_order)
    ).all()
    if existing:
        return existing

    selected = _selected_files(session, pin_record.target)
    created: list[FetchEntryRecord] = []
    for index, file_record in enumerate(
        sorted(selected, key=lambda item: (item.collection_id, item.path)), start=1
    ):
        content = hot_store.get_collection_file(file_record.collection_id, file_record.path)
        copy_records = session.scalars(
            select(FileCopyRecord).where(
                FileCopyRecord.collection_id == file_record.collection_id,
                FileCopyRecord.path == file_record.path,
            )
        ).all()
        if not copy_records or all(r.part_index is None for r in copy_records):
            payloads: tuple[bytes, ...] = (encrypt_recovery_payload(content),)
        else:
            part_count = max((r.part_count or 1) for r in copy_records)
            payloads = tuple(
                encrypt_recovery_payload(part)
                for part in _split_plaintext(content, part_count)
            )
        recovery_bytes = sum(len(p) for p in payloads)

        entry = FetchEntryRecord(
            fetch_id=pin_record.fetch_id,
            entry_id=f"e{index}",
            entry_order=index,
            collection_id=file_record.collection_id,
            path=file_record.path,
            bytes=file_record.bytes,
            sha256=file_record.sha256,
            recovery_bytes=recovery_bytes,
            uploaded_bytes=0,
            upload_expires_at=None,
            tus_url=None,
        )
        session.add(entry)
        created.append(entry)
    session.flush()
    return created


def _summary_from_records(
    pin_record: ActivePinRecord,
    entries: list[FetchEntryRecord],
) -> FetchSummary:
    entries_total = len(entries)
    entries_pending = sum(1 for entry in entries if _entry_upload_state(entry) == "pending")
    entries_partial = sum(1 for entry in entries if _entry_upload_state(entry) == "partial")
    entries_uploaded = sum(1 for entry in entries if _entry_upload_state(entry) == "uploaded")
    uploaded_bytes = sum(entry.uploaded_bytes for entry in entries)
    missing_bytes = max(sum(_entry_recovery_bytes(entry) for entry in entries) - uploaded_bytes, 0)
    expiries = [entry.upload_expires_at for entry in entries if entry.upload_expires_at is not None]

    return FetchSummary(
        id=FetchId(pin_record.fetch_id),
        target=TargetStr(pin_record.target),
        state=FetchState(pin_record.fetch_state),
        files=len(entries),
        bytes=sum(entry.bytes for entry in entries),
        copies=_summary_copies(entries),
        entries_total=entries_total,
        entries_pending=entries_pending,
        entries_partial=entries_partial,
        entries_uploaded=entries_uploaded,
        uploaded_bytes=uploaded_bytes,
        missing_bytes=missing_bytes,
        upload_state_expires_at=max(expiries) if expiries else None,
    )


def _summary_copies(entries: list[FetchEntryRecord]) -> list[FetchCopyHint]:
    seen: set[tuple[str, str]] = set()
    copies: list[FetchCopyHint] = []
    for entry in entries:
        for copy in _entry_copies(entry):
            key = (copy.volume_id, str(copy.id))
            if key in seen:
                continue
            seen.add(key)
            copies.append(copy.hint)
    return copies


def _manifest_entry_payload(
    hot_store: HotStore, session, entry: FetchEntryRecord
) -> dict[str, object]:
    return {
        "id": entry.entry_id,
        "path": entry.path,
        "bytes": entry.bytes,
        "sha256": entry.sha256,
        "recovery_bytes": _entry_recovery_bytes(entry),
        "upload_state": _entry_upload_state(entry),
        "uploaded_bytes": entry.uploaded_bytes,
        "upload_state_expires_at": entry.upload_expires_at,
        "copies": [
            _manifest_copy_payload(hot_store, session, entry, copy)
            for copy in _entry_copies(entry)
        ],
        "parts": _manifest_parts_payload(hot_store, session, entry),
    }


def _manifest_parts_payload(
    hot_store: HotStore, session, entry: FetchEntryRecord
) -> list[dict[str, object]]:
    copies = _entry_copies(entry)
    if not copies:
        return []

    if all(copy.part_index is None for copy in copies):
        return [
            {
                "index": 0,
                "bytes": entry.bytes,
                "sha256": entry.sha256,
                "recovery_bytes": _entry_recovery_bytes(entry),
                "copies": [
                    _manifest_copy_payload(hot_store, session, entry, copy) for copy in copies
                ],
            }
        ]

    part_count = max((copy.part_count or 1) for copy in copies)
    parts: list[dict[str, object]] = []
    for part_index in range(part_count):
        part_copies = [copy for copy in copies if copy.part_index == part_index]
        if not part_copies:
            raise NotFound(f"missing copy hints for part {part_index} of entry {entry.entry_id}")
        bytes_hint = part_copies[0].part_bytes
        sha256_hint = part_copies[0].part_sha256
        if bytes_hint is None or sha256_hint is None:
            raise NotFound(f"missing part metadata for part {part_index} of entry {entry.entry_id}")
        parts.append(
            {
                "index": part_index,
                "bytes": bytes_hint,
                "sha256": sha256_hint,
                "recovery_bytes": len(
                    _copy_recovery_payload(hot_store, session, entry, part_copies[0])
                ),
                "copies": [
                    _manifest_copy_payload(hot_store, session, entry, copy)
                    for copy in part_copies
                ],
            }
        )
    return parts


def _manifest_copy_payload(
    hot_store: HotStore, session, entry: FetchEntryRecord, copy: _ManifestCopy
) -> dict[str, object]:
    recovery_payload = _copy_recovery_payload(hot_store, session, entry, copy)
    return {
        "copy": str(copy.id),
        "volume_id": copy.volume_id,
        "location": copy.location,
        "disc_path": copy.disc_path,
        "recovery_bytes": len(recovery_payload),
        "recovery_sha256": hashlib.sha256(recovery_payload).hexdigest(),
    }


def _entry_copies(entry: FetchEntryRecord) -> list[_ManifestCopy]:
    session = object_session(entry)
    if session is None:
        raise RuntimeError("fetch entry is not bound to a session")
    copy_records = session.scalars(
        select(FileCopyRecord)
        .where(
            FileCopyRecord.collection_id == entry.collection_id,
            FileCopyRecord.path == entry.path,
        )
        .order_by(
            FileCopyRecord.part_index.is_(None),
            FileCopyRecord.part_index,
            FileCopyRecord.volume_id,
            FileCopyRecord.copy_id,
            FileCopyRecord.location,
        )
    ).all()
    return [
        _ManifestCopy(
            id=CopyId(record.copy_id),
            volume_id=record.volume_id,
            location=record.location,
            disc_path=record.disc_path,
            enc=json.loads(record.enc_json),
            part_index=record.part_index,
            part_count=record.part_count,
            part_bytes=record.part_bytes,
            part_sha256=record.part_sha256,
        )
        for record in copy_records
    ]


def _entry_recovery_payloads(
    hot_store: HotStore, session, entry: FetchEntryRecord
) -> tuple[bytes, ...]:
    content = hot_store.get_collection_file(entry.collection_id, entry.path)
    copies = _entry_copies(entry)
    if not copies or all(copy.part_index is None for copy in copies):
        return (encrypt_recovery_payload(content),)
    part_count = max((copy.part_count or 1) for copy in copies)
    return tuple(
        encrypt_recovery_payload(part) for part in _split_plaintext(content, part_count)
    )


def _copy_recovery_payload(
    hot_store: HotStore, session, entry: FetchEntryRecord, copy: _ManifestCopy
) -> bytes:
    payloads = _entry_recovery_payloads(hot_store, session, entry)
    if copy.part_index is None:
        return payloads[0]
    return payloads[copy.part_index]


def _entry_upload_state(entry: FetchEntryRecord) -> str:
    if entry.recovery_bytes > 0 and entry.uploaded_bytes >= entry.recovery_bytes:
        return "uploaded"
    if entry.uploaded_bytes > 0:
        return "partial"
    return "pending"


def _entry_recovery_bytes(entry: FetchEntryRecord) -> int:
    return entry.recovery_bytes


def _expire_incomplete_uploads(
    pin_record: ActivePinRecord,
    entries: list[FetchEntryRecord],
    upload_store: UploadStore,
) -> None:
    now = _utc_now()
    expired = False
    for entry in entries:
        if entry.upload_expires_at is None:
            continue
        if datetime.fromisoformat(entry.upload_expires_at.replace("Z", "+00:00")) > now:
            continue
        if entry.tus_url is not None:
            target_path = f"/.arc/recovery/{entry.fetch_id}/{entry.entry_id}.enc"
            offset = upload_store.get_offset(entry.tus_url)
            if offset == -1:
                try:
                    upload_store.read_target(target_path)
                except Exception:
                    entry.tus_url = None
                    entry.uploaded_bytes = 0
                    entry.upload_expires_at = None
                    expired = True
                    continue
            upload_store.cancel_upload(entry.tus_url)
            upload_store.delete_target(target_path)
            entry.tus_url = None
        entry.uploaded_bytes = 0
        entry.upload_expires_at = None
        expired = True
    if expired and pin_record.fetch_state == FetchState.UPLOADING.value:
        pin_record.fetch_state = FetchState.WAITING_MEDIA.value


def _sync_upload_progress(
    pin_record: ActivePinRecord,
    entries: list[FetchEntryRecord],
    upload_store: UploadStore,
) -> None:
    any_uploaded = False
    for entry in entries:
        if entry.tus_url is None:
            continue
        offset = upload_store.get_offset(entry.tus_url)
        if offset == -1:
            target_path = f"/.arc/recovery/{entry.fetch_id}/{entry.entry_id}.enc"
            try:
                upload_store.read_target(target_path)
            except Exception:
                entry.uploaded_bytes = 0
                continue
            offset = entry.recovery_bytes
        entry.uploaded_bytes = offset
        if _entry_upload_state(entry) == "uploaded":
            entry.upload_expires_at = None
        if entry.uploaded_bytes > 0:
            any_uploaded = True
    if any_uploaded and pin_record.fetch_state == FetchState.WAITING_MEDIA.value:
        pin_record.fetch_state = FetchState.UPLOADING.value


def _get_entry(entries: list[FetchEntryRecord], entry_id: str) -> FetchEntryRecord:
    for entry in entries:
        if entry.entry_id == entry_id:
            return entry
    raise NotFound(f"entry not found: {entry_id}")


def _hot_payload(session, raw_target: str) -> dict[str, object]:
    selected = _selected_files(session, raw_target)
    present_bytes = sum(record.bytes for record in selected if record.hot)
    missing_bytes = sum(record.bytes for record in selected if not record.hot)
    return {
        "state": "ready" if missing_bytes == 0 else "waiting",
        "present_bytes": present_bytes,
        "missing_bytes": missing_bytes,
    }


def _split_plaintext(content: bytes, piece_count: int) -> tuple[bytes, ...]:
    if piece_count < 1:
        raise ValueError("piece_count must be at least 1")

    base, remainder = divmod(len(content), piece_count)
    offset = 0
    out: list[bytes] = []
    for part_index in range(piece_count):
        size = base + int(part_index < remainder)
        out.append(content[offset : offset + size])
        offset += size
    return tuple(out)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _upload_expiry_timestamp(ttl: timedelta) -> str:
    return (
        (_utc_now() + ttl)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def delete_fetch_entries(session, fetch_id: str) -> None:
    session.execute(delete(FetchEntryRecord).where(FetchEntryRecord.fetch_id == fetch_id))
