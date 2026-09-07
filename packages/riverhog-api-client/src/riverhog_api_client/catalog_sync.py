"""Durable bounded replica for Riverhog's native catalog synchronization contract."""

from __future__ import annotations

import os
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Protocol

from riverhog_protocol import (
    CATALOG_SYNC_PAGE_SIZE_MAX,
    CatalogSyncChangePage,
    CatalogSyncCheckpoint,
    CatalogSyncCollectionPage,
    CatalogSyncDelete,
    CatalogSyncDescriptor,
    CatalogSyncUpsert,
)
from riverhog_protocol.errors import RiverhogError


class CatalogSyncApi(Protocol):
    def create_catalog_sync_checkpoint(self) -> CatalogSyncCheckpoint: ...
    def list_catalog_sync_collections(
        self, cursor: str, *, limit: int = 100
    ) -> CatalogSyncCollectionPage: ...
    def list_catalog_sync_changes(
        self, cursor: str, *, limit: int = 100
    ) -> CatalogSyncChangePage: ...


class CatalogReplica:
    """Build and atomically publish an authorized local catalog projection."""

    def __init__(self, database: str | Path) -> None:
        self.path = Path(database).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass
        with closing(self._connect()) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS catalog_replica_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    source_identity TEXT,
                    authorization_view_identity TEXT,
                    active_generation TEXT,
                    building_generation TEXT,
                    phase TEXT NOT NULL DEFAULT 'new'
                        CHECK (phase IN ('new','catalog','catchup','following','reset_required')),
                    cursor TEXT,
                    through_revision INTEGER NOT NULL DEFAULT 0 CHECK (through_revision >= 0),
                    serial INTEGER NOT NULL DEFAULT 0 CHECK (serial >= 0),
                    usable INTEGER NOT NULL DEFAULT 0 CHECK (usable IN (0, 1))
                );
                INSERT OR IGNORE INTO catalog_replica_state (singleton) VALUES (1);
                CREATE TABLE IF NOT EXISTS catalog_replica_generations (
                    id TEXT PRIMARY KEY,
                    obsolete INTEGER NOT NULL DEFAULT 0 CHECK (obsolete IN (0, 1))
                );
                CREATE INDEX IF NOT EXISTS ix_catalog_replica_generations_gc
                    ON catalog_replica_generations (obsolete, id);
                CREATE TABLE IF NOT EXISTS catalog_replica_collections (
                    generation TEXT NOT NULL,
                    collection_id INTEGER NOT NULL CHECK (collection_id > 0),
                    revision INTEGER NOT NULL CHECK (revision > 0),
                    archive_root_sha256 TEXT,
                    content_identity TEXT,
                    deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
                    PRIMARY KEY (generation, collection_id),
                    FOREIGN KEY (generation) REFERENCES catalog_replica_generations(id)
                        ON DELETE CASCADE,
                    CHECK (
                        deleted = 1 AND archive_root_sha256 IS NULL AND content_identity IS NULL
                        OR deleted = 0
                           AND length(archive_root_sha256) = 64
                           AND length(content_identity) = 64
                    )
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS ix_catalog_replica_collections_live
                    ON catalog_replica_collections (generation, collection_id)
                    WHERE deleted = 0;
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA synchronous = FULL")
        return db

    def status(self) -> dict[str, object]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM catalog_replica_state").fetchone()
            if row is None:
                raise RuntimeError("catalog replica state is unavailable")
            return dict(row)

    def start(self, api: CatalogSyncApi) -> dict[str, object]:
        """Perform one checkpoint request and begin a fresh durable generation."""

        before = self.status()
        try:
            checkpoint = api.create_catalog_sync_checkpoint()
        except RiverhogError as exc:
            self._invalidate(exc, serial=_required_int(before, "serial"))
            raise
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._require_serial(db, _required_int(before, "serial"))
                keep_active = (
                    before["active_generation"] is not None
                    and before["source_identity"] == checkpoint.source_identity
                    and before["authorization_view_identity"]
                    == checkpoint.authorization_view_identity
                )
                building = before["building_generation"]
                if building is not None:
                    db.execute(
                        "UPDATE catalog_replica_generations SET obsolete = 1 WHERE id = ?",
                        (building,),
                    )
                generation = secrets.token_hex(16)
                db.execute(
                    "INSERT INTO catalog_replica_generations (id) VALUES (?)",
                    (generation,),
                )
                db.execute(
                    """
                    UPDATE catalog_replica_state
                    SET source_identity = ?, authorization_view_identity = ?,
                        building_generation = ?, phase = 'catalog', cursor = ?,
                        through_revision = 0, serial = serial + 1, usable = ?
                    """,
                    (
                        checkpoint.source_identity,
                        checkpoint.authorization_view_identity,
                        generation,
                        checkpoint.catalog_cursor,
                        int(keep_active),
                    ),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return self.status()

    def step(self, api: CatalogSyncApi, *, limit: int = 100) -> dict[str, object]:
        """Apply exactly one remote page and its continuation in one local transaction."""

        if isinstance(limit, bool) or not 1 <= limit <= CATALOG_SYNC_PAGE_SIZE_MAX:
            raise ValueError("catalog synchronization page size is outside the v1 bound")
        before = self.status()
        phase = str(before["phase"])
        cursor = before["cursor"]
        if phase not in {"catalog", "catchup", "following"} or not isinstance(cursor, str):
            raise RuntimeError("catalog replica requires an explicit synchronization start")
        try:
            if phase == "catalog":
                page: CatalogSyncCollectionPage | CatalogSyncChangePage = (
                    api.list_catalog_sync_collections(cursor, limit=limit)
                )
            else:
                page = api.list_catalog_sync_changes(cursor, limit=limit)
        except RiverhogError as exc:
            self._invalidate(exc, serial=_required_int(before, "serial"))
            raise
        if (
            page.source_identity != before["source_identity"]
            or page.authorization_view_identity != before["authorization_view_identity"]
        ):
            raise ValueError("catalog synchronization response changed its bound authority")

        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._require_serial(db, _required_int(before, "serial"))
                generation = before["building_generation"] or before["active_generation"]
                if not isinstance(generation, str):
                    raise RuntimeError("catalog replica generation is unavailable")
                if isinstance(page, CatalogSyncCollectionPage):
                    self._apply_catalog_page(db, generation, page)
                    if page.next_cursor is not None:
                        if not page.collections or page.changes_cursor is not None:
                            raise ValueError("catalog page continuation is inconsistent")
                        next_cursor = page.next_cursor
                        next_phase = "catalog"
                    else:
                        if page.changes_cursor is None:
                            raise ValueError("final catalog page omitted its change cursor")
                        next_cursor = page.changes_cursor
                        next_phase = "catchup"
                    through_revision = 0
                else:
                    through_revision = int(page.through_revision)
                    if through_revision < _required_int(before, "through_revision"):
                        raise ValueError("catalog change page moved its revision backward")
                    self._apply_change_page(
                        db,
                        generation,
                        page,
                        after=_required_int(before, "through_revision"),
                    )
                    next_cursor = page.next_cursor
                    next_phase = "following" if page.caught_up else phase
                promote = before["building_generation"] is not None and next_phase == "following"
                active = before["active_generation"]
                if promote and active is not None:
                    db.execute(
                        "UPDATE catalog_replica_generations SET obsolete = 1 WHERE id = ?",
                        (active,),
                    )
                db.execute(
                    """
                    UPDATE catalog_replica_state
                    SET cursor = ?, phase = ?, through_revision = ?, serial = serial + 1,
                        active_generation = ?, building_generation = ?, usable = ?
                    """,
                    (
                        next_cursor,
                        next_phase,
                        through_revision,
                        generation if promote else active,
                        None if promote else before["building_generation"],
                        int(active is not None or next_phase == "following"),
                    ),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return self.status()

    def page(self, *, after: int = 0, limit: int = 100) -> list[CatalogSyncDescriptor]:
        if isinstance(after, bool) or after < 0:
            raise ValueError("local catalog position must be non-negative")
        if isinstance(limit, bool) or not 1 <= limit <= CATALOG_SYNC_PAGE_SIZE_MAX:
            raise ValueError("local catalog page size is outside the v1 bound")
        with closing(self._connect()) as db:
            state = db.execute("SELECT * FROM catalog_replica_state").fetchone()
            if state is None or not state["usable"] or state["active_generation"] is None:
                raise RuntimeError("catalog replica is not synchronized for its current view")
            rows = db.execute(
                """
                SELECT collection_id, revision, archive_root_sha256, content_identity
                FROM catalog_replica_collections
                WHERE generation = ? AND collection_id > ? AND deleted = 0
                ORDER BY collection_id
                LIMIT ?
                """,
                (state["active_generation"], after, limit),
            ).fetchall()
            return [
                CatalogSyncDescriptor(
                    collection_id=int(row["collection_id"]),
                    revision=str(row["revision"]),
                    archive_root_sha256=str(row["archive_root_sha256"]),
                    content_identity=str(row["content_identity"]),
                )
                for row in rows
            ]

    def get(self, collection_id: int) -> CatalogSyncDescriptor | None:
        """Read one collection from the active local generation."""

        if isinstance(collection_id, bool) or collection_id < 1:
            raise ValueError("collection identity must be positive")
        with closing(self._connect()) as db:
            state = db.execute("SELECT * FROM catalog_replica_state").fetchone()
            if state is None or not state["usable"] or state["active_generation"] is None:
                raise RuntimeError("catalog replica is not synchronized for its current view")
            row = db.execute(
                """
                SELECT collection_id, revision, archive_root_sha256, content_identity
                FROM catalog_replica_collections
                WHERE generation = ? AND collection_id = ? AND deleted = 0
                """,
                (state["active_generation"], collection_id),
            ).fetchone()
            if row is None:
                return None
            return CatalogSyncDescriptor(
                collection_id=int(row["collection_id"]),
                revision=str(row["revision"]),
                archive_root_sha256=str(row["archive_root_sha256"]),
                content_identity=str(row["content_identity"]),
            )

    def reclaim(self, *, limit: int = 100) -> int:
        """Reclaim one bounded obsolete-generation or settled-tombstone slice."""

        if isinstance(limit, bool) or not 1 <= limit <= CATALOG_SYNC_PAGE_SIZE_MAX:
            raise ValueError("catalog replica reclaim size is outside the v1 bound")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                obsolete = db.execute(
                    "SELECT id FROM catalog_replica_generations "
                    "WHERE obsolete = 1 ORDER BY id LIMIT 1"
                ).fetchone()
                if obsolete is None:
                    tombstones = db.execute(
                        "SELECT generation, collection_id "
                        "FROM catalog_replica_collections WHERE deleted = 1 "
                        "ORDER BY generation, collection_id LIMIT ?",
                        (limit,),
                    ).fetchall()
                    db.executemany(
                        "DELETE FROM catalog_replica_collections "
                        "WHERE generation = ? AND collection_id = ? AND deleted = 1",
                        ((str(row["generation"]), int(row["collection_id"])) for row in tombstones),
                    )
                    db.commit()
                    return len(tombstones)
                generation = str(obsolete["id"])
                rows = db.execute(
                    "SELECT collection_id FROM catalog_replica_collections "
                    "WHERE generation = ? ORDER BY collection_id LIMIT ?",
                    (generation, limit),
                ).fetchall()
                db.executemany(
                    "DELETE FROM catalog_replica_collections "
                    "WHERE generation = ? AND collection_id = ?",
                    ((generation, int(row["collection_id"])) for row in rows),
                )
                if not db.execute(
                    "SELECT 1 FROM catalog_replica_collections WHERE generation = ? LIMIT 1",
                    (generation,),
                ).fetchone():
                    db.execute(
                        "DELETE FROM catalog_replica_generations WHERE id = ?",
                        (generation,),
                    )
                db.commit()
                return len(rows)
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _apply_catalog_page(
        db: sqlite3.Connection,
        generation: str,
        page: CatalogSyncCollectionPage,
    ) -> None:
        last = db.execute(
            "SELECT collection_id FROM catalog_replica_collections "
            "WHERE generation = ? ORDER BY collection_id DESC LIMIT 1",
            (generation,),
        ).fetchone()
        previous = int(last["collection_id"]) if last is not None else 0
        for item in page.collections:
            if item.collection_id <= previous:
                raise ValueError("catalog synchronization collection page is not canonical")
            previous = item.collection_id
            CatalogReplica._upsert(db, generation, item, deleted=False)

    @staticmethod
    def _apply_change_page(
        db: sqlite3.Connection,
        generation: str,
        page: CatalogSyncChangePage,
        *,
        after: int,
    ) -> None:
        previous = after
        for item in page.changes:
            revision = int(item.revision)
            if revision <= previous:
                raise ValueError("catalog synchronization change page is not canonical")
            previous = revision
            CatalogReplica._upsert(
                db,
                generation,
                item,
                deleted=isinstance(item, CatalogSyncDelete),
            )
        if page.changes and int(page.through_revision) < previous:
            raise ValueError("catalog synchronization change cursor precedes its page")
        if not page.caught_up and not page.changes:
            # Invisible changes may legitimately produce an empty result while the
            # signed through-position advances.
            if int(page.through_revision) <= after:
                raise ValueError("catalog synchronization change page did not advance")

    @staticmethod
    def _upsert(
        db: sqlite3.Connection,
        generation: str,
        item: CatalogSyncDescriptor | CatalogSyncUpsert | CatalogSyncDelete,
        *,
        deleted: bool,
    ) -> None:
        revision = int(item.revision)
        root = None if deleted else item.archive_root_sha256  # type: ignore[union-attr]
        content = None if deleted else item.content_identity  # type: ignore[union-attr]
        existing = db.execute(
            "SELECT * FROM catalog_replica_collections WHERE generation = ? AND collection_id = ?",
            (generation, item.collection_id),
        ).fetchone()
        if existing is not None and int(existing["revision"]) == revision:
            if (
                existing["archive_root_sha256"],
                existing["content_identity"],
                bool(existing["deleted"]),
            ) != (root, content, deleted):
                raise ValueError("equal catalog revisions have different contents")
            return
        db.execute(
            """
            INSERT INTO catalog_replica_collections (
                generation, collection_id, revision,
                archive_root_sha256, content_identity, deleted
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (generation, collection_id) DO UPDATE SET
                revision = excluded.revision,
                archive_root_sha256 = excluded.archive_root_sha256,
                content_identity = excluded.content_identity,
                deleted = excluded.deleted
            WHERE excluded.revision > catalog_replica_collections.revision
            """,
            (generation, item.collection_id, revision, root, content, int(deleted)),
        )

    def _invalidate(self, error: RiverhogError, *, serial: int) -> None:
        if error.code not in {
            "unauthorized",
            "forbidden",
            "catalog_sync_cursor_expired",
            "catalog_sync_history_expired",
            "catalog_sync_source_changed",
            "catalog_sync_view_changed",
        }:
            return
        with closing(self._connect()) as db:
            clear_active = error.code in {
                "unauthorized",
                "forbidden",
                "catalog_sync_source_changed",
                "catalog_sync_view_changed",
            }
            db.execute(
                "UPDATE catalog_replica_state "
                "SET usable = CASE WHEN ? THEN 0 ELSE usable END, "
                "phase = 'reset_required', serial = serial + 1 WHERE serial = ?",
                (int(clear_active), serial),
            )

    @staticmethod
    def _require_serial(db: sqlite3.Connection, expected: int) -> None:
        observed = db.execute("SELECT serial FROM catalog_replica_state").fetchone()
        if observed is None or int(observed["serial"]) != expected:
            raise RuntimeError("another catalog replica worker advanced state")


def _required_int(values: dict[str, object], key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"catalog replica state field {key} is invalid")
    return value


__all__ = ["CatalogReplica", "CatalogSyncApi"]
