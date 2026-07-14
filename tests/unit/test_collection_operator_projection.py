from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionImageOperatorSummaryRecord,
    CollectionOperatorSummaryRecord,
    CollectionRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    ImageDiscRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services import collections as collections_service
from riverhog_core.services.collections import SqlAlchemyCollectionService
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


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _summary_row(sqlite_path: Path, collection_id: str) -> CollectionOperatorSummaryRecord:
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        row = session.get(CollectionOperatorSummaryRecord, collection_id)
        assert row is not None
        return row


def test_collection_operator_projection_tracks_collection_summary_changes(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    session_factory = make_session_factory(sqlite_url(sqlite_path))

    with session_scope(session_factory) as session:
        session.add(CollectionRecord(id="docs"))
        session.add_all(
            [
                CollectionFileRecord(
                    collection_id="docs",
                    path="a.txt",
                    bytes=100,
                    sha256=_sha256(b"a"),
                    hot=True,
                ),
                CollectionFileRecord(
                    collection_id="docs",
                    path="b.txt",
                    bytes=50,
                    sha256=_sha256(b"b"),
                    hot=False,
                ),
            ]
        )

    row = _summary_row(sqlite_path, "docs")
    assert row.files == 2
    assert row.bytes == 150
    assert row.hot_bytes == 100
    assert row.disc_redundancy_bytes == 0
    assert row.disc_coverage_bytes == 0
    assert row.disc_redundancy_state == "none"
    assert row.has_archive == 0

    with session_scope(session_factory) as session:
        session.add(
            CollectionArchiveRecord(
                collection_id="docs",
                state="uploaded",
                object_path="archives/docs.tar",
                stored_bytes=151,
                backend="s3",
                storage_class="DEEP_ARCHIVE",
                archive_format="tar",
                compression="none",
                manifest_object_path="archives/docs.manifest.json",
                manifest_sha256=_sha256(b"manifest"),
            )
        )
        session.add(
            FinalizedImageRecord(
                image_id="img-001",
                candidate_id="candidate-001",
                filename="img-001.iso",
                bytes=150,
                image_root="/tmp/images",
                target_bytes=50_000_000_000,
                required_disc_count=2,
            )
        )
        for path in ("a.txt", "b.txt"):
            session.add(
                FinalizedImageCoveredPathRecord(
                    image_id="img-001",
                    collection_id="docs",
                    path=path,
                )
            )
            session.add(
                FinalizedImageCoveragePartRecord(
                    image_id="img-001",
                    collection_id="docs",
                    path=path,
                    part_index=0,
                    part_count=1,
                )
            )
        session.add(
            ImageDiscRecord(
                image_id="img-001",
                disc_id="disc-001",
                label_text="Disc 1",
                location="Shelf A",
                created_at="2026-06-20T12:00:00Z",
                state="registered",
                verification_state="pending",
            )
        )

    row = _summary_row(sqlite_path, "docs")
    assert row.has_archive == 1
    assert row.archive_state == "uploaded"
    assert row.archive_object_path == "archives/docs.tar"
    assert row.manifest_object_path == "archives/docs.manifest.json"
    assert row.disc_redundancy_bytes == 0
    assert row.disc_coverage_bytes == 150
    assert row.has_registered_image == 1
    assert row.disc_redundancy_state == "partial"

    with session_scope(session_factory) as session:
        session.add(
            ImageDiscRecord(
                image_id="img-001",
                disc_id="disc-002",
                label_text="Disc 2",
                location="Shelf B",
                created_at="2026-06-20T12:01:00Z",
                state="registered",
                verification_state="pending",
            )
        )

    row = _summary_row(sqlite_path, "docs")
    assert row.disc_redundancy_bytes == 150
    assert row.disc_coverage_bytes == 150
    assert row.disc_redundancy_state == "full"


def test_collection_list_reads_paged_operator_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        session.add_all([CollectionRecord(id="small"), CollectionRecord(id="large")])
        session.add_all(
            [
                CollectionFileRecord(
                    collection_id="small",
                    path="small.bin",
                    bytes=10,
                    sha256=_sha256(b"small"),
                    hot=True,
                ),
                CollectionFileRecord(
                    collection_id="large",
                    path="large.bin",
                    bytes=100,
                    sha256=_sha256(b"large"),
                    hot=True,
                ),
            ]
        )

    def require_operator_projection(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("collection list should read the operator projection")

    monkeypatch.setattr(
        collections_service,
        "_collection_list_summaries",
        require_operator_projection,
    )

    service = SqlAlchemyCollectionService(_config(sqlite_path), object(), object())  # type: ignore[arg-type]
    page = service.list(
        page=1,
        per_page=1,
        q=None,
        disc_redundancy_state=None,
        sort="bytes",
        order="desc",
    )

    assert page.total == 2
    assert page.pages == 2
    assert [summary.id for summary in page.collections] == ["large"]

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        collection_ids = list(
            session.scalars(
                select(CollectionOperatorSummaryRecord.collection_id).order_by(
                    CollectionOperatorSummaryRecord.collection_id
                )
            )
        )
    assert collection_ids == ["large", "small"]


def test_collection_show_reads_operator_projection_with_bounded_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        session.add(CollectionRecord(id="docs"))
        session.add_all(
            [
                CollectionFileRecord(
                    collection_id="docs",
                    path="a.txt",
                    bytes=10,
                    sha256=_sha256(b"a"),
                    hot=True,
                ),
                CollectionFileRecord(
                    collection_id="docs",
                    path="b.txt",
                    bytes=20,
                    sha256=_sha256(b"b"),
                    hot=False,
                ),
            ]
        )
        session.add(
            FinalizedImageRecord(
                image_id="img-001",
                candidate_id="candidate-001",
                filename="img-001.iso",
                bytes=30,
                image_root="/tmp/images",
                target_bytes=50_000_000_000,
                required_disc_count=2,
            )
        )
        for path in ("a.txt", "b.txt"):
            session.add(
                FinalizedImageCoveredPathRecord(
                    image_id="img-001",
                    collection_id="docs",
                    path=path,
                )
            )
        session.add(
            ImageDiscRecord(
                image_id="img-001",
                disc_id="disc-001",
                label_text="Disc 1",
                location="Shelf A",
                created_at="2026-06-20T12:00:00Z",
                state="registered",
                verification_state="pending",
            )
        )

    with session_scope(session_factory) as session:
        collection_image_row = session.get(
            CollectionImageOperatorSummaryRecord,
            ("docs", "img-001"),
        )
        assert collection_image_row is not None
        assert collection_image_row.covered_paths_total == 2

    with session_scope(session_factory) as session:
        covered_path = session.get(
            FinalizedImageCoveredPathRecord,
            ("img-001", "docs", "b.txt"),
        )
        assert covered_path is not None
        session.delete(covered_path)

    with session_scope(session_factory) as session:
        collection_image_row = session.get(
            CollectionImageOperatorSummaryRecord,
            ("docs", "img-001"),
        )
        assert collection_image_row is not None
        assert collection_image_row.covered_paths_total == 1

        covered_path = session.get(
            FinalizedImageCoveredPathRecord,
            ("img-001", "docs", "a.txt"),
        )
        assert covered_path is not None
        session.delete(covered_path)

    with session_scope(session_factory) as session:
        assert session.get(CollectionImageOperatorSummaryRecord, ("docs", "img-001")) is None
        session.add_all(
            [
                FinalizedImageCoveredPathRecord(
                    image_id="img-001",
                    collection_id="docs",
                    path="a.txt",
                ),
                FinalizedImageCoveredPathRecord(
                    image_id="img-001",
                    collection_id="docs",
                    path="b.txt",
                ),
            ]
        )

    with session_scope(session_factory) as session:
        collection_image_row = session.get(
            CollectionImageOperatorSummaryRecord,
            ("docs", "img-001"),
        )
        assert collection_image_row is not None
        assert collection_image_row.covered_paths_total == 2

    def fail_if_full_collection_show_path_is_used(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("bounded collection show should read operator projections")

    monkeypatch.setattr(
        collections_service,
        "_collection_file_summary_rows",
        fail_if_full_collection_show_path_is_used,
    )
    monkeypatch.setattr(
        collections_service,
        "_collection_image_coverage",
        fail_if_full_collection_show_path_is_used,
    )

    service = SqlAlchemyCollectionService(_config(sqlite_path), object(), object())  # type: ignore[arg-type]
    summary = service.get("docs", coverage_path_limit=1)

    assert summary.id == "docs"
    assert summary.files == 2
    assert summary.bytes == 30
    assert len(summary.image_coverage) == 1
    assert summary.image_coverage[0].covered_paths == ["a.txt"]
    assert summary.image_coverage[0].covered_paths_total == 2
    assert [disc.disc_id for disc in summary.image_coverage[0].discs] == ["disc-001"]
