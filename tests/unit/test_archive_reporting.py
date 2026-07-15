from __future__ import annotations

from pathlib import Path

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveUsageSnapshotRecord,
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
)
from riverhog_core.domain.enums import ArchiveState
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_reporting import SqlAlchemyArchiveReportingService
from tests.unit.db_helpers import sqlite_url


def _config(path: Path) -> RuntimeConfig:
    return RuntimeConfig(database_url=sqlite_url(path))


def _seed(path: Path) -> None:
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(CollectionRecord(id="2025/20250102T030405Z__docs"))
        session.add(
            CollectionFileRecord(
                collection_id="2025/20250102T030405Z__docs",
                path="readme.txt",
                bytes=12,
                sha256="a" * 64,
                hot=True,
            )
        )
        session.add(
            CollectionArchiveRecord(
                collection_id="2025/20250102T030405Z__docs",
                state="uploaded",
                object_path="collections/docs/archive.tar.age",
                stored_bytes=20,
                manifest_object_path="collections/docs/manifest.yml",
                manifest_sha256="b" * 64,
                manifest_stored_bytes=10,
                ots_object_path="collections/docs/manifest.yml.ots",
                ots_sha256="c" * 64,
                ots_stored_bytes=5,
                backend="s3",
                storage_class="DEEP_ARCHIVE",
            )
        )


def test_archive_report_measures_collection_objects(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)

    report = SqlAlchemyArchiveReportingService(_config(path)).get_report()

    assert report.scope == "all"
    assert report.totals.collections == 1
    assert report.totals.uploaded_collections == 1
    assert report.totals.measured_storage_bytes == 35
    assert report.collections[0].archive.state == ArchiveState.UPLOADED
    assert report.collections[0].measured_storage_bytes == 35
    assert report.history[0].uploaded_collections == 1


def test_archive_report_reuses_unchanged_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)
    service = SqlAlchemyArchiveReportingService(_config(path))

    service.get_report()
    service.get_report()

    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        assert session.query(ArchiveUsageSnapshotRecord).count() == 1


def test_archive_report_includes_pending_upload_in_database_totals(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(
            CollectionUploadRecord(
                collection_id="2025/20250103T030405Z__pending",
                state="archiving",
            )
        )
        session.add(
            CollectionUploadFileRecord(
                collection_id="2025/20250103T030405Z__pending",
                path="pending.txt",
                file_order=1,
                bytes=7,
                sha256="d" * 64,
                uploaded_bytes=7,
            )
        )

    report = SqlAlchemyArchiveReportingService(_config(path)).get_report()

    assert report.totals.collections == 2
    assert report.totals.uploaded_collections == 1
    assert report.totals.measured_storage_bytes == 35
    collection_rows = [
        (str(item.id), item.bytes, item.archive.state.value) for item in report.collections
    ]
    assert collection_rows == [
        ("2025/20250102T030405Z__docs", 12, "uploaded"),
        ("2025/20250103T030405Z__pending", 7, "uploading"),
    ]
