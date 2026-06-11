from __future__ import annotations

import hashlib
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

HASH_CHUNK_BYTES = 8 * 1024 * 1024
SQLITE_CACHE_TIMEOUT_SECONDS = 60.0
SQLITE_CACHE_BUSY_TIMEOUT_MS = int(SQLITE_CACHE_TIMEOUT_SECONDS * 1000)


class ProgressRenderer(Protocol):
    def update(self, update: dict[str, Any], *, force: bool = False) -> None: ...


@dataclass
class HashCacheStats:
    path: str | None = None
    hits: int = 0
    misses: int = 0
    writes: int = 0


@dataclass(frozen=True)
class LocalFileCandidate:
    source: Path
    rel_path: str
    bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class HashedLocalFile:
    source: Path
    rel_path: str
    bytes: int
    sha256: str


@dataclass
class LocalFileDiscovery:
    files: list[HashedLocalFile]
    hash_cache: HashCacheStats


class FileHashCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stats = HashCacheStats(path=str(path))
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> FileHashCache:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=SQLITE_CACHE_TIMEOUT_SECONDS)
        self.conn.execute(f"PRAGMA busy_timeout={SQLITE_CACHE_BUSY_TIMEOUT_MS}")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_hashes (
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (path, size, mtime_ns)
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS file_hashes_path_idx ON file_hashes(path)")
        self.conn.commit()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.conn is not None:
            self.conn.commit()
            self.conn.close()
            self.conn = None

    def get(self, path: Path, size: int, mtime_ns: int) -> str | None:
        if self.conn is None:
            return None
        try:
            row = self.conn.execute(
                "SELECT sha256 FROM file_hashes WHERE path = ? AND size = ? AND mtime_ns = ?",
                (str(path), size, mtime_ns),
            ).fetchone()
        except sqlite3.OperationalError:
            self.stats.misses += 1
            return None
        if row is None:
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return str(row[0])

    def put(self, path: Path, size: int, mtime_ns: int, sha256: str) -> None:
        if self.conn is None:
            return
        try:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO file_hashes (path, size, mtime_ns, sha256, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(path), size, mtime_ns, sha256, datetime.now(UTC).isoformat()),
            )
            self.conn.execute(
                "DELETE FROM file_hashes WHERE path = ? AND NOT (size = ? AND mtime_ns = ?)",
                (str(path), size, mtime_ns),
            )
            self.conn.commit()
        except sqlite3.OperationalError:
            self.conn.rollback()
            return
        self.stats.writes += 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file_cached(
    path: Path,
    *,
    size: int,
    mtime_ns: int,
    cache: FileHashCache | None,
) -> str:
    cached = cache.get(path, size, mtime_ns) if cache is not None else None
    if cached is not None:
        return cached
    digest = sha256_file(path)
    if cache is not None:
        cache.put(path, size, mtime_ns, digest)
    return digest


class HashProgress:
    def __init__(
        self,
        total_files: int,
        total_bytes: int,
        *,
        renderer: ProgressRenderer | None = None,
    ) -> None:
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.completed_files = 0
        self.completed_bytes = 0
        self.started_at = time.monotonic()
        self.last_printed_at = self.started_at
        self.printed_any = False
        self.renderer = renderer

    def mark_complete(self, item_bytes: int, stats: HashCacheStats) -> None:
        self.completed_files += 1
        self.completed_bytes += item_bytes
        now = time.monotonic()
        elapsed = max(now - self.started_at, 0.001)
        is_final = self.completed_files == self.total_files
        pct = (self.completed_bytes / self.total_bytes * 100.0) if self.total_bytes else 100.0
        rate = int(self.completed_bytes / elapsed)
        if self.renderer is not None:
            if not is_final and now - self.last_printed_at < 0.25:
                return
            self.renderer.update(
                {
                    "local_progress": {
                        "hash": {
                            "stage": "hash",
                            "label": "hash",
                            "files_done": self.completed_files,
                            "files_total": self.total_files,
                            "bytes_done": self.completed_bytes,
                            "bytes_total": self.total_bytes,
                            "percent_bytes": round(pct, 2),
                            "rate_bytes_per_second": rate,
                            "rate_label": "logical",
                            "elapsed_seconds": round(elapsed, 3),
                            "cache_hits": stats.hits,
                            "cache_misses": stats.misses,
                            "cache_writes": stats.writes,
                            "completed": is_final,
                        }
                    }
                },
                force=is_final,
            )
            self.last_printed_at = now
            return
        if not is_final and now - self.last_printed_at < 15:
            return
        if is_final and not self.printed_any and elapsed < 2:
            return
        rate_mib = rate / 1024**2
        print(
            (
                "hash progress: "
                f"{self.completed_files}/{self.total_files} files, "
                f"{self.completed_bytes / 1024**3:.2f}/{self.total_bytes / 1024**3:.2f} GiB, "
                f"{pct:.2f}%, {rate_mib:.1f} MiB/s logical, "
                f"cache hits {stats.hits}, misses {stats.misses}"
            ),
            file=sys.stderr,
        )
        self.printed_any = True
        self.last_printed_at = now


def hash_local_file_candidates(
    candidates: list[LocalFileCandidate],
    *,
    cache: FileHashCache | None,
    cache_enabled: bool = True,
    renderer: ProgressRenderer | None = None,
) -> LocalFileDiscovery:
    total_bytes = sum(candidate.bytes for candidate in candidates)
    stats = cache.stats if cache is not None else HashCacheStats()
    if cache is None and not cache_enabled:
        stats.path = None
    files: list[HashedLocalFile] = []
    progress = HashProgress(len(candidates), total_bytes, renderer=renderer)
    for candidate in candidates:
        sha256 = sha256_file_cached(
            candidate.source,
            size=candidate.bytes,
            mtime_ns=candidate.mtime_ns,
            cache=cache,
        )
        files.append(
            HashedLocalFile(
                source=candidate.source,
                rel_path=candidate.rel_path,
                bytes=candidate.bytes,
                sha256=sha256,
            )
        )
        progress.mark_complete(candidate.bytes, stats)
    return LocalFileDiscovery(files=files, hash_cache=stats)
