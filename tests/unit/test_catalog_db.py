from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text

import riverhog_core.finalized_image_coverage as finalized_image_coverage
from riverhog_core.catalog_db import (
    _backfill_finalized_image_manifest_topology,
    create_catalog_engine,
    initialize_db,
    migrate_schema,
)


def test_migrate_schema_repairs_manifest_sidecar_columns_when_prior_version_was_recorded(
    tmp_path: Path,
) -> None:
    engine = create_catalog_engine(tmp_path / "catalog.sqlite3")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)"))
        for version in range(1, 18):
            conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                {"v": version},
            )
        conn.execute(text("CREATE TABLE collection_uploads (collection_id TEXT PRIMARY KEY)"))

    migrate_schema(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("collection_uploads")}
    assert "collection_manifest_bytes_b64" in columns
    assert "collection_manifest_proof_bytes_b64" in columns


def test_migrate_schema_adds_collection_coverage_indexes(tmp_path: Path) -> None:
    engine = create_catalog_engine(tmp_path / "catalog.sqlite3")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)"))
        for version in range(1, 20):
            conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                {"v": version},
            )
        conn.execute(
            text(
                "CREATE TABLE candidate_covered_paths ("
                "candidate_id TEXT, collection_id TEXT, path TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE finalized_image_covered_paths ("
                "image_id TEXT, collection_id TEXT, path TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE finalized_image_coverage_parts ("
                "image_id TEXT, collection_id TEXT, path TEXT, "
                "part_index INTEGER, part_count INTEGER)"
            )
        )

    migrate_schema(engine)

    index_names = {
        index["name"]
        for table in (
            "candidate_covered_paths",
            "finalized_image_covered_paths",
            "finalized_image_coverage_parts",
        )
        for index in inspect(engine).get_indexes(table)
    }
    assert "ix_candidate_covered_paths_collection_path" in index_names
    assert "ix_finalized_image_covered_paths_collection_path" in index_names
    assert "ix_finalized_image_coverage_parts_collection_path" in index_names


def test_migrate_schema_drops_removed_protection_mirror_table(tmp_path: Path) -> None:
    engine = create_catalog_engine(tmp_path / "catalog.sqlite3")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)"))
        for version in range(1, 22):
            conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                {"v": version},
            )
        conn.execute(
            text(
                "CREATE TABLE collection_protection_mirrors ("
                "collection_id TEXT PRIMARY KEY, state TEXT)"
            )
        )

    migrate_schema(engine)

    assert not inspect(engine).has_table("collection_protection_mirrors")


def test_migrate_schema_adds_archive_storage_prefix_columns(tmp_path: Path) -> None:
    engine = create_catalog_engine(tmp_path / "catalog.sqlite3")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)"))
        for version in range(1, 25):
            conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                {"v": version},
            )
        conn.execute(text("CREATE TABLE collection_uploads (collection_id TEXT PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE collection_archives (collection_id TEXT PRIMARY KEY)"))

    migrate_schema(engine)

    upload_columns = {
        column["name"] for column in inspect(engine).get_columns("collection_uploads")
    }
    archive_columns = {
        column["name"] for column in inspect(engine).get_columns("collection_archives")
    }
    assert "archive_storage_prefix" in upload_columns
    assert "archive_storage_prefix" in archive_columns


def test_backfill_skips_images_with_persisted_manifest_topology(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_path)
    engine = create_catalog_engine(sqlite_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO finalized_images "
                "(image_id, candidate_id, filename, bytes, image_root, target_bytes, "
                "required_copy_count) "
                "VALUES "
                "('20260420T040001Z', 'candidate-1', '20260420T040001Z.iso', 1, "
                "'/does/not/exist', 10, 2)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO finalized_image_collection_artifacts "
                "(image_id, collection_id, manifest_path, proof_path) "
                "VALUES "
                "('20260420T040001Z', 'docs', 'collections/000001.yml', "
                "'collections/000001.yml.ots')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO finalized_image_coverage_parts "
                "(image_id, collection_id, path, part_index, part_count, "
                "object_path, sidecar_path) "
                "VALUES "
                "('20260420T040001Z', 'docs', 'invoice.pdf', 0, 1, "
                "'files/000001.age', 'files/000001.yml.age')"
            )
        )

    calls = 0

    def _unexpected_read(*args, **kwargs):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(
        finalized_image_coverage,
        "read_finalized_image_collection_artifacts",
        _unexpected_read,
    )
    monkeypatch.setattr(
        finalized_image_coverage,
        "read_finalized_image_coverage_parts",
        _unexpected_read,
    )

    _backfill_finalized_image_manifest_topology(str(sqlite_path))

    assert calls == 0
