from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from arc_core.imports.tar_stream import QueueReader, extract_tar_stream, safe_target



def _tar_bytes() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        data = b"hello world"
        info = tarfile.TarInfo("fetch/example.txt")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()



def test_safe_target_rejects_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        safe_target(tmp_path, "../evil.txt")



def test_extract_tar_stream_writes_regular_file(tmp_path: Path) -> None:
    reader = QueueReader()
    reader.feed(_tar_bytes())
    reader.finish()

    result = extract_tar_stream(reader, tmp_path, allow_member=lambda name: name.startswith("fetch/"))

    assert result.files == 1
    assert (tmp_path / "fetch" / "example.txt").read_text() == "hello world"
    assert result.manifest_path.exists()
