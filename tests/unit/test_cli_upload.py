from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from riverhog_age import iter_decrypt_age_scrypt
from riverhog_api_client.ingress import iter_ingress_upload_parts
from riverhog_cli import main as riverhog_main
from riverhog_cli.upload_progress import CollectionUploadProgress, format_upload_progress_line
from riverhog_core.ingress_crypto import (
    create_ingress_encryption,
    ingress_encryption_descriptor,
)
from riverhog_core.runtime_config import RuntimeConfig
from typer.testing import CliRunner

from tests.unit.db_helpers import sqlite_url

RUNNER = CliRunner()
COLLECTION_ID = "collection/20250101T000000Z"


def _descriptor(tmp_path: Path, *, content: bytes) -> dict[str, object]:
    config = RuntimeConfig(database_url=sqlite_url(tmp_path / "state.sqlite3"))
    encryption = create_ingress_encryption(
        config,
        collection_id=COLLECTION_ID,
        path="clip.bin",
        plaintext_bytes=len(content),
    )
    return ingress_encryption_descriptor(
        config,
        collection_id=COLLECTION_ID,
        path="clip.bin",
        plaintext_bytes=len(content),
        ciphertext_bytes=encryption.ciphertext_bytes,
        secret_envelope=encryption.secret_envelope,
        state_json=encryption.state_json,
    )


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

    assert riverhog_main._local_collection_manifest(root) == [
        {
            "path": "clip.bin",
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    ]


def test_collection_upload_dry_run_hashes_without_opening_an_api_client(
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
            "--archive-store",
            "b2",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["collection_id"] == "my-trip/20260713T120000Z"
    assert payload["archive_store"] == "b2"
    assert payload["files_preview"][0]["sha256"] == hashlib.sha256(b"video").hexdigest()


def test_upload_streams_client_encrypted_bytes_and_reports_plaintext_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = (b"encrypted ingress\n" * 10_000) + b"end"
    source = tmp_path / "clip.bin"
    source.write_bytes(content)
    descriptor = _descriptor(tmp_path, content=content)
    uploaded: list[bytes] = []
    progress: list[int] = []
    monkeypatch.setenv("RIVERHOG_UPLOAD_CHUNK_BYTES", "70000")

    class Api:
        def create_or_resume_collection_file_upload(
            self, collection_id: str, path: str
        ) -> dict[str, object]:
            assert (collection_id, path) == (COLLECTION_ID, "clip.bin")
            return {
                "upload_url": "https://uploads.test/opaque",
                "offset": sum(len(chunk) for chunk in uploaded),
                "length": descriptor["ciphertext_bytes"],
                "checksum_algorithm": "sha256",
                "encryption": descriptor,
            }

        def append_upload_chunk(self, _upload_url: str, **kwargs: Any) -> dict[str, object]:
            chunk = bytes(kwargs["content"])
            uploaded.append(chunk)
            return {"offset": int(kwargs["offset"]) + len(chunk), "expires_at": None}

    riverhog_main._upload_collection_file(
        Api(),  # type: ignore[arg-type]
        COLLECTION_ID,
        source,
        {"path": "clip.bin", "bytes": len(content)},
        progress=progress.append,
    )

    ciphertext = b"".join(uploaded)
    assert len(ciphertext) == descriptor["ciphertext_bytes"]
    assert sum(progress) == len(content)
    assert b"".join(iter_decrypt_age_scrypt([ciphertext], str(descriptor["passphrase"]))) == content


def test_resumed_upload_reports_existing_plaintext_without_counting_it_as_new_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = (b"resume encrypted ingress\n" * 10_000) + b"end"
    source = tmp_path / "clip.bin"
    source.write_bytes(content)
    descriptor = _descriptor(tmp_path, content=content)
    monkeypatch.setenv("RIVERHOG_UPLOAD_CHUNK_BYTES", "70000")
    parts = list(
        iter_ingress_upload_parts(
            source,
            descriptor,
            ciphertext_offset=0,
            target_part_bytes=70_000,
        )
    )
    assert len(parts) > 1
    resume_offset = parts[1].ciphertext_offset
    resumed: list[int] = []
    uploaded_ciphertext: list[int] = []
    uploaded_plaintext: list[int] = []

    class Api:
        def create_or_resume_collection_file_upload(
            self, _collection_id: str, _path: str
        ) -> dict[str, object]:
            return {
                "upload_url": "https://uploads.test/opaque",
                "offset": resume_offset,
                "length": descriptor["ciphertext_bytes"],
                "checksum_algorithm": "sha256",
                "encryption": descriptor,
            }

        def append_upload_chunk(self, _upload_url: str, **kwargs: Any) -> dict[str, object]:
            uploaded_ciphertext.append(len(bytes(kwargs["content"])))
            return {
                "offset": int(kwargs["offset"]) + uploaded_ciphertext[-1],
                "expires_at": None,
            }

    riverhog_main._upload_collection_file(
        Api(),  # type: ignore[arg-type]
        COLLECTION_ID,
        source,
        {"path": "clip.bin", "bytes": len(content)},
        progress=uploaded_plaintext.append,
        resumed=resumed.append,
    )

    assert resumed == [parts[1].plaintext_start]
    assert resumed[0] + sum(uploaded_plaintext) == len(content)


def test_ingress_resume_survives_a_transport_chunk_size_change(tmp_path: Path) -> None:
    content = (b"chunk-size-independent encrypted resume\n" * 20_000) + b"end"
    source = tmp_path / "clip.bin"
    source.write_bytes(content)
    descriptor = _descriptor(tmp_path, content=content)

    original_parts = list(
        iter_ingress_upload_parts(
            source,
            descriptor,
            ciphertext_offset=0,
            target_part_bytes=70_000,
        )
    )
    assert len(original_parts) > 2
    resume_offset = original_parts[2].ciphertext_offset

    larger_parts = list(
        iter_ingress_upload_parts(
            source,
            descriptor,
            ciphertext_offset=0,
            target_part_bytes=250_000,
        )
    )
    assert resume_offset not in {part.ciphertext_offset for part in larger_parts}

    resumed_parts = list(
        iter_ingress_upload_parts(
            source,
            descriptor,
            ciphertext_offset=resume_offset,
            target_part_bytes=250_000,
        )
    )

    ciphertext = b"".join(part.ciphertext for part in larger_parts)
    assert resumed_parts[0].ciphertext_offset == resume_offset
    assert b"".join(part.ciphertext for part in resumed_parts) == ciphertext[resume_offset:]


def test_upload_retries_the_same_deterministic_ciphertext_after_transport_loss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = b"retry-safe ciphertext"
    source = tmp_path / "clip.bin"
    source.write_bytes(content)
    descriptor = _descriptor(tmp_path, content=content)
    attempts: list[bytes] = []
    retry_delays: list[float] = []
    monkeypatch.setattr(riverhog_main.time, "sleep", retry_delays.append)

    class Api:
        def create_or_resume_collection_file_upload(
            self, _collection_id: str, _path: str
        ) -> dict[str, object]:
            return {
                "upload_url": "https://uploads.test/opaque",
                "offset": 0,
                "length": descriptor["ciphertext_bytes"],
                "checksum_algorithm": "sha256",
                "encryption": descriptor,
            }

        def append_upload_chunk(self, _upload_url: str, **kwargs: Any) -> dict[str, object]:
            chunk = bytes(kwargs["content"])
            attempts.append(chunk)
            if len(attempts) == 1:
                raise httpx.ReadError("lost response")
            return {"offset": len(chunk), "expires_at": None}

    riverhog_main._upload_collection_file(
        Api(),  # type: ignore[arg-type]
        COLLECTION_ID,
        source,
        {"path": "clip.bin", "bytes": len(content)},
    )

    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert retry_delays == [riverhog_main.UPLOAD_RESUME_RETRY_INITIAL_DELAY_SECONDS]


def test_upload_wait_mode_defaults_to_finalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_UPLOAD_WAIT", raising=False)

    assert riverhog_main._default_upload_wait_mode() == "finalized"
    assert riverhog_main._normalize_upload_wait_mode("staged") == "staged"


def test_upload_chunk_size_defaults_to_shared_tus_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_UPLOAD_CHUNK_BYTES", raising=False)

    assert riverhog_main._upload_chunk_bytes() == 64 * 1024 * 1024


@pytest.mark.parametrize(("concurrency", "expected_overlap"), [(1, 1), (2, 2)])
def test_incremental_upload_uses_bounded_persistent_workers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    concurrency: int,
    expected_overlap: int,
) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    for name in ("a.bin", "b.bin", "c.bin"):
        (root / name).write_bytes(name.encode())

    lock = threading.Lock()
    overlap = threading.Barrier(2) if concurrency > 1 else None
    second_finished = threading.Event()
    registered: dict[str, dict[str, object]] = {}
    completion_order: list[str] = []
    active = 0
    max_active = 0
    complete_calls = 0
    workers: list[Api] = []

    class Api:
        def __init__(self, *, worker: bool = False) -> None:
            self.worker = worker
            self.closed = False

        def create_or_resume_collection_upload_session(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"collection_id": COLLECTION_ID}

        def register_collection_upload_session_file(
            self,
            collection_id: str,
            entry: dict[str, object],
        ) -> dict[str, object]:
            assert collection_id == COLLECTION_ID
            with lock:
                registered[str(entry["path"])] = dict(entry)
            return {"files": [{**entry, "upload_state": "pending", "uploaded_bytes": 0}]}

        def get_collection_upload(self, collection_id: str) -> dict[str, object]:
            assert collection_id == COLLECTION_ID
            with lock:
                return {"files": list(registered.values())}

        def complete_collection_upload_session(self, collection_id: str) -> dict[str, object]:
            nonlocal complete_calls
            assert collection_id == COLLECTION_ID
            with lock:
                assert set(completion_order) == set(registered) == {"a.bin", "b.bin", "c.bin"}
                complete_calls += 1
            return {"collection_id": COLLECTION_ID, "state": "archiving", "files": []}

        def close(self) -> None:
            self.closed = True

    main_api = Api()

    def api_factory() -> Api:
        worker = Api(worker=True)
        workers.append(worker)
        return worker

    def upload_file(
        _api: Api,
        _collection_id: str,
        _source_path: Path,
        file_payload: dict[str, object],
        *,
        progress=None,  # type: ignore[no-untyped-def]
        resumed=None,  # type: ignore[no-untyped-def]
    ) -> None:
        del resumed
        nonlocal active, max_active
        path = str(file_payload["path"])
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            if overlap is not None and path in {"a.bin", "b.bin"}:
                overlap.wait(timeout=2)
                if path == "a.bin":
                    assert second_finished.wait(timeout=2)
                else:
                    second_finished.set()
            if progress is not None:
                progress(int(file_payload["bytes"]))
            with lock:
                completion_order.append(path)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(riverhog_main, "_upload_collection_file", upload_file)
    payload = riverhog_main._upload_collection_via_session(
        main_api,  # type: ignore[arg-type]
        "collection",
        root,
        ingest_source=str(root),
        upload_timestamp="20250101T000000Z",
        wait_mode="staged",
        file_concurrency=concurrency,
        api_factory=api_factory,  # type: ignore[arg-type]
    )

    assert payload["state"] == "archiving"
    assert max_active == expected_overlap
    assert complete_calls == 1
    if concurrency > 1:
        assert completion_order.index("b.bin") < completion_order.index("a.bin")
        assert len(workers) == concurrency
        assert all(worker.closed for worker in workers)
    else:
        assert workers == []
        assert not main_api.closed


def test_incremental_upload_failure_leaves_session_resumable_and_never_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    for name in ("a.bin", "b.bin", "c.bin"):
        (root / name).write_bytes(name.encode())

    lock = threading.Lock()
    registered: dict[str, dict[str, object]] = {}
    uploaded: set[str] = set()
    attempts: dict[str, int] = {}
    first_pair = threading.Barrier(2)
    fail = True
    complete_calls = 0

    class Api:
        def create_or_resume_collection_upload_session(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"collection_id": COLLECTION_ID}

        def register_collection_upload_session_file(
            self,
            _collection_id: str,
            entry: dict[str, object],
        ) -> dict[str, object]:
            with lock:
                registered[str(entry["path"])] = dict(entry)
            return {"files": [{**entry, "upload_state": "pending", "uploaded_bytes": 0}]}

        def get_collection_upload(self, _collection_id: str) -> dict[str, object]:
            with lock:
                return {"files": list(registered.values())}

        def complete_collection_upload_session(self, _collection_id: str) -> dict[str, object]:
            nonlocal complete_calls
            with lock:
                assert uploaded == set(registered) == {"a.bin", "b.bin", "c.bin"}
                complete_calls += 1
            return {"collection_id": COLLECTION_ID, "state": "archiving", "files": []}

        def close(self) -> None:
            return

    api = Api()

    def upload_file(
        _api: Api,
        _collection_id: str,
        _source_path: Path,
        file_payload: dict[str, object],
        **_kwargs: object,
    ) -> None:
        path = str(file_payload["path"])
        with lock:
            attempts[path] = attempts.get(path, 0) + 1
            already_uploaded = path in uploaded
        if already_uploaded:
            return
        if fail and path in {"a.bin", "b.bin"}:
            first_pair.wait(timeout=2)
            if path == "a.bin":
                raise RuntimeError("worker failed")
        with lock:
            uploaded.add(path)

    monkeypatch.setattr(riverhog_main, "_upload_collection_file", upload_file)
    with pytest.raises(RuntimeError, match="worker failed"):
        riverhog_main._upload_collection_via_session(
            api,  # type: ignore[arg-type]
            "collection",
            root,
            ingest_source=str(root),
            upload_timestamp="20250101T000000Z",
            wait_mode="staged",
            file_concurrency=2,
            api_factory=lambda: Api(),  # type: ignore[arg-type]
        )

    assert complete_calls == 0
    assert "b.bin" in uploaded
    fail = False
    payload = riverhog_main._upload_collection_via_session(
        api,  # type: ignore[arg-type]
        "collection",
        root,
        ingest_source=str(root),
        upload_timestamp="20250101T000000Z",
        wait_mode="staged",
        file_concurrency=2,
        api_factory=lambda: Api(),  # type: ignore[arg-type]
    )

    assert payload["state"] == "archiving"
    assert complete_calls == 1
    assert uploaded == {"a.bin", "b.bin", "c.bin"}
    assert attempts["b.bin"] == 2


def test_incremental_progress_does_not_report_a_final_percentage_during_discovery() -> None:
    progress = CollectionUploadProgress(
        collection_id=COLLECTION_ID,
        files_total=0,
        bytes_total=0,
        files_hashed=0,
        files_registered=0,
        file_concurrency=2,
        discovery_complete=False,
    )
    progress.set_totals(files_total=1, bytes_total=10)
    progress.hashed_file()
    progress.registered_file()
    progress.uploaded(10)
    progress.complete_file()

    open_line = format_upload_progress_line(progress._state())
    assert "final total open" in open_line
    assert "100.0%" not in open_line
    assert "pipeline=1 discovered/1 hashed/1 registered/1 uploaded" in open_line

    progress.finish_discovery()
    completed_line = format_upload_progress_line(progress._state())
    assert "final total open" not in completed_line
    assert "100.0%" in completed_line


def test_upload_concurrency_and_window_defaults_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_UPLOAD_FILE_CONCURRENCY", raising=False)
    monkeypatch.delenv("RIVERHOG_UPLOAD_FILE_WINDOW", raising=False)

    concurrency = riverhog_main._upload_file_concurrency()
    assert concurrency == 8
    assert riverhog_main._upload_file_window(concurrency) == 16

    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_CONCURRENCY", "65")
    with pytest.raises(Exception, match="between 1 and 64"):
        riverhog_main._upload_file_concurrency()
