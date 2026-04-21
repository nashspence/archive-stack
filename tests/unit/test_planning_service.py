from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from arc_core.domain.errors import NotYetImplemented
from arc_core.services.planning import (
    ImageRootPlanningService,
    ImageRootRecord,
    StubPlanningService,
)


def test_stub_planning_service_raises_not_yet_implemented_for_every_entrypoint() -> None:
    service = StubPlanningService()

    with pytest.raises(NotYetImplemented, match="StubPlanningService is not implemented yet"):
        service.get_plan()

    with pytest.raises(NotYetImplemented, match="StubPlanningService is not implemented yet"):
        service.get_image("img_001")

    with pytest.raises(NotYetImplemented, match="StubPlanningService is not implemented yet"):
        service.get_iso_stream("img_001")


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
        plan_lookup=lambda: {"ready": True},
    )

    monkeypatch.setattr(
        "arc_core.services.planning.stream_iso_from_root",
        fake_stream_iso_from_root,
    )

    assert service.get_plan() == {"ready": True}
    assert service.get_image("img_001") is record
    assert asyncio.run(service.get_iso_stream("img_001")) == {"filename": "img_001.iso"}
    assert calls == [(record.image_root, record.volume_id, record.filename)]


def test_image_root_planning_service_rejects_non_image_root_records() -> None:
    service = ImageRootPlanningService(
        image_lookup=lambda _: {"image_id": "img_001"},
        plan_lookup=lambda: {"ready": True},
    )

    with pytest.raises(TypeError, match="ImageRootRecord"):
        asyncio.run(service.get_iso_stream("img_001"))
