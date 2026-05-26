from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from riverhog_core.domain.errors import NotYetImplemented
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.planning import (
    ImageRootPlanningService,
    ImageRootRecord,
    _build_plan_piece_groups,
    _PlanFile,
)


def _runtime_config(tmp_path: Path, **overrides: object) -> RuntimeConfig:
    return RuntimeConfig(
        object_store="s3",
        s3_endpoint_url="http://example.invalid:9000",
        s3_region="us-east-1",
        s3_bucket="riverhog",
        s3_access_key_id="test-access",
        s3_secret_access_key="test-secret",
        s3_force_path_style=True,
        tusd_base_url="http://example.invalid:1080/files",
        tusd_hook_secret="hook-secret",
        sqlite_path=tmp_path / "state.sqlite3",
        **overrides,
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
        volume_id="RIVERHOG-IMG-001",
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
        "riverhog_core.services.planning.stream_iso_from_root",
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


def test_planner_starts_large_collection_on_fresh_disc_to_minimize_splits(
    tmp_path: Path,
) -> None:
    config = _runtime_config(
        tmp_path,
        planner_disc_target_bytes=1_000_000,
        planner_min_fill_bytes=1,
        planner_image_root=tmp_path / "images",
    )
    plan_files = [
        _PlanFile(
            collection_id="2026/20260101T000000Z__alpha",
            path="a.bin",
            bytes=400_000,
            sha256="a" * 64,
        ),
        *[
            _PlanFile(
                collection_id="2026/20260102T000000Z__bravo",
                path=f"{index:02d}.bin",
                bytes=100_000,
                sha256=f"{index:064x}"[-64:],
            )
            for index in range(14)
        ],
    ]

    groups = _build_plan_piece_groups(plan_files, config)
    group_collections = [{piece.collection_id for piece in group} for group in groups]

    assert {"2026/20260101T000000Z__alpha"} in group_collections
    assert [
        collections
        for collections in group_collections
        if "2026/20260102T000000Z__bravo" in collections
    ] == [
        {"2026/20260102T000000Z__bravo"},
        {"2026/20260102T000000Z__bravo"},
    ]


def test_planner_uses_leftover_space_when_collection_disc_count_is_unchanged(
    tmp_path: Path,
) -> None:
    config = _runtime_config(
        tmp_path,
        planner_disc_target_bytes=1_000_000,
        planner_min_fill_bytes=1,
        planner_image_root=tmp_path / "images",
    )
    plan_files = [
        _PlanFile(
            collection_id="2026/20260101T000000Z__alpha",
            path="a.bin",
            bytes=100_000,
            sha256="a" * 64,
        ),
        *[
            _PlanFile(
                collection_id="2026/20260102T000000Z__bravo",
                path=f"{index:02d}.bin",
                bytes=100_000,
                sha256=f"{index:064x}"[-64:],
            )
            for index in range(12)
        ],
    ]

    groups = _build_plan_piece_groups(plan_files, config)
    group_collections = [{piece.collection_id for piece in group} for group in groups]

    assert {
        "2026/20260101T000000Z__alpha",
        "2026/20260102T000000Z__bravo",
    } in group_collections
    assert sum("2026/20260102T000000Z__bravo" in group for group in group_collections) == 2


def test_planner_reorders_collections_to_pack_candidates_better(tmp_path: Path) -> None:
    config = _runtime_config(
        tmp_path,
        planner_disc_target_bytes=1_000_000,
        planner_min_fill_bytes=1,
        planner_image_root=tmp_path / "images",
    )
    plan_files = [
        _PlanFile(
            collection_id="2026/20260101T000000Z__alpha-small",
            path="payload.bin",
            bytes=400_000,
            sha256="a" * 64,
        ),
        _PlanFile(
            collection_id="2026/20260102T000000Z__bravo-small",
            path="payload.bin",
            bytes=400_000,
            sha256="b" * 64,
        ),
        _PlanFile(
            collection_id="2026/20260103T000000Z__charlie-large",
            path="payload.bin",
            bytes=500_000,
            sha256="c" * 64,
        ),
        _PlanFile(
            collection_id="2026/20260104T000000Z__delta-large",
            path="payload.bin",
            bytes=500_000,
            sha256="d" * 64,
        ),
    ]

    groups = _build_plan_piece_groups(plan_files, config)
    group_slugs = [
        {piece.collection_id.rsplit("__", maxsplit=1)[1] for piece in group} for group in groups
    ]

    assert len(groups) == 2
    assert {"alpha-small", "charlie-large"} in group_slugs
    assert {"bravo-small", "delta-large"} in group_slugs
