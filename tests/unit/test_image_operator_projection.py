from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    ImageCopyRecord,
    ImageOperatorSummaryRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services import planning as planning_service
from riverhog_core.services.planning import SqlAlchemyPlanningService
from tests.unit.db_helpers import sqlite_url


def _config(sqlite_path: Path) -> RuntimeConfig:
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
        database_url=sqlite_url(sqlite_path),
    )


def _summary_row(sqlite_path: Path, image_id: str) -> ImageOperatorSummaryRecord:
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        row = session.get(ImageOperatorSummaryRecord, image_id)
        assert row is not None
        return row


def _seed_image(sqlite_path: Path) -> None:
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        session.add_all([CollectionRecord(id="docs"), CollectionRecord(id="photos")])
        session.add(
            FinalizedImageRecord(
                image_id="20260616T121459Z",
                candidate_id="cand-1",
                filename="20260616T121459Z.iso",
                bytes=100,
                image_root="/tmp/image",
                target_bytes=200,
                required_copy_count=2,
            )
        )
        session.add_all(
            [
                FinalizedImageCoveredPathRecord(
                    image_id="20260616T121459Z",
                    collection_id="docs",
                    path="a.txt",
                ),
                FinalizedImageCoveredPathRecord(
                    image_id="20260616T121459Z",
                    collection_id="photos",
                    path="b.jpg",
                ),
            ]
        )
        session.add_all(
            [
                ImageCopyRecord(
                    image_id="20260616T121459Z",
                    copy_id="copy-1",
                    label_text="copy-1",
                    location="shelf",
                    created_at="2026-06-16T12:15:00Z",
                    state="registered",
                    verification_state="pending",
                ),
                ImageCopyRecord(
                    image_id="20260616T121459Z",
                    copy_id="copy-2",
                    label_text="copy-2",
                    location=None,
                    created_at="2026-06-16T12:16:00Z",
                    state="needed",
                    verification_state="pending",
                ),
            ]
        )


def test_image_operator_projection_tracks_coverage_and_copy_changes(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    _seed_image(sqlite_path)

    row = _summary_row(sqlite_path, "20260616T121459Z")
    assert row.filename == "20260616T121459Z.iso"
    assert row.finalized_at == "2026-06-16T12:14:59Z"
    assert row.files == 2
    assert row.collections == 2
    assert row.collection_ids_text == "docs\nphotos"
    assert row.physical_protection_state == "partially_protected"
    assert row.physical_copies_required == 2
    assert row.physical_copies_registered == 1
    assert row.physical_copies_verified == 0
    assert row.physical_copies_missing == 1

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        copy = session.get(ImageCopyRecord, ("20260616T121459Z", "copy-2"))
        assert copy is not None
        copy.state = "verified"
        copy.verification_state = "verified"
        covered = session.get(
            FinalizedImageCoveredPathRecord,
            ("20260616T121459Z", "photos", "b.jpg"),
        )
        assert covered is not None
        session.delete(covered)

    row = _summary_row(sqlite_path, "20260616T121459Z")
    assert row.files == 1
    assert row.collections == 1
    assert row.collection_ids_text == "docs"
    assert row.physical_protection_state == "protected"
    assert row.physical_copies_registered == 2
    assert row.physical_copies_verified == 1
    assert row.physical_copies_missing == 0


def test_image_list_and_show_read_operator_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    _seed_image(sqlite_path)

    def fail_if_canonical_image_view_is_used(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("image operator views should read the projection")

    monkeypatch.setattr(
        planning_service,
        "_finalized_image_view",
        fail_if_canonical_image_view_is_used,
    )

    service = SqlAlchemyPlanningService(_config(sqlite_path))
    page = service.list_images(
        page=1,
        per_page=25,
        sort="finalized_at",
        order="desc",
        q="docs",
        collection="docs",
        has_copies=True,
    )
    assert page["total"] == 1
    assert page["images"] == [
        {
            "id": "20260616T121459Z",
            "filename": "20260616T121459Z.iso",
            "finalized_at": "2026-06-16T12:14:59Z",
            "bytes": 100,
            "target_bytes": 200,
            "fill": 0.5,
            "files": 2,
            "collections": 2,
            "collection_ids": ["docs", "photos"],
            "iso_ready": True,
            "physical_protection_state": "partially_protected",
            "physical_copies_required": 2,
            "physical_copies_registered": 1,
            "physical_copies_verified": 0,
            "physical_copies_missing": 1,
        }
    ]
    shown = service.get_image("20260616T121459Z")
    assert shown["collection_ids"] == ["docs", "photos"]
