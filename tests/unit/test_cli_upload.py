from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from riverhog_cli import main as riverhog_main


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
