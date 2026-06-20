from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


SCHEMA_BASELINE_VERSION = 1
SCHEMA_LATEST_VERSION = 3
_SUPPORTED_SCHEMA_VERSIONS = {SCHEMA_BASELINE_VERSION, 2, SCHEMA_LATEST_VERSION}
_SCHEMA_V2_DROPPED_COLUMNS = {
    "glacier_usage_snapshots": (
        "estimated_billable_bytes",
        "estimated_monthly_cost_usd",
        "pricing_label",
        "glacier_storage_rate_usd_per_gib_month",
        "standard_storage_rate_usd_per_gib_month",
        "archived_metadata_bytes_per_object",
        "standard_metadata_bytes_per_object",
        "minimum_storage_duration_days",
    ),
    "glacier_recovery_sessions": ("estimate_json",),
}
_COLLECTION_OPERATOR_SUMMARY_COLUMNS = (
    "collection_id",
    "files",
    "bytes",
    "hot_bytes",
    "archived_bytes",
    "pending_bytes",
    "protected_bytes",
    "physical_bytes",
    "has_registered_image",
    "protection_state",
    "has_archive",
    "archive_state",
    "archive_object_path",
    "archive_stored_bytes",
    "archive_backend",
    "archive_storage_class",
    "archive_last_uploaded_at",
    "archive_last_verified_at",
    "archive_failure",
    "archive_format",
    "compression",
    "manifest_object_path",
    "manifest_sha256",
    "ots_object_path",
    "ots_sha256",
    "updated_at",
)


def _normalize_database_url(database_url: str) -> str:
    raw = database_url.strip()
    if "://" not in raw:
        raise ValueError("database URL must be a SQLAlchemy URL")
    return raw


def create_catalog_engine(database_url: str) -> Engine:
    database_url = _normalize_database_url(database_url)
    backend = _database_url_backend(database_url)
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if backend == "sqlite" else {},
        future=True,
        pool_pre_ping=True,
    )

    if backend == "sqlite":

        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.close()

    return engine


def _database_url_backend(database_url: str) -> str:
    return make_url(database_url).get_backend_name()


def _check_database_is_baseline_compatible(engine: Engine) -> None:
    table_names = set(inspect(engine).get_table_names())
    if table_names and "schema_migrations" not in table_names:
        raise RuntimeError(
            "unsupported unversioned catalog schema detected; reset the Riverhog "
            "database before starting this greenfield build"
        )


def _apply_schema_migrations(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
        )
        applied_versions = {
            row[0] for row in conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
        }
        if applied_versions - _SUPPORTED_SCHEMA_VERSIONS:
            raise RuntimeError(
                "unsupported pre-baseline catalog schema detected; reset the Riverhog "
                "database before starting this greenfield build"
            )
        if SCHEMA_BASELINE_VERSION not in applied_versions:
            conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                {"v": SCHEMA_BASELINE_VERSION},
            )
            applied_versions.add(SCHEMA_BASELINE_VERSION)
        for version, migration in (
            (2, _migrate_schema_v2),
            (3, _migrate_schema_v3),
        ):
            if version not in applied_versions:
                migration(conn)
                conn.execute(
                    text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                    {"v": version},
                )
                applied_versions.add(version)


def _migrate_schema_v2(conn: Connection) -> None:
    inspector = inspect(conn)
    table_names = set(inspector.get_table_names())
    for table_name, column_names in _SCHEMA_V2_DROPPED_COLUMNS.items():
        if table_name not in table_names:
            continue
        existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name in column_names:
            if column_name in existing_columns:
                conn.execute(
                    text(
                        "ALTER TABLE "
                        f"{_quote_identifier(conn, table_name)} "
                        "DROP COLUMN "
                        f"{_quote_identifier(conn, column_name)}"
                    )
                )


def _migrate_schema_v3(conn: Connection) -> None:
    _refresh_collection_operator_summaries(
        conn,
        "SELECT id AS collection_id FROM collections",
    )


def _quote_identifier(conn: Connection, identifier: str) -> str:
    return conn.dialect.identifier_preparer.quote(identifier)


def _ensure_schema_indexes(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_collection_upload_files_collection_order "
                "ON collection_upload_files (collection_id, file_order)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_collection_operator_summaries_protection "
                "ON collection_operator_summaries (protection_state, collection_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_collection_operator_summaries_bytes "
                "ON collection_operator_summaries (bytes, collection_id)"
            )
        )


def _collection_operator_summary_select_sql(affected_collections_sql: str) -> str:
    return f"""
SELECT *
FROM (
    WITH affected_collections(collection_id) AS (
        {affected_collections_sql}
    ),
    distinct_collections AS (
        SELECT DISTINCT collection_id
        FROM affected_collections
        WHERE collection_id IS NOT NULL
    ),
    file_stats AS (
        SELECT
            collection_id,
            COUNT(path) AS files,
            COALESCE(SUM(bytes), 0) AS bytes,
            COALESCE(SUM(CASE WHEN hot IS TRUE THEN bytes ELSE 0 END), 0) AS hot_bytes,
            COALESCE(SUM(CASE WHEN archived IS TRUE THEN bytes ELSE 0 END), 0) AS archived_bytes
        FROM collection_files
        WHERE collection_id IN (SELECT collection_id FROM distinct_collections)
        GROUP BY collection_id
    ),
    affected_images AS (
        SELECT DISTINCT image_id
        FROM finalized_image_covered_paths
        WHERE collection_id IN (SELECT collection_id FROM distinct_collections)
        UNION
        SELECT DISTINCT image_id
        FROM finalized_image_coverage_parts
        WHERE collection_id IN (SELECT collection_id FROM distinct_collections)
    ),
    registered_counts AS (
        SELECT
            image_id,
            COALESCE(
                SUM(
                    CASE
                        WHEN state IN ('registered', 'verified') OR state IS NULL THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS registered_count
        FROM image_copies
        WHERE image_id IN (SELECT image_id FROM affected_images)
        GROUP BY image_id
    ),
    image_states AS (
        SELECT
            finalized_images.image_id AS image_id,
            CASE
                WHEN COALESCE(registered_counts.registered_count, 0) >=
                    CASE
                        WHEN finalized_images.required_copy_count IS NULL
                            OR finalized_images.required_copy_count <= 0
                        THEN 2
                        ELSE finalized_images.required_copy_count
                    END
                THEN 1
                ELSE 0
            END AS is_protected,
            CASE
                WHEN COALESCE(registered_counts.registered_count, 0) > 0 THEN 1
                ELSE 0
            END AS has_registered
        FROM finalized_images
        JOIN affected_images
            ON affected_images.image_id = finalized_images.image_id
        LEFT JOIN registered_counts
            ON registered_counts.image_id = finalized_images.image_id
    ),
    path_protection AS (
        SELECT
            finalized_image_covered_paths.collection_id,
            finalized_image_covered_paths.path,
            MIN(image_states.is_protected) AS all_protected,
            MAX(image_states.has_registered) AS has_registered
        FROM finalized_image_covered_paths
        JOIN image_states
            ON image_states.image_id = finalized_image_covered_paths.image_id
        WHERE finalized_image_covered_paths.collection_id IN (
            SELECT collection_id FROM distinct_collections
        )
        GROUP BY
            finalized_image_covered_paths.collection_id,
            finalized_image_covered_paths.path
    ),
    protection_stats AS (
        SELECT
            collection_files.collection_id,
            COALESCE(
                SUM(
                    CASE
                        WHEN path_protection.all_protected = 1 THEN collection_files.bytes
                        ELSE 0
                    END
                ),
                0
            ) AS protected_bytes,
            COALESCE(MAX(path_protection.has_registered), 0) AS has_registered_image
        FROM collection_files
        LEFT JOIN path_protection
            ON path_protection.collection_id = collection_files.collection_id
            AND path_protection.path = collection_files.path
        WHERE collection_files.collection_id IN (SELECT collection_id FROM distinct_collections)
        GROUP BY collection_files.collection_id
    ),
    path_part_stats AS (
        SELECT
            finalized_image_coverage_parts.collection_id,
            finalized_image_coverage_parts.path,
            MAX(
                CASE
                    WHEN finalized_image_coverage_parts.part_count = 1
                        AND finalized_image_coverage_parts.part_index = 0
                    THEN 1
                    ELSE 0
                END
            ) AS has_single_full_part,
            MIN(finalized_image_coverage_parts.part_count) AS min_part_count,
            MAX(finalized_image_coverage_parts.part_count) AS max_part_count,
            COUNT(DISTINCT finalized_image_coverage_parts.part_index) AS present_parts
        FROM finalized_image_coverage_parts
        JOIN image_states
            ON image_states.image_id = finalized_image_coverage_parts.image_id
        WHERE finalized_image_coverage_parts.collection_id IN (
            SELECT collection_id FROM distinct_collections
        )
            AND image_states.has_registered = 1
        GROUP BY
            finalized_image_coverage_parts.collection_id,
            finalized_image_coverage_parts.path
    ),
    physical_stats AS (
        SELECT
            collection_files.collection_id,
            COALESCE(
                SUM(
                    CASE
                        WHEN path_part_stats.has_single_full_part = 1 THEN collection_files.bytes
                        WHEN path_part_stats.min_part_count IS NOT NULL
                            AND path_part_stats.min_part_count = path_part_stats.max_part_count
                            AND path_part_stats.present_parts > 0
                        THEN
                            CASE
                                WHEN CAST(
                                    (
                                        collection_files.bytes * path_part_stats.present_parts
                                    ) / path_part_stats.min_part_count
                                    AS BIGINT
                                ) < 1
                                THEN 1
                                WHEN CAST(
                                    (
                                        collection_files.bytes * path_part_stats.present_parts
                                    ) / path_part_stats.min_part_count
                                    AS BIGINT
                                ) > collection_files.bytes
                                THEN collection_files.bytes
                                ELSE CAST(
                                    (
                                        collection_files.bytes * path_part_stats.present_parts
                                    ) / path_part_stats.min_part_count
                                    AS BIGINT
                                )
                            END
                        ELSE 0
                    END
                ),
                0
            ) AS physical_bytes
        FROM collection_files
        LEFT JOIN path_part_stats
            ON path_part_stats.collection_id = collection_files.collection_id
            AND path_part_stats.path = collection_files.path
        WHERE collection_files.collection_id IN (SELECT collection_id FROM distinct_collections)
        GROUP BY collection_files.collection_id
    )
    SELECT
        collections.id AS collection_id,
        COALESCE(file_stats.files, 0) AS files,
        COALESCE(file_stats.bytes, 0) AS bytes,
        COALESCE(file_stats.hot_bytes, 0) AS hot_bytes,
        COALESCE(file_stats.archived_bytes, 0) AS archived_bytes,
        COALESCE(file_stats.bytes, 0) - COALESCE(file_stats.archived_bytes, 0) AS pending_bytes,
        COALESCE(protection_stats.protected_bytes, 0) AS protected_bytes,
        COALESCE(physical_stats.physical_bytes, 0) AS physical_bytes,
        COALESCE(protection_stats.has_registered_image, 0) AS has_registered_image,
        CASE
            WHEN COALESCE(file_stats.bytes, 0) > 0
                AND COALESCE(protection_stats.protected_bytes, 0) >= COALESCE(file_stats.bytes, 0)
            THEN 'protected'
            WHEN COALESCE(protection_stats.protected_bytes, 0) > 0
                OR COALESCE(file_stats.archived_bytes, 0) > 0
                OR COALESCE(protection_stats.has_registered_image, 0) = 1
            THEN 'partially_protected'
            ELSE 'unprotected'
        END AS protection_state,
        CASE
            WHEN collection_archives.collection_id IS NULL THEN 0
            ELSE 1
        END AS has_archive,
        COALESCE(collection_archives.state, 'pending') AS archive_state,
        collection_archives.object_path AS archive_object_path,
        collection_archives.stored_bytes AS archive_stored_bytes,
        collection_archives.backend AS archive_backend,
        collection_archives.storage_class AS archive_storage_class,
        collection_archives.last_uploaded_at AS archive_last_uploaded_at,
        collection_archives.last_verified_at AS archive_last_verified_at,
        collection_archives.failure AS archive_failure,
        collection_archives.archive_format AS archive_format,
        collection_archives.compression AS compression,
        collection_archives.manifest_object_path AS manifest_object_path,
        collection_archives.manifest_sha256 AS manifest_sha256,
        collection_archives.ots_object_path AS ots_object_path,
        collection_archives.ots_sha256 AS ots_sha256,
        CAST(CURRENT_TIMESTAMP AS TEXT) AS updated_at
    FROM collections
    JOIN distinct_collections
        ON distinct_collections.collection_id = collections.id
    LEFT JOIN file_stats
        ON file_stats.collection_id = collections.id
    LEFT JOIN protection_stats
        ON protection_stats.collection_id = collections.id
    LEFT JOIN physical_stats
        ON physical_stats.collection_id = collections.id
    LEFT JOIN collection_archives
        ON collection_archives.collection_id = collections.id
) AS collection_operator_summary_refresh
"""


def _collection_operator_summary_upsert_sql(
    conn: Connection,
    affected_collections_sql: str,
) -> str:
    columns = ", ".join(_COLLECTION_OPERATOR_SUMMARY_COLUMNS)
    select_sql = _collection_operator_summary_select_sql(affected_collections_sql)
    if conn.dialect.name == "sqlite":
        return f"INSERT OR REPLACE INTO collection_operator_summaries ({columns}) {select_sql}"
    updates = ", ".join(
        f"{column} = EXCLUDED.{column}"
        for column in _COLLECTION_OPERATOR_SUMMARY_COLUMNS
        if column != "collection_id"
    )
    return (
        f"INSERT INTO collection_operator_summaries ({columns}) "
        f"{select_sql} "
        f"ON CONFLICT (collection_id) DO UPDATE SET {updates}"
    )


def _refresh_collection_operator_summaries(
    conn: Connection,
    affected_collections_sql: str,
) -> None:
    conn.execute(text(_collection_operator_summary_upsert_sql(conn, affected_collections_sql)))


def _install_collection_operator_projection(engine: Engine) -> None:
    with engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            _install_sqlite_collection_operator_projection(conn)
            return
        if conn.dialect.name == "postgresql":
            _install_postgresql_collection_operator_projection(conn)
            return
        raise RuntimeError(f"unsupported catalog backend: {conn.dialect.name}")


def _sqlite_collection_operator_refresh(affected_collections_sql: str) -> str:
    columns = ", ".join(_COLLECTION_OPERATOR_SUMMARY_COLUMNS)
    select_sql = _collection_operator_summary_select_sql(affected_collections_sql)
    return f"INSERT OR REPLACE INTO collection_operator_summaries ({columns}) {select_sql};"


def _install_sqlite_collection_operator_projection(conn: Connection) -> None:
    trigger_sql = {
        "riverhog_cos_collections_insert": (
            "collections",
            _sqlite_collection_operator_refresh("SELECT NEW.id AS collection_id"),
        ),
        "riverhog_cos_collections_update": (
            "collections",
            _sqlite_collection_operator_refresh("SELECT NEW.id AS collection_id"),
        ),
        "riverhog_cos_collection_files_insert": (
            "collection_files",
            _sqlite_collection_operator_refresh("SELECT NEW.collection_id AS collection_id"),
        ),
        "riverhog_cos_collection_files_update": (
            "collection_files",
            _sqlite_collection_operator_refresh(
                "SELECT OLD.collection_id AS collection_id "
                "UNION SELECT NEW.collection_id AS collection_id"
            ),
        ),
        "riverhog_cos_collection_files_delete": (
            "collection_files",
            _sqlite_collection_operator_refresh("SELECT OLD.collection_id AS collection_id"),
        ),
        "riverhog_cos_collection_archives_insert": (
            "collection_archives",
            _sqlite_collection_operator_refresh("SELECT NEW.collection_id AS collection_id"),
        ),
        "riverhog_cos_collection_archives_update": (
            "collection_archives",
            _sqlite_collection_operator_refresh(
                "SELECT OLD.collection_id AS collection_id "
                "UNION SELECT NEW.collection_id AS collection_id"
            ),
        ),
        "riverhog_cos_collection_archives_delete": (
            "collection_archives",
            _sqlite_collection_operator_refresh("SELECT OLD.collection_id AS collection_id"),
        ),
        "riverhog_cos_covered_paths_insert": (
            "finalized_image_covered_paths",
            _sqlite_collection_operator_refresh("SELECT NEW.collection_id AS collection_id"),
        ),
        "riverhog_cos_covered_paths_update": (
            "finalized_image_covered_paths",
            _sqlite_collection_operator_refresh(
                "SELECT OLD.collection_id AS collection_id "
                "UNION SELECT NEW.collection_id AS collection_id"
            ),
        ),
        "riverhog_cos_covered_paths_delete": (
            "finalized_image_covered_paths",
            _sqlite_collection_operator_refresh("SELECT OLD.collection_id AS collection_id"),
        ),
        "riverhog_cos_coverage_parts_insert": (
            "finalized_image_coverage_parts",
            _sqlite_collection_operator_refresh("SELECT NEW.collection_id AS collection_id"),
        ),
        "riverhog_cos_coverage_parts_update": (
            "finalized_image_coverage_parts",
            _sqlite_collection_operator_refresh(
                "SELECT OLD.collection_id AS collection_id "
                "UNION SELECT NEW.collection_id AS collection_id"
            ),
        ),
        "riverhog_cos_coverage_parts_delete": (
            "finalized_image_coverage_parts",
            _sqlite_collection_operator_refresh("SELECT OLD.collection_id AS collection_id"),
        ),
        "riverhog_cos_finalized_images_update": (
            "finalized_images",
            _sqlite_collection_operator_refresh(
                "SELECT collection_id FROM finalized_image_covered_paths "
                "WHERE image_id = OLD.image_id OR image_id = NEW.image_id "
                "UNION "
                "SELECT collection_id FROM finalized_image_coverage_parts "
                "WHERE image_id = OLD.image_id OR image_id = NEW.image_id"
            ),
        ),
        "riverhog_cos_image_copies_insert": (
            "image_copies",
            _sqlite_collection_operator_refresh(
                "SELECT collection_id FROM finalized_image_covered_paths "
                "WHERE image_id = NEW.image_id "
                "UNION "
                "SELECT collection_id FROM finalized_image_coverage_parts "
                "WHERE image_id = NEW.image_id"
            ),
        ),
        "riverhog_cos_image_copies_update": (
            "image_copies",
            _sqlite_collection_operator_refresh(
                "SELECT collection_id FROM finalized_image_covered_paths "
                "WHERE image_id = OLD.image_id OR image_id = NEW.image_id "
                "UNION "
                "SELECT collection_id FROM finalized_image_coverage_parts "
                "WHERE image_id = OLD.image_id OR image_id = NEW.image_id"
            ),
        ),
        "riverhog_cos_image_copies_delete": (
            "image_copies",
            _sqlite_collection_operator_refresh(
                "SELECT collection_id FROM finalized_image_covered_paths "
                "WHERE image_id = OLD.image_id "
                "UNION "
                "SELECT collection_id FROM finalized_image_coverage_parts "
                "WHERE image_id = OLD.image_id"
            ),
        ),
    }
    for trigger_name, (table_name, body) in trigger_sql.items():
        conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name}"))
        operation = trigger_name.rsplit("_", maxsplit=1)[-1].upper()
        conn.execute(
            text(
                f"CREATE TRIGGER {trigger_name} AFTER {operation} ON {table_name} BEGIN {body} END"
            )
        )


def _install_postgresql_collection_operator_projection(conn: Connection) -> None:
    refresh_sql = _collection_operator_summary_upsert_sql(
        conn,
        "SELECT unnest(p_collection_ids) AS collection_id",
    )
    conn.execute(
        text(
            f"""
CREATE OR REPLACE FUNCTION riverhog_refresh_collection_operator_summaries(
    p_collection_ids text[]
) RETURNS void
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    IF p_collection_ids IS NULL OR cardinality(p_collection_ids) = 0 THEN
        RETURN;
    END IF;

    DELETE FROM collection_operator_summaries
    WHERE collection_id = ANY(p_collection_ids)
        AND NOT EXISTS (
            SELECT 1
            FROM collections
            WHERE collections.id = collection_operator_summaries.collection_id
        );

    {refresh_sql};
END;
$riverhog$;
"""
        )
    )
    trigger_functions = {
        "riverhog_cos_from_new_collection_records": """
CREATE OR REPLACE FUNCTION riverhog_cos_from_new_collection_records()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_collection_operator_summaries(
        ARRAY(SELECT DISTINCT id::text FROM new_rows WHERE id IS NOT NULL)
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_cos_from_changed_collection_records": """
CREATE OR REPLACE FUNCTION riverhog_cos_from_changed_collection_records()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_collection_operator_summaries(
        ARRAY(
            SELECT DISTINCT id::text
            FROM (
                SELECT id::text AS id FROM old_rows
                UNION
                SELECT id::text AS id FROM new_rows
            ) AS affected
            WHERE id IS NOT NULL
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_cos_from_new_collection_ids": """
CREATE OR REPLACE FUNCTION riverhog_cos_from_new_collection_ids()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_collection_operator_summaries(
        ARRAY(SELECT DISTINCT collection_id::text FROM new_rows WHERE collection_id IS NOT NULL)
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_cos_from_old_collection_ids": """
CREATE OR REPLACE FUNCTION riverhog_cos_from_old_collection_ids()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_collection_operator_summaries(
        ARRAY(SELECT DISTINCT collection_id::text FROM old_rows WHERE collection_id IS NOT NULL)
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_cos_from_changed_collection_ids": """
CREATE OR REPLACE FUNCTION riverhog_cos_from_changed_collection_ids()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_collection_operator_summaries(
        ARRAY(
            SELECT DISTINCT collection_id::text
            FROM (
                SELECT collection_id::text AS collection_id FROM old_rows
                UNION
                SELECT collection_id::text AS collection_id FROM new_rows
            ) AS affected
            WHERE collection_id IS NOT NULL
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_cos_from_new_image_ids": """
CREATE OR REPLACE FUNCTION riverhog_cos_from_new_image_ids()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_collection_operator_summaries(
        ARRAY(
            SELECT DISTINCT collection_id::text
            FROM (
                SELECT collection_id
                FROM finalized_image_covered_paths
                WHERE image_id IN (SELECT image_id FROM new_rows)
                UNION
                SELECT collection_id
                FROM finalized_image_coverage_parts
                WHERE image_id IN (SELECT image_id FROM new_rows)
            ) AS affected
            WHERE collection_id IS NOT NULL
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_cos_from_old_image_ids": """
CREATE OR REPLACE FUNCTION riverhog_cos_from_old_image_ids()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_collection_operator_summaries(
        ARRAY(
            SELECT DISTINCT collection_id::text
            FROM (
                SELECT collection_id
                FROM finalized_image_covered_paths
                WHERE image_id IN (SELECT image_id FROM old_rows)
                UNION
                SELECT collection_id
                FROM finalized_image_coverage_parts
                WHERE image_id IN (SELECT image_id FROM old_rows)
            ) AS affected
            WHERE collection_id IS NOT NULL
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_cos_from_changed_image_ids": """
CREATE OR REPLACE FUNCTION riverhog_cos_from_changed_image_ids()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_collection_operator_summaries(
        ARRAY(
            SELECT DISTINCT collection_id::text
            FROM (
                SELECT collection_id
                FROM finalized_image_covered_paths
                WHERE image_id IN (
                    SELECT image_id FROM old_rows
                    UNION
                    SELECT image_id FROM new_rows
                )
                UNION
                SELECT collection_id
                FROM finalized_image_coverage_parts
                WHERE image_id IN (
                    SELECT image_id FROM old_rows
                    UNION
                    SELECT image_id FROM new_rows
                )
            ) AS affected
            WHERE collection_id IS NOT NULL
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
    }
    for function_sql in trigger_functions.values():
        conn.execute(text(function_sql))

    triggers = (
        (
            "riverhog_cos_collections_insert",
            "collections",
            "AFTER INSERT",
            "REFERENCING NEW TABLE AS new_rows",
            "riverhog_cos_from_new_collection_records",
        ),
        (
            "riverhog_cos_collections_update",
            "collections",
            "AFTER UPDATE",
            "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_cos_from_changed_collection_records",
        ),
        (
            "riverhog_cos_collection_files_insert",
            "collection_files",
            "AFTER INSERT",
            "REFERENCING NEW TABLE AS new_rows",
            "riverhog_cos_from_new_collection_ids",
        ),
        (
            "riverhog_cos_collection_files_update",
            "collection_files",
            "AFTER UPDATE",
            "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_cos_from_changed_collection_ids",
        ),
        (
            "riverhog_cos_collection_files_delete",
            "collection_files",
            "AFTER DELETE",
            "REFERENCING OLD TABLE AS old_rows",
            "riverhog_cos_from_old_collection_ids",
        ),
        (
            "riverhog_cos_collection_archives_insert",
            "collection_archives",
            "AFTER INSERT",
            "REFERENCING NEW TABLE AS new_rows",
            "riverhog_cos_from_new_collection_ids",
        ),
        (
            "riverhog_cos_collection_archives_update",
            "collection_archives",
            "AFTER UPDATE",
            "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_cos_from_changed_collection_ids",
        ),
        (
            "riverhog_cos_collection_archives_delete",
            "collection_archives",
            "AFTER DELETE",
            "REFERENCING OLD TABLE AS old_rows",
            "riverhog_cos_from_old_collection_ids",
        ),
        (
            "riverhog_cos_covered_paths_insert",
            "finalized_image_covered_paths",
            "AFTER INSERT",
            "REFERENCING NEW TABLE AS new_rows",
            "riverhog_cos_from_new_collection_ids",
        ),
        (
            "riverhog_cos_covered_paths_update",
            "finalized_image_covered_paths",
            "AFTER UPDATE",
            "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_cos_from_changed_collection_ids",
        ),
        (
            "riverhog_cos_covered_paths_delete",
            "finalized_image_covered_paths",
            "AFTER DELETE",
            "REFERENCING OLD TABLE AS old_rows",
            "riverhog_cos_from_old_collection_ids",
        ),
        (
            "riverhog_cos_coverage_parts_insert",
            "finalized_image_coverage_parts",
            "AFTER INSERT",
            "REFERENCING NEW TABLE AS new_rows",
            "riverhog_cos_from_new_collection_ids",
        ),
        (
            "riverhog_cos_coverage_parts_update",
            "finalized_image_coverage_parts",
            "AFTER UPDATE",
            "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_cos_from_changed_collection_ids",
        ),
        (
            "riverhog_cos_coverage_parts_delete",
            "finalized_image_coverage_parts",
            "AFTER DELETE",
            "REFERENCING OLD TABLE AS old_rows",
            "riverhog_cos_from_old_collection_ids",
        ),
        (
            "riverhog_cos_finalized_images_insert",
            "finalized_images",
            "AFTER INSERT",
            "REFERENCING NEW TABLE AS new_rows",
            "riverhog_cos_from_new_image_ids",
        ),
        (
            "riverhog_cos_finalized_images_update",
            "finalized_images",
            "AFTER UPDATE",
            "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_cos_from_changed_image_ids",
        ),
        (
            "riverhog_cos_finalized_images_delete",
            "finalized_images",
            "AFTER DELETE",
            "REFERENCING OLD TABLE AS old_rows",
            "riverhog_cos_from_old_image_ids",
        ),
        (
            "riverhog_cos_image_copies_insert",
            "image_copies",
            "AFTER INSERT",
            "REFERENCING NEW TABLE AS new_rows",
            "riverhog_cos_from_new_image_ids",
        ),
        (
            "riverhog_cos_image_copies_update",
            "image_copies",
            "AFTER UPDATE",
            "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_cos_from_changed_image_ids",
        ),
        (
            "riverhog_cos_image_copies_delete",
            "image_copies",
            "AFTER DELETE",
            "REFERENCING OLD TABLE AS old_rows",
            "riverhog_cos_from_old_image_ids",
        ),
    )
    for trigger_name, table_name, timing, referencing, function_name in triggers:
        conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}"))
        conn.execute(
            text(
                f"CREATE TRIGGER {trigger_name} "
                f"{timing} ON {table_name} "
                f"{referencing} "
                "FOR EACH STATEMENT "
                f"EXECUTE FUNCTION {function_name}()"
            )
        )


def initialize_db(database_url: str) -> None:
    """Create the current baseline catalog schema.

    Call this once on service startup before any other database access.
    It is safe to call multiple times; all operations are idempotent.
    """
    from riverhog_core.catalog_models import (  # noqa: PLC0415 - avoid circular import at module level
        ActivePinRecord,
        CandidateCoveredPathRecord,
        CollectionArchiveRecord,
        CollectionFileRecord,
        CollectionOperatorSummaryRecord,
        CollectionRecord,
        CollectionUploadFileRecord,
        CollectionUploadRecord,
        FetchEntryRecord,
        FileCopyRecord,
        FinalizedImageCollectionArtifactRecord,
        FinalizedImageCoveragePartRecord,
        FinalizedImageCoveredPathRecord,
        FinalizedImageRecord,
        GlacierRecoverySessionCollectionRecord,
        GlacierRecoverySessionImageRecord,
        GlacierRecoverySessionRecord,
        GlacierUsageSnapshotRecord,
        ImageCopyEventRecord,
        ImageCopyRecord,
        PlannedCandidateRecord,
    )

    _ = (
        ActivePinRecord,
        CandidateCoveredPathRecord,
        CollectionArchiveRecord,
        CollectionFileRecord,
        CollectionOperatorSummaryRecord,
        CollectionRecord,
        FetchEntryRecord,
        FileCopyRecord,
        FinalizedImageCollectionArtifactRecord,
        FinalizedImageCoveragePartRecord,
        FinalizedImageCoveredPathRecord,
        FinalizedImageRecord,
        GlacierRecoverySessionImageRecord,
        GlacierRecoverySessionCollectionRecord,
        GlacierRecoverySessionRecord,
        GlacierUsageSnapshotRecord,
        ImageCopyEventRecord,
        ImageCopyRecord,
        CollectionUploadFileRecord,
        CollectionUploadRecord,
        PlannedCandidateRecord,
    )
    engine = create_catalog_engine(database_url)
    _check_database_is_baseline_compatible(engine)
    Base.metadata.create_all(engine)
    _ensure_schema_indexes(engine)
    _apply_schema_migrations(engine)
    _install_collection_operator_projection(engine)


def make_session_factory(database_url: str) -> sessionmaker[Session]:
    engine = create_catalog_engine(database_url)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
