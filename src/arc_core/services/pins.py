from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from arc_core.catalog_models import (
    ActivePinRecord,
    CollectionFileRecord,
    FetchEntryRecord,
)
from arc_core.domain.enums import FetchState
from arc_core.domain.errors import NotFound
from arc_core.domain.models import FetchCopyHint, FetchSummary, PinSummary
from arc_core.domain.selectors import parse_target
from arc_core.domain.types import CopyId, FetchId, TargetStr
from arc_core.ports.hot_store import HotStore
from arc_core.ports.upload_store import UploadStore
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.fetches import delete_fetch_entries
from arc_core.sqlite_db import make_session_factory, session_scope


class SqlAlchemyPinService:
    def __init__(
        self, config: RuntimeConfig, hot_store: HotStore, upload_store: UploadStore
    ) -> None:
        self._session_factory = make_session_factory(str(config.sqlite_path))
        self._hot_store = hot_store
        self._upload_store = upload_store

    def pin(self, raw_target: str) -> dict[str, object]:
        target = parse_target(raw_target)
        canonical = TargetStr(target.canonical)
        with session_scope(self._session_factory) as session:
            selected = _selected_files(session, target.canonical)
            present_bytes = sum(record.bytes for record in selected if record.hot)
            missing_bytes = sum(record.bytes for record in selected if not record.hot)

            pin_record = session.get(ActivePinRecord, canonical)
            if pin_record is None:
                fetch_order = _next_fetch_order(session)
                pin_record = ActivePinRecord(
                    target=canonical,
                    fetch_id=f"fx-{fetch_order}",
                    fetch_order=fetch_order,
                    fetch_state=(
                        FetchState.DONE.value
                        if missing_bytes == 0
                        else FetchState.WAITING_MEDIA.value
                    ),
                )
                session.add(pin_record)
                session.flush()

            fetch_summary = _fetch_summary(pin_record, selected)
            return {
                "target": str(canonical),
                "pin": True,
                "hot": {
                    "state": "ready" if missing_bytes == 0 else "waiting",
                    "present_bytes": present_bytes,
                    "missing_bytes": missing_bytes,
                },
                "fetch": _fetch_payload(fetch_summary),
            }

    def release(self, raw_target: str) -> dict[str, object]:
        target = parse_target(raw_target)
        canonical = TargetStr(target.canonical)
        with session_scope(self._session_factory) as session:
            pin_record = session.get(ActivePinRecord, canonical)
            if pin_record is not None:
                fetch_id = pin_record.fetch_id
                entries = session.scalars(
                    select(FetchEntryRecord).where(FetchEntryRecord.fetch_id == fetch_id)
                ).all()
                for entry in entries:
                    if entry.tus_url is not None:
                        self._upload_store.cancel_upload(entry.tus_url)
                    target_path = f"/.arc/uploads/recovery/{fetch_id}/{entry.entry_id}.enc"
                    self._upload_store.delete_target(target_path)
                delete_fetch_entries(session, fetch_id)
                session.delete(pin_record)
                session.flush()
            _reconcile_hot_from_pins(session, self._hot_store)
        return {
            "target": str(canonical),
            "pin": False,
        }

    def list_pins(self) -> list[PinSummary]:
        with session_scope(self._session_factory) as session:
            pin_records = session.scalars(
                select(ActivePinRecord).order_by(ActivePinRecord.target)
            ).all()
            summaries: list[PinSummary] = []
            for pin_record in pin_records:
                selected = _selected_files(session, pin_record.target)
                summaries.append(
                    PinSummary(
                        target=TargetStr(pin_record.target),
                        fetch=_fetch_summary(pin_record, selected),
                    )
                )
            return summaries


def _next_fetch_order(session: Session) -> int:
    max_fetch_order = session.scalar(select(func.max(ActivePinRecord.fetch_order)))
    return int(max_fetch_order or 0) + 1


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


def _fetch_summary(
    pin_record: ActivePinRecord,
    selected: list[CollectionFileRecord],
) -> FetchSummary:
    return FetchSummary(
        id=FetchId(pin_record.fetch_id),
        target=TargetStr(pin_record.target),
        state=FetchState(pin_record.fetch_state),
        files=len(selected),
        bytes=sum(record.bytes for record in selected),
        copies=_summary_copies(selected),
    )


def _summary_copies(selected: list[CollectionFileRecord]) -> list[FetchCopyHint]:
    seen: set[tuple[str, str]] = set()
    out: list[FetchCopyHint] = []
    for record in selected:
        for copy in sorted(
            record.copies,
            key=lambda item: (item.volume_id, item.copy_id, item.location),
        ):
            key = (copy.volume_id, copy.copy_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                FetchCopyHint(
                    id=CopyId(copy.copy_id),
                    volume_id=copy.volume_id,
                    location=copy.location,
                )
            )
    return out


def _fetch_payload(fetch_summary: FetchSummary) -> dict[str, object]:
    return {
        "id": str(fetch_summary.id),
        "state": fetch_summary.state.value,
        "copies": [
            {"id": str(copy.id), "volume_id": copy.volume_id, "location": copy.location}
            for copy in fetch_summary.copies
        ],
    }


def _reconcile_hot_from_pins(session: Session, hot_store: HotStore) -> None:
    active_targets = session.scalars(select(ActivePinRecord.target)).all()
    selected_paths: set[tuple[str, str]] = set()
    for raw_target in active_targets:
        for record in _selected_files(session, raw_target):
            selected_paths.add((record.collection_id, record.path))
    records = session.scalars(select(CollectionFileRecord)).all()
    for record in records:
        should_be_hot = (record.collection_id, record.path) in selected_paths
        if record.hot and not should_be_hot:
            hot_store.delete_collection_file(record.collection_id, record.path)
        record.hot = should_be_hot
