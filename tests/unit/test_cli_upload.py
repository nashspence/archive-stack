from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from riverhog_cli import main as riverhog_main
from riverhog_core.domain.errors import NotFound, ServiceUnavailable


def test_local_collection_manifest_streams_file_hashes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    source = root / "clip.bin"
    content = b"0123456789abcdef"
    source.write_bytes(content)

    def forbidden_read_bytes(self: Path) -> bytes:
        raise AssertionError(f"read_bytes should not be used for upload manifests: {self}")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)

    manifest = riverhog_main._local_collection_manifest(root)

    assert manifest == [
        {
            "path": "clip.bin",
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    ]


def test_upload_collection_file_streams_chunks_from_resume_offset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "clip.bin"
    content = b"0123456789abcdef"
    source.write_bytes(content)
    uploaded: list[tuple[int, bytes]] = []

    class FakeApi:
        def create_or_resume_collection_file_upload(
            self,
            collection_id: str,
            path: str,
        ) -> dict[str, object]:
            assert collection_id == "2025/collection"
            assert path == "clip.bin"
            return {
                "upload_url": "https://uploads.test/clip.bin",
                "offset": 6,
                "checksum_algorithm": "sha256",
            }

        def append_upload_chunk(
            self,
            upload_url: str,
            *,
            offset: int,
            checksum_algorithm: str,
            content: bytes,
        ) -> dict[str, object]:
            assert upload_url == "https://uploads.test/clip.bin"
            assert checksum_algorithm == "sha256"
            uploaded.append((offset, content))
            return {"offset": offset + len(content), "expires_at": None}

    monkeypatch.setattr(riverhog_main, "UPLOAD_CHUNK_BYTES", 5)

    riverhog_main._upload_collection_file(
        FakeApi(),  # type: ignore[arg-type]
        "2025/collection",
        source,
        {"path": "clip.bin", "bytes": len(content)},
    )

    assert uploaded == [(6, b"6789a"), (11, b"bcdef")]


def test_upload_collection_file_honors_chunk_size_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "clip.bin"
    content = b"abcdefghij"
    source.write_bytes(content)
    uploaded: list[bytes] = []

    class FakeApi:
        def create_or_resume_collection_file_upload(
            self,
            collection_id: str,
            path: str,
        ) -> dict[str, object]:
            return {
                "upload_url": "https://uploads.test/clip.bin",
                "offset": 0,
                "checksum_algorithm": "sha256",
            }

        def append_upload_chunk(
            self,
            upload_url: str,
            *,
            offset: int,
            checksum_algorithm: str,
            content: bytes,
        ) -> dict[str, object]:
            uploaded.append(content)
            return {"offset": offset + len(content), "expires_at": None}

    monkeypatch.setenv("RIVERHOG_UPLOAD_CHUNK_BYTES", "4")

    riverhog_main._upload_collection_file(
        FakeApi(),  # type: ignore[arg-type]
        "2025/collection",
        source,
        {"path": "clip.bin", "bytes": len(content)},
    )

    assert uploaded == [b"abcd", b"efgh", b"ij"]


def test_upload_collection_file_retries_after_interrupted_chunk_with_unchanged_offset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "clip.bin"
    content = b"abcdefghij"
    source.write_bytes(content)
    uploaded: list[tuple[int, bytes]] = []

    class FakeApi:
        resume_calls = 0
        append_calls = 0

        def create_or_resume_collection_file_upload(
            self,
            collection_id: str,
            path: str,
        ) -> dict[str, object]:
            self.resume_calls += 1
            return {
                "upload_url": "https://uploads.test/clip.bin",
                "offset": 0,
                "checksum_algorithm": "sha256",
            }

        def append_upload_chunk(
            self,
            upload_url: str,
            *,
            offset: int,
            checksum_algorithm: str,
            content: bytes,
        ) -> dict[str, object]:
            self.append_calls += 1
            uploaded.append((offset, content))
            if self.append_calls == 1:
                raise httpx.ReadTimeout("temporary stall")
            return {"offset": offset + len(content), "expires_at": None}

    monkeypatch.setattr(riverhog_main, "UPLOAD_CHUNK_BYTES", 20)
    progress: list[int] = []

    riverhog_main._upload_collection_file(
        FakeApi(),  # type: ignore[arg-type]
        "2025/collection",
        source,
        {"path": "clip.bin", "bytes": len(content)},
        progress=progress.append,
    )

    assert uploaded == [(0, content), (0, content)]
    assert progress == [len(content)]


def test_upload_collection_file_continues_after_lost_chunk_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "clip.bin"
    content = b"abcdefghij"
    source.write_bytes(content)
    uploaded: list[tuple[int, bytes]] = []

    class FakeApi:
        resume_calls = 0
        append_calls = 0

        def create_or_resume_collection_file_upload(
            self,
            collection_id: str,
            path: str,
        ) -> dict[str, object]:
            self.resume_calls += 1
            offset = 0 if self.resume_calls == 1 else 5
            return {
                "upload_url": "https://uploads.test/clip.bin",
                "offset": offset,
                "checksum_algorithm": "sha256",
            }

        def append_upload_chunk(
            self,
            upload_url: str,
            *,
            offset: int,
            checksum_algorithm: str,
            content: bytes,
        ) -> dict[str, object]:
            self.append_calls += 1
            uploaded.append((offset, content))
            if self.append_calls == 1:
                raise httpx.ReadTimeout("response was lost")
            return {"offset": offset + len(content), "expires_at": None}

    monkeypatch.setattr(riverhog_main, "UPLOAD_CHUNK_BYTES", 5)
    progress: list[int] = []

    riverhog_main._upload_collection_file(
        FakeApi(),  # type: ignore[arg-type]
        "2025/collection",
        source,
        {"path": "clip.bin", "bytes": len(content)},
        progress=progress.append,
    )

    assert uploaded == [(0, b"abcde"), (5, b"fghij")]
    assert progress == [5, 5]


def test_upload_collection_file_retries_after_transient_http_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "clip.bin"
    content = b"abcdefghij"
    source.write_bytes(content)
    uploaded: list[tuple[int, bytes]] = []

    class FakeApi:
        append_calls = 0

        def create_or_resume_collection_file_upload(
            self,
            collection_id: str,
            path: str,
        ) -> dict[str, object]:
            return {
                "upload_url": "https://uploads.test/clip.bin",
                "offset": 0,
                "checksum_algorithm": "sha256",
            }

        def append_upload_chunk(
            self,
            upload_url: str,
            *,
            offset: int,
            checksum_algorithm: str,
            content: bytes,
        ) -> dict[str, object]:
            self.append_calls += 1
            uploaded.append((offset, content))
            if self.append_calls == 1:
                request = httpx.Request("PATCH", upload_url)
                response = httpx.Response(503, request=request, content=b"")
                raise httpx.HTTPStatusError(
                    "service unavailable",
                    request=request,
                    response=response,
                )
            return {"offset": offset + len(content), "expires_at": None}

    monkeypatch.setattr(riverhog_main, "UPLOAD_CHUNK_BYTES", 20)

    riverhog_main._upload_collection_file(
        FakeApi(),  # type: ignore[arg-type]
        "2025/collection",
        source,
        {"path": "clip.bin", "bytes": len(content)},
    )

    assert uploaded == [(0, content), (0, content)]


def test_upload_collection_file_retries_after_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "clip.bin"
    content = b"abcdefghij"
    source.write_bytes(content)
    uploaded: list[tuple[int, bytes]] = []

    class FakeApi:
        append_calls = 0

        def create_or_resume_collection_file_upload(
            self,
            collection_id: str,
            path: str,
        ) -> dict[str, object]:
            return {
                "upload_url": "https://uploads.test/clip.bin",
                "offset": 0,
                "checksum_algorithm": "sha256",
            }

        def append_upload_chunk(
            self,
            upload_url: str,
            *,
            offset: int,
            checksum_algorithm: str,
            content: bytes,
        ) -> dict[str, object]:
            self.append_calls += 1
            uploaded.append((offset, content))
            if self.append_calls == 1:
                raise ServiceUnavailable("temporary upload backend stall")
            return {"offset": offset + len(content), "expires_at": None}

    monkeypatch.setattr(riverhog_main, "UPLOAD_CHUNK_BYTES", 20)

    riverhog_main._upload_collection_file(
        FakeApi(),  # type: ignore[arg-type]
        "2025/collection",
        source,
        {"path": "clip.bin", "bytes": len(content)},
    )

    assert uploaded == [(0, content), (0, content)]


def test_wait_for_finalized_collection_waits_through_archiving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest: list[riverhog_main.CollectionManifestEntry] = [
        {"path": "clip.bin", "bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}
    ]

    class FakeApi:
        collection_calls = 0

        def get_collection(self, collection_id: str) -> dict[str, object]:
            self.collection_calls += 1
            if self.collection_calls == 1:
                raise NotFound("not finalized yet")
            return {
                "id": collection_id,
                "files": 1,
                "bytes": 3,
                "glacier": {"state": "uploaded"},
            }

        def get_collection_upload(self, collection_id: str) -> dict[str, object]:
            return {
                "collection_id": collection_id,
                "state": "archiving",
                "files_total": 1,
                "files_uploaded": 1,
                "bytes_total": 3,
                "uploaded_bytes": 3,
                "files": [],
            }

    monkeypatch.setattr(riverhog_main.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(riverhog_main, "_upload_finalize_poll_seconds", lambda: 0.001)
    monkeypatch.setattr(riverhog_main, "_upload_finalize_timeout_seconds", lambda: 1.0)

    payload, state = riverhog_main._wait_for_finalized_collection(
        FakeApi(),  # type: ignore[arg-type]
        "2025/docs",
        manifest,
    )

    assert state == "finalized"
    assert payload["state"] == "finalized"
    assert payload["collection"]["glacier"]["state"] == "uploaded"  # type: ignore[index]


def test_wait_for_finalized_collection_returns_failed_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeApi:
        def get_collection(self, collection_id: str) -> dict[str, object]:
            raise NotFound("not finalized")

        def get_collection_upload(self, collection_id: str) -> dict[str, object]:
            return {
                "collection_id": collection_id,
                "state": "failed",
                "archive_failure": "archive bucket unavailable",
                "files": [],
            }

    monkeypatch.setattr(riverhog_main, "_upload_finalize_timeout_seconds", lambda: 1.0)

    payload, state = riverhog_main._wait_for_finalized_collection(
        FakeApi(),  # type: ignore[arg-type]
        "2025/docs",
        [],
    )

    assert state == "failed"
    assert payload["archive_failure"] == "archive bucket unavailable"
