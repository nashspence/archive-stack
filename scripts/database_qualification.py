#!/usr/bin/env python3
"""Record exact-SHA PostgreSQL selector and complete-stream qualification evidence."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import time
import tracemalloc
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from riverhog_core.catalog_db import create_catalog_engine, initialize_db
from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url
from stove0_core.persistence import stove0_state_schema

from tests.integration.test_public_selector_plans_postgres import (
    _DATABASE_PLAN_OPERATIONS,
    _index_names,
    _node_types,
    _plan_cases,
    _PlanCase,
    _seed_selector_relations,
    _seed_stove0_selector_relations,
)

SCHEMA = "riverhog-database-qualification/v1"
CARDINALITIES = (4096, 16384)
STREAM_CHUNK_ROWS = 100
MAX_STREAM_PEAK_BYTES = 32 * 1024 * 1024
FIXTURE_ROOT = Path("tests/fixtures/state/v1_0001")


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


def _measure_plan(engine: Engine, case: _PlanCase) -> dict[str, object]:
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
    return {
        "case": case.id,
        "database": case.database,
        "actual_rows": actual_rows,
        "planning_ms": round(float(root.get("Planning Time", 0.0) or 0.0), 3),
        "execution_ms": round(float(root.get("Execution Time", 0.0) or 0.0), 3),
        "indexes": sorted(_index_names(payload)),
        "nodes": sorted(_node_types(payload)),
        "work": _plan_work(payload),
    }


def _stream_case_by_family(cases: Sequence[_PlanCase]) -> dict[str, _PlanCase]:
    result: dict[str, _PlanCase] = {}
    for family in sorted(set(_DATABASE_PLAN_OPERATIONS.values())):
        candidates = sorted(
            (case for case in cases if case.id.startswith(f"{family}.sort.")),
            key=lambda case: case.id,
        )
        if not candidates:
            raise QualificationError(f"database selector family has no stream witness: {family}")
        result[family] = candidates[0]
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
        "peak_application_bytes": peak,
        "chunk_rows": STREAM_CHUNK_ROWS,
        "cancellation": {
            "consumed_rows": consumed,
            "consumer_delay_ms": 10,
            "cursor_closed": interrupted.closed,
            "connection_reusable": reusable == 1,
        },
    }


def _schema_url(database_url: str, schema: str) -> str:
    return (
        make_url(database_url)
        .update_query_dict({"options": f"-csearch_path={schema},public"})
        .render_as_string(hide_password=False)
    )


def _measure_cardinality(
    database_url: str,
    *,
    rows: int,
    cases: Sequence[_PlanCase],
) -> dict[str, object]:
    suffix = uuid4().hex
    riverhog_schema = f"riverhog_database_qualification_{rows}_{suffix}"
    stove0_schema = f"stove0_database_qualification_{rows}_{suffix}"
    admin = create_catalog_engine(database_url)
    with admin.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public"))
        connection.execute(text(f'CREATE SCHEMA "{riverhog_schema}"'))
        connection.execute(text(f'CREATE SCHEMA "{stove0_schema}"'))
    riverhog_url = _schema_url(database_url, riverhog_schema)
    stove0_url = _schema_url(database_url, stove0_schema)
    riverhog_engine: Engine | None = None
    stove0_engine: Engine | None = None
    try:
        initialize_db(riverhog_url)
        stove0_state_schema(stove0_url).upgrade()
        riverhog_engine = create_catalog_engine(riverhog_url)
        stove0_engine = create_catalog_engine(stove0_url)
        _seed_selector_relations(riverhog_engine, rows=rows)
        _seed_stove0_selector_relations(stove0_engine, rows=rows)
        with riverhog_engine.begin() as connection:
            connection.execute(text("ANALYZE"))
        with stove0_engine.begin() as connection:
            connection.execute(text("ANALYZE"))
        engines = {"riverhog": riverhog_engine, "stove0": stove0_engine}
        plans = [_measure_plan(engines[case.database], case) for case in cases]
        streams = [
            _measure_stream(engines[case.database], family, case)
            for family, case in _stream_case_by_family(cases).items()
        ]
        return {
            "rows": rows,
            "schema": {
                "riverhog": initialize_db(riverhog_url).as_dict(),
                "stove0": stove0_state_schema(stove0_url).validate().as_dict(),
            },
            "plans": plans,
            "streams": streams,
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


def _compare_cardinalities(measurements: Sequence[dict[str, object]]) -> None:
    if len(measurements) != 2:
        raise QualificationError("database qualification requires exactly two cardinalities")
    low, high = measurements
    low_plans = _by_identity(cast(list[dict[str, object]], low["plans"]), "case")
    high_plans = _by_identity(cast(list[dict[str, object]], high["plans"]), "case")
    if set(low_plans) != set(high_plans):
        raise QualificationError("plan identity changed between cardinalities")
    for case_id, low_plan in low_plans.items():
        high_plan = high_plans[case_id]
        low_work = float(cast(dict[str, object], low_plan["work"])["node_rows"])
        high_work = float(cast(dict[str, object], high_plan["work"])["node_rows"])
        if high_work + 1 > (low_work + 1) * 12 + 10_000:
            raise QualificationError(f"{case_id} database work regressed superlinearly")

    low_streams = _by_identity(cast(list[dict[str, object]], low["streams"]), "family")
    high_streams = _by_identity(cast(list[dict[str, object]], high["streams"]), "family")
    if set(low_streams) != set(high_streams):
        raise QualificationError("stream identity changed between cardinalities")
    for family, low_stream in low_streams.items():
        high_stream = high_streams[family]
        if int(high_stream["rows"]) < int(low_stream["rows"]):
            raise QualificationError(f"{family} lost rows at the larger cardinality")
        low_peak = int(low_stream["peak_application_bytes"])
        high_peak = int(high_stream["peak_application_bytes"])
        if high_peak > low_peak * 4 + 8 * 1024 * 1024:
            raise QualificationError(f"{family} application memory grew with result cardinality")


def _fixture_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_evidence(database_url: str, *, source_sha: str) -> dict[str, object]:
    cases = _plan_cases()
    if len({case.id for case in cases}) != len(cases):
        raise QualificationError("database selector plan identities are not unique")
    measurements = [
        _measure_cardinality(database_url, rows=rows, cases=cases) for rows in CARDINALITIES
    ]
    _compare_cardinalities(measurements)
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
            "consumer_delay_ms": 10,
        },
        "measurements": measurements,
        "qualification": {
            "exact_schemas": "passed",
            "natural_plans": "passed",
            "bounded_pages": "passed",
            "complete_streams": "passed",
            "cancellation": "passed",
            "backpressure": "passed",
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
