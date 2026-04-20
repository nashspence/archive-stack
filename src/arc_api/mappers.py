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
        "image": str(summary.image),
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
        "copies": [{"id": str(c.id), "location": c.location} for c in summary.copies],
    }


def map_pin(summary: PinSummary) -> dict[str, str]:
    return {"target": str(summary.target)}
