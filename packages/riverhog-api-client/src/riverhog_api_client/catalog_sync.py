"""Durable bounded replica for Riverhog's native catalog synchronization contract."""

from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol, cast

from riverhog_protocol import (
    CATALOG_SYNC_PAGE_SIZE_MAX,
    COLLECTION_TAG_REQUEST_MEMBERS_MAX,
    CatalogSyncChangePage,
    CatalogSyncCheckpoint,
    CatalogSyncCollectionPage,
    CatalogSyncDelete,
    CatalogSyncDescriptor,
    CatalogSyncUpsert,
    validate_collection_tag,
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
    def list_collection_tags(
        self,
        collection_id: int,
        *,
        revision: int,
        tag_set_identity: str,
        page_size: int = 25,
        page_token: str | None = None,
    ) -> dict[str, Any]: ...


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
                    description TEXT,
                    description_revision INTEGER,
                    description_identity TEXT,
                    tag_revision INTEGER,
                    tag_set_identity TEXT,
                    deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
                    PRIMARY KEY (generation, collection_id),
                    FOREIGN KEY (generation) REFERENCES catalog_replica_generations(id)
                        ON DELETE CASCADE,
                    CHECK (
                        deleted = 1
                           AND archive_root_sha256 IS NULL
                           AND content_identity IS NULL
                           AND description IS NULL
                           AND description_revision IS NULL
                           AND description_identity IS NULL
                           AND tag_revision IS NULL
                           AND tag_set_identity IS NULL
                        OR deleted = 0
                           AND length(archive_root_sha256) = 64
                           AND length(content_identity) = 64
                           AND description_revision >= 0
                           AND length(description_identity) = 64
                           AND tag_revision >= 1
                           AND length(tag_set_identity) = 64
                    )
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS ix_catalog_replica_collections_live
                    ON catalog_replica_collections (generation, collection_id)
                    WHERE deleted = 0;
                CREATE TABLE IF NOT EXISTS catalog_replica_tag_sync (
                    generation TEXT NOT NULL,
                    collection_id INTEGER NOT NULL CHECK (collection_id > 0),
                    tag_revision INTEGER NOT NULL CHECK (tag_revision >= 1),
                    tag_set_identity TEXT NOT NULL CHECK (length(tag_set_identity) = 64),
                    page_token TEXT,
                    complete INTEGER NOT NULL DEFAULT 0 CHECK (complete IN (0, 1)),
                    PRIMARY KEY (generation, collection_id),
                    FOREIGN KEY (generation, collection_id)
                        REFERENCES catalog_replica_collections(generation, collection_id)
                        ON DELETE CASCADE
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS ix_catalog_replica_tag_sync_work
                    ON catalog_replica_tag_sync (generation, complete, collection_id);
                CREATE TABLE IF NOT EXISTS catalog_replica_tags (
                    generation TEXT NOT NULL,
                    collection_id INTEGER NOT NULL CHECK (collection_id > 0),
                    tag_revision INTEGER NOT NULL CHECK (tag_revision >= 1),
                    tag TEXT NOT NULL,
                    PRIMARY KEY (generation, collection_id, tag_revision, tag),
                    FOREIGN KEY (generation, collection_id)
                        REFERENCES catalog_replica_tag_sync(generation, collection_id)
                        ON DELETE CASCADE
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS ix_catalog_replica_tags_lookup
                    ON catalog_replica_tags (tag, generation, collection_id);
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
        pending_tags = self._pending_tags(before)
        if pending_tags is not None:
            return self._step_tags(api, before=before, pending=pending_tags, limit=limit)
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
                promote = (
                    before["building_generation"] is not None
                    and next_phase == "following"
                    and not self._has_pending_tags(db, generation)
                )
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

    def page(
        self,
        *,
        after: int = 0,
        limit: int = 100,
        tags: Sequence[str] = (),
    ) -> list[CatalogSyncDescriptor]:
        if isinstance(after, bool) or after < 0:
            raise ValueError("local catalog position must be non-negative")
        if isinstance(limit, bool) or not 1 <= limit <= CATALOG_SYNC_PAGE_SIZE_MAX:
            raise ValueError("local catalog page size is outside the v1 bound")
        canonical_tags = tuple(validate_collection_tag(tag) for tag in tags)
        if len(canonical_tags) > COLLECTION_TAG_REQUEST_MEMBERS_MAX or len(
            set(canonical_tags)
        ) != len(canonical_tags):
            raise ValueError("local catalog tag selector is outside the v1 bound")
        with closing(self._connect()) as db:
            state = db.execute("SELECT * FROM catalog_replica_state").fetchone()
            if state is None or not state["usable"] or state["active_generation"] is None:
                raise RuntimeError("catalog replica is not synchronized for its current view")
            tag_filter = ""
            parameters: list[object] = [state["active_generation"], after]
            if canonical_tags:
                placeholders = ",".join("?" for _tag in canonical_tags)
                tag_filter = f"""
                AND EXISTS (
                    SELECT 1 FROM catalog_replica_tag_sync AS s
                    WHERE s.generation = catalog_replica_collections.generation
                      AND s.collection_id = catalog_replica_collections.collection_id
                      AND s.tag_revision = catalog_replica_collections.tag_revision
                      AND s.tag_set_identity = catalog_replica_collections.tag_set_identity
                      AND s.complete = 1
                )
                AND (
                    SELECT COUNT(*) FROM catalog_replica_tags AS t
                    WHERE t.generation = catalog_replica_collections.generation
                      AND t.collection_id = catalog_replica_collections.collection_id
                      AND t.tag_revision = catalog_replica_collections.tag_revision
                      AND t.tag IN ({placeholders})
                ) = ?
                """
                parameters.extend(canonical_tags)
                parameters.append(len(canonical_tags))
            parameters.append(limit)
            rows = db.execute(
                f"""
                SELECT collection_id, revision, archive_root_sha256, content_identity,
                       description, description_revision, description_identity,
                       tag_revision, tag_set_identity
                FROM catalog_replica_collections
                WHERE generation = ? AND collection_id > ? AND deleted = 0
                {tag_filter}
                ORDER BY collection_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            return [
                CatalogSyncDescriptor(
                    collection_id=int(row["collection_id"]),
                    revision=str(row["revision"]),
                    archive_root_sha256=str(row["archive_root_sha256"]),
                    content_identity=str(row["content_identity"]),
                    description=row["description"],
                    description_revision=int(row["description_revision"]),
                    description_identity=str(row["description_identity"]),
                    tag_revision=int(row["tag_revision"]),
                    tag_set_identity=str(row["tag_set_identity"]),
                )
                for row in rows
            ]

    def tag_page(
        self,
        collection_id: int,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> list[str]:
        """Read one bounded page from a completely hydrated current local tag authority."""

        if isinstance(collection_id, bool) or collection_id < 1:
            raise ValueError("collection identity must be positive")
        if isinstance(limit, bool) or not 1 <= limit <= CATALOG_SYNC_PAGE_SIZE_MAX:
            raise ValueError("local tag page size is outside the v1 bound")
        canonical_after = None if after is None else validate_collection_tag(after)
        with closing(self._connect()) as db:
            state = db.execute("SELECT * FROM catalog_replica_state").fetchone()
            if state is None or not state["usable"] or state["active_generation"] is None:
                raise RuntimeError("catalog replica is not synchronized for its current view")
            authority = db.execute(
                """
                SELECT c.tag_revision, c.tag_set_identity, s.complete
                FROM catalog_replica_collections AS c
                JOIN catalog_replica_tag_sync AS s
                  ON s.generation = c.generation AND s.collection_id = c.collection_id
                 AND s.tag_revision = c.tag_revision
                 AND s.tag_set_identity = c.tag_set_identity
                WHERE c.generation = ? AND c.collection_id = ? AND c.deleted = 0
                """,
                (state["active_generation"], collection_id),
            ).fetchone()
            if authority is None or not authority["complete"]:
                raise RuntimeError("local collection tag authority is not synchronized")
            return [
                str(row["tag"])
                for row in db.execute(
                    """
                    SELECT tag FROM catalog_replica_tags
                    WHERE generation = ? AND collection_id = ? AND tag_revision = ?
                      AND (? IS NULL OR tag > ?)
                    ORDER BY tag LIMIT ?
                    """,
                    (
                        state["active_generation"],
                        collection_id,
                        int(authority["tag_revision"]),
                        canonical_after,
                        canonical_after,
                        limit,
                    ),
                )
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
                SELECT collection_id, revision, archive_root_sha256, content_identity,
                       description, description_revision, description_identity,
                       tag_revision, tag_set_identity
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
                description=row["description"],
                description_revision=int(row["description_revision"]),
                description_identity=str(row["description_identity"]),
                tag_revision=int(row["tag_revision"]),
                tag_set_identity=str(row["tag_set_identity"]),
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

    def _pending_tags(self, state: dict[str, object]) -> sqlite3.Row | None:
        generation = state["building_generation"] or state["active_generation"]
        if not isinstance(generation, str):
            return None
        with closing(self._connect()) as db:
            return cast(
                sqlite3.Row | None,
                db.execute(
                    """
                SELECT generation, collection_id, tag_revision, tag_set_identity, page_token
                FROM catalog_replica_tag_sync
                WHERE generation = ? AND complete = 0
                ORDER BY collection_id
                LIMIT 1
                """,
                    (generation,),
                ).fetchone(),
            )

    def _step_tags(
        self,
        api: CatalogSyncApi,
        *,
        before: dict[str, object],
        pending: sqlite3.Row,
        limit: int,
    ) -> dict[str, object]:
        try:
            payload = api.list_collection_tags(
                int(pending["collection_id"]),
                revision=int(pending["tag_revision"]),
                tag_set_identity=str(pending["tag_set_identity"]),
                page_size=limit,
                page_token=None if pending["page_token"] is None else str(pending["page_token"]),
            )
        except RiverhogError as exc:
            self._invalidate(exc, serial=_required_int(before, "serial"), tag_authority=True)
            raise
        expected = (
            int(pending["collection_id"]),
            int(pending["tag_revision"]),
            str(pending["tag_set_identity"]),
        )
        if (
            payload.get("collection_id"),
            payload.get("revision"),
            payload.get("tag_set_identity"),
        ) != expected:
            raise ValueError("collection tag page changed its bound authority")
        raw_tags = payload.get("tags")
        if not isinstance(raw_tags, list) or len(raw_tags) > limit:
            raise ValueError("collection tag page has invalid contents")
        tags = tuple(validate_collection_tag(tag) for tag in raw_tags if isinstance(tag, str))
        if len(tags) != len(raw_tags) or tuple(sorted(tags, key=lambda tag: tag.encode())) != tags:
            raise ValueError("collection tag page is not canonical")
        next_page_token = payload.get("next_page_token")
        if next_page_token is not None and (
            not isinstance(next_page_token, str) or not next_page_token
        ):
            raise ValueError("collection tag page continuation is invalid")

        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._require_serial(db, _required_int(before, "serial"))
                current = db.execute(
                    "SELECT * FROM catalog_replica_tag_sync "
                    "WHERE generation = ? AND collection_id = ?",
                    (pending["generation"], pending["collection_id"]),
                ).fetchone()
                if current is None or (
                    int(current["tag_revision"]),
                    str(current["tag_set_identity"]),
                    current["page_token"],
                ) != (expected[1], expected[2], pending["page_token"]):
                    raise RuntimeError("local collection tag authority changed")
                for tag in tags:
                    try:
                        db.execute(
                            "INSERT INTO catalog_replica_tags "
                            "(generation, collection_id, tag_revision, tag) VALUES (?, ?, ?, ?)",
                            (pending["generation"], expected[0], expected[1], tag),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ValueError("collection tag traversal repeated a member") from exc
                db.execute(
                    "UPDATE catalog_replica_tag_sync SET page_token = ?, complete = ? "
                    "WHERE generation = ? AND collection_id = ?",
                    (
                        next_page_token,
                        int(next_page_token is None),
                        pending["generation"],
                        expected[0],
                    ),
                )
                promote = (
                    next_page_token is None
                    and before["building_generation"] == pending["generation"]
                    and before["phase"] == "following"
                    and not self._has_pending_tags(db, str(pending["generation"]))
                )
                active = before["active_generation"]
                if promote and active is not None:
                    db.execute(
                        "UPDATE catalog_replica_generations SET obsolete = 1 WHERE id = ?",
                        (active,),
                    )
                db.execute(
                    """
                    UPDATE catalog_replica_state
                    SET serial = serial + 1, active_generation = ?, building_generation = ?,
                        usable = ?
                    """,
                    (
                        pending["generation"] if promote else active,
                        None if promote else before["building_generation"],
                        int(active is not None or promote),
                    ),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return self.status()

    @staticmethod
    def _has_pending_tags(db: sqlite3.Connection, generation: str) -> bool:
        return (
            db.execute(
                "SELECT 1 FROM catalog_replica_tag_sync "
                "WHERE generation = ? AND complete = 0 LIMIT 1",
                (generation,),
            ).fetchone()
            is not None
        )

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
        description = None if deleted else item.description  # type: ignore[union-attr]
        description_revision = None if deleted else item.description_revision  # type: ignore[union-attr]
        description_identity = None if deleted else item.description_identity  # type: ignore[union-attr]
        tag_revision = None if deleted else item.tag_revision  # type: ignore[union-attr]
        tag_set_identity = None if deleted else item.tag_set_identity  # type: ignore[union-attr]
        existing = db.execute(
            "SELECT * FROM catalog_replica_collections WHERE generation = ? AND collection_id = ?",
            (generation, item.collection_id),
        ).fetchone()
        if existing is not None and int(existing["revision"]) == revision:
            if (
                existing["archive_root_sha256"],
                existing["content_identity"],
                existing["description"],
                existing["description_revision"],
                existing["description_identity"],
                existing["tag_revision"],
                existing["tag_set_identity"],
                bool(existing["deleted"]),
            ) != (
                root,
                content,
                description,
                description_revision,
                description_identity,
                tag_revision,
                tag_set_identity,
                deleted,
            ):
                raise ValueError("equal catalog revisions have different contents")
            return
        db.execute(
            """
            INSERT INTO catalog_replica_collections (
                generation, collection_id, revision,
                archive_root_sha256, content_identity, description,
                description_revision, description_identity,
                tag_revision, tag_set_identity, deleted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (generation, collection_id) DO UPDATE SET
                revision = excluded.revision,
                archive_root_sha256 = excluded.archive_root_sha256,
                content_identity = excluded.content_identity,
                description = excluded.description,
                description_revision = excluded.description_revision,
                description_identity = excluded.description_identity,
                tag_revision = excluded.tag_revision,
                tag_set_identity = excluded.tag_set_identity,
                deleted = excluded.deleted
            WHERE excluded.revision > catalog_replica_collections.revision
            """,
            (
                generation,
                item.collection_id,
                revision,
                root,
                content,
                description,
                description_revision,
                description_identity,
                tag_revision,
                tag_set_identity,
                int(deleted),
            ),
        )
        if deleted:
            db.execute(
                "DELETE FROM catalog_replica_tag_sync WHERE generation = ? AND collection_id = ?",
                (generation, item.collection_id),
            )
        elif (
            existing is None
            or existing["tag_revision"] != tag_revision
            or existing["tag_set_identity"] != tag_set_identity
        ):
            db.execute(
                "DELETE FROM catalog_replica_tag_sync WHERE generation = ? AND collection_id = ?",
                (generation, item.collection_id),
            )
            db.execute(
                """
                INSERT INTO catalog_replica_tag_sync (
                    generation, collection_id, tag_revision, tag_set_identity,
                    page_token, complete
                ) VALUES (?, ?, ?, ?, NULL, 0)
                """,
                (generation, item.collection_id, tag_revision, tag_set_identity),
            )

    def _invalidate(
        self,
        error: RiverhogError,
        *,
        serial: int,
        tag_authority: bool = False,
    ) -> None:
        if error.code not in {
            "unauthorized",
            "forbidden",
            "catalog_sync_cursor_expired",
            "catalog_sync_history_expired",
            "catalog_sync_source_changed",
            "catalog_sync_view_changed",
        } and not (tag_authority and error.code == "precondition_failed"):
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
