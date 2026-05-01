from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from arc_core.archive_compliance import (
    copy_counts_toward_protection,
    normalize_copy_state,
    normalize_required_copy_count,
    normalize_verification_state,
)
from arc_core.catalog_models import (
    CollectionFileRecord,
    FileCopyRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    GlacierRecoverySessionImageRecord,
    GlacierRecoverySessionRecord,
    ImageCopyEventRecord,
    ImageCopyRecord,
)
from arc_core.domain.enums import CopyState, VerificationState
from arc_core.domain.errors import BadRequest, Conflict, InvalidState, NotFound
from arc_core.domain.models import CopyHistoryEntry, CopySummary
from arc_core.domain.types import CopyId
from arc_core.finalized_image_coverage import group_disc_manifest_entries
from arc_core.ports.hot_store import HotStore
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.recovery_sessions import ensure_glacier_recovery_session_for_image
from arc_core.sqlite_db import make_session_factory, session_scope

_ENC_JSON = json.dumps({"alg": "fixture-age-plugin-batchpass/v1"}, sort_keys=True)
_REGISTERABLE_STATES = {CopyState.NEEDED, CopyState.BURNING}


class SqlAlchemyCopyService:
    def __init__(self, config: RuntimeConfig, hot_store: HotStore) -> None:
        self._config = config
        self._hot_store = hot_store
        self._session_factory = make_session_factory(str(config.sqlite_path))

    def register(
        self,
        image_id: str,
        location: str,
        *,
        copy_id: str | None = None,
    ) -> CopySummary:
        with session_scope(self._session_factory) as session:
            image = self._require_image(session, image_id)
            copies = self._ensure_required_copy_slots(session, image)
            target = self._resolve_registration_target(copies, requested_copy_id=copy_id)
            target.location = location
            target.state = CopyState.REGISTERED.value
            if target.verification_state is None:
                target.verification_state = VerificationState.PENDING.value
            self._sync_file_copy_rows(session, image, target)
            self._append_history(session, target, event="registered")
            session.flush()
            return self._copy_summary(session, target)

    def list_for_image(self, image_id: str) -> list[CopySummary]:
        with session_scope(self._session_factory) as session:
            image = self._require_image(session, image_id)
            copies = self._ensure_required_copy_slots(session, image)
            session.flush()
            return [self._copy_summary(session, copy) for copy in copies]

    def update(
        self,
        image_id: str,
        copy_id: str,
        *,
        location: str | None = None,
        state: str | None = None,
        verification_state: str | None = None,
    ) -> CopySummary:
        with session_scope(self._session_factory) as session:
            image = self._require_image(session, image_id)
            self._ensure_required_copy_slots(session, image)
            target = session.get(ImageCopyRecord, {"image_id": image_id, "copy_id": copy_id})
            if target is None:
                raise NotFound(f"copy not found for image: {copy_id}")

            previous_state = normalize_copy_state(target.state)
            location_changed = location is not None and location != target.location
            state_changed = False
            verification_changed = False

            if location is not None:
                target.location = location

            if state is not None:
                normalized_state = _parse_copy_state(state)
                state_changed = normalized_state.value != normalize_copy_state(target.state).value
                target.state = normalized_state.value

            if verification_state is not None:
                normalized_verification_state = _parse_verification_state(verification_state)
                verification_changed = (
                    normalized_verification_state.value
                    != normalize_verification_state(target.verification_state).value
                )
                target.verification_state = normalized_verification_state.value

            self._sync_file_copy_rows(session, image, target)
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
                self._ensure_required_copy_slots(session, image)
            session.flush()
            if copy_counts_toward_protection(previous_state.value) and target.state in {
                CopyState.LOST.value,
                CopyState.DAMAGED.value,
            }:
                ensure_glacier_recovery_session_for_image(
                    session,
                    config=self._config,
                    image_id=image_id,
                )
            return self._copy_summary(session, target)

    def _require_image(self, session: Session, image_id: str) -> FinalizedImageRecord:
        image = cast(FinalizedImageRecord | None, session.get(FinalizedImageRecord, image_id))
        if image is None:
            raise NotFound(f"image not found: {image_id}")
        return image

    def _ensure_required_copy_slots(
        self,
        session: Session,
        image: FinalizedImageRecord,
    ) -> list[ImageCopyRecord]:
        copies = list(
            session.scalars(
                select(ImageCopyRecord)
                .where(ImageCopyRecord.image_id == image.image_id)
                .order_by(ImageCopyRecord.copy_id)
            ).all()
        )
        required_copy_count = normalize_required_copy_count(image.required_copy_count)
        used_ids = {copy.copy_id for copy in copies}

        if not copies:
            while len(copies) < required_copy_count:
                copies.append(self._create_generated_copy_slot(session, image, used_ids=used_ids))
            return sorted(copies, key=lambda current: current.copy_id)

        active_slot_count = sum(1 for copy in copies if _copy_counts_toward_slot_pool(copy.state))
        protected_copy_count = sum(
            1 for copy in copies if copy_counts_toward_protection(copy.state)
        )
        if protected_copy_count > 0:
            while active_slot_count < required_copy_count:
                copies.append(self._create_generated_copy_slot(session, image, used_ids=used_ids))
                active_slot_count += 1
        elif _has_recoverable_session(session, image.image_id):
            while active_slot_count < 1:
                copies.append(self._create_generated_copy_slot(session, image, used_ids=used_ids))
                active_slot_count += 1
        return sorted(copies, key=lambda current: current.copy_id)

    def _create_generated_copy_slot(
        self,
        session: Session,
        image: FinalizedImageRecord,
        *,
        used_ids: set[str],
    ) -> ImageCopyRecord:
        ordinal = 1
        while True:
            candidate_id = _generated_copy_id(image.image_id, ordinal)
            ordinal += 1
            if candidate_id in used_ids:
                continue
            created_at = _utc_now()
            copy = ImageCopyRecord(
                image_id=image.image_id,
                copy_id=candidate_id,
                label_text=_label_text(candidate_id),
                location=None,
                created_at=created_at,
                state=CopyState.NEEDED.value,
                verification_state=VerificationState.PENDING.value,
            )
            session.add(copy)
            session.flush()
            self._append_history(session, copy, event="created")
            used_ids.add(candidate_id)
            return copy

    def _resolve_registration_target(
        self,
        copies: list[ImageCopyRecord],
        *,
        requested_copy_id: str | None,
    ) -> ImageCopyRecord:
        if requested_copy_id is None:
            for copy in copies:
                if normalize_copy_state(copy.state) in _REGISTERABLE_STATES:
                    return copy
            raise Conflict("all required copy slots are already registered")

        for copy in copies:
            if copy.copy_id != requested_copy_id:
                continue
            if normalize_copy_state(copy.state) not in _REGISTERABLE_STATES:
                raise Conflict(f"copy is not available for registration: {requested_copy_id}")
            return copy
        raise NotFound(f"copy not found for image: {requested_copy_id}")

    def _sync_file_copy_rows(
        self,
        session: Session,
        image: FinalizedImageRecord,
        copy: ImageCopyRecord,
    ) -> None:
        if not copy_counts_toward_protection(copy.state) or not copy.location:
            self._remove_file_copy_rows(session, image.image_id, copy.copy_id)
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
        for covered_path in covered:
            file_record = session.get(
                CollectionFileRecord,
                {"collection_id": covered_path.collection_id, "path": covered_path.path},
            )
            if file_record is None:
                continue
            file_record.archived = True
            expected_entries = disc_entries.get((covered_path.collection_id, covered_path.path), [])
            existing_rows = session.scalars(
                select(FileCopyRecord).where(
                    FileCopyRecord.collection_id == covered_path.collection_id,
                    FileCopyRecord.path == covered_path.path,
                    FileCopyRecord.volume_id == image.image_id,
                    FileCopyRecord.copy_id == copy.copy_id,
                )
            ).all()
            existing_by_key = {
                (row.disc_path, row.part_index, row.part_count): row for row in existing_rows
            }
            expected_keys: set[tuple[str, int | None, int | None]] = set()
            for disc_path, part_index, part_count in expected_entries:
                expected_keys.add((disc_path, part_index, part_count if part_count > 1 else None))
                row = existing_by_key.get(
                    (disc_path, part_index, part_count if part_count > 1 else None)
                )
                if row is None:
                    part_bytes_val = None
                    part_sha256_val = None
                    if part_count > 1:
                        assert part_index is not None
                        content = self._hot_store.get_collection_file(
                            covered_path.collection_id, covered_path.path
                        )
                        parts = _split_plaintext(content, part_count)
                        part_bytes_val = len(parts[part_index])
                        part_sha256_val = hashlib.sha256(parts[part_index]).hexdigest()
                    session.add(
                        FileCopyRecord(
                            collection_id=covered_path.collection_id,
                            path=covered_path.path,
                            copy_id=copy.copy_id,
                            volume_id=image.image_id,
                            location=copy.location,
                            disc_path=disc_path,
                            enc_json=_ENC_JSON,
                            part_index=part_index,
                            part_count=part_count if part_count > 1 else None,
                            part_bytes=part_bytes_val,
                            part_sha256=part_sha256_val,
                        )
                    )
                    continue
                row.location = copy.location

            for row in existing_rows:
                row_key = (row.disc_path, row.part_index, row.part_count)
                if row_key not in expected_keys:
                    session.delete(row)

    def _remove_file_copy_rows(self, session: Session, image_id: str, copy_id: str) -> None:
        impacted_rows = session.scalars(
            select(FileCopyRecord).where(
                FileCopyRecord.volume_id == image_id,
                FileCopyRecord.copy_id == copy_id,
            )
        ).all()
        impacted_files = {(row.collection_id, row.path) for row in impacted_rows}
        session.execute(
            delete(FileCopyRecord).where(
                FileCopyRecord.volume_id == image_id,
                FileCopyRecord.copy_id == copy_id,
            )
        )
        for collection_id, path in impacted_files:
            file_record = session.get(
                CollectionFileRecord, {"collection_id": collection_id, "path": path}
            )
            if file_record is None:
                continue
            remaining = session.scalar(
                select(FileCopyRecord.id)
                .where(
                    FileCopyRecord.collection_id == collection_id,
                    FileCopyRecord.path == path,
                )
                .limit(1)
            )
            file_record.archived = remaining is not None

    def _append_history(
        self,
        session: Session,
        copy: ImageCopyRecord,
        *,
        event: str,
    ) -> None:
        session.add(
            ImageCopyEventRecord(
                image_id=copy.image_id,
                copy_id=copy.copy_id,
                occurred_at=_utc_now(),
                event=event,
                state=normalize_copy_state(copy.state).value,
                verification_state=normalize_verification_state(copy.verification_state).value,
                location=copy.location,
            )
        )

    def _copy_summary(self, session: Session, copy: ImageCopyRecord) -> CopySummary:
        history_rows = session.scalars(
            select(ImageCopyEventRecord)
            .where(
                ImageCopyEventRecord.image_id == copy.image_id,
                ImageCopyEventRecord.copy_id == copy.copy_id,
            )
            .order_by(ImageCopyEventRecord.id)
        ).all()
        return CopySummary(
            id=CopyId(copy.copy_id),
            volume_id=copy.image_id,
            label_text=copy.label_text,
            location=copy.location,
            created_at=copy.created_at,
            state=normalize_copy_state(copy.state),
            verification_state=normalize_verification_state(copy.verification_state),
            history=tuple(
                CopyHistoryEntry(
                    at=row.occurred_at,
                    event=row.event,
                    state=normalize_copy_state(row.state),
                    verification_state=normalize_verification_state(row.verification_state),
                    location=row.location,
                )
                for row in history_rows
            ),
        )


def _generated_copy_id(image_id: str, ordinal: int) -> str:
    return f"{image_id}-{ordinal}"


def _has_recoverable_session(session: Session, image_id: str) -> bool:
    recoverable_states = {"restore_requested", "ready", "expired"}
    session_id = session.scalar(
        select(GlacierRecoverySessionRecord.session_id)
        .join(
            GlacierRecoverySessionImageRecord,
            GlacierRecoverySessionImageRecord.session_id == GlacierRecoverySessionRecord.session_id,
        )
        .where(
            GlacierRecoverySessionImageRecord.image_id == image_id,
            GlacierRecoverySessionRecord.state.in_(recoverable_states),
        )
        .order_by(GlacierRecoverySessionRecord.created_at.desc())
        .limit(1)
    )
    return session_id is not None


def _label_text(copy_id: str) -> str:
    return copy_id


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


def _copy_counts_toward_slot_pool(state: str | None) -> bool:
    normalized = normalize_copy_state(state)
    return normalized in _REGISTERABLE_STATES or copy_counts_toward_protection(normalized.value)


def _should_top_up_replacement_slots(*, previous_state: CopyState, next_state: str | None) -> bool:
    return copy_counts_toward_protection(previous_state.value) and normalize_copy_state(
        next_state
    ) in {
        CopyState.LOST,
        CopyState.DAMAGED,
    }


def _parse_copy_state(raw_state: str) -> CopyState:
    try:
        return CopyState(raw_state)
    except ValueError as exc:
        raise BadRequest(f"invalid copy state: {raw_state}") from exc


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


def _split_plaintext(content: bytes, part_count: int) -> tuple[bytes, ...]:
    base, remainder = divmod(len(content), part_count)
    offset = 0
    parts: list[bytes] = []
    for i in range(part_count):
        size = base + int(i < remainder)
        parts.append(content[offset : offset + size])
        offset += size
    return tuple(parts)
