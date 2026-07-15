from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
import typer
from typer.testing import CliRunner

from riverhog_cli import main as riverhog_main
from riverhog_cli.upload_progress import (
    CollectionUploadProgress,
    CollectionUploadProgressState,
    UploadProgressRenderer,
)
from riverhog_core.domain.errors import Conflict, NotFound, ServiceUnavailable

RUNNER = CliRunner()


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


def test_collection_upload_dry_run_hashes_manifest_without_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    (root / "clip.bin").write_bytes(b"video")

    def forbidden_client() -> object:
        raise AssertionError("dry-run must not create an API client")

    monkeypatch.setattr(riverhog_main, "client", forbidden_client)

    result = RUNNER.invoke(
        riverhog_main.app,
        [
            "collection",
            "upload",
            "My Trip",
            str(root),
            "--timestamp",
            "20260713T120000Z",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["status"] == "would_upload"
    assert payload["normalized_slug"] == "my-trip"
    assert payload["collection_id"] == "2026/20260713T120000Z__my-trip"
    assert payload["files_total"] == 1
    assert payload["bytes_total"] == 5
    assert payload["server_validation"] == "not_run"


def test_collection_upload_dry_run_human_output_marks_server_assigned_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    (root / "clip.bin").write_bytes(b"video")

    def forbidden_client() -> object:
        raise AssertionError("dry-run must not create an API client")

    monkeypatch.setattr(riverhog_main, "client", forbidden_client)

    result = RUNNER.invoke(
        riverhog_main.app,
        ["collection", "upload", "My Trip", str(root), "--dry-run"],
    )

    assert result.exit_code == 0
    assert "collection upload dry-run" in result.stdout
    assert "server-assigned" in result.stdout
    assert "None" not in result.stdout


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
            assert collection_id == "2025/20250101T000000Z__collection"
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
        "2025/20250101T000000Z__collection",
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
        "2025/20250101T000000Z__collection",
        source,
        {"path": "clip.bin", "bytes": len(content)},
    )

    assert uploaded == [b"abcd", b"efgh", b"ij"]


def test_upload_file_concurrency_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_CONCURRENCY", "8")

    assert riverhog_main._upload_file_concurrency() == 8


def test_upload_file_concurrency_rejects_invalid_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_CONCURRENCY", "0")

    with pytest.raises(typer.BadParameter):
        riverhog_main._upload_file_concurrency()


def test_upload_file_log_bytes_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_LOG_BYTES", "0")

    assert riverhog_main._upload_file_log_bytes() == 0


def test_upload_file_log_bytes_rejects_invalid_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_LOG_BYTES", "-1")

    with pytest.raises(typer.BadParameter):
        riverhog_main._upload_file_log_bytes()


def test_upload_collection_files_uses_worker_clients_when_concurrent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    (root / "a.txt").write_bytes(b"aaa")
    (root / "b.txt").write_bytes(b"bbbb")
    (root / "c.txt").write_bytes(b"ccccc")
    uploaded: list[tuple[str, int]] = []
    closed_clients: list[str] = []
    clients: list[object] = []

    class FakeConcurrentUploadApi:
        def __init__(self, name: str) -> None:
            self.name = name

        def create_or_resume_collection_file_upload(
            self,
            collection_id: str,
            path: str,
        ) -> dict[str, object]:
            assert collection_id == "2025/20250101T000000Z__collection"
            return {
                "upload_url": f"https://uploads.test/{path}",
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
            uploaded.append((self.name, len(content)))
            return {"offset": offset + len(content), "expires_at": None}

        def close(self) -> None:
            closed_clients.append(self.name)

    def api_factory() -> FakeConcurrentUploadApi:
        worker = FakeConcurrentUploadApi(f"worker-{len(clients)}")
        clients.append(worker)
        return worker

    monkeypatch.setattr(riverhog_main, "UPLOAD_CHUNK_BYTES", 100)
    progress: list[int] = []

    riverhog_main._upload_collection_files(
        FakeConcurrentUploadApi("primary"),  # type: ignore[arg-type]
        "2025/20250101T000000Z__collection",
        root,
        [
            {"path": "a.txt", "bytes": 3, "upload_state": "pending"},
            {"path": "b.txt", "bytes": 4, "upload_state": "pending"},
            {"path": "c.txt", "bytes": 5, "upload_state": "pending"},
        ],
        progress=progress.append,
        file_concurrency=2,
        api_factory=api_factory,  # type: ignore[arg-type]
    )

    assert len(clients) == 2
    assert sorted(size for _, size in uploaded) == [3, 4, 5]
    assert all(name.startswith("worker-") for name, _ in uploaded)
    assert sorted(progress) == [3, 4, 5]
    assert sorted(closed_clients) == ["worker-0", "worker-1"]


def test_upload_collection_files_reports_file_completion_after_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    (root / "a.txt").write_bytes(b"aaa")
    completed = 0

    class FakeApi:
        create_calls = 0
        append_calls = 0

        def create_or_resume_collection_file_upload(
            self,
            collection_id: str,
            path: str,
        ) -> dict[str, object]:
            self.create_calls += 1
            return {
                "upload_url": f"https://uploads.test/{path}",
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
            return {"offset": offset + len(content), "expires_at": None}

    def note_complete() -> None:
        nonlocal completed
        completed += 1

    fake_api = FakeApi()
    monkeypatch.setattr(riverhog_main, "UPLOAD_CHUNK_BYTES", 100)

    riverhog_main._upload_collection_files(
        fake_api,  # type: ignore[arg-type]
        "2025/20250101T000000Z__collection",
        root,
        [{"path": "a.txt", "bytes": 3, "upload_state": "pending"}],
        progress=lambda _delta: None,
        file_complete=note_complete,
        file_concurrency=1,
    )

    assert fake_api.create_calls == 1
    assert fake_api.append_calls == 1
    assert completed == 1


def test_upload_progress_throttles_accepted_byte_rendering() -> None:
    updates: list[CollectionUploadProgressState] = []
    now = 0.0

    class Renderer(UploadProgressRenderer):
        def update(
            self,
            state: CollectionUploadProgressState,
            *,
            force: bool = False,
        ) -> None:
            updates.append(state)

    def clock() -> float:
        return now

    progress = CollectionUploadProgress(
        collection_id="2025/20250101T000000Z__collection",
        files_total=1,
        bytes_total=100,
        renderer=Renderer(),
        interval_seconds=5.0,
        clock=clock,
    )

    with progress:
        progress.uploaded(10)
        progress.uploaded(10)
        now = 4.9
        progress.uploaded(10)
        now = 5.0
        progress.uploaded(10)
        progress.complete_file()

    assert [state.uploaded_bytes for state in updates] == [0, 40]
    assert progress.files_uploaded == 1


def test_upload_collection_via_session_registers_files_before_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    (root / "a.txt").write_bytes(b"aaa")
    (root / "b.txt").write_bytes(b"bbbb")
    events: list[tuple[str, str]] = []
    registered: dict[str, dict[str, object]] = {}
    uploaded_offsets: dict[str, int] = {}

    class FakeSessionApi:
        def create_or_resume_collection_upload_session(
            self,
            slug: str,
            *,
            ingest_source: str | None = None,
            upload_timestamp: str | None = None,
        ) -> dict[str, object]:
            assert slug == "photos 2024"
            assert ingest_source == str(root)
            assert upload_timestamp == "20250712T213200Z"
            events.append(("open", slug))
            return {"collection_id": "2025/20250712T213200Z__photos-2024", "state": "open"}

        def register_collection_upload_session_file(
            self,
            collection_id: str,
            file: dict[str, object],
        ) -> dict[str, object]:
            assert collection_id == "2025/20250712T213200Z__photos-2024"
            path = str(file["path"])
            events.append(("register", path))
            registered[path] = dict(file)
            uploaded_offsets.setdefault(path, 0)
            return {
                "collection_id": collection_id,
                "state": "open",
                "files": [
                    {
                        **payload,
                        "upload_state": "pending",
                        "uploaded_bytes": uploaded_offsets[str(payload["path"])],
                    }
                    for payload in registered.values()
                ],
            }

        def create_or_resume_collection_file_upload(
            self,
            collection_id: str,
            path: str,
        ) -> dict[str, object]:
            assert collection_id == "2025/20250712T213200Z__photos-2024"
            events.append(("resume", path))
            return {
                "upload_url": f"https://uploads.test/{path}",
                "offset": uploaded_offsets[path],
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
            path = upload_url.rsplit("/", 1)[-1]
            assert checksum_algorithm == "sha256"
            assert offset == uploaded_offsets[path]
            uploaded_offsets[path] = offset + len(content)
            events.append(("upload", path))
            return {"offset": uploaded_offsets[path], "expires_at": None}

        def get_collection_upload(self, collection_id: str) -> dict[str, object]:
            assert collection_id == "2025/20250712T213200Z__photos-2024"
            return {
                "collection_id": collection_id,
                "state": "open",
                "files": [
                    {
                        **payload,
                        "upload_state": "uploaded",
                        "uploaded_bytes": uploaded_offsets[str(payload["path"])],
                    }
                    for payload in registered.values()
                ],
            }

        def complete_collection_upload_session(self, collection_id: str) -> dict[str, object]:
            assert collection_id == "2025/20250712T213200Z__photos-2024"
            events.append(("complete", collection_id))
            return {"collection_id": collection_id, "state": "archiving", "files": []}

    monkeypatch.setattr(riverhog_main, "UPLOAD_CHUNK_BYTES", 100)

    payload = riverhog_main._upload_collection_via_session(
        FakeSessionApi(),  # type: ignore[arg-type]
        "photos 2024",
        root,
        ingest_source=str(root),
        upload_timestamp="20250712T213200Z",
        wait_mode="staged",
    )

    assert payload["state"] == "archiving"
    assert events == [
        ("open", "photos 2024"),
        ("register", "a.txt"),
        ("resume", "a.txt"),
        ("upload", "a.txt"),
        ("register", "b.txt"),
        ("resume", "b.txt"),
        ("upload", "b.txt"),
        ("complete", "2025/20250712T213200Z__photos-2024"),
    ]


def test_upload_collection_via_session_rejects_empty_source_before_opening(
    tmp_path: Path,
) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    class FakeSessionApi:
        def create_or_resume_collection_upload_session(self, *args: object) -> object:
            raise AssertionError("empty session should fail before opening on the server")

    with pytest.raises(typer.BadParameter, match="at least one file"):
        riverhog_main._upload_collection_via_session(
            FakeSessionApi(),  # type: ignore[arg-type]
            "empty",
            root,
            ingest_source=str(root),
            upload_timestamp=None,
            wait_mode="staged",
        )


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
        "2025/20250101T000000Z__collection",
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
        "2025/20250101T000000Z__collection",
        source,
        {"path": "clip.bin", "bytes": len(content)},
        progress=progress.append,
    )

    assert uploaded == [(0, b"abcde"), (5, b"fghij")]
    assert progress == [5, 5]


def test_upload_collection_file_resyncs_after_offset_conflict(
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
                raise Conflict("collection upload offset is 0, expected 5")
            return {"offset": offset + len(content), "expires_at": None}

    monkeypatch.setattr(riverhog_main, "UPLOAD_CHUNK_BYTES", 5)
    progress: list[int] = []

    riverhog_main._upload_collection_file(
        FakeApi(),  # type: ignore[arg-type]
        "2025/20250101T000000Z__collection",
        source,
        {"path": "clip.bin", "bytes": len(content)},
        progress=progress.append,
    )

    assert uploaded == [(0, b"abcde"), (5, b"fghij")]
    assert progress == [5, 5]


def test_upload_collection_file_stops_after_unresolved_offset_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "clip.bin"
    content = b"abcdefghij"
    source.write_bytes(content)
    uploaded: list[tuple[int, bytes]] = []

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
            uploaded.append((offset, content))
            raise Conflict("collection upload not found")

    monkeypatch.setattr(riverhog_main, "UPLOAD_CHUNK_BYTES", 5)

    with pytest.raises(RuntimeError, match="without advancing the offset"):
        riverhog_main._upload_collection_file(
            FakeApi(),  # type: ignore[arg-type]
            "2025/20250101T000000Z__collection",
            source,
            {"path": "clip.bin", "bytes": len(content)},
        )

    assert uploaded == [(0, b"abcde")]


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
        "2025/20250101T000000Z__collection",
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
        "2025/20250101T000000Z__collection",
        source,
        {"path": "clip.bin", "bytes": len(content)},
    )

    assert uploaded == [(0, content), (0, content)]


def test_create_or_resume_file_upload_keeps_retrying_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "https://riverhog.test/upload")
    response = httpx.Response(502, request=request, content=b"Bad Gateway")
    sleeps: list[float] = []

    class FakeApi:
        calls = 0

        def create_or_resume_collection_file_upload(
            self,
            collection_id: str,
            path: str,
        ) -> dict[str, object]:
            self.calls += 1
            if self.calls <= 6:
                raise httpx.HTTPStatusError("bad gateway", request=request, response=response)
            return {
                "upload_url": "https://uploads.test/clip.bin",
                "offset": 0,
                "checksum_algorithm": "sha256",
            }

    fake_api = FakeApi()
    monkeypatch.setattr(riverhog_main.time, "sleep", lambda seconds: sleeps.append(seconds))

    payload = riverhog_main._create_or_resume_collection_file_upload(
        fake_api,  # type: ignore[arg-type]
        "2025/20250101T000000Z__collection",
        "clip.bin",
    )

    assert fake_api.calls == 7
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]
    assert payload["offset"] == 0


def test_create_or_resume_collection_upload_retries_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "https://riverhog.test/v1/collection-uploads")
    response = httpx.Response(503, request=request, content=b"Service Unavailable")

    class FakeApi:
        calls = 0

        def create_or_resume_collection_upload(
            self,
            slug: str,
            files: list[riverhog_main.CollectionManifestEntry],
            *,
            ingest_source: str | None,
            upload_timestamp: str | None,
        ) -> dict[str, object]:
            self.calls += 1
            if self.calls <= 2:
                raise httpx.HTTPStatusError(
                    "service unavailable",
                    request=request,
                    response=response,
                )
            return {"collection_id": "2025/20250102T030405Z__docs", "files": []}

    fake_api = FakeApi()
    monkeypatch.setattr(riverhog_main.time, "sleep", lambda seconds: None)

    payload = riverhog_main._create_or_resume_collection_upload(
        fake_api,  # type: ignore[arg-type]
        "2025/20250102T030405Z__docs",
        [],
        ingest_source="/tmp/docs",
        upload_timestamp="20250101T000000Z",
    )

    assert fake_api.calls == 3
    assert payload["collection_id"] == "2025/20250102T030405Z__docs"


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
                "archive": {"state": "uploaded"},
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
        "2025/20250102T030405Z__docs",
        manifest,
    )

    assert state == "finalized"
    assert payload["state"] == "finalized"
    assert payload["collection"]["archive"]["state"] == "uploaded"  # type: ignore[index]


def test_wait_for_finalized_collection_supports_reattach_without_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeApi:
        def get_collection(self, collection_id: str) -> dict[str, object]:
            return {
                "id": collection_id,
                "files": 7,
                "bytes": 1234,
                "archive": {"state": "uploaded", "stored_bytes": 567},
            }

    monkeypatch.setattr(riverhog_main, "_upload_finalize_timeout_seconds", lambda: 1.0)

    payload, state = riverhog_main._wait_for_finalized_collection(
        FakeApi(),  # type: ignore[arg-type]
        "2026/20260101T000000Z__camera",
        None,
    )

    assert state == "finalized"
    assert payload["files_total"] == 7
    assert payload["hot_promoted_files"] == 7
    assert payload["archive_uploaded_bytes"] == 567


def test_wait_for_finalized_collection_retries_transient_poll_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest: list[riverhog_main.CollectionManifestEntry] = [
        {"path": "clip.bin", "bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}
    ]
    request = httpx.Request(
        "GET", "https://riverhog.test/v1/collections/2025/20250102T030405Z__docs"
    )
    response = httpx.Response(502, request=request, content=b"Bad Gateway")

    class FakeApi:
        collection_calls = 0

        def get_collection(self, collection_id: str) -> dict[str, object]:
            self.collection_calls += 1
            if self.collection_calls == 1:
                raise httpx.HTTPStatusError("bad gateway", request=request, response=response)
            return {
                "id": collection_id,
                "files": 1,
                "bytes": 3,
                "archive": {"state": "uploaded"},
            }

        def get_collection_upload(self, collection_id: str) -> dict[str, object]:
            raise AssertionError("transient collection polling should retry directly")

    monkeypatch.setattr(riverhog_main.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(riverhog_main, "_upload_finalize_poll_seconds", lambda: 0.001)
    monkeypatch.setattr(riverhog_main, "_upload_finalize_timeout_seconds", lambda: 1.0)

    payload, state = riverhog_main._wait_for_finalized_collection(
        FakeApi(),  # type: ignore[arg-type]
        "2025/20250102T030405Z__docs",
        manifest,
    )

    assert state == "finalized"
    assert payload["state"] == "finalized"


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
        "2025/20250102T030405Z__docs",
        [],
    )

    assert state == "failed"
    assert payload["archive_failure"] == "archive bucket unavailable"


def test_staged_collection_upload_payload_returns_archiving_session() -> None:
    manifest: list[riverhog_main.CollectionManifestEntry] = [
        {"path": "clip.bin", "bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}
    ]

    class FakeApi:
        def get_collection_upload(self, collection_id: str) -> dict[str, object]:
            return {
                "collection_id": collection_id,
                "state": "archiving",
                "files_total": 1,
                "files_uploaded": 1,
                "bytes_total": 3,
                "uploaded_bytes": 3,
                "files": [],
                "collection": None,
            }

        def get_collection(self, collection_id: str) -> dict[str, object]:
            raise AssertionError("staged mode should not need finalized collection")

    payload = riverhog_main._staged_collection_upload_payload(
        FakeApi(),  # type: ignore[arg-type]
        "2025/20250102T030405Z__docs",
        manifest,
    )

    assert payload["state"] == "archiving"
    assert payload["files_uploaded"] == 1


def test_normalize_upload_wait_mode_rejects_unknown_value() -> None:
    with pytest.raises(typer.BadParameter):
        riverhog_main._normalize_upload_wait_mode("forever")


def test_default_upload_wait_mode_is_finalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_UPLOAD_WAIT", raising=False)

    assert riverhog_main._default_upload_wait_mode() == "finalized"
