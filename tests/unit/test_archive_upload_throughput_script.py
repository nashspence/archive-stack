from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "archive_upload_throughput.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("archive_upload_throughput", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_timed_source_reads_only_the_requested_range(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "source.bin"
    path.write_bytes(b"0123456789")
    source = module._TimedFileSource(path)

    assert b"".join(source.chunks_range(4, 3)) == b"456"
    assert source.bytes_read == 3
    assert source.read_seconds >= 0


def test_probe_cleanup_is_scoped_and_verified() -> None:
    module = _module()

    class FakeClient:
        uploads = [{"Key": "archive/archives/probe/data.age", "UploadId": "u1"}]
        objects = {"archive/archives/probe/data.age", "unrelated/object"}

        def list_multipart_uploads(self, **_kwargs: object) -> dict[str, object]:
            return {"IsTruncated": False, "Uploads": list(self.uploads)}

        def abort_multipart_upload(self, **kwargs: object) -> None:
            assert kwargs["Key"] == "archive/archives/probe/data.age"
            self.uploads.clear()

        def list_objects_v2(self, *, Bucket: str, Prefix: str) -> dict[str, Any]:
            _ = Bucket
            return {"Contents": [{"Key": key} for key in self.objects if key.startswith(Prefix)]}

        def delete_object(self, *, Bucket: str, Key: str) -> None:
            _ = Bucket
            self.objects.remove(Key)

    client = FakeClient()
    module._cleanup_probe(client, bucket="bucket", prefix="archive/archives/probe")

    assert client.uploads == []
    assert client.objects == {"unrelated/object"}
