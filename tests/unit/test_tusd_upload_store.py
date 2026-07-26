from __future__ import annotations

from pathlib import Path

import pytest
from riverhog_core.runtime_config import IngressStoreConfig, RuntimeConfig
from riverhog_core.stores import tusd_upload_store
from riverhog_core.stores.tusd_upload_store import TusdUploadStore

from tests.unit.db_helpers import sqlite_url


class EmptyPaginator:
    def paginate(self, **_kwargs: object) -> list[dict[str, object]]:
        return []


class EmptyS3:
    def get_paginator(self, _name: str) -> EmptyPaginator:
        return EmptyPaginator()


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        ingress_store=IngressStoreConfig(
            endpoint_url="https://ingress.example.invalid",
            region="us-east-1",
            bucket="riverhog-ingress",
            access_key_id="test-access",
            secret_access_key="test-secret",
        ),
        ingress_secret_key="test ingress envelope secret key 1234567890",
        tusd_base_url="http://example.invalid:1080/files",
        tusd_hook_secret="hook-secret",
        database_url=sqlite_url(tmp_path / "state.sqlite3"),
    )


def test_ingress_store_reads_only_the_requested_object_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class Body:
        def iter_chunks(self, *, chunk_size: int):
            assert chunk_size == 1024 * 1024
            yield b"2345"

        def close(self) -> None:
            return

    class S3(EmptyS3):
        def get_object(self, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"Body": Body()}

    monkeypatch.setattr(tusd_upload_store, "create_ingress_s3_client", lambda *_: S3())
    store = TusdUploadStore(_config(tmp_path))

    assert b"".join(store.iter_target("collection/file.mov", offset=2, size=4)) == b"2345"
    assert calls == [
        {
            "Bucket": "riverhog-ingress",
            "Key": store._object_key(store._upload_id("collection/file.mov")),
            "Range": "bytes=2-5",
        }
    ]


def test_ingress_deletion_removes_every_exact_object_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_deleted: list[dict[str, object]] = []
    deleted: list[dict[str, object]] = []

    class Paginator:
        def paginate(self, **kwargs: object):
            key = str(kwargs["Prefix"])
            return [
                {
                    "Versions": [
                        {"Key": key, "VersionId": "v1"},
                        {"Key": f"{key}-neighbor", "VersionId": "v2"},
                    ]
                }
            ]

    class S3:
        def get_paginator(self, name: str) -> Paginator:
            assert name == "list_object_versions"
            return Paginator()

        def delete_objects(self, **kwargs: object) -> None:
            deleted.append(kwargs)

        def delete_object(self, **kwargs: object) -> None:
            current_deleted.append(kwargs)

    monkeypatch.setattr(tusd_upload_store, "create_ingress_s3_client", lambda *_: S3())
    store = TusdUploadStore(_config(tmp_path))

    store.delete_target("collection/file.mov")

    assert len(current_deleted) == 3
    assert len(deleted) == 3
    assert all(len(call["Delete"]["Objects"]) == 1 for call in deleted)  # type: ignore[index]


def test_tusd_control_and_append_requests_reuse_persistent_clients(
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
            instances.append(self)

        def post(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse(201, {"Location": "upload-a"})

        def head(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse(204, {"Upload-Offset": "3"})

        def patch(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse(204, {"Upload-Offset": "4"})

        def close(self) -> None:
            return

    monkeypatch.setattr(tusd_upload_store.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        tusd_upload_store,
        "create_ingress_s3_client",
        lambda *_: EmptyS3(),
    )
    store = TusdUploadStore(_config(tmp_path))

    store.create_upload("collection/a.webm", 10)
    assert store.get_offset("http://example.invalid:1080/files/upload-a") == 3
    store.append_upload_chunk(
        "http://example.invalid:1080/files/upload-a",
        offset=0,
        checksum="sha256 abc",
        content=b"data",
    )
    store.append_upload_chunk(
        "http://example.invalid:1080/files/upload-a",
        offset=4,
        checksum="sha256 def",
        content=b"more",
    )

    assert len(instances) == 2
    assert instances[0].timeout == 300.0
    assert instances[1].timeout == store._append_timeout_seconds
