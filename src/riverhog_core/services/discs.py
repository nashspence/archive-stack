from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import delete, func, insert, or_, select
from sqlalchemy.orm import Session

from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveRestoreImageRecord,
    ArchiveRestoreRecord,
    DiscOperatorSummaryRecord,
    FileDiscRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    ImageDiscEventRecord,
    ImageDiscRecord,
)
from riverhog_core.domain.enums import DiscState, VerificationState
from riverhog_core.domain.errors import BadRequest, Conflict, InvalidState, NotFound
from riverhog_core.domain.models import DiscHistoryEntry, DiscSummary
from riverhog_core.domain.types import DiscId
from riverhog_core.durability import (
    disc_counts_toward_redundancy,
    normalize_disc_state,
    normalize_required_disc_count,
    normalize_verification_state,
)
from riverhog_core.finalized_image_coverage import group_disc_manifest_entries
from riverhog_core.ports.hot_store import HotStore
from riverhog_core.recovery_payloads import (
    CommandAgeBatchpassRecoveryPayloadCodec,
    RecoveryPayloadCodec,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_restores import ensure_archive_restore_for_image
from riverhog_core.webhooks import (
    WebhookConfig,
    build_disc_label_needed_payload,
    post_webhook,
    utcnow,
)

_REGISTERABLE_STATES = {DiscState.NEEDED, DiscState.BURNING}
_BATCH_SIZE = 1_000
_LOG = logging.getLogger(__name__)


class SqlAlchemyDiscService:
    def __init__(
        self,
        config: RuntimeConfig,
        hot_store: HotStore,
        recovery_payload_codec: RecoveryPayloadCodec | None = None,
    ) -> None:
        self._config = config
        self._hot_store = hot_store
        self._recovery_payload_codec = (
            recovery_payload_codec
            or CommandAgeBatchpassRecoveryPayloadCodec(
                command=config.recovery_payload_command,
                passphrase=config.recovery_payload_passphrase,
                work_factor=config.recovery_payload_work_factor,
                max_work_factor=config.recovery_payload_max_work_factor,
            )
        )
        self._session_factory = make_session_factory(config.database_url)

    def register(
        self,
        image_id: str,
        location: str,
        *,
        disc_id: str | None = None,
    ) -> DiscSummary:
        with session_scope(self._session_factory) as session:
            image = self._require_image(session, image_id)
            discs = self._ensure_required_disc_slots(session, image)
            target = self._resolve_registration_target(discs, requested_disc_id=disc_id)
            target.location = location
            target.state = DiscState.REGISTERED.value
            if target.verification_state is None:
                target.verification_state = VerificationState.PENDING.value
            self._sync_file_disc_rows(session, image, target)
            self._append_history(session, target, event="registered")
            session.flush()
            return self._disc_summary(session, target)

    def list_for_image(self, image_id: str) -> list[DiscSummary]:
        with session_scope(self._session_factory) as session:
            image = self._require_image(session, image_id)
            discs = self._ensure_required_disc_slots(session, image)
            session.flush()
            return [self._disc_summary(session, disc) for disc in discs]

    def list_discs(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        q: str | None,
        image_id: str | None,
    ) -> dict[str, object]:
        normalized_sort = _normalize_disc_sort(sort)
        normalized_order = _normalize_disc_order(order)
        with session_scope(self._session_factory) as session:
            if image_id is not None:
                image = self._require_image(session, image_id)
                self._ensure_required_disc_slots(session, image)
                session.flush()

            stmt = select(DiscOperatorSummaryRecord)
            filters = []
            if image_id is not None:
                filters.append(DiscOperatorSummaryRecord.image_id == image_id)
            if q:
                needle = q.casefold()
                filters.append(
                    or_(
                        func.lower(DiscOperatorSummaryRecord.disc_id).contains(needle),
                        func.lower(DiscOperatorSummaryRecord.image_id).contains(needle),
                        func.lower(DiscOperatorSummaryRecord.state).contains(needle),
                        func.lower(DiscOperatorSummaryRecord.verification_state).contains(needle),
                        func.lower(func.coalesce(DiscOperatorSummaryRecord.location, "")).contains(
                            needle
                        ),
                    )
                )
            if filters:
                stmt = stmt.where(*filters)

            total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            pages = (total + per_page - 1) // per_page if total else 0
            rows = session.scalars(
                stmt.order_by(*_disc_summary_order(sort=normalized_sort, order=normalized_order))
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).all()

        payload: dict[str, object] = {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": pages,
            "sort": normalized_sort,
            "order": normalized_order,
            "query": q,
            "discs": [_disc_summary_payload(row) for row in rows],
        }
        if image_id is not None:
            payload["image_id"] = image_id
        return payload

    def get_disc(self, disc_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            rows = session.scalars(
                select(DiscOperatorSummaryRecord)
                .where(DiscOperatorSummaryRecord.disc_id == disc_id)
                .order_by(DiscOperatorSummaryRecord.image_id.asc())
                .limit(2)
            ).all()
            if not rows:
                raise NotFound(f"disc not found: {disc_id}")
            if len(rows) > 1:
                raise BadRequest(f"disc id is ambiguous: {disc_id}")
            row = rows[0]
            history_rows = session.scalars(
                select(ImageDiscEventRecord)
                .where(
                    ImageDiscEventRecord.image_id == row.image_id,
                    ImageDiscEventRecord.disc_id == row.disc_id,
                )
                .order_by(ImageDiscEventRecord.id)
            ).all()
            return _disc_summary_payload(row, history_rows=history_rows)

    def notify_label_needed(self, image_id: str, disc_id: str) -> DiscSummary:
        with session_scope(self._session_factory) as session:
            image = self._require_image(session, image_id)
            self._ensure_required_disc_slots(session, image)
            target = session.get(ImageDiscRecord, {"image_id": image_id, "disc_id": disc_id})
            if target is None:
                raise NotFound(f"disc not found for image: {disc_id}")
            summary = self._disc_summary(session, target)

        if self._config.operator_webhook_url:
            try:
                webhook_config = WebhookConfig(
                    url=self._config.operator_webhook_url,
                    base_url=self._config.public_base_url or "",
                    timeout_seconds=self._config.operator_webhook_timeout.total_seconds(),
                )
                post_webhook(
                    config=webhook_config,
                    payload=build_disc_label_needed_payload(
                        config=webhook_config,
                        image_id=image_id,
                        disc_id=disc_id,
                        label_text=summary.label_text,
                        delivered_at=utcnow(),
                    ),
                )
            except Exception:
                _LOG.warning(
                    "failed to deliver disc label-needed webhook for %s",
                    disc_id,
                    exc_info=True,
                )
        return summary

    def update(
        self,
        image_id: str,
        disc_id: str,
        *,
        location: str | None = None,
        state: str | None = None,
        verification_state: str | None = None,
    ) -> DiscSummary:
        with session_scope(self._session_factory) as session:
            image = self._require_image(session, image_id)
            self._ensure_required_disc_slots(session, image)
            target = session.get(ImageDiscRecord, {"image_id": image_id, "disc_id": disc_id})
            if target is None:
                raise NotFound(f"disc not found for image: {disc_id}")

            previous_state = normalize_disc_state(target.state)
            location_changed = location is not None and location != target.location
            state_changed = False
            verification_changed = False

            if location is not None:
                target.location = location

            if state is not None:
                normalized_state = _parse_disc_state(state)
                state_changed = normalized_state.value != normalize_disc_state(target.state).value
                target.state = normalized_state.value

            if verification_state is not None:
                normalized_verification_state = _parse_verification_state(verification_state)
                verification_changed = (
                    normalized_verification_state.value
                    != normalize_verification_state(target.verification_state).value
                )
                target.verification_state = normalized_verification_state.value

            previous_counted = disc_counts_toward_redundancy(previous_state.value)
            next_counted = disc_counts_toward_redundancy(target.state)
            if location_changed or previous_counted != next_counted:
                self._sync_file_disc_rows(session, image, target)
            if location_changed or state_changed or verification_changed:
                self._append_history(
                    session,
                    target,
                    event=_history_event_name(
                        location_changed=location_changed,
                        state_changed=state_changed,
                        verification_changed=verification_changed,
                    ),
                )
            if _should_top_up_replacement_slots(
                previous_state=previous_state,
                next_state=target.state,
            ):
                self._ensure_required_disc_slots(session, image)
            session.flush()
            if disc_counts_toward_redundancy(previous_state.value) and target.state in {
                DiscState.LOST.value,
                DiscState.DAMAGED.value,
            }:
                ensure_archive_restore_for_image(
                    session,
                    config=self._config,
                    image_id=image_id,
                )
            return self._disc_summary(session, target)

    def _require_image(self, session: Session, image_id: str) -> FinalizedImageRecord:
        image = cast(FinalizedImageRecord | None, session.get(FinalizedImageRecord, image_id))
        if image is None:
            raise NotFound(f"image not found: {image_id}")
        return image

    def _ensure_required_disc_slots(
        self,
        session: Session,
        image: FinalizedImageRecord,
    ) -> list[ImageDiscRecord]:
        discs = list(
            session.scalars(
                select(ImageDiscRecord)
                .where(ImageDiscRecord.image_id == image.image_id)
                .order_by(ImageDiscRecord.disc_id)
            ).all()
        )
        required_disc_count = normalize_required_disc_count(image.required_disc_count)
        used_ids = {disc.disc_id for disc in discs}

        if not discs:
            while len(discs) < required_disc_count:
                discs.append(self._create_generated_disc_slot(session, image, used_ids=used_ids))
            return sorted(discs, key=lambda current: current.disc_id)

        active_slot_count = sum(1 for disc in discs if _disc_counts_toward_slot_pool(disc.state))
        disc_redundancy_count = sum(
            1 for disc in discs if disc_counts_toward_redundancy(disc.state)
        )
        if disc_redundancy_count > 0:
            while active_slot_count < required_disc_count:
                discs.append(self._create_generated_disc_slot(session, image, used_ids=used_ids))
                active_slot_count += 1
        elif _has_recoverable_session(session, image.image_id):
            while active_slot_count < 1:
                discs.append(self._create_generated_disc_slot(session, image, used_ids=used_ids))
                active_slot_count += 1
        return sorted(discs, key=lambda current: current.disc_id)

    def _create_generated_disc_slot(
        self,
        session: Session,
        image: FinalizedImageRecord,
        *,
        used_ids: set[str],
    ) -> ImageDiscRecord:
        ordinal = 1
        while True:
            candidate_id = _generated_disc_id(image.image_id, ordinal)
            ordinal += 1
            if candidate_id in used_ids:
                continue
            created_at = _utc_now()
            disc = ImageDiscRecord(
                image_id=image.image_id,
                disc_id=candidate_id,
                label_text=_label_text(candidate_id),
                location=None,
                created_at=created_at,
                state=DiscState.NEEDED.value,
                verification_state=VerificationState.PENDING.value,
            )
            session.add(disc)
            session.flush()
            self._append_history(session, disc, event="created")
            used_ids.add(candidate_id)
            return disc

    def _resolve_registration_target(
        self,
        discs: list[ImageDiscRecord],
        *,
        requested_disc_id: str | None,
    ) -> ImageDiscRecord:
        if requested_disc_id is None:
            for disc in discs:
                if normalize_disc_state(disc.state) in _REGISTERABLE_STATES:
                    return disc
            raise Conflict("all required disc slots are already registered")

        for disc in discs:
            if disc.disc_id != requested_disc_id:
                continue
            if normalize_disc_state(disc.state) not in _REGISTERABLE_STATES:
                raise Conflict(f"disc is not available for registration: {requested_disc_id}")
            return disc
        raise NotFound(f"disc not found for image: {requested_disc_id}")

    def _sync_file_disc_rows(
        self,
        session: Session,
        image: FinalizedImageRecord,
        disc: ImageDiscRecord,
    ) -> None:
        if not disc_counts_toward_redundancy(disc.state) or not disc.location:
            self._remove_file_disc_rows(session, image.image_id, disc.disc_id)
            return

        covered = session.scalars(
            select(FinalizedImageCoveredPathRecord).where(
                FinalizedImageCoveredPathRecord.image_id == image.image_id
            )
        ).all()
        coverage_parts = session.scalars(
            select(FinalizedImageCoveragePartRecord).where(
                FinalizedImageCoveragePartRecord.image_id == image.image_id
            )
        ).all()
        disc_entries = _disc_entries_from_coverage_parts(
            image.image_id,
            coverage_parts=coverage_parts,
        )
        enc_json = json.dumps(dict(self._recovery_payload_codec.metadata), sort_keys=True)
        recovery_bytes_by_disc_path: dict[str, int] = {}
        rows: list[dict[str, Any]] = []
        covered_paths_by_collection: dict[str, list[str]] = {}
        for covered_path in covered:
            covered_paths_by_collection.setdefault(covered_path.collection_id, []).append(
                covered_path.path
            )
            expected_entries = disc_entries.get((covered_path.collection_id, covered_path.path), [])
            for disc_path, part_index, part_count in expected_entries:
                recovery_bytes = recovery_bytes_by_disc_path.get(disc_path)
                if recovery_bytes is None:
                    recovery_bytes = _disc_recovery_bytes(image, disc_path)
                    recovery_bytes_by_disc_path[disc_path] = recovery_bytes
                rows.append(
                    {
                        "collection_id": covered_path.collection_id,
                        "path": covered_path.path,
                        "disc_id": disc.disc_id,
                        "image_id": image.image_id,
                        "location": disc.location,
                        "disc_path": disc_path,
                        "enc_json": enc_json,
                        "part_index": part_index,
                        "part_count": part_count if part_count > 1 else None,
                        "part_bytes": None,
                        "part_sha256": None,
                        "recovery_bytes": recovery_bytes,
                        "recovery_sha256": None,
                    }
                )

        session.execute(
            delete(FileDiscRecord).where(
                FileDiscRecord.image_id == image.image_id,
                FileDiscRecord.disc_id == disc.disc_id,
            )
        )
        for row_batch in _batches(rows, _BATCH_SIZE):
            session.execute(insert(FileDiscRecord), row_batch)

    def _remove_file_disc_rows(self, session: Session, image_id: str, disc_id: str) -> None:
        session.execute(
            delete(FileDiscRecord).where(
                FileDiscRecord.image_id == image_id,
                FileDiscRecord.disc_id == disc_id,
            )
        )

    def _append_history(
        self,
        session: Session,
        disc: ImageDiscRecord,
        *,
        event: str,
    ) -> None:
        session.add(
            ImageDiscEventRecord(
                image_id=disc.image_id,
                disc_id=disc.disc_id,
                occurred_at=_utc_now(),
                event=event,
                state=normalize_disc_state(disc.state).value,
                verification_state=normalize_verification_state(disc.verification_state).value,
                location=disc.location,
            )
        )

    def _disc_summary(self, session: Session, disc: ImageDiscRecord) -> DiscSummary:
        history_rows = session.scalars(
            select(ImageDiscEventRecord)
            .where(
                ImageDiscEventRecord.image_id == disc.image_id,
                ImageDiscEventRecord.disc_id == disc.disc_id,
            )
            .order_by(ImageDiscEventRecord.id)
        ).all()
        return DiscSummary(
            disc_id=DiscId(disc.disc_id),
            image_id=disc.image_id,
            label_text=disc.label_text,
            location=disc.location,
            created_at=disc.created_at,
            state=normalize_disc_state(disc.state),
            verification_state=normalize_verification_state(disc.verification_state),
            history=tuple(
                DiscHistoryEntry(
                    at=row.occurred_at,
                    event=row.event,
                    state=normalize_disc_state(row.state),
                    verification_state=normalize_verification_state(row.verification_state),
                    location=row.location,
                )
                for row in history_rows
            ),
        )


def _generated_disc_id(image_id: str, ordinal: int) -> str:
    return f"{image_id}-{ordinal}"


def _has_recoverable_session(session: Session, image_id: str) -> bool:
    recoverable_states = {"requested", "ready", "expired"}
    restore_id = session.scalar(
        select(ArchiveRestoreRecord.restore_id)
        .join(
            ArchiveRestoreImageRecord,
            ArchiveRestoreImageRecord.restore_id == ArchiveRestoreRecord.restore_id,
        )
        .where(
            ArchiveRestoreImageRecord.image_id == image_id,
            ArchiveRestoreRecord.state.in_(recoverable_states),
        )
        .order_by(ArchiveRestoreRecord.created_at.desc())
        .limit(1)
    )
    return restore_id is not None


def _label_text(disc_id: str) -> str:
    return disc_id


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _history_event_name(
    *,
    location_changed: bool,
    state_changed: bool,
    verification_changed: bool,
) -> str:
    changed = sum(int(flag) for flag in (location_changed, state_changed, verification_changed))
    if changed > 1:
        return "updated"
    if location_changed:
        return "location_updated"
    if state_changed:
        return "state_updated"
    if verification_changed:
        return "verification_updated"
    return "updated"


def _disc_counts_toward_slot_pool(state: str | None) -> bool:
    normalized = normalize_disc_state(state)
    return normalized in _REGISTERABLE_STATES or disc_counts_toward_redundancy(normalized.value)


def _batches[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _should_top_up_replacement_slots(*, previous_state: DiscState, next_state: str | None) -> bool:
    return disc_counts_toward_redundancy(previous_state.value) and normalize_disc_state(
        next_state
    ) in {
        DiscState.LOST,
        DiscState.DAMAGED,
    }


def _parse_disc_state(raw_state: str) -> DiscState:
    try:
        return DiscState(raw_state)
    except ValueError as exc:
        raise BadRequest(f"invalid disc state: {raw_state}") from exc


def _parse_verification_state(raw_state: str) -> VerificationState:
    try:
        return VerificationState(raw_state)
    except ValueError as exc:
        raise BadRequest(f"invalid verification state: {raw_state}") from exc


def _disc_entries_from_coverage_parts(
    image_id: str,
    *,
    coverage_parts: Sequence[FinalizedImageCoveragePartRecord],
) -> dict[tuple[str, str], list[tuple[str, int | None, int]]]:
    try:
        grouped = group_disc_manifest_entries(coverage_parts)
    except ValueError as exc:
        raise InvalidState(f"incomplete finalized-image artifact mapping for {image_id}") from exc

    result: dict[tuple[str, str], list[tuple[str, int | None, int]]] = {}
    for key, parts in grouped.items():
        result[key] = [
            (object_path, None if part_count == 1 else part_index, part_count)
            for object_path, part_index, part_count, _sidecar_path in parts
        ]
    return result


def _disc_recovery_bytes(
    image: FinalizedImageRecord,
    disc_path: str,
) -> int:
    payload_path = Path(image.image_root) / disc_path.lstrip("/")
    try:
        return payload_path.stat().st_size
    except FileNotFoundError as exc:
        raise InvalidState(f"finalized-image payload is missing: {payload_path}") from exc


def _normalize_disc_sort(sort: str) -> str:
    if sort not in _DISC_SORT_COLUMNS:
        raise BadRequest(f"sort must be one of {', '.join(sorted(_DISC_SORT_COLUMNS))}")
    return sort


def _normalize_disc_order(order: str) -> str:
    normalized = order.casefold()
    if normalized not in {"asc", "desc"}:
        raise BadRequest("order must be asc or desc")
    return normalized


_DISC_SORT_COLUMNS = {
    "disc_id": (DiscOperatorSummaryRecord.disc_id, DiscOperatorSummaryRecord.image_id),
    "image_id": (DiscOperatorSummaryRecord.image_id, DiscOperatorSummaryRecord.disc_id),
    "state": (DiscOperatorSummaryRecord.state, DiscOperatorSummaryRecord.disc_id),
    "verification_state": (
        DiscOperatorSummaryRecord.verification_state,
        DiscOperatorSummaryRecord.disc_id,
    ),
    "location": (DiscOperatorSummaryRecord.location, DiscOperatorSummaryRecord.disc_id),
}


def _disc_summary_order(*, sort: str, order: str) -> list[Any]:
    columns = _DISC_SORT_COLUMNS[sort]
    if order == "desc":
        return [column.desc() for column in columns]
    return [column.asc() for column in columns]


def _disc_summary_payload(
    row: DiscOperatorSummaryRecord,
    *,
    history_rows: Sequence[ImageDiscEventRecord] = (),
) -> dict[str, object]:
    return {
        "disc_id": row.disc_id,
        "image_id": row.image_id,
        "filename": row.filename,
        "label_text": row.label_text,
        "location": row.location,
        "created_at": row.created_at,
        "state": normalize_disc_state(row.state).value,
        "verification_state": normalize_verification_state(row.verification_state).value,
        "history": [
            {
                "at": history.occurred_at,
                "event": history.event,
                "state": normalize_disc_state(history.state).value,
                "verification_state": normalize_verification_state(
                    history.verification_state
                ).value,
                "location": history.location,
            }
            for history in history_rows
        ],
    }
