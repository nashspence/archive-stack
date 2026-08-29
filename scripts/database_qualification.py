#!/usr/bin/env python3
"""Record exact-SHA PostgreSQL selector and complete-stream qualification evidence."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import socket
import threading
import time
import tracemalloc
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import httpx
import uvicorn
from riverhog_api.app import create_app
from riverhog_api_client import ApiClient
from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import create_catalog_engine, initialize_db
from riverhog_core.catalog_models import CollectionFileRecord
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_core.services.tags import SqlAlchemyTagService
from riverhog_protocol import PortableCollectionIdentityBuilder
from riverhog_protocol.paths import relpath_search_key, relpath_sort_key, text_search_key
from sqlalchemy import select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.sql.compiler import IdentifierPreparer
from stove0_core.persistence import stove0_state_schema

from tests.integration.test_public_selector_plans_postgres import (
    _DATABASE_PLAN_OPERATIONS,
    _READER,
    _index_names,
    _node_types,
    _plan_cases,
    _PlanCase,
    _seed_selector_relations,
    _seed_stove0_selector_relations,
)
from tests.unit.archive_object_fixtures import MemoryArchiveStore, archive_store_binding

SCHEMA = "riverhog-database-qualification/v1"
CARDINALITIES = (4096, 65536)
STREAM_CHUNK_ROWS = 100
MAX_STREAM_PEAK_BYTES = 32 * 1024 * 1024
MAX_HTTP_PEAK_BYTES = 64 * 1024 * 1024
FIXTURE_ROOT = Path("tests/fixtures/state/v1_0001")


def _qualification_archive_stores() -> ArchiveStoreRegistry:
    """Return the inert store required by the production service composition."""

    return ArchiveStoreRegistry({"qualification": archive_store_binding(MemoryArchiveStore())})


class QualificationError(RuntimeError):
    """Raised when retained database evidence cannot satisfy its contract."""


def _source_sha(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 40 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise QualificationError("source SHA must be one exact 40-character Git commit")
    return normalized


def _json_plan(value: object) -> list[dict[str, Any]]:
    payload = json.loads(value) if isinstance(value, str) else value
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise QualificationError("PostgreSQL returned an invalid JSON plan")
    return cast(list[dict[str, Any]], payload)


def _plan_work(value: object) -> dict[str, float | int]:
    totals: dict[str, float | int] = {
        "node_rows": 0.0,
        "rows_removed": 0.0,
        "shared_hit_blocks": 0,
        "shared_read_blocks": 0,
        "temp_read_blocks": 0,
        "temp_written_blocks": 0,
    }

    def visit(node: object) -> None:
        if isinstance(node, dict):
            if "Node Type" in node:
                loops = float(node.get("Actual Loops", 1) or 0)
                totals["node_rows"] = float(totals["node_rows"]) + (
                    float(node.get("Actual Rows", 0) or 0) * loops
                )
                totals["rows_removed"] = float(totals["rows_removed"]) + (
                    float(node.get("Rows Removed by Filter", 0) or 0) * loops
                    + float(node.get("Rows Removed by Join Filter", 0) or 0) * loops
                )
                for source, target in (
                    ("Shared Hit Blocks", "shared_hit_blocks"),
                    ("Shared Read Blocks", "shared_read_blocks"),
                    ("Temp Read Blocks", "temp_read_blocks"),
                    ("Temp Written Blocks", "temp_written_blocks"),
                ):
                    totals[target] = int(totals[target]) + int(node.get(source, 0) or 0)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    totals["node_rows"] = round(float(totals["node_rows"]), 3)
    totals["rows_removed"] = round(float(totals["rows_removed"]), 3)
    return totals


def _plan_node_details(value: object) -> list[dict[str, str]]:
    """Project the relation/operator facts needed for reviewed natural-plan rules."""

    details: list[dict[str, str]] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            node_type = node.get("Node Type")
            if isinstance(node_type, str):
                detail = {"node": node_type}
                for source, target in (
                    ("Relation Name", "relation"),
                    ("Index Name", "index"),
                    ("Sort Method", "sort_method"),
                ):
                    value = node.get(source)
                    if isinstance(value, str):
                        detail[target] = value
                details.append(detail)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return details


def _measure_plan(engine: Engine, case: _PlanCase, *, rows: int) -> dict[str, object]:
    statement = cast(Any, case.statement).limit(STREAM_CHUNK_ROWS)
    compiled = statement.compile(
        dialect=engine.dialect,
        compile_kwargs={"literal_binds": True},
    )
    with engine.begin() as connection:
        payload = _json_plan(
            connection.exec_driver_sql(
                f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {compiled}"
            ).scalar_one()
        )
    root = payload[0]
    plan = root.get("Plan")
    if not isinstance(plan, dict):
        raise QualificationError(f"{case.id} has no PostgreSQL plan root")
    actual_rows = int(plan.get("Actual Rows", 0) or 0)
    if actual_rows > STREAM_CHUNK_ROWS:
        raise QualificationError(f"{case.id} escaped its bounded page limit")
    indexes = _index_names(payload)
    nodes = _node_types(payload)
    accepted_indexes = indexes & case.expected_indexes
    accepted_nodes = nodes & case.expected_nodes
    reviewed_alternatives: list[str] = []
    low_cardinality_scan = (
        case.allow_low_cardinality_seq_scan and rows == min(CARDINALITIES) and "Seq Scan" in nodes
    )
    if low_cardinality_scan:
        reviewed_alternatives.append("low-cardinality-sequential-scan")
    if not (accepted_indexes or accepted_nodes):
        if not low_cardinality_scan:
            raise QualificationError(
                f"{case.id} natural plan used indexes {sorted(indexes)} and nodes "
                f"{sorted(nodes)}, expected one of indexes {sorted(case.expected_indexes)} "
                f"or nodes {sorted(case.expected_nodes)}"
            )
    node_details = _plan_node_details(payload)
    indexed_relations = {
        detail["relation"]
        for detail in node_details
        if detail.get("index") in accepted_indexes and "relation" in detail
    }
    scanned_indexed_relations = sorted(
        {
            detail["relation"]
            for detail in node_details
            if detail.get("node") == "Seq Scan" and detail.get("relation") in indexed_relations
        }
    )
    if scanned_indexed_relations and not low_cardinality_scan:
        raise QualificationError(
            f"{case.id} naturally scanned its indexed relation(s): {scanned_indexed_relations}"
        )
    if ".sort." in case.id and "Sort" in nodes and not case.allow_explicit_sort:
        raise QualificationError(f"{case.id} did not preserve its declared index ordering")
    if case.allow_explicit_sort and "Sort" in nodes:
        reviewed_alternatives.append("derived-order-sort")
    work = _plan_work(payload)
    if int(work["temp_read_blocks"]) or int(work["temp_written_blocks"]):
        raise QualificationError(f"{case.id} natural plan spilled to temporary storage")
    return {
        "case": case.id,
        "database": case.database,
        "actual_rows": actual_rows,
        "planning_ms": round(float(root.get("Planning Time", 0.0) or 0.0), 3),
        "execution_ms": round(float(root.get("Execution Time", 0.0) or 0.0), 3),
        "indexes": sorted(indexes),
        "nodes": sorted(nodes),
        "accepted_indexes": sorted(accepted_indexes),
        "accepted_nodes": sorted(accepted_nodes),
        "reviewed_alternatives": reviewed_alternatives,
        "node_details": node_details,
        "work_per_returned_row": round(
            (float(work["node_rows"]) + float(work["rows_removed"])) / max(actual_rows, 1),
            6,
        ),
        "work": work,
    }


def _stream_cases_by_family(cases: Sequence[_PlanCase]) -> dict[str, tuple[_PlanCase, ...]]:
    result: dict[str, tuple[_PlanCase, ...]] = {}
    for family in sorted(set(_DATABASE_PLAN_OPERATIONS.values())):
        candidates = sorted(
            (case for case in cases if case.id.startswith(f"{family}.")),
            key=lambda case: case.id,
        )
        if not candidates:
            raise QualificationError(f"database selector family has no stream witness: {family}")
        result[family] = tuple(candidates)
    return result


def _measure_stream(engine: Engine, family: str, case: _PlanCase) -> dict[str, object]:
    statement = cast(Any, case.statement).execution_options(
        stream_results=True,
        yield_per=STREAM_CHUNK_ROWS,
        max_row_buffer=STREAM_CHUNK_ROWS,
    )
    gc.collect()
    tracemalloc.start()
    result: Any | None = None
    started = time.perf_counter()
    first_ms = 0.0
    count = 0
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            result = connection.execute(statement).yield_per(STREAM_CHUNK_ROWS)
            iterator = iter(result)
            first = next(iterator, None)
            first_ms = (time.perf_counter() - started) * 1000
            if first is not None:
                count = 1
                for _row in iterator:
                    count += 1
            result.close()
            transaction.rollback()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        if result is not None:
            result.close()
        tracemalloc.stop()
    total_ms = (time.perf_counter() - started) * 1000
    if peak > MAX_STREAM_PEAK_BYTES:
        raise QualificationError(f"{family} stream exceeded its application-memory budget")

    with engine.connect() as connection:
        transaction = connection.begin()
        interrupted = connection.execute(statement).yield_per(STREAM_CHUNK_ROWS)
        consumed = 1 if next(iter(interrupted), None) is not None else 0
        time.sleep(0.01)
        interrupted.close()
        reusable = connection.execute(text("SELECT 1")).scalar_one()
        transaction.rollback()
    if reusable != 1 or not interrupted.closed:
        raise QualificationError(f"{family} stream did not release its canceled cursor")

    return {
        "family": family,
        "database": case.database,
        "statement_case": case.id,
        "rows": count,
        "first_item_ms": round(first_ms, 3),
        "total_ms": round(total_ms, 3),
        "milliseconds_per_row": round(total_ms / max(count, 1), 6),
        "peak_application_bytes": peak,
        "chunk_rows": STREAM_CHUNK_ROWS,
        "cancellation": {
            "consumed_rows": consumed,
            "consumer_delay_ms": 10,
            "cursor_closed": interrupted.closed,
            "connection_reusable": reusable == 1,
        },
    }


def _schema_url(database_url: str, schema: str, *, application_name: str) -> str:
    return (
        make_url(database_url)
        .update_query_dict(
            {
                "application_name": application_name,
                "options": f"-csearch_path={schema},public",
            }
        )
        .render_as_string(hide_password=False)
    )


def _seed_unicode_files(engine: Engine) -> tuple[str, ...]:
    paths = (
        "unicode/Éclair.bin",
        "unicode/éclair.bin",
        "unicode/ΩMEGA.bin",
        "unicode/Ωmega.bin",
    )
    with engine.begin() as connection:
        for ordinal, path in enumerate(paths):
            connection.execute(
                text(
                    "INSERT INTO collection_files "
                    "(collection_id, path, bytes, sha256, provenance_status, path_sort_key, "
                    "search_text, path_search_text) "
                    "VALUES (1, :path, :bytes, :sha256, 'omitted', :sort_key, "
                    ":search_text, :path_search_text)"
                ),
                {
                    "path": path,
                    "bytes": ordinal,
                    "sha256": hashlib.sha256(path.encode("utf-8")).hexdigest(),
                    "sort_key": relpath_sort_key(path),
                    "search_text": f"1/{relpath_search_key(path)}",
                    "path_search_text": relpath_search_key(path),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO collection_file_provenance "
                    "(collection_id, path, status, journal_id, current_state_id, "
                    "omission_reason) VALUES "
                    "(1, :path, 'omitted', NULL, NULL, 'qualification fixture')"
                ),
                {"path": path},
            )
        connection.execute(
            text(
                "UPDATE collections SET file_count = file_count + :files, "
                "file_bytes = file_bytes + :bytes WHERE id = 1"
            ),
            {"files": len(paths), "bytes": sum(range(len(paths)))},
        )
    return paths


def _seal_fixture_inventory(database_url: str) -> None:
    service = SqlAlchemyRetrievalService(
        RuntimeConfig(database_url=database_url),
        _qualification_archive_stores(),
        None,
    )
    header, files, _identity, file_count, file_bytes = service.collection_inventory(1)
    builder = PortableCollectionIdentityBuilder(header)
    for file in files:
        builder.add(file)
    if builder.files != file_count or builder.bytes != file_bytes:
        raise QualificationError("high-fanout fixture inventory projections differ")
    engine = create_catalog_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE collections SET inventory_identity = :identity WHERE id = 1"),
                {"identity": builder.identity},
            )
    finally:
        engine.dispose()


def _database_semantics(engine: Engine, *, unicode_paths: Sequence[str]) -> dict[str, object]:
    with engine.begin() as connection:
        extension = connection.execute(
            text(
                "SELECT n.nspname, e.extversion FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid = e.extnamespace "
                "WHERE e.extname = 'pg_trgm'"
            )
        ).one_or_none()
        if extension is None or extension[0] != "public":
            raise QualificationError("pg_trgm is not installed in public")
        operator_class = connection.execute(
            text(
                "SELECT 1 FROM pg_opclass o JOIN pg_namespace n ON n.oid = o.opcnamespace "
                "WHERE o.opcname = 'gin_trgm_ops' AND n.nspname = 'public'"
            )
        ).scalar_one_or_none()
        if operator_class != 1:
            raise QualificationError("pg_trgm does not provide public.gin_trgm_ops")
        schema = str(connection.scalar(text("SELECT current_schema()")))
        digest_columns = connection.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = :schema AND data_type = 'character varying' "
                "AND character_maximum_length = 64 "
                "ORDER BY table_name, ordinal_position"
            ),
            {"schema": schema},
        ).all()
        if not digest_columns:
            raise QualificationError("qualified schema exposes no durable SHA-256 columns")
        preparer = IdentifierPreparer(engine.dialect)
        invalid: list[str] = []
        for table_name, column_name in digest_columns:
            qualified = f"{preparer.quote_schema(schema)}.{preparer.quote(str(table_name))}"
            column = preparer.quote(str(column_name))
            count = int(
                connection.exec_driver_sql(
                    f"SELECT count(*) FROM {qualified} "
                    f"WHERE {column} IS NOT NULL "
                    f"AND {column} !~ '^[0-9a-f]{{64}}$'"
                ).scalar_one()
            )
            if count:
                invalid.append(f"{table_name}.{column_name}:{count}")
        if invalid:
            raise QualificationError("durable SHA-256 invariant scan failed: " + ", ".join(invalid))
        ordered = tuple(
            connection.scalars(
                text(
                    "SELECT path FROM collection_files "
                    "WHERE path LIKE 'unicode/%' ORDER BY path_sort_key"
                )
            )
        )
        expected_order = tuple(sorted(unicode_paths, key=relpath_sort_key))
        if ordered != expected_order:
            raise QualificationError("PostgreSQL path ordering differs from canonical UTF-8")
        search_results: dict[str, list[str]] = {}
        expected_searches = {
            "ΩMEGA": sorted(path for path in unicode_paths if "Ωmega" in relpath_search_key(path)),
            "Éclair": ["unicode/Éclair.bin"],
            "éclair": ["unicode/éclair.bin"],
        }
        for query, expected in expected_searches.items():
            rows = list(
                connection.scalars(
                    select(CollectionFileRecord.path)
                    .where(
                        CollectionFileRecord.path_search_text.like(
                            f"%{text_search_key(query)}%",
                            escape="\\",
                        )
                    )
                    .order_by(CollectionFileRecord.path_sort_key)
                )
            )
            if rows != sorted(expected, key=relpath_sort_key):
                raise QualificationError(f"PostgreSQL path search differs for {query!r}")
            search_results[query] = rows
        database_settings = connection.execute(
            text(
                "SELECT current_setting('server_encoding'), datlocprovider, "
                "datcollate, daticulocale FROM pg_database WHERE datname = current_database()"
            )
        ).one()
    return {
        "pg_trgm": {
            "schema": str(extension[0]),
            "version": str(extension[1]),
            "operator_classes": ["gin_trgm_ops"],
        },
        "digest_invariants": {
            "columns_checked": len(digest_columns),
            "invalid_values": 0,
        },
        "text": {
            "server_encoding": str(database_settings[0]),
            "ambient_collation_recorded_not_authoritative": {
                "provider": str(database_settings[1]),
                "collation": str(database_settings[2]),
                "icu_locale": (
                    str(database_settings[3]) if database_settings[3] is not None else None
                ),
            },
            "ordered_paths": list(ordered),
            "search_results": search_results,
        },
    }


class _QualificationAppKeys:
    def authenticate(self, token: str) -> ApplicationPrincipal | None:
        return _READER if token == "qualification-token" else None


def _start_http_server(application: object) -> tuple[uvicorn.Server, threading.Thread, str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            cast(Any, application),
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
        name="riverhog-database-qualification-http",
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        raise QualificationError("qualification HTTP server did not start")
    return server, thread, f"http://127.0.0.1:{port}"


def _stop_http_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=10)
    if thread.is_alive():
        raise QualificationError("qualification HTTP server did not stop")


def _open_transactions(engine: Engine, *, application_name: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE application_name = :application_name AND xact_start IS NOT NULL"
                ),
                {"application_name": application_name},
            )
            or 0
        )


def _measure_http_path(
    database_url: str,
    *,
    admin: Engine,
    application_name: str,
) -> dict[str, object]:
    config = RuntimeConfig(database_url=database_url)
    tags = SqlAlchemyTagService(config)
    retrieval = SqlAlchemyRetrievalService(config, _qualification_archive_stores(), None)
    container = SimpleNamespace(
        app_keys=_QualificationAppKeys(),
        tags=tags,
        retrieval=retrieval,
    )
    application = create_app(container=cast(Any, container))
    server, thread, base_url = _start_http_server(application)
    api = ApiClient(
        base_url=base_url,
        token="qualification-token",
        allow_insecure_http=True,
    )
    try:
        gc.collect()
        tracemalloc.start()
        started = time.perf_counter()
        with api.stream_tags(sort="id", order="asc") as items:
            tag_rows = sum(1 for _item in items)
        tags_ms = (time.perf_counter() - started) * 1000
        _current, tags_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        gc.collect()
        tracemalloc.start()
        started = time.perf_counter()
        with api.stream_portable_collection_inventory(1) as inventory:
            inventory_rows = sum(1 for _file in inventory)
        inventory_ms = (time.perf_counter() - started) * 1000
        _current, inventory_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        if max(tags_peak, inventory_peak) > MAX_HTTP_PEAK_BYTES:
            raise QualificationError("full HTTP path exceeded its application-memory budget")

        headers = {"Authorization": "Bearer qualification-token"}
        with httpx.Client(base_url=base_url, headers=headers, timeout=30) as raw_client:
            with raw_client.stream(
                "GET",
                "/v1/tags/stream",
                params={"sort": "id", "order": "asc"},
                headers={"Accept": "application/json-seq"},
            ) as response:
                response.raise_for_status()
                first_chunk = next(response.iter_bytes())
                if not first_chunk:
                    raise QualificationError("complete stream emitted an empty first chunk")
                time.sleep(0.1)
                transactions_while_consumer_paused = _open_transactions(
                    admin,
                    application_name=application_name,
                )
            time.sleep(0.05)
            transactions_after_disconnect = _open_transactions(
                admin,
                application_name=application_name,
            )
        if transactions_while_consumer_paused or transactions_after_disconnect:
            raise QualificationError("HTTP consumer lifetime retained a database transaction")
    finally:
        api.close()
        _stop_http_server(server, thread)

    restarted, restarted_thread, restarted_url = _start_http_server(application)
    try:
        with httpx.Client(
            base_url=restarted_url,
            headers={"Authorization": "Bearer qualification-token"},
            timeout=30,
        ) as client:
            restart_response = client.get(
                "/v1/tags",
                params={"page": 1, "per_page": 1, "sort": "id", "order": "asc"},
            )
            restart_response.raise_for_status()
    finally:
        _stop_http_server(restarted, restarted_thread)
    return {
        "official_client_complete_stream": {
            "rows": tag_rows,
            "total_ms": round(tags_ms, 3),
            "peak_application_bytes": tags_peak,
        },
        "official_client_inventory": {
            "rows": inventory_rows,
            "total_ms": round(inventory_ms, 3),
            "peak_application_bytes": inventory_peak,
        },
        "slow_consumer": {
            "pause_ms": 100,
            "open_transactions_while_paused": transactions_while_consumer_paused,
        },
        "disconnect": {
            "open_transactions_after_disconnect": transactions_after_disconnect,
        },
        "restart": {"bounded_read_succeeded": True},
    }


def _measure_cardinality(
    database_url: str,
    *,
    rows: int,
    cases: Sequence[_PlanCase],
) -> dict[str, object]:
    suffix = uuid4().hex
    riverhog_schema = f"riverhog_database_qualification_{rows}_{suffix}"
    stove0_schema = f"stove0_database_qualification_{rows}_{suffix}"
    riverhog_application_name = f"riverhog-db-qualification-{rows}-{suffix}"
    stove0_application_name = f"stove0-db-qualification-{rows}-{suffix}"
    admin = create_catalog_engine(database_url)
    with admin.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public"))
        connection.execute(text(f'CREATE SCHEMA "{riverhog_schema}"'))
        connection.execute(text(f'CREATE SCHEMA "{stove0_schema}"'))
    riverhog_url = _schema_url(
        database_url,
        riverhog_schema,
        application_name=riverhog_application_name,
    )
    stove0_url = _schema_url(
        database_url,
        stove0_schema,
        application_name=stove0_application_name,
    )
    riverhog_engine: Engine | None = None
    stove0_engine: Engine | None = None
    try:
        initialize_db(riverhog_url)
        stove0_state_schema(stove0_url).upgrade()
        riverhog_engine = create_catalog_engine(riverhog_url)
        stove0_engine = create_catalog_engine(stove0_url)
        _seed_selector_relations(riverhog_engine, rows=rows)
        _seed_stove0_selector_relations(stove0_engine, rows=rows)
        unicode_paths = _seed_unicode_files(riverhog_engine)
        _seal_fixture_inventory(riverhog_url)
        with riverhog_engine.begin() as connection:
            connection.execute(text("ANALYZE"))
        with stove0_engine.begin() as connection:
            connection.execute(text("ANALYZE"))
        engines = {"riverhog": riverhog_engine, "stove0": stove0_engine}
        plans: list[dict[str, object]] = []
        plan_failures: list[str] = []
        for case in cases:
            try:
                plans.append(_measure_plan(engines[case.database], case, rows=rows))
            except QualificationError as exc:
                plan_failures.append(str(exc))
        if plan_failures:
            raise QualificationError(
                f"natural-plan failures at {rows} rows:\n" + "\n".join(plan_failures)
            )
        streams = [
            _measure_stream(engines[case.database], family, case)
            for family, family_cases in _stream_cases_by_family(cases).items()
            for case in family_cases
        ]
        return {
            "rows": rows,
            "schema": {
                "riverhog": initialize_db(riverhog_url).as_dict(),
                "stove0": stove0_state_schema(stove0_url).validate().as_dict(),
            },
            "plans": plans,
            "streams": streams,
            "database_semantics": _database_semantics(
                riverhog_engine,
                unicode_paths=unicode_paths,
            ),
            "http": _measure_http_path(
                riverhog_url,
                admin=admin,
                application_name=riverhog_application_name,
            ),
        }
    finally:
        if riverhog_engine is not None:
            riverhog_engine.dispose()
        if stove0_engine is not None:
            stove0_engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{riverhog_schema}" CASCADE'))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{stove0_schema}" CASCADE'))
        admin.dispose()


def _by_identity(rows: Iterable[dict[str, object]], key: str) -> dict[str, dict[str, object]]:
    return {str(row[key]): row for row in rows}


def _compare_cardinalities(
    measurements: Sequence[dict[str, object]],
    *,
    cases: Sequence[_PlanCase],
) -> None:
    if len(measurements) != 2:
        raise QualificationError("database qualification requires exactly two cardinalities")
    low, high = measurements
    low_plans = _by_identity(cast(list[dict[str, object]], low["plans"]), "case")
    high_plans = _by_identity(cast(list[dict[str, object]], high["plans"]), "case")
    if set(low_plans) != set(high_plans):
        raise QualificationError("plan identity changed between cardinalities")
    relation_growth = int(high["rows"]) / max(int(low["rows"]), 1)
    case_contracts = {case.id: case for case in cases}
    for case_id, low_plan in low_plans.items():
        high_plan = high_plans[case_id]
        low_work = float(cast(dict[str, object], low_plan["work"])["node_rows"])
        high_work = float(cast(dict[str, object], high_plan["work"])["node_rows"])
        expected_indexes = case_contracts[case_id].expected_indexes
        growth_factor = 4 if expected_indexes else relation_growth * 1.5
        if high_work + 1 > (low_work + 1) * growth_factor + 1_000:
            raise QualificationError(
                f"{case_id} database work regressed superlinearly: "
                f"low={low_work}, high={high_work}, allowed_factor={growth_factor}, "
                f"low_nodes={low_plan['node_details']}, high_nodes={high_plan['node_details']}"
            )
        low_execution = float(low_plan["execution_ms"])
        high_execution = float(high_plan["execution_ms"])
        # The work gate above is the scaling authority.  Wall-clock latency also
        # includes cache locality and runner scheduling, so retain a wider linear
        # envelope while still rejecting a material cardinality-driven regression.
        latency_factor = max(8, relation_growth * 4)
        if high_execution > low_execution * latency_factor + 100:
            raise QualificationError(
                f"{case_id} database latency regressed with cardinality: "
                f"low_ms={low_execution}, high_ms={high_execution}, "
                f"allowed_factor={latency_factor}"
            )

    low_streams = _by_identity(cast(list[dict[str, object]], low["streams"]), "statement_case")
    high_streams = _by_identity(cast(list[dict[str, object]], high["streams"]), "statement_case")
    if set(low_streams) != set(high_streams):
        raise QualificationError("stream identity changed between cardinalities")
    for case_id, low_stream in low_streams.items():
        high_stream = high_streams[case_id]
        if int(high_stream["rows"]) < int(low_stream["rows"]):
            raise QualificationError(f"{case_id} lost rows at the larger cardinality")
        low_peak = int(low_stream["peak_application_bytes"])
        high_peak = int(high_stream["peak_application_bytes"])
        if high_peak > low_peak * 4 + 8 * 1024 * 1024:
            raise QualificationError(f"{case_id} application memory grew with result cardinality")
        low_ms_per_row = float(low_stream["milliseconds_per_row"])
        high_ms_per_row = float(high_stream["milliseconds_per_row"])
        if high_ms_per_row > low_ms_per_row * 4 + 0.5:
            raise QualificationError(f"{case_id} per-row stream latency regressed")

    low_http = cast(dict[str, dict[str, object]], low["http"])
    high_http = cast(dict[str, dict[str, object]], high["http"])
    for key in ("official_client_complete_stream", "official_client_inventory"):
        low_peak = int(low_http[key]["peak_application_bytes"])
        high_peak = int(high_http[key]["peak_application_bytes"])
        if high_peak > low_peak * 4 + 8 * 1024 * 1024:
            raise QualificationError(f"{key} memory regressed with cardinality")


def _fixture_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_evidence(database_url: str, *, source_sha: str) -> dict[str, object]:
    cases = _plan_cases()
    if len({case.id for case in cases}) != len(cases):
        raise QualificationError("database selector plan identities are not unique")
    measurements = [
        _measure_cardinality(database_url, rows=rows, cases=cases) for rows in CARDINALITIES
    ]
    _compare_cardinalities(measurements, cases=cases)
    applications = sorted({application for application, _operation in _DATABASE_PLAN_OPERATIONS})
    return {
        "schema": SCHEMA,
        "source_sha": source_sha,
        "generated_at": datetime.now(UTC).isoformat(),
        "cardinalities": list(CARDINALITIES),
        "selector_contract": {
            "applications": applications,
            "operations": len(_DATABASE_PLAN_OPERATIONS),
            "statement_families": len(set(_DATABASE_PLAN_OPERATIONS.values())),
            "plan_cases": len(cases),
        },
        "schema_fixtures": {
            "riverhog": _fixture_sha256(FIXTURE_ROOT / "riverhog.postgresql.sql"),
            "stove0": _fixture_sha256(FIXTURE_ROOT / "stove0.postgresql.sql"),
        },
        "stream_contract": {
            "chunk_rows": STREAM_CHUNK_ROWS,
            "max_peak_application_bytes": MAX_STREAM_PEAK_BYTES,
            "max_full_http_peak_bytes": MAX_HTTP_PEAK_BYTES,
            "consumer_delay_ms": 10,
        },
        "measurements": measurements,
        "qualification": {
            "exact_schemas": "passed",
            "natural_plans": "passed",
            "bounded_pages": "passed",
            "complete_streams": "passed",
            "full_http_official_client": "passed",
            "high_fanout_inventory": "passed",
            "cancellation": "passed",
            "backpressure": "passed",
            "restart": "passed",
            "digest_invariants": "passed",
            "unicode_semantics": "passed",
            "postgresql_capabilities": "passed",
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source_sha = _source_sha(args.source_sha)
    evidence = build_evidence(args.database_url, source_sha=source_sha)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
