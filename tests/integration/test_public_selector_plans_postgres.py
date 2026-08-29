from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from http_api_contracts import closed_literal_values
from riverhog_api.app import create_app as create_riverhog_app
from riverhog_core.app_permissions import (
    ALL_RESOURCES,
    CATALOG_READ,
    PROVENANCE_READ,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.catalog_db import create_catalog_engine, initialize_db
from riverhog_core.services.app_keys import (
    _access_list_statement,
    _app_list_statement,
    _key_list_statement,
)
from riverhog_core.services.archive_copies import _archive_copy_list_statement
from riverhog_core.services.collection_uploads import _upload_list_statement
from riverhog_core.services.collection_workflows import _claim_list_statement
from riverhog_core.services.collections import _collection_list_statement
from riverhog_core.services.download_allowances import _key_quota_statements
from riverhog_core.services.provenance import _provenance_file_statement
from riverhog_core.services.retrieval import _cache_list_statement
from riverhog_core.services.search import _search_statement
from riverhog_core.services.tags import _tag_list_statements
from riverhog_protocol import (
    ApplicationAccessSort,
    ApplicationKeySort,
    ApplicationSort,
    ArchiveCopySort,
    CollectionSort,
    CollectionUploadSort,
    DownloadQuotaSort,
    ProcessingClaimSort,
    ProvenanceSort,
    RetrievalCacheSort,
    SearchSort,
    TagSort,
)
from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url
from stove0_core.persistence import (
    _evaluation_list_statement,
    _work_list_statement,
    stove0_state_schema,
)
from stove0_operator_contracts import EvaluationSort, WorkSort
from time_formats import parse_utc_timestamp

from scripts.operation_qualification import (
    create_adapter_contract_app,
    create_stove0_contract_app,
)

pytestmark = pytest.mark.integration
_ROWS = 16384
_NOW = "2026-08-28T00:00:00.000000Z"
_READER = ApplicationPrincipal(
    app="qualification",
    key_id=None,
    access=frozenset(
        {
            ApplicationAccess(CATALOG_READ, ALL_RESOURCES),
            ApplicationAccess(PROVENANCE_READ, ALL_RESOURCES),
        }
    ),
)

# Every database-backed public selector maps to one semantic statement family.
# Page bounds, exact-resource lookups, cursor feeds, and bounded configured-store
# enumeration are classified separately below; none may disappear between this
# inventory and the physical-plan proof without failing the suite.
_DATABASE_PLAN_OPERATIONS = {
    ("riverhog", "list_app_key_access"): "application-access",
    ("riverhog", "stream_app_key_access"): "application-access",
    ("riverhog", "list_app_keys"): "application-keys",
    ("riverhog", "stream_app_keys"): "application-keys",
    ("riverhog", "list_apps"): "applications",
    ("riverhog", "stream_apps"): "applications",
    ("riverhog", "list_archive_copy_jobs"): "archive-copies",
    ("riverhog", "stream_archive_copy_jobs"): "archive-copies",
    ("riverhog", "list_collection_provenance"): "provenance",
    ("riverhog", "stream_collection_provenance"): "provenance",
    ("riverhog", "list_collection_upload_sessions"): "uploads",
    ("riverhog", "stream_collection_upload_sessions"): "uploads",
    ("riverhog", "list_collections"): "collections",
    ("riverhog", "stream_collections"): "collections",
    ("riverhog", "list_download_quotas"): "download-quotas",
    ("riverhog", "stream_download_quotas"): "download-quotas",
    ("riverhog", "list_processing_claims"): "processing-claims",
    ("riverhog", "stream_processing_claims"): "processing-claims",
    ("riverhog", "list_retrieval_cache_objects"): "retrieval-cache",
    ("riverhog", "stream_retrieval_cache_objects"): "retrieval-cache",
    ("riverhog", "search"): "search",
    ("riverhog", "stream_search"): "search",
    ("riverhog", "list_tags"): "tags",
    ("riverhog", "stream_tags"): "tags",
    ("stove0", "list_evaluations"): "stove0-evaluations",
    ("stove0", "stream_evaluations"): "stove0-evaluations",
    ("stove0", "list_work"): "stove0-work",
    ("stove0", "stream_work"): "stove0-work",
}
_DATABASE_FILTER_SELECTORS = {
    "application-access": {"active", "app", "key", "permission", "q", "resource"},
    "application-keys": {"active", "q"},
    "applications": {"active", "q"},
    "archive-copies": {"q", "state"},
    "collections": {"encryption_format", "passphrase_id", "q", "tag"},
    "download-quotas": {"active", "app", "q"},
    "processing-claims": {"state"},
    "provenance": {"q", "status"},
    "retrieval-cache": {
        "collection_id",
        "expires_after",
        "expires_before",
        "protection",
        "q",
        "source_store",
        "state",
        "tag",
    },
    "search": {"collection", "q"},
    "stove0-evaluations": {"phase", "q"},
    "stove0-work": {"phase", "q"},
    "tags": {"q"},
    "uploads": {"q", "state", "tag"},
}
_NON_PLAN_QUERY_OPERATIONS = {
    ("riverhog", "download_retrieval_file"): {"collection_id", "path"},
    ("riverhog", "list_archive_stores"): {"order", "page", "per_page", "q", "sort"},
    ("riverhog", "stream_archive_stores"): {"order", "q", "sort"},
    ("riverhog", "list_collection_upload_session_files"): {"page", "per_page"},
    ("riverhog", "list_lifecycle_events"): {"after", "limit"},
    ("riverhog", "plan_collection_deletion"): {"retirement_claim_id"},
    ("riverhog", "resourcesync_change_list"): {"after"},
    ("riverhog", "trace_collection_file_provenance"): {"page", "per_page"},
    ("stove0", "get_artifact_selection"): {"page", "per_page"},
    ("stove0", "get_recipe"): {"revision"},
    ("stove0", "list_events"): {"after", "limit"},
}


@dataclass(frozen=True)
class _PlanCase:
    id: str
    statement: object
    expected_indexes: frozenset[str] = frozenset()
    expected_nodes: frozenset[str] = frozenset()
    database: str = "riverhog"


@dataclass(frozen=True)
class _QualifiedEngines:
    riverhog: Engine
    stove0: Engine


@pytest.fixture(scope="module")
def qualified_engines() -> Iterator[_QualifiedEngines]:
    value = os.getenv("RIVERHOG_TEST_POSTGRES_URL", "").strip()
    if not value:
        pytest.skip("RIVERHOG_TEST_POSTGRES_URL is required")
    suffix = uuid4().hex
    schemas = (f"riverhog_selector_plans_{suffix}", f"stove0_selector_plans_{suffix}")
    admin = create_catalog_engine(value)
    with admin.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public"))
        for schema in schemas:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    riverhog_url = make_url(value).update_query_dict(
        {"options": f"-csearch_path={schemas[0]},public"}
    )
    stove0_url = make_url(value).update_query_dict(
        {"options": f"-csearch_path={schemas[1]},public"}
    )
    riverhog_database_url = riverhog_url.render_as_string(hide_password=False)
    stove0_database_url = stove0_url.render_as_string(hide_password=False)
    initialize_db(riverhog_database_url)
    stove0_state_schema(stove0_database_url).upgrade()
    riverhog_engine = create_catalog_engine(riverhog_database_url)
    stove0_engine = create_catalog_engine(stove0_database_url)
    _seed_selector_relations(riverhog_engine, rows=_ROWS)
    _seed_stove0_selector_relations(stove0_engine, rows=_ROWS)
    try:
        yield _QualifiedEngines(riverhog=riverhog_engine, stove0=stove0_engine)
    finally:
        riverhog_engine.dispose()
        stove0_engine.dispose()
        with admin.begin() as connection:
            for schema in schemas:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _seed_selector_relations(engine: Engine, *, rows: int) -> None:
    timestamp = (
        "'2026-08-28T00:00:' || lpad((g % 60)::text, 2, '0') || '.' || lpad(g::text, 6, '0') || 'Z'"
    )
    sha = "repeat(md5(g::text), 2)"
    with engine.begin() as connection:
        for statement in (
            f"""
            INSERT INTO tags (id, created_by_app, created_at, collection_count)
            SELECT 'tag-' || lpad(g::text, 6, '0'), 'qualification', {timestamp}, g
            FROM generate_series(1, {rows}) AS g
            """,
            f"""
            INSERT INTO collections (
                id, creation_idempotency_key, creation_identity_sha256,
                creation_custody_mode, content_identity, encryption_format,
                passphrase_id, provenance_mode, provenance_identity, record_etag,
                metadata_revision, metadata_updated_at, ingest_source,
                created_by_app, created_at, file_count, file_bytes
            )
            SELECT g, 'collection-' || g, {sha}, 'producer-retained', {sha},
                   CASE WHEN g = 4096 THEN 'age-v1-scrypt' ELSE 'age-v1-other' END,
                   CASE WHEN g = 4096 THEN 'qualification-key-v1'
                        ELSE 'qualification-key-v2' END,
                   'omitted', NULL, {sha}, 1, {timestamp},
                   'fixture-' || lpad(g::text, 6, '0'), 'qualification', {timestamp},
                   1, g
            FROM generate_series(1, {rows}) AS g
            """,
            f"""
            INSERT INTO collection_files (
                collection_id, path, bytes, sha256, provenance_status
            )
            SELECT g, 'camera/file-' || lpad(g::text, 6, '0') || '.bin', g,
                   {sha}, 'omitted'
            FROM generate_series(1, {rows}) AS g
            """,
            f"""
            INSERT INTO collection_files (
                collection_id, path, bytes, sha256, provenance_status
            )
            SELECT 1, 'archive/member-' || md5(g::text) || '.bin', g,
                   {sha}, CASE WHEN g % 2 = 0 THEN 'captured' ELSE 'omitted' END
            FROM generate_series(1, {rows}) AS g
            """,
            f"""
            UPDATE collections SET file_count = {rows + 1},
                                   file_bytes = {rows * (rows + 1) // 2 + 1}
            WHERE id = 1
            """,
            """
            INSERT INTO collection_file_provenance (
                collection_id, path, status, journal_id, current_state_id,
                omission_reason
            )
            SELECT collection_id, path, 'omitted', NULL, NULL, 'qualification fixture'
            FROM collection_files
            WHERE provenance_status = 'omitted'
            """,
            f"""
            INSERT INTO collection_tags (
                collection_id, tag_id, assigned_by_app, assigned_at
            )
            SELECT g, 'tag-' || lpad(g::text, 6, '0'), 'qualification', {timestamp}
            FROM generate_series(1, {rows}) AS g
            """,
            f"""
            INSERT INTO collection_uploads (
                collection_id, idempotency_key, creation_identity_sha256,
                ingest_source, provenance_mode, provenance_omission_reason,
                provenance_identity, encryption_format, passphrase_id,
                initiated_by_app, initiated_by_key_id, event_context_json,
                state, custody_mode, lease_expires_at, orphaned_at, archive_store,
                opened_at, last_activity_at, closed_at, archive_phase,
                archive_phase_updated_at, archive_attempt_count,
                archive_next_attempt_at, archive_last_attempt_at, archive_failure,
                archive_storage_prefix, collection_manifest_bytes_b64,
                collection_manifest_proof_bytes_b64, planner_checkpoint_json,
                file_count, file_bytes, custodied_file_count, custodied_file_bytes
            )
            SELECT {rows} + g, 'upload-' || g, {sha},
                   'source-' || lpad(g::text, 6, '0'), 'omitted',
                   'qualification fixture', NULL, 'age-v1-scrypt',
                   'qualification-key-v1', 'qualification', NULL, NULL,
                   CASE WHEN g = 4096 THEN 'open' ELSE 'orphaned' END,
                   'producer-retained', NULL,
                   CASE WHEN g = 4096 THEN NULL ELSE {timestamp} END,
                   'archive', {timestamp}, {timestamp}, NULL,
                   CASE WHEN g = 4096 THEN 'planning' ELSE 'orphaned' END,
                   {timestamp}, 0, NULL, NULL, NULL, 'qualification/' || g,
                   NULL, NULL, '{{}}', g % 32, g * 1024, 0, 0
            FROM generate_series(1, {rows}) AS g
            """,
            f"""
            INSERT INTO collection_upload_tags (collection_id, tag_id)
            SELECT {rows} + g, 'tag-' || lpad(g::text, 6, '0')
            FROM generate_series(1, {rows}) AS g
            """,
            f"""
            INSERT INTO app_keys (
                id, app, token_sha256, monthly_download_quota_bytes,
                created_at, expires_at, revoked_at, last_used_at
            )
            SELECT substring(md5(g::text), 1, 16),
                   CASE WHEN g <= {rows // 2} THEN 'app-000'
                        ELSE 'app-' || lpad(g::text, 6, '0') END, {sha},
                   1000000000 + g, {timestamp},
                   CASE WHEN g % 3 = 0 THEN '2027-08-28T00:00:00.000000Z' ELSE NULL END,
                   CASE WHEN g = 4096 THEN NULL ELSE {timestamp} END,
                   CASE WHEN g % 5 = 0 THEN {timestamp} ELSE NULL END
            FROM generate_series(1, {rows}) AS g
            """,
            f"""
            INSERT INTO app_key_access_grants (
                key_id, permission, resource, created_at
            )
            SELECT substring(md5(g::text), 1, 16),
                   CASE WHEN g = 4096 THEN 'provenance:read' ELSE 'catalog:read' END,
                   CASE WHEN g = 4096 THEN 'tag:tag-000001' ELSE '*' END,
                   {timestamp}
            FROM generate_series(1, {rows}) AS g
            """,
            f"""
            INSERT INTO key_download_usage (
                key_id, month_started_at, accounted_bytes, updated_at
            )
            SELECT substring(md5(g::text), 1, 16),
                   '2026-08-01T00:00:00.000000Z', g * 1000, {timestamp}
            FROM generate_series(1, {rows}) AS g
            """,
            f"""
            INSERT INTO key_download_reservations (
                id, key_id, job_id, kind, month_started_at, reserved_bytes,
                created_at, expires_at
            )
            SELECT 'reservation-' || lpad(g::text, 6, '0'),
                   substring(md5(g::text), 1, 16), 'job-' || g,
                   CASE WHEN g % 2 = 0 THEN 'job' ELSE 'stream' END,
                   '2026-08-01T00:00:00.000000Z', g * 10, {timestamp},
                   CASE WHEN g % 2 = 0 THEN '2027-08-28T00:00:00.000000Z'
                        ELSE '2026-01-01T00:00:00.000000Z' END
            FROM generate_series(1, {rows}) AS g
            """,
            f"""
            INSERT INTO collection_archive_copies (
                collection_id, store, state, archive_storage_prefix,
                last_uploaded_at, last_verified_at
            )
            SELECT g, 'archive-' || lpad((g % 16)::text, 2, '0'), 'uploaded',
                   'collections/' || g, {timestamp}, {timestamp}
            FROM generate_series(1, {rows}) AS g
            """,
            f"""
            INSERT INTO collection_archive_objects (
                collection_id, store, object_id, object_order, kind, object_path,
                plaintext_bytes, stored_bytes, sha256, stored_sha256, revision,
                uploaded_at, verified_at
            )
            SELECT g, 'archive-' || lpad((g % 16)::text, 2, '0'),
                   'object-' || lpad(g::text, 6, '0'), 0, 'pack',
                   'collections/' || g || '/object', g, g + 64, {sha}, {sha},
                   'revision-' || g, {timestamp}, {timestamp}
            FROM generate_series(1, {rows}) AS g
            """,
            f"""
            INSERT INTO retrieval_cache_objects (
                source_store, collection_id, object_id, object_path, revision,
                stored_bytes, stored_sha256, cached_at, verified_at, state
            )
            SELECT 'archive-' || lpad((g % 16)::text, 2, '0'), g,
                   'object-' || lpad(g::text, 6, '0'),
                   'collections/' || g || '/object', 'revision-' || g,
                   g + 64, {sha}, {timestamp}, {timestamp},
                   CASE WHEN g = 4096 THEN 'delete_pending' ELSE 'ready' END
            FROM generate_series(1, {rows}) AS g
            """,
            f"""
            INSERT INTO retrieval_cache_leases (
                owner, source_store, collection_id, object_id, expires_at
            )
            SELECT 'indexer-' || (g % 8),
                   'archive-' || lpad((g % 16)::text, 2, '0'), g,
                   'object-' || lpad(g::text, 6, '0'),
                   CASE WHEN g % 2 = 0 THEN '2027-08-28T00:00:00.000000Z'
                        ELSE '2026-01-01T00:00:00.000000Z' END
            FROM generate_series(1, {rows}) AS g
            """,
            f"""
            INSERT INTO archive_copy_jobs (
                collection_id, destination_store, destination_storage_prefix,
                source_store, initiated_by_app, state, requested_at
            )
            SELECT g, 'copy-' || lpad((g % 16)::text, 2, '0'),
                   'copies/' || g, 'archive-' || lpad((g % 16)::text, 2, '0'),
                   'qualification',
                   CASE WHEN g = 4096 THEN 'requested' ELSE 'waiting' END,
                   {timestamp}
            FROM generate_series(1, {rows}) AS g
            """,
            f"""
            INSERT INTO collection_processing_claims (
                id, work_id, consumer_app, purpose, work_document_json,
                work_document_sha256, execution_id, retirement_grace_seconds,
                state, fence, expires_at, created_at, updated_at
            )
            SELECT repeat(md5('claim-' || g), 2), repeat(md5('work-' || g), 2),
                   'qualification', 'archive-' || g, '{{}}', {sha},
                   repeat(md5('execution-' || g), 2), 0,
                   CASE WHEN g % 2 = 0 THEN 'active' ELSE 'settled' END,
                   1, '2027-08-28T00:00:00.000000Z', {timestamp}, {timestamp}
            FROM generate_series(1, {rows}) AS g
            """,
        ):
            connection.execute(text(statement))
        connection.exec_driver_sql("ANALYZE")


def _seed_stove0_selector_relations(engine: Engine, *, rows: int) -> None:
    timestamp = (
        "'2026-08-28T00:00:' || lpad((g % 60)::text, 2, '0') || '.' || lpad(g::text, 6, '0') || 'Z'"
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
            INSERT INTO stove0_work_records (
                work_id, revision, phase, updated_at, document_bytes, document_json
            )
            SELECT repeat(md5('stove-work-' || g), 2), 1,
                   CASE WHEN g % 2 = 0 THEN 'eligible' ELSE 'complete' END,
                   {timestamp}, 2, '{{}}'
            FROM generate_series(1, {rows}) AS g
            """
            )
        )
        connection.execute(
            text(
                f"""
            INSERT INTO stove0_evaluation_records (
                evaluation_id, revision, phase, updated_at, document_bytes, document_json
            )
            SELECT repeat(md5('stove-evaluation-' || g), 2), 1,
                   CASE WHEN g % 2 = 0 THEN 'planning' ELSE 'complete' END,
                   {timestamp}, 2, '{{}}'
            FROM generate_series(1, {rows}) AS g
            """
            )
        )
        connection.exec_driver_sql("ANALYZE")


def _plan_cases() -> tuple[_PlanCase, ...]:
    cases: list[_PlanCase] = []
    collection_indexes = {
        "id": "collections_pkey",
        "created_at": "ix_collections_created_at_id",
        "files": "ix_collections_file_count_id",
        "bytes": "ix_collections_file_bytes_id",
    }
    for sort in sorted(closed_literal_values(CollectionSort)):
        for order in ("asc", "desc"):
            statement = _collection_list_statement(
                q=None,
                tag=None,
                encryption_format=None,
                passphrase_id=None,
                sort=sort,
                order=order,
                principal=None,
            )[4]
            cases.append(
                _PlanCase(
                    f"collections.sort.{sort}.{order}",
                    statement,
                    frozenset({collection_indexes[sort]}),
                )
            )
    for name, kwargs, index in (
        ("q", {"q": "004096"}, "ix_collections_search_trgm"),
        ("tag", {"tag": "tag-004096"}, "ix_collection_tags_tag"),
        (
            "encryption_format",
            {"encryption_format": "age-v1-scrypt"},
            "ix_collections_encryption_format",
        ),
        (
            "passphrase_id",
            {"passphrase_id": "qualification-key-v1"},
            "ix_collections_passphrase_id",
        ),
    ):
        statement = _collection_list_statement(
            q=str(kwargs["q"]) if "q" in kwargs else None,
            tag=str(kwargs["tag"]) if "tag" in kwargs else None,
            encryption_format=(
                str(kwargs["encryption_format"]) if "encryption_format" in kwargs else None
            ),
            passphrase_id=(str(kwargs["passphrase_id"]) if "passphrase_id" in kwargs else None),
            sort="id",
            order="asc",
            principal=None,
        )[4]
        cases.append(_PlanCase(f"collections.filter.{name}", statement, frozenset({index})))

    search_indexes = {
        "file_ref": "collection_files_pkey",
        "collection_id": "collection_files_pkey",
        "path": "ix_collection_files_path",
        "bytes": "ix_collection_files_bytes",
    }
    for sort in sorted(closed_literal_values(SearchSort)):
        for order in ("asc", "desc"):
            statement = _search_statement(
                q=None,
                collection=None,
                sort=sort,
                order=order,
                principal=None,
            )[3]
            cases.append(
                _PlanCase(
                    f"search.sort.{sort}.{order}",
                    statement,
                    frozenset({search_indexes[sort]}),
                )
            )
    cases.extend(
        (
            _PlanCase(
                "search.filter.q",
                _search_statement(
                    q="file-004096",
                    collection=None,
                    sort="file_ref",
                    order="asc",
                    principal=None,
                )[3],
                frozenset({"ix_collection_files_search_trgm"}),
            ),
            _PlanCase(
                "search.filter.collection",
                _search_statement(
                    q=None,
                    collection=1,
                    sort="file_ref",
                    order="asc",
                    principal=None,
                )[3],
                frozenset({"collection_files_pkey"}),
            ),
        )
    )

    tag_indexes = {
        "id": "tags_pkey",
        "created_at": "ix_tags_created_at_id",
        "collections": "ix_tags_collection_count_id",
    }
    for sort in sorted(closed_literal_values(TagSort)):
        for order in ("asc", "desc"):
            statement = _tag_list_statements(
                q=None,
                sort=sort,
                order=order,
                principal=_READER,
            )[2]
            cases.append(
                _PlanCase(
                    f"tags.sort.{sort}.{order}",
                    statement,
                    frozenset({tag_indexes[sort]}),
                )
            )
    cases.append(
        _PlanCase(
            "tags.filter.q",
            _tag_list_statements(q="004096", sort="id", order="asc", principal=_READER)[2],
            frozenset({"ix_tags_id_trgm"}),
        )
    )

    upload_indexes = {
        "id": "collection_uploads_pkey",
        "created_at": "ix_collection_uploads_opened_at",
        "state": "ix_collection_uploads_state",
        "files": "ix_collection_uploads_file_count",
        "bytes": "ix_collection_uploads_file_bytes",
    }
    for sort in sorted(closed_literal_values(CollectionUploadSort)):
        for order in ("asc", "desc"):
            statement = _upload_list_statement(
                q=None,
                tag=None,
                state=None,
                sort=sort,
                order=order,
                principal=_READER,
            )
            cases.append(
                _PlanCase(
                    f"uploads.sort.{sort}.{order}",
                    statement,
                    frozenset({upload_indexes[sort]}),
                )
            )
    for name, kwargs, index in (
        ("q", {"q": "source-004096"}, "ix_collection_uploads_search_trgm"),
        ("tag", {"tag": "tag-004096"}, "ix_collection_upload_tags_tag"),
        ("state", {"state": "open"}, "ix_collection_uploads_state"),
    ):
        statement = _upload_list_statement(
            q=str(kwargs["q"]) if "q" in kwargs else None,
            tag=str(kwargs["tag"]) if "tag" in kwargs else None,
            state=str(kwargs["state"]) if "state" in kwargs else None,
            sort="id",
            order="asc",
            principal=_READER,
        )
        cases.append(_PlanCase(f"uploads.filter.{name}", statement, frozenset({index})))

    provenance_indexes = {
        "path": "collection_files_pkey",
        "bytes": "ix_collection_files_collection_bytes",
        "status": "ix_collection_files_collection_provenance",
    }
    for sort in sorted(closed_literal_values(ProvenanceSort)):
        for order in ("asc", "desc"):
            statement = _provenance_file_statement(
                collection_id=1,
                principal=_READER,
                q=None,
                status=None,
                sort=sort,
                order=order,
            )
            cases.append(
                _PlanCase(
                    f"provenance.sort.{sort}.{order}",
                    statement,
                    frozenset({provenance_indexes[sort]}),
                )
            )
    cases.extend(
        (
            _PlanCase(
                "provenance.filter.q",
                _provenance_file_statement(
                    collection_id=1,
                    principal=_READER,
                    q="member-f7efa4f864ae9b88d43527f4b14f750f",
                    status=None,
                    sort="path",
                    order="asc",
                ),
                frozenset({"ix_collection_files_path_search_trgm"}),
            ),
            _PlanCase(
                "provenance.filter.status",
                _provenance_file_statement(
                    collection_id=1,
                    principal=_READER,
                    q=None,
                    status="omitted",
                    sort="path",
                    order="asc",
                ),
                frozenset({"ix_collection_files_collection_provenance"}),
            ),
        )
    )

    archive_copy_indexes = {
        "collection_id": "archive_copy_jobs_pkey",
        "source_store": "ix_archive_copy_jobs_source",
        "destination_store": "ix_archive_copy_jobs_destination",
        "state": "ix_archive_copy_jobs_state",
        "requested_at": "ix_archive_copy_jobs_requested",
    }
    for sort in sorted(closed_literal_values(ArchiveCopySort)):
        for order in ("asc", "desc"):
            statement = _archive_copy_list_statement(
                q=None,
                state=None,
                sort=sort,
                order=order,
                principal=None,
            )[3]
            cases.append(
                _PlanCase(
                    f"archive-copies.sort.{sort}.{order}",
                    statement,
                    frozenset({archive_copy_indexes[sort]}),
                )
            )
    cases.extend(
        (
            _PlanCase(
                "archive-copies.filter.q",
                _archive_copy_list_statement(
                    q="0000000000001000",
                    state=None,
                    sort="collection_id",
                    order="asc",
                    principal=None,
                )[3],
                frozenset({"ix_archive_copy_jobs_search_trgm"}),
            ),
            _PlanCase(
                "archive-copies.filter.state",
                _archive_copy_list_statement(
                    q=None,
                    state="requested",
                    sort="collection_id",
                    order="asc",
                    principal=None,
                )[3],
                frozenset({"ix_archive_copy_jobs_state"}),
            ),
        )
    )

    cache_indexes = {
        "collection_id": "ix_retrieval_cache_objects_collection",
        "source_store": "retrieval_cache_objects_pkey",
        "object_id": "ix_retrieval_cache_objects_object",
        "stored_bytes": "ix_retrieval_cache_objects_bytes",
        "cached_at": "ix_retrieval_cache_objects_cached",
        "verified_at": "ix_retrieval_cache_objects_verified",
    }
    for sort in sorted(closed_literal_values(RetrievalCacheSort)):
        for order in ("asc", "desc"):
            statement = _cache_list_statement(
                q=None,
                tag=None,
                collection_id=None,
                source_store=None,
                state=None,
                protection=None,
                expires_before=None,
                expires_after=None,
                sort=sort,
                order=order,
                principal=None,
                now=_NOW,
            )[0]
            if sort == "protected_until":
                cases.append(
                    _PlanCase(
                        f"retrieval-cache.sort.{sort}.{order}",
                        statement,
                        expected_nodes=frozenset({"Aggregate"}),
                    )
                )
            else:
                cases.append(
                    _PlanCase(
                        f"retrieval-cache.sort.{sort}.{order}",
                        statement,
                        frozenset({cache_indexes[sort]}),
                    )
                )
    cache_filters = (
        (
            "q",
            {"q": "object-004096"},
            "ix_retrieval_cache_objects_search_trgm",
            None,
        ),
        (
            "collection_id",
            {"collection_id": 4096},
            "ix_retrieval_cache_objects_collection",
            None,
        ),
        (
            "source_store",
            {"source_store": "archive-00"},
            "retrieval_cache_objects_pkey",
            None,
        ),
        ("state", {"state": "delete_pending"}, "ix_retrieval_cache_objects_cleanup", None),
        ("tag", {"tag": "tag-004096"}, "ix_collection_tags_tag", None),
        ("protection", {"protection": "protected"}, None, "Aggregate"),
        (
            "expires_before",
            {"expires_before": "2028-08-28T00:00:00.000000Z"},
            None,
            "Aggregate",
        ),
        (
            "expires_after",
            {"expires_after": "2026-08-28T00:00:00.000000Z"},
            None,
            "Aggregate",
        ),
    )
    for name, kwargs, index, node in cache_filters:
        statement = _cache_list_statement(
            q=str(kwargs["q"]) if "q" in kwargs else None,
            tag=str(kwargs["tag"]) if "tag" in kwargs else None,
            collection_id=(int(kwargs["collection_id"]) if "collection_id" in kwargs else None),
            source_store=(str(kwargs["source_store"]) if "source_store" in kwargs else None),
            state=str(kwargs["state"]) if "state" in kwargs else None,
            protection=str(kwargs["protection"]) if "protection" in kwargs else None,
            expires_before=(str(kwargs["expires_before"]) if "expires_before" in kwargs else None),
            expires_after=str(kwargs["expires_after"]) if "expires_after" in kwargs else None,
            sort="collection_id",
            order="asc",
            principal=None,
            now=_NOW,
        )[0]
        cases.append(
            _PlanCase(
                f"retrieval-cache.filter.{name}",
                statement,
                frozenset({index}) if index is not None else frozenset(),
                frozenset({node}) if node is not None else frozenset(),
            )
        )

    claim_indexes = {
        "created_at": "ix_collection_processing_claims_owner_created",
        "updated_at": "ix_collection_processing_claims_owner_updated",
        "expires_at": "ix_collection_processing_claims_owner_expires",
        "state": "ix_collection_processing_claims_owner_state_id",
        "work_id": "ix_collection_processing_claims_owner_work_id",
        "execution_id": "ix_collection_processing_claims_owner_execution",
    }
    for sort in sorted(closed_literal_values(ProcessingClaimSort)):
        for order in ("asc", "desc"):
            statement = _claim_list_statement(
                state=None,
                sort=sort,
                order=order,
                principal=_READER,
            )[1]
            cases.append(
                _PlanCase(
                    f"processing-claims.sort.{sort}.{order}",
                    statement,
                    frozenset({claim_indexes[sort]}),
                )
            )
    cases.append(
        _PlanCase(
            "processing-claims.filter.state",
            _claim_list_statement(
                state="active",
                sort="updated_at",
                order="asc",
                principal=_READER,
            )[1],
            frozenset(
                {
                    "ix_collection_processing_claims_owner_state",
                    "ix_collection_processing_claims_expiry",
                }
            ),
        )
    )

    key_indexes = {
        "id": "ix_app_keys_app",
        "created_at": "ix_app_keys_app_created",
        "expires_at": "ix_app_keys_app_expires",
        "last_used_at": "ix_app_keys_app_last_used",
    }
    for sort in sorted(closed_literal_values(ApplicationKeySort)):
        for order in ("asc", "desc"):
            statement = _key_list_statement(
                app="app-000",
                q=None,
                sort=sort,
                order=order,
                active=None,
                now=_NOW,
            )[3]
            cases.append(
                _PlanCase(
                    f"application-keys.sort.{sort}.{order}",
                    statement,
                    frozenset(
                        {key_indexes[sort], "app_keys_pkey"}
                        if sort == "id"
                        else {key_indexes[sort]}
                    ),
                )
            )
    cases.extend(
        (
            _PlanCase(
                "application-keys.filter.q",
                _key_list_statement(
                    app="app-000",
                    q="f7efa4f864ae9b88",
                    sort="id",
                    order="asc",
                    active=None,
                    now=_NOW,
                )[3],
                frozenset({"ix_app_keys_id_trgm"}),
            ),
            _PlanCase(
                "application-keys.filter.active",
                _key_list_statement(
                    app="app-000",
                    q=None,
                    sort="id",
                    order="asc",
                    active=True,
                    now=_NOW,
                )[3],
                frozenset({"ix_app_keys_app_active", "ix_app_keys_active"}),
            ),
        )
    )

    access_indexes = {
        "app": "ix_app_keys_app",
        "key_id": "app_key_access_grants_pkey",
        "permission": "ix_app_key_access_grants_permission",
        "resource": "ix_app_key_access_grants_resource",
        "created_at": "ix_app_key_access_grants_created",
    }
    for sort in sorted(closed_literal_values(ApplicationAccessSort)):
        for order in ("asc", "desc"):
            statement = _access_list_statement(
                q=None,
                sort=sort,
                order=order,
                app=None,
                key_id=None,
                permission=None,
                resource=None,
                active=None,
                now=_NOW,
            )[6]
            cases.append(
                _PlanCase(
                    f"application-access.sort.{sort}.{order}",
                    statement,
                    frozenset({access_indexes[sort]}),
                )
            )
    for name, kwargs, indexes in (
        ("q", {"q": "provenance"}, frozenset({"ix_app_key_access_grants_search_trgm"})),
        (
            "app",
            {"app": "app-000"},
            frozenset({"ix_app_keys_app", "ix_app_keys_app_trgm"}),
        ),
        (
            "key_id",
            {"key_id": "f7efa4f864ae9b88"},
            frozenset({"app_key_access_grants_pkey"}),
        ),
        (
            "permission",
            {"permission": "provenance:read"},
            frozenset({"ix_app_key_access_grants_permission"}),
        ),
        (
            "resource",
            {"resource": "tag:tag-000001"},
            frozenset({"ix_app_key_access_grants_resource"}),
        ),
        ("active", {"active": True}, frozenset({"ix_app_keys_active"})),
    ):
        statement = _access_list_statement(
            q=str(kwargs["q"]) if "q" in kwargs else None,
            sort="key_id",
            order="asc",
            app=str(kwargs["app"]) if "app" in kwargs else None,
            key_id=str(kwargs["key_id"]) if "key_id" in kwargs else None,
            permission=str(kwargs["permission"]) if "permission" in kwargs else None,
            resource=str(kwargs["resource"]) if "resource" in kwargs else None,
            active=bool(kwargs["active"]) if "active" in kwargs else None,
            now=_NOW,
        )[6]
        cases.append(_PlanCase(f"application-access.filter.{name}", statement, indexes))

    for sort in sorted(closed_literal_values(ApplicationSort)):
        for order in ("asc", "desc"):
            statement = _app_list_statement(
                q=None,
                sort=sort,
                order=order,
                active=None,
                now=_NOW,
            )[2]
            cases.append(
                _PlanCase(
                    f"applications.sort.{sort}.{order}",
                    statement,
                    expected_nodes=frozenset({"Aggregate"}),
                )
            )
    cases.extend(
        (
            _PlanCase(
                "applications.filter.q",
                _app_list_statement(
                    q="app-016384",
                    sort="name",
                    order="asc",
                    active=None,
                    now=_NOW,
                )[2],
                frozenset({"ix_app_keys_app_trgm"}),
            ),
            _PlanCase(
                "applications.filter.active",
                _app_list_statement(
                    q=None,
                    sort="name",
                    order="asc",
                    active=True,
                    now=_NOW,
                )[2],
                expected_nodes=frozenset({"Aggregate"}),
            ),
        )
    )

    qualification_now = parse_utc_timestamp(_NOW)
    for sort in sorted(closed_literal_values(DownloadQuotaSort)):
        for order in ("asc", "desc"):
            statement = _key_quota_statements(
                now=qualification_now,
                q=None,
                sort=sort,
                order=order,
                app=None,
                active=None,
            )[5]
            cases.append(
                _PlanCase(
                    f"download-quotas.sort.{sort}.{order}",
                    statement,
                    expected_nodes=frozenset({"Aggregate"}),
                )
            )
    cases.extend(
        (
            _PlanCase(
                "download-quotas.filter.q",
                _key_quota_statements(
                    now=qualification_now,
                    q="f7efa4f864ae9b88",
                    sort="key_id",
                    order="asc",
                    app=None,
                    active=None,
                )[5],
                frozenset({"ix_app_keys_search_trgm"}),
            ),
            _PlanCase(
                "download-quotas.filter.app",
                _key_quota_statements(
                    now=qualification_now,
                    q=None,
                    sort="key_id",
                    order="asc",
                    app="app-000",
                    active=None,
                )[5],
                frozenset({"ix_app_keys_app", "ix_app_keys_app_trgm"}),
            ),
            _PlanCase(
                "download-quotas.filter.active",
                _key_quota_statements(
                    now=qualification_now,
                    q=None,
                    sort="key_id",
                    order="asc",
                    app=None,
                    active=True,
                )[5],
                expected_nodes=frozenset({"Aggregate"}),
            ),
        )
    )

    work_indexes = {
        "work_id": "stove0_work_records_pkey",
        "updated_at": "ix_stove0_work_records_updated_work_id",
        "phase": "ix_stove0_work_records_phase_work_id",
    }
    for sort in sorted(closed_literal_values(WorkSort)):
        for order in ("asc", "desc"):
            statement = _work_list_statement(
                phase=None,
                query=None,
                sort=sort,
                order=order,
            )[1]
            cases.append(
                _PlanCase(
                    f"stove0-work.sort.{sort}.{order}",
                    statement,
                    frozenset({work_indexes[sort]}),
                    database="stove0",
                )
            )
    cases.extend(
        (
            _PlanCase(
                "stove0-work.filter.q",
                _work_list_statement(
                    phase=None,
                    query="abc",
                    sort="work_id",
                    order="asc",
                )[1],
                frozenset({"ix_stove0_work_records_id_trgm"}),
                database="stove0",
            ),
            _PlanCase(
                "stove0-work.filter.phase",
                _work_list_statement(
                    phase="eligible",
                    query=None,
                    sort="work_id",
                    order="asc",
                )[1],
                frozenset({"ix_stove0_work_records_phase_work_id"}),
                database="stove0",
            ),
        )
    )

    evaluation_indexes = {
        "evaluation_id": "stove0_evaluation_records_pkey",
        "updated_at": "ix_stove0_evaluation_records_updated_id",
        "phase": "ix_stove0_evaluation_records_phase_id",
    }
    for sort in sorted(closed_literal_values(EvaluationSort)):
        for order in ("asc", "desc"):
            statement = _evaluation_list_statement(
                phase=None,
                query=None,
                sort=sort,
                order=order,
            )[1]
            cases.append(
                _PlanCase(
                    f"stove0-evaluations.sort.{sort}.{order}",
                    statement,
                    frozenset({evaluation_indexes[sort]}),
                    database="stove0",
                )
            )
    cases.extend(
        (
            _PlanCase(
                "stove0-evaluations.filter.q",
                _evaluation_list_statement(
                    phase=None,
                    query="abc",
                    sort="evaluation_id",
                    order="asc",
                )[1],
                frozenset({"ix_stove0_evaluation_records_id_trgm"}),
                database="stove0",
            ),
            _PlanCase(
                "stove0-evaluations.filter.phase",
                _evaluation_list_statement(
                    phase="planning",
                    query=None,
                    sort="evaluation_id",
                    order="asc",
                )[1],
                frozenset({"ix_stove0_evaluation_records_phase_id"}),
                database="stove0",
            ),
        )
    )
    return tuple(cases)


def _index_names(plan: object) -> set[str]:
    names: set[str] = set()
    if isinstance(plan, dict):
        if "Index Name" in plan:
            names.add(str(plan["Index Name"]))
        for value in plan.values():
            names.update(_index_names(value))
    elif isinstance(plan, list):
        for value in plan:
            names.update(_index_names(value))
    return names


def _node_types(plan: object) -> set[str]:
    nodes: set[str] = set()
    if isinstance(plan, dict):
        if "Node Type" in plan:
            nodes.add(str(plan["Node Type"]))
        for value in plan.values():
            nodes.update(_node_types(value))
    elif isinstance(plan, list):
        for value in plan:
            nodes.update(_node_types(value))
    return nodes


def _openapi_operations(schema: dict[str, object]) -> dict[str, dict[str, object]]:
    paths = schema["paths"]
    assert isinstance(paths, dict)
    return {
        str(operation["operationId"]): operation
        for path_item in paths.values()
        if isinstance(path_item, dict)
        for method, operation in path_item.items()
        if method in {"delete", "get", "patch", "post", "put"}
        if isinstance(operation, dict)
    }


def _query_selectors(operation: dict[str, object]) -> set[str]:
    parameters = operation.get("parameters", [])
    assert isinstance(parameters, list)
    return {
        str(parameter["name"])
        for parameter in parameters
        if isinstance(parameter, dict) and parameter.get("in") == "query"
    }


def _query_enum(
    schema: dict[str, object],
    operation: dict[str, object],
    name: str,
) -> set[str]:
    parameters = operation.get("parameters", [])
    assert isinstance(parameters, list)
    parameter = next(
        item
        for item in parameters
        if isinstance(item, dict) and item.get("in") == "query" and item.get("name") == name
    )
    candidate = parameter["schema"]
    assert isinstance(candidate, dict)
    components = schema["components"]
    assert isinstance(components, dict)
    component_schemas = components["schemas"]
    assert isinstance(component_schemas, dict)

    def values(value: dict[str, object]) -> set[str]:
        reference = value.get("$ref")
        if reference is not None:
            resolved = component_schemas[str(reference).rsplit("/", 1)[-1]]
            assert isinstance(resolved, dict)
            return values(resolved)
        enum = value.get("enum")
        if isinstance(enum, list):
            return {str(item) for item in enum}
        variants = value.get("anyOf", [])
        assert isinstance(variants, list)
        return {
            item
            for variant in variants
            if isinstance(variant, dict) and variant.get("type") != "null"
            for item in values(variant)
        }

    return values(candidate)


def test_every_repo_query_selector_is_classified_and_every_database_selector_is_planned() -> None:
    schemas = {
        "riverhog": create_riverhog_app().openapi(),
        "stove0": create_stove0_contract_app().openapi(),
        "riverhog-ftp-adapter": create_adapter_contract_app().openapi(),
    }
    operations = {
        (application, operation_id): operation
        for application, schema in schemas.items()
        for operation_id, operation in _openapi_operations(schema).items()
    }
    observed = {
        key: _query_selectors(operation)
        for key, operation in operations.items()
        if _query_selectors(operation)
    }

    assert set(observed) == set(_DATABASE_PLAN_OPERATIONS) | set(_NON_PLAN_QUERY_OPERATIONS)
    for key, expected in _NON_PLAN_QUERY_OPERATIONS.items():
        assert observed[key] == expected

    cases = _plan_cases()
    case_ids = {case.id for case in cases}
    assert len(case_ids) == len(cases)
    expected_case_ids: set[str] = set()
    representatives: dict[str, tuple[str, str]] = {}
    filter_names = {"key": "key_id", "collection": "collection"}
    for key, prefix in _DATABASE_PLAN_OPERATIONS.items():
        selectors = observed[key]
        expected_selectors = _DATABASE_FILTER_SELECTORS[prefix] | {"order", "sort"}
        if "page" in selectors or "per_page" in selectors:
            expected_selectors |= {"page", "per_page"}
        assert selectors == expected_selectors, key
        representatives.setdefault(prefix, key)

    for prefix, key in representatives.items():
        for selector in _DATABASE_FILTER_SELECTORS[prefix]:
            expected_case_ids.add(f"{prefix}.filter.{filter_names.get(selector, selector)}")
        application, operation_id = key
        schema = schemas[application]
        operation = operations[(application, operation_id)]
        for sort in _query_enum(schema, operation, "sort"):
            for order in _query_enum(schema, operation, "order"):
                expected_case_ids.add(f"{prefix}.sort.{sort}.{order}")

    assert case_ids == expected_case_ids


@pytest.mark.parametrize("case", _plan_cases(), ids=lambda case: case.id)
def test_every_public_database_selector_has_its_declared_postgres_operator(
    qualified_engines: _QualifiedEngines,
    case: _PlanCase,
) -> None:
    qualified_engine = getattr(qualified_engines, case.database)
    statement = case.statement  # type: ignore[assignment]
    if ".filter." in case.id:
        # Filter and order vocabularies are independent public selectors. Prove
        # the filter's physical operator without letting the representative
        # page order win a small-fixture cost tie.
        statement = statement.order_by(None)
    statement = statement.limit(100)
    compiled = statement.compile(
        dialect=qualified_engine.dialect,
        compile_kwargs={"literal_binds": True},
    )
    with qualified_engine.begin() as connection:
        # This exhaustive matrix proves that every public selector has a usable
        # physical operator. Natural planner choices and scale are qualified
        # separately at multiple cardinalities; low-cardinality fixtures must
        # not turn this structural proof into a cost-estimator coincidence.
        connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
        if ".sort." in case.id:
            connection.exec_driver_sql("SET LOCAL enable_bitmapscan = off")
        elif ".filter." in case.id:
            connection.exec_driver_sql("SET LOCAL enable_indexscan = off")
        payload = connection.exec_driver_sql(
            f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {compiled}"
        ).scalar_one()
    indexes = _index_names(payload)
    nodes = _node_types(payload)

    if not (indexes & case.expected_indexes or nodes & case.expected_nodes):
        pytest.fail(
            f"{case.id}: expected one of indexes {sorted(case.expected_indexes)} or nodes "
            f"{sorted(case.expected_nodes)}; used indexes {sorted(indexes)} and nodes "
            f"{sorted(nodes)}"
        )
