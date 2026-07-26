from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from riverhog_core.app_permissions import ARCHIVES_READ, ApplicationAccess, ApplicationPrincipal
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveUsageSnapshotRecord,
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionTagRecord,
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    TagRecord,
)
from riverhog_core.domain.enums import ArchiveState
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_reporting import SqlAlchemyArchiveReportingService
from riverhog_core.services.download_allowances import SqlAlchemyDownloadAllowance

from tests.unit.db_helpers import sqlite_url


def _config(path: Path) -> RuntimeConfig:
    return RuntimeConfig(database_url=sqlite_url(path))


def _seed(path: Path) -> None:
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(
            TagRecord(
                id="docs",
                created_by_app="fixture",
                created_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionRecord(
                id=1,
                creation_idempotency_key="fixture-1",
                content_etag="0" * 64,
                record_etag="1" * 64,
                metadata_revision=1,
                metadata_updated_at="2026-01-01T00:00:00.000000Z",
                created_by_app="fixture",
                created_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionTagRecord(
                collection_id=1,
                tag_id="docs",
                assigned_by_app="fixture",
                assigned_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionFileRecord(
                collection_id=1,
                path="readme.txt",
                bytes=12,
                sha256="a" * 64,
            )
        )
        copy = CollectionArchiveCopyRecord(
            collection_id=1,
            store="deep",
            state="uploaded",
            archive_storage_prefix="collections/docs",
            backend="s3",
            storage_class="DEEP_ARCHIVE",
            last_uploaded_at="2026-01-01T00:00:00.000000Z",
            last_verified_at="2026-01-01T00:00:00.000000Z",
        )
        session.add(copy)
        for order, (object_id, kind, size) in enumerate(
            (("data-000000", "pack", 20), ("manifest", "manifest", 10), ("proof", "proof", 5))
        ):
            copy.objects.append(
                CollectionArchiveObjectRecord(
                    collection_id=copy.collection_id,
                    store=copy.store,
                    object_id=object_id,
                    object_order=order,
                    kind=kind,
                    object_path=f"collections/docs/{object_id}.age",
                    plaintext_bytes=size,
                    stored_bytes=size,
                    sha256=chr(ord("a") + order) * 64,
                    stored_sha256=chr(ord("a") + order) * 64,
                    backend="s3",
                    storage_class="DEEP_ARCHIVE" if kind == "pack" else "STANDARD",
                    uploaded_at="2026-01-01T00:00:00.000000Z",
                    verified_at="2026-01-01T00:00:00.000000Z",
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
    assert report.collections[0].archive_copies[0].store == "deep"
    assert report.collections[0].archive_copies[0].state == ArchiveState.UPLOADED
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


def test_archive_report_includes_store_download_allowances(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    config = _config(path)
    config = replace(
        config,
        archive_stores={
            "deep": replace(
                config.archive_store("deep"),
                monthly_download_allowance_bytes=1_000,
                download_safety_buffer_bytes=100,
            )
        },
    )
    initialize_db(config.database_url)
    allowance = SqlAlchemyDownloadAllowance(config)
    assert b"".join(
        allowance.track(
            store="deep",
            expected_bytes=125,
            content=iter((b"x" * 125,)),
        )
    )

    report = SqlAlchemyArchiveReportingService(
        config,
        download_allowance=allowance,
    ).get_report()

    assert len(report.download_allowances) == 1
    status = report.download_allowances[0]
    assert status.store == "deep"
    assert status.allowance_bytes == 1_000
    assert status.safety_buffer_bytes == 100
    assert status.effective_limit_bytes == 900
    assert status.accounted_bytes == 125
    assert status.remaining_bytes == 775


def test_archive_report_includes_pending_upload_in_database_totals(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(
            TagRecord(
                id="pending",
                created_by_app="fixture",
                created_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionUploadRecord(
                collection_id=2,
                idempotency_key="fixture-2",
                tags_json='["pending"]',
                initiated_by_app="fixture",
                archive_store="deep",
                state="archiving",
            )
        )
        session.add(
            CollectionUploadFileRecord(
                collection_id=2,
                path="pending.txt",
                file_order=1,
                bytes=7,
                sha256="d" * 64,
                ingress_bytes=7,
                ingress_uploaded_bytes=7,
                ingress_secret_envelope="fixture-envelope",
                ingress_state_json="{}",
                ingress_upload_id="fixture-upload",
            )
        )

    report = SqlAlchemyArchiveReportingService(_config(path)).get_report()

    assert report.totals.collections == 2
    assert report.totals.uploaded_collections == 1
    assert report.totals.measured_storage_bytes == 35
    collection_rows = [
        (str(item.id), item.bytes, item.archive_copies[0].state.value)
        for item in report.collections
    ]
    assert collection_rows == [
        ("1", 12, "uploaded"),
        ("2", 7, "uploading"),
    ]

    restricted = SqlAlchemyArchiveReportingService(_config(path)).get_report(
        principal=ApplicationPrincipal(
            app="docs-reader",
            key_id="docs-key",
            access=frozenset({ApplicationAccess(ARCHIVES_READ, "tag:docs")}),
        )
    )
    assert restricted.totals.collections == 1
    assert restricted.totals.uploaded_collections == 1
    assert [str(item.id) for item in restricted.collections] == ["1"]
    assert restricted.history == ()
