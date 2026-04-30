from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from arc_core.runtime_config import RuntimeConfig
from arc_core.stores.s3_archive_store import ISO_BYTES_METADATA, ISO_SHA256_METADATA, S3ArchiveStore


class _MissingObjectError(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "404"}}


class _FakeS3Client:
    def __init__(self, *, existing_head: dict[str, object] | None) -> None:
        self._existing_head = existing_head
        self.uploaded: list[tuple[str, str, str, dict[str, object]]] = []

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        if self._existing_head is None:
            raise _MissingObjectError()
        return self._existing_head

    def upload_file(
        self,
        filename: str,
        bucket: str,
        key: str,
        *,
        ExtraArgs: dict[str, object],
    ) -> None:
        self.uploaded.append((filename, bucket, key, ExtraArgs))
        metadata = ExtraArgs.get("Metadata", {})
        assert isinstance(metadata, dict)
        self._existing_head = {
            "ContentLength": Path(filename).stat().st_size,
            "LastModified": datetime(2026, 4, 20, 4, 1, 0, tzinfo=UTC),
            "Metadata": metadata,
        }


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


def test_upload_finalized_image_reuses_existing_object_without_reupload(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client(
        existing_head={
            "ContentLength": 456,
            "LastModified": datetime(2026, 4, 20, 4, 1, 0, tzinfo=UTC),
            "Metadata": {
                ISO_BYTES_METADATA: "456",
                ISO_SHA256_METADATA: "a" * 64,
            },
        }
    )
    monkeypatch.setattr(
        "arc_core.stores.s3_archive_store.create_glacier_s3_client",
        lambda config: client,
    )
    config = _config(tmp_path)
    store = S3ArchiveStore(config)

    receipt = store.upload_finalized_image(
        image_id="20260420T040001Z",
        filename="20260420T040001Z.iso",
        image_root=tmp_path,
    )

    assert receipt.object_path == "glacier/finalized-images/20260420T040001Z/20260420T040001Z.iso"
    assert receipt.stored_bytes == 456
    assert receipt.uploaded_at == "2026-04-20T04:01:00Z"
    assert receipt.verified_at is not None
    assert client.uploaded == []


def test_upload_finalized_image_rejects_existing_object_without_validation_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client(
        existing_head={
            "ContentLength": 456,
            "LastModified": datetime(2026, 4, 20, 4, 1, 0, tzinfo=UTC),
        }
    )
    monkeypatch.setattr(
        "arc_core.stores.s3_archive_store.create_glacier_s3_client",
        lambda config: client,
    )
    store = S3ArchiveStore(_config(tmp_path))

    with pytest.raises(RuntimeError, match="missing ISO validation metadata"):
        store.upload_finalized_image(
            image_id="20260420T040001Z",
            filename="20260420T040001Z.iso",
            image_root=tmp_path,
        )


def test_upload_finalized_image_uploads_when_object_is_missing(monkeypatch, tmp_path: Path) -> None:
    client = _FakeS3Client(existing_head=None)
    monkeypatch.setattr(
        "arc_core.stores.s3_archive_store.create_glacier_s3_client",
        lambda config: client,
    )

    def _fake_subprocess_run(*args, stdout, stderr, check):  # type: ignore[no-untyped-def]
        stdout.write(b"iso-bytes")

        class _Result:
            returncode = 0
            stderr = b""

        return _Result()

    monkeypatch.setattr("arc_core.stores.s3_archive_store.subprocess.run", _fake_subprocess_run)
    validated: list[Path] = []
    monkeypatch.setattr(
        "arc_core.stores.s3_archive_store.validate_iso_image",
        lambda iso_path: validated.append(iso_path),
    )
    monkeypatch.setattr(
        "arc_core.stores.s3_archive_store.build_iso_cmd_from_root",
        lambda *, image_root, volume_id: ["xorriso", str(image_root), volume_id],
    )
    monkeypatch.setattr("arc_core.stores.s3_archive_store._utc_now", lambda: "2026-04-20T04:01:00Z")

    config = _config(tmp_path)
    store = S3ArchiveStore(config)

    receipt = store.upload_finalized_image(
        image_id="20260420T040001Z",
        filename="20260420T040001Z.iso",
        image_root=tmp_path,
    )

    assert receipt.object_path == "glacier/finalized-images/20260420T040001Z/20260420T040001Z.iso"
    assert receipt.stored_bytes == len(b"iso-bytes")
    assert receipt.uploaded_at == "2026-04-20T04:01:00Z"
    assert receipt.verified_at == "2026-04-20T04:01:00Z"
    assert len(client.uploaded) == 1
    assert len(validated) == 1
    metadata = client.uploaded[0][3]["Metadata"]
    assert isinstance(metadata, dict)
    assert metadata[ISO_BYTES_METADATA] == str(len(b"iso-bytes"))
    assert metadata[ISO_SHA256_METADATA] == (
        "4bc485f29c8bda3640b8d904070e38e722d7acd9cba16f7a0ea8bedce2528178"
    )


def test_upload_finalized_image_fails_before_upload_when_iso_validation_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client(existing_head=None)
    monkeypatch.setattr(
        "arc_core.stores.s3_archive_store.create_glacier_s3_client",
        lambda config: client,
    )

    def _fake_subprocess_run(*args, stdout, stderr, check):  # type: ignore[no-untyped-def]
        stdout.write(b"not-an-iso")

        class _Result:
            returncode = 0
            stderr = b""

        return _Result()

    monkeypatch.setattr("arc_core.stores.s3_archive_store.subprocess.run", _fake_subprocess_run)

    def _fail_validation(iso_path: Path) -> None:
        raise RuntimeError("invalid ISO")

    monkeypatch.setattr("arc_core.stores.s3_archive_store.validate_iso_image", _fail_validation)

    store = S3ArchiveStore(_config(tmp_path))

    with pytest.raises(RuntimeError, match="invalid ISO"):
        store.upload_finalized_image(
            image_id="20260420T040001Z",
            filename="20260420T040001Z.iso",
            image_root=tmp_path,
        )
    assert client.uploaded == []
