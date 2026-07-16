from __future__ import annotations

from pathlib import Path

import pytest

from riverhog_core.domain.errors import NotFound
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.stores import tusd_upload_store
from riverhog_core.stores.tusd_upload_store import TusdUploadStore
from riverhog_core.tusd_ids import tusd_upload_id_for_target_path
from tests.unit.db_helpers import sqlite_url


def _config(tmp_path: Path, **overrides: object) -> RuntimeConfig:
    return RuntimeConfig(
        hot_store_endpoint_url="http://example.invalid:9000",
        hot_store_region="us-east-1",
        hot_store_bucket="riverhog",
        hot_store_access_key_id="test-access",
        hot_store_secret_access_key="test-secret",
        hot_store_force_path_style=True,
        tusd_base_url="http://example.invalid:1080/files",
        tusd_hook_secret="hook-secret",
        database_url=sqlite_url(tmp_path / "state.sqlite3"),
        upload_staging_root=tmp_path / "uploads",
        **overrides,
    )


def test_filesystem_tusd_upload_store_reads_ranges_from_shared_tusd_directory(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = TusdUploadStore(config)
    target_path = ".riverhog/uploads/collections/2025/demo/video.mov"
    upload_path = config.upload_staging_root / tusd_upload_id_for_target_path(target_path)
    upload_path.parent.mkdir(parents=True)
    upload_path.write_bytes(b"0123456789")

    assert store.read_target(target_path) == b"0123456789"
    assert b"".join(store.iter_target(target_path, offset=2, size=4)) == b"2345"


def test_filesystem_tusd_upload_store_deletes_local_target_and_sidecars(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = TusdUploadStore(config)
    target_path = ".riverhog/uploads/collections/2025/demo/video.mov"
    upload_path = config.upload_staging_root / tusd_upload_id_for_target_path(target_path)
    upload_path.parent.mkdir(parents=True)
    upload_path.write_bytes(b"content")
    upload_path.with_name(f"{upload_path.name}.info").write_text("{}", encoding="utf-8")
    upload_path.with_name(f"{upload_path.name}.part").write_bytes(b"partial")

    store.delete_target(target_path)

    assert not upload_path.exists()
    assert not upload_path.with_name(f"{upload_path.name}.info").exists()
    assert not upload_path.with_name(f"{upload_path.name}.part").exists()


def test_filesystem_tusd_upload_store_raises_not_found_for_missing_target(
    tmp_path: Path,
) -> None:
    store = TusdUploadStore(_config(tmp_path))

    with pytest.raises(NotFound, match="upload target not found"):
        store.read_target(".riverhog/uploads/collections/2025/demo/missing.mov")


def test_tusd_upload_store_reuses_http_clients_by_request_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[FakeClient] = []

    class FakeResponse:
        def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
            self.status_code = status_code
            self.headers = headers or {}

        def raise_for_status(self) -> None:
            return

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout
            self.closed = False
            instances.append(self)

        def post(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse(201, {"Location": "upload-a"})

        def head(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse(204, {"Upload-Offset": "3"})

        def patch(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse(204, {"Upload-Offset": "4"})

        def delete(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse(204)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(tusd_upload_store.httpx, "Client", FakeClient)
    store = TusdUploadStore(_config(tmp_path))

    assert store.create_upload(".riverhog/uploads/collections/demo/a.webm", 10).endswith(
        "/files/upload-a"
    )
    assert store.get_offset("http://example.invalid:1080/files/upload-a") == 3
    store.cancel_upload("http://example.invalid:1080/files/upload-a")

    assert len(instances) == 1
    assert instances[0].timeout == 300.0

    offset, _expires = store.append_upload_chunk(
        "http://example.invalid:1080/files/upload-a",
        offset=0,
        checksum="sha256 abc",
        content=b"data",
    )
    assert offset == 4
    store.append_upload_chunk(
        "http://example.invalid:1080/files/upload-a",
        offset=4,
        checksum="sha256 def",
        content=b"more",
    )

    assert len(instances) == 2
    assert instances[1].timeout == store._append_timeout_seconds
