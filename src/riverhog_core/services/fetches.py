from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, object_session, selectinload

from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ActivePinRecord,
    CollectionFileRecord,
    FetchEntryRecord,
    FileCopyRecord,
    FinalizedImageRecord,
)
from riverhog_core.domain.enums import FetchState
from riverhog_core.domain.errors import Conflict, HashMismatch, InvalidState, NotFound
from riverhog_core.domain.models import FetchCopyHint, FetchSummary
from riverhog_core.domain.selectors import parse_target
from riverhog_core.domain.types import CopyId, FetchId, TargetStr
from riverhog_core.ports.hot_store import HotStore
from riverhog_core.ports.upload_store import UploadStore
from riverhog_core.recovery_payloads import (
    CommandAgeBatchpassRecoveryPayloadCodec,
    RecoveryPayloadCodec,
    RecoveryPayloadError,
    decrypt_recovery_payload,
    encrypt_recovery_payload,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.copy_recovery_metadata import (
    CopyRecoveryMetadata,
    read_copy_recovery_metadata,
)
from riverhog_core.services.resumable_uploads import (
    UploadLifecycleState,
    create_or_resume_upload_state,
    expire_upload_state,
    sync_upload_state,
    upload_expiry_timestamp,
)


def _read_collection_file_content(
    hot_store: HotStore,
    collection_id: str,
    path: str,
) -> bytes:
    try:
        return hot_store.get_collection_file(collection_id, path)
    except FileNotFoundError as exc:
        raise NotFound(f"file not found in hot store: {collection_id}/{path}") from exc


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
    recovery_bytes: int | None
    recovery_sha256: str | None

    @property
    def hint(self) -> FetchCopyHint:
        return FetchCopyHint(id=self.id, volume_id=self.volume_id, location=self.location)


class SqlAlchemyFetchService:
    def __init__(
        self,
        config: RuntimeConfig,
        hot_store: HotStore,
        upload_store: UploadStore,
        recovery_payload_codec: RecoveryPayloadCodec | None = None,
    ) -> None:
        self._config = config
        self._hot_store = hot_store
        self._upload_store = upload_store
        self._recovery_payload_codec = (
            recovery_payload_codec
            or CommandAgeBatchpassRecoveryPayloadCodec(
                command=config.recovery_payload_command,
                passphrase=config.recovery_payload_passphrase,
                work_factor=config.recovery_payload_work_factor,
                max_work_factor=config.recovery_payload_max_work_factor,
            )
        )
        self._upload_ttl = config.incomplete_upload_ttl
        self._session_factory = make_session_factory(config.database_url)

    def get(self, fetch_id: str) -> FetchSummary:
        with session_scope(self._session_factory) as session:
            pin_record = _get_pin_record(session, fetch_id)
            entries = _ensure_fetch_entries(
                session,
                pin_record,
                self._hot_store,
                self._recovery_payload_codec,
            )
            _sync_upload_progress(pin_record, entries, self._upload_store)
            _expire_incomplete_uploads(pin_record, entries, self._upload_store)
            return _summary_from_records(pin_record, entries)

    def manifest(self, fetch_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            pin_record = _get_pin_record(session, fetch_id)
            entries = _ensure_fetch_entries(
                session,
                pin_record,
                self._hot_store,
                self._recovery_payload_codec,
            )
            _sync_upload_progress(pin_record, entries, self._upload_store)
            _expire_incomplete_uploads(pin_record, entries, self._upload_store)
            return {
                "id": pin_record.fetch_id,
                "target": pin_record.target,
                "entries": [
                    _manifest_entry_payload(
                        entry,
                        recovery_payload_codec=self._recovery_payload_codec,
                        fetch_state=FetchState(pin_record.fetch_state),
                    )
                    for entry in entries
                ],
            }

    def create_or_resume_upload(self, fetch_id: str, entry_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            pin_record = _get_pin_record(session, fetch_id)
            if pin_record.fetch_state == FetchState.DONE.value:
                raise InvalidState("fetch is already complete")
            entries = _ensure_fetch_entries(
                session,
                pin_record,
                self._hot_store,
                self._recovery_payload_codec,
            )
            _sync_upload_progress(pin_record, entries, self._upload_store)
            _expire_incomplete_uploads(pin_record, entries, self._upload_store)
            entry = _get_entry(entries, entry_id)

            target_path = _entry_upload_target_path(entry)
            updated, tus_url = create_or_resume_upload_state(
                current=_entry_upload_lifecycle_state(entry),
                target_path=target_path,
                length=entry.recovery_bytes,
                upload_store=self._upload_store,
                ttl=self._upload_ttl,
            )
            _apply_entry_upload_lifecycle_state(entry, updated)

            if (
                pin_record.fetch_state == FetchState.WAITING_MEDIA.value
                and entry.uploaded_bytes > 0
            ):
                pin_record.fetch_state = FetchState.UPLOADING.value

            return _entry_upload_payload(entry)

    def append_upload_chunk(
        self,
        fetch_id: str,
        entry_id: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            pin_record = _get_pin_record(session, fetch_id)
            entries = _ensure_fetch_entries(
                session,
                pin_record,
                self._hot_store,
                self._recovery_payload_codec,
            )
            entry = _get_entry(entries, entry_id)
            _expire_incomplete_upload_for_entry(pin_record, entry, self._upload_store)

            if entry.tus_url is None:
                raise Conflict(f"fetch entry upload is not resumable: {entry_id}")
            if offset != entry.uploaded_bytes:
                raise Conflict(
                    f"fetch entry upload offset for {entry_id} is "
                    f"{offset}, expected {entry.uploaded_bytes}"
                )

            next_offset, _ = self._upload_store.append_upload_chunk(
                entry.tus_url,
                offset=offset,
                checksum=checksum,
                content=content,
            )
            entry.uploaded_bytes = next_offset
            if next_offset >= entry.recovery_bytes:
                entry.upload_expires_at = None
            else:
                entry.upload_expires_at = upload_expiry_timestamp(self._upload_ttl)

            if pin_record.fetch_state == FetchState.WAITING_MEDIA.value:
                pin_record.fetch_state = FetchState.UPLOADING.value

            return {
                "offset": entry.uploaded_bytes,
                "length": entry.recovery_bytes,
                "expires_at": entry.upload_expires_at,
            }

    def get_entry_upload(self, fetch_id: str, entry_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            pin_record = _get_pin_record(session, fetch_id)
            entries = _ensure_fetch_entries(
                session,
                pin_record,
                self._hot_store,
                self._recovery_payload_codec,
            )
            _sync_upload_progress(pin_record, entries, self._upload_store)
            _expire_incomplete_uploads(pin_record, entries, self._upload_store)
            entry = _get_entry(entries, entry_id)

            if entry.tus_url is None:
                raise NotFound(f"fetch entry upload is not resumable: {entry_id}")
            return _entry_upload_payload(entry)

    def cancel_entry_upload(self, fetch_id: str, entry_id: str) -> None:
        with session_scope(self._session_factory) as session:
            pin_record = _get_pin_record(session, fetch_id)
            entries = _ensure_fetch_entries(
                session,
                pin_record,
                self._hot_store,
                self._recovery_payload_codec,
            )
            _sync_upload_progress(pin_record, entries, self._upload_store)
            _expire_incomplete_uploads(pin_record, entries, self._upload_store)
            entry = _get_entry(entries, entry_id)

            if entry.tus_url is None:
                raise NotFound(f"fetch entry upload is not resumable: {entry_id}")

            self._upload_store.cancel_upload(entry.tus_url)
            self._upload_store.delete_target(_entry_upload_target_path(entry))
            _apply_entry_upload_lifecycle_state(
                entry,
                UploadLifecycleState(
                    tus_url=None,
                    uploaded_bytes=0,
                    upload_expires_at=None,
                ),
            )
            if pin_record.fetch_state == FetchState.UPLOADING.value:
                pin_record.fetch_state = FetchState.WAITING_MEDIA.value

    def expire_stale_uploads(self) -> None:
        with session_scope(self._session_factory) as session:
            pin_records = session.scalars(
                select(ActivePinRecord).where(ActivePinRecord.fetch_id.is_not(None))
            ).all()
            for pin_record in pin_records:
                entries = list(
                    session.scalars(
                        select(FetchEntryRecord)
                        .where(FetchEntryRecord.fetch_id == pin_record.fetch_id)
                        .order_by(FetchEntryRecord.entry_order)
                    ).all()
                )
                if not entries:
                    continue
                _sync_upload_progress(pin_record, entries, self._upload_store)
                _expire_incomplete_uploads(pin_record, entries, self._upload_store)

    def complete(self, fetch_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            pin_record = _get_pin_record(session, fetch_id)
            entries = _ensure_fetch_entries(
                session,
                pin_record,
                self._hot_store,
                self._recovery_payload_codec,
            )
            _sync_upload_progress(pin_record, entries, self._upload_store)
            _expire_incomplete_uploads(pin_record, entries, self._upload_store)

            if pin_record.fetch_state == FetchState.DONE.value:
                return {
                    "id": pin_record.fetch_id,
                    "state": pin_record.fetch_state,
                    "hot": _hot_payload(session, pin_record.target),
                }

            if any(
                _entry_upload_state(entry, fetch_state=FetchState(pin_record.fetch_state))
                != "byte_complete"
                for entry in entries
            ):
                raise InvalidState("fetch is missing required entry uploads")

            pin_record.fetch_state = FetchState.VERIFYING.value
            for entry in entries:
                target_path = _entry_upload_target_path(entry)
                encrypted = self._upload_store.read_target(target_path)

                copies = _entry_copies(entry)
                try:
                    if copies and any(copy.part_index is not None for copy in copies):
                        copies = _entry_copies(
                            entry,
                            recovery_payload_codec=self._recovery_payload_codec,
                            require_recovery_metadata=True,
                        )
                        part_copies = _ordered_entry_part_copies(entry, copies)
                        offset = 0
                        plaintext_chunks: list[bytes] = []
                        for copy in part_copies:
                            size = _copy_recovery_bytes(copy)
                            plaintext_chunks.append(
                                decrypt_recovery_payload(
                                    encrypted[offset : offset + size],
                                    self._recovery_payload_codec,
                                )
                            )
                            offset += size
                        if offset != len(encrypted):
                            raise HashMismatch("uploaded recovery bytes have trailing data")
                        plaintext = b"".join(plaintext_chunks)
                    else:
                        plaintext = decrypt_recovery_payload(
                            encrypted,
                            self._recovery_payload_codec,
                        )
                except RecoveryPayloadError as exc:
                    raise HashMismatch("uploaded recovery bytes did not decrypt cleanly") from exc

                if hashlib.sha256(plaintext).hexdigest() != entry.sha256:
                    raise HashMismatch("sha256 did not match")

                self._hot_store.put_collection_file(entry.collection_id, entry.path, plaintext)
                if entry.tus_url is not None:
                    self._upload_store.cancel_upload(entry.tus_url)
                self._upload_store.delete_target(target_path)
                _apply_entry_upload_lifecycle_state(
                    entry,
                    UploadLifecycleState(
                        tus_url=None,
                        uploaded_bytes=entry.recovery_bytes,
                        upload_expires_at=None,
                    ),
                )

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


def _get_pin_record(session: Session, fetch_id: str) -> ActivePinRecord:
    pin_record = session.scalar(select(ActivePinRecord).where(ActivePinRecord.fetch_id == fetch_id))
    if pin_record is None:
        raise NotFound(f"fetch not found: {fetch_id}")
    return pin_record


def _selected_files(session: Session, raw_target: str) -> list[CollectionFileRecord]:
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
    session: Session,
    pin_record: ActivePinRecord,
    hot_store: HotStore,
    recovery_payload_codec: RecoveryPayloadCodec,
) -> list[FetchEntryRecord]:
    existing = list(
        session.scalars(
            select(FetchEntryRecord)
            .where(FetchEntryRecord.fetch_id == pin_record.fetch_id)
            .order_by(FetchEntryRecord.entry_order)
        ).all()
    )
    if existing:
        return existing

    selected = _selected_files(session, pin_record.target)
    created: list[FetchEntryRecord] = []
    for index, file_record in enumerate(
        sorted(selected, key=lambda item: (item.collection_id, item.path)), start=1
    ):
        copy_records = _copy_records_for_file(
            session,
            file_record.collection_id,
            file_record.path,
            recovery_payload_codec,
            require_recovery_metadata=True,
        )
        if copy_records:
            recovery_bytes = _file_recovery_bytes_from_copies(file_record, copy_records)
        else:
            content = _read_collection_file_content(
                hot_store,
                file_record.collection_id,
                file_record.path,
            )
            payloads: tuple[bytes, ...] = (
                encrypt_recovery_payload(content, recovery_payload_codec),
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
    fetch_state = FetchState(pin_record.fetch_state)
    entries_total = len(entries)
    entries_pending = sum(
        1 for entry in entries if _entry_upload_state(entry, fetch_state=fetch_state) == "pending"
    )
    entries_partial = sum(
        1 for entry in entries if _entry_upload_state(entry, fetch_state=fetch_state) == "partial"
    )
    entries_byte_complete = sum(
        1
        for entry in entries
        if _entry_upload_state(entry, fetch_state=fetch_state) == "byte_complete"
    )
    entries_uploaded = sum(
        1 for entry in entries if _entry_upload_state(entry, fetch_state=fetch_state) == "uploaded"
    )
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
        entries_byte_complete=entries_byte_complete,
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
    entry: FetchEntryRecord,
    *,
    recovery_payload_codec: RecoveryPayloadCodec,
    fetch_state: FetchState,
) -> dict[str, object]:
    copies = _entry_copies(
        entry,
        recovery_payload_codec=recovery_payload_codec,
        require_recovery_metadata=True,
    )
    return {
        "id": entry.entry_id,
        "path": entry.path,
        "bytes": entry.bytes,
        "sha256": entry.sha256,
        "recovery_bytes": _entry_recovery_bytes(entry),
        "upload_state": _entry_upload_state(entry, fetch_state=fetch_state),
        "uploaded_bytes": entry.uploaded_bytes,
        "upload_state_expires_at": entry.upload_expires_at,
        "copies": [_manifest_copy_payload(copy) for copy in copies],
        "parts": _manifest_parts_payload(entry, copies),
    }


def _manifest_parts_payload(
    entry: FetchEntryRecord,
    copies: list[_ManifestCopy],
) -> list[dict[str, object]]:
    if not copies:
        return []

    if all(copy.part_index is None for copy in copies):
        return [
            {
                "index": 0,
                "bytes": entry.bytes,
                "sha256": entry.sha256,
                "recovery_bytes": _entry_recovery_bytes(entry),
                "copies": [_manifest_copy_payload(copy) for copy in copies],
            }
        ]

    if any(copy.part_index is None for copy in copies):
        raise InvalidState(f"mixed whole-file and multipart copy hints for {entry.entry_id}")

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
                "recovery_bytes": _copy_recovery_bytes(part_copies[0]),
                "copies": [_manifest_copy_payload(copy) for copy in part_copies],
            }
        )
    return parts


def _ordered_entry_part_copies(
    entry: FetchEntryRecord,
    copies: list[_ManifestCopy],
) -> list[_ManifestCopy]:
    if any(copy.part_index is None for copy in copies):
        raise InvalidState(f"mixed whole-file and multipart copy hints for {entry.entry_id}")
    part_count = max((copy.part_count or 1) for copy in copies)
    selected: list[_ManifestCopy] = []
    for part_index in range(part_count):
        part_copies = [copy for copy in copies if copy.part_index == part_index]
        if not part_copies:
            raise NotFound(f"missing copy hints for part {part_index} of entry {entry.entry_id}")
        selected.append(part_copies[0])
    return selected


def _manifest_copy_payload(copy: _ManifestCopy) -> dict[str, object]:
    return {
        "copy": str(copy.id),
        "volume_id": copy.volume_id,
        "location": copy.location,
        "disc_path": copy.disc_path,
        "recovery_bytes": _copy_recovery_bytes(copy),
        "recovery_sha256": _copy_recovery_sha256(copy),
    }


def _entry_copies(
    entry: FetchEntryRecord,
    *,
    recovery_payload_codec: RecoveryPayloadCodec | None = None,
    require_recovery_metadata: bool = False,
) -> list[_ManifestCopy]:
    session = object_session(entry)
    if session is None:
        raise RuntimeError("fetch entry is not bound to a session")
    copy_records = _copy_records_for_file(
        session,
        entry.collection_id,
        entry.path,
        recovery_payload_codec,
        require_recovery_metadata=require_recovery_metadata,
    )
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
            recovery_bytes=record.recovery_bytes,
            recovery_sha256=record.recovery_sha256,
        )
        for record in copy_records
    ]


def _copy_records_for_file(
    session: Session,
    collection_id: str,
    path: str,
    recovery_payload_codec: RecoveryPayloadCodec | None,
    *,
    require_recovery_metadata: bool,
) -> list[FileCopyRecord]:
    copy_records = session.scalars(
        select(FileCopyRecord)
        .where(
            FileCopyRecord.collection_id == collection_id,
            FileCopyRecord.path == path,
        )
        .order_by(
            FileCopyRecord.part_index.is_(None),
            FileCopyRecord.part_index,
            FileCopyRecord.volume_id,
            FileCopyRecord.copy_id,
            FileCopyRecord.location,
        )
    ).all()
    if require_recovery_metadata:
        if recovery_payload_codec is None:
            raise RuntimeError("recovery payload codec is required to backfill copy metadata")
        for record in copy_records:
            _ensure_file_copy_recovery_metadata(session, record, recovery_payload_codec)
    return list(copy_records)


def _file_recovery_bytes_from_copies(
    file_record: CollectionFileRecord,
    copy_records: list[FileCopyRecord],
) -> int:
    if not copy_records:
        return 0

    if all(record.part_index is None for record in copy_records):
        return _record_recovery_bytes(copy_records[0])

    if any(record.part_index is None for record in copy_records):
        raise InvalidState(f"mixed whole-file and multipart copy hints for {file_record.path}")

    part_count = max(record.part_count or 1 for record in copy_records)
    total = 0
    for part_index in range(part_count):
        candidates = [record for record in copy_records if record.part_index == part_index]
        if not candidates:
            raise NotFound(f"missing copy hints for part {part_index} of {file_record.path}")
        total += _record_recovery_bytes(candidates[0])
    return total


def _ensure_file_copy_recovery_metadata(
    session: Session,
    record: FileCopyRecord,
    recovery_payload_codec: RecoveryPayloadCodec,
) -> None:
    needs_recovery_metadata = record.recovery_bytes is None or not record.recovery_sha256
    needs_part_metadata = record.part_index is not None and (
        record.part_bytes is None or record.part_sha256 is None
    )
    if not needs_recovery_metadata and not needs_part_metadata:
        return

    image = session.get(FinalizedImageRecord, record.volume_id)
    if image is None:
        raise InvalidState(f"finalized image not found for copy metadata: {record.volume_id}")

    metadata = _read_copy_recovery_metadata(
        image.image_root,
        record.disc_path,
        recovery_payload_codec,
        include_plaintext=needs_part_metadata,
    )
    record.recovery_bytes = metadata.recovery_bytes
    record.recovery_sha256 = metadata.recovery_sha256
    if record.part_index is not None:
        record.part_bytes = metadata.plaintext_bytes
        record.part_sha256 = metadata.plaintext_sha256


def _read_copy_recovery_metadata(
    image_root: str,
    disc_path: str,
    recovery_payload_codec: RecoveryPayloadCodec,
    *,
    include_plaintext: bool,
) -> CopyRecoveryMetadata:
    try:
        return read_copy_recovery_metadata(
            image_root,
            disc_path,
            recovery_payload_codec,
            include_plaintext=include_plaintext,
        )
    except FileNotFoundError as exc:
        missing_path = Path(image_root) / disc_path.lstrip("/")
        raise InvalidState(f"finalized-image payload is missing: {missing_path}") from exc
    except RecoveryPayloadError as exc:
        raise InvalidState(f"finalized-image payload could not be decrypted: {disc_path}") from exc


def _record_recovery_bytes(record: FileCopyRecord) -> int:
    if record.recovery_bytes is None:
        raise InvalidState(f"missing recovery byte count for copy: {record.copy_id}")
    return record.recovery_bytes


def _copy_recovery_bytes(copy: _ManifestCopy) -> int:
    if copy.recovery_bytes is None:
        raise InvalidState(f"missing recovery byte count for copy: {copy.id}")
    return copy.recovery_bytes


def _copy_recovery_sha256(copy: _ManifestCopy) -> str:
    if not copy.recovery_sha256:
        raise InvalidState(f"missing recovery sha256 for copy: {copy.id}")
    return copy.recovery_sha256


def _entry_upload_state(entry: FetchEntryRecord, *, fetch_state: FetchState) -> str:
    if entry.recovery_bytes > 0 and entry.uploaded_bytes >= entry.recovery_bytes:
        if fetch_state == FetchState.DONE:
            return "uploaded"
        return "byte_complete"
    if entry.uploaded_bytes > 0:
        return "partial"
    return "pending"


def _entry_recovery_bytes(entry: FetchEntryRecord) -> int:
    return entry.recovery_bytes


def _entry_upload_lifecycle_state(entry: FetchEntryRecord) -> UploadLifecycleState:
    return UploadLifecycleState(
        tus_url=entry.tus_url,
        uploaded_bytes=entry.uploaded_bytes,
        upload_expires_at=entry.upload_expires_at,
    )


def _apply_entry_upload_lifecycle_state(
    entry: FetchEntryRecord, state: UploadLifecycleState
) -> None:
    entry.tus_url = state.tus_url
    entry.uploaded_bytes = state.uploaded_bytes
    entry.upload_expires_at = state.upload_expires_at


def _expire_incomplete_uploads(
    pin_record: ActivePinRecord,
    entries: list[FetchEntryRecord],
    upload_store: UploadStore,
) -> None:
    expired = False
    for entry in entries:
        target_path = _entry_upload_target_path(entry)
        updated, did_expire = expire_upload_state(
            current=_entry_upload_lifecycle_state(entry),
            target_path=target_path,
            upload_store=upload_store,
        )
        _apply_entry_upload_lifecycle_state(entry, updated)
        if not did_expire:
            continue
        expired = True
    if expired and pin_record.fetch_state == FetchState.UPLOADING.value:
        pin_record.fetch_state = FetchState.WAITING_MEDIA.value


def _expire_incomplete_upload_for_entry(
    pin_record: ActivePinRecord,
    entry: FetchEntryRecord,
    upload_store: UploadStore,
) -> None:
    target_path = _entry_upload_target_path(entry)
    updated, did_expire = expire_upload_state(
        current=_entry_upload_lifecycle_state(entry),
        target_path=target_path,
        upload_store=upload_store,
    )
    _apply_entry_upload_lifecycle_state(entry, updated)
    if did_expire and pin_record.fetch_state == FetchState.UPLOADING.value:
        pin_record.fetch_state = FetchState.WAITING_MEDIA.value


def _sync_upload_progress(
    pin_record: ActivePinRecord,
    entries: list[FetchEntryRecord],
    upload_store: UploadStore,
) -> None:
    any_uploaded = False
    for entry in entries:
        target_path = _entry_upload_target_path(entry)
        updated = sync_upload_state(
            current=_entry_upload_lifecycle_state(entry),
            target_path=target_path,
            length=entry.recovery_bytes,
            upload_store=upload_store,
        )
        _apply_entry_upload_lifecycle_state(entry, updated)
        if entry.uploaded_bytes > 0:
            any_uploaded = True
    if any_uploaded and pin_record.fetch_state == FetchState.WAITING_MEDIA.value:
        pin_record.fetch_state = FetchState.UPLOADING.value


def _get_entry(entries: list[FetchEntryRecord], entry_id: str) -> FetchEntryRecord:
    for entry in entries:
        if entry.entry_id == entry_id:
            return entry
    raise NotFound(f"entry not found: {entry_id}")


def _entry_upload_payload(entry: FetchEntryRecord) -> dict[str, object]:
    return {
        "entry": entry.entry_id,
        "protocol": "tus",
        "upload_url": entry.tus_url,
        "offset": entry.uploaded_bytes,
        "length": entry.recovery_bytes,
        "checksum_algorithm": "sha256",
        "expires_at": entry.upload_expires_at,
    }


def _entry_upload_target_path(entry: FetchEntryRecord) -> str:
    return f"/.riverhog/uploads/recovery/{entry.fetch_id}/{entry.entry_id}.enc"


def _hot_payload(session: Session, raw_target: str) -> dict[str, object]:
    selected = _selected_files(session, raw_target)
    present_bytes = sum(record.bytes for record in selected if record.hot)
    missing_bytes = sum(record.bytes for record in selected if not record.hot)
    return {
        "state": "ready" if missing_bytes == 0 else "waiting",
        "present_bytes": present_bytes,
        "missing_bytes": missing_bytes,
    }


def delete_fetch_entries(session: Session, fetch_id: str) -> None:
    session.execute(delete(FetchEntryRecord).where(FetchEntryRecord.fetch_id == fetch_id))
