from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


SCHEMA_BASELINE_VERSION = 7
SCHEMA_LATEST_VERSION = 7
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
_COLLECTION_IMAGE_OPERATOR_SUMMARY_COLUMNS = (
    "collection_id",
    "image_id",
    "covered_paths_total",
    "updated_at",
)
_FETCH_OPERATOR_SUMMARY_COLUMNS = (
    "fetch_id",
    "name",
    "fetch_order",
    "fetch_state",
    "selectors",
    "targets_text",
    "files",
    "bytes",
    "hot_files",
    "hot_bytes",
    "missing_files",
    "missing_bytes",
    "entries_total",
    "entries_pending",
    "entries_partial",
    "entries_byte_complete",
    "entries_uploaded",
    "entry_bytes",
    "entry_recovery_bytes",
    "uploaded_bytes",
    "upload_missing_bytes",
    "upload_state_expires_at",
    "updated_at",
)
_IMAGE_OPERATOR_SUMMARY_COLUMNS = (
    "image_id",
    "filename",
    "finalized_at",
    "bytes",
    "target_bytes",
    "files",
    "collections",
    "collection_ids_text",
    "physical_protection_state",
    "physical_copies_required",
    "physical_copies_registered",
    "physical_copies_verified",
    "physical_copies_missing",
    "updated_at",
)
_DISC_OPERATOR_SUMMARY_COLUMNS = (
    "image_id",
    "copy_id",
    "label_text",
    "location",
    "created_at",
    "state",
    "verification_state",
    "filename",
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
    if not table_names:
        return
    if "schema_migrations" not in table_names:
        raise RuntimeError(
            "unsupported unversioned catalog schema detected; reset the Riverhog "
            "database before starting this greenfield build"
        )
    with engine.begin() as conn:
        applied_versions = _applied_schema_versions(conn)
    if applied_versions != {SCHEMA_BASELINE_VERSION}:
        raise RuntimeError(
            "unsupported pre-baseline catalog schema detected; reset or normalize the "
            "Riverhog database before starting this greenfield build"
        )


def _ensure_schema_baseline_marker(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
        )
        applied_versions = _applied_schema_versions(conn)
        if applied_versions and applied_versions != {SCHEMA_BASELINE_VERSION}:
            raise RuntimeError(
                "unsupported pre-baseline catalog schema detected; reset or normalize the "
                "Riverhog database before starting this greenfield build"
            )
        if SCHEMA_BASELINE_VERSION not in applied_versions:
            conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                {"v": SCHEMA_BASELINE_VERSION},
            )


def _applied_schema_versions(conn: Connection) -> set[int]:
    return {
        row[0] for row in conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
    }


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
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_fetch_operator_summaries_state "
                "ON fetch_operator_summaries (fetch_state, fetch_order, fetch_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_fetch_operator_summaries_order "
                "ON fetch_operator_summaries (fetch_order, fetch_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_fetch_selectors_target "
                "ON fetch_selectors (target, fetch_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_image_operator_summaries_finalized_at "
                "ON image_operator_summaries (image_id, filename)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_image_operator_summaries_bytes "
                "ON image_operator_summaries (bytes, image_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_image_operator_summaries_copies "
                "ON image_operator_summaries (physical_copies_registered, image_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_disc_operator_summaries_copy_id "
                "ON disc_operator_summaries (copy_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_disc_operator_summaries_state "
                "ON disc_operator_summaries (state, copy_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_disc_operator_summaries_verification "
                "ON disc_operator_summaries (verification_state, copy_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_disc_operator_summaries_location "
                "ON disc_operator_summaries (location, copy_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_collection_image_operator_summaries_image "
                "ON collection_image_operator_summaries (image_id, collection_id)"
            )
        )


def _ensure_schema_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "glacier_recovery_sessions" not in table_names:
        return
    recovery_columns = {
        column["name"] for column in inspector.get_columns("glacier_recovery_sessions")
    }
    if "restore_paths_json" not in recovery_columns:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE glacier_recovery_sessions ADD COLUMN restore_paths_json TEXT")
            )
    recovery_column_sql = {
        "canceled_at": "TEXT",
        "paused_at": "TEXT",
        "paused_from_state": "TEXT",
        "failure_count": "INTEGER DEFAULT 0",
        "last_failure_at": "TEXT",
        "last_failure": "TEXT",
        "last_failure_notification_at": "TEXT",
        "canceled_notification_sent_at": "TEXT",
        "canceled_notification_next_attempt_at": "TEXT",
        "canceled_notification_failure": "TEXT",
    }
    missing_recovery_columns = [
        (name, column_sql)
        for name, column_sql in recovery_column_sql.items()
        if name not in recovery_columns
    ]
    if missing_recovery_columns:
        with engine.begin() as conn:
            for name, column_sql in missing_recovery_columns:
                conn.execute(
                    text(f"ALTER TABLE glacier_recovery_sessions ADD COLUMN {name} {column_sql}")
                )
    if "approved_at" in recovery_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE glacier_recovery_sessions DROP COLUMN approved_at"))


def _transition_pin_schema_to_fetches(engine: Engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "active_pins" not in table_names:
        return

    with engine.begin() as conn:
        conn.execute(
            text(
                """
INSERT INTO fetches (
    fetch_id,
    name,
    fetch_order,
    fetch_state,
    fetch_notification_sent_at,
    fetch_notification_next_attempt_at,
    fetch_notification_failure,
    fetch_notification_count
)
SELECT
    active_pins.fetch_id,
    active_pins.target,
    active_pins.fetch_order,
    CASE active_pins.fetch_state
        WHEN 'waiting_media' THEN 'queued_djdan'
        ELSE active_pins.fetch_state
    END,
    active_pins.fetch_notification_sent_at,
    active_pins.fetch_notification_next_attempt_at,
    active_pins.fetch_notification_failure,
    active_pins.fetch_notification_count
FROM active_pins
WHERE active_pins.fetch_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM fetches
        WHERE fetches.fetch_id = active_pins.fetch_id
    )
"""
            )
        )
        conn.execute(
            text(
                """
INSERT INTO fetch_selectors (fetch_id, target, selector_order)
SELECT active_pins.fetch_id, active_pins.target, 1
FROM active_pins
WHERE active_pins.fetch_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM fetch_selectors
        WHERE fetch_selectors.fetch_id = active_pins.fetch_id
            AND fetch_selectors.target = active_pins.target
    )
"""
            )
        )
        if conn.dialect.name == "postgresql":
            conn.execute(
                text(
                    "ALTER TABLE fetch_entries "
                    "DROP CONSTRAINT IF EXISTS fetch_entries_fetch_id_fkey"
                )
            )
            conn.execute(
                text(
                    """
DELETE FROM fetch_entries
WHERE NOT EXISTS (
    SELECT 1
    FROM fetches
    WHERE fetches.fetch_id = fetch_entries.fetch_id
)
"""
                )
            )
            conn.execute(
                text(
                    """
ALTER TABLE fetch_entries
ADD CONSTRAINT fetch_entries_fetch_id_fkey
FOREIGN KEY (fetch_id)
REFERENCES fetches(fetch_id)
ON DELETE CASCADE
"""
                )
            )
        _drop_legacy_hot_fetch_projection(conn)
        if conn.dialect.name == "postgresql":
            conn.execute(text("DROP TABLE IF EXISTS hot_fetch_operator_summaries CASCADE"))
            conn.execute(text("DROP TABLE IF EXISTS active_pins CASCADE"))


def _drop_legacy_hot_fetch_projection(conn: Connection) -> None:
    if conn.dialect.name == "sqlite":
        for trigger_name in (
            "riverhog_hfos_active_pins_insert",
            "riverhog_hfos_active_pins_update",
            "riverhog_hfos_collection_files_insert",
            "riverhog_hfos_collection_files_update",
            "riverhog_hfos_collection_files_delete",
            "riverhog_hfos_fetch_entries_insert",
            "riverhog_hfos_fetch_entries_update",
            "riverhog_hfos_fetch_entries_delete",
        ):
            conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name}"))
        return

    if conn.dialect.name != "postgresql":
        return

    for trigger_name, table_name in (
        ("riverhog_hfos_active_pins_insert", "active_pins"),
        ("riverhog_hfos_active_pins_update", "active_pins"),
        ("riverhog_hfos_collection_files_insert", "collection_files"),
        ("riverhog_hfos_collection_files_update", "collection_files"),
        ("riverhog_hfos_collection_files_delete", "collection_files"),
        ("riverhog_hfos_fetch_entries_insert", "fetch_entries"),
        ("riverhog_hfos_fetch_entries_update", "fetch_entries"),
        ("riverhog_hfos_fetch_entries_delete", "fetch_entries"),
    ):
        conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}"))

    for function_name in (
        "riverhog_refresh_hot_fetch_operator_summaries(text[])",
        "riverhog_hfos_from_new_active_pins()",
        "riverhog_hfos_from_changed_active_pins()",
        "riverhog_hfos_from_new_files()",
        "riverhog_hfos_from_old_files()",
        "riverhog_hfos_from_changed_files()",
        "riverhog_hfos_from_new_fetch_entries()",
        "riverhog_hfos_from_old_fetch_entries()",
        "riverhog_hfos_from_changed_fetch_entries()",
    ):
        conn.execute(text(f"DROP FUNCTION IF EXISTS {function_name} CASCADE"))


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


def _collection_image_operator_summary_select_sql(affected_pairs_sql: str) -> str:
    return f"""
SELECT *
FROM (
    WITH affected_pairs(collection_id, image_id) AS (
        {affected_pairs_sql}
    ),
    distinct_pairs AS (
        SELECT DISTINCT collection_id, image_id
        FROM affected_pairs
        WHERE collection_id IS NOT NULL
            AND image_id IS NOT NULL
    ),
    path_stats AS (
        SELECT
            finalized_image_covered_paths.collection_id,
            finalized_image_covered_paths.image_id,
            COUNT(finalized_image_covered_paths.path) AS covered_paths_total
        FROM finalized_image_covered_paths
        JOIN distinct_pairs
            ON distinct_pairs.collection_id = finalized_image_covered_paths.collection_id
            AND distinct_pairs.image_id = finalized_image_covered_paths.image_id
        GROUP BY
            finalized_image_covered_paths.collection_id,
            finalized_image_covered_paths.image_id
    )
    SELECT
        path_stats.collection_id,
        path_stats.image_id,
        path_stats.covered_paths_total,
        CAST(CURRENT_TIMESTAMP AS TEXT) AS updated_at
    FROM path_stats
) AS collection_image_operator_summary_refresh
"""


def _collection_image_operator_summary_upsert_sql(
    conn: Connection,
    affected_pairs_sql: str,
) -> str:
    columns = ", ".join(_COLLECTION_IMAGE_OPERATOR_SUMMARY_COLUMNS)
    select_sql = _collection_image_operator_summary_select_sql(affected_pairs_sql)
    if conn.dialect.name == "sqlite":
        return (
            f"INSERT OR REPLACE INTO collection_image_operator_summaries ({columns}) {select_sql}"
        )
    updates = ", ".join(
        f"{column} = EXCLUDED.{column}"
        for column in _COLLECTION_IMAGE_OPERATOR_SUMMARY_COLUMNS
        if column not in {"collection_id", "image_id"}
    )
    return (
        f"INSERT INTO collection_image_operator_summaries ({columns}) "
        f"{select_sql} "
        f"ON CONFLICT (collection_id, image_id) DO UPDATE SET {updates}"
    )


def _collection_image_operator_summary_delete_sql(
    conn: Connection,
    affected_pairs_sql: str,
) -> str:
    separator = "char(9)" if conn.dialect.name == "sqlite" else "E'\\t'"
    return f"""
DELETE FROM collection_image_operator_summaries
WHERE collection_id || {separator} || image_id IN (
    WITH affected_pairs(collection_id, image_id) AS (
        {affected_pairs_sql}
    ),
    distinct_pairs AS (
        SELECT DISTINCT collection_id, image_id
        FROM affected_pairs
        WHERE collection_id IS NOT NULL
            AND image_id IS NOT NULL
    )
    SELECT collection_id || {separator} || image_id
    FROM distinct_pairs
)
"""


def _refresh_collection_image_operator_summaries(
    conn: Connection,
    affected_pairs_sql: str,
) -> None:
    conn.execute(text(_collection_image_operator_summary_delete_sql(conn, affected_pairs_sql)))
    conn.execute(text(_collection_image_operator_summary_upsert_sql(conn, affected_pairs_sql)))


def _install_collection_image_operator_projection(engine: Engine) -> None:
    with engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            _install_sqlite_collection_image_operator_projection(conn)
            return
        if conn.dialect.name == "postgresql":
            _install_postgresql_collection_image_operator_projection(conn)
            return
        raise RuntimeError(f"unsupported catalog backend: {conn.dialect.name}")


def _install_sqlite_collection_image_operator_projection(conn: Connection) -> None:
    def refresh(affected_pairs_sql: str) -> str:
        delete_sql = _collection_image_operator_summary_delete_sql(conn, affected_pairs_sql)
        upsert_sql = _collection_image_operator_summary_upsert_sql(conn, affected_pairs_sql)
        return f"{delete_sql}; {upsert_sql};"

    trigger_sql = {
        "riverhog_cios_finalized_image_covered_paths_insert": (
            "finalized_image_covered_paths",
            refresh("SELECT NEW.collection_id AS collection_id, NEW.image_id AS image_id"),
        ),
        "riverhog_cios_finalized_image_covered_paths_update": (
            "finalized_image_covered_paths",
            refresh(
                "SELECT OLD.collection_id AS collection_id, OLD.image_id AS image_id "
                "UNION SELECT NEW.collection_id AS collection_id, NEW.image_id AS image_id"
            ),
        ),
        "riverhog_cios_finalized_image_covered_paths_delete": (
            "finalized_image_covered_paths",
            refresh("SELECT OLD.collection_id AS collection_id, OLD.image_id AS image_id"),
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


def _install_postgresql_collection_image_operator_projection(conn: Connection) -> None:
    affected_pairs_sql = (
        "SELECT "
        "split_part(pair_key, E'\\t', 1) AS collection_id, "
        "split_part(pair_key, E'\\t', 2) AS image_id "
        "FROM unnest(p_pair_keys) AS affected_pairs(pair_key)"
    )
    refresh_sql = _collection_image_operator_summary_upsert_sql(conn, affected_pairs_sql)
    delete_sql = _collection_image_operator_summary_delete_sql(conn, affected_pairs_sql)
    conn.execute(
        text(
            f"""
CREATE OR REPLACE FUNCTION riverhog_refresh_collection_image_operator_summaries(
    p_pair_keys text[]
) RETURNS void
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    IF p_pair_keys IS NULL OR cardinality(p_pair_keys) = 0 THEN
        RETURN;
    END IF;

    {delete_sql};
    {refresh_sql};
END;
$riverhog$;
"""
        )
    )
    trigger_functions = {
        "riverhog_cios_from_new_covered_paths": """
CREATE OR REPLACE FUNCTION riverhog_cios_from_new_covered_paths()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_collection_image_operator_summaries(
        ARRAY(
            SELECT DISTINCT collection_id || E'\\t' || image_id
            FROM new_rows
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_cios_from_old_covered_paths": """
CREATE OR REPLACE FUNCTION riverhog_cios_from_old_covered_paths()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_collection_image_operator_summaries(
        ARRAY(
            SELECT DISTINCT collection_id || E'\\t' || image_id
            FROM old_rows
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_cios_from_changed_covered_paths": """
CREATE OR REPLACE FUNCTION riverhog_cios_from_changed_covered_paths()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_collection_image_operator_summaries(
        ARRAY(
            SELECT DISTINCT pair_key
            FROM (
                SELECT collection_id || E'\\t' || image_id AS pair_key
                FROM old_rows
                UNION
                SELECT collection_id || E'\\t' || image_id AS pair_key
                FROM new_rows
            ) AS changed_pairs
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
    }
    for sql in trigger_functions.values():
        conn.execute(text(sql))

    trigger_sql = {
        "riverhog_cios_finalized_image_covered_paths_insert": (
            "INSERT",
            "NEW TABLE AS new_rows",
            "riverhog_cios_from_new_covered_paths",
        ),
        "riverhog_cios_finalized_image_covered_paths_update": (
            "UPDATE",
            "OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_cios_from_changed_covered_paths",
        ),
        "riverhog_cios_finalized_image_covered_paths_delete": (
            "DELETE",
            "OLD TABLE AS old_rows",
            "riverhog_cios_from_old_covered_paths",
        ),
    }
    for trigger_name, (operation, referencing, function_name) in trigger_sql.items():
        conn.execute(
            text(f"DROP TRIGGER IF EXISTS {trigger_name} ON finalized_image_covered_paths")
        )
        conn.execute(
            text(
                f"CREATE TRIGGER {trigger_name} "
                f"AFTER {operation} ON finalized_image_covered_paths "
                f"REFERENCING {referencing} "
                f"FOR EACH STATEMENT EXECUTE FUNCTION {function_name}()"
            )
        )


def _image_operator_summary_collection_agg_sql(conn: Connection) -> str:
    if conn.dialect.name == "sqlite":
        return "COALESCE(group_concat(image_collections.collection_id, char(10)), '')"
    return (
        "COALESCE(string_agg(image_collections.collection_id, "
        "chr(10) ORDER BY image_collections.collection_id), '')"
    )


def _image_operator_summary_select_sql(
    conn: Connection,
    affected_images_sql: str,
) -> str:
    collection_ids = _image_operator_summary_collection_agg_sql(conn)
    return f"""
SELECT *
FROM (
    WITH affected_images(image_id) AS (
        {affected_images_sql}
    ),
    distinct_images AS (
        SELECT DISTINCT image_id
        FROM affected_images
        WHERE image_id IS NOT NULL
    ),
    image_rows AS (
        SELECT finalized_images.*
        FROM finalized_images
        JOIN distinct_images
            ON distinct_images.image_id = finalized_images.image_id
    ),
    image_required AS (
        SELECT
            image_rows.image_id,
            CASE
                WHEN image_rows.required_copy_count > 0 THEN image_rows.required_copy_count
                ELSE 2
            END AS required_copy_count
        FROM image_rows
    ),
    image_collections AS (
        SELECT
            finalized_image_covered_paths.image_id,
            finalized_image_covered_paths.collection_id
        FROM finalized_image_covered_paths
        JOIN distinct_images
            ON distinct_images.image_id = finalized_image_covered_paths.image_id
        GROUP BY
            finalized_image_covered_paths.image_id,
            finalized_image_covered_paths.collection_id
        ORDER BY
            finalized_image_covered_paths.image_id,
            finalized_image_covered_paths.collection_id
    ),
    file_stats AS (
        SELECT
            finalized_image_covered_paths.image_id,
            COUNT(*) AS files
        FROM finalized_image_covered_paths
        JOIN distinct_images
            ON distinct_images.image_id = finalized_image_covered_paths.image_id
        GROUP BY finalized_image_covered_paths.image_id
    ),
    collection_stats AS (
        SELECT
            image_collections.image_id,
            COUNT(*) AS collections,
            {collection_ids} AS collection_ids_text
        FROM image_collections
        GROUP BY image_collections.image_id
    ),
    copy_stats AS (
        SELECT
            image_copies.image_id,
            COALESCE(
                SUM(
                    CASE
                        WHEN COALESCE(image_copies.state, 'registered')
                            IN ('registered', 'verified')
                        THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS physical_copies_registered,
            COALESCE(
                SUM(
                    CASE
                        WHEN COALESCE(image_copies.state, 'registered')
                            IN ('registered', 'verified')
                            AND COALESCE(image_copies.verification_state, 'pending') = 'verified'
                        THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS physical_copies_verified
        FROM image_copies
        JOIN distinct_images
            ON distinct_images.image_id = image_copies.image_id
        GROUP BY image_copies.image_id
    )
    SELECT
        image_rows.image_id,
        image_rows.filename,
        substr(image_rows.image_id, 1, 4)
            || '-'
            || substr(image_rows.image_id, 5, 2)
            || '-'
            || substr(image_rows.image_id, 7, 2)
            || 'T'
            || substr(image_rows.image_id, 10, 2)
            || ':'
            || substr(image_rows.image_id, 12, 2)
            || ':'
            || substr(image_rows.image_id, 14, 2)
            || 'Z' AS finalized_at,
        image_rows.bytes,
        image_rows.target_bytes,
        COALESCE(file_stats.files, 0) AS files,
        COALESCE(collection_stats.collections, 0) AS collections,
        COALESCE(collection_stats.collection_ids_text, '') AS collection_ids_text,
        CASE
            WHEN COALESCE(copy_stats.physical_copies_registered, 0)
                >= image_required.required_copy_count
            THEN 'protected'
            WHEN COALESCE(copy_stats.physical_copies_registered, 0) > 0
            THEN 'partially_protected'
            ELSE 'unprotected'
        END AS physical_protection_state,
        image_required.required_copy_count AS physical_copies_required,
        COALESCE(copy_stats.physical_copies_registered, 0) AS physical_copies_registered,
        COALESCE(copy_stats.physical_copies_verified, 0) AS physical_copies_verified,
        CASE
            WHEN image_required.required_copy_count
                - COALESCE(copy_stats.physical_copies_registered, 0) > 0
            THEN image_required.required_copy_count
                - COALESCE(copy_stats.physical_copies_registered, 0)
            ELSE 0
        END AS physical_copies_missing,
        CAST(CURRENT_TIMESTAMP AS TEXT) AS updated_at
    FROM image_rows
    JOIN image_required
        ON image_required.image_id = image_rows.image_id
    LEFT JOIN file_stats
        ON file_stats.image_id = image_rows.image_id
    LEFT JOIN collection_stats
        ON collection_stats.image_id = image_rows.image_id
    LEFT JOIN copy_stats
        ON copy_stats.image_id = image_rows.image_id
) AS image_operator_summary_refresh
"""


def _image_operator_summary_upsert_sql(
    conn: Connection,
    affected_images_sql: str,
) -> str:
    columns = ", ".join(_IMAGE_OPERATOR_SUMMARY_COLUMNS)
    select_sql = _image_operator_summary_select_sql(conn, affected_images_sql)
    if conn.dialect.name == "sqlite":
        return f"INSERT OR REPLACE INTO image_operator_summaries ({columns}) {select_sql}"
    updates = ", ".join(
        f"{column} = EXCLUDED.{column}"
        for column in _IMAGE_OPERATOR_SUMMARY_COLUMNS
        if column != "image_id"
    )
    return (
        f"INSERT INTO image_operator_summaries ({columns}) "
        f"{select_sql} "
        f"ON CONFLICT (image_id) DO UPDATE SET {updates}"
    )


def _refresh_image_operator_summaries(
    conn: Connection,
    affected_images_sql: str,
) -> None:
    conn.execute(text(_image_operator_summary_upsert_sql(conn, affected_images_sql)))


def _install_image_operator_projection(engine: Engine) -> None:
    with engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            _install_sqlite_image_operator_projection(conn)
            return
        if conn.dialect.name == "postgresql":
            _install_postgresql_image_operator_projection(conn)
            return
        raise RuntimeError(f"unsupported catalog backend: {conn.dialect.name}")


def _install_sqlite_image_operator_projection(conn: Connection) -> None:
    def refresh(affected_images_sql: str) -> str:
        columns = ", ".join(_IMAGE_OPERATOR_SUMMARY_COLUMNS)
        select_sql = _image_operator_summary_select_sql(conn, affected_images_sql)
        return f"INSERT OR REPLACE INTO image_operator_summaries ({columns}) {select_sql};"

    trigger_sql = {
        "riverhog_ios_finalized_images_insert": (
            "finalized_images",
            refresh("SELECT NEW.image_id AS image_id"),
        ),
        "riverhog_ios_finalized_images_update": (
            "finalized_images",
            refresh("SELECT OLD.image_id AS image_id UNION SELECT NEW.image_id AS image_id"),
        ),
        "riverhog_ios_finalized_image_covered_paths_insert": (
            "finalized_image_covered_paths",
            refresh("SELECT NEW.image_id AS image_id"),
        ),
        "riverhog_ios_finalized_image_covered_paths_update": (
            "finalized_image_covered_paths",
            refresh("SELECT OLD.image_id AS image_id UNION SELECT NEW.image_id AS image_id"),
        ),
        "riverhog_ios_finalized_image_covered_paths_delete": (
            "finalized_image_covered_paths",
            refresh("SELECT OLD.image_id AS image_id"),
        ),
        "riverhog_ios_image_copies_insert": (
            "image_copies",
            refresh("SELECT NEW.image_id AS image_id"),
        ),
        "riverhog_ios_image_copies_update": (
            "image_copies",
            refresh("SELECT OLD.image_id AS image_id UNION SELECT NEW.image_id AS image_id"),
        ),
        "riverhog_ios_image_copies_delete": (
            "image_copies",
            refresh("SELECT OLD.image_id AS image_id"),
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


def _install_postgresql_image_operator_projection(conn: Connection) -> None:
    refresh_sql = _image_operator_summary_upsert_sql(
        conn,
        "SELECT unnest(p_image_ids) AS image_id",
    )
    conn.execute(
        text(
            f"""
CREATE OR REPLACE FUNCTION riverhog_refresh_image_operator_summaries(
    p_image_ids text[]
) RETURNS void
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    IF p_image_ids IS NULL OR cardinality(p_image_ids) = 0 THEN
        RETURN;
    END IF;

    DELETE FROM image_operator_summaries
    WHERE image_id = ANY(p_image_ids)
        AND NOT EXISTS (
            SELECT 1
            FROM finalized_images
            WHERE finalized_images.image_id = image_operator_summaries.image_id
        );

    {refresh_sql};
END;
$riverhog$;
"""
        )
    )
    trigger_functions = {
        "riverhog_ios_from_new_finalized_images": """
CREATE OR REPLACE FUNCTION riverhog_ios_from_new_finalized_images()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_image_operator_summaries(
        ARRAY(SELECT DISTINCT image_id::text FROM new_rows WHERE image_id IS NOT NULL)
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_ios_from_changed_finalized_images": """
CREATE OR REPLACE FUNCTION riverhog_ios_from_changed_finalized_images()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_image_operator_summaries(
        ARRAY(
            SELECT DISTINCT image_id::text
            FROM (
                SELECT image_id::text AS image_id FROM old_rows
                UNION
                SELECT image_id::text AS image_id FROM new_rows
            ) AS affected
            WHERE image_id IS NOT NULL
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_ios_from_new_covered_paths": """
CREATE OR REPLACE FUNCTION riverhog_ios_from_new_covered_paths()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_image_operator_summaries(
        ARRAY(SELECT DISTINCT image_id::text FROM new_rows WHERE image_id IS NOT NULL)
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_ios_from_old_covered_paths": """
CREATE OR REPLACE FUNCTION riverhog_ios_from_old_covered_paths()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_image_operator_summaries(
        ARRAY(SELECT DISTINCT image_id::text FROM old_rows WHERE image_id IS NOT NULL)
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_ios_from_changed_covered_paths": """
CREATE OR REPLACE FUNCTION riverhog_ios_from_changed_covered_paths()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_image_operator_summaries(
        ARRAY(
            SELECT DISTINCT image_id::text
            FROM (
                SELECT image_id::text AS image_id FROM old_rows
                UNION
                SELECT image_id::text AS image_id FROM new_rows
            ) AS affected
            WHERE image_id IS NOT NULL
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_ios_from_new_image_copies": """
CREATE OR REPLACE FUNCTION riverhog_ios_from_new_image_copies()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_image_operator_summaries(
        ARRAY(SELECT DISTINCT image_id::text FROM new_rows WHERE image_id IS NOT NULL)
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_ios_from_old_image_copies": """
CREATE OR REPLACE FUNCTION riverhog_ios_from_old_image_copies()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_image_operator_summaries(
        ARRAY(SELECT DISTINCT image_id::text FROM old_rows WHERE image_id IS NOT NULL)
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_ios_from_changed_image_copies": """
CREATE OR REPLACE FUNCTION riverhog_ios_from_changed_image_copies()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_image_operator_summaries(
        ARRAY(
            SELECT DISTINCT image_id::text
            FROM (
                SELECT image_id::text AS image_id FROM old_rows
                UNION
                SELECT image_id::text AS image_id FROM new_rows
            ) AS affected
            WHERE image_id IS NOT NULL
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
            "riverhog_ios_finalized_images_insert",
            "finalized_images",
            "AFTER INSERT",
            "REFERENCING NEW TABLE AS new_rows",
            "riverhog_ios_from_new_finalized_images",
        ),
        (
            "riverhog_ios_finalized_images_update",
            "finalized_images",
            "AFTER UPDATE",
            "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_ios_from_changed_finalized_images",
        ),
        (
            "riverhog_ios_finalized_image_covered_paths_insert",
            "finalized_image_covered_paths",
            "AFTER INSERT",
            "REFERENCING NEW TABLE AS new_rows",
            "riverhog_ios_from_new_covered_paths",
        ),
        (
            "riverhog_ios_finalized_image_covered_paths_update",
            "finalized_image_covered_paths",
            "AFTER UPDATE",
            "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_ios_from_changed_covered_paths",
        ),
        (
            "riverhog_ios_finalized_image_covered_paths_delete",
            "finalized_image_covered_paths",
            "AFTER DELETE",
            "REFERENCING OLD TABLE AS old_rows",
            "riverhog_ios_from_old_covered_paths",
        ),
        (
            "riverhog_ios_image_copies_insert",
            "image_copies",
            "AFTER INSERT",
            "REFERENCING NEW TABLE AS new_rows",
            "riverhog_ios_from_new_image_copies",
        ),
        (
            "riverhog_ios_image_copies_update",
            "image_copies",
            "AFTER UPDATE",
            "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_ios_from_changed_image_copies",
        ),
        (
            "riverhog_ios_image_copies_delete",
            "image_copies",
            "AFTER DELETE",
            "REFERENCING OLD TABLE AS old_rows",
            "riverhog_ios_from_old_image_copies",
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


def _disc_operator_summary_select_sql(affected_copies_sql: str) -> str:
    return f"""
SELECT *
FROM (
    WITH affected_copies(image_id, copy_id) AS (
        {affected_copies_sql}
    ),
    distinct_copies AS (
        SELECT DISTINCT image_id, copy_id
        FROM affected_copies
        WHERE image_id IS NOT NULL
            AND copy_id IS NOT NULL
    ),
    copy_rows AS (
        SELECT
            image_copies.image_id,
            image_copies.copy_id,
            image_copies.label_text,
            image_copies.location,
            image_copies.created_at,
            COALESCE(image_copies.state, 'registered') AS state,
            COALESCE(image_copies.verification_state, 'pending') AS verification_state,
            finalized_images.filename
        FROM image_copies
        JOIN distinct_copies
            ON distinct_copies.image_id = image_copies.image_id
            AND distinct_copies.copy_id = image_copies.copy_id
        JOIN finalized_images
            ON finalized_images.image_id = image_copies.image_id
    )
    SELECT
        copy_rows.image_id,
        copy_rows.copy_id,
        copy_rows.label_text,
        copy_rows.location,
        copy_rows.created_at,
        copy_rows.state,
        copy_rows.verification_state,
        copy_rows.filename,
        CAST(CURRENT_TIMESTAMP AS TEXT) AS updated_at
    FROM copy_rows
) AS disc_operator_summary_refresh
"""


def _disc_operator_summary_upsert_sql(
    conn: Connection,
    affected_copies_sql: str,
) -> str:
    columns = ", ".join(_DISC_OPERATOR_SUMMARY_COLUMNS)
    select_sql = _disc_operator_summary_select_sql(affected_copies_sql)
    if conn.dialect.name == "sqlite":
        return f"INSERT OR REPLACE INTO disc_operator_summaries ({columns}) {select_sql}"
    updates = ", ".join(
        f"{column} = EXCLUDED.{column}"
        for column in _DISC_OPERATOR_SUMMARY_COLUMNS
        if column not in {"image_id", "copy_id"}
    )
    return (
        f"INSERT INTO disc_operator_summaries ({columns}) "
        f"{select_sql} "
        f"ON CONFLICT (image_id, copy_id) DO UPDATE SET {updates}"
    )


def _refresh_disc_operator_summaries(
    conn: Connection,
    affected_copies_sql: str,
) -> None:
    conn.execute(text(_disc_operator_summary_upsert_sql(conn, affected_copies_sql)))


def _install_disc_operator_projection(engine: Engine) -> None:
    with engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            _install_sqlite_disc_operator_projection(conn)
            return
        if conn.dialect.name == "postgresql":
            _install_postgresql_disc_operator_projection(conn)
            return
        raise RuntimeError(f"unsupported catalog backend: {conn.dialect.name}")


def _install_sqlite_disc_operator_projection(conn: Connection) -> None:
    def refresh(affected_copies_sql: str) -> str:
        columns = ", ".join(_DISC_OPERATOR_SUMMARY_COLUMNS)
        select_sql = _disc_operator_summary_select_sql(affected_copies_sql)
        return f"INSERT OR REPLACE INTO disc_operator_summaries ({columns}) {select_sql};"

    trigger_sql = {
        "riverhog_dos_image_copies_insert": (
            "image_copies",
            refresh("SELECT NEW.image_id AS image_id, NEW.copy_id AS copy_id"),
        ),
        "riverhog_dos_image_copies_update": (
            "image_copies",
            refresh(
                "SELECT OLD.image_id AS image_id, OLD.copy_id AS copy_id "
                "UNION SELECT NEW.image_id AS image_id, NEW.copy_id AS copy_id"
            ),
        ),
        "riverhog_dos_image_copies_delete": (
            "image_copies",
            refresh("SELECT OLD.image_id AS image_id, OLD.copy_id AS copy_id"),
        ),
        "riverhog_dos_finalized_images_update": (
            "finalized_images",
            refresh(
                "SELECT image_id, copy_id FROM image_copies "
                "WHERE image_id = OLD.image_id OR image_id = NEW.image_id"
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


def _install_postgresql_disc_operator_projection(conn: Connection) -> None:
    refresh_sql = _disc_operator_summary_upsert_sql(
        conn,
        "SELECT "
        "split_part(pair_key, E'\\t', 1) AS image_id, "
        "split_part(pair_key, E'\\t', 2) AS copy_id "
        "FROM unnest(p_pair_keys) AS affected_pairs(pair_key)",
    )
    conn.execute(
        text(
            f"""
CREATE OR REPLACE FUNCTION riverhog_refresh_disc_operator_summaries(
    p_pair_keys text[]
) RETURNS void
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    IF p_pair_keys IS NULL OR cardinality(p_pair_keys) = 0 THEN
        RETURN;
    END IF;

    DELETE FROM disc_operator_summaries
    WHERE image_id || E'\\t' || copy_id = ANY(p_pair_keys)
        AND NOT EXISTS (
            SELECT 1
            FROM image_copies
            WHERE image_copies.image_id = disc_operator_summaries.image_id
                AND image_copies.copy_id = disc_operator_summaries.copy_id
        );

    {refresh_sql};
END;
$riverhog$;
"""
        )
    )
    trigger_functions = {
        "riverhog_dos_from_new_image_copies": """
CREATE OR REPLACE FUNCTION riverhog_dos_from_new_image_copies()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_disc_operator_summaries(
        ARRAY(
            SELECT DISTINCT image_id::text || E'\\t' || copy_id::text
            FROM new_rows
            WHERE image_id IS NOT NULL AND copy_id IS NOT NULL
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_dos_from_old_image_copies": """
CREATE OR REPLACE FUNCTION riverhog_dos_from_old_image_copies()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_disc_operator_summaries(
        ARRAY(
            SELECT DISTINCT image_id::text || E'\\t' || copy_id::text
            FROM old_rows
            WHERE image_id IS NOT NULL AND copy_id IS NOT NULL
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_dos_from_changed_image_copies": """
CREATE OR REPLACE FUNCTION riverhog_dos_from_changed_image_copies()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_disc_operator_summaries(
        ARRAY(
            SELECT DISTINCT image_id::text || E'\\t' || copy_id::text
            FROM (
                SELECT image_id, copy_id FROM old_rows
                UNION
                SELECT image_id, copy_id FROM new_rows
            ) AS affected
            WHERE image_id IS NOT NULL AND copy_id IS NOT NULL
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_dos_from_changed_finalized_images": """
CREATE OR REPLACE FUNCTION riverhog_dos_from_changed_finalized_images()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_disc_operator_summaries(
        ARRAY(
            SELECT DISTINCT image_copies.image_id::text || E'\\t' || image_copies.copy_id::text
            FROM image_copies
            JOIN (
                SELECT image_id FROM old_rows
                UNION
                SELECT image_id FROM new_rows
            ) AS affected_images
                ON affected_images.image_id = image_copies.image_id
            WHERE image_copies.image_id IS NOT NULL
                AND image_copies.copy_id IS NOT NULL
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
            "riverhog_dos_image_copies_insert",
            "image_copies",
            "AFTER INSERT",
            "REFERENCING NEW TABLE AS new_rows",
            "riverhog_dos_from_new_image_copies",
        ),
        (
            "riverhog_dos_image_copies_update",
            "image_copies",
            "AFTER UPDATE",
            "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_dos_from_changed_image_copies",
        ),
        (
            "riverhog_dos_image_copies_delete",
            "image_copies",
            "AFTER DELETE",
            "REFERENCING OLD TABLE AS old_rows",
            "riverhog_dos_from_old_image_copies",
        ),
        (
            "riverhog_dos_finalized_images_update",
            "finalized_images",
            "AFTER UPDATE",
            "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_dos_from_changed_finalized_images",
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


def _fetch_selector_selects_file_sql(
    *,
    target: str,
    collection_id: str,
    path: str,
) -> str:
    full_path = f"({collection_id} || '/' || {path})"
    return (
        "("
        f"({target} LIKE '%/' AND substr({full_path}, 1, length({target})) = {target}) "
        "OR "
        f"({target} NOT LIKE '%/' AND {full_path} = {target})"
        ")"
    )


def _fetch_targets_agg_sql(conn: Connection) -> str:
    if conn.dialect.name == "sqlite":
        return "COALESCE(group_concat(ordered_selectors.target, char(10)), '')"
    return "COALESCE(string_agg(ordered_selectors.target, chr(10)), '')"


def _fetch_operator_summary_select_sql(
    conn: Connection,
    affected_fetches_sql: str,
) -> str:
    target_match = _fetch_selector_selects_file_sql(
        target="fetch_selectors.target",
        collection_id="collection_files.collection_id",
        path="collection_files.path",
    )
    targets_text = _fetch_targets_agg_sql(conn)
    return f"""
SELECT *
FROM (
    WITH affected_fetches(fetch_id) AS (
        {affected_fetches_sql}
    ),
    distinct_fetches AS (
        SELECT DISTINCT fetch_id
        FROM affected_fetches
        WHERE fetch_id IS NOT NULL
    ),
    fetch_rows AS (
        SELECT fetches.*
        FROM fetches
        JOIN distinct_fetches
            ON distinct_fetches.fetch_id = fetches.fetch_id
    ),
    ordered_selectors AS (
        SELECT fetch_selectors.*
        FROM fetch_selectors
        JOIN fetch_rows
            ON fetch_rows.fetch_id = fetch_selectors.fetch_id
        ORDER BY fetch_selectors.fetch_id, fetch_selectors.selector_order, fetch_selectors.target
    ),
    selector_stats AS (
        SELECT
            ordered_selectors.fetch_id,
            COUNT(ordered_selectors.target) AS selectors,
            {targets_text} AS targets_text
        FROM ordered_selectors
        GROUP BY ordered_selectors.fetch_id
    ),
    selected_files AS (
        SELECT DISTINCT
            fetch_rows.fetch_id,
            collection_files.collection_id,
            collection_files.path,
            collection_files.bytes,
            collection_files.hot
        FROM fetch_rows
        JOIN fetch_selectors
            ON fetch_selectors.fetch_id = fetch_rows.fetch_id
        JOIN collection_files
            ON {target_match}
    ),
    file_stats AS (
        SELECT
            selected_files.fetch_id,
            COUNT(selected_files.path) AS files,
            COALESCE(SUM(selected_files.bytes), 0) AS bytes,
            COALESCE(
                SUM(
                    CASE
                        WHEN selected_files.hot IS TRUE THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS hot_files,
            COALESCE(
                SUM(
                    CASE
                        WHEN selected_files.hot IS TRUE THEN selected_files.bytes
                        ELSE 0
                    END
                ),
                0
            ) AS hot_bytes
        FROM selected_files
        GROUP BY selected_files.fetch_id
    ),
    entry_stats AS (
        SELECT
            fetch_rows.fetch_id,
            COUNT(fetch_entries.entry_id) AS entries_total,
            COALESCE(
                SUM(
                    CASE
                        WHEN fetch_entries.entry_id IS NOT NULL
                            AND NOT (
                                fetch_entries.recovery_bytes > 0
                                AND fetch_entries.uploaded_bytes >= fetch_entries.recovery_bytes
                            )
                            AND fetch_entries.uploaded_bytes <= 0
                        THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS entries_pending,
            COALESCE(
                SUM(
                    CASE
                        WHEN fetch_entries.entry_id IS NOT NULL
                            AND NOT (
                                fetch_entries.recovery_bytes > 0
                                AND fetch_entries.uploaded_bytes >= fetch_entries.recovery_bytes
                            )
                            AND fetch_entries.uploaded_bytes > 0
                        THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS entries_partial,
            COALESCE(
                SUM(
                    CASE
                        WHEN fetch_entries.entry_id IS NOT NULL
                            AND fetch_entries.recovery_bytes > 0
                            AND fetch_entries.uploaded_bytes >= fetch_entries.recovery_bytes
                            AND fetch_rows.fetch_state != 'done'
                        THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS entries_byte_complete,
            COALESCE(
                SUM(
                    CASE
                        WHEN fetch_entries.entry_id IS NOT NULL
                            AND fetch_entries.recovery_bytes > 0
                            AND fetch_entries.uploaded_bytes >= fetch_entries.recovery_bytes
                            AND fetch_rows.fetch_state = 'done'
                        THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS entries_uploaded,
            COALESCE(SUM(fetch_entries.bytes), 0) AS entry_bytes,
            COALESCE(SUM(fetch_entries.recovery_bytes), 0) AS entry_recovery_bytes,
            COALESCE(SUM(fetch_entries.uploaded_bytes), 0) AS uploaded_bytes,
            MAX(fetch_entries.upload_expires_at) AS upload_state_expires_at
        FROM fetch_rows
        LEFT JOIN fetch_entries
            ON fetch_entries.fetch_id = fetch_rows.fetch_id
        GROUP BY fetch_rows.fetch_id
    )
    SELECT
        fetch_rows.fetch_id,
        fetch_rows.name,
        fetch_rows.fetch_order,
        fetch_rows.fetch_state,
        COALESCE(selector_stats.selectors, 0) AS selectors,
        COALESCE(selector_stats.targets_text, '') AS targets_text,
        COALESCE(file_stats.files, 0) AS files,
        COALESCE(file_stats.bytes, 0) AS bytes,
        COALESCE(file_stats.hot_files, 0) AS hot_files,
        COALESCE(file_stats.hot_bytes, 0) AS hot_bytes,
        COALESCE(file_stats.files, 0) - COALESCE(file_stats.hot_files, 0) AS missing_files,
        COALESCE(file_stats.bytes, 0) - COALESCE(file_stats.hot_bytes, 0) AS missing_bytes,
        COALESCE(entry_stats.entries_total, 0) AS entries_total,
        COALESCE(entry_stats.entries_pending, 0) AS entries_pending,
        COALESCE(entry_stats.entries_partial, 0) AS entries_partial,
        COALESCE(entry_stats.entries_byte_complete, 0) AS entries_byte_complete,
        COALESCE(entry_stats.entries_uploaded, 0) AS entries_uploaded,
        COALESCE(entry_stats.entry_bytes, 0) AS entry_bytes,
        COALESCE(entry_stats.entry_recovery_bytes, 0) AS entry_recovery_bytes,
        COALESCE(entry_stats.uploaded_bytes, 0) AS uploaded_bytes,
        CASE
            WHEN COALESCE(entry_stats.entry_recovery_bytes, 0)
                - COALESCE(entry_stats.uploaded_bytes, 0) > 0
            THEN COALESCE(entry_stats.entry_recovery_bytes, 0)
                - COALESCE(entry_stats.uploaded_bytes, 0)
            ELSE 0
        END AS upload_missing_bytes,
        entry_stats.upload_state_expires_at,
        CAST(CURRENT_TIMESTAMP AS TEXT) AS updated_at
    FROM fetch_rows
    LEFT JOIN selector_stats
        ON selector_stats.fetch_id = fetch_rows.fetch_id
    LEFT JOIN file_stats
        ON file_stats.fetch_id = fetch_rows.fetch_id
    LEFT JOIN entry_stats
        ON entry_stats.fetch_id = fetch_rows.fetch_id
) AS fetch_operator_summary_refresh
"""


def _fetch_operator_summary_upsert_sql(
    conn: Connection,
    affected_fetches_sql: str,
) -> str:
    columns = ", ".join(_FETCH_OPERATOR_SUMMARY_COLUMNS)
    select_sql = _fetch_operator_summary_select_sql(conn, affected_fetches_sql)
    if conn.dialect.name == "sqlite":
        return f"INSERT OR REPLACE INTO fetch_operator_summaries ({columns}) {select_sql}"
    updates = ", ".join(
        f"{column} = EXCLUDED.{column}"
        for column in _FETCH_OPERATOR_SUMMARY_COLUMNS
        if column != "fetch_id"
    )
    return (
        f"INSERT INTO fetch_operator_summaries ({columns}) "
        f"{select_sql} "
        f"ON CONFLICT (fetch_id) DO UPDATE SET {updates}"
    )


def _refresh_fetch_operator_summaries(
    conn: Connection,
    affected_fetches_sql: str,
) -> None:
    conn.execute(text(_fetch_operator_summary_upsert_sql(conn, affected_fetches_sql)))


def _install_fetch_operator_projection(engine: Engine) -> None:
    with engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            _install_sqlite_fetch_operator_projection(conn)
            return
        if conn.dialect.name == "postgresql":
            _install_postgresql_fetch_operator_projection(conn)
            return
        raise RuntimeError(f"unsupported catalog backend: {conn.dialect.name}")


def _sqlite_fetch_operator_refresh(conn: Connection, affected_fetches_sql: str) -> str:
    columns = ", ".join(_FETCH_OPERATOR_SUMMARY_COLUMNS)
    select_sql = _fetch_operator_summary_select_sql(conn, affected_fetches_sql)
    return f"INSERT OR REPLACE INTO fetch_operator_summaries ({columns}) {select_sql};"


def _sqlite_affected_fetch_ids_for_file(collection_id: str, path: str) -> str:
    return "SELECT fetch_id FROM fetch_selectors WHERE " + _fetch_selector_selects_file_sql(
        target="fetch_selectors.target",
        collection_id=collection_id,
        path=path,
    )


def _install_sqlite_fetch_operator_projection(conn: Connection) -> None:
    trigger_sql = {
        "riverhog_fos_fetches_insert": (
            "fetches",
            _sqlite_fetch_operator_refresh(conn, "SELECT NEW.fetch_id AS fetch_id"),
        ),
        "riverhog_fos_fetches_update": (
            "fetches",
            _sqlite_fetch_operator_refresh(
                conn, "SELECT OLD.fetch_id AS fetch_id UNION SELECT NEW.fetch_id AS fetch_id"
            ),
        ),
        "riverhog_fos_fetch_selectors_insert": (
            "fetch_selectors",
            _sqlite_fetch_operator_refresh(conn, "SELECT NEW.fetch_id AS fetch_id"),
        ),
        "riverhog_fos_fetch_selectors_update": (
            "fetch_selectors",
            _sqlite_fetch_operator_refresh(
                conn, "SELECT OLD.fetch_id AS fetch_id UNION SELECT NEW.fetch_id AS fetch_id"
            ),
        ),
        "riverhog_fos_fetch_selectors_delete": (
            "fetch_selectors",
            _sqlite_fetch_operator_refresh(conn, "SELECT OLD.fetch_id AS fetch_id"),
        ),
        "riverhog_fos_collection_files_insert": (
            "collection_files",
            _sqlite_fetch_operator_refresh(
                conn,
                _sqlite_affected_fetch_ids_for_file("NEW.collection_id", "NEW.path"),
            ),
        ),
        "riverhog_fos_collection_files_update": (
            "collection_files",
            _sqlite_fetch_operator_refresh(
                conn,
                _sqlite_affected_fetch_ids_for_file("OLD.collection_id", "OLD.path")
                + " UNION "
                + _sqlite_affected_fetch_ids_for_file("NEW.collection_id", "NEW.path"),
            ),
        ),
        "riverhog_fos_collection_files_delete": (
            "collection_files",
            _sqlite_fetch_operator_refresh(
                conn,
                _sqlite_affected_fetch_ids_for_file("OLD.collection_id", "OLD.path"),
            ),
        ),
        "riverhog_fos_fetch_entries_insert": (
            "fetch_entries",
            _sqlite_fetch_operator_refresh(conn, "SELECT NEW.fetch_id AS fetch_id"),
        ),
        "riverhog_fos_fetch_entries_update": (
            "fetch_entries",
            _sqlite_fetch_operator_refresh(
                conn,
                "SELECT OLD.fetch_id AS fetch_id UNION SELECT NEW.fetch_id AS fetch_id",
            ),
        ),
        "riverhog_fos_fetch_entries_delete": (
            "fetch_entries",
            _sqlite_fetch_operator_refresh(conn, "SELECT OLD.fetch_id AS fetch_id"),
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


def _install_postgresql_fetch_operator_projection(conn: Connection) -> None:
    target_match = _fetch_selector_selects_file_sql(
        target="fetch_selectors.target",
        collection_id="affected_files.collection_id",
        path="affected_files.path",
    )
    refresh_sql = _fetch_operator_summary_upsert_sql(
        conn,
        "SELECT unnest(p_fetch_ids) AS fetch_id",
    )
    conn.execute(
        text(
            f"""
CREATE OR REPLACE FUNCTION riverhog_refresh_fetch_operator_summaries(
    p_fetch_ids text[]
) RETURNS void
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    IF p_fetch_ids IS NULL OR cardinality(p_fetch_ids) = 0 THEN
        RETURN;
    END IF;

    DELETE FROM fetch_operator_summaries
    WHERE fetch_id = ANY(p_fetch_ids)
        AND NOT EXISTS (
            SELECT 1
            FROM fetches
            WHERE fetches.fetch_id = fetch_operator_summaries.fetch_id
        );

    {refresh_sql};
END;
$riverhog$;
"""
        )
    )
    trigger_functions = {
        "riverhog_fos_from_new_fetches": """
CREATE OR REPLACE FUNCTION riverhog_fos_from_new_fetches()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_fetch_operator_summaries(
        ARRAY(SELECT DISTINCT fetch_id::text FROM new_rows WHERE fetch_id IS NOT NULL)
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_fos_from_changed_fetches": """
CREATE OR REPLACE FUNCTION riverhog_fos_from_changed_fetches()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_fetch_operator_summaries(
        ARRAY(
            SELECT DISTINCT fetch_id::text
            FROM (
                SELECT fetch_id::text AS fetch_id FROM old_rows
                UNION
                SELECT fetch_id::text AS fetch_id FROM new_rows
            ) AS affected
            WHERE fetch_id IS NOT NULL
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_fos_from_old_fetch_selectors": """
CREATE OR REPLACE FUNCTION riverhog_fos_from_old_fetch_selectors()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_fetch_operator_summaries(
        ARRAY(SELECT DISTINCT fetch_id::text FROM old_rows WHERE fetch_id IS NOT NULL)
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_fos_from_new_fetch_selectors": """
CREATE OR REPLACE FUNCTION riverhog_fos_from_new_fetch_selectors()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_fetch_operator_summaries(
        ARRAY(SELECT DISTINCT fetch_id::text FROM new_rows WHERE fetch_id IS NOT NULL)
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_fos_from_changed_fetch_selectors": """
CREATE OR REPLACE FUNCTION riverhog_fos_from_changed_fetch_selectors()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_fetch_operator_summaries(
        ARRAY(
            SELECT DISTINCT fetch_id::text
            FROM (
                SELECT fetch_id::text AS fetch_id FROM old_rows
                UNION
                SELECT fetch_id::text AS fetch_id FROM new_rows
            ) AS affected
            WHERE fetch_id IS NOT NULL
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_fos_from_new_files": f"""
CREATE OR REPLACE FUNCTION riverhog_fos_from_new_files()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_fetch_operator_summaries(
        ARRAY(
            SELECT DISTINCT fetch_selectors.fetch_id::text
            FROM fetch_selectors
            JOIN new_rows AS affected_files
                ON {target_match}
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_fos_from_old_files": f"""
CREATE OR REPLACE FUNCTION riverhog_fos_from_old_files()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_fetch_operator_summaries(
        ARRAY(
            SELECT DISTINCT fetch_selectors.fetch_id::text
            FROM fetch_selectors
            JOIN old_rows AS affected_files
                ON {target_match}
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_fos_from_changed_files": f"""
CREATE OR REPLACE FUNCTION riverhog_fos_from_changed_files()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_fetch_operator_summaries(
        ARRAY(
            SELECT DISTINCT fetch_selectors.fetch_id::text
            FROM fetch_selectors
            JOIN (
                SELECT collection_id, path FROM old_rows
                UNION
                SELECT collection_id, path FROM new_rows
            ) AS affected_files
                ON {target_match}
        )
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_fos_from_new_fetch_entries": """
CREATE OR REPLACE FUNCTION riverhog_fos_from_new_fetch_entries()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_fetch_operator_summaries(
        ARRAY(SELECT DISTINCT fetch_id::text FROM new_rows WHERE fetch_id IS NOT NULL)
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_fos_from_old_fetch_entries": """
CREATE OR REPLACE FUNCTION riverhog_fos_from_old_fetch_entries()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_fetch_operator_summaries(
        ARRAY(SELECT DISTINCT fetch_id::text FROM old_rows WHERE fetch_id IS NOT NULL)
    );
    RETURN NULL;
END;
$riverhog$;
""",
        "riverhog_fos_from_changed_fetch_entries": """
CREATE OR REPLACE FUNCTION riverhog_fos_from_changed_fetch_entries()
RETURNS trigger
LANGUAGE plpgsql
AS $riverhog$
BEGIN
    PERFORM riverhog_refresh_fetch_operator_summaries(
        ARRAY(
            SELECT DISTINCT fetch_id::text
            FROM (
                SELECT fetch_id::text AS fetch_id FROM old_rows
                UNION
                SELECT fetch_id::text AS fetch_id FROM new_rows
            ) AS affected
            WHERE fetch_id IS NOT NULL
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
            "riverhog_fos_fetches_insert",
            "fetches",
            "AFTER INSERT",
            "REFERENCING NEW TABLE AS new_rows",
            "riverhog_fos_from_new_fetches",
        ),
        (
            "riverhog_fos_fetches_update",
            "fetches",
            "AFTER UPDATE",
            "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_fos_from_changed_fetches",
        ),
        (
            "riverhog_fos_fetch_selectors_insert",
            "fetch_selectors",
            "AFTER INSERT",
            "REFERENCING NEW TABLE AS new_rows",
            "riverhog_fos_from_new_fetch_selectors",
        ),
        (
            "riverhog_fos_fetch_selectors_update",
            "fetch_selectors",
            "AFTER UPDATE",
            "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_fos_from_changed_fetch_selectors",
        ),
        (
            "riverhog_fos_fetch_selectors_delete",
            "fetch_selectors",
            "AFTER DELETE",
            "REFERENCING OLD TABLE AS old_rows",
            "riverhog_fos_from_old_fetch_selectors",
        ),
        (
            "riverhog_fos_collection_files_insert",
            "collection_files",
            "AFTER INSERT",
            "REFERENCING NEW TABLE AS new_rows",
            "riverhog_fos_from_new_files",
        ),
        (
            "riverhog_fos_collection_files_update",
            "collection_files",
            "AFTER UPDATE",
            "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_fos_from_changed_files",
        ),
        (
            "riverhog_fos_collection_files_delete",
            "collection_files",
            "AFTER DELETE",
            "REFERENCING OLD TABLE AS old_rows",
            "riverhog_fos_from_old_files",
        ),
        (
            "riverhog_fos_fetch_entries_insert",
            "fetch_entries",
            "AFTER INSERT",
            "REFERENCING NEW TABLE AS new_rows",
            "riverhog_fos_from_new_fetch_entries",
        ),
        (
            "riverhog_fos_fetch_entries_update",
            "fetch_entries",
            "AFTER UPDATE",
            "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
            "riverhog_fos_from_changed_fetch_entries",
        ),
        (
            "riverhog_fos_fetch_entries_delete",
            "fetch_entries",
            "AFTER DELETE",
            "REFERENCING OLD TABLE AS old_rows",
            "riverhog_fos_from_old_fetch_entries",
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
        CandidateCoveredPathRecord,
        CollectionArchiveRecord,
        CollectionFileRecord,
        CollectionImageOperatorSummaryRecord,
        CollectionOperatorSummaryRecord,
        CollectionRecord,
        CollectionUploadFileRecord,
        CollectionUploadRecord,
        DiscOperatorSummaryRecord,
        FetchEntryRecord,
        FetchOperatorSummaryRecord,
        FetchRecord,
        FetchSelectorRecord,
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
        ImageOperatorSummaryRecord,
        PlannedCandidateRecord,
    )

    _ = (
        CandidateCoveredPathRecord,
        CollectionArchiveRecord,
        CollectionFileRecord,
        CollectionImageOperatorSummaryRecord,
        CollectionOperatorSummaryRecord,
        CollectionRecord,
        DiscOperatorSummaryRecord,
        FetchEntryRecord,
        FetchOperatorSummaryRecord,
        FetchRecord,
        FetchSelectorRecord,
        FileCopyRecord,
        FinalizedImageCollectionArtifactRecord,
        FinalizedImageCoveragePartRecord,
        FinalizedImageCoveredPathRecord,
        FinalizedImageRecord,
        GlacierRecoverySessionImageRecord,
        GlacierRecoverySessionCollectionRecord,
        GlacierRecoverySessionRecord,
        GlacierUsageSnapshotRecord,
        ImageOperatorSummaryRecord,
        ImageCopyEventRecord,
        ImageCopyRecord,
        CollectionUploadFileRecord,
        CollectionUploadRecord,
        PlannedCandidateRecord,
    )
    engine = create_catalog_engine(database_url)
    _check_database_is_baseline_compatible(engine)
    Base.metadata.create_all(engine)
    _transition_pin_schema_to_fetches(engine)
    _ensure_schema_columns(engine)
    _ensure_schema_indexes(engine)
    _ensure_schema_baseline_marker(engine)
    _install_collection_operator_projection(engine)
    _install_collection_image_operator_projection(engine)
    _install_image_operator_projection(engine)
    _install_disc_operator_projection(engine)
    _install_fetch_operator_projection(engine)


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
