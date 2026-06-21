from __future__ import annotations

from riverhog_core.catalog_models import FetchOperatorSummaryRecord
from riverhog_core.domain.enums import FetchState
from riverhog_core.domain.models import FetchSummary
from riverhog_core.domain.types import FetchId, TargetStr


def fetch_summary_from_projection(
    row: FetchOperatorSummaryRecord,
    *,
    prefer_entries: bool,
) -> FetchSummary:
    state = FetchState(row.fetch_state)
    targets = tuple(
        TargetStr(target) for target in str(row.targets_text or "").splitlines() if target.strip()
    )
    if prefer_entries and state != FetchState.DONE and row.entries_total > 0:
        return FetchSummary(
            id=FetchId(row.fetch_id),
            name=row.name,
            targets=targets,
            state=state,
            files=int(row.entries_total or 0),
            bytes=int(row.entry_bytes or 0),
            copies=[],
            entries_total=int(row.entries_total or 0),
            entries_pending=int(row.entries_pending or 0),
            entries_partial=int(row.entries_partial or 0),
            entries_byte_complete=int(row.entries_byte_complete or 0),
            entries_uploaded=int(row.entries_uploaded or 0),
            uploaded_bytes=int(row.uploaded_bytes or 0),
            missing_bytes=int(row.upload_missing_bytes or 0),
            upload_state_expires_at=row.upload_state_expires_at,
        )
    return FetchSummary(
        id=FetchId(row.fetch_id),
        name=row.name,
        targets=targets,
        state=state,
        files=int(row.files or 0),
        bytes=int(row.bytes or 0),
        copies=[],
        entries_total=int(row.files or 0),
        entries_pending=int(row.missing_files or 0),
        entries_partial=0,
        entries_byte_complete=0,
        entries_uploaded=int(row.hot_files or 0),
        uploaded_bytes=int(row.hot_bytes or 0),
        missing_bytes=int(row.missing_bytes or 0),
        upload_state_expires_at=None,
    )
