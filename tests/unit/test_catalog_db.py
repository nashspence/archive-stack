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
