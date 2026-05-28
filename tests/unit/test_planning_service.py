from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from riverhog_core.crypto_age import encrypted_size_for_plaintext_size
from riverhog_core.domain.errors import NotYetImplemented
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.planning import (
    ImageRootPlanningService,
    ImageRootRecord,
    _build_plan_piece_groups,
    _candidate_metadata_pad,
    _CandidateSpec,
    _CollectionPieceGroup,
    _estimated_encrypted_leaf_size,
    _pack_collection_piece_groups,
    _PlanFile,
    _planner_refresh_file_lock,
    _PlanPiece,
    _saturation_release_candidate_id,
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


def _piece(collection_id: str, path: str, estimated_bytes: int) -> _PlanPiece:
    sha256 = (collection_id + path).encode("utf-8").hex().rjust(64, "0")[-64:]
    return _PlanPiece(
        collection_id=collection_id,
        path=path,
        file_id=f"{collection_id}\0{path}",
        bytes=estimated_bytes,
        sha256=sha256,
        offset=0,
        plaintext_bytes=estimated_bytes,
        part_index=0,
        part_count=1,
        estimated_payload_bytes=estimated_bytes,
        estimated_sidecar_bytes=0,
    )


def _group(
    collection_id: str,
    estimated_piece_bytes: list[int],
    *,
    artifact_estimate: int = 0,
) -> _CollectionPieceGroup:
    pieces = tuple(
        _piece(collection_id, f"{index:02d}.bin", estimated_bytes)
        for index, estimated_bytes in enumerate(estimated_piece_bytes)
    )
    return _CollectionPieceGroup(
        collection_id=collection_id,
        pieces=pieces,
        estimated_bytes=artifact_estimate + sum(piece.estimated_total_bytes for piece in pieces),
        artifact_estimate=artifact_estimate,
    )


def _collection_disc_count(
    groups: list[list[_PlanPiece]],
    collection_id: str,
) -> int:
    return sum(any(piece.collection_id == collection_id for piece in group) for group in groups)


def _spec(candidate_id: str, estimated_bytes: int) -> _CandidateSpec:
    return _CandidateSpec(
        candidate_id=candidate_id,
        plan_fingerprint=f"fingerprint-{candidate_id}",
        finalized_id=f"image-{candidate_id}",
        estimated_bytes=estimated_bytes,
        pieces=(),
    )


def test_planner_uses_tight_file_count_aware_leaf_estimates() -> None:
    assert _estimated_encrypted_leaf_size(1) == encrypted_size_for_plaintext_size(1) + 2304
    assert _candidate_metadata_pad(50_000_000_000) == 4 * 1024 * 1024


def test_planner_pieces_budget_disc_manifest_entries(tmp_path: Path) -> None:
    config = _runtime_config(
        tmp_path,
        planner_disc_target_bytes=1_000_000,
        planner_min_fill_bytes=1,
    )
    groups = _build_plan_piece_groups(
        [
            _PlanFile(
                collection_id="2026/20260101T000000Z__tiny",
                path="a.txt",
                bytes=1,
                sha256="a" * 64,
            )
        ],
        config,
    )

    piece = groups[0][0]

    assert piece.estimated_payload_bytes == encrypted_size_for_plaintext_size(1) + 2304
    assert piece.estimated_disc_manifest_bytes == 256


def test_image_root_planning_service_delegates_lookups_and_stream_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[Path, str, str, int | None]] = []

    async def fake_stream_iso_from_root(
        *,
        image_root: Path,
        volume_id: str,
        filename: str,
        content_length: int | None = None,
    ) -> object:
        calls.append((image_root, volume_id, filename, content_length))
        return {"filename": filename}

    record = ImageRootRecord(
        image_id="img_001",
        volume_id="RIVERHOG-IMG-001",
        filename="img_001.iso",
        image_root=tmp_path / "image-root",
        bytes=12345,
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
    assert calls == [(record.image_root, record.volume_id, record.filename, 12345)]


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


def test_planner_refresh_file_lock_rejects_concurrent_holder(tmp_path: Path) -> None:
    config = _runtime_config(tmp_path, planner_image_root=tmp_path / "images")

    with _planner_refresh_file_lock(config) as first_acquired:
        assert first_acquired is True
        with _planner_refresh_file_lock(config) as second_acquired:
            assert second_acquired is False


def test_planner_starts_large_collection_on_fresh_disc_to_minimize_splits(
    tmp_path: Path,
) -> None:
    config = _runtime_config(
        tmp_path,
        planner_disc_target_bytes=1_050_000,
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
        planner_disc_target_bytes=1_050_000,
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
        planner_disc_target_bytes=1_050_000,
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


def test_planner_may_split_one_single_disc_collection_by_whole_files() -> None:
    groups = _pack_collection_piece_groups(
        [
            _group("2026/20260101T000000Z__alpha", [300, 300, 300], artifact_estimate=50),
            _group("2026/20260102T000000Z__bravo", [100]),
        ],
        payload_capacity=1_000,
        minimum_payload_fill=1,
    )

    alpha_groups = [
        group for group in groups if any(piece.collection_id.endswith("__alpha") for piece in group)
    ]

    assert len(groups) == 2
    assert len(alpha_groups) == 2
    assert sorted(
        sum(piece.collection_id.endswith("__alpha") for piece in group)
        for group in alpha_groups
    ) == [1, 2]
    assert all(piece.part_count == 1 for group in groups for piece in group)


def test_planner_may_split_single_disc_collection_to_make_another_disc_ready() -> None:
    groups = _pack_collection_piece_groups(
        [
            _group("2026/20260101T000000Z__alpha", [800]),
            _group("2026/20260102T000000Z__bravo", [150, 150]),
        ],
        payload_capacity=1_000,
        minimum_payload_fill=900,
    )

    group_collections = [{piece.collection_id for piece in group} for group in groups]

    assert {
        "2026/20260101T000000Z__alpha",
        "2026/20260102T000000Z__bravo",
    } in group_collections
    assert _collection_disc_count(groups, "2026/20260102T000000Z__bravo") == 2


def test_planner_does_not_optionally_split_remaining_required_split_tail() -> None:
    groups = _pack_collection_piece_groups(
        [
            _group("2026/20260101T000000Z__alpha", [300, 300, 300], artifact_estimate=50),
            _group("2026/20260102T000000Z__bravo", [100]),
        ],
        payload_capacity=1_000,
        minimum_payload_fill=1,
        optionally_splittable_collections={"2026/20260102T000000Z__bravo"},
    )

    assert _collection_disc_count(groups, "2026/20260101T000000Z__alpha") == 1


def test_planner_counts_artifacts_when_optionally_splitting_collection() -> None:
    groups = _pack_collection_piece_groups(
        [
            _group("2026/20260101T000000Z__alpha", [300, 300], artifact_estimate=100),
            _group("2026/20260102T000000Z__bravo", [620]),
        ],
        payload_capacity=1_000,
        minimum_payload_fill=1,
    )

    assert _collection_disc_count(groups, "2026/20260101T000000Z__alpha") == 1


def test_planner_does_not_optionally_split_required_split_collections() -> None:
    groups = _pack_collection_piece_groups(
        [
            _group("2026/20260101T000000Z__alpha", [900], artifact_estimate=50),
            _group("2026/20260101T000000Z__alpha", [900], artifact_estimate=50),
            _group("2026/20260102T000000Z__bravo", [100]),
        ],
        payload_capacity=1_000,
        minimum_payload_fill=1,
    )

    assert len(groups) == 3
    assert _collection_disc_count(groups, "2026/20260101T000000Z__alpha") == 2


def test_planner_allows_only_one_optional_split_collection_per_disc() -> None:
    groups = _pack_collection_piece_groups(
        [
            _group("2026/20260101T000000Z__alpha", [300, 300]),
            _group("2026/20260102T000000Z__bravo", [290, 290]),
            _group("2026/20260103T000000Z__charlie", [450]),
        ],
        payload_capacity=1_000,
        minimum_payload_fill=1,
    )

    split_collection_ids = {
        collection_id
        for collection_id in {
            piece.collection_id for group in groups for piece in group
        }
        if _collection_disc_count(groups, collection_id) > 1
    }

    assert len(split_collection_ids) == 1


def test_planner_saturation_releases_best_filled_waiting_candidate(tmp_path: Path) -> None:
    config = _runtime_config(
        tmp_path,
        planner_min_fill_bytes=900,
        planner_unplanned_saturation_bytes=1_000,
    )

    candidate_id = _saturation_release_candidate_id(
        specs=(_spec("small", 400), _spec("large", 700), _spec("ready", 950)),
        config=config,
    )

    assert candidate_id == "large"


def test_planner_saturation_can_be_disabled(tmp_path: Path) -> None:
    config = _runtime_config(
        tmp_path,
        planner_min_fill_bytes=900,
        planner_unplanned_saturation_bytes=0,
    )

    candidate_id = _saturation_release_candidate_id(
        specs=(_spec("small", 400), _spec("large", 700)),
        config=config,
    )

    assert candidate_id is None
