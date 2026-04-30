from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from arc_core.collection_archives import (
    CollectionArchiveFile,
    CollectionArchivePackage,
    build_collection_archive_package,
)
from arc_core.runtime_config import RuntimeConfig
from arc_core.stores.s3_archive_store import (
    COLLECTION_BYTES_METADATA,
    COLLECTION_SHA256_METADATA,
    S3ArchiveStore,
)
from tests.fixtures.data import DOCS_FILES


class _MissingObjectError(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "404"}}


class _FakeBody:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.closed = False

    def iter_chunks(self, *, chunk_size: int) -> Iterator[bytes]:
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset : offset + chunk_size]

    def read(self) -> bytes:
        return self._content

    def close(self) -> None:
        self.closed = True


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.restore_requests: list[str] = []

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        _ = Bucket
        try:
            return {key: value for key, value in self.objects[Key].items() if key != "Body"}
        except KeyError as exc:
            raise _MissingObjectError() from exc

    def put_object(self, *, Bucket: str, Key: str, Body: object, **kwargs: Any) -> None:
        _ = Bucket
        if isinstance(Body, bytes):
            body = Body
        else:
            read = Body.read
            body = cast(bytes, read())
        self.objects[Key] = {
            "Body": body,
            "ContentLength": len(body),
            "LastModified": datetime(2026, 4, 20, 4, 1, 0, tzinfo=UTC),
            **kwargs,
        }

    def restore_object(
        self,
        *,
        Bucket: str,
        Key: str,
        RestoreRequest: dict[str, object],
    ) -> None:
        _ = Bucket, RestoreRequest
        self.restore_requests.append(Key)
        self.objects[Key]["Restore"] = 'ongoing-request="true"'

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        _ = Bucket
        return {"Body": _FakeBody(cast(bytes, self.objects[Key]["Body"]))}


def _config(tmp_path: Path, **overrides: object) -> RuntimeConfig:
    config = RuntimeConfig(
        object_store="s3",
        s3_endpoint_url="http://example.invalid:9000",
        s3_region="us-east-1",
        s3_bucket="riverhog",
        s3_access_key_id="test-access",
        s3_secret_access_key="test-secret",
        s3_force_path_style=True,
        tusd_base_url="http://example.invalid:1080/files",
        tusd_hook_secret="hook-secret",
        sqlite_path=tmp_path / "state.sqlite3",
    )
    return replace(config, **overrides)


def _package() -> CollectionArchivePackage:
    return build_collection_archive_package(
        collection_id="docs",
        files=tuple(
            CollectionArchiveFile(
                path=path,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            )
            for path, content in sorted(DOCS_FILES.items())
        ),
    )


def _store_with_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: _FakeS3Client,
    **config_overrides: object,
) -> S3ArchiveStore:
    monkeypatch.setattr(
        "arc_core.stores.s3_archive_store.create_glacier_s3_client",
        lambda config: client,
    )
    return S3ArchiveStore(_config(tmp_path, **config_overrides))


def test_upload_collection_archive_package_uploads_archive_manifest_and_proof(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(monkeypatch, tmp_path, client)
    package = _package()

    receipt = store.upload_collection_archive_package(collection_id="docs", package=package)

    assert receipt.archive.object_path.endswith("/archive.tar")
    assert receipt.manifest.object_path.endswith("/manifest.yml")
    assert receipt.proof.object_path.endswith("/manifest.yml.ots")
    assert receipt.archive_format == "tar"
    assert receipt.compression == "none"
    archive_head = client.objects[receipt.archive.object_path]
    archive_metadata = archive_head["Metadata"]
    assert archive_metadata[COLLECTION_BYTES_METADATA] == str(len(package.archive_bytes))
    assert archive_metadata[COLLECTION_SHA256_METADATA] == package.archive_sha256
    assert archive_metadata["arc-archive-format"] == "tar"
    assert archive_metadata["arc-compression"] == "none"


def test_request_collection_archive_restore_requests_all_package_objects(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(
        monkeypatch,
        tmp_path,
        client,
        glacier_backend="aws",
        glacier_endpoint_url="https://s3.us-west-2.amazonaws.com",
        glacier_storage_class="DEEP_ARCHIVE",
    )
    package = _package()
    receipt = store.upload_collection_archive_package(collection_id="docs", package=package)

    status = store.request_collection_archive_restore(
        collection_id="docs",
        object_path=receipt.archive.object_path,
        manifest_object_path=receipt.manifest.object_path,
        proof_object_path=receipt.proof.object_path,
        retrieval_tier="bulk",
        hold_days=1,
        requested_at="2026-04-20T04:00:00Z",
        estimated_ready_at="2026-04-22T04:00:00Z",
    )

    assert status.state == "requested"
    assert client.restore_requests == [
        receipt.archive.object_path,
        receipt.manifest.object_path,
        receipt.proof.object_path,
    ]


def test_iter_restored_collection_archive_streams_when_ready(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    store = _store_with_client(monkeypatch, tmp_path, client)
    package = _package()
    receipt = store.upload_collection_archive_package(collection_id="docs", package=package)

    chunks = list(
        store.iter_restored_collection_archive(
            collection_id="docs",
            object_path=receipt.archive.object_path,
        )
    )

    assert b"".join(chunks) == package.archive_bytes
