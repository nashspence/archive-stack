from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from arc_core.domain.errors import NotYetImplemented
from arc_core.services.planning import (
    ImageRootPlanningService,
    ImageRootRecord,
)


def test_image_root_planning_service_delegates_lookups_and_stream_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[Path, str, str]] = []

    async def fake_stream_iso_from_root(
        *,
        image_root: Path,
        volume_id: str,
        filename: str,
    ) -> object:
        calls.append((image_root, volume_id, filename))
        return {"filename": filename}

    record = ImageRootRecord(
        image_id="img_001",
        volume_id="ARC-IMG-001",
        filename="img_001.iso",
        image_root=tmp_path / "image-root",
    )
    service = ImageRootPlanningService(
        image_lookup=lambda image_id: record if image_id == "img_001" else None,
        list_lookup=lambda **kwargs: {"images": [], **kwargs},
        plan_lookup=lambda **kwargs: {"ready": True, **kwargs},
        finalize_lookup=lambda image_id: {"id": image_id, "volume_id": record.volume_id},
    )

    monkeypatch.setattr(
        "arc_core.services.planning.stream_iso_from_root",
        fake_stream_iso_from_root,
    )

    assert service.get_plan(
        page=1,
        per_page=25,
        sort="fill",
        order="desc",
        q=None,
        collection=None,
        iso_ready=None,
    ) == {
        "ready": True,
        "page": 1,
        "per_page": 25,
        "sort": "fill",
        "order": "desc",
        "q": None,
        "collection": None,
        "iso_ready": None,
    }
    assert service.list_images(
        page=1,
        per_page=25,
        sort="finalized_at",
        order="desc",
        q=None,
        collection=None,
        has_copies=None,
    ) == {
        "images": [],
        "page": 1,
        "per_page": 25,
        "sort": "finalized_at",
        "order": "desc",
        "q": None,
        "collection": None,
        "has_copies": None,
    }
    assert service.get_image("img_001") is record
    assert service.finalize_image("img_001") == {"id": "img_001", "volume_id": record.volume_id}
    assert asyncio.run(service.get_iso_stream("img_001")) == {"filename": "img_001.iso"}
    assert calls == [(record.image_root, record.volume_id, record.filename)]


def test_image_root_planning_service_rejects_non_image_root_records() -> None:
    service = ImageRootPlanningService(
        image_lookup=lambda _: {"image_id": "img_001"},
        plan_lookup=lambda **kwargs: {"ready": True, **kwargs},
    )

    with pytest.raises(TypeError, match="ImageRootRecord"):
        asyncio.run(service.get_iso_stream("img_001"))


def test_image_root_planning_service_requires_finalize_lookup_when_finalizing() -> None:
    service = ImageRootPlanningService(
        image_lookup=lambda _: {"image_id": "img_001"},
        plan_lookup=lambda **kwargs: {"ready": True, **kwargs},
    )

    with pytest.raises(NotYetImplemented, match="finalize_image is not configured"):
        service.finalize_image("img_001")


def test_image_root_planning_service_requires_list_lookup_when_listing_images() -> None:
    service = ImageRootPlanningService(
        image_lookup=lambda _: {"image_id": "img_001"},
        plan_lookup=lambda **kwargs: {"ready": True, **kwargs},
    )

    with pytest.raises(NotYetImplemented, match="list_images is not configured"):
        service.list_images(
            page=1,
            per_page=25,
            sort="finalized_at",
            order="desc",
            q=None,
            collection=None,
            has_copies=None,
        )
