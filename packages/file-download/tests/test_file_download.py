from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from file_download import (
    DownloadDestinationExists,
    DownloadIntegrityError,
    FileDownloadError,
    verified_download,
)


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def test_verified_download_installs_an_exact_stream(tmp_path: Path) -> None:
    output = tmp_path / "bundle.tar.gz"
    content = b"diagnostic bundle"
    progress: list[tuple[int, int]] = []

    receipt = verified_download(
        (content[:4], content[4:]),
        output=output,
        expected_bytes=len(content),
        expected_sha256=sha256(content),
        progress=lambda current, total: progress.append((current, total)),
    )

    assert output.read_bytes() == content
    assert receipt.output == output
    assert receipt.bytes == len(content)
    assert receipt.sha256 == sha256(content)
    assert progress[-1] == (len(content), len(content))
    assert not list(tmp_path.glob(".*.part"))


def test_verified_download_refuses_an_existing_destination(tmp_path: Path) -> None:
    output = tmp_path / "bundle.tar.gz"
    output.write_bytes(b"original")

    with pytest.raises(DownloadDestinationExists, match="already exists"):
        verified_download(
            (b"replacement",),
            output=output,
            expected_bytes=11,
            expected_sha256=sha256(b"replacement"),
        )

    assert output.read_bytes() == b"original"


def test_verified_download_replaces_only_after_verification(tmp_path: Path) -> None:
    output = tmp_path / "bundle.tar.gz"
    output.write_bytes(b"original")

    receipt = verified_download(
        (b"replacement",),
        output=output,
        expected_bytes=11,
        expected_sha256=sha256(b"replacement"),
        overwrite=True,
    )

    assert output.read_bytes() == b"replacement"
    assert receipt.bytes == 11


@pytest.mark.parametrize(
    ("expected_bytes", "expected_sha256"),
    [
        (12, sha256(b"replacement")),
        (11, "0" * 64),
    ],
)
def test_verified_download_preserves_destination_on_integrity_failure(
    tmp_path: Path,
    expected_bytes: int,
    expected_sha256: str,
) -> None:
    output = tmp_path / "bundle.tar.gz"
    output.write_bytes(b"original")

    with pytest.raises(DownloadIntegrityError):
        verified_download(
            (b"replacement",),
            output=output,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
            overwrite=True,
        )

    assert output.read_bytes() == b"original"
    assert not list(tmp_path.glob(".*.part"))


def test_verified_download_requires_an_existing_parent(tmp_path: Path) -> None:
    with pytest.raises(FileDownloadError, match="parent directory does not exist"):
        verified_download(
            (b"content",),
            output=tmp_path / "missing" / "bundle.tar.gz",
            expected_bytes=7,
            expected_sha256=sha256(b"content"),
        )


def test_verified_download_cleans_partial_file_when_stream_fails(tmp_path: Path) -> None:
    def broken_stream():  # type: ignore[no-untyped-def]
        yield b"partial"
        raise OSError("connection lost")

    output = tmp_path / "bundle.tar.gz"
    with pytest.raises(OSError, match="connection lost"):
        verified_download(
            broken_stream(),
            output=output,
            expected_bytes=20,
            expected_sha256="0" * 64,
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".*.part"))
