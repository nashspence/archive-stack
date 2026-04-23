from __future__ import annotations

from arc_core.domain.models import CollectionSummary, CopySummary, FetchSummary, PinSummary


def map_collection(summary: CollectionSummary) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "files": summary.files,
        "bytes": summary.bytes,
        "hot_bytes": summary.hot_bytes,
        "archived_bytes": summary.archived_bytes,
        "pending_bytes": summary.pending_bytes,
    }


def map_copy(summary: CopySummary) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "volume_id": summary.volume_id,
        "location": summary.location,
        "created_at": summary.created_at,
    }


def map_fetch(summary: FetchSummary) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "target": str(summary.target),
        "state": summary.state.value,
        "files": summary.files,
        "bytes": summary.bytes,
        "entries_total": summary.entries_total,
        "entries_pending": summary.entries_pending,
        "entries_partial": summary.entries_partial,
        "entries_uploaded": summary.entries_uploaded,
        "uploaded_bytes": summary.uploaded_bytes,
        "missing_bytes": summary.missing_bytes,
        "upload_state_expires_at": summary.upload_state_expires_at,
        "copies": [
            {"id": str(c.id), "volume_id": c.volume_id, "location": c.location}
            for c in summary.copies
        ],
    }


def map_pin(summary: PinSummary) -> dict[str, object]:
    return {
        "target": str(summary.target),
        "fetch": {
            "id": str(summary.fetch.id),
            "state": summary.fetch.state.value,
            "copies": [
                {"id": str(copy.id), "volume_id": copy.volume_id, "location": copy.location}
                for copy in summary.fetch.copies
            ],
        },
    }
