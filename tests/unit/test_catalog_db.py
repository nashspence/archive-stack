from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text

from riverhog_core.catalog_db import create_catalog_engine, migrate_schema


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
