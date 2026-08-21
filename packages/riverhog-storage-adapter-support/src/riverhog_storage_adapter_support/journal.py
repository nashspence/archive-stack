"""Bounded SQLite transfer journal for adapter restart and lost-response recovery."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from riverhog_storage_adapter_protocol import (
    ObjectReceipt,
    UploadDeclaration,
    UploadPartReceipt,
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class JournalUpload:
    declaration: UploadDeclaration
    state: str
    provider_upload_id: str | None
    parts: tuple[UploadPartReceipt, ...]
    object: ObjectReceipt | None


class UploadJournal:
    """One application-owned durable journal; provider details remain opaque strings."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS uploads (
                    transfer_id TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL,
                    declaration_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    provider_upload_id TEXT,
                    object_json TEXT,
                    acknowledged_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS upload_parts (
                    transfer_id TEXT NOT NULL REFERENCES uploads(transfer_id) ON DELETE CASCADE,
                    number INTEGER NOT NULL,
                    receipt_json TEXT NOT NULL,
                    PRIMARY KEY (transfer_id, number)
                );
                CREATE INDEX IF NOT EXISTS ix_uploads_state_created
                    ON uploads(state, created_at);
                CREATE TABLE IF NOT EXISTS objects (
                    object_path TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    PRIMARY KEY (object_path, revision)
                );
                CREATE INDEX IF NOT EXISTS ix_objects_path
                    ON objects(object_path);
                """
            )

    def load(self, transfer_id: str) -> JournalUpload | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM uploads WHERE transfer_id = ?",
                (transfer_id,),
            ).fetchone()
            if row is None:
                return None
            part_rows = connection.execute(
                """
                SELECT receipt_json
                FROM upload_parts
                WHERE transfer_id = ?
                ORDER BY number
                """,
                (transfer_id,),
            ).fetchall()
        declaration = UploadDeclaration.model_validate_json(str(row["declaration_json"]))
        parts = tuple(
            UploadPartReceipt.model_validate_json(str(part["receipt_json"])) for part in part_rows
        )
        object_json = row["object_json"]
        return JournalUpload(
            declaration=declaration,
            state=str(row["state"]),
            provider_upload_id=(
                str(row["provider_upload_id"]) if row["provider_upload_id"] is not None else None
            ),
            parts=parts,
            object=(
                ObjectReceipt.model_validate_json(str(object_json))
                if object_json is not None
                else None
            ),
        )

    def declare(self, declaration: UploadDeclaration) -> JournalUpload:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT request_sha256 FROM uploads WHERE transfer_id = ?",
                (declaration.transfer_id,),
            ).fetchone()
            if row is None:
                now = _now()
                connection.execute(
                    """
                    INSERT INTO uploads(
                        transfer_id, request_sha256, declaration_json, state,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'creating', ?, ?)
                    """,
                    (
                        declaration.transfer_id,
                        declaration.request_sha256,
                        declaration.model_dump_json(exclude_none=True),
                        now,
                        now,
                    ),
                )
            elif str(row["request_sha256"]) != declaration.request_sha256:
                raise ValueError("transfer ID is already bound to another request")
        loaded = self.load(declaration.transfer_id)
        if loaded is None:  # pragma: no cover - SQLite postcondition
            raise RuntimeError("declared upload disappeared from its journal")
        return loaded

    def bind_provider_upload(self, transfer_id: str, provider_upload_id: str) -> None:
        if not provider_upload_id:
            raise ValueError("provider upload ID must not be empty")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE uploads
                SET state = 'open', provider_upload_id = ?, updated_at = ?
                WHERE transfer_id = ? AND state = 'creating'
                """,
                (provider_upload_id, _now(), transfer_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("upload is not awaiting its provider binding")

    def record_part(self, transfer_id: str, receipt: UploadPartReceipt) -> None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM uploads WHERE transfer_id = ?",
                (transfer_id,),
            ).fetchone()
            if row is None or str(row["state"]) != "open":
                raise RuntimeError("upload is not open")
            existing = connection.execute(
                """
                SELECT receipt_json
                FROM upload_parts
                WHERE transfer_id = ? AND number = ?
                """,
                (transfer_id, receipt.number),
            ).fetchone()
            if existing is not None:
                current = UploadPartReceipt.model_validate_json(str(existing["receipt_json"]))
                if current != receipt:
                    raise ValueError("upload part is already bound to a different receipt")
                return
            connection.execute(
                """
                INSERT INTO upload_parts(transfer_id, number, receipt_json)
                VALUES (?, ?, ?)
                """,
                (transfer_id, receipt.number, receipt.model_dump_json()),
            )
            connection.execute(
                "UPDATE uploads SET updated_at = ? WHERE transfer_id = ?",
                (_now(), transfer_id),
            )

    def complete(self, transfer_id: str, receipt: ObjectReceipt) -> None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT state, object_json FROM uploads WHERE transfer_id = ?",
                (transfer_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("upload is not declared")
            if str(row["state"]) == "completed":
                existing = ObjectReceipt.model_validate_json(str(row["object_json"]))
                if existing != receipt:
                    raise ValueError("completed upload receipt changed")
                return
            if str(row["state"]) != "open":
                raise RuntimeError("upload is not open")
            connection.execute(
                """
                UPDATE uploads
                SET state = 'completed', object_json = ?, updated_at = ?
                WHERE transfer_id = ?
                """,
                (receipt.model_dump_json(), _now(), transfer_id),
            )
            existing_object = connection.execute(
                """
                SELECT receipt_json
                FROM objects
                WHERE object_path = ? AND revision = ?
                """,
                (receipt.object_path, receipt.revision),
            ).fetchone()
            if existing_object is None:
                connection.execute(
                    """
                    INSERT INTO objects(object_path, revision, receipt_json)
                    VALUES (?, ?, ?)
                    """,
                    (
                        receipt.object_path,
                        receipt.revision,
                        receipt.model_dump_json(),
                    ),
                )
            elif ObjectReceipt.model_validate_json(str(existing_object["receipt_json"])) != receipt:
                raise ValueError("object revision is already bound to another receipt")

    def abort(self, transfer_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE uploads
                SET state = 'aborted', updated_at = ?
                WHERE transfer_id = ? AND state IN ('creating', 'open')
                """,
                (_now(), transfer_id),
            )

    def acknowledge_terminal(self, transfer_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE uploads
                SET acknowledged_at = COALESCE(acknowledged_at, ?), updated_at = ?
                WHERE transfer_id = ? AND state IN ('completed', 'aborted')
                """,
                (_now(), _now(), transfer_id),
            )

    def open_before(self, initiated_before: str) -> tuple[JournalUpload, ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT transfer_id
                FROM uploads
                WHERE state IN ('creating', 'open') AND created_at < ?
                ORDER BY transfer_id
                """,
                (initiated_before,),
            ).fetchall()
        return tuple(
            current for row in rows if (current := self.load(str(row["transfer_id"]))) is not None
        )

    def object_receipt(self, object_path: str, revision: str) -> ObjectReceipt | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT receipt_json
                FROM objects
                WHERE object_path = ? AND revision = ?
                """,
                (object_path, revision),
            ).fetchone()
        return (
            ObjectReceipt.model_validate_json(str(row["receipt_json"])) if row is not None else None
        )

    def remove_object(self, object_path: str, revision: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM objects WHERE object_path = ? AND revision = ?",
                (object_path, revision),
            )

    def remove_prefix(self, object_prefix: str) -> int:
        escaped = object_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM objects
                WHERE object_path = ? OR object_path LIKE ? ESCAPE '\\'
                """,
                (object_prefix, f"{escaped}/%"),
            )
            return int(cursor.rowcount)

    def sweep_terminal(self, *, acknowledged_before: str) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM uploads
                WHERE state IN ('completed', 'aborted')
                  AND acknowledged_at IS NOT NULL
                  AND acknowledged_at < ?
                """,
                (acknowledged_before,),
            )
            return int(cursor.rowcount)


__all__ = ["JournalUpload", "UploadJournal"]
