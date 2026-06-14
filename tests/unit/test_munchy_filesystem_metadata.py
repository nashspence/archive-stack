from __future__ import annotations

import subprocess
from pathlib import Path

from munchy import filesystem_metadata


def test_collect_filesystem_metadata_uses_xattr_cli_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    monkeypatch.delattr(filesystem_metadata.os, "listxattr", raising=False)
    monkeypatch.delattr(filesystem_metadata.os, "getxattr", raising=False)
    monkeypatch.setattr(filesystem_metadata.shutil, "which", lambda name: "/usr/bin/xattr")

    def fake_run(
        args: list[str],
        *,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        assert capture_output is True
        assert check is False
        if args == ["xattr", str(source)]:
            return subprocess.CompletedProcess(args, 0, stdout=b"com.example.test\n", stderr=b"")
        if args == ["xattr", "-px", "com.example.test", str(source)]:
            return subprocess.CompletedProcess(args, 0, stdout=b"76 61 6c 75 65\n", stderr=b"")
        raise AssertionError(args)

    monkeypatch.setattr(filesystem_metadata.subprocess, "run", fake_run)

    metadata = filesystem_metadata.collect_filesystem_metadata(source)

    xattrs = metadata["extended_attributes"]
    assert xattrs["available"] is True
    assert xattrs["items"] == [
        {
            "name": "com.example.test",
            "bytes": 5,
            "sha256": "cd42404d52ad55ccfa9aca4adc828aa5800ad9d385a0671fbcbf724118320619",
            "value_base64": "dmFsdWU=",
        }
    ]
