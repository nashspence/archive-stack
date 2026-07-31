from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from munchy.job_templates import JobTemplateError, job_template_digest, normalize_job_template

JOB_TEMPLATE_COLUMNS = (
    "template_id",
    "definition",
    "resolved_job",
    "digest",
    "revision",
    "enabled",
    "created_at",
    "updated_at",
)
SNAPSHOT_SCHEMA_VERSION = 2


class TemplateRegistryError(ValueError):
    """Raised when a template-registry snapshot cannot be trusted or restored."""


def ensure_template_registry_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_templates (
            template_id TEXT PRIMARY KEY,
            definition TEXT NOT NULL,
            resolved_job TEXT NOT NULL,
            digest TEXT NOT NULL,
            revision INTEGER NOT NULL,
            enabled INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS job_templates_enabled_id ON job_templates(enabled, template_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS job_templates_updated_id "
        "ON job_templates(updated_at, template_id)"
    )


def _connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise TemplateRegistryError(f"SQLite database does not exist: {path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _quick_check(conn: sqlite3.Connection, *, label: str) -> None:
    rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
    if rows != ["ok"]:
        raise TemplateRegistryError(f"{label} failed SQLite quick_check: {rows}")


def _require_template_schema(conn: sqlite3.Connection, *, label: str) -> None:
    columns = tuple(str(row[1]) for row in conn.execute("PRAGMA table_info(job_templates)"))
    if columns != JOB_TEMPLATE_COLUMNS:
        raise TemplateRegistryError(
            f"{label} has incompatible job_templates columns: {', '.join(columns) or 'none'}"
        )


def _validate_template_rows(conn: sqlite3.Connection, *, label: str) -> int:
    count = 0
    query = "SELECT " + ", ".join(JOB_TEMPLATE_COLUMNS) + " FROM job_templates ORDER BY template_id"
    for row in conn.execute(query):
        template_id = str(row[0]).strip()
        if not template_id:
            raise TemplateRegistryError(f"{label} contains a blank template ID")
        try:
            definition = json.loads(str(row[1]))
            resolved_job = json.loads(str(row[2]))
        except json.JSONDecodeError as exc:
            raise TemplateRegistryError(
                f"{label} template {template_id} contains invalid JSON"
            ) from exc
        if not isinstance(definition, Mapping) or not isinstance(resolved_job, Mapping):
            raise TemplateRegistryError(
                f"{label} template {template_id} definition and resolved job must be objects"
            )
        try:
            normalized_definition, expected_resolved_job = normalize_job_template(definition)
        except JobTemplateError as exc:
            raise TemplateRegistryError(
                f"{label} template {template_id} does not satisfy the current job-template contract"
            ) from exc
        if normalized_definition != definition:
            raise TemplateRegistryError(
                f"{label} template {template_id} definition is not canonical"
            )
        if expected_resolved_job != resolved_job:
            raise TemplateRegistryError(
                f"{label} template {template_id} resolved job does not match its definition"
            )
        if str(row[3]) != job_template_digest(definition):
            raise TemplateRegistryError(f"{label} template {template_id} has an invalid digest")
        if int(row[4]) < 1:
            raise TemplateRegistryError(f"{label} template {template_id} has an invalid revision")
        if int(row[5]) not in (0, 1):
            raise TemplateRegistryError(
                f"{label} template {template_id} has an invalid enabled state"
            )
        if not str(row[6]).strip() or not str(row[7]).strip():
            raise TemplateRegistryError(f"{label} template {template_id} has a blank timestamp")
        count += 1
    return count


def validate_template_registry(source_db: Path) -> int:
    with closing(_connect_read_only(Path(source_db))) as source:
        _quick_check(source, label="template registry")
        _require_template_schema(source, label="template registry")
        return _validate_template_rows(source, label="template registry")


def _temporary_path(parent: Path, stem: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{stem}.", suffix=".sqlite3", dir=parent)
    os.close(descriptor)
    return Path(raw_path)


def create_template_registry_snapshot(source_db: Path, snapshot_db: Path) -> int:
    """Atomically create a verified, template-only snapshot from a live Munchy server DB."""

    source_db = Path(source_db)
    snapshot_db = Path(snapshot_db)
    snapshot_db.parent.mkdir(parents=True, exist_ok=True)
    online_copy = _temporary_path(snapshot_db.parent, "server-online-copy")
    pending_snapshot = _temporary_path(snapshot_db.parent, "template-registry")
    try:
        with (
            closing(_connect_read_only(source_db)) as source,
            closing(sqlite3.connect(online_copy)) as consistent,
        ):
            source.backup(consistent)
            _quick_check(consistent, label="Munchy server database snapshot")
            _require_template_schema(consistent, label="Munchy server database snapshot")

        with closing(sqlite3.connect(pending_snapshot)) as snapshot:
            ensure_template_registry_schema(snapshot)
            snapshot.execute(
                """
                CREATE TABLE template_registry_snapshot (
                    schema_version INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
                """
            )
            snapshot.execute(
                "INSERT INTO template_registry_snapshot(schema_version, created_at) VALUES(?, ?)",
                (
                    SNAPSHOT_SCHEMA_VERSION,
                    datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                ),
            )
            snapshot.execute("ATTACH DATABASE ? AS server", (str(online_copy),))
            columns = ", ".join(JOB_TEMPLATE_COLUMNS)
            snapshot.execute(
                f"INSERT INTO main.job_templates({columns}) "
                f"SELECT {columns} FROM server.job_templates ORDER BY template_id"
            )
            snapshot.commit()
            snapshot.execute("DETACH DATABASE server")
            _quick_check(snapshot, label="template registry snapshot")
            count = _validate_template_rows(snapshot, label="template registry snapshot")
        os.replace(pending_snapshot, snapshot_db)
        return count
    finally:
        online_copy.unlink(missing_ok=True)
        pending_snapshot.unlink(missing_ok=True)


def inspect_template_registry_snapshot(snapshot_db: Path) -> int:
    with closing(_connect_read_only(Path(snapshot_db))) as snapshot:
        _quick_check(snapshot, label="template registry snapshot")
        _require_template_schema(snapshot, label="template registry snapshot")
        metadata = snapshot.execute(
            "SELECT schema_version, created_at FROM template_registry_snapshot"
        ).fetchall()
        if (
            len(metadata) != 1
            or int(metadata[0][0]) != SNAPSHOT_SCHEMA_VERSION
            or not str(metadata[0][1]).strip()
        ):
            raise TemplateRegistryError("template registry snapshot metadata is invalid")
        return _validate_template_rows(snapshot, label="template registry snapshot")


def restore_template_registry_snapshot(
    snapshot_db: Path,
    target_db: Path,
    *,
    replace: bool = False,
) -> int:
    """Restore templates while preserving all non-registry state in the target DB."""

    snapshot_db = Path(snapshot_db)
    target_db = Path(target_db)
    expected_count = inspect_template_registry_snapshot(snapshot_db)
    target_db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(target_db, timeout=30)) as target:
        target.execute("PRAGMA busy_timeout=30000")
        ensure_template_registry_schema(target)
        target.commit()
        target.execute("BEGIN IMMEDIATE")
        existing = int(target.execute("SELECT COUNT(*) FROM job_templates").fetchone()[0])
        if existing and not replace:
            target.rollback()
            raise TemplateRegistryError(
                "target template registry is not empty; pass replace=True to replace it"
            )
        target.execute("ATTACH DATABASE ? AS snapshot", (str(snapshot_db.resolve()),))
        try:
            target.execute("DELETE FROM main.job_templates")
            columns = ", ".join(JOB_TEMPLATE_COLUMNS)
            target.execute(
                f"INSERT INTO main.job_templates({columns}) "
                f"SELECT {columns} FROM snapshot.job_templates ORDER BY template_id"
            )
            restored_count = int(
                target.execute("SELECT COUNT(*) FROM main.job_templates").fetchone()[0]
            )
            if restored_count != expected_count:
                raise TemplateRegistryError(
                    f"restored {restored_count} templates; expected {expected_count}"
                )
            _validate_template_rows(target, label="restored template registry")
            target.commit()
        except Exception:
            target.rollback()
            raise
        finally:
            target.execute("DETACH DATABASE snapshot")
        _quick_check(target, label="restored Munchy server database")
    return expected_count


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Back up and restore Munchy job templates")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup", help="create a template-only SQLite snapshot")
    backup.add_argument("source_db", type=Path)
    backup.add_argument("snapshot_db", type=Path)
    verify = subparsers.add_parser("verify", help="verify a template snapshot")
    verify.add_argument("snapshot_db", type=Path)
    restore = subparsers.add_parser(
        "restore", help="restore templates into a Munchy server database"
    )
    restore.add_argument("snapshot_db", type=Path)
    restore.add_argument("target_db", type=Path)
    restore.add_argument(
        "--replace",
        action="store_true",
        help="replace a non-empty target registry",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "backup":
        count = create_template_registry_snapshot(args.source_db, args.snapshot_db)
    elif args.command == "verify":
        count = inspect_template_registry_snapshot(args.snapshot_db)
    else:
        count = restore_template_registry_snapshot(
            args.snapshot_db,
            args.target_db,
            replace=bool(args.replace),
        )
    print(f"templates={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "TemplateRegistryError",
    "create_template_registry_snapshot",
    "ensure_template_registry_schema",
    "inspect_template_registry_snapshot",
    "restore_template_registry_snapshot",
    "validate_template_registry",
]
