from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from arc_core.runtime_config import RuntimeConfig
from tests.harness import configure_garage


class _FakeS3Client:
    def __init__(self) -> None:
        self.put_buckets: list[str] = []
        self.lifecycle_by_bucket: dict[str, dict[str, object]] = {}

    def put_bucket_lifecycle_configuration(
        self,
        *,
        Bucket: str,
        LifecycleConfiguration: dict[str, object],
    ) -> None:
        self.put_buckets.append(Bucket)
        self.lifecycle_by_bucket[Bucket] = LifecycleConfiguration

    def get_bucket_lifecycle_configuration(self, *, Bucket: str) -> dict[str, object]:
        return self.lifecycle_by_bucket[Bucket]


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


def test_lifecycle_targets_uses_archive_bucket_when_distinct(tmp_path: Path) -> None:
    config = _config(tmp_path, glacier_bucket="riverhog-archive")
    hot_client = _FakeS3Client()
    archive_client = _FakeS3Client()

    original_hot = configure_garage.create_s3_client
    original_archive = configure_garage.create_glacier_s3_client
    configure_garage.create_s3_client = lambda current: hot_client  # type: ignore[assignment]
    configure_garage.create_glacier_s3_client = (  # type: ignore[assignment]
        lambda current: archive_client
    )
    try:
        targets = configure_garage._lifecycle_targets(config)
    finally:
        configure_garage.create_s3_client = original_hot  # type: ignore[assignment]
        configure_garage.create_glacier_s3_client = original_archive  # type: ignore[assignment]

    assert targets == [
        (hot_client, "riverhog"),
        (archive_client, "riverhog-archive"),
    ]


def test_configure_bucket_lifecycle_verifies_expected_payload() -> None:
    client = _FakeS3Client()

    configure_garage._configure_bucket_lifecycle(client=client, bucket="riverhog-archive")

    assert client.put_buckets == ["riverhog-archive"]
    assert client.lifecycle_by_bucket["riverhog-archive"] == (
        configure_garage.EXPECTED_LIFECYCLE_CONFIGURATION
    )
