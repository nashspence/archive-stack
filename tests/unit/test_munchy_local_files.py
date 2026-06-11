from __future__ import annotations

from pathlib import Path

from munchy.local_files import (
    FileHashCache,
    LocalFileCandidate,
    hash_local_file_candidates,
)


def test_hash_local_file_candidates_reuses_cached_hash(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    stat = source.stat()
    candidate = LocalFileCandidate(
        source=source,
        rel_path="camera/clip.mp4",
        bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )
    cache_path = tmp_path / "hashes.sqlite3"

    with FileHashCache(cache_path) as cache:
        first = hash_local_file_candidates([candidate], cache=cache)

    with FileHashCache(cache_path) as cache:
        second = hash_local_file_candidates([candidate], cache=cache)

    assert first.files[0].sha256 == (
        "0cab1c9617404faf2b24e221e189ca5945813e14d3f766345b09ca13bbe28ffc"
    )
    assert first.hash_cache.hits == 0
    assert first.hash_cache.misses == 1
    assert first.hash_cache.writes == 1
    assert second.hash_cache.hits == 1
    assert second.hash_cache.misses == 0
    assert second.hash_cache.writes == 0


def test_hash_local_file_candidates_reports_local_hash_progress(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    stat = source.stat()
    updates: list[dict[str, object]] = []

    class Renderer:
        def update(self, job: dict[str, object], *, force: bool = False) -> None:
            updates.append(job)

    discovery = hash_local_file_candidates(
        [
            LocalFileCandidate(
                source=source,
                rel_path="camera/clip.mp4",
                bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        ],
        cache=None,
        renderer=Renderer(),
    )

    assert len(discovery.files) == 1
    progress = updates[-1]["local_progress"]["hash"]  # type: ignore[index]
    assert progress["files_done"] == 1
    assert progress["files_total"] == 1
    assert progress["bytes_done"] == stat.st_size
    assert progress["completed"] is True


def test_file_hash_cache_commits_each_write_for_parallel_runs(tmp_path: Path) -> None:
    hash_path = tmp_path / "hashes.sqlite3"
    with FileHashCache(hash_path) as first, FileHashCache(hash_path) as second:
        first.put(tmp_path / "a.mp4", 100, 1, "a" * 64)
        assert first.conn is not None
        assert not first.conn.in_transaction
        assert second.get(tmp_path / "a.mp4", 100, 1) == "a" * 64

        second.put(tmp_path / "b.mp4", 200, 2, "b" * 64)
        assert second.conn is not None
        assert not second.conn.in_transaction
        assert first.get(tmp_path / "b.mp4", 200, 2) == "b" * 64
