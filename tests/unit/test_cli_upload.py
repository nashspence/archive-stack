from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from riverhog_age import iter_decrypt_age_scrypt
from riverhog_cli import main as riverhog_main
from riverhog_core.ingress_client import iter_ingress_upload_parts
from riverhog_core.ingress_crypto import (
    create_ingress_encryption,
    ingress_encryption_descriptor,
)
from riverhog_core.runtime_config import RuntimeConfig
from tests.unit.db_helpers import sqlite_url

RUNNER = CliRunner()
COLLECTION_ID = "2025/20250101T000000Z__collection"


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
    assert payload["collection_id"] == "2026/20260713T120000Z__my-trip"
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


def test_upload_retries_the_same_deterministic_ciphertext_after_transport_loss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = b"retry-safe ciphertext"
    source = tmp_path / "clip.bin"
    source.write_bytes(content)
    descriptor = _descriptor(tmp_path, content=content)
    attempts: list[bytes] = []

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


def test_upload_wait_mode_defaults_to_finalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_UPLOAD_WAIT", raising=False)

    assert riverhog_main._default_upload_wait_mode() == "finalized"
    assert riverhog_main._normalize_upload_wait_mode("staged") == "staged"


def test_upload_chunk_size_defaults_to_shared_tus_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_UPLOAD_CHUNK_BYTES", raising=False)

    assert riverhog_main._upload_chunk_bytes() == 64 * 1024 * 1024
