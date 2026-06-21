from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import and_, asc, case, delete, desc, func, or_, select
from sqlalchemy.orm import Session, object_session
from sqlalchemy.sql.elements import ColumnElement

from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionFileRecord,
    FetchEntryRecord,
    FetchOperatorFileRecord,
    FetchOperatorSummaryRecord,
    FetchRecord,
    FetchSelectorRecord,
    FileCopyRecord,
    FinalizedImageRecord,
    GlacierRecoverySessionCollectionRecord,
    GlacierRecoverySessionRecord,
)
from riverhog_core.domain.enums import FetchState, RecoverySessionState
from riverhog_core.domain.errors import BadRequest, Conflict, HashMismatch, InvalidState, NotFound
from riverhog_core.domain.models import FetchCopyHint, FetchListPage, FetchSummary
from riverhog_core.domain.selectors import parse_target
from riverhog_core.domain.types import CopyId
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
from riverhog_core.services.compliance import file_is_fully_compliant
from riverhog_core.services.copy_recovery_metadata import (
    CopyRecoveryMetadata,
    read_copy_recovery_metadata,
)
from riverhog_core.services.hot_fetch_projection import fetch_summary_from_projection
from riverhog_core.services.resumable_uploads import (
    UploadLifecycleState,
    create_or_resume_upload_state,
    expire_upload_state,
    sync_upload_state,
    upload_expiry_timestamp,
)
from riverhog_core.services.target_selection import (
    selected_collection_files,
)
from riverhog_core.webhooks import WebhookConfig, build_fetch_queued_payload, post_webhook, utcnow

_ACTIVE_CLOUD_FETCH_STATES = {
    RecoverySessionState.RESTORE_REQUESTED.value,
    RecoverySessionState.READY.value,
    RecoverySessionState.PAUSED.value,
}
_FETCH_SORT_FIELDS = {"id", "name", "state", "order", "files", "bytes", "missing_bytes"}
_EDITABLE_FETCH_STATES = {FetchState.DRAFT.value}
_DJDAN_FETCH_STATES = {
    FetchState.QUEUED_DJDAN.value,
    FetchState.UPLOADING.value,
    FetchState.VERIFYING.value,
}
_CLOUD_FETCH_STATES = {FetchState.QUEUED_CLOUD.value, FetchState.CLOUD_FETCHING.value}
_FETCH_FILE_SORT_FIELDS = {"target", "collection", "path", "bytes", "hot", "archived", "disc"}


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

    def create(self, *, name: str, targets: Sequence[str] | None = None) -> FetchSummary:
        normalized_name = _normalize_fetch_name(name)
        canonical_targets = _canonical_targets(targets or [])
        with session_scope(self._session_factory) as session:
            fetch_order = _next_fetch_order(session)
            fetch_record = FetchRecord(
                fetch_id=f"fx-{fetch_order}",
                name=normalized_name,
                fetch_order=fetch_order,
                fetch_state=FetchState.DRAFT.value,
            )
            session.add(fetch_record)
            _replace_fetch_selectors(session, fetch_record, canonical_targets)
            session.flush()
            return fetch_summary_from_projection(
                _get_fetch_projection(session, fetch_record.fetch_id),
                prefer_entries=True,
            )

    def list(
        self,
        *,
        page: int,
        per_page: int,
        state: str | None = None,
        q: str | None = None,
        sort: str = "order",
        order: str = "asc",
    ) -> FetchListPage:
        if page < 1:
            raise BadRequest("page must be at least 1")
        if per_page < 1 or per_page > 100:
            raise BadRequest("per_page must be between 1 and 100")
        if state is not None and state not in {item.value for item in FetchState}:
            raise BadRequest("state must be a valid fetch state")
        if sort not in _FETCH_SORT_FIELDS:
            raise BadRequest(f"sort must be one of {', '.join(sorted(_FETCH_SORT_FIELDS))}")
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")

        with session_scope(self._session_factory) as session:
            stmt = select(FetchOperatorSummaryRecord)
            if state is not None:
                stmt = stmt.where(FetchOperatorSummaryRecord.fetch_state == state)
            if q:
                pattern = f"%{q}%"
                stmt = stmt.where(
                    or_(
                        FetchOperatorSummaryRecord.fetch_id.like(pattern),
                        FetchOperatorSummaryRecord.name.like(pattern),
                        FetchOperatorSummaryRecord.targets_text.like(pattern),
                    )
                )

            total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            sort_expr = {
                "id": FetchOperatorSummaryRecord.fetch_id,
                "name": FetchOperatorSummaryRecord.name,
                "state": FetchOperatorSummaryRecord.fetch_state,
                "order": FetchOperatorSummaryRecord.fetch_order,
                "files": FetchOperatorSummaryRecord.files,
                "bytes": FetchOperatorSummaryRecord.bytes,
                "missing_bytes": FetchOperatorSummaryRecord.missing_bytes,
            }[sort]
            direction = desc if order == "desc" else asc
            rows = session.scalars(
                stmt.order_by(direction(sort_expr), asc(FetchOperatorSummaryRecord.fetch_id))
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).all()
            return FetchListPage(
                page=page,
                per_page=per_page,
                total=total,
                pages=(total + per_page - 1) // per_page if total else 0,
                fetches=[fetch_summary_from_projection(row, prefer_entries=True) for row in rows],
            )

    def add_targets(self, fetch_id: str, targets: Sequence[str]) -> FetchSummary:
        canonical_targets = _canonical_targets(targets)
        if not canonical_targets:
            raise BadRequest("at least one target is required")
        with session_scope(self._session_factory) as session:
            fetch_record = _get_fetch_record(session, fetch_id)
            _require_editable_fetch(fetch_record)
            existing = {selector.target for selector in _fetch_selectors(session, fetch_id)}
            merged = [
                *sorted(existing),
                *[target for target in canonical_targets if target not in existing],
            ]
            _replace_fetch_selectors(session, fetch_record, merged)
            session.flush()
            return fetch_summary_from_projection(
                _get_fetch_projection(session, fetch_record.fetch_id),
                prefer_entries=True,
            )

    def remove_targets(self, fetch_id: str, targets: Sequence[str]) -> FetchSummary:
        canonical_targets = set(_canonical_targets(targets))
        if not canonical_targets:
            raise BadRequest("at least one target is required")
        with session_scope(self._session_factory) as session:
            fetch_record = _get_fetch_record(session, fetch_id)
            _require_editable_fetch(fetch_record)
            remaining = [
                selector.target
                for selector in _fetch_selectors(session, fetch_id)
                if selector.target not in canonical_targets
            ]
            _replace_fetch_selectors(session, fetch_record, remaining)
            session.flush()
            return fetch_summary_from_projection(
                _get_fetch_projection(session, fetch_record.fetch_id),
                prefer_entries=True,
            )

    def start(self, fetch_id: str, *, cloud: bool = False) -> FetchSummary:
        with session_scope(self._session_factory) as session:
            fetch_record = _get_fetch_record(session, fetch_id)
            _require_startable_fetch(fetch_record)
            if not _fetch_selectors(session, fetch_id):
                raise InvalidState("fetch has no targets")
            fetch_record.fetch_state = (
                FetchState.QUEUED_CLOUD.value if cloud else FetchState.QUEUED_DJDAN.value
            )
            session.flush()
            return fetch_summary_from_projection(
                _get_fetch_projection(session, fetch_record.fetch_id),
                prefer_entries=True,
            )

    def cancel(self, fetch_id: str) -> FetchSummary:
        with session_scope(self._session_factory) as session:
            fetch_record = _get_fetch_record(session, fetch_id)
            if fetch_record.fetch_state in _CLOUD_FETCH_STATES:
                raise InvalidState("cloud fetches are canceled through recovery sessions")
            if fetch_record.fetch_state == FetchState.DONE.value:
                raise InvalidState("completed fetches cannot be canceled")

            _discard_existing_fetch_uploads(
                session,
                fetch_record.fetch_id,
                self._upload_store,
            )
            fetch_record.fetch_state = FetchState.DRAFT.value
            _clear_fetch_notification_state(fetch_record)
            session.flush()
            return fetch_summary_from_projection(
                _get_fetch_projection(session, fetch_record.fetch_id),
                prefer_entries=True,
            )

    def evict(self, targets: Sequence[str]) -> dict[str, object]:
        canonical_targets = _canonical_targets(targets)
        if not canonical_targets:
            raise BadRequest("at least one target is required")
        with session_scope(self._session_factory) as session:
            selected_by_key: dict[tuple[str, str], CollectionFileRecord] = {}
            for target in canonical_targets:
                for record in selected_collection_files(session, target):
                    selected_by_key[(record.collection_id, record.path)] = record
            selected = [
                selected_by_key[key]
                for key in sorted(selected_by_key, key=lambda item: (item[0], item[1]))
            ]
            if not selected:
                raise NotFound("target selectors matched no files")
            noncompliant = [
                record
                for record in selected
                if not file_is_fully_compliant(
                    session,
                    collection_id=record.collection_id,
                    path=record.path,
                )
            ]
            if noncompliant:
                first = noncompliant[0]
                raise Conflict(
                    "cannot evict hot file without verified disc protection: "
                    f"{first.collection_id}/{first.path}"
                )
            evicted_files = 0
            evicted_bytes = 0
            for record in selected:
                if not record.hot:
                    continue
                try:
                    self._hot_store.delete_collection_file(record.collection_id, record.path)
                except FileNotFoundError:
                    pass
                record.hot = False
                evicted_files += 1
                evicted_bytes += int(record.bytes)
            return {
                "targets": canonical_targets,
                "files": len(selected),
                "bytes": sum(int(record.bytes) for record in selected),
                "evicted_files": evicted_files,
                "evicted_bytes": evicted_bytes,
            }

    def get(self, fetch_id: str) -> FetchSummary:
        with session_scope(self._session_factory) as session:
            row = _get_fetch_projection(session, fetch_id)
            return fetch_summary_from_projection(row, prefer_entries=True)

    def status(self, fetch_id: str, *, limit: int = 25) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            row = _get_fetch_projection(session, fetch_id)
            summary = fetch_summary_from_projection(row, prefer_entries=True)
            target_summaries = _fetch_target_summaries(session, row.fetch_id)
            file_rollup = _fetch_file_rollup(session, row.fetch_id)
            files_preview = _fetch_file_preview(session, row.fetch_id, limit=limit)
            if summary.state == FetchState.DONE:
                entries: list[dict[str, object]] = []
            elif row.entries_total > 0:
                entries = _status_entries_from_query(
                    session,
                    row.fetch_id,
                    fetch_state=summary.state,
                    limit=limit,
                )
            else:
                entries = _pending_status_entries_from_fetch_query(
                    session,
                    row.fetch_id,
                    limit,
                )
            return _fetch_status_payload(
                summary,
                entries=entries,
                target_summaries=target_summaries,
                file_rollup=file_rollup,
                files_preview=files_preview,
                limit=limit,
            )

    def files(
        self,
        fetch_id: str,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        q: str | None = None,
        hot: bool | None = None,
        archived: bool | None = None,
        disc_coverage: bool | None = None,
    ) -> dict[str, object]:
        if page < 1:
            raise BadRequest("page must be greater than or equal to 1")
        if per_page < 1 or per_page > 100:
            raise BadRequest("per_page must be between 1 and 100")
        if sort not in _FETCH_FILE_SORT_FIELDS:
            raise BadRequest(f"sort must be one of {', '.join(sorted(_FETCH_FILE_SORT_FIELDS))}")
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")
        with session_scope(self._session_factory) as session:
            _get_fetch_projection(session, fetch_id)
            selected_files = _selected_files_subquery(fetch_id)
            filters = _fetch_file_filters(
                selected_files,
                q=q,
                hot=hot,
                archived=archived,
                disc_coverage=disc_coverage,
            )
            total_stmt = select(func.count()).select_from(selected_files)
            if filters:
                total_stmt = total_stmt.where(*filters)
            total = int(session.scalar(total_stmt) or 0)
            rows_stmt = (
                select(
                    selected_files.c.collection_id,
                    selected_files.c.path,
                    selected_files.c.bytes,
                    selected_files.c.hot,
                    selected_files.c.archived,
                    selected_files.c.registered_disc_coverage,
                )
                .select_from(selected_files)
                .order_by(*_fetch_file_order(selected_files, sort=sort, order=order))
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
            if filters:
                rows_stmt = rows_stmt.where(*filters)
            rows = session.execute(rows_stmt).all()
            files = [_fetch_file_payload(row) for row in rows]
            return _fetch_files_payload(
                fetch_id,
                page=page,
                per_page=per_page,
                total=total,
                sort=sort,
                order=order,
                q=q,
                hot=hot,
                archived=archived,
                disc_coverage=disc_coverage,
                files=files,
            )

    def manifest(self, fetch_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            fetch_record = _get_fetch_record(session, fetch_id)
            _require_djdan_fetch(fetch_record)
            entries = _ensure_fetch_entries(
                session,
                fetch_record,
                self._hot_store,
                self._recovery_payload_codec,
            )
            _sync_upload_progress(fetch_record, entries, self._upload_store)
            _expire_incomplete_uploads(fetch_record, entries, self._upload_store)
            return {
                "id": fetch_record.fetch_id,
                "name": fetch_record.name,
                "targets": _fetch_target_values(session, fetch_record.fetch_id),
                "entries": [
                    _manifest_entry_payload(
                        entry,
                        recovery_payload_codec=self._recovery_payload_codec,
                        fetch_state=FetchState(fetch_record.fetch_state),
                    )
                    for entry in entries
                ],
            }

    def create_or_resume_upload(self, fetch_id: str, entry_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            fetch_record = _get_fetch_record(session, fetch_id)
            _require_djdan_fetch(fetch_record)
            if fetch_record.fetch_state == FetchState.DONE.value:
                raise InvalidState("fetch is already complete")
            entries = _ensure_fetch_entries(
                session,
                fetch_record,
                self._hot_store,
                self._recovery_payload_codec,
            )
            _sync_upload_progress(fetch_record, entries, self._upload_store)
            _expire_incomplete_uploads(fetch_record, entries, self._upload_store)
            _raise_if_fetch_is_cloud_fetching(session, fetch_record, entries)
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
                fetch_record.fetch_state == FetchState.QUEUED_DJDAN.value
                and entry.uploaded_bytes > 0
            ):
                fetch_record.fetch_state = FetchState.UPLOADING.value

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
            fetch_record = _get_fetch_record(session, fetch_id)
            _require_djdan_fetch(fetch_record)
            entries = _ensure_fetch_entries(
                session,
                fetch_record,
                self._hot_store,
                self._recovery_payload_codec,
            )
            _raise_if_fetch_is_cloud_fetching(session, fetch_record, entries)
            entry = _get_entry(entries, entry_id)
            _expire_incomplete_upload_for_entry(fetch_record, entry, self._upload_store)

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

            if fetch_record.fetch_state == FetchState.QUEUED_DJDAN.value:
                fetch_record.fetch_state = FetchState.UPLOADING.value

            return {
                "offset": entry.uploaded_bytes,
                "length": entry.recovery_bytes,
                "expires_at": entry.upload_expires_at,
            }

    def get_entry_upload(self, fetch_id: str, entry_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            fetch_record = _get_fetch_record(session, fetch_id)
            _require_djdan_fetch(fetch_record)
            entries = _ensure_fetch_entries(
                session,
                fetch_record,
                self._hot_store,
                self._recovery_payload_codec,
            )
            _sync_upload_progress(fetch_record, entries, self._upload_store)
            _expire_incomplete_uploads(fetch_record, entries, self._upload_store)
            entry = _get_entry(entries, entry_id)

            if entry.tus_url is None:
                raise NotFound(f"fetch entry upload is not resumable: {entry_id}")
            return _entry_upload_payload(entry)

    def cancel_entry_upload(self, fetch_id: str, entry_id: str) -> None:
        with session_scope(self._session_factory) as session:
            fetch_record = _get_fetch_record(session, fetch_id)
            _require_djdan_fetch(fetch_record)
            entries = _ensure_fetch_entries(
                session,
                fetch_record,
                self._hot_store,
                self._recovery_payload_codec,
            )
            _sync_upload_progress(fetch_record, entries, self._upload_store)
            _expire_incomplete_uploads(fetch_record, entries, self._upload_store)
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
            if fetch_record.fetch_state == FetchState.UPLOADING.value:
                fetch_record.fetch_state = FetchState.QUEUED_DJDAN.value

    def expire_stale_uploads(self) -> None:
        with session_scope(self._session_factory) as session:
            fetch_records = session.scalars(
                select(FetchRecord).where(FetchRecord.fetch_state.in_(_DJDAN_FETCH_STATES))
            ).all()
            for fetch_record in fetch_records:
                entries = list(
                    session.scalars(
                        select(FetchEntryRecord)
                        .where(FetchEntryRecord.fetch_id == fetch_record.fetch_id)
                        .order_by(FetchEntryRecord.entry_order)
                    ).all()
                )
                if not entries:
                    continue
                _sync_upload_progress(fetch_record, entries, self._upload_store)
                _expire_incomplete_uploads(fetch_record, entries, self._upload_store)

    def deliver_due_queued_notifications(self, *, limit: int = 100) -> int:
        if limit < 1 or not self._config.operator_webhook_url:
            return 0

        current = utcnow()
        current_text = _isoformat_z(current)
        delivered = 0
        with session_scope(self._session_factory) as session:
            fetches = session.scalars(
                select(FetchRecord)
                .where(FetchRecord.fetch_state == FetchState.QUEUED_DJDAN.value)
                .where(
                    or_(
                        FetchRecord.fetch_notification_sent_at.is_(None),
                        (
                            FetchRecord.fetch_notification_next_attempt_at.is_not(None)
                            & (FetchRecord.fetch_notification_next_attempt_at <= current_text)
                        ),
                    )
                )
                .order_by(FetchRecord.fetch_order)
                .limit(limit)
            ).all()
            for fetch_record in fetches:
                entries = _ensure_fetch_entries(
                    session,
                    fetch_record,
                    self._hot_store,
                    self._recovery_payload_codec,
                )
                _sync_upload_progress(fetch_record, entries, self._upload_store)
                _expire_incomplete_uploads(fetch_record, entries, self._upload_store)
                if fetch_record.fetch_state != FetchState.QUEUED_DJDAN.value:
                    continue
                if _active_cloud_fetch_session_id(session, fetch_record, entries) is not None:
                    continue
                if _notify_fetch_queued(
                    config=self._config,
                    fetch_record=fetch_record,
                    entries=entries,
                    current=current,
                ):
                    delivered += 1
        return delivered

    def complete(self, fetch_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            fetch_record = _get_fetch_record(session, fetch_id)
            _require_djdan_fetch(fetch_record)
            entries = _ensure_fetch_entries(
                session,
                fetch_record,
                self._hot_store,
                self._recovery_payload_codec,
            )
            _sync_upload_progress(fetch_record, entries, self._upload_store)
            _expire_incomplete_uploads(fetch_record, entries, self._upload_store)
            _raise_if_fetch_is_cloud_fetching(session, fetch_record, entries)

            if fetch_record.fetch_state == FetchState.DONE.value:
                return {
                    "id": fetch_record.fetch_id,
                    "state": fetch_record.fetch_state,
                    "hot": _hot_payload_for_fetch(session, fetch_record.fetch_id),
                }

            if any(
                _entry_upload_state(entry, fetch_state=FetchState(fetch_record.fetch_state))
                != "byte_complete"
                for entry in entries
            ):
                raise InvalidState("fetch is missing required entry uploads")

            fetch_record.fetch_state = FetchState.VERIFYING.value
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

            fetch_record.fetch_state = FetchState.DONE.value
            return {
                "id": fetch_record.fetch_id,
                "state": fetch_record.fetch_state,
                "hot": _hot_payload_for_fetch(session, fetch_record.fetch_id),
            }


def _get_fetch_record(session: Session, fetch_id: str) -> FetchRecord:
    fetch_record = session.get(FetchRecord, fetch_id)
    if fetch_record is None:
        raise NotFound(f"fetch not found: {fetch_id}")
    return fetch_record


def _get_fetch_projection(
    session: Session,
    fetch_id: str,
) -> FetchOperatorSummaryRecord:
    row = session.scalar(
        select(FetchOperatorSummaryRecord).where(FetchOperatorSummaryRecord.fetch_id == fetch_id)
    )
    if row is None:
        raise NotFound(f"fetch not found: {fetch_id}")
    return row


def _notify_fetch_queued(
    *,
    config: RuntimeConfig,
    fetch_record: FetchRecord,
    entries: list[FetchEntryRecord],
    current: datetime,
) -> bool:
    if not config.operator_webhook_url:
        fetch_record.fetch_notification_next_attempt_at = None
        fetch_record.fetch_notification_failure = None
        return False
    reminder = fetch_record.fetch_notification_sent_at is not None
    current_count = int(fetch_record.fetch_notification_count or 0)
    webhook_config = _webhook_config(config)
    try:
        post_webhook(
            config=webhook_config,
            payload=build_fetch_queued_payload(
                config=webhook_config,
                fetch_id=fetch_record.fetch_id,
                name=fetch_record.name,
                files=len(entries),
                bytes=sum(int(entry.bytes) for entry in entries),
                copies=_fetch_copy_payload(entries),
                delivered_at=current,
                reminder_count=current_count,
                reminder=reminder,
            ),
        )
    except Exception as exc:
        fetch_record.fetch_notification_failure = str(exc).strip() or exc.__class__.__name__
        fetch_record.fetch_notification_next_attempt_at = _isoformat_z(
            current + config.operator_webhook_retry_delay
        )
        return False

    if fetch_record.fetch_notification_sent_at is None:
        fetch_record.fetch_notification_sent_at = _isoformat_z(current)
    if reminder:
        current_count += 1
    fetch_record.fetch_notification_count = current_count
    fetch_record.fetch_notification_failure = None
    interval = config.operator_webhook_reminder_interval
    if interval.total_seconds() > 0:
        fetch_record.fetch_notification_next_attempt_at = _isoformat_z(current + interval)
    else:
        fetch_record.fetch_notification_next_attempt_at = None
    return True


def _active_cloud_fetch_session_id(
    session: Session,
    fetch_record: FetchRecord,
    entries: list[FetchEntryRecord],
) -> str | None:
    paths_by_collection: dict[str, set[str]] = {}
    for entry in entries:
        paths_by_collection.setdefault(entry.collection_id, set()).add(entry.path)
    if not paths_by_collection:
        return None

    type_expr = func.coalesce(GlacierRecoverySessionRecord.type, "image_rebuild")
    rows = session.execute(
        select(
            GlacierRecoverySessionRecord,
            GlacierRecoverySessionCollectionRecord.collection_id,
        )
        .join(
            GlacierRecoverySessionCollectionRecord,
            GlacierRecoverySessionCollectionRecord.session_id
            == GlacierRecoverySessionRecord.session_id,
        )
        .where(type_expr == "collection_restore")
        .where(GlacierRecoverySessionRecord.state.in_(_ACTIVE_CLOUD_FETCH_STATES))
        .where(
            GlacierRecoverySessionCollectionRecord.collection_id.in_(sorted(paths_by_collection))
        )
        .order_by(GlacierRecoverySessionRecord.created_at.desc())
    ).all()
    for record, collection_id in rows:
        fetch_paths = paths_by_collection.get(collection_id)
        if not fetch_paths:
            continue
        restore_paths = _restore_paths_from_json(record.restore_paths_json)
        if restore_paths is None or set(restore_paths) & fetch_paths:
            return str(record.session_id)
    return None


def _raise_if_fetch_is_cloud_fetching(
    session: Session,
    fetch_record: FetchRecord,
    entries: list[FetchEntryRecord],
) -> None:
    session_id = _active_cloud_fetch_session_id(session, fetch_record, entries)
    if session_id is None:
        return
    raise InvalidState(
        f"fetch {fetch_record.fetch_id} is actively being cloud-fetched by {session_id}; "
        "wait for cloud-fetch to finish before running djdan fetch"
    )


def _restore_paths_from_json(raw_value: str | None) -> tuple[str, ...] | None:
    if raw_value is None:
        return None
    loaded = json.loads(raw_value)
    if not isinstance(loaded, list):
        raise InvalidState("cloud-fetch restore paths are corrupt")
    return tuple(str(item) for item in loaded)


def _fetch_copy_payload(entries: list[FetchEntryRecord]) -> list[dict[str, str]]:
    copies: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for hint in _summary_copies(entries):
        key = (hint.volume_id, str(hint.id))
        if key in seen:
            continue
        seen.add(key)
        copies.append(
            {
                "copy_id": str(hint.id),
                "volume_id": hint.volume_id,
                "location": hint.location,
            }
        )
    return copies


def _webhook_config(config: RuntimeConfig) -> WebhookConfig:
    return WebhookConfig(
        url=config.operator_webhook_url or "",
        base_url=config.public_base_url or "",
        timeout_seconds=config.operator_webhook_timeout.total_seconds(),
        retry_seconds=config.operator_webhook_retry_delay.total_seconds(),
        reminder_interval_seconds=config.operator_webhook_reminder_interval.total_seconds(),
    )


def _isoformat_z(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_fetch_name(name: str) -> str:
    normalized = " ".join(name.strip().split())
    if not normalized:
        raise BadRequest("fetch name is required")
    return normalized


def _canonical_targets(targets: Sequence[str]) -> list[str]:
    canonical: list[str] = []
    seen: set[str] = set()
    for raw_target in targets:
        target = parse_target(raw_target)
        value = target.canonical
        if value in seen:
            continue
        seen.add(value)
        canonical.append(value)
    return canonical


def _next_fetch_order(session: Session) -> int:
    max_fetch_order = session.scalar(select(func.max(FetchRecord.fetch_order)))
    return int(max_fetch_order or 0) + 1


def _fetch_selectors(session: Session, fetch_id: str) -> list[FetchSelectorRecord]:
    return list(
        session.scalars(
            select(FetchSelectorRecord)
            .where(FetchSelectorRecord.fetch_id == fetch_id)
            .order_by(FetchSelectorRecord.selector_order, FetchSelectorRecord.target)
        ).all()
    )


def _fetch_target_values(session: Session, fetch_id: str) -> tuple[str, ...]:
    return tuple(selector.target for selector in _fetch_selectors(session, fetch_id))


def _replace_fetch_selectors(
    session: Session,
    fetch_record: FetchRecord,
    targets: list[str],
) -> None:
    session.execute(
        delete(FetchSelectorRecord).where(FetchSelectorRecord.fetch_id == fetch_record.fetch_id)
    )
    for index, target in enumerate(targets, start=1):
        session.add(
            FetchSelectorRecord(
                fetch_id=fetch_record.fetch_id,
                target=target,
                selector_order=index,
            )
        )


def _clear_fetch_notification_state(fetch_record: FetchRecord) -> None:
    fetch_record.fetch_notification_sent_at = None
    fetch_record.fetch_notification_next_attempt_at = None
    fetch_record.fetch_notification_failure = None
    fetch_record.fetch_notification_count = None


def _require_editable_fetch(fetch_record: FetchRecord) -> None:
    if fetch_record.fetch_state not in _EDITABLE_FETCH_STATES:
        raise InvalidState("fetch is already started and cannot be edited")


def _require_startable_fetch(fetch_record: FetchRecord) -> None:
    if fetch_record.fetch_state == FetchState.DONE.value:
        raise InvalidState("fetch is already complete")
    if fetch_record.fetch_state in _DJDAN_FETCH_STATES | _CLOUD_FETCH_STATES:
        raise InvalidState("fetch is already started")
    if fetch_record.fetch_state != FetchState.DRAFT.value:
        raise InvalidState(f"fetch cannot be started from state {fetch_record.fetch_state}")


def _require_djdan_fetch(fetch_record: FetchRecord) -> None:
    if fetch_record.fetch_state == FetchState.DONE.value:
        return
    if fetch_record.fetch_state not in _DJDAN_FETCH_STATES:
        raise InvalidState("fetch is not queued for djdan")


def _selected_files_for_fetch(
    session: Session,
    fetch_id: str,
    *,
    load_copies: bool,
) -> list[CollectionFileRecord]:
    selected_by_key: dict[tuple[str, str], CollectionFileRecord] = {}
    targets = _fetch_target_values(session, fetch_id)
    for target in targets:
        for record in selected_collection_files(session, target, load_copies=load_copies):
            selected_by_key[(record.collection_id, record.path)] = record
    if targets and not selected_by_key:
        raise NotFound(f"fetch targets matched no files: {fetch_id}")
    return [
        selected_by_key[key] for key in sorted(selected_by_key, key=lambda item: (item[0], item[1]))
    ]


def _existing_fetch_entries(
    session: Session,
    fetch_id: str,
) -> list[FetchEntryRecord]:
    return list(
        session.scalars(
            select(FetchEntryRecord)
            .where(FetchEntryRecord.fetch_id == fetch_id)
            .order_by(FetchEntryRecord.entry_order)
        ).all()
    )


def _ensure_fetch_entries(
    session: Session,
    fetch_record: FetchRecord,
    hot_store: HotStore,
    recovery_payload_codec: RecoveryPayloadCodec,
) -> list[FetchEntryRecord]:
    existing = _existing_fetch_entries(session, fetch_record.fetch_id)
    if existing:
        return existing

    selected = _selected_files_for_fetch(session, fetch_record.fetch_id, load_copies=False)
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
            fetch_id=fetch_record.fetch_id,
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


def _discard_existing_fetch_uploads(
    session: Session,
    fetch_id: str,
    upload_store: UploadStore,
) -> None:
    entries = _existing_fetch_entries(session, fetch_id)
    for entry in entries:
        target_path = _entry_upload_target_path(entry)
        if entry.tus_url is not None:
            upload_store.cancel_upload(entry.tus_url)
        upload_store.delete_target(target_path)
    session.execute(delete(FetchEntryRecord).where(FetchEntryRecord.fetch_id == fetch_id))


def _fetch_status_payload(
    summary: FetchSummary,
    *,
    entries: list[dict[str, object]],
    target_summaries: list[dict[str, object]],
    file_rollup: dict[str, int],
    files_preview: list[dict[str, object]],
    limit: int,
) -> dict[str, object]:
    next_action, next_action_reason = _fetch_next_action(summary, file_rollup)
    return {
        "id": str(summary.id),
        "name": summary.name,
        "targets": [str(target) for target in summary.targets],
        "state": summary.state.value,
        "files": summary.files,
        "bytes": summary.bytes,
        "hot_files": file_rollup["hot_files"],
        "hot_bytes": file_rollup["hot_bytes"],
        "archived_files": file_rollup["archived_files"],
        "archived_bytes": file_rollup["archived_bytes"],
        "registered_disc_files": file_rollup["registered_disc_files"],
        "missing_files": file_rollup["missing_files"],
        "missing_with_disc_files": file_rollup["missing_with_disc_files"],
        "missing_without_disc_files": file_rollup["missing_without_disc_files"],
        "entries_total": summary.entries_total,
        "entries_pending": summary.entries_pending,
        "entries_partial": summary.entries_partial,
        "entries_byte_complete": summary.entries_byte_complete,
        "entries_uploaded": summary.entries_uploaded,
        "uploaded_bytes": summary.uploaded_bytes,
        "missing_bytes": summary.missing_bytes,
        "upload_state_expires_at": summary.upload_state_expires_at,
        "copies": [
            {"id": str(copy.id), "volume_id": copy.volume_id, "location": copy.location}
            for copy in summary.copies
        ],
        "entries_limit": limit,
        "entries_returned": len(entries),
        "entries": entries,
        "target_summaries": target_summaries,
        "files_preview_limit": limit,
        "files_preview_returned": len(files_preview),
        "files_preview": files_preview,
        "next_action": next_action,
        "next_action_reason": next_action_reason,
    }


def _status_entry_payload(
    entry_id: str,
    *,
    collection_id: str,
    path: str,
    bytes_: int,
    upload_state: str,
    uploaded_bytes: int,
    upload_state_expires_at: str | None,
) -> dict[str, object]:
    return {
        "id": entry_id,
        "collection_id": collection_id,
        "path": path,
        "bytes": bytes_,
        "upload_state": upload_state,
        "uploaded_bytes": uploaded_bytes,
        "upload_state_expires_at": upload_state_expires_at,
    }


def _status_entries_from_records(
    entries: list[FetchEntryRecord],
    *,
    fetch_state: FetchState,
    limit: int,
) -> list[dict[str, object]]:
    if limit <= 0:
        return []
    out: list[dict[str, object]] = []
    for entry in entries:
        upload_state = _entry_upload_state(entry, fetch_state=fetch_state)
        if upload_state == "uploaded":
            continue
        out.append(
            _status_entry_payload(
                entry.entry_id,
                collection_id=entry.collection_id,
                path=entry.path,
                bytes_=entry.bytes,
                upload_state=upload_state,
                uploaded_bytes=entry.uploaded_bytes,
                upload_state_expires_at=entry.upload_expires_at,
            )
        )
        if len(out) >= limit:
            break
    return out


def _status_entries_from_query(
    session: Session,
    fetch_id: str,
    *,
    fetch_state: FetchState,
    limit: int,
) -> list[dict[str, object]]:
    if limit <= 0 or fetch_state == FetchState.DONE:
        return []
    rows = session.scalars(
        select(FetchEntryRecord)
        .where(FetchEntryRecord.fetch_id == fetch_id)
        .order_by(FetchEntryRecord.entry_order)
        .limit(limit)
    ).all()
    return [
        _status_entry_payload(
            entry.entry_id,
            collection_id=entry.collection_id,
            path=entry.path,
            bytes_=entry.bytes,
            upload_state=_entry_upload_state(entry, fetch_state=fetch_state),
            uploaded_bytes=entry.uploaded_bytes,
            upload_state_expires_at=entry.upload_expires_at,
        )
        for entry in rows
    ]


def _target_file_match_clause(
    raw_target: str,
    *,
    collection_id: Any = CollectionFileRecord.collection_id,
    path: Any = CollectionFileRecord.path,
) -> ColumnElement[bool]:
    target = parse_target(raw_target)
    full_path = collection_id + "/" + path
    if target.is_dir:
        canonical = target.canonical
        return func.substr(full_path, 1, len(canonical)) == canonical
    return cast(ColumnElement[bool], full_path == target.canonical)


def _selected_files_subquery(fetch_id: str) -> Any:
    return (
        select(
            FetchOperatorFileRecord.collection_id.label("collection_id"),
            FetchOperatorFileRecord.path.label("path"),
            FetchOperatorFileRecord.bytes.label("bytes"),
            FetchOperatorFileRecord.hot.label("hot"),
            FetchOperatorFileRecord.archived.label("archived"),
            FetchOperatorFileRecord.registered_disc_coverage.label("registered_disc_coverage"),
        )
        .where(FetchOperatorFileRecord.fetch_id == fetch_id)
        .subquery()
    )


def _fetch_file_rollup(session: Session, fetch_id: str) -> dict[str, int]:
    selected_files = _selected_files_subquery(fetch_id)
    row = session.execute(
        select(
            func.count().label("files"),
            func.coalesce(func.sum(selected_files.c.bytes), 0).label("bytes"),
            func.coalesce(
                func.sum(case((selected_files.c.hot.is_(True), 1), else_=0)),
                0,
            ).label("hot_files"),
            func.coalesce(
                func.sum(
                    case(
                        (selected_files.c.hot.is_(True), selected_files.c.bytes),
                        else_=0,
                    )
                ),
                0,
            ).label("hot_bytes"),
            func.coalesce(
                func.sum(case((selected_files.c.archived.is_(True), 1), else_=0)),
                0,
            ).label("archived_files"),
            func.coalesce(
                func.sum(
                    case(
                        (selected_files.c.archived.is_(True), selected_files.c.bytes),
                        else_=0,
                    )
                ),
                0,
            ).label("archived_bytes"),
            func.coalesce(
                func.sum(case((selected_files.c.registered_disc_coverage.is_(True), 1), else_=0)),
                0,
            ).label("registered_disc_files"),
            func.coalesce(
                func.sum(case((selected_files.c.hot.is_(False), 1), else_=0)),
                0,
            ).label("missing_files"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                selected_files.c.hot.is_(False),
                                selected_files.c.registered_disc_coverage.is_(True),
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("missing_with_disc_files"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                selected_files.c.hot.is_(False),
                                selected_files.c.registered_disc_coverage.is_(False),
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("missing_without_disc_files"),
        ).select_from(selected_files)
    ).one()
    return {
        "files": int(row.files or 0),
        "bytes": int(row.bytes or 0),
        "hot_files": int(row.hot_files or 0),
        "hot_bytes": int(row.hot_bytes or 0),
        "archived_files": int(row.archived_files or 0),
        "archived_bytes": int(row.archived_bytes or 0),
        "registered_disc_files": int(row.registered_disc_files or 0),
        "missing_files": int(row.missing_files or 0),
        "missing_with_disc_files": int(row.missing_with_disc_files or 0),
        "missing_without_disc_files": int(row.missing_without_disc_files or 0),
    }


def _empty_fetch_file_rollup() -> dict[str, int]:
    return {
        "files": 0,
        "bytes": 0,
        "hot_files": 0,
        "hot_bytes": 0,
        "archived_files": 0,
        "archived_bytes": 0,
        "registered_disc_files": 0,
        "missing_files": 0,
        "missing_with_disc_files": 0,
        "missing_without_disc_files": 0,
    }


def _fetch_target_summaries(session: Session, fetch_id: str) -> list[dict[str, object]]:
    selected_files = _selected_files_subquery(fetch_id)
    summaries: list[dict[str, object]] = []
    for target in _fetch_target_values(session, fetch_id):
        disc_coverage = selected_files.c.registered_disc_coverage
        row = session.execute(
            select(
                func.count().label("files"),
                func.coalesce(func.sum(selected_files.c.bytes), 0).label("bytes"),
                func.coalesce(
                    func.sum(case((selected_files.c.hot.is_(True), 1), else_=0)),
                    0,
                ).label("hot_files"),
                func.coalesce(
                    func.sum(
                        case(
                            (selected_files.c.hot.is_(True), selected_files.c.bytes),
                            else_=0,
                        )
                    ),
                    0,
                ).label("hot_bytes"),
                func.coalesce(
                    func.sum(case((selected_files.c.archived.is_(True), 1), else_=0)),
                    0,
                ).label("archived_files"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                selected_files.c.archived.is_(True),
                                selected_files.c.bytes,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("archived_bytes"),
                func.coalesce(
                    func.sum(case((disc_coverage, 1), else_=0)),
                    0,
                ).label("registered_disc_files"),
                func.coalesce(
                    func.sum(case((selected_files.c.hot.is_(False), 1), else_=0)),
                    0,
                ).label("missing_files"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    selected_files.c.hot.is_(False),
                                    disc_coverage.is_(True),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("missing_with_disc_files"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    selected_files.c.hot.is_(False),
                                    disc_coverage.is_(False),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("missing_without_disc_files"),
            )
            .select_from(selected_files)
            .where(
                _target_file_match_clause(
                    target,
                    collection_id=selected_files.c.collection_id,
                    path=selected_files.c.path,
                )
            )
        ).one()
        summaries.append(
            {
                "target": target,
                "files": int(row.files or 0),
                "bytes": int(row.bytes or 0),
                "hot_files": int(row.hot_files or 0),
                "hot_bytes": int(row.hot_bytes or 0),
                "archived_files": int(row.archived_files or 0),
                "archived_bytes": int(row.archived_bytes or 0),
                "registered_disc_files": int(row.registered_disc_files or 0),
                "missing_files": int(row.missing_files or 0),
                "missing_with_disc_files": int(row.missing_with_disc_files or 0),
                "missing_without_disc_files": int(row.missing_without_disc_files or 0),
            }
        )
    return summaries


def _fetch_file_preview(
    session: Session,
    fetch_id: str,
    *,
    limit: int,
) -> list[dict[str, object]]:
    if limit <= 0:
        return []
    selected_files = _selected_files_subquery(fetch_id)
    rows = session.execute(
        select(
            selected_files.c.collection_id,
            selected_files.c.path,
            selected_files.c.bytes,
            selected_files.c.hot,
            selected_files.c.archived,
            selected_files.c.registered_disc_coverage,
        )
        .select_from(selected_files)
        .order_by(selected_files.c.collection_id, selected_files.c.path)
        .limit(limit)
    ).all()
    return [_fetch_file_payload(row) for row in rows]


def _fetch_file_payload(row: Any) -> dict[str, object]:
    return {
        "target": f"{row.collection_id}/{row.path}",
        "collection_id": row.collection_id,
        "path": row.path,
        "bytes": int(row.bytes),
        "hot": bool(row.hot),
        "archived": bool(row.archived),
        "registered_disc_coverage": bool(row.registered_disc_coverage),
    }


def _fetch_files_payload(
    fetch_id: str,
    *,
    page: int,
    per_page: int,
    total: int,
    sort: str,
    order: str,
    q: str | None,
    hot: bool | None,
    archived: bool | None,
    disc_coverage: bool | None,
    files: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "fetch_id": fetch_id,
        "query": q,
        "hot": hot,
        "archived": archived,
        "disc_coverage": disc_coverage,
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": (total + per_page - 1) // per_page if total else 0,
        "sort": sort,
        "order": order,
        "files": files,
    }


def _fetch_file_filters(
    selected_files: Any,
    *,
    q: str | None,
    hot: bool | None,
    archived: bool | None,
    disc_coverage: bool | None,
) -> list[ColumnElement[bool]]:
    filters: list[ColumnElement[bool]] = []
    if q:
        target_text = selected_files.c.collection_id + "/" + selected_files.c.path
        pattern = _like_pattern(q)
        filters.append(
            or_(
                target_text.like(pattern, escape="\\"),
                selected_files.c.collection_id.like(pattern, escape="\\"),
                selected_files.c.path.like(pattern, escape="\\"),
            )
        )
    if hot is not None:
        filters.append(selected_files.c.hot.is_(hot))
    if archived is not None:
        filters.append(selected_files.c.archived.is_(archived))
    if disc_coverage is not None:
        filters.append(selected_files.c.registered_disc_coverage.is_(disc_coverage))
    return filters


def _fetch_file_order(selected_files: Any, *, sort: str, order: str) -> tuple[Any, ...]:
    direction = desc if order == "desc" else asc
    if sort == "target":
        return (
            direction(selected_files.c.collection_id),
            direction(selected_files.c.path),
        )
    if sort == "collection":
        return (
            direction(selected_files.c.collection_id),
            asc(selected_files.c.path),
        )
    if sort == "path":
        return (
            direction(selected_files.c.path),
            asc(selected_files.c.collection_id),
        )
    if sort == "bytes":
        return (
            direction(selected_files.c.bytes),
            asc(selected_files.c.collection_id),
            asc(selected_files.c.path),
        )
    if sort == "hot":
        return (
            direction(selected_files.c.hot),
            asc(selected_files.c.collection_id),
            asc(selected_files.c.path),
        )
    if sort == "archived":
        return (
            direction(selected_files.c.archived),
            asc(selected_files.c.collection_id),
            asc(selected_files.c.path),
        )
    if sort == "disc":
        return (
            direction(selected_files.c.registered_disc_coverage),
            asc(selected_files.c.collection_id),
            asc(selected_files.c.path),
        )
    raise BadRequest(f"sort must be one of {', '.join(sorted(_FETCH_FILE_SORT_FIELDS))}")


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _fetch_next_action(summary: FetchSummary, file_rollup: dict[str, int]) -> tuple[str, str]:
    if summary.state == FetchState.DRAFT:
        if not summary.targets:
            return "add_targets", "add at least one target selector before starting"
        if file_rollup["files"] <= 0:
            return "edit_targets", "current target selectors match no files"
        if file_rollup["missing_files"] <= 0:
            return "already_hot", "all selected files are already hot"
        if file_rollup["missing_without_disc_files"] > 0:
            return (
                "start_cloud",
                "some missing files have no registered disc coverage",
            )
        return "start_djdan", "missing files have registered disc coverage"
    if summary.state == FetchState.QUEUED_DJDAN:
        return "run_djdan_fetch", "queued for optical media recovery"
    if summary.state == FetchState.UPLOADING:
        return "continue_djdan_fetch", "optical media upload is in progress"
    if summary.state == FetchState.VERIFYING:
        return "wait", "server-side verification is in progress"
    if summary.state in {FetchState.QUEUED_CLOUD, FetchState.CLOUD_FETCHING}:
        return "monitor_cloud_fetch", "cloud materialization is active"
    if summary.state == FetchState.DONE:
        return "done", "all selected files are hot"
    if summary.state == FetchState.FAILED:
        return "inspect_failure", "fetch failed and needs operator review"
    return "inspect", f"fetch is in state {summary.state.value}"


def _pending_status_entries_from_fetch_query(
    session: Session,
    fetch_id: str,
    limit: int,
) -> list[dict[str, object]]:
    if limit <= 0:
        return []
    selected_files = (
        select(
            FetchOperatorFileRecord.collection_id.label("collection_id"),
            FetchOperatorFileRecord.path.label("path"),
            FetchOperatorFileRecord.bytes.label("bytes"),
            FetchOperatorFileRecord.hot.label("hot"),
            func.row_number()
            .over(order_by=(FetchOperatorFileRecord.collection_id, FetchOperatorFileRecord.path))
            .label("entry_order"),
        )
        .where(FetchOperatorFileRecord.fetch_id == fetch_id)
        .subquery()
    )
    rows = session.execute(
        select(
            selected_files.c.collection_id,
            selected_files.c.path,
            selected_files.c.bytes,
            selected_files.c.entry_order,
        )
        .where(selected_files.c.hot.is_(False))
        .order_by(selected_files.c.entry_order)
        .limit(limit)
    ).all()
    return [
        _status_entry_payload(
            f"e{int(row.entry_order)}",
            collection_id=row.collection_id,
            path=row.path,
            bytes_=int(row.bytes),
            upload_state="pending",
            uploaded_bytes=0,
            upload_state_expires_at=None,
        )
        for row in rows
    ]


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
        "collection_id": entry.collection_id,
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
    fetch_record: FetchRecord,
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
    if expired and fetch_record.fetch_state == FetchState.UPLOADING.value:
        fetch_record.fetch_state = FetchState.QUEUED_DJDAN.value


def _expire_incomplete_upload_for_entry(
    fetch_record: FetchRecord,
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
    if did_expire and fetch_record.fetch_state == FetchState.UPLOADING.value:
        fetch_record.fetch_state = FetchState.QUEUED_DJDAN.value


def _sync_upload_progress(
    fetch_record: FetchRecord,
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
    if any_uploaded and fetch_record.fetch_state == FetchState.QUEUED_DJDAN.value:
        fetch_record.fetch_state = FetchState.UPLOADING.value


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


def _hot_payload_for_fetch(session: Session, fetch_id: str) -> dict[str, object]:
    selected = _selected_files_for_fetch(session, fetch_id, load_copies=False)
    present_bytes = sum(record.bytes for record in selected if record.hot)
    missing_bytes = sum(record.bytes for record in selected if not record.hot)
    return {
        "state": "ready" if missing_bytes == 0 else "waiting",
        "present_bytes": present_bytes,
        "missing_bytes": missing_bytes,
    }


def delete_fetch_entries(session: Session, fetch_id: str) -> None:
    session.execute(delete(FetchEntryRecord).where(FetchEntryRecord.fetch_id == fetch_id))
